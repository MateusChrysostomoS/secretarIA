"""TASK-041: natural-language edits cannot escape their guided draft."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from secretaria.ai import graph
from secretaria.models import FlowState
from secretaria.services import (
    appointment_edit as ae,
    appointment_edit_flow as ef,
    flow_router as fr,
)
from secretaria.workers.shared.state_expiry import _expire_stale_edit_state
from tests._edit_flow_support import (
    DOCTOR_A,
    DOCTOR_B,
    EditCalendar,
    appointment,
    context,
    conversation,
    draft_for,
    original_appointment,
    professionals,
    tenant,
)

ENTRIES = [
    ae.LABEL_EDIT_DATE,
    ae.LABEL_EDIT_TIME,
    ae.LABEL_EDIT_SERVICE,
    ae.LABEL_EDIT_DOCTOR,
    ae.LABEL_EDIT_MORE,
]


@pytest.fixture(autouse=True)
def holds(monkeypatch):
    monkeypatch.setattr(fr, "_hold_windows", AsyncMock(return_value=[]))


async def turn(conv, text, ctx):
    return await fr.route(
        conv,
        tenant(),
        None,
        text,
        upcoming_appointments=[original_appointment(ae.EditDraft.from_json(conv.flow_edit_draft))],
        professionals=professionals(),
        edit_context=ctx,
    )


def carry(conv, result):
    for name in vars(result):
        if name.startswith("flow_"):
            setattr(conv, name, getattr(result, name))


async def ai(conv, payload, ctx):
    handler = getattr(ef, "apply_ai_edit", None)
    assert handler is not None, "AI proposals need a landing inside the active edit"
    return await handler(
        conv,
        tenant(),
        payload,
        [original_appointment(ae.EditDraft.from_json(conv.flow_edit_draft))],
        professionals(),
        ctx,
    )


@pytest.mark.parametrize("entry", ENTRIES)
async def test_i1_opening_picker_never_changes_original_or_current(entry):
    draft = draft_for()
    conv = conversation(draft, fr.STEP_EDIT_MENU)
    result = await turn(conv, entry, context())
    assert result.flow_state == FlowState.EDIT_BOOKING
    assert result.flow_edit_draft["original"] == draft.original
    assert result.flow_edit_draft["current"] == draft.current


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("interruption", ["recognized", "unknown", "switch", "unavailable"])
async def test_i2_every_picker_preserves_draft_across_ai_interruptions(entry, interruption):
    draft = draft_for()
    conv = conversation(draft, fr.STEP_EDIT_MENU)
    ctx = context()
    carry(conv, await turn(conv, entry, ctx))
    if interruption == "switch":
        result = await turn(conv, ae.LABEL_EDIT_SERVICE, ctx)
        assert result.flow_step == fr.STEP_EDIT_SERVICE
    else:
        payload = {"keep": ["time"]} if interruption == "recognized" else None
        result = await ai(conv, payload, ctx)
        assert result.action == "reply"
    assert result.flow_state == FlowState.EDIT_BOOKING
    assert result.flow_edit_draft["original"] == draft.original
    assert result.flow_edit_draft["current"] == draft.current


@pytest.mark.parametrize("v2", [False, True])
async def test_i3_edit_agent_has_no_booking_cancel_or_navigation_tools(v2):
    from secretaria.workers.shared.llm_context import _flow_handback_tools

    extras = _flow_handback_tools(tenant(initial_flows={"ai_draft_v2": v2}), "unknown", [])
    active = graph.effective_tools("unknown", extras, toolset_v2=v2, editing=True)
    names = {t.name for t in active}
    assert "propose_appointment_edit" in names
    assert not names.intersection(
        {
            "manage_existing_appointment",
            "set_booking_draft",
            "start_guided_booking",
            "create_event",
            "cancel_event",
            "show_main_menu",
            "iniciar_pre_consulta",
        }
    )


async def test_i4_mid_date_change_intention_then_doctor_keeps_slot_and_only_doctor_diff():
    appt = appointment(
        professional_id=str(DOCTOR_B), end_at=datetime(2030, 10, 15, 19, 0, tzinfo=UTC)
    )
    docs = professionals()
    docs[1].appointment_types[0]["duration_min"] = 40
    draft = draft_for(appt)
    conv = conversation(draft, fr.STEP_EDIT_MENU)
    old, new = EditCalendar(), EditCalendar()
    ctx = context(calendars={str(DOCTOR_A): new, str(DOCTOR_B): old})
    carry(conv, await ef.edit_step(conv, tenant(), ae.LABEL_EDIT_DATE, [appt], docs, ctx))
    handler = getattr(ef, "apply_ai_edit", None)
    assert handler is not None, "no editing handback exists"
    for payload, step in [
        ({"open_field": "service"}, fr.STEP_EDIT_SERVICE),
        ({"open_field": "doctor"}, fr.STEP_EDIT_DOCTOR),
        ({"professional": "Dr. Diogo Raposo", "keep": ["time"]}, fr.STEP_EDIT_CONFIRM),
    ]:
        result = await handler(conv, tenant(), payload, [appt], docs, ctx)
        assert result.flow_step == step
        assert result.flow_state == FlowState.EDIT_BOOKING
        carry(conv, result)
    assert ae.EditDraft.from_json(conv.flow_edit_draft).changed() == ["médico"]
    assert result.bubbles[0].body.endswith("O que mudou: médico.")
    assert new.created == [] and old.cancelled == []
    confirmed = await ef.edit_step(conv, tenant(), fr.LABEL_CONFIRM, [appt], docs, ctx)
    assert len(new.created) == 1
    assert confirmed.appointment_edit["doctor_changed"]
    assert not confirmed.appointment_edit["time_changed"]


async def test_i5_busy_new_doctor_keeps_original_time_and_uses_new_calendar():
    draft = draft_for()
    conv = conversation(draft, fr.STEP_EDIT_DAY)
    old, new = EditCalendar(), EditCalendar(free=False)
    ctx = context(calendars={str(DOCTOR_A): old, str(DOCTOR_B): new})
    result = await ai(conv, {"professional": "Dra. Ana Lima"}, ctx)
    assert result.flow_step in fr._EDIT_DAY_STEPS
    assert result.flow_edit_draft["current"]["start_at"] == draft.current["start_at"]
    assert result.flow_edit_draft["current"]["professional_id"] == str(DOCTOR_B)
    assert new.checked and not old.checked
    assert result.bubbles[0].body == ae.EDIT_RESLOT_NOTICE


@pytest.mark.parametrize(
    "payload,guard",
    [
        ({"service": "Retorno"}, "paid_deposit"),
        ({"professional": "Dra. Ana Lima"}, "paid_deposit"),
        ({"insurance": "Amil"}, "paid_deposit"),
        ({"day": "2030-10-16"}, "reschedule_blocked"),
        ({"time": "09:00"}, "reschedule_blocked"),
    ],
)
async def test_i6_ai_cannot_bypass_pix(payload, guard):
    draft = draft_for()
    result = await ai(conversation(draft, fr.STEP_EDIT_MENU), payload, context(**{guard: True}))
    assert result.flow_state == FlowState.EDIT_BOOKING
    assert result.flow_edit_draft["current"] == draft.current
    assert result.flow_edit_draft["original"] == draft.original


async def test_i7_edit_still_expires_without_optional_clinic_configuration():
    conv = conversation(draft_for(), fr.STEP_EDIT_DAY)
    assert _expire_stale_edit_state(conv, tenant(), datetime.now(UTC) - timedelta(days=2))
    assert conv.flow_state == FlowState.IDLE and conv.flow_edit_draft is None


async def test_i8_proposal_cannot_apply_to_another_appointment_draft():
    draft = draft_for()
    conv = conversation(draft, fr.STEP_EDIT_MENU)
    conv.flow_managing_appointment_id = __import__("uuid").uuid4()
    result = await ai(conv, {"professional": "Dra. Ana Lima"}, context())
    assert result.appointment_edit is None
    assert result.flow_state == FlowState.EDIT_BOOKING
    assert result.flow_edit_draft["current"] == draft.current


@pytest.mark.parametrize(
    "payload",
    [
        {"service": "Unknown service"},
        {"professional": "Unknown doctor"},
        {"professional": "Dra. Ana Lima", "service": "Retorno"},
        {"insurance": "Unknown plan"},
        {"day": "2030-99-99"},
        {"time": "25:00"},
        {"attendee": "unregistered raw name"},
        {"keep": "time"},
        {"finish": "yes"},
        {"unrecognized": "value"},
    ],
)
async def test_invalid_proposals_preserve_current_stage_and_both_snapshots(payload):
    draft = draft_for().with_changes(insurance="Amil")
    conv = conversation(draft, fr.STEP_EDIT_SERVICE)
    result = await ai(conv, payload, context())
    assert result.action == "reply" and result.flow_step == fr.STEP_EDIT_SERVICE
    assert result.flow_edit_draft == draft.to_json()


@pytest.mark.parametrize("field", ["day", "time", "insurance"])
async def test_keep_means_current_value_not_original_value(field):
    draft = draft_for().with_changes(
        start_at="2030-10-16T09:00", end_at="2030-10-16T09:40", insurance="Amil"
    )
    result = await ai(conversation(draft, fr.STEP_EDIT_DAY), {"keep": [field]}, context())
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert result.flow_edit_draft["current"] == draft.current
    assert result.flow_edit_draft["original"] == draft.original


async def test_finish_or_abandon_with_pending_changes_requires_patient_final_confirmation():
    draft = draft_for().with_changes(insurance="Amil")
    for payload in ({"finish": True}, {"abandon": True}):
        result = await ai(conversation(draft, fr.STEP_EDIT_DAY), payload, context())
        assert result.flow_step == fr.STEP_EDIT_CONFIRM
        assert result.flow_state == FlowState.EDIT_BOOKING
        assert result.appointment_edit is None


async def test_attendee_other_requires_existing_name_authorization_not_ai_name_capture():
    result = await ai(
        conversation(draft_for(), fr.STEP_EDIT_MENU), {"attendee": "other"}, context()
    )
    assert result.flow_step == fr.STEP_EDIT_ATT_NAME
    assert result.flow_edit_draft["current"]["attendee_name"] is None


async def test_keep_cannot_be_overwritten_by_a_conflicting_ai_value():
    draft = draft_for()
    result = await ai(
        conversation(draft, fr.STEP_EDIT_DAY), {"keep": ["time"], "time": "09:00"}, context()
    )
    assert result.flow_edit_draft["current"] == draft.current


@pytest.mark.parametrize("v2", [False, True])
def test_edit_agent_cache_cannot_reuse_normal_booking_tools(monkeypatch, v2):
    from types import SimpleNamespace

    monkeypatch.setattr(
        graph,
        "get_settings",
        lambda: SimpleNamespace(
            OPENAI_API_KEY="test", OPENAI_SECRETARIA_MODEL="test", OPENAI_MAX_TOKENS=500
        ),
    )
    monkeypatch.setattr(graph, "ChatOpenAI", lambda **kw: object())
    monkeypatch.setattr(
        graph, "create_react_agent", lambda model, tools, **kw: tuple(t.name for t in tools)
    )
    monkeypatch.setattr(graph, "_AGENTS", {})
    normal = graph.build_agent(toolset_v2=v2)
    editing = graph.build_agent(toolset_v2=v2, editing=True)
    assert "show_main_menu" in normal and "show_main_menu" not in editing
    assert "propose_appointment_edit" in editing
    assert len(graph._AGENTS) == 2


async def test_proposal_tool_only_raises_a_draft_request_and_never_writes():
    from secretaria.ai.tools import AppointmentEditRequested, _editing_ctx, propose_appointment_edit

    assert "error" in await propose_appointment_edit.ainvoke({"service": "Consulta"})
    token = _editing_ctx.set(True)
    try:
        with pytest.raises(AppointmentEditRequested) as requested:
            await propose_appointment_edit.ainvoke({"professional": "Dr. Diogo", "keep": ["time"]})
        assert requested.value.proposal["professional"] == "Dr. Diogo"
        assert requested.value.proposal["keep"] == ["time"]
    finally:
        _editing_ctx.reset(token)


async def test_review_rejects_ambiguous_doctor_name_without_picking_first_calendar():
    draft = draft_for()
    docs = professionals()
    docs[0].name = docs[1].name = "Dr. Same Name"
    docs.reverse()
    conv = conversation(draft, fr.STEP_EDIT_DOCTOR)
    result = await ef.apply_ai_edit(
        conv,
        tenant(),
        {"professional": "Dr. Same Name"},
        [original_appointment(draft)],
        docs,
        context(),
    )
    assert result.flow_step == fr.STEP_EDIT_DOCTOR
    assert result.flow_edit_draft["current"] == draft.current


@pytest.mark.parametrize("proposal", [{"keep": ["time"]}, {"finish": True}, {"insurance": "Amil"}])
async def test_review_incompatible_current_doctor_service_never_looks_complete(proposal):
    appt = appointment(appointment_type="Retorno")
    draft = draft_for(appt).with_changes(professional_id=str(DOCTOR_B))
    conv = conversation(draft, fr.STEP_EDIT_SERVICE)
    result = await ai(conv, proposal, context())
    assert result.flow_step == fr.STEP_EDIT_SERVICE
    assert result.flow_edit_draft["current"]["service"] == "Retorno"
    assert result.flow_edit_draft["current"]["professional_id"] == str(DOCTOR_B)


@pytest.mark.parametrize("keep", [[{}], [[]], [None], [3]])
async def test_review_malformed_keep_members_redraw_instead_of_raising(keep):
    draft = draft_for()
    result = await ai(conversation(draft, fr.STEP_EDIT_DOCTOR), {"keep": keep}, context())
    assert result.flow_step == fr.STEP_EDIT_DOCTOR
    assert result.flow_edit_draft == draft.to_json()
