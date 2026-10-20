"""CORE: tell the clinic, the doctor and the Portal patient that the PATIENT cancelled or moved.

Booking mails three people (`plugins/booking_notifications.py`: the clinic and
the Portal patient; `plugins/professional_notification.py`: the doctor). The
patient cancelling or moving that same appointment through the conversation
mailed nobody - bug A7, proven live on 2026-10-05 ("Cancelar"/"Remarcar" taps
and the agent's tools; the clinic found out from the agenda, if at all).
TASK-044 covers the other direction - the CLINIC acting from the hub tells the
patient (`services/clinic_action_notice.py`, `services/staff_patient_message.py`)
- and this module is deliberately never reached from there: staff made that
change themselves, so nothing here is enqueued by `api/hub/`.

Who hears what (templates in `services/email.py`):

* cancelled -> clinic, doctor, Portal patient;
* moved     -> clinic, Portal patient. NOT the doctor: both patient move paths
  (the "Remarcar" sub-flow and the "Alterar Dados" edit, both through
  `workers/shared/flow_runner.py::_apply_flow_result`) already record a
  `ProfessionalEditNotice`, and `workers/shared/appointment_edit_notification.py`
  mails the doctor "Consulta alterada" from it. A second mail here is exactly
  the duplicate that kept the first attempt (2d067b2) off main.

A WhatsApp patient gets no mail, as on booking: the chat already confirmed it.

Same shape as the booking mails: `Tenant.contact_email`; the doctor's address
asked of brain-api per send (`fetch_professional_emails`, never stored); the
Portal patient's asked of brain-api with the stored `Patient.email` only when it
cannot answer (never written back here - the booking hook owns that refresh).
One `ProcessedEvent` claim per recipient per change, given back when the mail
did not leave; a transient failure (SMTP blip, brain-api down) raises
`arq.Retry` inside the professional notification's own budget; a lapsed clinic
sends nothing. Times are the clinic's (`_local_when`). Only ids, the change and
a reason are logged - never an address.

Every patient path calls `enqueue_appointment_change_notification` AFTER its own
commit, fire-and-forget like `enqueue_post_booking_hooks`: a mail problem can
never cost the patient the cancel or the move they just confirmed.
"""

from datetime import UTC, datetime
from uuid import UUID

from arq import Retry
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    ProcessedEvent,
    Professional,
    Tenant,
    is_live_status,
)
from secretaria.plugins.booking_notifications import agenda_line, professional_line
from secretaria.plugins.professional_notification import (
    _as_utc,
    _local_when,
    _patient_line_name,
    _retry_decision,
)
from secretaria.services.brain_patients import fetch_patient_email_result
from secretaria.services.brain_professionals import fetch_professional_emails
from secretaria.services.calendar import build_patient_calendar_link
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome, send_transactional_email_result
from secretaria.services.entitlements_client import get_entitlements

logger = get_logger(__name__)

JOB_NAME = "notify_appointment_change"

CHANGE_CANCELLED = "cancelled"
CHANGE_RESCHEDULED = "rescheduled"

_CLINIC = "clinic"
_PROFESSIONAL = "professional"
_PATIENT = "patient"

# The doctor of a moved booking is told by the edit outbox - see the docstring.
_RECIPIENTS: dict[str, tuple[str, ...]] = {
    CHANGE_CANCELLED: (_CLINIC, _PROFESSIONAL, _PATIENT),
    CHANGE_RESCHEDULED: (_CLINIC, _PATIENT),
}

TEMPLATES: dict[tuple[str, str], str] = {
    (CHANGE_CANCELLED, _CLINIC): "appointment_cancelled_clinic",
    (CHANGE_CANCELLED, _PROFESSIONAL): "appointment_cancelled_professional",
    (CHANGE_CANCELLED, _PATIENT): "appointment_cancelled_patient",
    (CHANGE_RESCHEDULED, _CLINIC): "appointment_rescheduled_clinic",
    (CHANGE_RESCHEDULED, _PATIENT): "appointment_rescheduled_patient",
}

