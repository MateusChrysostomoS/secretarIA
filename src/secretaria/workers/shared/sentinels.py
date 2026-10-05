"""sentinels - split out of workers/tasks.py (TASK-023)."""

import json
from types import SimpleNamespace
from uuid import UUID

from secretaria.ai.graph import (
    BOOKING_DRAFT_SENTINEL_PREFIX,
    SELECT_PROFESSIONAL_SENTINEL_PREFIX,
    START_GUIDED_BOOKING_SENTINEL_PREFIX,
)
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Conversation,
    FlowState,
    Tenant,
)
from secretaria.services.booking_scope import (
    BOOKING_TOPOLOGY_MULTI,
    booking_topology,
    canonical_service_name,
)
from secretaria.services.flow_router import (
    ATTENDEE_STEPS,
    FlowRouterResult,
    MenuBubble,
    _enter_professional_services,
    _start_booking,
    enter_booking,
    enter_guided_booking,
    enter_manage_action,
    flows_enabled,
    main_menu_buttons,
    match_insurance_plan,
    menu_label,
)
from secretaria.services.insurance_catalog import (
    load_tenant_insurance,
)
from secretaria.services.patient_context import (
    load_upcoming_appointments,
)
from secretaria.services.service_catalog import (
    load_service_catalog,
)
from secretaria.services.tenant_config import (
    list_active_professionals,
    professional_appointment_types,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.flow_runner import (
    _apply_flow_result,
)
from secretaria.workers.shared.greeting import (
    _flow_tenant_snapshot,
)
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
)

logger = get_logger(__name__)


async def _handle_show_main_menu(
    reply: _ReplyContext,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
    source: str = "agent_tool",
) -> bool:
    """Non-destructive menu return. The ONE way back to the menu.

    Shared by the agent's `show_main_menu` tool (`source="agent_tool"`), the
    `/menu` family of commands (`source="command"`, PROMPT_FIX_18) and the
    malformed-sentinel fallbacks (`source="sentinel_fallback"`), so every
    surface produces the same effective menu and the same state write.

    NOTHING is deleted: `_apply_flow_result` resets the flow fields to MENU
    (its unconditional writes also clear flow_selected_professional_id /
    flow_selected_insurance) and the effective menu bubbles go out. History,
    the patient row, appointments, deposits and Google Calendar events all stay
    untouched - contrast `_handle_remove_context_command`, which is the only
    destructive path and is reachable only by its own exact literal command.

    Idempotent by construction: it consumes no input and derives the menu from
    the tenant + roster, so running it twice sends the same menu twice and
    leaves the same state.
    """
    if tenant is None:
        logger.warning(
            "worker_show_main_menu_without_tenant",
            conversation_id=str(reply.conversation_id),
            source=source,
        )
        return False
    result = FlowRouterResult(
        action="reply",
        bubbles=[
            MenuBubble(
                body=menu_label(tenant),
                labels=main_menu_buttons(),
            )
        ],
        flow_state=FlowState.MENU,
    )
    rendered = await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )
    logger.info(
        "conversation_menu_rendered",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        source=source,
        # The bot was active for this turn by construction (handover is checked
        # in `_persist_inbound_message`); recorded so the pairing with
        # `conversation_menu_requested` stays unambiguous in the logs.
        handover="bot_active",
        rendered=rendered,
    )
    return rendered

async def _handle_select_professional(
    reply: _ReplyContext,
    reply_text: str,
    tenant: Tenant | None,
    flow_snapshot: tuple[SimpleNamespace, SimpleNamespace] | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """LLM hand-back: re-enter the deterministic flow at the chosen doctor.

    `select_professional_and_continue` already resolved the name against the
    ACTIVE roster, so the id in the sentinel should re-resolve here; if it
    doesn't (deactivated in the same instant, or a malformed sentinel), fall
    back to the plain menu instead of dropping the turn.
    """
    raw_id = reply_text[len(SELECT_PROFESSIONAL_SENTINEL_PREFIX) :]
    professional = None
    try:
        professional_id = UUID(raw_id)
    except ValueError:
        logger.error("worker_select_professional_bad_sentinel", raw=raw_id[:64])
    else:
        professional = next((p for p in professionals or [] if p.id == professional_id), None)
    tenant_snapshot = flow_snapshot[1] if flow_snapshot is not None else tenant
    if professional is None or tenant_snapshot is None:
        logger.warning(
            "worker_select_professional_unresolved",
            conversation_id=str(reply.conversation_id),
        )
        await _handle_show_main_menu(
            reply,
            tenant,
            professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
            source="sentinel_fallback",
        )
        return
    result = _enter_professional_services(professional, tenant_snapshot)
    # Built here, not by route(), so `_carry_booking` never saw it: keep the
    # authorized attendee and the convênio answer of the booking this hand-back
    # continues (an unanswered convênio is asked at "Sim, agendar").
    if flow_snapshot is not None and result.flow_state == FlowState.SERVICE_CATALOG:
        result.flow_attendee_name = getattr(flow_snapshot[0], "flow_attendee_name", None)
        result.flow_selected_insurance = getattr(
            flow_snapshot[0], "flow_selected_insurance", None
        )
    await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )

