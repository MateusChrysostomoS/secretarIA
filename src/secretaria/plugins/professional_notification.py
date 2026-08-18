"""CORE post_booking hook: tell the professional a patient just booked with them.

Until this existed, a doctor learned about a new appointment only by opening
the agenda (or from Google Calendar, when the clinic had connected one).
Nothing pushed. This closes that.

Shape mirrors `plugins/ehr.py` — a `_post_booking` coroutine registered through
one `PluginSpec` — with three deliberate differences:

**CORE, not an add-on.** `entitlement_keys=()`. Telling a professional that
somebody booked with them is product hygiene, not a sellable feature. See
`registry._spec_enabled`, which this round taught to read an empty tuple as
"core, enabled while the subscription is active"; it previously fell out of
`any(())` as silently disabled, and this module is the first to register a real
hook that way.

**The address is not ours.** `Professional` has no email column and will not
get one: brain-api owns identity and already stores the address on its own
`users` row, linked by `professional_id`. We ask, per booking
(`services/brain_professionals.fetch_professional_emails`). A professional
created without an invite has no linked user and therefore no address — this
hook is then a logged no-op for them, which is honest, and which the
professionals screen of both frontends now says out loud.

**Exactly one email per appointment.** `run_post_booking_hooks` is an arq job
and arq retries jobs; a second "nova consulta marcada" is direct noise in a
doctor's inbox. Claimed through `ProcessedEvent` — the same durable ledger the
webhook pipeline and `plugins/reminders.py` already use, namespaced
`profnotif:<appointment_id>` — with one addition reminders does not need: a
claim whose send then FAILS is RELEASED, so a retry can try again. Reminders
can afford "claimed means done" (a missed reminder beats a duplicate); here an
SMTP outage must not permanently swallow the mail.

Failure is contained by construction: `registry.run_post_booking` wraps each
hook in its own try/except, so SMTP being down cannot disturb the already-
committed booking nor stop `pix_deposit` running in the same sweep.

Never logs an email address, a phone number, or any patient content — only
ids, counts and a reason string.
"""

from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import ProcessedEvent, Professional
from secretaria.plugins.base import PluginSpec, PostBookingContext
from secretaria.plugins.registry import register
from secretaria.services.brain_professionals import fetch_professional_emails
from secretaria.services.email import send_transactional_email_message

logger = get_logger(__name__)

TEMPLATE_ID = "appointment_booked_professional"

# Fallbacks for a booking that reached here missing a display field. Never
# invent something that could read as fact — "horário a confirmar" is visibly a
# placeholder, a made-up time would not be.
_UNKNOWN_WHEN = "horário a confirmar"
_UNKNOWN_SERVICE = "Consulta"
_UNKNOWN_PATIENT = "Paciente"


def _ledger_key(appointment_id: UUID) -> str:
    return f"profnotif:{appointment_id}"


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive timestamp (e.g. from SQLite) as UTC; pass tz-aware through."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _local_when(start_at: datetime | None, timezone: str | None) -> str:
    """`start_at` rendered in the TENANT's timezone, never the server's.

    A clinic in America/Sao_Paulo reading "17:00" for a 14:00 consultation
    would be worse than useless. An unparseable tz falls back to UTC rather
    than raising — the same defensive shape `plugins/reminders.py` uses.
    """
    if start_at is None:
        return _UNKNOWN_WHEN
    try:
        tz = ZoneInfo(timezone or "UTC")
    except Exception:
        tz = UTC
    return _as_utc(start_at).astimezone(tz).strftime("%d/%m/%Y às %H:%M")


async def _claim(appointment_id: UUID) -> bool:
    """Insert the ledger row. True iff THIS call claimed the send.

    Claim-insert + IntegrityError race pattern, identical to
    `plugins/reminders.py::_claim_reminder` and the message pipeline's
    `_event_already_processed`.
    """
    key = _ledger_key(appointment_id)
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