# What `_notify` reports back (same vocabulary as professional_notification).
_SENT = "sent"
_SKIPPED = "skipped"
_TRANSIENT = "transient"


def _iso(value: datetime | None) -> str:
    """One spelling per instant (UTC), so the flow (clinic-local times) and the edit
    path (UTC) name the same move with the same job id and ledger key."""
    return _as_utc(value).astimezone(UTC).isoformat() if value is not None else ""


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _change_ref(change: str, appointment_id, start_iso: str, previous_iso: str) -> str:
    """`<appointment_id>` for a cancellation (it happens once); for a move also the
    window it left and the one it went to - a booking can move many times and every
    move is its own mail, while a replay of the same move is the same key."""
    if change == CHANGE_RESCHEDULED:
        return f"{appointment_id}:{previous_iso}:{start_iso}"
    return str(appointment_id)


async def enqueue_appointment_change_notification(
    redis,
    tenant_id,
    appointment_id,
    change: str,
    *,
    new_start_at: datetime | None = None,
    previous_start_at: datetime | None = None,
) -> None:
    """Fire-and-forget enqueue of `notify_appointment_change`. Never raises.

    Call it only from a PATIENT path, AFTER the transaction that cancelled or moved
    the row committed - the job re-reads the row and will not mail about a change it
    cannot see. A move carries the window it left (`previous_start_at`) and the one
    it went to (`new_start_at`); one that did not actually change the start is not
    a move and is dropped here. `redis=None` (no pool: unit tests, dev scripts) is a
    logged no-op, exactly like `enqueue_post_booking_hooks`.
    """
    start_iso = previous_iso = ""
    if change == CHANGE_RESCHEDULED:
        start_iso, previous_iso = _iso(new_start_at), _iso(previous_start_at)
        if not start_iso or start_iso == previous_iso:
            _skip(appointment_id, "not_moved", change=change)
            return
    if redis is None:
        logger.info(
            "appointment_change_enqueue_skipped_no_redis",
            appointment_id=str(appointment_id),
            change=change,
        )
        return
    ref = _change_ref(change, appointment_id, start_iso, previous_iso)
    try:
        # The job id names the change: arq refuses a second enqueue of the same one
        # while it still knows the first; the per-recipient ledger covers the rest.
        await redis.enqueue_job(
            JOB_NAME,
            str(tenant_id),
            str(appointment_id),
            change,
            start_iso,
            previous_iso,
            _job_id=f"apptchange:{change}:{ref}",
        )
    except Exception as exc:
        logger.warning(
            "appointment_change_enqueue_failed",
            error_type=type(exc).__name__,
            appointment_id=str(appointment_id),
            change=change,
        )


def _ledger_key(recipient: str, change: str, ref: str) -> str:
    return f"apptchange:{change}:{recipient}:{ref}"


async def _claim(key: str) -> bool:
    """Insert the ledger row; True iff THIS call claimed the send (same race
    pattern as `booking_notifications._claim`)."""
    async with async_session_factory() as session:
        try:
            async with session.begin():
                if await session.scalar(
                    select(ProcessedEvent.id).where(ProcessedEvent.event_id == key)
                ):
                    return False
                session.add(ProcessedEvent(event_id=key))
        except IntegrityError:
            return False
    return True


async def _release(key: str) -> None:
    """Give the claim back: the send did not happen. Best-effort."""
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == key))
    except Exception as exc:
        logger.warning("appointment_change_release_failed", error_type=type(exc).__name__)


def _skip(appointment_id, reason: str, **fields) -> None:
    logger.info(
        "appointment_change_notification_skipped",
        appointment_id=str(appointment_id),
        reason=reason,
        **fields,
    )