async def _handle_manage_appointment(
    reply: _ReplyContext,
    action: str,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """LLM hand-back: re-enter the deterministic manage (cancel/reschedule) flow.

    `manage_existing_appointment` (ai/tools.py) already normalizes/validates
    `action` to "reschedule"/"cancel" before raising ManageAppointmentRequested,
    so an unrecognized suffix here means a malformed sentinel; fall back to the
    plain menu instead of dropping the turn, mirroring
    `_handle_select_professional`'s guard on a bad professional id. The tool
    itself is only ever exposed to flow-enabled tenants (see the `extra_tools`
    wiring in `_send_bot_reply`), so `tenant` being None or not
    `flows_enabled` here should not normally happen - guarded defensively with
    a count-only warning, same style as `_handle_show_main_menu`.

    Re-loads the patient's upcoming appointments FRESH in its own session
    (authoritative regardless of what this turn preloaded - the LLM may have
    taken several tool-call turns since) before handing off to
    `enter_manage_action`, the exact same deterministic entry a direct
    "Remarcar"/"Cancelar" button tap uses.
    """
    if action not in ("reschedule", "cancel"):
        logger.warning("worker_manage_appointment_bad_action", action=action[:32])
        await _handle_show_main_menu(
            reply,
            tenant,
            professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
            source="sentinel_fallback",
        )
        return
    if tenant is None or not flows_enabled(tenant):
        logger.warning(
            "worker_manage_appointment_without_flows",
            conversation_id=str(reply.conversation_id),
        )
        return

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        patient_id = conversation.patient_id if conversation is not None else None
        if patient_id is None:
            logger.warning(
                "worker_manage_appointment_no_patient",
                conversation_id=str(reply.conversation_id),
            )
            return
        appointments = await load_upcoming_appointments(session, tenant.id, patient_id)
        # Only the single-appointment reschedule opens the day picker in this
        # turn; every other branch (cancel, or 2+ appointments to pick from)
        # needs no agenda at all, so nothing is built for them.
        manage_calendar = None
        if action == "reschedule" and len(appointments) == 1:
            manage_calendar = await _appointment_calendar(
                session,
                tenant,
                _appointment_calendar_target(
                    appointments[0], await list_active_professionals(session, tenant.id)
                ),
            )

    result = await enter_manage_action(
        action, tenant, appointments, professionals, calendar=manage_calendar
    )
    await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )

