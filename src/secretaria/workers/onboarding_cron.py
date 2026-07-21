"""Onboarding lifecycle crons (cross-service contract v1 §11): retry nudges,
D+30 manual-review flag, D+60 closing email, config reminders, and monthly
billable-patient + active-professional usage metering.

Registered in `workers/arq_worker.py` (own module, mirroring the
`plugins/reminders.py` / `plugins/post_booking.py` split — these two jobs are
NOT addon-gated plugins, but the same "job body lives next to its own
concerns, arq_worker.py just imports + registers" shape applies).

`run_onboarding_nudges` is a thin per-tenant loop: all state (onboarding
state/blocker/anchors/timestamps) lives on brain-api's `tenants` table — this
service only READS the tenant list (`services/brain_onboarding.py`) and its
OWN local `Tenant.timezone`, and WRITES nothing locally. Every cadence/due
decision is delegated to the pure functions in `workers/onboarding_cadence.py`
so they are unit-testable without a DB, network, or event loop.

`run_patient_usage_metering` is the one local-DB-heavy job: it tallies real
Appointment rows (billable patients) AND active Professional rows (active
professionals) per tenant and reports both to brain-api's existing
usage-events endpoint (`services/usage_events.py`, already idempotent on
`event_id`), so a daily re-run of the same month is free.

Both jobs wrap EACH tenant's processing in its own try/except — one tenant
with unexpected/malformed data must never abort the sweep for every other
tenant (contract v1 §11: "per-tenant try/except, structured logs, no PII in
logs"). Recipient email ADDRESSES are never logged — only tenant_id and,
where useful, the template/event name (the stricter of the two logging
patterns already in this codebase; compare workers/tasks.py's
`send_transactional_email`, which logs `template`+`sent` but never `to`, to
services/email.py's older alert path, which does log the address).
"""

from collections import Counter
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from secretaria.config import Settings, get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Professional, Tenant
from secretaria.services.brain_onboarding import (
    OnboardingTenant,
    list_onboarding_tenants,
    post_onboarding_event,
)
from secretaria.services.email import send_transactional_email_message
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.usage_events import emit_usage_event
from secretaria.workers.onboarding_cadence import (
    config_reminder_due,
    days_elapsed,
    next_retry_time,
    within_send_window,
)

logger = get_logger(__name__)

# --------------------------------------------------------------------------
# run_onboarding_nudges
# --------------------------------------------------------------------------

# States a tenant must be in to receive AUTOMATIC retry nudges / D+30 flag /
# D+60 closing email. `aguardando_acao_manual` (manual-action blockers) is
# deliberately excluded — those tenants need the doctor to act, not a nudge.
_RETRY_ELIGIBLE_STATES = frozenset({"aquecimento", "aguardando_elegibilidade"})
# Manual-action-only blockers (numero_em_outro_bsp, sem_acesso_admin_waba,
# sem_pagina_facebook) are excluded from the auto-retry population — None
# included so a tenant with no blocker at all is eligible.
_RETRY_ELIGIBLE_BLOCKERS = frozenset({None, "atividade_insuficiente", "outro"})
_DEFAULT_TENANT_TIMEZONE = "America/Sao_Paulo"


def _tenant_zoneinfo(tenant: Tenant) -> ZoneInfo:
    """The tenant's own IANA tz, falling back to the shipped default on a bad value."""
    try:
        return ZoneInfo(tenant.timezone or _DEFAULT_TENANT_TIMEZONE)
    except Exception:
        return ZoneInfo(_DEFAULT_TENANT_TIMEZONE)


async def _send_onboarding_email(item: OnboardingTenant, template: str) -> bool:
    """Send one onboarding template to the tenant owner. Never logs the address."""
    if not item.owner_email:
        logger.warning(
            "onboarding_email_skipped_no_owner_email",
            tenant_id=str(item.tenant_id),
            template=template,
        )
        return False
    return await send_transactional_email_message(
        to=item.owner_email,
        template=template,
        variables={"clinic_name": item.clinic_name or ""},
    )


