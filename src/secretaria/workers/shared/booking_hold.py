"""booking_hold - split out of workers/tasks.py (TASK-023)."""

import math
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from secretaria.ai.graph import (
    BOOKING_DRAFT_SENTINEL_PREFIX,
    CALENDAR_UNAVAILABLE_SENTINEL,
    HUMAN_HANDOFF_SENTINEL_PREFIX,
    MANAGE_APPOINTMENT_SENTINEL_PREFIX,
    SELECT_PROFESSIONAL_SENTINEL_PREFIX,
    SHOW_MAIN_MENU_SENTINEL,
)
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    Conversation,
    Patient,
    Tenant,
)
from secretaria.plugins.post_booking import enqueue_post_booking_hooks
from secretaria.services import reminder_hooks
from secretaria.services.appointment_status import (
    SOURCE_FLOW,
)
from secretaria.services.booking_hold import (
    HOLD_TTL_MINUTES,
    HeldSlot,
    latest_hold_in,
    release_in as release_hold_in,
)
from secretaria.services.calendar import (
    CalendarUnavailableError,
    build_event_description,
    build_patient_calendar_link,
)
from secretaria.services.flow_router import (
    WHEN_FORMAT,
)
from secretaria.services.insurance_catalog import (
    resolve_booking_plan_ids,
)
from secretaria.services.pending_identity import (
    BOOKING_HOLD_EXPIRED_MESSAGE,
    CODE_NOTICE_BUTTONS,
    booking_gate_body,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.dispatch import (
    _send_buttons_reply,
)
from secretaria.workers.shared.handover import (
    _set_conversation_human_active,
)
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
)

logger = get_logger(__name__)


def _log_booking_scope(appointment: Appointment, tenant_id: UUID, *, source: str) -> None:
    """Emit the two booking-scope facts for one committed appointment.

    Ids and enums only — never the patient, the phone, the event title or the
    service description. `booking_owner_resolved` answers "did this booking
    get an owner, and does the tenant even have one to give?"; the paired
    event in ai/tools.py::_persist_appointment does the same for the LLM path,
    so both surfaces are countable with one query. `has_owner=False` on a
    single-professional tenant is exactly the regression this round fixed.
    """
    logger.info(
        "booking_owner_resolved",
        tenant_id=str(tenant_id),
        appointment_id=str(appointment.id),
        professional_id=(str(appointment.professional_id) if appointment.professional_id else None),
        has_owner=appointment.professional_id is not None,
        source=source,
    )
    logger.info(
        "booking_service_resolved",
        tenant_id=str(tenant_id),
        appointment_id=str(appointment.id),
        professional_id=(str(appointment.professional_id) if appointment.professional_id else None),
        has_type=bool(appointment.appointment_type),
        source=source,
    )

def _is_agent_sentinel(reply_text: str) -> bool:
    """True when `run_agent` returned a PROTOCOL string, not prose for a patient.

    The sentinels are hand-back instructions this module consumes itself and
    never shows to anybody, so the honesty filter has nothing to say about
    them. Listed in ONE place so a new sentinel cannot be forgotten by the
    filter and silently rewritten into an apology.
    """
    return reply_text in (CALENDAR_UNAVAILABLE_SENTINEL, SHOW_MAIN_MENU_SENTINEL) or (
        reply_text.startswith(SELECT_PROFESSIONAL_SENTINEL_PREFIX)
        or reply_text.startswith(MANAGE_APPOINTMENT_SENTINEL_PREFIX)
        or reply_text.startswith(HUMAN_HANDOFF_SENTINEL_PREFIX)
        or reply_text.startswith(BOOKING_DRAFT_SENTINEL_PREFIX)
    )

def _hold_minutes_left(held: HeldSlot) -> int:
    """Whole minutes still on the reservation, never below 1.

    Rounded UP, and floored at one: telling a patient "0 minutos" while the
    hold is technically still live would read as "you already lost it", and
    the honest ceiling is the one the clock will actually enforce.
    """
    remaining = (held.expires_at - datetime.now(UTC)).total_seconds()
    return max(1, min(HOLD_TTL_MINUTES, math.ceil(remaining / 60)))

