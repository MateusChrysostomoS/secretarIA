"""reminders plugin: proactive appointment-reminder cron sweep.

Entitlement-gated addon: `bronze_1` tier OR the `reactivation_pack` addon
(any-of — `bronze_2` satisfies the tier leg too, via TIERS cumulativity in
`services.entitlements_client.is_entitled`). Registered as a `PluginSpec`
purely so `registry.enabled_plugins`/`is_entitled` gate it consistently with
every other addon; nothing calls its `agent_tools` or `on_inbound` (both
default to empty/None) — this plugin's only surface is the cron job below.

The job body lives HERE (not workers/tasks.py) per the addon-plugin split:
`workers/arq_worker.py` just imports and registers `send_appointment_reminders`
as a cron job (see WorkerSettings.cron_jobs), every 5 minutes.

Two lead windows per appointment: 24h and 1h ahead. Idempotency ledger
(`ProcessedEvent`, event_id = f"reminder:{kind}:{appointment_id}") guarantees
one reminder per appointment per kind, EVER — across missed ticks, worker
restarts, and cron retries alike. `REMINDER_SCAN_WINDOW_MINUTES` gives the
scan a lookback behind each lead time so a missed/delayed tick still catches
appointments that drifted past the exact lead moment.

Free-vs-template: inside the WhatsApp 24h customer-service window (the
patient's last inbound message was < 24h ago) the reminder is a free text
send; outside it, Meta requires a pre-approved HSM utility template
(`settings.REMINDER_TEMPLATE_NAME`), and that send is billable — a usage
event is emitted to brain-api (services/usage_events.py), FAIL-OPEN: an
emission failure is logged (`usage_emit_failed`) but never blocks or reverts
anything — the WhatsApp send already happened, and the ledger row (claimed
before the send) is what actually prevents a resend on the next tick.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    Message,
    MessageDirection,
    Patient,
    ProcessedEvent,
    Tenant,
)
from secretaria.plugins.base import PluginSpec
from secretaria.plugins.registry import register
from secretaria.services.entitlements_client import (
    EntitlementSummary,
    get_entitlements,
    is_entitled,
)
from secretaria.services.tenant_config import get_waba_token
from secretaria.services.usage_events import emit_usage_event
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)

# Lead time before the appointment each reminder "kind" fires at.
LEAD_WINDOWS: tuple[tuple[str, timedelta], ...] = (
    ("24h", timedelta(hours=24)),
    ("1h", timedelta(hours=1)),
)

# Appointments in any other status (CANCELLED, RESCHEDULED, ATTENDED, NO_SHOW)
# are never reminded.
_REMINDABLE_STATUSES = (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED)

# Meta Cloud API language codes for templates. Today's only shipped locale is
# pt-BR (see CLAUDE.md's "Eye Company" note — language is real per-tenant
# config); this map just needs a new entry alongside each new locale. Unknown
# values fall back to a best-effort "-" -> "_" swap, then pt_BR if that fails.
_META_LANGUAGE_CODES = {"pt-BR": "pt_BR", "pt": "pt_BR", "en": "en_US", "es": "es_ES"}


def _meta_language_code(language: str | None) -> str:
    if not language:
        return "pt_BR"
    return _META_LANGUAGE_CODES.get(language, language.replace("-", "_") or "pt_BR")


def _reminder_key(kind: str, appointment_id: UUID) -> str:
    return f"reminder:{kind}:{appointment_id}"


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive timestamp (e.g. from SQLite) as UTC; pass tz-aware through."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _render_reminder_text(tenant: Tenant, appointment: Appointment) -> str:
    """Render the reminder line from the appointment, in the tenant's own timezone."""
    try:
        tz = ZoneInfo(tenant.timezone or "UTC")
    except Exception:
        tz = UTC
    local_start = _as_utc(appointment.start_at).astimezone(tz) if appointment.start_at else None
    when = local_start.strftime("%d/%m/%Y às %H:%M") if local_start else "horário a confirmar"
    appt_type = appointment.appointment_type or "sua consulta"
    return f"Lembrete: você tem {appt_type} agendado(a) para {when} na {tenant.clinic_name}."


async def _claim_reminder(kind: str, appointment_id: UUID) -> bool:
    """Insert the idempotency ledger row for (kind, appointment). True iff this
    call is the one that claimed it — mirrors the message pipeline's
    claim-insert + IntegrityError race pattern (workers/tasks.py
    `_event_already_processed`)."""
    key = _reminder_key(kind, appointment_id)
    async with async_session_factory() as session:
        try:
            async with session.begin():
                existing = await session.scalar(
                    select(ProcessedEvent.id).where(ProcessedEvent.event_id == key)
                )
                if existing is not None:
                    return False
                session.add(ProcessedEvent(event_id=key))
        except IntegrityError:
            return False
    return True