async def _process_retry_and_closing(
    item: OnboardingTenant, settings: Settings, now_utc: datetime, in_window: bool
) -> Counter:
    """Retry nudges + D+30 manual-review flag + D+60 closing email for one tenant.

    All three share the same eligible population (contract v1 §11); the D+30
    flag is NOT gated by the send window — it posts no message to anyone, it
    only marks the tenant for ops follow-up, so there is no reason to delay it
    to business hours. The retry-nudge send and the closing email ARE gated
    (they are outbound emails to the tenant owner).
    """
    counts: Counter = Counter()
    eligible = (
        item.subscription_active
        and item.onboarding_state in _RETRY_ELIGIBLE_STATES
        and item.blocker_reason in _RETRY_ELIGIBLE_BLOCKERS
        and not item.retry_paused
    )
    if not eligible or item.onboarding_anchor_at is None:
        return counts

    anchor = item.onboarding_anchor_at
    elapsed = days_elapsed(anchor, now_utc)

    if (
        in_window
        and item.next_retry_at is not None
        and item.next_retry_at <= now_utc
    ):
        template = f"retry_nudge_{item.blocker_reason or 'atividade_insuficiente'}"
        sent = await _send_onboarding_email(item, template)
        next_retry = next_retry_time(
            anchor,
            now_utc,
            settings.RETRY_CADENCE_DAYS,
            settings.RETRY_BIWEEKLY_DAYS,
            settings.RETRY_WINDOW_TOTAL_DAYS,
        )
        applied = await post_onboarding_event(
            item.tenant_id, "retry_nudge_sent", now_utc, next_retry
        )
        counts["retry_nudge_sent"] += 1
        logger.info(
            "onboarding_retry_nudge_processed",
            tenant_id=str(item.tenant_id),
            template=template,
            email_sent=sent,
            event_applied=applied,
        )

    if elapsed >= 30 and item.manual_review_flagged_at is None:
        applied = await post_onboarding_event(
            item.tenant_id, "manual_review_flagged", now_utc, None
        )
        counts["manual_review_flagged"] += 1
        logger.info(
            "onboarding_manual_review_flagged",
            tenant_id=str(item.tenant_id),
            event_applied=applied,
        )

    if (
        in_window
        and elapsed >= settings.RETRY_WINDOW_TOTAL_DAYS
        and item.closing_email_sent_at is None
    ):
        sent = await _send_onboarding_email(item, "closing_email")
        applied = await post_onboarding_event(item.tenant_id, "closing_email_sent", now_utc, None)
        counts["closing_email_sent"] += 1
        logger.info(
            "onboarding_closing_email_processed",
            tenant_id=str(item.tenant_id),
            email_sent=sent,
            event_applied=applied,
        )

    return counts


async def _process_config_reminder(
    item: OnboardingTenant, settings: Settings, now_utc: datetime, in_window: bool
) -> Counter:
    """Config reminders for one tenant — independent of onboarding_state/blocker.

    NOTE (spec-internal edge, flagged not solved): a tenant with
    config_status=='completa' stops receiving reminders even when it has
    UNFINISHED additional professionals (the >=11-professional partial-
    activation rule only requires ONE complete professional for
    config_status to read 'completa' — see services/tenant_config.py
    professional_completeness). Those still-unconfigured professionals get no
    further nudge from this cron.
    """
    counts: Counter = Counter()
    eligible = (
        item.subscription_active
        and item.config_status != "completa"
        and not item.config_reminder_paused
    )
    if not eligible or item.config_reminder_anchor_at is None or not in_window:
        return counts

    due = config_reminder_due(
        item.config_reminder_anchor_at,
        item.last_config_reminder_at,
        now_utc,
        settings.CONFIG_REMINDER_CADENCE_DAYS,
        settings.CONFIG_REMINDER_WEEKLY_DAYS,
    )
    if not due:
        return counts

    template = (
        "config_reminder_connected"
        if item.onboarding_state == "conectado"
        else "config_reminder_pre_connection"
    )
    sent = await _send_onboarding_email(item, template)
    applied = await post_onboarding_event(item.tenant_id, "config_reminder_sent", now_utc, None)
    counts["config_reminder_sent"] += 1
    logger.info(
        "onboarding_config_reminder_processed",
        tenant_id=str(item.tenant_id),
        template=template,
        email_sent=sent,
        event_applied=applied,
    )
    return counts