async def _send_once(key: str, to: str, template: str, variables: dict, *, log: dict) -> str:
    """Claim, send, and give the claim back unless the mail actually left."""
    if not await _claim(key):
        return _SKIPPED
    try:
        outcome = await send_transactional_email_result(
            to=to, template=template, variables=variables
        )
    except Exception:
        outcome = EmailOutcome.SEND_FAILED
    if outcome is EmailOutcome.SENT:
        logger.info("appointment_change_notification_sent", **log)
        return _SENT
    await _release(key)
    if outcome.is_transient:
        return _TRANSIENT
    if outcome is EmailOutcome.DISABLED:
        logger.info("appointment_change_notification_skipped", reason="email_disabled", **log)
    else:
        # Our own defect (template id / placeholder): every attempt would fail.
        logger.error(
            "appointment_change_notification_undeliverable",
            alarm="appointment_change_notification_undelivered",
            reason=outcome.value,
            **log,
        )
    return _SKIPPED


async def _patient_address(tenant: Tenant, patient: Patient | None) -> str | None:
    """The Portal patient's address, resolved exactly like the booking confirmation:
    brain-api's answer wins, the stored copy only when it cannot answer. WhatsApp
    patients (and a Portal patient with no address) get None."""
    if patient is None or getattr(patient, "channel", None) != CHANNEL_BRAIN_MESSAGE:
        return None
    external_id = getattr(patient, "external_id", None)
    if not external_id:
        return None
    fetched = await fetch_patient_email_result(tenant.id, external_id)
    stored = (getattr(patient, "email", None) or "").strip() or None
    return fetched.email if fetched.available else stored


async def _notify(
    recipient: str,
    *,
    change: str,
    ref: str,
    tenant: Tenant,
    patient: Patient | None,
    appointment: Appointment,
    variables: dict,
) -> str:
    log = {
        "appointment_id": str(appointment.id),
        "tenant_id": str(tenant.id),
        "change": change,
        "recipient": recipient,
    }
    template = TEMPLATES[(change, recipient)]
    key = _ledger_key(recipient, change, ref)

    if recipient == _CLINIC:
        to = (getattr(tenant, "contact_email", None) or "").strip()
        if not to:
            _skip(appointment.id, "clinic_without_email", recipient=recipient)
            return _SKIPPED
        return await _send_once(key, to, template, variables, log=log)

    if recipient == _PROFESSIONAL:
        if appointment.professional_id is None:
            _skip(appointment.id, "no_professional", recipient=recipient)
            return _SKIPPED
        async with async_session_factory() as session:
            # Tenant-scoped: a mis-set id never mails another clinic's doctor.
            professional = await session.scalar(
                select(Professional).where(
                    Professional.id == appointment.professional_id,
                    Professional.tenant_id == tenant.id,
                )
            )
        if professional is None:
            _skip(appointment.id, "professional_not_found", recipient=recipient)
            return _SKIPPED
        emails = await fetch_professional_emails(tenant.id)
        if emails is None:
            # "We do not know" is not "nobody" - worth another look.
            return _TRANSIENT
        to = (emails.get(str(professional.id)) or "").strip()
        if not to:
            _skip(appointment.id, "professional_without_email", recipient=recipient)
            return _SKIPPED
        professional_vars = {
            **variables,
            "professional_name": professional.name,
            "patient_name": _patient_line_name(appointment, patient),
        }
        return await _send_once(key, to, template, professional_vars, log=log)

    to = await _patient_address(tenant, patient)
    if not to:
        _skip(appointment.id, "no_portal_patient_email", recipient=recipient)
        return _SKIPPED
    calendar_line = ""
    if change == CHANGE_RESCHEDULED and appointment.start_at and appointment.end_at:
        link = build_patient_calendar_link(
            _as_utc(appointment.start_at),
            _as_utc(appointment.end_at),
            appointment.appointment_type or "Consulta",
        )
        calendar_line = f"Adicionar à sua agenda:\n{link}\n\n"
    return await _send_once(
        key, to, template, {**variables, "calendar_line": calendar_line}, log=log
    )


