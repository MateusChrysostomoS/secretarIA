"""booking_hold - split out of workers/tasks.py (TASK-023)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from secretaria.ai.graph import (
    BOOKING_DRAFT_SENTINEL_PREFIX,
    CALENDAR_UNAVAILABLE_SENTINEL,
    HUMAN_HANDOFF_OFFER_SENTINEL,
    HUMAN_HANDOFF_SENTINEL_PREFIX,
    MANAGE_APPOINTMENT_SENTINEL_PREFIX,
    SELECT_PROFESSIONAL_SENTINEL_PREFIX,
    SHOW_MAIN_MENU_SENTINEL,
    START_GUIDED_BOOKING_SENTINEL_PREFIX,
)
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    Conversation,
    Patient,
    Tenant,
)
from secretaria.models.deposit_release_offer import CARRY_CHECK_REFUNDED, CARRY_CHECK_UNKNOWN
from secretaria.plugins.post_booking import enqueue_post_booking_hooks
from secretaria.services import release_carry_check, release_offer, reminder_hooks
from secretaria.services.appointment_replacement import (
    ReplacedAppointment,
    cancel_replaced_appointment,
)
from secretaria.services.appointment_status import (
    SOURCE_FLOW,
)
from secretaria.services.attendee import ATTENDEE_SELF
from secretaria.services.availability import slot_taken_at_confirm
from secretaria.services.booking_hold import (
    HeldSlot,
    latest_hold_in,
    live_hold_overlaps,
    release_in as release_hold_in,
)
from secretaria.services.booking_scope import resolve_booking_owner_id
from secretaria.services.calendar import (
    CalendarSlotTakenError,
    CalendarUnavailableError,
    build_event_description,
    build_patient_calendar_link,
)
from secretaria.services.flow_router import (
    SLOT_TAKEN_AT_CONFIRM_MESSAGE,
    WHEN_FORMAT,
    FlowRouterResult,
    booking_gate_scope,
    reoffer_taken_booking_slot,
)
from secretaria.services.insurance_catalog import (
    resolve_booking_plan_ids,
)
from secretaria.services.local_calendar import create_event_exclusive
from secretaria.services.pending_identity import (
    CODE_NOTICE_BUTTONS,
    booking_gate_body,
)
from secretaria.services.tenant_config import (
    list_active_professionals,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.dispatch import (
    _send_buttons_reply,
)
from secretaria.workers.shared.draft_resolution import (
    _load_draft_context,
    _turn_booking_gate,
)
from secretaria.workers.shared.handover import (
    _set_conversation_human_active,
)
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
)
from secretaria.workers.shared.replacement import (
    _finish_replacement,
    _replaced_notice,
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
    return reply_text in (
        CALENDAR_UNAVAILABLE_SENTINEL,
        SHOW_MAIN_MENU_SENTINEL,
        HUMAN_HANDOFF_OFFER_SENTINEL,
    ) or (
        reply_text.startswith(SELECT_PROFESSIONAL_SENTINEL_PREFIX)
        or reply_text.startswith(MANAGE_APPOINTMENT_SENTINEL_PREFIX)
        or reply_text.startswith(HUMAN_HANDOFF_SENTINEL_PREFIX)
        or reply_text.startswith(BOOKING_DRAFT_SENTINEL_PREFIX)
        or reply_text.startswith(START_GUIDED_BOOKING_SENTINEL_PREFIX)
    )

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

@dataclass(frozen=True)
class SlotTakenAtPromotion:
    """`_promote_booking_hold`'s third answer: the code was good, the slot is gone.

    Not a paragraph like the other two, because the patient's next step is not text: the
    caller says the account is active, then persists and sends `reoffer` (the day picker,
    prefixed with the "just taken" line, the rest of the booking kept - TASK-072) instead
    of leaving the conversation IDLE.
    """

    reoffer: FlowRouterResult

# Said only when the conversation cannot be re-read to rebuild the slot list (it was
# there a moment ago): the same fact, and the one thing the patient can do about it.
_SLOT_TAKEN_NO_REOFFER_MESSAGE = (
    f"{SLOT_TAKEN_AT_CONFIRM_MESSAGE}\n\nDigite *menu* para escolher outro horário."
)

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
        body=booking_gate_body(hold.get("email_masked"), when=hold.get("when")),
        buttons=list(CODE_NOTICE_BUTTONS),
        event="booking_gate_notice_sent",
    )

async def _promote_booking_hold(
    reply: _ReplyContext,
    *,
    tenant: Tenant | None,
    waba_token: str | None,
    redis,
) -> str | SlotTakenAtPromotion | None:
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

    The DOUBLE CHECK (TASK-072, owner decision 2026-10-29) comes before the
    create: a code-phase hold reserves nothing, so while the code was being typed
    the window may have been filled on the doctor's agenda (another patient, the
    clinic, Google), taken by another patient's Pix, or simply passed. Each of
    those is "taken": nothing is created, the hold is released and a
    `SlotTakenAtPromotion` carries the day picker back, with service, doctor,
    convenio and attendee kept. A failed calendar read books as before (fail-open).

    The row's `expires_at` is NOT a reason to refuse any more: it only bounds how
    long the choice is remembered (`services/booking_hold.py::HOLD_TTL_MINUTES`),
    never a reservation, so a patient whose code arrived late still books when the
    slot is free.
    """
    if tenant is None:
        return None
    async with async_session_factory() as session:
        held = await latest_hold_in(session, reply.conversation_id)
    if held is None:
        return None
    if held.phase == "payment":
        # The reservation waits for a PIX, not a code: only the payment may create the
        # appointment (`services/payments/fulfillment.py`). Promoting here would confirm a
        # consultation nobody paid for.
        return None

    async with async_session_factory() as session:
        # The owner is resolved against ORM rows read here, never against the
        # flow router's roster snapshot: those are SimpleNamespace copies, and
        # `_appointment_calendar` only recognises a real `Professional`. A
        # snapshot used to fall through to the CLINIC calendar, so every gated
        # Portal booking at a multi-doctor clinic landed off the doctor's own
        # agenda - the slot stayed free there (double booking) and the
        # reschedule, which acts on the doctor's agenda, hit a 404 and looped.
        target = _appointment_calendar_target(
            {"professional_id": held.professional_id},
            await list_active_professionals(session, tenant.id),
        )
        calendar = await _appointment_calendar(session, tenant, target)
        patient_id = await session.scalar(
            select(Conversation.patient_id).where(Conversation.id == reply.conversation_id)
        )
    if calendar is None:
        # No agenda to book on. The row is KEPT: it costs nothing, reserves
        # nothing, and carries what a human needs to rescue this booking.
        logger.error(
            "booking_hold_promote_no_calendar",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        await _set_conversation_human_active(reply.conversation_id)
        return _PROMOTE_FAILED_MESSAGE

    if await _held_slot_unavailable(reply, tenant, held, calendar):
        # Released, unlike the no-calendar case above: there is nothing left to
        # rescue - the time belongs to somebody else's event now.
        await _release_hold(held.id)
        return await _slot_taken_reoffer(reply, tenant, held, calendar)

    service_type = held.appointment_type or "Consulta"
    # The attendee's name titles the event on a booking for someone else, as
    # on the ungated path (flow_router._handle_confirmation).
    event_name = held.attendee_name or await _patient_display_name(patient_id)
    summary = f"{service_type} - {event_name}" if event_name else service_type
    try:
        # Exclusive on the secretarIA agenda (atomic with its own re-check); plain on
        # Google, where the re-check above is the whole protection.
        event = await create_event_exclusive(
            calendar,
            start=held.start_at, end=held.end_at, summary=summary,
            description=build_event_description(
                service=service_type, insurance=held.insurance,
                channel=reply.channel, attendee_name=held.attendee_name,
            ),
        )
    except CalendarSlotTakenError:
        # Taken between the re-check and the write: exactly the taken re-check's answer.
        await _release_hold(held.id)
        return await _slot_taken_reoffer(reply, tenant, held, calendar)
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
    # TASK-032 R3: the appointment a "Marcar outra" booking replaced, if any.
    replaced: ReplacedAppointment | None = None
    carried = None  # TASK-052: the paid deposit this booking carried over, if any
    # TASK-052: ask the provider BEFORE the transaction (bounded wait; failure = new charge).
    carry_check = await release_carry_check.ensure_checked(reply.conversation_id)
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
                # TASK-052: a released PAID slot's deposit moves to this booking, in this
                # transaction (the Portal booking is born here, after the code), when REUSABLE.
                await session.flush()
                carried = await release_offer.claim_carry_over(
                    session,
                    tenant_id=tenant.id,
                    conversation_id=reply.conversation_id,
                    new_appointment=appointment,
                )
                # The Portal booking is confirmed HERE (the code gate), so the
                # original goes in this same transaction (spec §4.3).
                conversation = await session.get(Conversation, reply.conversation_id)
                if (
                    conversation is not None
                    and conversation.flow_replaces_appointment_id is not None
                ):
                    await session.flush()
                    replaced = await cancel_replaced_appointment(
                        session,
                        tenant=tenant,
                        patient_id=conversation.patient_id,
                        replaced_id=conversation.flow_replaces_appointment_id,
                        new_appointment_id=appointment.id,
                        waba_token=waba_token,
                    )
                    conversation.flow_replaces_appointment_id = None
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

    if replaced is not None:
        await _finish_replacement(tenant, replaced, redis=redis)

    tz = _tenant_tzinfo(tenant)
    local_start = held.start_at.astimezone(tz)
    local_end = held.end_at.astimezone(tz)
    confirmation = (
        "Pronto! Seu agendamento está confirmado. \u2705\n\n"
        f"{service_type}\n"
        + (f"Paciente: {held.attendee_name}\n" if held.attendee_name else "")
        + f"{local_start.strftime(WHEN_FORMAT)}\n\n"
        "Adicionar à sua agenda:\n"
        f"{build_patient_calendar_link(local_start, local_end, summary, tz=tz)}"
    )
    if replaced is not None:
        confirmation = f"{confirmation}\n\n{_replaced_notice(tenant, replaced)}"
    if carried is not None:
        confirmation = f"{confirmation}\n\n{release_offer.CARRIED_OVER_NOTE}"
    elif carry_check in (CARRY_CHECK_REFUNDED, CARRY_CHECK_UNKNOWN):
        confirmation = f"{confirmation}\n\n{release_offer.CARRY_REFUSED_NOTE}"
    return confirmation

async def _held_slot_unavailable(
    reply: _ReplyContext, tenant: Tenant, held: HeldSlot, calendar
) -> bool:
    """The commit-time double check of a gated booking (TASK-072). True = do not write it.

    Three ways the chosen window stopped being bookable while the code was typed, since a
    code-phase row reserves nothing:
      * it already started (a code typed very late);
      * another conversation's Pix now holds it (a payment-phase hold - the only kind that
        still blocks; read best-effort, a failed read does not stop the booking);
      * an event now sits on it in the calendar the booking would be written to
        (`slot_taken_at_confirm`, fail-open).
    """
    if held.start_at <= datetime.now(UTC):
        logger.info(
            "booking_slot_taken_at_confirm",
            source="booking_hold_promotion_past",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        return True
    try:
        async with async_session_factory() as session:
            pix_holds_it = await live_hold_overlaps(
                session,
                tenant.id,
                held.start_at,
                held.end_at,
                professional_id=held.professional_id,
            )
    except Exception as exc:
        logger.warning("booking_hold_promote_hold_check_failed", error_type=type(exc).__name__)
        pix_holds_it = False
    if pix_holds_it:
        logger.info(
            "booking_slot_taken_at_confirm",
            source="booking_hold_promotion_payment_hold",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        return True
    return await slot_taken_at_confirm(
        calendar,
        held.start_at,
        held.end_at,
        source="booking_hold_promotion",
        conversation_id=reply.conversation_id,
        tenant_id=tenant.id,
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

async def _slot_taken_reoffer(
    reply: _ReplyContext, tenant: Tenant, held: HeldSlot, calendar
) -> SlotTakenAtPromotion | str:
    """The DAY picker again, for the doctor, service, convênio and attendee the hold carried.

    TASK-072 (owner decision 2026-10-29): the patient goes back to "Para quando você
    gostaria? Escolha um dia:" prefixed with the "just taken" line, and only picks a new
    day and time; their account is verified by now, so the next "Confirmar" commits without
    another code (the gate stands down on NOT_PENDING).

    `_handle_confirmation` cleared the flow fields when it placed the hold (the hold row
    carries them instead), so the booking is rebuilt here from `held` onto a FRESH
    conversation snapshot and handed to `flow_router.reoffer_taken_booking_slot`, on the
    calendar the promotion just read - the owner's. The doctor goes back into
    `flow_selected_professional_id` only when the clinic's roster would not imply it (the
    reverse of `resolve_booking_owner_id`), so a single-doctor clinic keeps reading its
    agenda exactly the way it did before the code. "Pra mim" is the empty marker on the
    conversation (a hold row stores no ""). A "Marcar outra" marker rides along, so the
    booking the patient makes next still replaces the original.
    """
    ctx = await _load_draft_context(reply, tenant)
    if ctx is None:
        return _SLOT_TAKEN_NO_REOFFER_MESSAGE
    async with async_session_factory() as session:
        replaces = await session.scalar(
            select(Conversation.flow_replaces_appointment_id).where(
                Conversation.id == reply.conversation_id
            )
        )
    implied = resolve_booking_owner_id(ctx.professionals, None)
    state = SimpleNamespace(
        **{
            **vars(ctx.conversation),
            "flow_selected_type": held.appointment_type,
            "flow_selected_professional_id": (
                None if held.professional_id == implied else held.professional_id
            ),
            "flow_selected_insurance": held.insurance,
            "flow_attendee_name": held.attendee_name or ATTENDEE_SELF,
            "flow_selected_day": None,
            "flow_selected_slot": None,
            "flow_draft": None,
            "flow_replaces_appointment_id": replaces,
            "flow_edit_draft": None,
        }
    )
    minutes = int((held.end_at - held.start_at).total_seconds() // 60)
    # Pix holds of OTHER conversations stay hidden on the slot list that follows, as on any.
    with booking_gate_scope(_turn_booking_gate(reply, tenant)):
        result = await reoffer_taken_booking_slot(
            state,
            ctx.tenant_snapshot,
            calendar,
            held.start_at,
            duration_minutes=minutes,
            professionals=ctx.professionals,
        )
    return SlotTakenAtPromotion(reoffer=result)

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