def _tenant_tzinfo(tenant: Tenant | None):
    """The clinic's timezone, falling back to the product default."""
    name = getattr(tenant, "timezone", None) or "America/Sao_Paulo"
    try:
        return ZoneInfo(str(name))
    except Exception:
        return ZoneInfo("America/Sao_Paulo")

def _hold_when(held: HeldSlot, tenant: Tenant | None) -> str:
    """The held window, spelled in the CLINIC's timezone.

    The row is UTC; the patient is not. Rendering the raw UTC value would tell
    a Sao Paulo patient their 14:00 appointment is at 17:00 - the exact class
    of defect DEF-2 already records on the manage screen, so it is not
    repeated here.
    """
    return held.start_at.astimezone(_tenant_tzinfo(tenant)).strftime(WHEN_FORMAT)

async def _send_booking_gate_notice(
    reply: _ReplyContext,
    hold: dict,
    *,
    tenant: Tenant | None,
    waba_token: str | None,
) -> None:
    """Ask for the code that will CREATE the appointment, as the same card.

    The card is TASK-003's three-button one, reused verbatim - `identity_back`,
    `identity_resend`, `identity_change_email` all keep working, and so does
    the router branch that reads their ids. Only the BODY is new, because the
    old body opens by saying the consultation is confirmed and at this point
    it is not (`services/pending_identity.py` explains the two wordings).
    """
    if tenant is None:
        logger.warning(
            "booking_gate_notice_skipped_no_tenant",
            conversation_id=str(reply.conversation_id),
        )
        return
    await _send_buttons_reply(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=booking_gate_body(
            hold.get("email_masked"),
            when=hold.get("when"),
            hold_minutes=HOLD_TTL_MINUTES,
        ),
        buttons=list(CODE_NOTICE_BUTTONS),
        event="booking_gate_notice_sent",
    )

