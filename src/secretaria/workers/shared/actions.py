"""actions - split out of workers/tasks.py (TASK-023)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    PixDepositStatus,
    Professional,
    Tenant,
    is_live_status,
)
from secretaria.services import reminder_hooks
from secretaria.services.appointment_status import (
    SOURCE_BUTTON,
    log_status_transition,
)
from secretaria.services.calendar import (
    CalendarService,
)
from secretaria.services.flow_router import (
    enter_decline_reasons,
    enter_manage_action,
    enter_rebooking,
    flows_enabled,
    rebooking_candidates,
)
from secretaria.services.patient_context import (
    load_upcoming_appointments,
)
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.reminder_text import REMINDER_ROW_ACTIONS
from secretaria.services.service_catalog import (
    load_service_catalog,
)
from secretaria.services.tenant_config import (
    get_waba_token,
    list_active_professionals,
    load_tenant_config,
    resolve_professional_calendar,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.deposit import (
    _hours_until_start,
    _pix_retention_warning_line,
    _send_reschedule_limit_buttons,
)
from secretaria.workers.shared.flow_runner import (
    _apply_flow_result,
)
from secretaria.workers.shared.greeting import (
    _format_appointment_when,
)
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
)
from secretaria.workers.shared.reminder_actions import handle_reminder_button
from secretaria.workers.shared.sender import (
    _reply_sender,
    _send_simple_text,
)
from secretaria.workers.shared.text import (
    _as_utc,
)

logger = get_logger(__name__)


_APPOINTMENT_NOT_FOUND_TEXT = "Não encontrei essa consulta."

async def _calendar_for_appointment(
    session: AsyncSession,
    tenant: Tenant,
    tenant_config,
    appointment: Appointment,
) -> CalendarService | None:
    """The calendar that owns `appointment`: the booking professional's own
    resolved calendar, or the tenant-level one. Mirrors
    `_manage_owner_calendar_target` + `resolve_professional_calendar`'s
    resolution, but reads straight off the appointment ROW (no
    upcoming-appointments dict lookup needed — the caller already holds it).

    A booked-with professional that no longer resolves (deleted/deactivated)
    returns None rather than silently falling back to the TENANT calendar —
    mirroring `_manage_owner_calendar_target`'s own "don't guess, degrade"
    rule. Guessing wrong here is worse than skipping: `cancel_event` treats a
    404 as "already gone" (success), so calling it against the WRONG
    calendar could silently no-op while the event still lives on the
    professional's own agenda.
    """
    if appointment.professional_id is not None:
        professional = await session.get(Professional, appointment.professional_id)
        if professional is None:
            return None
        try:
            return await resolve_professional_calendar(
                session, tenant, professional, tenant_config=tenant_config
            )
        except Exception as exc:
            logger.warning(
                "action_button_professional_calendar_failed",
                error=str(exc),
                professional_id=str(appointment.professional_id),
            )
            return None
    return CalendarService.from_tenant_config(tenant_config) if tenant_config else None

async def _execute_appointment_cancel(
    session: AsyncSession,
    tenant: Tenant,
    tenant_config,
    appointment: Appointment,
    waba_token: str | None,
) -> str:
    """Cancel `appointment` (Calendar delete + status + deposit outcome).

    Mirrors flow_router._manage_cancel's happy path, for the button-driven
    carrier (no flow_state involved). Returns the patient-facing reply text,
    including the deposit `cancellation_notice` when there is one. The
    Calendar delete is best-effort: the platform row is the source of truth
    for a patient's cancel request even when Calendar is unreachable (same
    philosophy as deposit_lifecycle's own best-effort Calendar deletes).
    """
    if appointment.google_event_id:
        calendar = await _calendar_for_appointment(session, tenant, tenant_config, appointment)
        if calendar is not None:
            try:
                await calendar.cancel_event(appointment.google_event_id)
            except Exception as exc:
                logger.warning(
                    "action_button_calendar_cancel_failed",
                    error=str(exc),
                    appointment_id=str(appointment.id),
                )

    previous_status = appointment.status
    appointment.status = AppointmentStatus.CANCELLED
    log_status_transition(
        appointment_id=appointment.id,
        tenant_id=tenant.id,
        old_status=previous_status,
        new_status=AppointmentStatus.CANCELLED,
        source=SOURCE_BUTTON,
        idempotency_key=f"apptcancel:{appointment.id}",
    )
    text = "Consulta cancelada."
    try:
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment, waba_token=waba_token
        )
        if outcome is not None:
            deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
            if deposit is not None:
                notice = deposit_lifecycle.cancellation_notice(outcome, tenant, deposit)
                if notice:
                    text = f"{text} {notice}"
    except Exception as exc:
        logger.warning(
            "action_button_deposit_cancel_hook_failed",
            error=str(exc),
            appointment_id=str(appointment.id),
        )
    return text

async def _handle_action_button(
    reply: _ReplyContext, action: str, appointment_id: str, redis=None
) -> None:
    """Handle a tap on a reminder's deposit-aware action button.

    Runs before handover/flow/LLM routing (see `_persist_inbound_message`):
    these are structured, unambiguous commands tied to one appointment id,
    not a free-form conversational turn, so they fire even while a human has
    taken the conversation over — and they leave `Conversation.flow_state`
    untouched, except `apptresched`'s reschedule-entry branch, which sets its
    own (mirroring a direct "Remarcar" tap).

    Every lookup is scoped by the CALLER'S tenant_id (resolved from
    `reply.conversation_id`, never from the payload); an appointment id that
    resolves to nothing — or to another tenant's row — gets the same polite
    miss as a stale/expired one. The payload's appointment id is NEVER
    trusted on its own (see schemas/webhook.py::extract_action_button).
    """
    if reply.conversation_id is None:
        return
    try:
        appt_uuid = UUID(appointment_id)
    except ValueError:
        return  # already validated by extract_action_button; defensive only

    # TASK-032 R2/R3: the reminder buttons carry a REMINDER row id, not an
    # appointment id; their handler checks the row against the patient. Some
    # steps of the "Cancelar" path continue in a branch below, with the
    # (already checked) appointment id: the reschedule entry with its Pix limit,
    # "Não vou mais" confirmed, "Agendar Outra".
    if action in REMINDER_ROW_ACTIONS:
        continuation = await handle_reminder_button(reply, action, appointment_id, redis=redis)
        if continuation is None:
            return
        action, appointment_id = continuation
        appt_uuid = UUID(appointment_id)

    # Set only on the "enter the reschedule sub-flow" path (apptresched,
    # under the limit) - handled AFTER this session closes, mirroring
    # _handle_manage_appointment's own short-read-session-then-handoff shape.
    reschedule_handoff: tuple[Tenant, list[dict], list, str | None] | None = None
    # FEAT_34 §4: a tap on the cancellation notice's rebooking buttons. Same
    # shape as the reschedule handoff — resolved inside the session, acted on
    # after it closes.
    rebooking_handoff: tuple | None = None
    decline_handoff: tuple | None = None

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return
        waba_token = await get_waba_token(session, tenant.id)
        # Fail closed (PROMPT_FIX_21): the tap is already deduped by its
        # ProcessedEvent, so raising here would just retry a configuration
        # problem forever. The patient simply gets no answer to the tap.
        client = _reply_sender(reply, tenant, waba_token)
        if client is None:
            logger.error("worker_action_button_no_credential", tenant_id=str(tenant.id))
            return

        appointment = await session.scalar(
            select(Appointment).where(
                Appointment.id == appt_uuid, Appointment.tenant_id == tenant.id
            )
        )
        if appointment is None:
            await client.send_text_message(to=reply.patient_ref, body=_APPOINTMENT_NOT_FOUND_TEXT)
            return

        if action == "apptconfirm":
            now = datetime.now(UTC)
            is_future = appointment.start_at is not None and _as_utc(appointment.start_at) > now
            # LIVE, not a hand-written pair (PROMPT_FIX_16): a booking the
            # patient already rescheduled is still theirs to confirm - it used
            # to answer "essa consulta não está mais ativa" instead.
            if is_live_status(appointment.status) and is_future:
                previous_status = appointment.status
                appointment.status = AppointmentStatus.CONFIRMED
                log_status_transition(
                    appointment_id=appointment.id,
                    tenant_id=tenant.id,
                    old_status=previous_status,
                    new_status=AppointmentStatus.CONFIRMED,
                    source=SOURCE_BUTTON,
                    idempotency_key=f"apptconfirm:{appointment.id}",
                )
                when = _format_appointment_when(appointment.start_at, tenant.timezone)
                text = f"Presença confirmada! Até {when}."
            else:
                text = "Essa consulta não está mais ativa."
            await session.commit()
            await client.send_text_message(to=reply.patient_ref, body=text)
            return

        if action == "apptcancel":
            tenant_config = await load_tenant_config(session, tenant)
            deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
            hours_until = _hours_until_start(appointment, datetime.now(UTC))
            inside_window = (
                deposit is not None
                and deposit.status == PixDepositStatus.PAID
                and hours_until is not None
                and hours_until <= tenant.pix_refund_window_hours
            )
            if inside_window:
                warning = _pix_retention_warning_line(tenant, deposit)
                body = f"{warning} Cancelar mesmo assim ou prefere reagendar?"
                await client.send_buttons(
                    reply.patient_ref,
                    body,
                    [
                        (f"apptcancelyes|{appointment.id}", "Cancelar mesmo assim"),
                        (f"apptresched|{appointment.id}", "Reagendar"),
                    ],
                )
                return
            text = await _execute_appointment_cancel(
                session, tenant, tenant_config, appointment, waba_token
            )
            await session.commit()
            if reminder_hooks.enabled_for(tenant):
                await reminder_hooks.after_appointment_closed(appointment.id, reason="cancelled")
            await client.send_text_message(to=reply.patient_ref, body=text)
            return

        if action == "apptcancelyes":
            tenant_config = await load_tenant_config(session, tenant)
            text = await _execute_appointment_cancel(
                session, tenant, tenant_config, appointment, waba_token
            )
            await session.commit()
            if reminder_hooks.enabled_for(tenant):
                await reminder_hooks.after_appointment_closed(appointment.id, reason="cancelled")
            await client.send_text_message(to=reply.patient_ref, body=text)
            return

        if action == "rebookno":
            # The patient does not want to rebook. Ask WHY — deterministically,
            # with a fixed option list — because "vou procurar outra clínica"
            # and "não preciso mais" are the same tap and mean opposite things
            # commercially (FEAT_34 §8). Never handed to the LLM.
            #
            # Handed off like the branches below rather than answered here:
            # `_apply_flow_result` opens its OWN session, and calling it while
            # this one is still open nests two sessions on the same engine.
            decline_handoff = (tenant, waba_token, appointment.id)

        if action in ("rebooksame", "rebookother"):
            # Rebooking after the DOCTOR cancelled. The cancelled appointment
            # stays CANCELLED and is NOT reopened (FEAT_34 §4.3) — confirming
            # produces a new booking through the normal tail.
            if not flows_enabled(tenant):
                await client.send_text_message(
                    to=reply.patient_ref,
                    body="Para remarcar, entre em contato com a nossa equipe.",
                )
                return
            professional_rows = await list_active_professionals(session, tenant.id)
            professionals = [
                SimpleNamespace(
                    id=p.id,
                    name=p.name,
                    specialty=p.specialty,
                    about=p.about,
                    context_doctor_message=p.context_doctor_message,
                    appointment_types=p.appointment_types,
                    # Verbatim, NULL and all: the router reads it through
                    # `professional_business_hours`, whose whole contract is
                    # that NULL inherits the clinic's hours and `{}` does not.
                    # Flattening it here would erase that distinction and make
                    # an inheriting doctor look unbookable.
                    business_hours=p.business_hours,
                )
                for p in professional_rows
            ]
            same = action == "rebooksame"
            service_name = appointment.appointment_type
            candidates: list = []
            prefix = None
            if same:
                target_id = appointment.professional_id
            else:
                services = await load_service_catalog(session, tenant.id)
                candidates, prefix = rebooking_candidates(
                    tenant,
                    professionals,
                    cancelled_professional_id=appointment.professional_id,
                    service_name=service_name,
                    services=services,
                )
                # Only a single candidate fixes the agenda in advance; with a
                # list the patient still picks, and their own calendar is
                # resolved when they do.
                target_id = candidates[0].id if len(candidates) == 1 else None
            rebook_calendar = await _appointment_calendar(
                session,
                tenant,
                _appointment_calendar_target({"professional_id": target_id}, professional_rows),
            )
            rebooking_handoff = (
                tenant,
                conversation,
                professionals,
                waba_token,
                rebook_calendar,
                same,
                appointment.professional_id,
                service_name,
                candidates,
                prefix,
                appointment.attendee_name,
            )

        if action == "apptresched":
            deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
            if deposit is not None and deposit.reschedule_count >= tenant.pix_reschedule_limit:
                await _send_reschedule_limit_buttons(
                    client,
                    reply.patient_ref,
                    appointment.id,
                    deposit.reschedule_count,
                    tenant.pix_reschedule_limit,
                )
                return
            if not flows_enabled(tenant):
                # Patient self-service reschedule (deterministic OR
                # LLM-mediated - see ai/tools.py's ManageAppointmentRequested)
                # is a flow-only capability throughout this codebase; there is
                # no non-flow equivalent to hand off to.
                await client.send_text_message(
                    to=reply.patient_ref,
                    body="Para remarcar essa consulta, entre em contato com a nossa equipe.",
                )
                return
            if appointment.patient_id is None:
                await client.send_text_message(
                    to=reply.patient_ref, body=_APPOINTMENT_NOT_FOUND_TEXT
                )
                return
            appointments = await load_upcoming_appointments(
                session, tenant.id, appointment.patient_id
            )
            professional_rows = await list_active_professionals(session, tenant.id)
            professionals = [
                SimpleNamespace(
                    id=p.id,
                    name=p.name,
                    specialty=p.specialty,
                    about=p.about,
                    context_doctor_message=p.context_doctor_message,
                    appointment_types=p.appointment_types,
                    # Verbatim, NULL and all: the router reads it through
                    # `professional_business_hours`, whose whole contract is
                    # that NULL inherits the clinic's hours and `{}` does not.
                    # Flattening it here would erase that distinction and make
                    # an inheriting doctor look unbookable.
                    business_hours=p.business_hours,
                )
                for p in professional_rows
            ]
            # The reschedule opens the day picker in this very turn, so it
            # needs THIS appointment's own agenda resolved here, inside the
            # session. A failure leaves it None and the picker answers
            # `calendar_unavailable` - never a day list off the wrong calendar.
            reschedule_calendar = await _appointment_calendar(
                session,
                tenant,
                _appointment_calendar_target(
                    {"professional_id": appointment.professional_id}, professional_rows
                ),
            )
            reschedule_handoff = (
                tenant,
                appointments,
                professionals,
                waba_token,
                reschedule_calendar,
            )

    if decline_handoff is not None:
        dh_tenant, dh_waba_token, dh_appointment_id = decline_handoff
        await _apply_flow_result(
            reply,
            enter_decline_reasons(dh_appointment_id),
            reply.patient_ref,
            redis=redis,
            tenant=dh_tenant,
            waba_token=dh_waba_token,
        )
        return

    if rebooking_handoff is not None:
        (
            rb_tenant,
            rb_conversation,
            rb_professionals,
            rb_waba_token,
            rb_calendar,
            rb_same,
            rb_cancelled_professional_id,
            rb_service_name,
            rb_candidates,
            rb_prefix,
            rb_attendee_name,
        ) = rebooking_handoff
        result = await enter_rebooking(
            rb_conversation,
            rb_tenant,
            rb_calendar,
            professionals=rb_professionals,
            cancelled_professional_id=rb_cancelled_professional_id,
            service_name=rb_service_name,
            same_professional=rb_same,
            candidates=rb_candidates,
            prefix=rb_prefix,
        )
        # Rebooking the SAME person the doctor cancelled on: a booking made for
        # a third party (already authorized for that appointment) stays theirs.
        if rb_attendee_name and result.flow_state == FlowState.SERVICE_CATALOG:
            result.flow_attendee_name = rb_attendee_name
        await _apply_flow_result(
            reply,
            result,
            reply.patient_ref,
            redis=redis,
            tenant=rb_tenant,
            waba_token=rb_waba_token,
        )
        return

    if reschedule_handoff is not None:
        (
            handoff_tenant,
            appointments,
            professionals,
            handoff_waba_token,
            reschedule_calendar,
        ) = reschedule_handoff
        result = await enter_manage_action(
            "reschedule",
            handoff_tenant,
            appointments,
            professionals,
            preselected_id=appt_uuid,
            calendar=reschedule_calendar,
        )
        await _apply_flow_result(
            reply,
            result,
            reply.patient_ref,
            redis=redis,
            tenant=handoff_tenant,
            waba_token=handoff_waba_token,
        )

# Fixed pt-BR degrade per known greeting-button action, for a tenant with
# flows disabled (flow_router.flows_enabled) - see
# _handle_greeting_button_unavailable. There is no non-flow equivalent to
# hand these off to (same acknowledged limitation as _handle_action_button's
# apptresched-while-flows-disabled branch above), so the reply is a fixed
# "contact us" message, never the LLM. Any suffix not in this dict (a legacy
# pre-deploy numeric id, or anything else `extract_greeting_button` didn't
# recognize) gets the generic default below.
_GREETING_ACTION_UNAVAILABLE_TEXT: dict[str, str] = {
    "agendar": "Para agendar uma consulta, entre em contato com a nossa equipe.",
    "gerenciar": "Para remarcar ou cancelar sua consulta, entre em contato com a nossa equipe.",
    "remarcar": "Para remarcar sua consulta, entre em contato com a nossa equipe.",
    "cancelar": "Para cancelar sua consulta, entre em contato com a nossa equipe.",
}

_GREETING_ACTION_UNAVAILABLE_DEFAULT = (
    "Não consigo processar esse pedido automaticamente. "
    "Entre em contato com a nossa equipe, por favor."
)

async def _handle_greeting_button_unavailable(reply: _ReplyContext, suffix: str) -> None:
    """Deterministic degrade for a greeting-button tap a flows-disabled
    tenant can't fulfil (see `_persist_inbound_message`'s greeting-button
    short-circuit, which is the only place that sets
    `reply.greeting_button_unavailable`). A fully self-contained turn (its
    own tenant lookup, its own reply), mirroring `_handle_action_button`'s
    shape: never falls through to the entitlement gate, deterministic flow,
    or LLM.
    """
    if reply.conversation_id is None:
        return
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return
        waba_token = await get_waba_token(session, tenant.id)
        client = _reply_sender(reply, tenant, waba_token)
    if client is None:
        return  # fail closed - never on the global scaffold (PROMPT_FIX_21)
    text = _GREETING_ACTION_UNAVAILABLE_TEXT.get(suffix, _GREETING_ACTION_UNAVAILABLE_DEFAULT)
    await _send_simple_text(reply.patient_ref, text, client=client)