async def _release(appointment_id: UUID) -> None:
    """Undo a claim whose send did not go out, so a retry may try again.

    The half of idempotency reminders does not implement: there, "claimed"
    means "done, never repeat". Here a claim is only a lock, and a send that
    returned False (SMTP down, email disabled, unknown template) never
    happened — leaving the row would turn a transient outage into a
    permanently lost notification. Best-effort: a failure to release is logged
    and swallowed, since the caller is already on its failure path.
    """
    key = _ledger_key(appointment_id)
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == key))
    except Exception as exc:
        logger.warning(
            "professional_notification_release_failed",
            appointment_id=str(appointment_id),
            error=str(exc),
        )


async def _load_professional(tenant_id: UUID, professional_id: UUID) -> Professional | None:
    """Re-fetch the professional, scoped to THIS tenant.

    The tenant filter is the isolation invariant, not a formality: it is what
    makes it impossible for a mis-set `professional_id` to address a doctor
    belonging to another clinic.
    """
    async with async_session_factory() as session:
        return await session.scalar(
            select(Professional).where(
                Professional.id == professional_id, Professional.tenant_id == tenant_id
            )
        )


def _skip(appointment_id, reason: str) -> None:
    logger.info(
        "professional_notification_skipped",
        appointment_id=str(appointment_id),
        reason=reason,
    )


async def _post_booking(ctx: PostBookingContext) -> None:
    """Email the appointment's owning professional. Best-effort, exactly once."""
    appointment = ctx.appointment
    professional_id = appointment.professional_id
    if professional_id is None:
        # No owner to address. Since the booking-ownership round this only
        # happens for a tenant with zero or 2+ active professionals and no
        # explicit selection (services/booking_scope.resolve_booking_owner_id);
        # the common single-professional clinic now always resolves an owner.
        _skip(appointment.id, "no_professional")
        return

    professional = await _load_professional(ctx.tenant.id, professional_id)
    if professional is None:
        # Either deleted between booking and this job, or — the reason the
        # tenant filter exists — not this clinic's to notify.
        _skip(appointment.id, "professional_not_found")
        return

    emails = await fetch_professional_emails(ctx.tenant.id)
    if emails is None:
        _skip(appointment.id, "email_lookup_failed")
        return
    to_email = emails.get(str(professional_id))
    if not to_email:
        _skip(appointment.id, "professional_without_email")
        return

    if not await _claim(appointment.id):
        _skip(appointment.id, "already_notified")
        return

    settings = get_settings()
    agenda_url = (settings.DOCTOR_AGENDA_URL or "").strip()
    insurance = (appointment.insurance or "").strip()
    variables = {
        "professional_name": professional.name,
        # `patient` is None for a booking with no patient row (a hub-created
        # block). The appointment is still real, so the mail still goes.
        "patient_name": (ctx.patient.name if ctx.patient else None) or _UNKNOWN_PATIENT,
        "service": appointment.appointment_type or _UNKNOWN_SERVICE,
        "when": _local_when(appointment.start_at, ctx.tenant.timezone),
        # Pre-rendered lines: empty when there is nothing to say, so the
        # template itself needs no conditionals (see services/email.py).
        "insurance_line": f"Convênio: {insurance}\n" if insurance else "",
        "agenda_line": f"Ver na agenda:\n{agenda_url}\n\n" if agenda_url else "",
    }

    sent = await send_transactional_email_message(
        to=to_email, template=TEMPLATE_ID, variables=variables
    )
    if not sent:
        # Disabled, misconfigured, or a transient SMTP failure — that module's
        # contract is to return False rather than raise, and it already logged
        # which. Give the claim back so a retry is still possible.
        await _release(appointment.id)
        logger.info(
            "professional_notification_not_sent",
            appointment_id=str(appointment.id),
            tenant_id=str(ctx.tenant.id),
        )
        return

    logger.info(
        "professional_notification_sent",
        appointment_id=str(appointment.id),
        tenant_id=str(ctx.tenant.id),
        professional_id=str(professional_id),
        source=ctx.source,
    )


PROFESSIONAL_NOTIFICATION_SPEC = PluginSpec(
    id="professional_notification",
    entitlement_keys=(),
    post_booking=_post_booking,
)
register(PROFESSIONAL_NOTIFICATION_SPEC)
