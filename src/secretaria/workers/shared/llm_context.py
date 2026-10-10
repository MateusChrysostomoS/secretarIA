"""llm_context - split out of workers/tasks.py (TASK-023)."""

from types import SimpleNamespace
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from secretaria.ai.availability_tool import get_availability
from secretaria.ai.staging_tools import cancel_event_v2, create_event_v2
from secretaria.ai.tools import (
    manage_existing_appointment,
    manage_existing_appointment_v2,
    offer_human_handoff,
    request_human_handoff,
    set_booking_draft,
    set_booking_draft_v2,
    start_guided_booking,
)
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import (
    strip_decoration,
    truncate_button_label,
)
from secretaria.models import (
    FlowState,
    Professional,
    Tenant,
)
from secretaria.services.appointment_calendar_origin import (
    CalendarOriginUnresolved,
    calendar_professional_id,
)
from secretaria.services.booking_scope import (
    BOOKING_TOPOLOGY_MULTI,
)
from secretaria.services.calendar import (
    CalendarService,
)
from secretaria.services.flow_router import (
    LABEL_RESCHEDULE,
    ai_draft_v2_enabled,
    flows_enabled,
)
from secretaria.services.manage_request import appointment_ref
from secretaria.services.patient_context import (
    PatientOpeningState,
)
from secretaria.services.tenant_config import (
    RuntimeAppointmentType,
    RuntimeServiceGuide,
    load_tenant_config,
    resolve_professional_calendar,
)
from secretaria.workers.shared.greeting import (
    _appointment_doctor_name,
    _format_appointment_when,
)

logger = get_logger(__name__)


def _llm_activation_reason(
    flow_state: FlowState | None,
    delegated_to_llm: bool,
) -> str:
    """Why THIS turn is about to spend a full LangGraph agent call.

    The deterministic router is the product; the model is the last resort. So
    every activation is worth naming, and the names are chosen to be acted on:

    - `sticky_llm_mode`: the conversation is already parked in FlowState.LLM
      (the patient tapped "Outro" earlier and nothing handed them back). Since
      2026-10-06 the "Outro" tap itself is answered by the flow with a fixed
      question, so the patient's FIRST answer after it is also counted here. A
      high count beyond that means the hand-backs (`show_main_menu`,
      select-professional, manage-appointment) are not firing often enough.
    - `router_delegated`: the router saw this turn and gave up on it. This is
      the number to drive to zero - each one is a flow node that needs a
      deterministic answer. Pair with `flow_state`/`flow_step` to find it.
    - `no_deterministic_flow`: the router never ran at all for this turn (no
      flow snapshot could be built - e.g. tenant/config resolution came back
      empty). Not a flow gap; a data/config problem.

    Pure function over already-resolved turn facts, like the gate below.
    """
    if flow_state is FlowState.LLM:
        return "sticky_llm_mode"
    if delegated_to_llm:
        return "router_delegated"
    return "no_deterministic_flow"

def _should_inject_post_consult_knowledge(
    knowledge: str | None,
    opening_state: PatientOpeningState | None,
    flow_state: FlowState | None,
    delegated_to_llm: bool,
) -> bool:
    """Whether THIS turn's system prompt should carry post_consult_knowledge.

    Pure gate over already-resolved turn facts (see the orchestration in
    `_send_bot_reply`) - does no I/O itself. `knowledge` blank/None always
    wins (nothing to inject regardless of state). Otherwise the turn
    qualifies when the patient just had a consult, the conversation is
    already in full-LLM ("Outro") mode, or the deterministic router just
    delegated this very turn to the LLM.
    """
    if not (knowledge or "").strip():
        return False
    return (
        opening_state is PatientOpeningState.JUST_HAD_CONSULT
        or flow_state is FlowState.LLM
        or delegated_to_llm
    )