async def _last_inbound_at(session, tenant_id: UUID, patient_id: UUID) -> datetime | None:
    """Timestamp of the patient's most recent INBOUND message, or None."""
    conversation_id = await session.scalar(
        select(Conversation.id).where(
            Conversation.tenant_id == tenant_id, Conversation.patient_id == patient_id
        )
    )
    if conversation_id is None:
        return None
    return await session.scalar(
        select(Message.created_at)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.INBOUND,
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )


async def _send_one_reminder(
    kind: str,
    appointment: Appointment,
    tenant: Tenant,
    patient: Patient,
    waba_token: str | None,
    last_inbound_at: datetime | None,
) -> None:
    """Send one reminder (free text inside the 24h window, template outside it)
    and, when billable, best-effort emit the usage event. Never raises — the
    caller wraps every item so one bad send never kills the sweep."""
    client = WhatsAppClient.for_tenant(tenant, waba_token)
    text = _render_reminder_text(tenant, appointment)
    inside_window = last_inbound_at is not None and datetime.now(UTC) - _as_utc(
        last_inbound_at
    ) < timedelta(hours=24)

    if inside_window:
        await client.send_text_message(to=patient.wa_id, body=text)
        return

    settings = get_settings()
    await client.send_template(
        to=patient.wa_id,
        template=settings.REMINDER_TEMPLATE_NAME,
        lang=_meta_language_code(tenant.language),
        variables=[text],
    )

    event_id = _reminder_key(kind, appointment.id)
    try:
        recorded = await emit_usage_event(
            tenant_id=str(tenant.id), feature="reminders", amount=1, event_id=event_id
        )
        if not recorded:
            logger.warning("usage_emit_failed", event_id=event_id, tenant_id=str(tenant.id))
    except Exception as exc:  # fail-open: the send already happened
        logger.warning(
            "usage_emit_failed", error=str(exc), event_id=event_id, tenant_id=str(tenant.id)
        )


async def _process_lead_window(
    kind: str,
    lead: timedelta,
    entitlement_cache: dict[UUID, EntitlementSummary | None],
    redis,
) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    window_start = now + lead - timedelta(minutes=settings.REMINDER_SCAN_WINDOW_MINUTES)
    window_end = now + lead

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(Appointment, Tenant, Patient)
                .join(Tenant, Tenant.id == Appointment.tenant_id)
                .join(Patient, Patient.id == Appointment.patient_id)
                .where(
                    Appointment.status.in_(_REMINDABLE_STATUSES),
                    Appointment.start_at.is_not(None),
                    Appointment.start_at > window_start,
                    Appointment.start_at <= window_end,
                )
            )
        ).all()

        for appointment, tenant, patient in rows:
            try:
                if patient.reminder_opt_out:
                    continue

                if tenant.id not in entitlement_cache:
                    entitlement_cache[tenant.id] = await get_entitlements(tenant.id, redis)
                summary = entitlement_cache[tenant.id]
                if summary is None or not any(
                    is_entitled(summary, key) for key in REMINDERS_SPEC.entitlement_keys
                ):
                    continue  # disabled addon (or unresolvable) = silent no-op

                if not await _claim_reminder(kind, appointment.id):
                    continue  # already sent (this tick or a prior one)

                last_inbound = await _last_inbound_at(session, tenant.id, patient.id)
                waba_token = await get_waba_token(session, tenant.id)
                await _send_one_reminder(
                    kind, appointment, tenant, patient, waba_token, last_inbound
                )
            except Exception as exc:
                logger.warning(
                    "reminder_send_failed",
                    error=str(exc),
                    appointment_id=str(appointment.id),
                    kind=kind,
                )


async def send_appointment_reminders(ctx: dict) -> None:
    """arq cron job: sweep upcoming appointments and send lead-window reminders.

    Registered in workers/arq_worker.py, every 5 minutes. `get_entitlements`
    is called at most ONCE per tenant per sweep (memoized in
    `entitlement_cache`, shared across both lead windows) to bound brain-api
    call volume.
    """
    entitlement_cache: dict[UUID, EntitlementSummary | None] = {}
    redis = ctx.get("redis")
    for kind, lead in LEAD_WINDOWS:
        await _process_lead_window(kind, lead, entitlement_cache, redis)


REMINDERS_SPEC = PluginSpec(id="reminders", entitlement_keys=("bronze_1", "reactivation_pack"))
register(REMINDERS_SPEC)