async def _promote_booking_hold(
    reply: _ReplyContext,
    *,
    tenant: Tenant | None,
    waba_token: str | None,
    professionals: list | None,
    redis,
) -> str | None:
    """Turn this conversation's reservation into a real appointment.

    Returns the extra paragraph to append to the code-accepted message, or
    None. None means "this conversation was never gated" - a patient whose
    booking was committed the old way, or one the gate stood down on. That
    path must stay byte-identical to what it was, which is why None is a
    distinct answer and not an empty string.

    The order here is the whole feature: the hold is read, the Google event is
    created, the appointment row is written, and only THEN is the hold
    dropped. An event created with no row behind it is the orphan of the
    previous wave; a hold dropped before the row exists would free a slot the
    patient just paid for with a code.
    """
    if tenant is None:
        return None
    async with async_session_factory() as session:
        held = await latest_hold_in(session, reply.conversation_id)
    if held is None:
        return None

    if held.expires_at <= datetime.now(UTC):
        # The ten minutes ran out. The slot went back on sale the moment the
        # clock passed, so there is nothing to promote and nothing to undo -
        # say so plainly rather than confirming an appointment that does not
        # exist. The account IS active: the code itself was still good.
        await _release_hold(held.id)
        logger.info(
            "booking_hold_expired_at_verify",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        return BOOKING_HOLD_EXPIRED_MESSAGE

    target = _appointment_calendar_target({"professional_id": held.professional_id}, professionals)
    async with async_session_factory() as session:
        calendar = await _appointment_calendar(session, tenant, target)
        patient_id = await session.scalar(
            select(Conversation.patient_id).where(Conversation.id == reply.conversation_id)
        )
    if calendar is None:
        # No agenda to book on. The reservation is KEPT: it costs nothing, it
        # expires on its own, and dropping it here would hand the slot to
        # somebody else while a human is still trying to rescue this booking.
        logger.error(
            "booking_hold_promote_no_calendar",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        await _set_conversation_human_active(reply.conversation_id)
        return _PROMOTE_FAILED_MESSAGE

    service_type = held.appointment_type or "Consulta"
    # The attendee's name titles the event on a booking for someone else, as
    # on the ungated path (flow_router._handle_confirmation).
    event_name = held.attendee_name or await _patient_display_name(patient_id)
    summary = f"{service_type} - {event_name}" if event_name else service_type
    try:
        event = await calendar.create_event(
            start=held.start_at, end=held.end_at, summary=summary,
            description=build_event_description(
                service=service_type, insurance=held.insurance,
                channel=reply.channel, attendee_name=held.attendee_name,
            ),
        )
    except CalendarUnavailableError:
        logger.error(
            "booking_hold_promote_calendar_unavailable",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        await _set_conversation_human_active(reply.conversation_id)
        return _PROMOTE_FAILED_MESSAGE

    appointment = Appointment(
        tenant_id=tenant.id,
        patient_id=patient_id,
        conversation_id=reply.conversation_id,
        google_event_id=event.get("id") or "",
        google_event_link=event.get("htmlLink"),
        appointment_type=service_type[:120],
        start_at=held.start_at,
        end_at=held.end_at,
        # NOT the patient_ref: on Brain-Message that is a 36-character UUID and
        # `appointments.phone` is VARCHAR(32). Writing it there is the defect
        # 697c24a fixed; a Portal patient simply has no phone number.
        phone=None,
        professional_id=held.professional_id,
        insurance=held.insurance,
        attendee_name=held.attendee_name,
    )
    try:
        async with async_session_factory() as session:
            async with session.begin():
                (
                    appointment.insurance_plan_id,
                    appointment.insurance_professional_plan_id,
                ) = await resolve_booking_plan_ids(
                    session, tenant.id, held.insurance, held.professional_id
                )
                session.add(appointment)
    except Exception as exc:
        # The event EXISTS on Google and the row does not. Same answer the
        # flow path gives in the same situation: never tell the patient it is
        # confirmed, hand it to a human to reconcile.
        logger.error(
            "booking_hold_promote_persist_failed",
            error_type=type(exc).__name__,
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        await _set_conversation_human_active(reply.conversation_id)
        return _PROMOTE_FAILED_MESSAGE

    await _release_hold(held.id)
    _log_booking_scope(appointment, tenant.id, source=SOURCE_FLOW)
    logger.info(
        "booking_hold_promoted",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        appointment_id=str(appointment.id),
    )
    # NOW the post_booking hooks fire, from the point where the appointment
    # genuinely starts existing. `plugins/precheck_handoff.py` gets the
    # committed appointment it has always needed; `plugins/pending_identity.py`
    # runs too and skips on its own, because brain-api answers NOT_PENDING for
    # a visitor who just verified - no second card, no parallel mechanism.
    await enqueue_post_booking_hooks(redis, tenant.id, appointment.id, source="flow")
    # TASK-032 R2: the Portal booking is born here, so its reminders are planned here.
    if reminder_hooks.enabled_for(tenant):
        await reminder_hooks.after_appointment_booked(appointment.id)

    tz = _tenant_tzinfo(tenant)
    local_start = held.start_at.astimezone(tz)
    local_end = held.end_at.astimezone(tz)
    return (
        "Pronto! Seu agendamento está confirmado. \u2705\n\n"
        f"{service_type}\n"
        + (f"Paciente: {held.attendee_name}\n" if held.attendee_name else "")
        + f"{local_start.strftime(WHEN_FORMAT)}\n\n"
        "Adicionar à sua agenda:\n"
        f"{build_patient_calendar_link(local_start, local_end, summary, tz=tz)}"
    )

async def _release_hold(hold_id) -> None:
    """Drop the reservation, on THIS module's session factory. Best-effort.

    Routed through the worker's own factory rather than the service's so the
    whole promotion leg - read, write, release - runs on one engine, which is
    also the one the worker's tests substitute.
    """
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await release_hold_in(session, hold_id)
    except Exception as exc:
        logger.warning("booking_hold_release_failed", hold_id=str(hold_id), error=str(exc))

async def _patient_display_name(patient_id) -> str | None:
    """The patient's name for the calendar event title, or None."""
    if patient_id is None:
        return None
    async with async_session_factory() as session:
        return await session.scalar(select(Patient.name).where(Patient.id == patient_id))

# Said when the code was good but the appointment could not be created. Never
# claims a booking, never blames the patient, and is always paired with a
# handover so a human is already looking at it.
_PROMOTE_FAILED_MESSAGE = (
    "Ativei sua conta, mas não consegui fechar o agendamento agora. 😕\n\n"
    "Já avisei a equipe da clínica — alguém fala com você por aqui em instantes."
)