def _should_inject_appointment_context(
    future_appointments: list[dict] | None,
    flow_state: FlowState | None,
    delegated_to_llm: bool,
) -> bool:
    """Whether THIS turn's system prompt should carry the appointment context block.

    Pure gate over already-resolved turn facts (see the orchestration in
    `_send_bot_reply`) - does no I/O itself. Mirrors
    `_should_inject_post_consult_knowledge`'s shape: an empty/None
    `future_appointments` always loses (nothing to render regardless of state
    - see `_appointment_context_text`). Otherwise the turn qualifies when the
    conversation is already in full-LLM ("Outro") mode, or the deterministic
    router just delegated THIS turn to the LLM.
    """
    if not future_appointments:
        return False
    return flow_state is FlowState.LLM or delegated_to_llm

def _appointment_context_text(
    future_appointments: list[dict],
    tz_name: str | None,
    professional_names: dict[str, str],
    appointment_types: list[RuntimeAppointmentType],
    *,
    with_refs: bool = False,
    service_guides: list[RuntimeServiceGuide] | None = None,
) -> str | None:
    """Render the per-turn "consultas marcadas" block for the LLM prompt.

    Pure formatting over already-resolved data (see
    `_should_inject_appointment_context` for the gate and `_send_bot_reply`
    for the orchestration) - no DB access, and no appointment content is ever
    logged from here (only counts are logged elsewhere, e.g.
    `resolve_patient_opening_state`). Returns None (never "") when
    `future_appointments` is empty, so the caller can tell "nothing to
    inject" apart from "injected an empty string" the same way
    `ai/prompts.py::_format_post_consult_knowledge` etc. do with a falsy check.

    `future_appointments` must be nearest-first (see
    `patient_context.load_upcoming_appointments`). The NEAREST one (index 0)
    gets the fuller "Próxima consulta" line (service, doctor when resolvable,
    price when its service matches a catalog entry) plus an "Orientações"
    line when that matched entry has `requirements`. Every OTHER appointment
    gets one brief "when — service[ — doctor]" line, mirroring the greeting's
    own brief-list rendering (`_adapt_greeting_has_upcoming`).

    `professional_names` is id -> display name, expected to be built from the
    ACTIVE-professionals roster (`flow_professionals` in `_send_bot_reply`):
    an appointment whose owner is no longer active simply renders with no
    doctor name (`_appointment_doctor_name` degrades to None on a miss).
    `appointment_types` is `TenantRuntimeConfig.appointment_types`, matched
    against the nearest appointment's stored service name by casefold/strip.

    `with_refs` (TASK-030 P3, clinics with the AI draft v2 switch): every appointment line
    ends with "(ref AAAA-MM-DD HH:MM)" - the reference `manage_existing_appointment` v2
    takes to name WHICH appointment (services/manage_request.py::appointment_ref).

    `service_guides` (TenantRuntimeConfig.service_guides): the clinic-wide orientations from the
    canonical catalog. `appointment_types` misses a service that only the professionals' own lists
    offer (2+ active professionals), so the "Orientações" line falls back to the matching guide.
    """
    if not future_appointments:
        return None
    tz = ZoneInfo(tz_name or "America/Sao_Paulo")

    def ref(appointment: dict) -> str:
        return f" (ref {appointment_ref(appointment['start_at'], tz)})" if with_refs else ""

    nearest = future_appointments[0]
    when = _format_appointment_when(nearest["start_at"], tz_name)
    service_name = nearest.get("appointment_type") or "Consulta"
    doctor = _appointment_doctor_name(nearest, professional_names)
    matched = next(
        (
            t
            for t in appointment_types
            if t.name.strip().casefold() == service_name.strip().casefold()
        ),
        None,
    )

    nearest_line = f"Próxima consulta: {when} — {service_name}"
    if doctor:
        nearest_line += f" — {doctor}"
    if matched and matched.price:
        nearest_line += f" — {matched.price}"
    nearest_line += ref(nearest)

    lines = [nearest_line]
    requirements = matched.requirements if matched else []
    if not requirements:
        guide = next(
            (
                g
                for g in service_guides or []
                if g.name.strip().casefold() == service_name.strip().casefold()
            ),
            None,
        )
        requirements = list(guide.requirements or []) if guide else []
    if requirements:
        lines.append("Orientações: " + "; ".join(requirements))

    for appt in future_appointments[1:]:
        appt_when = _format_appointment_when(appt["start_at"], tz_name)
        line = f"{appt_when} — {appt.get('appointment_type') or 'Consulta'}"
        appt_doctor = _appointment_doctor_name(appt, professional_names)
        if appt_doctor:
            line += f" — {appt_doctor}"
        lines.append(line + ref(appt))

    return "\n".join(lines)

