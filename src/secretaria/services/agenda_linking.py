"""Links `professionals` rows of one real person into one `agendas` row (TASK-083, spec §6).

## Identity

brain-api is the single source of identity. Its `professional-accounts` answer
(`services/brain_professionals.py::fetch_professional_accounts`) says which brain-api ACCOUNT each
of a tenant's professionals is; two professionals (any tenants) with the same account are the same
person and get the same `Agenda` (`agendas.person_key = account_id`, UNIQUE). Nothing is ever linked
by e-mail, name or anything a client sent; the hub never passes an `agenda_id`/`person_key` in.

## Failure is silence

A brain-api failure (unconfigured, timeout, 5xx, bad body) links nothing and raises nothing: the
professional simply keeps no `agenda_id`, which is exactly today's behaviour (spec §3). The
`try_link_professional` trigger additionally swallows ANY exception, because it runs on the
onboarding path of a professional that is already saved and must never be undone by a linking
problem. A later `POST /tenants/me/agendas/sync` or `scripts/reconcile_agendas.py` catches up.

A professional brain-api does not list (no user bound yet, or the user unbound) is left exactly as
it is - an absent row is not proof that the person changed.

Callers own the transaction: `ensure_agenda_for_professional` and `sync_tenant_agendas` only flush;
`try_link_professional` and `reconcile_all_tenants` commit.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from secretaria.core.logging import get_logger
from secretaria.models import Agenda, Professional, Tenant
from secretaria.services.brain_professionals import fetch_professional_accounts

logger = get_logger(__name__)


@dataclass(frozen=True)
class AgendaSyncResult:
    """What one tenant looks like after a sync. Counts only.

    linked:   the tenant's professionals that belong to an agenda;
    shared:   of those, how many share it with at least one other professional (any tenant);
    changed:  professionals newly linked (or moved to another agenda) by THIS call;
    brain_ok: False when brain-api could not be asked (nothing was changed).
    """

    linked: int
    shared: int
    changed: int
    brain_ok: bool


async def _person_lock(session: AsyncSession, person_key: str) -> None:
    """Serialize concurrent linkers of ONE account until the transaction ends (Postgres only).

    Two clinics syncing the same person at once would otherwise both miss the `agendas` row and one
    would die on the UNIQUE `person_key`. A lock (not a SAVEPOINT retry) keeps the caller's
    transaction intact. SQLite (the test engine) has one writer at a time.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.blake2b(f"agenda_person:{person_key}".encode(), digest_size=8).digest()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int.from_bytes(digest, "big", signed=True)},
    )


async def ensure_agenda_for_professional(
    session: AsyncSession, professional: Professional, account_id: str
) -> Agenda:
    """Get-or-create `Agenda(person_key=account_id)` and point the professional at it.

    Idempotent and race-safe (see `_person_lock`). A professional already on another agenda is
    MOVED to this one: brain-api's account is the truth, and a stale link would keep two different
    people sharing a calendar. Flushes, does not commit.
    """
    await _person_lock(session, account_id)
    agenda = await session.scalar(select(Agenda).where(Agenda.person_key == account_id))
    if agenda is None:
        agenda = Agenda(person_key=account_id)
        session.add(agenda)
        await session.flush()
    professional.agenda_id = agenda.id
    await session.flush()
    return agenda


async def _tenant_counts(session: AsyncSession, tenant_id: UUID) -> tuple[int, int]:
    """`(linked, shared)` for one tenant's professionals, from the database as it is now."""
    sibling = aliased(Professional)
    linked = await session.scalar(
        select(func.count())
        .select_from(Professional)
        .where(Professional.tenant_id == tenant_id, Professional.agenda_id.is_not(None))
    )
    shared = await session.scalar(
        select(func.count())
        .select_from(Professional)
        .where(
            Professional.tenant_id == tenant_id,
            Professional.agenda_id.is_not(None),
            exists().where(
                sibling.agenda_id == Professional.agenda_id, sibling.id != Professional.id
            ),
        )
    )
    return int(linked or 0), int(shared or 0)


async def sync_tenant_agendas(
    session: AsyncSession, tenant_id: UUID, *, only_professional_id: UUID | None = None
) -> AgendaSyncResult:
    """Link the tenant's professionals to their agendas (ONE brain-api call). Never raises on a
    brain-api failure; flushes, does not commit.

    `only_professional_id` restricts the writes to that professional (the attach trigger); the
    returned counts always describe the whole tenant. A tenant with no professionals costs no call.
    """
    stmt = select(Professional).where(Professional.tenant_id == tenant_id)
    if only_professional_id is not None:
        stmt = stmt.where(Professional.id == only_professional_id)
    professionals = list(await session.scalars(stmt))

    changed = 0
    brain_ok = True
    if professionals:
        accounts = await fetch_professional_accounts(tenant_id)
        if accounts is None:
            brain_ok = False
        else:
            for professional in professionals:
                account_id = accounts.get(str(professional.id))
                if not account_id:
                    continue
                before = professional.agenda_id
                agenda = await ensure_agenda_for_professional(session, professional, account_id)
                if before != agenda.id:
                    changed += 1

    linked, shared = await _tenant_counts(session, tenant_id)
    logger.info(
        "agenda_sync_done",
        tenant_id=str(tenant_id),
        linked=linked,
        shared=shared,
        changed=changed,
        brain_ok=brain_ok,
    )
    return AgendaSyncResult(linked=linked, shared=shared, changed=changed, brain_ok=brain_ok)


async def try_link_professional(
    session: AsyncSession, tenant_id: UUID, professional_id: UUID
) -> None:
    """Best-effort link of ONE just-created/attached professional; swallows everything.

    Commits on success. On ANY failure it rolls back its own work and returns: the professional
    was already saved by the caller and keeps working without an agenda.
    """
    try:
        await sync_tenant_agendas(session, tenant_id, only_professional_id=professional_id)
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - linking must never break onboarding
        await session.rollback()
        logger.warning(
            "agenda_link_failed",
            tenant_id=str(tenant_id),
            professional_id=str(professional_id),
            error_type=type(exc).__name__,
        )


async def reconcile_all_tenants(
    session_factory: Callable[[], AsyncSession], *, dry_run: bool = False
) -> dict[UUID, AgendaSyncResult]:
    """Run `sync_tenant_agendas` for every tenant that has professionals (the backfill).

    Idempotent. One session (transaction) per tenant: a failing tenant is logged and skipped, the
    rest still run. With `dry_run` each tenant's work is rolled back, so the result says what WOULD
    change while nothing is written. Tenants with no professionals are not in the result.
    """
    async with session_factory() as session:
        tenant_ids = list(
            await session.scalars(
                select(Tenant.id)
                .where(exists().where(Professional.tenant_id == Tenant.id))
                .order_by(Tenant.id)
            )
        )

    results: dict[UUID, AgendaSyncResult] = {}
    for tenant_id in tenant_ids:
        async with session_factory() as session:
            try:
                result = await sync_tenant_agendas(session, tenant_id)
                if dry_run:
                    await session.rollback()
                else:
                    await session.commit()
            except Exception as exc:  # noqa: BLE001 - one bad tenant must not stop the backfill
                await session.rollback()
                logger.warning(
                    "agenda_reconcile_tenant_failed",
                    tenant_id=str(tenant_id),
                    error_type=type(exc).__name__,
                )
                continue
        results[tenant_id] = result
    return results
