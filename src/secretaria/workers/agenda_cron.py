"""Shared-agenda reconcile cron (TASK-083, spec §6).

Why a cron: in the real invite flow brain-api binds `users.professional_id` AFTER it calls the
internal attach, so `try_link_professional` (the attach trigger) usually asks brain-api about a
professional that has no account yet and links nothing. This tick catches up without anybody
clicking "sync".

What it does: picks the tenants that still have at least one ACTIVE professional with no
`agenda_id`, and runs `services/agenda_linking.sync_tenant_agendas` for each - one brain-api call
per tenant, each tenant in its OWN session/transaction.

* Bounded: at most `AGENDA_RECONCILE_MAX_TENANTS_PER_RUN` tenants per tick, picked in random order
  so a tenant whose professionals brain-api never returns cannot starve the others forever.
* Tolerant: a brain-api failure links nothing for that tenant (the service returns
  `brain_ok=False`); an unexpected error is logged (tenant id + error type only), sweep goes on.
* Idempotent: a professional brain-api does not list stays NULL and is simply picked again by a
  later tick; a tenant with everything linked is never picked.

Layering: workers -> services. Registered in `workers/arq_worker.py`.
"""

from uuid import UUID

from sqlalchemy import exists, func, select

from secretaria.config import Settings, get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Professional, Tenant
from secretaria.services.agenda_linking import sync_tenant_agendas

logger = get_logger(__name__)

# First minute of the hour the tick fires at; the interval comes from Settings.
AGENDA_RECONCILE_OFFSET_MINUTE = 9


def agenda_reconcile_minutes(settings: Settings | None = None) -> set[int]:
    """The minutes of the hour the cron fires at: every `AGENDA_RECONCILE_INTERVAL_MINUTES`."""
    settings = settings or get_settings()
    interval = min(max(int(settings.AGENDA_RECONCILE_INTERVAL_MINUTES), 1), 60)
    return set(range(AGENDA_RECONCILE_OFFSET_MINUTE % interval, 60, interval))


async def _tenants_with_unlinked_professionals(limit: int) -> list[UUID]:
    async with async_session_factory() as session:
        rows = await session.scalars(
            select(Tenant.id)
            .where(
                exists().where(
                    Professional.tenant_id == Tenant.id,
                    Professional.is_active.is_(True),
                    Professional.agenda_id.is_(None),
                )
            )
            .order_by(func.random())
            .limit(limit)
        )
        return list(rows)


async def run_agenda_reconcile(ctx: dict) -> dict[str, int]:
    """One tick. Returns counts (tenants, changed, brain_unavailable, failed) for arq's job log."""
    limit = max(int(get_settings().AGENDA_RECONCILE_MAX_TENANTS_PER_RUN), 0)
    counts = {"tenants": 0, "changed": 0, "brain_unavailable": 0, "failed": 0}
    if limit == 0:
        return counts
    for tenant_id in await _tenants_with_unlinked_professionals(limit):
        counts["tenants"] += 1
        async with async_session_factory() as session:
            try:
                result = await sync_tenant_agendas(session, tenant_id)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - one tenant must not stop the sweep
                counts["failed"] += 1
                try:
                    await session.rollback()
                except Exception:  # noqa: BLE001, S110 - the session is discarded anyway
                    pass
                logger.warning(
                    "agenda_reconcile_cron_tenant_failed",
                    tenant_id=str(tenant_id),
                    error_type=type(exc).__name__,
                )
                continue
        counts["changed"] += result.changed
        if not result.brain_ok:
            counts["brain_unavailable"] += 1
    if counts["tenants"]:
        logger.info("agenda_reconcile_cron_tick", **counts)
    return counts