def _label_match_body(body: str | None, label: str) -> bool:
    """True when `body` equals `label` or its 20-char button truncation.

    Normalises through `strip_decoration` on both sides, exactly as
    flow_router's and booking_scope's `_norm` do: the greeting card this matches
    renders LABEL_CANCEL_APPT as "❌ Cancelar", and without the strip a patient
    who typed "cancelar" - or tapped a greeting sent before FEAT 44 - would stop
    reaching the manage flow.
    """
    target = strip_decoration(body).casefold()
    return bool(target) and (
        target == strip_decoration(label).casefold()
        or target == strip_decoration(truncate_button_label(label)).casefold()
    )

def _ai_toolset_v2(tenant: Tenant | None) -> bool:
    """Whether THIS tenant's agent runs on the v2 toolset (TASK-030 P4, spec §4.6).

    The per-clinic switch (`flow_router.ai_draft_v2_enabled`) AND the flow existing at all:
    on v2 the agent books and cancels only by handing back to the flow (its create_event /
    cancel_event are blind staging tools), so a tenant without those hand-backs must never
    swap the legacy `create_event` for the blind one. Read once per turn, here, and handed
    to `run_agent(toolset_v2=...)`.
    """
    return tenant is not None and flows_enabled(tenant) and ai_draft_v2_enabled(tenant)


def _flow_handback_tools(tenant: Tenant | None, topology: str, plugin_tools: list) -> list:
    """This turn's `extra_tools`: the plugin set + the flow hand-back tools.

    `set_booking_draft` is offered on EVERY topology: it resolves the doctor itself
    on a multi-doctor clinic.

    All hand-backs re-enter the deterministic flow through a sentinel, so
    neither means anything to a tenant that has no such flow — hence the
    `flows_enabled` gate they have always shared (unconditional since the flows
    became the product, kept because it is the file's pattern and the switch
    could come back).

    `start_guided_booking` carries one gate the other does not: it is withheld
    from a MULTI-professional tenant. The day picker it opens would read
    availability off the clinic-level agenda, not the chosen doctor's, so on
    those tenants the way back into the flow is `select_professional_and_continue`
    (which re-enters at a doctor whose calendar IS resolved) or the menu. Same
    two-lock shape the calendar tools use: withheld from the tool set here,
    and refused again inside the tool by `_blocked_tenant_level` if it ever
    arrives anyway.

    On the v2 toolset (`_ai_toolset_v2`) the AI also gets `get_availability`, its only
    agenda read (free windows, never events), and the blind `create_event` /
    `cancel_event` (ai/staging_tools.py), which only stage the patient's confirmation card;
    the tools it loses are withheld in ai/graph.py::effective_tools, not here.
    """
    if tenant is None or not flows_enabled(tenant):
        return list(plugin_tools)
    # TASK-030: same model-facing names, two implementations each; the clinic's switch
    # picks both (P2: the booking draft; P3: the manage request).
    v2 = ai_draft_v2_enabled(tenant)
    draft_tool = set_booking_draft_v2 if v2 else set_booking_draft
    manage_tool = manage_existing_appointment_v2 if v2 else manage_existing_appointment
    handbacks = [manage_tool, draft_tool, request_human_handoff, offer_human_handoff]
    if topology != BOOKING_TOPOLOGY_MULTI:
        handbacks.append(start_guided_booking)
    v2_tools = (
        [get_availability, create_event_v2, cancel_event_v2] if _ai_toolset_v2(tenant) else []
    )
    return [*plugin_tools, *handbacks, *v2_tools]

