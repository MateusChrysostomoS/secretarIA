"""sentinels - split out of workers/tasks.py (TASK-023)."""

from collections.abc import Mapping, Sequence
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
from secretaria.services.booking_draft import BookingDraft
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
    ai_draft_v2_enabled,
    enter_guided_booking,
    enter_manage_action,
    flows_enabled,
    main_menu_buttons,
    match_insurance_plan,
    menu_label,
)
from secretaria.services.patient_context import (
    load_upcoming_appointments,
)
from secretaria.services.tenant_config import (
    list_active_professionals,
    professional_appointment_types,
)
from secretaria.workers.shared import handback_log as hb
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.draft_resolution import (
    DraftContext,
    _load_draft_context,
    _resolve_draft,
)
from secretaria.workers.shared.flow_runner import (
    _apply_flow_result,
)
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
)

logger = get_logger(__name__)

# `_handle_show_main_menu`'s `source` when the agent's own `show_main_menu` tool asked for the
# menu - the only caller of that function that is an AI hand-back.
_AGENT_TOOL_SOURCE = "agent_tool"


async def _handle_show_main_menu(
    reply: _ReplyContext,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
    source: str = _AGENT_TOOL_SOURCE,
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

    Hand-back accounting: only `source="agent_tool"` records a
    `conversation_handback_entered` event here. `/menu`, the name step and the
    identity cards render the same menu without the AI being involved, and the
    malformed-sentinel fallbacks are recorded by the handler that fell back
    (`_fallback_to_menu`), so one hand-back is never counted twice.
    """
    if tenant is None:
        logger.warning(
            "worker_show_main_menu_without_tenant",
            conversation_id=str(reply.conversation_id),
            source=source,
        )
        if source == _AGENT_TOOL_SOURCE:
            hb.log_handback_entered(
                conversation_id=reply.conversation_id,
                tenant_id=reply.tenant_id,
                source_tool=hb.SOURCE_SHOW_MAIN_MENU,
                landing_step=None,
                fallback=hb.FALLBACK_NO_TENANT,
                topology=booking_topology(professionals),
                channel=reply.channel,
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
    if source == _AGENT_TOOL_SOURCE:
        # Logged before the write and the send, so a failure there cannot erase the fact
        # that the hand-back reached the menu.
        hb.log_handback_entered(
            conversation_id=reply.conversation_id,
            tenant_id=tenant.id,
            source_tool=hb.SOURCE_SHOW_MAIN_MENU,
            landing_step=hb.LANDING_MENU,
            topology=booking_topology(professionals),
            channel=reply.channel,
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


async def _fallback_to_menu(
    reply: _ReplyContext,
    *,
    source_tool: str,
    reason: str,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
    supplied: Sequence[str] = (),
    accepted: Sequence[str] = (),
    dropped: Mapping[str, str] | None = None,
    topology: str | None = None,
) -> None:
    """A hand-back that could not land: record it ONCE, then show the plain menu.

    The menu itself goes through `_handle_show_main_menu(source="sentinel_fallback")`, which
    records no hand-back event of its own - this is the single place the fallback is counted,
    so a hand-back is never logged twice or not at all. `landing_step` is None without a
    tenant: `_handle_show_main_menu` renders nothing then, so nothing landed.
    """
    hb.log_handback_entered(
        conversation_id=reply.conversation_id,
        tenant_id=tenant.id if tenant is not None else reply.tenant_id,
        source_tool=source_tool,
        landing_step=hb.LANDING_MENU if tenant is not None else None,
        supplied=supplied,
        accepted=accepted,
        dropped=dropped,
        fallback=reason,
        topology=topology if topology is not None else booking_topology(professionals),
        channel=reply.channel,
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


async def _land_handback(
    reply: _ReplyContext,
    result: FlowRouterResult,
    patient_wa: str | None,
    *,
    source_tool: str,
    tenant: Tenant | None,
    professionals: list | None,
    redis=None,
    waba_token: str | None = None,
    supplied: Sequence[str] = (),
    accepted: Sequence[str] = (),
    dropped: Mapping[str, str] | None = None,
    fallback: str | None = None,
    topology: str | None = None,
) -> bool:
    """Record where a hand-back lands, THEN persist the flow state and send the bubbles.

    The event goes first on purpose: `_apply_flow_result` writes the flow row and talks to
    WhatsApp / the Portal, and a failure there must not erase the fact that the hand-back
    reached a step. `fallback` names a bounce the CALLER detected (e.g. nothing to manage);
    otherwise the result itself decides - a calendar outage or an unbookable doctor is a
    fallback too (`handback_log.landing_of`).
    """
    landing_step, result_fallback = hb.landing_of(result)
    hb.log_handback_entered(
        conversation_id=reply.conversation_id,
        tenant_id=tenant.id if tenant is not None else reply.tenant_id,
        source_tool=source_tool,
        landing_step=landing_step,
        supplied=supplied,
        accepted=accepted,
        dropped=dropped,
        fallback=fallback or result_fallback,
        topology=topology if topology is not None else booking_topology(professionals),
        channel=reply.channel,
    )
    return await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )


def _log_no_landing(
    reply: _ReplyContext,
    *,
    source_tool: str,
    reason: str,
    tenant: Tenant | None,
    professionals: list | None,
    supplied: Sequence[str] = (),
    accepted: Sequence[str] = (),
    topology: str | None = None,
) -> None:
    """A hand-back that did nothing at all - no menu, no flow step - still gets its event."""
    hb.log_handback_entered(
        conversation_id=reply.conversation_id,
        tenant_id=tenant.id if tenant is not None else reply.tenant_id,
        source_tool=source_tool,
        landing_step=None,
        supplied=supplied,
        accepted=accepted,
        fallback=reason,
        topology=topology if topology is not None else booking_topology(professionals),
        channel=reply.channel,
    )


def _draft_verdicts(
    *,
    appointment_type: str | None,
    canonical_type: str | None,
    professional_id: UUID | None,
    professional: object | None,
    insurance_text: str | None,
    insurance: str | None,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Which of the fields the agent supplied survived validation, and why the rest did not.

    Pure and value-free: it returns field NAMES and reason CODES (`handback_log`), never the
    service, doctor or convênio themselves. A field the agent did not supply is in neither
    result - a convênio already stored on the conversation is not "supplied".
    """
    accepted: list[str] = []
    dropped: dict[str, str] = {}
    if appointment_type is not None:
        if canonical_type is not None:
            accepted.append(hb.FIELD_SERVICE)
        else:
            dropped[hb.FIELD_SERVICE] = hb.DROP_NOT_IN_CATALOG
    if professional_id is not None:
        if professional is not None:
            accepted.append(hb.FIELD_PROFESSIONAL)
        else:
            dropped[hb.FIELD_PROFESSIONAL] = hb.DROP_UNKNOWN_PROFESSIONAL
    if insurance_text is not None:
        if insurance is not None:
            accepted.append(hb.FIELD_INSURANCE)
        else:
            dropped[hb.FIELD_INSURANCE] = hb.DROP_UNMATCHED_PLAN
    return tuple(accepted), dropped


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

    TASK-030 P2: the roster, pra-quem and convênio are read FRESH (`_load_draft_context`);
    the turn-start `flow_snapshot` is no longer consulted - after several tool calls it can
    say the opposite of the row. With pra-quem unknown, or the clinic's draft v2 switch
    on, the resolver lands it (pra-quem first, the doctor parked in `flow_draft`);
    otherwise the doctor's own service list, as before. An id that does not resolve
    against the ACTIVE roster falls back to the plain menu.
    """
    raw_id = reply_text[len(SELECT_PROFESSIONAL_SENTINEL_PREFIX) :]
    professional_id = None
    bad_sentinel = False
    try:
        professional_id = UUID(raw_id)
    except ValueError:
        bad_sentinel = True
        logger.error(
            "worker_select_professional_bad_sentinel", conversation_id=str(reply.conversation_id)
        )
    ctx = (
        await _load_draft_context(reply, tenant)
        if tenant is not None and professional_id is not None
        else None
    )
    roster = ctx.professionals if ctx is not None else list(professionals or [])
    professional = next((p for p in roster if p.id == professional_id), None)
    if professional is None or tenant is None or ctx is None:
        logger.warning(
            "worker_select_professional_unresolved",
            conversation_id=str(reply.conversation_id),
        )
        # Most specific reason first: a malformed id, a doctor who is no longer on the
        # active roster, no tenant to render a menu for, or no conversation row at all.
        supplied: tuple[str, ...] = ()
        accepted: tuple[str, ...] = ()
        dropped: dict[str, str] = {}
        if bad_sentinel:
            reason = hb.FALLBACK_BAD_SENTINEL
        elif professional is None:
            reason = hb.FALLBACK_UNKNOWN_PROFESSIONAL
            supplied = (hb.FIELD_PROFESSIONAL,)
            dropped = {hb.FIELD_PROFESSIONAL: hb.DROP_UNKNOWN_PROFESSIONAL}
        elif tenant is None:
            reason = hb.FALLBACK_NO_TENANT
            supplied = accepted = (hb.FIELD_PROFESSIONAL,)
        else:
            reason = hb.FALLBACK_NO_PATIENT
            supplied = accepted = (hb.FIELD_PROFESSIONAL,)
        await _fallback_to_menu(
            reply,
            source_tool=hb.SOURCE_SELECT_PROFESSIONAL,
            reason=reason,
            tenant=tenant,
            professionals=professionals,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
            supplied=supplied,
            accepted=accepted,
            dropped=dropped,
        )
        return
    tenant = ctx.tenant
    draft = BookingDraft(professional_id=professional.id)
    if ai_draft_v2_enabled(tenant) or _pra_quem_unknown(ctx.conversation, draft):
        await _apply_draft_resolution(
            reply,
            tenant,
            draft,
            ctx,
            source_tool=hb.SOURCE_SELECT_PROFESSIONAL,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    result = _enter_professional_services(professional, ctx.tenant_snapshot)
    # Built here, not by route(), so `_carry_booking` never saw it: keep the authorized
    # attendee and the convênio answer of the booking this hand-back continues - FRESH.
    # (An unanswered convênio is asked at "Sim, agendar".)
    if result.flow_state == FlowState.SERVICE_CATALOG:
        result.flow_attendee_name = ctx.conversation.flow_attendee_name
        result.flow_selected_insurance = ctx.conversation.flow_selected_insurance
    await _land_handback(
        reply,
        result,
        patient_wa,
        source_tool=hb.SOURCE_SELECT_PROFESSIONAL,
        tenant=tenant,
        professionals=ctx.professional_rows,
        redis=redis,
        waba_token=waba_token,
        supplied=(hb.FIELD_PROFESSIONAL,),
        accepted=(hb.FIELD_PROFESSIONAL,),
        topology=ctx.topology,
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
        await _fallback_to_menu(
            reply,
            source_tool=hb.SOURCE_MANAGE_EXISTING_APPOINTMENT,
            reason=hb.FALLBACK_BAD_SENTINEL,
            tenant=tenant,
            professionals=professionals,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    if tenant is None or not flows_enabled(tenant):
        logger.warning(
            "worker_manage_appointment_without_flows",
            conversation_id=str(reply.conversation_id),
        )
        _log_no_landing(
            reply,
            source_tool=hb.SOURCE_MANAGE_EXISTING_APPOINTMENT,
            reason=hb.FALLBACK_NO_TENANT if tenant is None else hb.FALLBACK_WITHOUT_FLOWS,
            tenant=tenant,
            professionals=professionals,
            supplied=(hb.FIELD_ACTION,),
            accepted=(hb.FIELD_ACTION,),
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
            _log_no_landing(
                reply,
                source_tool=hb.SOURCE_MANAGE_EXISTING_APPOINTMENT,
                reason=hb.FALLBACK_NO_PATIENT,
                tenant=tenant,
                professionals=professionals,
                supplied=(hb.FIELD_ACTION,),
                accepted=(hb.FIELD_ACTION,),
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
    await _land_handback(
        reply,
        result,
        patient_wa,
        source_tool=hb.SOURCE_MANAGE_EXISTING_APPOINTMENT,
        tenant=tenant,
        professionals=professionals,
        redis=redis,
        waba_token=waba_token,
        supplied=(hb.FIELD_ACTION,),
        accepted=(hb.FIELD_ACTION,),
        # A patient with nothing to manage lands on the menu with an explanation: the
        # hand-back worked, just not where the agent meant it to.
        fallback=None if appointments else hb.FALLBACK_NO_APPOINTMENTS,
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

    The agent's OPTIONAL offer (ai/tools.py::start_guided_booking). Everything is re-read
    FRESH (`_load_draft_context`), and TASK-030 P2 adds three things:

      * the service is RE-canonicalized against the fresh catalog - the tool proved it
        against its own turn's catalog, which may be minutes old; a service that no
        longer resolves goes to the resolver, which drops it and shows the service list
        (it used to ride on into a booking of a service the clinic no longer has);
      * pra-quem unknown -> the resolver asks it first (the safety fix);
      * the clinic's draft v2 switch on -> the resolver lands it.

    A multi-professional tenant is still turned away to the menu, decided on the FRESH
    roster: the day picker this opens would read the clinic-level agenda, which is no
    doctor's real availability.
    """
    appointment_type = reply_text[len(START_GUIDED_BOOKING_SENTINEL_PREFIX) :].strip() or None
    # The tool proved the service against the catalog of ITS turn; None means the clinic
    # had no catalog and nothing was supplied.
    supplied = (hb.FIELD_SERVICE,) if appointment_type is not None else ()
    if tenant is None or not flows_enabled(tenant):
        # The tool is only ever exposed to flow-enabled tenants, so this is a
        # defensive count-only warning, same style as _handle_manage_appointment.
        logger.warning(
            "worker_start_guided_booking_without_flows",
            conversation_id=str(reply.conversation_id),
        )
        _log_no_landing(
            reply,
            source_tool=hb.SOURCE_START_GUIDED_BOOKING,
            reason=hb.FALLBACK_NO_TENANT if tenant is None else hb.FALLBACK_WITHOUT_FLOWS,
            tenant=tenant,
            professionals=professionals,
            supplied=supplied,
            accepted=supplied,
        )
        return
    ctx = await _load_draft_context(reply, tenant)
    if ctx is None:
        logger.warning(
            "worker_start_guided_booking_no_conversation",
            conversation_id=str(reply.conversation_id),
        )
        _log_no_landing(
            reply,
            source_tool=hb.SOURCE_START_GUIDED_BOOKING,
            reason=hb.FALLBACK_NO_PATIENT,
            tenant=tenant,
            professionals=professionals,
            supplied=supplied,
            accepted=supplied,
        )
        return
    tenant = ctx.tenant
    if ctx.topology == BOOKING_TOPOLOGY_MULTI:
        logger.warning(
            "worker_start_guided_booking_multi_professional",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        await _fallback_to_menu(
            reply,
            source_tool=hb.SOURCE_START_GUIDED_BOOKING,
            reason=hb.FALLBACK_MULTI_PROFESSIONAL,
            tenant=tenant,
            professionals=professionals,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
            supplied=supplied,
            accepted=supplied,
            topology=ctx.topology,
        )
        return
    canonical = (
        canonical_service_name(ctx.tenant_snapshot.appointment_types, appointment_type)
        if appointment_type is not None
        else None
    )
    draft = BookingDraft(service=appointment_type)
    if (
        ai_draft_v2_enabled(tenant)
        or _pra_quem_unknown(ctx.conversation, draft)
        or (appointment_type is not None and canonical is None)
    ):
        await _apply_draft_resolution(
            reply,
            tenant,
            draft,
            ctx,
            source_tool=hb.SOURCE_START_GUIDED_BOOKING,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    selected_id = ctx.conversation.flow_selected_professional_id
    # WHOSE agenda the day picker reads: the selection when it still resolves, the tenant
    # agenda when none was ever picked (on a sole-professional clinic `load_tenant_config`
    # has already resolved THAT professional's credentials into it), None otherwise - so
    # the picker answers `calendar_unavailable` instead of guessing.
    async with async_session_factory() as session:
        booking_calendar = await _appointment_calendar(
            session,
            tenant,
            _appointment_calendar_target({"professional_id": selected_id}, ctx.professional_rows),
        )
    result = await enter_guided_booking(
        ctx.tenant_snapshot,
        booking_calendar,
        canonical,
        conversation_id=reply.conversation_id,
        professional_id=selected_id,
        insurance=ctx.conversation.flow_selected_insurance,
        professionals=ctx.professional_rows,
        attendee_name=ctx.conversation.flow_attendee_name,
    )
    logger.info(
        "conversation_guided_booking_entered",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        # Enums and flags only: whether a service came through and which step the flow
        # resumed at. Never the service name, never the convênio.
        has_type=canonical is not None,
        flow_step=result.flow_step,
    )
    await _land_handback(
        reply,
        result,
        patient_wa,
        source_tool=hb.SOURCE_START_GUIDED_BOOKING,
        tenant=tenant,
        professionals=ctx.professional_rows,
        redis=redis,
        waba_token=waba_token,
        supplied=supplied,
        accepted=supplied,
        topology=ctx.topology,
    )

def _pra_quem_unknown(conversation: SimpleNamespace, draft: BookingDraft) -> bool:
    """Neither the draft nor the conversation says who the booking is for.

    `flow_attendee_name is None` means "never asked"; ATTENDEE_SELF ("") is an answer
    (services/attendee.py). A hand-back must never read "never asked" as "for the
    patient" - that was the only barrier between "marcar pra minha mãe" and a booking in
    the patient's own name (spec §3, §4.11: the safety fix, outside the switch).
    """
    return draft.attendee is None and conversation.flow_attendee_name is None


async def _apply_draft_resolution(
    reply: _ReplyContext,
    tenant: Tenant,
    draft: BookingDraft,
    ctx: DraftContext,
    *,
    source_tool: str,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """The resolver decides; ONE hand-back event is logged; `_apply_flow_result` persists.

    The pre-existing per-tool events keep being emitted (P1's constraint), so dashboards
    built on them see resolver landings too.
    """
    resolution = await _resolve_draft(reply, tenant, draft, ctx)
    result = resolution.result
    if source_tool == hb.SOURCE_SET_BOOKING_DRAFT:
        logger.info(
            "conversation_booking_draft_entered",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
            multi=ctx.topology == BOOKING_TOPOLOGY_MULTI,
            has_type=draft.service is not None,
            flow_step=result.flow_step,
        )
    elif source_tool == hb.SOURCE_START_GUIDED_BOOKING:
        logger.info(
            "conversation_guided_booking_entered",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
            has_type=draft.service is not None,
            flow_step=result.flow_step,
        )
    await _land_handback(
        reply,
        result,
        patient_wa,
        source_tool=source_tool,
        tenant=tenant,
        professionals=ctx.professional_rows,
        redis=redis,
        waba_token=waba_token,
        supplied=draft.supplied_fields(),
        accepted=resolution.accepted,
        dropped=resolution.dropped,
        fallback=resolution.fallback,
        topology=ctx.topology,
    )


async def _legacy_booking_draft(
    reply: _ReplyContext,
    tenant: Tenant,
    draft: BookingDraft,
    ctx: DraftContext,
    *,
    patient_wa: str | None,
    professionals: list | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """The v1 landing (switch OFF, pra-quem already answered), with the TASK-030 fixes.

    The branches are the pre-TASK-030 ones, read from `ctx` (fresh) instead of a session
    of their own, with two corrections (spec §7, P2): a doctor whose config is incomplete
    now reaches the clinic alert (the menu fallback used to swallow it), and a STORED
    convênio is kept verbatim - a typed "Outro convênio" is the patient's answer, and
    re-validating it against the plan catalog used to erase it.
    """
    conversation = ctx.conversation
    appointment_type = draft.service
    professional_id = draft.professional_id
    insurance_text = draft.insurance
    supplied = draft.supplied_fields()
    selected_id = professional_id or conversation.flow_selected_professional_id
    attendee = conversation.flow_attendee_name
    stored_type = conversation.flow_selected_type
    professional_rows = ctx.professional_rows
    service_catalog = ctx.service_catalog
    tenant_snapshot = ctx.tenant_snapshot
    topology = ctx.topology
    is_multi = topology == BOOKING_TOPOLOGY_MULTI
    selection_only = appointment_type is None and professional_id is None
    # A convênio the agent typed only counts when it names a real plan; anything else is
    # dropped so the flow ASKS instead of storing a made-up label. One already stored is
    # the patient's own answer and is kept as it is.
    insurance = (
        match_insurance_plan(tenant_snapshot, insurance_text)
        if insurance_text is not None
        else conversation.flow_selected_insurance
    )
    professional = next((p for p in professional_rows if p.id == selected_id), None)
    fresh_services = (
        professional_appointment_types(professional, tenant_snapshot, service_catalog)
        if professional is not None
        else tenant_snapshot.appointment_types
    )
    canonical_type = canonical_service_name(fresh_services, appointment_type)
    accepted, dropped = _draft_verdicts(
        appointment_type=appointment_type,
        canonical_type=canonical_type,
        professional_id=professional_id,
        professional=professional,
        insurance_text=insurance_text,
        insurance=insurance,
    )

    async def _menu(reason: str) -> None:
        await _fallback_to_menu(
            reply,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            reason=reason,
            tenant=tenant,
            professionals=professional_rows,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
            supplied=supplied,
            accepted=accepted,
            dropped=dropped,
            topology=topology,
        )

    async def _land(result: FlowRouterResult) -> None:
        await _land_handback(
            reply,
            result,
            patient_wa,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            tenant=tenant,
            professionals=professional_rows,
            redis=redis,
            waba_token=waba_token,
            supplied=supplied,
            accepted=accepted,
            dropped=dropped,
            topology=topology,
        )

    if selection_only:
        # An unspecified service means administrative choices, never an inferred procedure
        # or a jump to availability. Stored selections are revalidated against the FRESH
        # roster/catalog. Pra-quem is answered on this path (an unknown one goes to the
        # resolver), so the next list is the booking's own, never the question again.
        stored_type = canonical_service_name(fresh_services, stored_type)
        if professional is not None:
            result = _enter_professional_services(professional, tenant_snapshot)
        else:
            result = _start_booking(tenant_snapshot, professional_rows, insurance=insurance)
        if result.action == "professional_config_incomplete":
            await _land(result)
            return
        if result.flow_state != FlowState.SERVICE_CATALOG:
            await _menu(hb.FALLBACK_NO_BOOKABLE_CATALOG)
            return
        result.flow_selected_insurance = insurance
        result.flow_attendee_name = attendee
        if result.flow_step not in ATTENDEE_STEPS:
            result.flow_selected_type = stored_type
        await _land(result)
        return
    if (
        (appointment_type is not None and canonical_type is None)
        or (appointment_type is None and (not is_multi or professional_id is None))
        or (selected_id is not None and professional is None)
    ):
        logger.warning(
            "worker_booking_draft_invalid_selection", conversation_id=str(reply.conversation_id)
        )
        await _menu(hb.FALLBACK_INVALID_SELECTION)
        return
    async with async_session_factory() as session:
        booking_calendar = await _appointment_calendar(
            session,
            tenant,
            _appointment_calendar_target({"professional_id": selected_id}, professional_rows),
        )
    appointment_type = canonical_type
    if is_multi:
        if professional is None:
            # A service without a doctor on a multi-doctor clinic: no one to book with yet.
            await _menu(hb.FALLBACK_MISSING_PROFESSIONAL)
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
    await _land(result)


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
    """LLM hand-back: the AI's booking draft (v1 or v2 payload, services/booking_draft.py).

    TASK-030 P2. Everything is re-read FRESH (`_load_draft_context`); `flow_snapshot`
    stays in the signature for the caller and is no longer read. Who decides the landing:

      * the resolver (`resolve_booking_draft`) when the clinic's draft v2 switch is on -
        AND, for every clinic, whenever pra-quem is still unknown: the workflow asks it
        first and parks the rest in `flow_draft` (spec §4.11, the safety fix);
      * otherwise the v1 landing (`_legacy_booking_draft`).

    Every exit records ONE `conversation_handback_entered` (workers/shared/handback_log.py):
    the landing step, or a `fallback` code on every bounce and silent return. Field NAMES
    and reason codes only - never the service, convênio, doctor, day or time.
    """
    try:
        draft = BookingDraft.from_payload(reply_text[len(BOOKING_DRAFT_SENTINEL_PREFIX) :])
    except ValueError:
        logger.warning(
            "worker_booking_draft_bad_sentinel", conversation_id=str(reply.conversation_id)
        )
        await _fallback_to_menu(
            reply,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            reason=hb.FALLBACK_BAD_SENTINEL,
            tenant=tenant,
            professionals=professionals,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    supplied = draft.supplied_fields()
    if tenant is None or not flows_enabled(tenant):
        logger.warning(
            "worker_booking_draft_without_flows", conversation_id=str(reply.conversation_id)
        )
        _log_no_landing(
            reply,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            reason=hb.FALLBACK_NO_TENANT if tenant is None else hb.FALLBACK_WITHOUT_FLOWS,
            tenant=tenant,
            professionals=professionals,
            supplied=supplied,
        )
        return
    ctx = await _load_draft_context(reply, tenant)
    if ctx is None:
        logger.warning(
            "worker_booking_draft_no_conversation", conversation_id=str(reply.conversation_id)
        )
        _log_no_landing(
            reply,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            reason=hb.FALLBACK_NO_PATIENT,
            tenant=tenant,
            professionals=professionals,
            supplied=supplied,
        )
        return
    # The loader re-read the tenant too; do not restore a stale switch/config.
    tenant = ctx.tenant
    if ai_draft_v2_enabled(tenant) or _pra_quem_unknown(ctx.conversation, draft):
        await _apply_draft_resolution(
            reply,
            tenant,
            draft,
            ctx,
            source_tool=hb.SOURCE_SET_BOOKING_DRAFT,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    await _legacy_booking_draft(
        reply,
        tenant,
        draft,
        ctx,
        patient_wa=patient_wa,
        professionals=professionals,
        redis=redis,
        waba_token=waba_token,
    )