async def _notify_all(
    change: str,
    tenant: Tenant,
    patient: Patient | None,
    appointment: Appointment,
    start_iso: str,
    previous_start_iso: str,
) -> list[str]:
    """Send this change's mails; one result per recipient."""
    previous = _parse_iso(previous_start_iso)
    variables = {
        "clinic_name": tenant.clinic_name,
        "service": appointment.appointment_type or "Consulta",
        # The cancelled slot, or the window it moved to; always the clinic's tz.
        "when": _local_when(appointment.start_at, tenant.timezone),
        "previous_line": (
            f"Horário anterior: {_local_when(previous, tenant.timezone)}\n" if previous else ""
        ),
        "professional_line": await professional_line(tenant, appointment),
        "agenda_line": agenda_line(),
        # Footer reference only (support), on the clinic's mail.
        "appointment_id": str(appointment.id),
    }
    ref = _change_ref(change, appointment.id, start_iso, previous_start_iso)
    results: list[str] = []
    for recipient in _RECIPIENTS[change]:
        try:
            results.append(
                await _notify(
                    recipient,
                    change=change,
                    ref=ref,
                    tenant=tenant,
                    patient=patient,
                    appointment=appointment,
                    variables=variables,
                )
            )
        except Exception as exc:
            # One recipient's surprise never costs the others their mail.
            logger.warning(
                "appointment_change_notification_failed",
                error_type=type(exc).__name__,
                appointment_id=str(appointment.id),
                recipient=recipient,
            )
            results.append(_TRANSIENT)
    return results


def _still_current(appointment: Appointment, change: str, start_iso: str) -> bool:
    """Is the change this job announces still the row's truth? A later change has its
    own job; a mail about a superseded one would only mislead."""
    if change == CHANGE_CANCELLED:
        return appointment.status == AppointmentStatus.CANCELLED
    expected = _parse_iso(start_iso)
    return (
        is_live_status(appointment.status)
        and expected is not None
        and appointment.start_at is not None
        and _as_utc(appointment.start_at) == expected
    )


async def notify_appointment_change(
    ctx: dict,
    tenant_id: str,
    appointment_id: str,
    change: str,
    start_iso: str = "",
    previous_start_iso: str = "",
) -> None:
    """arq job: mail everyone who should hear that the patient cancelled or moved.

    Registered in `workers/arq_worker.py`, enqueued only through
    `enqueue_appointment_change_notification`. Reloads every row by id
    (tenant-checked), so nothing serialised into the job is trusted beyond the two
    instants that describe the move itself. Raises `arq.Retry` while a recipient
    failed transiently and budget is left; whoever already got mail is skipped on
    the next run by their own ledger key.
    """
    if change not in _RECIPIENTS:
        _skip(appointment_id, "unknown_change", change=change)
        return
    async with async_session_factory() as session:
        appointment = await session.get(Appointment, UUID(appointment_id))
        if appointment is None or str(appointment.tenant_id) != tenant_id:
            # Deleted meanwhile, or - the isolation guard - not this clinic's.
            _skip(appointment_id, "appointment_not_found")
            return
        tenant = await session.get(Tenant, appointment.tenant_id)
        patient = (
            await session.get(Patient, appointment.patient_id)
            if appointment.patient_id is not None
            else None
        )
    if tenant is None:
        _skip(appointment_id, "tenant_not_found")
        return
    if not _still_current(appointment, change, start_iso):
        _skip(appointment_id, "superseded", change=change)
        return

    summary = await get_entitlements(tenant.id, ctx.get("redis"))
    if summary is None:
        # brain-api could not say. Not a "no" - try again within the budget.
        results = [_TRANSIENT]
    elif not summary.active:
        # The core-plugin rule (`registry._spec_enabled`): a lapsed clinic sends nothing.
        _skip(appointment_id, "not_entitled")
        return
    else:
        results = await _notify_all(
            change, tenant, patient, appointment, start_iso, previous_start_iso
        )

    if _TRANSIENT not in results:
        return
    defer = _retry_decision(ctx)
    if defer is None:
        logger.error(
            "appointment_change_notification_abandoned",
            alarm="appointment_change_notification_undelivered",
            appointment_id=appointment_id,
            tenant_id=tenant_id,
            change=change,
        )
        return
    logger.warning(
        "appointment_change_notification_retrying",
        appointment_id=appointment_id,
        tenant_id=tenant_id,
        change=change,
        retry_in_s=defer,
    )
    raise Retry(defer=defer)