def _flow_turn_calendar(
    conv_snapshot: SimpleNamespace,
    tenant_config,
    flow_calendar: CalendarService | None,
) -> CalendarService | None:
    """The calendar the flow router should use for THIS turn.

    With a professional selected, ONLY that professional's resolved calendar
    counts: when its resolution failed (None), the router degrades to the LLM
    (its calendar-needing steps delegate on None) instead of silently listing
    or booking on the tenant-level agenda. Without a selection, the tenant
    calendar is used exactly as before.
    """
    if getattr(conv_snapshot, "flow_selected_professional_id", None) is not None:
        return flow_calendar
    return CalendarService.from_tenant_config(tenant_config) if tenant_config else None

def _appointment_calendar_target(
    appt: dict | None, professional_rows: list[Professional] | None
) -> Professional | Literal["tenant"] | None:
    """Whose calendar owns ONE appointment dict (pure, no I/O).

    The owning `Professional` row when the appointment names one on the active
    roster; the literal "tenant" when it names none (booked at the tenant
    level); None when there is no appointment, or its `professional_id` no
    longer resolves - the caller must then NOT guess an agenda.
    """
    if appt is None:
        return None
    try:
        professional_id = calendar_professional_id(appt)
    except CalendarOriginUnresolved:
        return None
    if not professional_id:
        return "tenant"
    return next((p for p in (professional_rows or []) if str(p.id) == str(professional_id)), None)

async def _appointment_calendar(
    session, tenant: Tenant, target: "Professional | Literal['tenant'] | None"
) -> CalendarService | None:
    """Build the CalendarService for an owner `_appointment_calendar_target` found.

    Used by the two entries that open the reschedule day picker WITHOUT a
    conversation snapshot to hang it off (a reminder button tap, and the LLM's
    manage-appointment hand-back). None in -> None out, and a build failure is
    logged count-only and answered with None too: the day picker then replies
    `calendar_unavailable`, which is the honest answer, rather than listing
    days off whichever agenda happened to be at hand.
    """
    if target is None:
        return None
    try:
        tenant_config = await load_tenant_config(session, tenant)
        if isinstance(target, Professional):
            return await resolve_professional_calendar(
                session, tenant, target, tenant_config=tenant_config
            )
        return CalendarService.from_tenant_config(tenant_config) if tenant_config else None
    except Exception as exc:
        logger.warning("worker_appointment_calendar_failed", error=str(exc))
        return None

def _manage_owner_calendar_target(
    flow_state: FlowState | None,
    flow_managing_appointment_id: UUID | None,
    upcoming_appointments: list[dict] | None,
    professional_rows: list[Professional] | None,
    inbound_body: str | None = None,
) -> Professional | Literal["tenant"] | None:
    """Whose calendar this turn's manage action acts on (pure, no I/O).

    Two turns need it, and both are resolved BEFORE `route()` runs because the
    router does no DB I/O of its own:

      - a turn already INSIDE the manage flow with a target set: resolve
        `flow_managing_appointment_id` against the `upcoming_appointments`
        dicts already loaded for this turn (see `wants_upcoming_appointments`
        above - it guarantees they are loaded whenever this matters);
      - the ENTRY turn of a direct "Remarcar" tap, which is still IDLE/MENU
        here yet opens the day picker inside this very turn
        (`enter_manage_action`'s single-appointment shortcut). Exactly one
        upcoming appointment is unambiguous; with 2+ the router shows a pick
        list and needs no calendar at all, so None is correct there.

    Returns None for every other turn - the caller falls back to its existing
    calendar resolution (`_flow_turn_calendar`).

    Deliberately ignores `flow_selected_professional_id` (the SERVICE_CATALOG
    booking selection) - a stale doctor pick from an earlier booking flow must
    never decide which agenda a cancel/reschedule acts on.
    """
    if flow_state == FlowState.MANAGE_BOOKING and flow_managing_appointment_id is not None:
        target = str(flow_managing_appointment_id)
        appt = next((a for a in (upcoming_appointments or []) if str(a.get("id")) == target), None)
        return _appointment_calendar_target(appt, professional_rows)
    if _label_match_body(inbound_body, LABEL_RESCHEDULE) and len(upcoming_appointments or []) == 1:
        return _appointment_calendar_target((upcoming_appointments or [])[0], professional_rows)
    return None