async def _handle_start_guided_booking(
    reply: _ReplyContext,
    reply_text: str,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """LLM hand-back: resume the booking in the button flow, service in hand.

    The agent's OPTIONAL offer (ai/tools.py::start_guided_booking) — it decided
    the free-text conversation had got as far as naming the service and that
    buttons should take it from there. Where the flow resumes is
    `services/flow_router.py::enter_guided_booking`'s decision, not this
    function's: convênio when the clinic collects it, the day picker when it
    does not, exactly as the "Sim, agendar" tap behaves. This orchestrates —
    it re-reads state, builds the right calendar, and pipes the result to
    `_apply_flow_result` like the other three hand-back handlers.

    Everything is re-read FRESH in its own session, the same reason
    `_handle_manage_appointment` gives: the model may have spent several
    tool-call turns since whatever this turn preloaded, and the roster/selection
    it saw could be stale.

    WHOSE agenda the day picker reads is the one thing worth getting right.
    `_appointment_calendar_target` answers it with the machinery the manage
    flow already uses: the conversation's selected professional when it still
    resolves, the tenant calendar when no doctor was ever picked (on a clinic
    with a single active professional `load_tenant_config` has already resolved
    THEIR credentials into it), and None when a selection no longer resolves —
    which makes the picker reply `calendar_unavailable` instead of listing days
    off whichever agenda happened to be at hand.

    A multi-professional tenant is turned away here rather than served. The
    tool is already withheld from it (`_flow_handback_tools`), but that gate
    judges by the roster THIS TURN loaded, and a failed roster load maps to
    UNKNOWN and hands the tool over anyway — so the roster re-read above is
    also the second lock, and a clinic that turns out to be multi-doctor gets
    the menu instead of a picker built on the clinic-level agenda.
    """
    appointment_type = reply_text[len(START_GUIDED_BOOKING_SENTINEL_PREFIX) :].strip() or None
    if tenant is None or not flows_enabled(tenant):
        # The tool is only ever exposed to flow-enabled tenants, so this is a
        # defensive count-only warning, same style as _handle_manage_appointment.
        logger.warning(
            "worker_start_guided_booking_without_flows",
            conversation_id=str(reply.conversation_id),
        )
        return

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        selected_id = (
            conversation.flow_selected_professional_id if conversation is not None else None
        )
        selected_insurance = (
            conversation.flow_selected_insurance if conversation is not None else None
        )
        selected_attendee = (
            conversation.flow_attendee_name if conversation is not None else None
        )
        professional_rows = await list_active_professionals(session, tenant.id)
        service_catalog = await load_service_catalog(session, tenant.id)
        tenant_insurance = await load_tenant_insurance(session, tenant.id)
        booking_calendar = await _appointment_calendar(
            session,
            tenant,
            _appointment_calendar_target({"professional_id": selected_id}, professional_rows),
        )

    # The topology decided on the FRESH roster, not on whatever this turn saw.
    # `_flow_handback_tools` withholds the tool from a multi tenant, but it
    # judges by the roster the turn loaded — and that load can fail, which maps
    # to UNKNOWN and hands the tool over anyway. A clinic that is really
    # multi-doctor would then open a picker built on the CLINIC-level agenda:
    # days no individual doctor may have free. The menu is the honest answer,
    # and it is where a multi-doctor booking is supposed to start.
    if booking_topology(professional_rows) == BOOKING_TOPOLOGY_MULTI:
        logger.warning(
            "worker_start_guided_booking_multi_professional",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        await _handle_show_main_menu(
            reply,
            tenant,
            professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
            source="sentinel_fallback",
        )
        return

    # The SAME tenant-shaped snapshot `route()` always receives — never the raw
    # ORM row. It is what resolves the clinic's canonical catalog AND, on a
    # clinic with one active professional, substitutes THAT professional's own
    # services (see `_flow_tenant_snapshot`). Passing the row instead would
    # read the legacy `tenants.appointment_types` column, which is empty on
    # exactly the clinics that configure everything per-professional — the day
    # picker would then slot on the clinic default instead of the service's own
    # duration, and offer the patient the wrong lengths.
    tenant_snapshot = _flow_tenant_snapshot(
        tenant, professional_rows, service_catalog, tenant_insurance
    )

    result = await enter_guided_booking(
        tenant_snapshot,
        booking_calendar,
        appointment_type,
        conversation_id=reply.conversation_id,
        professional_id=selected_id,
        insurance=selected_insurance,
        professionals=professionals,
        attendee_name=selected_attendee,
    )
    logger.info(
        "conversation_guided_booking_entered",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        # Enums and flags only: whether a service came through and which step
        # the flow resumed at. Never the service name, never the convênio.
        has_type=appointment_type is not None,
        flow_step=result.flow_step,
    )
    await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )

async def _handle_set_booking_draft(
    reply: _ReplyContext,
    reply_text: str,
    tenant: Tenant | None,
    flow_snapshot: tuple[SimpleNamespace, SimpleNamespace] | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """LLM hand-back: resume the booking at the first step still missing.

    The superset of `_handle_start_guided_booking`: the agent may also name the
    doctor (multi-doctor clinics, which `start_guided_booking` turns away) and
    the convênio. Everything is re-read FRESH (same reason as its sibling) and
    the result goes through `_apply_flow_result`, the one persistence seam.
    """
    try:
        payload = json.loads(reply_text[len(BOOKING_DRAFT_SENTINEL_PREFIX) :])
        if not isinstance(payload, dict) or any(
            payload.get(key) is not None and not isinstance(payload[key], str)
            for key in ("t", "p", "i")
        ):
            raise ValueError("Invalid booking draft payload")
        appointment_type = payload.get("t") or None
        professional_id = UUID(payload["p"]) if payload.get("p") else None
        insurance_text = payload.get("i") or None
    except (ValueError, TypeError, KeyError):
        logger.warning(
            "worker_booking_draft_bad_sentinel", conversation_id=str(reply.conversation_id)
        )
        await _handle_show_main_menu(
            reply, tenant, professionals, patient_wa, redis=redis,
            waba_token=waba_token, source="sentinel_fallback",
        )
        return
    if tenant is None or not flows_enabled(tenant):
        logger.warning(
            "worker_booking_draft_without_flows", conversation_id=str(reply.conversation_id)
        )
        return

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        selected_id = professional_id or (
            conversation.flow_selected_professional_id if conversation is not None else None
        )
        stored_insurance = (
            conversation.flow_selected_insurance if conversation is not None else None
        )
        attendee = conversation.flow_attendee_name if conversation is not None else None
        stored_type = conversation.flow_selected_type if conversation is not None else None
        professional_rows = await list_active_professionals(session, tenant.id)
        service_catalog = await load_service_catalog(session, tenant.id)
        tenant_insurance = await load_tenant_insurance(session, tenant.id)
        selection_only = appointment_type is None and professional_id is None
        booking_calendar = None
        if not selection_only:
            booking_calendar = await _appointment_calendar(
                session,
                tenant,
                _appointment_calendar_target({"professional_id": selected_id}, professional_rows),
            )

    tenant_snapshot = _flow_tenant_snapshot(
        tenant, professional_rows, service_catalog, tenant_insurance
    )
    # A convênio the patient typed only counts when it names a real plan; anything
    # else is dropped so the flow ASKS instead of storing a made-up label.
    insurance = (
        match_insurance_plan(tenant_snapshot, insurance_text)
        if insurance_text is not None else stored_insurance
    )

    is_multi = booking_topology(professional_rows) == BOOKING_TOPOLOGY_MULTI
    professional = next((p for p in professional_rows if p.id == selected_id), None)
    fresh_services = (
        professional_appointment_types(professional, tenant_snapshot, service_catalog)
        if professional is not None else tenant_snapshot.appointment_types
    )
    canonical_type = canonical_service_name(fresh_services, appointment_type)
    if selection_only:
        # An unspecified service means administrative choices, never an inferred
        # procedure or a jump to availability. Revalidate stored selections using
        # this tenant's current roster/catalog rather than the LLM snapshot.
        stored_type = canonical_service_name(fresh_services, stored_type)
        if insurance_text is None and stored_insurance is not None:
            insurance = match_insurance_plan(tenant_snapshot, stored_insurance)
        if professional is not None:
            result = _enter_professional_services(professional, tenant_snapshot)
        elif attendee is not None or stored_type is not None:
            # `attendee is not None` includes ATTENDEE_SELF (""): the patient
            # already answered "Essa consulta é pra você?" earlier in this
            # booking, so the hand-back must NOT ask it again.
            result = _start_booking(tenant_snapshot, professional_rows, insurance=insurance)
        else:
            result = enter_booking(tenant_snapshot, professional_rows)
        if result.flow_state != FlowState.SERVICE_CATALOG:
            await _handle_show_main_menu(
                reply, tenant, professional_rows, patient_wa, redis=redis,
                waba_token=waba_token, source="sentinel_fallback",
            )
            return
        result.flow_selected_insurance = insurance
        result.flow_attendee_name = attendee
        if result.flow_step not in ATTENDEE_STEPS:
            result.flow_selected_type = stored_type
        await _apply_flow_result(
            reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
        )
        return
    if (
        (appointment_type is not None and canonical_type is None)
        or (appointment_type is None and (not is_multi or professional_id is None))
        or (selected_id is not None and professional is None)
    ):
        logger.warning(
            "worker_booking_draft_invalid_selection", conversation_id=str(reply.conversation_id)
        )
        await _handle_show_main_menu(
            reply, tenant, professional_rows, patient_wa, redis=redis,
            waba_token=waba_token, source="sentinel_fallback",
        )
        return
    appointment_type = canonical_type
    if is_multi:
        professional = next((p for p in professional_rows if p.id == selected_id), None)
        if professional is None:
            await _handle_show_main_menu(
                reply, tenant, professionals, patient_wa, redis=redis,
                waba_token=waba_token, source="sentinel_fallback",
            )
            return
        if appointment_type is None:
            result = _enter_professional_services(professional, tenant_snapshot)
            if result.flow_state == FlowState.SERVICE_CATALOG:
                result.flow_selected_insurance = insurance
                result.flow_attendee_name = attendee
        else:
            result = await enter_guided_booking(
                tenant_snapshot,
                booking_calendar,
                appointment_type,
                conversation_id=reply.conversation_id,
                professional_id=selected_id,
                insurance=insurance,
                services=professional_appointment_types(
                    professional, tenant_snapshot, service_catalog
                ),
                professionals=professional_rows,
                attendee_name=attendee,
            )
    else:
        result = await enter_guided_booking(
            tenant_snapshot,
            booking_calendar,
            appointment_type,
            conversation_id=reply.conversation_id,
            professional_id=selected_id,
            insurance=insurance,
            professionals=professional_rows,
            attendee_name=attendee,
        )
    logger.info(
        "conversation_booking_draft_entered",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        multi=is_multi,
        has_type=appointment_type is not None,
        flow_step=result.flow_step,
    )
    await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )
