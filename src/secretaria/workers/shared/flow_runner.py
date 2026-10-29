"""flow_runner - split out of workers/tasks.py (TASK-023)."""

from types import SimpleNamespace

from sqlalchemy import select, update

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    ConsentEvent,
    Conversation,
    Patient,
    RebookingDecline,
    Tenant,
)
from secretaria.models.deposit_release_offer import CARRY_CHECK_REFUNDED, CARRY_CHECK_UNKNOWN
from secretaria.plugins.appointment_change_notifications import (
    CHANGE_CANCELLED,
    CHANGE_RESCHEDULED,
    enqueue_appointment_change_notification,
)
from secretaria.plugins.post_booking import enqueue_post_booking_hooks
from secretaria.services import release_carry_check, release_offer, reminder_hooks
from secretaria.services.appointment_edit import EditDraft
from secretaria.services.appointment_replacement import (
    ReplacedAppointment,
    cancel_replaced_appointment,
)
from secretaria.services.appointment_status import (
    SOURCE_FLOW,
    log_status_transition,
)
from secretaria.services.attendee import (
    CONSENT_KIND_THIRD_PARTY_BOOKING,
    CONSENT_LEGAL_BASIS_THIRD_PARTY_BOOKING,
)
from secretaria.services.calendar import (
    CalendarService,
)
from secretaria.services.flow_router import (
    STEP_AWAITING_ATTENDEE_AUTH,
    STEP_EDIT_ATT_AUTH,
    FlowRouterResult,
    is_generic_menu_result,
    is_main_menu,
    main_menu_buttons,
    route,
)
from secretaria.services.insurance_catalog import (
    resolve_booking_plan_ids,
)
from secretaria.services.patient_context import (
    has_upcoming_appointment,
)
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.pii_pseudonymization import remember_attendee_name
from secretaria.services.professional_edit_outbox import record_professional_edit
from secretaria.workers.shared.appointment_edit_apply import (
    AppliedEdit,
    _finish_edit,
    _row_draft,
    apply_appointment_edit,
    compensate_appointment_edit,
)
from secretaria.workers.shared.appointment_edit_notification import (
    enqueue_professional_edit_notification,
)
from secretaria.workers.shared.booking_hold import (
    _log_booking_scope,
    _send_booking_gate_notice,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.deposit import (
    _apply_deposit_awareness,
)
from secretaria.workers.shared.deposit_request import (
    _abort_deposit_request,
    _send_deposit_request_notice,
)
from secretaria.workers.shared.dispatch import (
    _dispatch_bubbles,
)
from secretaria.workers.shared.draft_resolution import (
    _resume_booking_draft,
    _turn_booking_gate,
)
from secretaria.workers.shared.handover import (
    _handle_calendar_unavailable,
    _handle_professional_config_incomplete,
    _set_conversation_human_active,
)
from secretaria.workers.shared.llm_context import (
    _flow_turn_calendar,
)
from secretaria.workers.shared.replacement import (
    _finish_replacement,
    _replaced_notice,
)
from secretaria.workers.shared.service_change_actions import service_change_bubbles

logger = get_logger(__name__)


async def _run_flow(
    reply: _ReplyContext,
    conv_snapshot: SimpleNamespace,
    tenant_snapshot: SimpleNamespace,
    tenant_config,
    patient_name: str | None,
    patient_wa: str | None,
    upcoming_appointments: list[dict] | None = None,
    redis=None,
    tenant: Tenant | None = None,
    waba_token: str | None = None,
    professionals: list | None = None,
    flow_calendar: CalendarService | None = None,
    manage_calendar: CalendarService | None = None,
    manage_calendar_owned: bool = False,
    edit_context=None,
) -> bool:
    """Run the deterministic flow router for this turn.

    `conv_snapshot`/`tenant_snapshot` are detached plain copies of the flow-
    relevant fields (the router does no DB I/O). Returns True when the turn was
    fully handled (bubbles sent, or handed off on a calendar outage); False to
    fall through to the LLM agent. `tenant`/`waba_token` (already loaded by the
    caller) are threaded through to whichever send path fires.
    `professionals`/`flow_calendar` are the multi-doctor context loaded by
    `_send_bot_reply` (active snapshot + the selected professional's calendar).
    `manage_calendar` is the manage (cancel/reschedule) sub-flow's owning
    calendar and `manage_calendar_owned` says an owner was identified for this
    turn at all (`_manage_owner_calendar_target`) - together they replace
    `_flow_turn_calendar`, deliberately ignoring `flow_selected_professional_id`
    (a stale booking-flow selection must never decide the manage agenda).
    """
    # A turn that acts on an EXISTING appointment must use that appointment's
    # own agenda and no other. `manage_calendar_owned` says the caller
    # identified such an owner (`_manage_owner_calendar_target`); the calendar
    # itself may still be None because building it failed, and that must stay
    # a None the router degrades on - never a silent fallback to the tenant
    # agenda, which would cancel or reschedule on the wrong calendar.
    calendar = (
        manage_calendar
        if manage_calendar_owned
        else _flow_turn_calendar(conv_snapshot, tenant_config, flow_calendar)
    )
    # The booking gate for THIS turn (workers/shared/draft_resolution.py explains the
    # arming rule and the production bug it pins).
    gate = _turn_booking_gate(reply, tenant, redis=redis)
    try:
        result = await route(
            conv_snapshot,
            tenant_snapshot,
            calendar,
            reply.inbound_body,
            patient_name,
            upcoming_appointments=upcoming_appointments,
            professionals=professionals,
            gate=gate,
            edit_context=edit_context,
        )
    except Exception as exc:
        logger.warning(
            "worker_flow_router_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )
        return False

    if result.resume_draft and tenant is not None:
        # Pra-quem answered with an AI draft parked: the resolver lands it instead of the
        # list `result` would show (falls back to `result` on any failure).
        result = await _resume_booking_draft(reply, tenant, result, gate=gate)
    if reply.reminder_opening_appointment_id is not None and is_generic_menu_result(result):
        # TASK-032 R3: the reminder card that opened this turn replaces the plain
        # menu (its "Outro" covers it). Nothing is persisted: the conversation
        # stays where it was.
        logger.info(
            "reminder_opening_menu_suppressed",
            source="flow",
            conversation_id=str(reply.conversation_id),
        )
        return True
    return await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )


async def _apply_flow_result(
    reply: _ReplyContext,
    result: FlowRouterResult,
    patient_wa: str | None,
    redis=None,
    tenant: Tenant | None = None,
    waba_token: str | None = None,
) -> bool:
    """Persist a FlowRouterResult (+ any appointment) and dispatch its bubbles.

    Shared by the live flow router (`_run_flow`), the returning-patient
    resume/reset path, and every LLM-hand-back re-entry
    (`_handle_show_main_menu`/`_handle_select_professional`/
    `_handle_manage_appointment`) — i.e. the ONE seam every manage-flow
    result passes through, which is why the Pix-deposit hooks below
    (`_apply_deposit_awareness` up front, the cancel/reschedule money hooks
    inside the persist txn) live HERE rather than in each individual caller.
    Returns True when the turn was fully handled (bubbles sent, or handed off
    on a calendar outage); False for `delegate_llm`.
    """
    result = await _apply_deposit_awareness(reply, result, tenant, patient_wa, waba_token)

    # A third party's name was just captured (the authorization card): pin it
    # into the PII token map now, so it stays masked for the LLM even if this
    # booking is cancelled or abandoned before any row carries it.
    if (
        result.flow_step in (STEP_AWAITING_ATTENDEE_AUTH, STEP_EDIT_ATT_AUTH)
        and result.flow_attendee_name
    ):
        await remember_attendee_name(reply.conversation_id, result.flow_attendee_name)
    if isinstance(result.flow_edit_draft, dict):
        # Editing an existing booking can introduce a name without asking for it again.
        names = {
            part.get("attendee_name")
            for key in ("current", "original")
            if isinstance(part := result.flow_edit_draft.get(key), dict)
        }
        for name in names:
            if isinstance(name, str) and name:
                await remember_attendee_name(reply.conversation_id, name)

    # TASK-052 (owner 2026-10-12): a booking that may reuse a released slot's PAID deposit asks
    # the payment provider FIRST - outside the transaction, bounded wait, failure = new charge.
    carry_check = None
    if result.appointment is not None:
        carry_check = await release_carry_check.ensure_checked(reply.conversation_id)

    # Persist the new flow state (+ any booked appointment) in one short txn.
    persisted = True
    booked_appointment: Appointment | None = None
    cancellation_note: str | None = None
    # TASK-052: the line that says a released slot's paid deposit now covers this booking.
    carried_note: str | None = None
    # TASK-032 R3: the appointment a "Marcar outra" booking replaced, if any.
    replaced: ReplacedAppointment | None = None
    applied_edit: AppliedEdit | None = None
    # TASK-032 R2: the appointments this turn closed or moved, for the
    # reminder hooks that run after the commit.
    closed_appointment_id = None
    moved_appointment_id = None
    edit_patient_id = None
    # A7: (change, appointment id, new start, previous start) of what THIS turn
    # cancelled or moved at the patient's request - mailed to the clinic, the doctor
    # and the Portal patient only once the transaction committed.
    patient_changes: list[tuple] = []
    try:
        async with async_session_factory() as session:
            async with session.begin():
                if result.appointment_edit is not None:
                    conv = await session.scalar(
                        select(Conversation)
                        .where(
                            Conversation.id == reply.conversation_id,
                        )
                        .with_for_update()
                    )
                    if conv is None:
                        raise ValueError("edit_conversation_missing")
                    edit_patient_id = conv.patient_id
                else:
                    conv = await session.get(Conversation, reply.conversation_id)
                if conv is not None:
                    # TASK-032 R3: read BEFORE the writes below overwrite it.
                    replaced_id = conv.flow_replaces_appointment_id
                    conv.flow_state = result.flow_state
                    conv.flow_step = result.flow_step
                    conv.flow_selected_type = result.flow_selected_type
                    conv.flow_selected_day = result.flow_selected_day
                    conv.flow_selected_slot = result.flow_selected_slot
                    conv.flow_selected_professional_id = result.flow_selected_professional_id
                    conv.flow_selected_insurance = result.flow_selected_insurance
                    conv.flow_managing_appointment_id = result.flow_managing_appointment_id
                    conv.flow_attendee_name = result.flow_attendee_name
                    conv.flow_draft = result.flow_draft
                    conv.flow_replaces_appointment_id = result.flow_replaces_appointment_id
                    conv.flow_edit_draft = result.flow_edit_draft
                    if result.attendee_authorized and tenant is not None:
                        # The explicit "Confirmar" under the authorization
                        # sentence (services/attendee.py): the audit row, in
                        # the same transaction as the state it authorizes.
                        # Same subject handle every other consent row uses.
                        session.add(
                            ConsentEvent(
                                tenant_id=tenant.id,
                                wa_id=reply.patient_ref,
                                kind=CONSENT_KIND_THIRD_PARTY_BOOKING,
                                legal_basis=CONSENT_LEGAL_BASIS_THIRD_PARTY_BOOKING,
                            )
                        )
                    if result.appointment:
                        # `phone` is the patient's WhatsApp number, kept so a
                        # later cancel/reschedule can still reach them - NOT
                        # the channel-neutral send handle. On Brain-Message
                        # `patient_wa` falls back to `patient_ref`, a 36-char
                        # UUID that does not fit this VARCHAR(32) column: the
                        # INSERT was refused and rolled this whole transaction
                        # back AFTER the calendar event had been created,
                        # leaving the clinic an orphaned event and the patient
                        # a bare "agenda unavailable". A portal patient has no
                        # number, which is exactly what the nullable column and
                        # `Patient.wa_id` are for. The agent path resolves it
                        # the same way - ai/tools.py::_persist_appointment.
                        booking_phone: str | None = None
                        if conv.patient_id is not None:
                            booking_patient = await session.get(Patient, conv.patient_id)
                            if booking_patient is not None:
                                booking_phone = booking_patient.wa_id
                        (
                            booking_insurance_plan_id,
                            booking_insurance_professional_plan_id,
                        ) = await resolve_booking_plan_ids(
                            session,
                            conv.tenant_id,
                            result.appointment.get("insurance"),
                            result.appointment.get("professional_id"),
                        )
                        booked_appointment = Appointment(
                            tenant_id=conv.tenant_id,
                            patient_id=conv.patient_id,
                            conversation_id=conv.id,
                            phone=booking_phone,
                            status=AppointmentStatus.SCHEDULED,
                            # The clinic (or, in independent mode, the
                            # professional's own) plan the convênio text names,
                            # by id, for the Pix-deposit guard (both None for
                            # Particular / a typed plan / no answer).
                            insurance_plan_id=booking_insurance_plan_id,
                            insurance_professional_plan_id=booking_insurance_professional_plan_id,
                            **result.appointment,
                        )
                        session.add(booked_appointment)
                        # TASK-052: "Remarcar" on a slot the clinic released with a PAID deposit -
                        # the deposit moves to THIS booking in this transaction (no new charge)
                        # only when the provider check said REUSABLE.
                        await session.flush()
                        if await release_offer.claim_carry_over(
                            session,
                            tenant_id=conv.tenant_id,
                            conversation_id=conv.id,
                            new_appointment=booked_appointment,
                        ):
                            carried_note = release_offer.CARRIED_OVER_NOTE
                        # TASK-032 R3 ("Marcar outra consulta"): the original is
                        # cancelled HERE - same transaction as the new row, and
                        # only now that the new booking is confirmed. A failed
                        # persist rolls both back together.
                        if replaced_id is not None and tenant is not None:
                            await session.flush()
                            replaced = await cancel_replaced_appointment(
                                session,
                                tenant=tenant,
                                patient_id=conv.patient_id,
                                replaced_id=replaced_id,
                                new_appointment_id=booked_appointment.id,
                                waba_token=waba_token,
                            )
                    # Cancel/reschedule mirror the calendar action onto the
                    # platform row, scoped by tenant_id (google_event_id is
                    # indexed but not globally unique). Best-effort: the calendar
                    # is the source of truth, so a stale row never blocks the reply.
                    if result.appointment_cancel_id:
                        # Read BEFORE the write so the transition log can name
                        # the status we came from; the row is then reused by
                        # the money hook below instead of re-selected.
                        cancelled_appt = await session.scalar(
                            select(Appointment).where(
                                Appointment.google_event_id == result.appointment_cancel_id,
                                Appointment.tenant_id == conv.tenant_id,
                            )
                        )
                        previous_status = (
                            cancelled_appt.status if cancelled_appt is not None else None
                        )
                        await session.execute(
                            update(Appointment)
                            .where(
                                Appointment.google_event_id == result.appointment_cancel_id,
                                Appointment.tenant_id == conv.tenant_id,
                            )
                            .values(status=AppointmentStatus.CANCELLED)
                        )
                        if cancelled_appt is not None:
                            log_status_transition(
                                appointment_id=cancelled_appt.id,
                                tenant_id=conv.tenant_id,
                                old_status=previous_status,
                                new_status=AppointmentStatus.CANCELLED,
                                source=SOURCE_FLOW,
                                idempotency_key=f"cancel:{result.appointment_cancel_id}",
                            )
                            closed_appointment_id = cancelled_appt.id
                            # A replayed turn re-cancels nothing: no second mail.
                            if previous_status != AppointmentStatus.CANCELLED:
                                patient_changes.append(
                                    (CHANGE_CANCELLED, cancelled_appt.id, None, None)
                                )
                        # Money hook: resolve the deposit's outcome for this
                        # cancellation and carry the honest notice through to
                        # the reply dispatched below (PROMPT S3 section 4).
                        if tenant is not None:
                            if cancelled_appt is not None:
                                outcome = await deposit_lifecycle.on_appointment_cancelled(
                                    session,
                                    tenant=tenant,
                                    appointment=cancelled_appt,
                                    waba_token=waba_token,
                                )
                                if outcome is not None:
                                    cancelled_deposit = (
                                        await deposit_lifecycle.get_deposit_for_appointment(
                                            session, cancelled_appt.id
                                        )
                                    )
                                    if cancelled_deposit is not None:
                                        cancellation_note = deposit_lifecycle.cancellation_notice(
                                            outcome, tenant, cancelled_deposit
                                        )
                    if result.decline_reason:
                        # Business data (churn signal), written in the SAME txn
                        # as the flow state so an answer can never be recorded
                        # without the conversation having moved past the
                        # question. Tenant comes from the conversation, never
                        # from the message. The free text is patient content:
                        # it goes in this row and is never logged.
                        decline = result.decline_reason
                        owns_appointment = await session.scalar(
                            select(Appointment.id).where(
                                Appointment.id == decline["appointment_id"],
                                Appointment.tenant_id == conv.tenant_id,
                            )
                        )
                        if owns_appointment is not None:
                            session.add(
                                RebookingDecline(
                                    tenant_id=conv.tenant_id,
                                    appointment_id=decline["appointment_id"],
                                    reason_code=decline["reason_code"],
                                    reason_text=decline["reason_text"],
                                )
                            )
                    if result.appointment_reschedule:
                        resched = result.appointment_reschedule
                        # Read BEFORE the write (same reason as the cancel
                        # branch above) and reuse the row for the money hook.
                        resched_appt = await session.scalar(
                            select(Appointment)
                            .where(
                                Appointment.google_event_id == resched["google_event_id"],
                                Appointment.tenant_id == conv.tenant_id,
                                Appointment.patient_id == conv.patient_id,
                            )
                            .with_for_update()
                        )
                        previous_status = resched_appt.status if resched_appt is not None else None
                        # Before the UPDATE below, which refreshes the in-session row.
                        previous_start = resched_appt.start_at if resched_appt is not None else None
                        before_reschedule = (
                            _row_draft(resched_appt, tenant.timezone)
                            if resched_appt is not None and tenant is not None
                            else None
                        )
                        # The SAME row moves to the new window and stays LIVE -
                        # RESCHEDULED is not a tombstone (PROMPT_FIX_16, see the
                        # taxonomy on models/appointment.py). Its id,
                        # google_event_id and PixDeposit all carry over, which
                        # is exactly why every reader must keep counting it as
                        # upcoming and remindable.
                        await session.execute(
                            update(Appointment)
                            .where(
                                Appointment.google_event_id == resched["google_event_id"],
                                Appointment.tenant_id == conv.tenant_id,
                                Appointment.patient_id == conv.patient_id,
                            )
                            .values(
                                start_at=resched["start_at"],
                                end_at=resched["end_at"],
                                status=AppointmentStatus.RESCHEDULED,
                            )
                        )
                        if resched_appt is not None:
                            patient_changes.append(
                                (
                                    CHANGE_RESCHEDULED,
                                    resched_appt.id,
                                    resched["start_at"],
                                    previous_start,
                                )
                            )
                            if before_reschedule is not None:
                                after_reschedule = _row_draft(resched_appt, tenant.timezone)
                                fields = EditDraft(
                                    str(resched_appt.id),
                                    after_reschedule.current,
                                    before_reschedule.current,
                                ).changed()
                                notice = await record_professional_edit(
                                    session, resched_appt, fields
                                )
                                if notice is not None:
                                    applied_edit = AppliedEdit(
                                        appointment_id=resched_appt.id,
                                        moved=True,
                                        old_event_id=None,
                                        old_professional_id=resched_appt.professional_id,
                                        notice_id=str(notice.id),
                                        notice_version=notice.version,
                                        changed_fields=tuple(fields),
                                    )
                            log_status_transition(
                                appointment_id=resched_appt.id,
                                tenant_id=conv.tenant_id,
                                old_status=previous_status,
                                new_status=AppointmentStatus.RESCHEDULED,
                                source=SOURCE_FLOW,
                                # The moved window makes a REPLAY of this exact
                                # reschedule recognisable; re-running it is a
                                # no-op write, never a second move.
                                idempotency_key=(
                                    f"resched:{resched['google_event_id']}"
                                    f":{resched['start_at'].isoformat()}"
                                ),
                            )
                            # A moved booking is unconfirmed again even with
                            # the switch OFF (R1 reschedule_reminders zeroes
                            # the counter), so a count > 0 also calls the hook.
                            if (
                                reminder_hooks.enabled_for(tenant)
                                or (resched_appt.confirmation_count or 0) > 0
                            ):
                                moved_appointment_id = resched_appt.id
                        # Money hook: count this reschedule against the
                        # deposit's limit. Non-crashing on a race (entry was
                        # already pre-checked by _apply_deposit_awareness /
                        # the button carrier's own check) — never unwind an
                        # already-persisted reschedule over a counter race.
                        if tenant is not None:
                            if resched_appt is not None:
                                allowed, _count = await deposit_lifecycle.register_reschedule(
                                    session, tenant=tenant, appointment=resched_appt
                                )
                                if not allowed:
                                    logger.warning(
                                        "pix_reschedule_limit_race",
                                        tenant_id=str(tenant.id),
                                        appointment_id=str(resched_appt.id),
                                    )
                    if result.appointment_edit:
                        # TASK-032 R6: the confirmed "Alterar Dados" edit - the SAME row, the
                        # same transaction as the flow state it closes.
                        applied_edit = await apply_appointment_edit(
                            session,
                            tenant=tenant,
                            tenant_id=conv.tenant_id,
                            patient_id=conv.patient_id,
                            edit=result.appointment_edit,
                        )
                        if applied_edit is None:
                            raise ValueError("edit_no_longer_allowed")
                        if applied_edit is not None and applied_edit.moved:
                            moved_appointment_id = applied_edit.appointment_id
                        if applied_edit.previous_start_at is not None:
                            # "Alterar Dados" changed the date/time: a move too.
                            patient_changes.append(
                                (
                                    CHANGE_RESCHEDULED,
                                    applied_edit.appointment_id,
                                    applied_edit.new_start_at,
                                    applied_edit.previous_start_at,
                                )
                            )
    except Exception as exc:
        persisted = False
        logger.error(
            "worker_flow_persist_failed",
            error=(type(exc).__name__ if result.appointment_edit else str(exc)),
            conversation_id=str(reply.conversation_id),
        )

    # Fire-and-forget: enqueue post_booking plugin hooks off the hot path,
    # mirroring ai/tools.py:_persist_appointment's agent-path enqueue — see
    # plugins/post_booking.py. Only when the appointment row actually made it
    # to the DB (never on a persist failure) and `tenant` is loaded (always
    # true here — the flow engine only runs once `tenant` is resolved).
    if result.appointment_edit is not None and not persisted:
        if tenant is not None:
            await compensate_appointment_edit(tenant, edit_patient_id, result.appointment_edit)
        await _handle_calendar_unavailable(reply, redis=redis, tenant=tenant, waba_token=waba_token)
        return True
    if booked_appointment is not None and persisted and tenant is not None:
        _log_booking_scope(booked_appointment, tenant.id, source=SOURCE_FLOW)
        await enqueue_post_booking_hooks(redis, tenant.id, booked_appointment.id, source="flow")
        if replaced is not None:
            await _finish_replacement(tenant, replaced, redis=redis)
        # TASK-032 R2: plan the reminders, in their own transaction AFTER the
        # booking committed - a reminder problem never costs a booking.
        if reminder_hooks.enabled_for(tenant):
            await reminder_hooks.after_appointment_booked(booked_appointment.id)
    if persisted and closed_appointment_id is not None and reminder_hooks.enabled_for(tenant):
        await reminder_hooks.after_appointment_closed(closed_appointment_id, reason="cancelled")
    if persisted and moved_appointment_id is not None:
        await reminder_hooks.after_appointment_rescheduled(moved_appointment_id)
    if persisted and applied_edit is not None and tenant is not None:
        await _finish_edit(tenant, applied_edit)
        await enqueue_professional_edit_notification(redis, tenant.id, applied_edit)
    # A7: the patient cancelled or moved - tell the clinic, the doctor (a cancel only:
    # a move already reached them through the edit notice just above) and the Portal
    # patient. Every manage entry funnels through here: the "Remarcar"/"Cancelar"
    # taps, the agent's manage hand-back, the reminder preselection, "Alterar Dados".
    # The hub never does, so a staff action is never announced twice.
    if persisted and tenant is not None:
        for change, changed_id, new_start, previous_start in patient_changes:
            await enqueue_appointment_change_notification(
                redis,
                tenant.id,
                changed_id,
                change,
                new_start_at=new_start,
                previous_start_at=previous_start,
            )

    # A HELD slot, not a booking: the router reserved the window and brain-api
    # mailed a code. Nothing was created on Google Calendar and no appointment
    # row exists, so there is nothing to enqueue post_booking hooks for - both
    # of those happen later, in `_promote_booking_hold`, and only if the code
    # arrives before the reservation expires. The flow-state write above
    # already parked the conversation in AWAITING_EMAIL_CODE.
    if result.booking_hold is not None:
        await _send_booking_gate_notice(
            reply, result.booking_hold, tenant=tenant, waba_token=waba_token
        )
        return True

    # A slot held for a DEPOSIT (`before` timing): the router persisted AWAITING_DEPOSIT above
    # and a Pix exists. Send it on this turn's channel; if the patient cannot be reached the
    # charge is voided and the slot freed, because a Pix nobody received would hold the slot
    # until it expires (spec 7). Nothing was created on Google or in `appointments`.
    if result.deposit_request is not None and tenant is not None:
        delivered = await _send_deposit_request_notice(
            reply, result.deposit_request, tenant=tenant, waba_token=waba_token
        )
        if not delivered:
            # TASK-071 Plan 3: the Pix is voided and the slot is booked WITHOUT the deposit.
            fallback = await _abort_deposit_request(
                reply, result.deposit_request, tenant=tenant, waba_token=waba_token, redis=redis
            )
            if fallback is not None:
                await _apply_flow_result(
                    reply, fallback, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
                )
        return True

    if result.action == "calendar_unavailable":
        await _handle_calendar_unavailable(reply, redis=redis, tenant=tenant, waba_token=waba_token)
        return True
    # A booking was made on Google Calendar but recording it failed: do NOT
    # tell the patient it is confirmed. Hand off to a human to reconcile the
    # (now orphaned) event instead of claiming success or risking a re-book.
    if result.appointment is not None and not persisted:
        await _handle_calendar_unavailable(reply, redis=redis, tenant=tenant, waba_token=waba_token)
        return True
    # The professional the patient reached cannot be booked at all because
    # THEIR static config is incomplete. Tell the patient, then alert the
    # clinic AND that doctor. No handover: the other doctors are fine, and a
    # human secretary cannot conjure a schedule either - what is missing is a
    # config change, which is exactly what the email asks for.
    if result.action == "professional_config_incomplete":
        await _handle_professional_config_incomplete(
            reply, result, redis=redis, tenant=tenant, waba_token=waba_token
        )
        return True
    # A scoped-help node escalated: flip to human handover FIRST (mirroring
    # _handle_calendar_unavailable's order - if the send below fails, the
    # human is already on it), then tell the patient. The setter sends the
    # operational handoff alert after committing the transition.
    if result.action == "handover":
        await _set_conversation_human_active(reply.conversation_id)
        if result.bubbles:
            await _dispatch_bubbles(reply, result.bubbles, tenant=tenant, waba_token=waba_token)
        return True
    if (
        result.action == "reply"
        and persisted
        and applied_edit is not None
        and applied_edit.service_change is not None
        and result.appointment_edit is not None
    ):
        # 2026-10-26: a service change on a paid sinal - the reply follows the money (the Pix
        # of the difference, the refund question, or why nothing changed).
        result.bubbles = service_change_bubbles(
            result.bubbles,
            applied_edit.service_change,
            new_service=str(result.appointment_edit.get("appointment_type") or ""),
            tenant=tenant,
        )
    if result.action == "reply":
        if result.bubbles:
            if cancellation_note:
                result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{cancellation_note}"
            if carried_note and persisted:
                result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{carried_note}"
            elif carry_check in (CARRY_CHECK_REFUNDED, CARRY_CHECK_UNKNOWN) and persisted:
                note = release_offer.CARRY_REFUSED_NOTE
                result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{note}"
            if replaced is not None and persisted and tenant is not None:
                notice = _replaced_notice(tenant, replaced)
                result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{notice}"
            await _offer_manage_on_main_menu(reply, result.bubbles)
            await _dispatch_bubbles(reply, result.bubbles, tenant=tenant, waba_token=waba_token)
        return True
    return False  # delegate_llm


async def _offer_manage_on_main_menu(reply: _ReplyContext, bubbles: list) -> None:
    """Add "Gerenciar consulta" to the main menu when the patient has something booked.

    HERE, at the one seam every menu passes through - the router's own menus,
    `/menu`, the reactivation "Não" and the agent's `show_main_menu` hand-back
    all arrive through `_apply_flow_result` - and AFTER this turn's write
    committed, so a menu that follows the patient's last cancellation drops
    the entry and one that follows a booking gains it. Read fresh from the
    database rather than from the turn's snapshot for exactly that reason.

    Fail-open: on any error the plain menu goes out, which is what every
    patient saw before (Agendar + Outro; typing "Remarcar" still works).
    """
    menus = [bubble for bubble in bubbles if is_main_menu(bubble)]
    if not menus or reply.conversation_id is None:
        return
    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, reply.conversation_id)
            if conversation is None or conversation.patient_id is None:
                return
            upcoming = await has_upcoming_appointment(
                session, conversation.tenant_id, conversation.patient_id
            )
    except Exception as exc:
        logger.warning(
            "main_menu_upcoming_lookup_failed",
            error_type=type(exc).__name__,
            conversation_id=str(reply.conversation_id),
        )
        return
    if upcoming:
        for menu in menus:
            menu.labels = main_menu_buttons(has_upcoming=True)