async def run_onboarding_nudges(ctx: dict) -> None:
    """arq cron: hourly at :10 (contract v1 §11).

    Pulls the onboarding tenant list from brain-api ONCE per sweep (fail-soft:
    an unreachable/misconfigured brain-api logs and returns — there is nothing
    to do without the list). Each item is matched to its LOCAL `Tenant` row
    (same id — contract v1 §0) purely to read `timezone`; a tenant absent
    locally is skipped and logged (it cannot happen in a consistent deploy,
    but a cron must never crash on a stale/partial provisioning race).
    """
    settings = get_settings()
    items = await list_onboarding_tenants()
    if items is None:
        logger.warning("onboarding_nudges_pull_failed")
        return
    if not items:
        return

    now_utc = datetime.now(UTC)
    totals: Counter = Counter()
    tenants_processed = 0
    async with async_session_factory() as session:
        for item in items:
            try:
                tenant = await session.get(Tenant, item.tenant_id)
                if tenant is None:
                    logger.info(
                        "onboarding_nudge_tenant_missing_locally", tenant_id=str(item.tenant_id)
                    )
                    continue

                now_local = now_utc.astimezone(_tenant_zoneinfo(tenant))
                in_window = within_send_window(now_local, settings.NUDGE_SEND_WINDOW)

                totals.update(
                    await _process_retry_and_closing(item, settings, now_utc, in_window)
                )
                totals.update(await _process_config_reminder(item, settings, now_utc, in_window))
                tenants_processed += 1
            except Exception as exc:
                logger.warning(
                    "onboarding_nudge_tenant_failed",
                    error=str(exc),
                    tenant_id=str(item.tenant_id),
                )

    logger.info("onboarding_nudges_swept", tenants=tenants_processed, **totals)


# --------------------------------------------------------------------------
# run_patient_usage_metering
# --------------------------------------------------------------------------


def _month_bounds_utc(now_local: datetime) -> tuple[datetime, datetime, str]:
    """(month_start_utc, month_end_utc, 'YYYY-MM') for `now_local`'s calendar
    month, computed in `now_local`'s own tz then converted to UTC for the DB
    range query (Appointment.start_at is stored/queried as UTC-equivalent
    elsewhere in this codebase — see plugins/reminders.py). `.replace(...)` on
    a ZoneInfo-aware datetime re-resolves the UTC offset for the new
    wall-clock fields, so this stays correct even for a DST-observing tz.
    """
    month_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0, day=1)
    if month_start_local.month == 12:
        month_end_local = month_start_local.replace(year=month_start_local.year + 1, month=1)
    else:
        month_end_local = month_start_local.replace(month=month_start_local.month + 1)
    return (
        month_start_local.astimezone(UTC),
        month_end_local.astimezone(UTC),
        now_local.strftime("%Y-%m"),
    )


async def _tally_tenant_month(session, tenant: Tenant, now_local: datetime) -> Counter:
    """Distinct (professional_id, patient_id) pairs with >=1 Appointment this
    month, POSTed as usage events. Returns a Counter of {emitted, skipped_unattributed}.

    EVERY appointment row is a real booking for metering purposes, cancelled
    included: a patient who booked and later cancelled still consumed the
    secretary's work that month (the booking itself, any reminders sent, the
    back-and-forth to get there). There is deliberately no status filter on
    the query below — AppointmentStatus is exactly SCHEDULED/CANCELLED/
    RESCHEDULED/CONFIRMED/ATTENDED/NO_SHOW, so "no filter" already means
    "every status, cancelled included," with nothing left to enumerate.

    A local `seen` set guards against double-POSTing the same event_id
    within one call: the NULL-professional fallback can remap a (None,
    patient_id) row onto the SAME professional_id as another row in `pairs`
    that already named it directly — two distinct SQL tuples that collapse
    to one (professional_id, patient_id) pair, and so to one event_id, only
    after the fallback substitution. Harmless server-side (idempotent on
    event_id) but wasteful, and would otherwise double-count "emitted".
    """
    counts: Counter = Counter()
    month_start_utc, month_end_utc, month_key = _month_bounds_utc(now_local)

    pairs = (
        await session.execute(
            select(Appointment.professional_id, Appointment.patient_id)
            .where(
                Appointment.tenant_id == tenant.id,
                Appointment.patient_id.is_not(None),
                Appointment.start_at.is_not(None),
                Appointment.start_at >= month_start_utc,
                Appointment.start_at < month_end_utc,
            )
            .distinct()
        )
    ).all()
    if not pairs:
        return counts

    # NULL professional_id (legacy/base-tool bookings that never resolved a
    # professional) is only attributable when the tally is unambiguous: the
    # tenant has EXACTLY ONE active professional. Resolved lazily (only when
    # actually needed) and once per tenant, not once per pair.
    fallback_professional_id: UUID | None = None
    if any(professional_id is None for professional_id, _patient_id in pairs):
        active_ids = (
            await session.scalars(
                select(Professional.id).where(
                    Professional.tenant_id == tenant.id,
                    Professional.is_active.is_(True),
                )
            )
        ).all()
        if len(active_ids) == 1:
            fallback_professional_id = active_ids[0]

    seen: set[str] = set()
    for professional_id, patient_id in pairs:
        if professional_id is None:
            if fallback_professional_id is None:
                counts["skipped_unattributed"] += 1
                continue
            professional_id = fallback_professional_id
        event_id = f"bp:{tenant.id}:{professional_id}:{patient_id}:{month_key}"
        if event_id in seen:
            continue
        seen.add(event_id)
        recorded = await emit_usage_event(
            tenant_id=str(tenant.id),
            feature="billable_patients",
            amount=1,
            event_id=event_id,
        )
        if recorded:
            counts["emitted"] += 1

    return counts


async def _tally_active_professionals_month(session, tenant: Tenant, month_key: str) -> Counter:
    """One usage event per ACTIVE Professional row this tenant has right now.

    Sibling of `_tally_tenant_month`, same idempotency shape (`prof:` instead
    of `bp:`), same brain-api endpoint. This is about the professional
    EXISTING and being active — NOT about whether they saw any patients this
    month, so there is no join against Appointment here, unlike the patient
    tally. No proration: active at ANY daily sweep during the month bills
    the full month, the same philosophy the patient tally already follows.

    This is a POINT-IN-TIME snapshot taken once per daily 03:30 UTC run, not
    a continuous observation of the row's history — a professional created
    and deactivated again between two consecutive sweeps is never caught
    active by either one, and so is never billed for that month. That
    sub-day blind spot is accepted as-is; nothing here guarantees billing
    for a professional that was active for only part of a day no sweep
    landed on. Counter key ("professionals_emitted") is deliberately
    distinct from the patient tally's "emitted" so the sweep log's merged
    totals (`usage_metering_swept`) stay distinguishable.
    """
    counts: Counter = Counter()
    active_ids = (
        await session.scalars(
            select(Professional.id).where(
                Professional.tenant_id == tenant.id,
                Professional.is_active.is_(True),
            )
        )
    ).all()

    for professional_id in active_ids:
        event_id = f"prof:{tenant.id}:{professional_id}:{month_key}"
        recorded = await emit_usage_event(
            tenant_id=str(tenant.id),
            feature="active_professionals",
            amount=1,
            event_id=event_id,
        )
        if recorded:
            counts["professionals_emitted"] += 1

    return counts


async def run_patient_usage_metering(ctx: dict) -> None:
    """arq cron: daily at 03:30 UTC (contract v1 §11).

    Iterates every LOCAL tenant (not brain-api's onboarding list — that list
    deliberately excludes fully-onboarded 'ativo'+'completa' tenants, contract
    v1 §7, which is exactly the steady-state billable population). Per-tenant
    `subscription_active` is instead resolved via the SAME entitlement read
    the reminders plugin already uses (services/entitlements_client.py:
    get_entitlements -> EntitlementSummary.active), memoized once per tenant.

    Two tallies share this one sweep — same entitlement gate, same
    tenant-local calendar month: billable patients (`_tally_tenant_month`)
    and active professionals (`_tally_active_professionals_month`). The
    latter deliberately has no cron entry of its own; it piggybacks this job
    rather than registering a second one. The OUTER per-tenant try/except
    below covers only get_entitlements + timezone resolution (a failure
    there means neither tally can run at all). Each tally then gets its OWN
    inner try/except, so one tally's failure (e.g. one poisoned Appointment
    row) can never silently suppress the other for that same tenant — the
    tenant still counts as `tenants_processed` either way.
    """
    redis = ctx.get("redis")
    totals: Counter = Counter()
    tenants_processed = 0
    async with async_session_factory() as session:
        tenants = (await session.scalars(select(Tenant))).all()
        for tenant in tenants:
            try:
                summary = await get_entitlements(tenant.id, redis)
                if summary is None or not summary.active:
                    continue

                now_local = datetime.now(UTC).astimezone(_tenant_zoneinfo(tenant))

                try:
                    totals.update(await _tally_tenant_month(session, tenant, now_local))
                except Exception as exc:
                    logger.warning(
                        "usage_metering_patient_tally_failed",
                        error=str(exc),
                        tenant_id=str(tenant.id),
                    )

                try:
                    month_key = now_local.strftime("%Y-%m")
                    totals.update(
                        await _tally_active_professionals_month(session, tenant, month_key)
                    )
                except Exception as exc:
                    logger.warning(
                        "usage_metering_professionals_tally_failed",
                        error=str(exc),
                        tenant_id=str(tenant.id),
                    )

                tenants_processed += 1
            except Exception as exc:
                logger.warning(
                    "usage_metering_tenant_failed", error=str(exc), tenant_id=str(tenant.id)
                )

    logger.info("usage_metering_swept", tenants=tenants_processed, **totals)
