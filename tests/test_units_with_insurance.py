"""TASK-088 A2: switching from a unit without accepted plans."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.ai.formatter import ButtonBubble, SlotsBubble
from secretaria.core.whatsapp_limits import (
    MAX_BUTTON_LABEL_CHARS,
    MAX_LIST_ROW_DESCRIPTION_CHARS,
    MAX_LIST_ROW_TITLE_CHARS,
    MAX_LIST_ROWS,
)
from secretaria.models import Conversation, FlowState, ProfessionalUnit, Unit
from secretaria.schemas.webhook import inbound_routing_text
from secretaria.services import flow_router as fr, flow_units as fu
from secretaria.services.booking_draft import BookingDraft, draft_from_record, draft_record
from secretaria.workers.portal.inbound import offered_reply_ids
from secretaria.workers.shared import draft_resolution
from secretaria.workers.shared.bubbles import _bubble_interactive
from tests import test_t086_flow_matrix as matrix
from tests.test_booking_draft_resolver import DAY, NOW, _conv, _resolve, _sole
from tests.test_booking_draft_resume_questions import (
    DAY as CURRENT_DAY,
    _conversation,
    _onboard,
    _professional,
    _seed_tenant,
    _wa_turn,
    _WireClient,
    db as _db_fixture,
    wired as _wired_fixture,
)
from tests.test_booking_order_compat import PLAN_B, _refuse, _two_plans
from tests.test_booking_order_service_first import (
    AMIL,
    BRADESCO,
    ECO_ID,
    UNIMED,
    _doctor,
    _matrix,
    _tenant,
    _units,
    _Walk,
)
from tests.test_t086_ai_draft import CONSULTA, PLAN, _unit, _with_units

NO_PLAN_STEP = "awaiting_no_plan_at_unit"
OTHER_UNITS = "Unidades c/ convênio"
PARTICULAR = "Seguir particular"
db = _db_fixture
wired = _wired_fixture


def _clinic(*, known=None, no_alternative=False, extra=0):
    centro, sul = _units()
    unserved = _unit("Sem serviço")
    closed = replace(_unit("Fechada"), business_hours={})
    others = [_unit(f"Unidade {n}") for n in range(extra)]
    doctor = _doctor(units=[centro, sul, unserved, closed, *others])
    doctor.service_units = {ECO_ID: {u.id for u in [centro, sul, closed, *others]}}
    refusals = [(ECO_ID, centro, plan) for plan in (UNIMED, BRADESCO, AMIL)]
    refusals += [(ECO_ID, sul, plan) for plan in (BRADESCO, AMIL)]
    if no_alternative:
        refusals.append((ECO_ID, sul, UNIMED))
    tenant = _tenant(refused=_matrix("shared", doctor, *refusals), doctors=[doctor])
    walk = _Walk(
        tenant,
        [doctor],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fu.STEP_AWAITING_UNIT,
        flow_selected_type="Ecocardiograma",
        flow_selected_insurance=known,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    return walk, centro, sul, others


async def _pick(walk, unit):
    return await walk.say(inbound_routing_text(unit.name, f"unit|{unit.id}"))


def _rows(result):
    [card] = [b for b in result.bubbles if isinstance(b, SlotsBubble)]
    return card.rows


@pytest.mark.parametrize(
    ("known", "notice"),
    [
        (None, "Nessa unidade não há convênio para esse serviço."),
        ("Convênio não cadastrado", "Nessa unidade não há convênio para esse serviço."),
        ("Unimed", "Nessa unidade esse convênio não é aceito para o serviço."),
    ],
)
async def test_zero_accepted_with_alternative_offers_two_actions(known, notice):
    walk, centro, _, _ = _clinic(known=known)
    result = await _pick(walk, centro)
    assert result.flow_step == NO_PLAN_STEP
    assert result.flow_selected_unit_id == centro.id
    assert result.flow_selected_insurance is None
    [card] = [b for b in result.bubbles if isinstance(b, ButtonBubble)]
    assert notice in card.body
    assert [card.confirm_label, card.cancel_label] == [OTHER_UNITS, PARTICULAR]
    assert all(len(label) <= MAX_BUTTON_LABEL_CHARS for label in [OTHER_UNITS, PARTICULAR])
    assert walk.agenda.log["days"] == 0


async def test_zero_accepted_with_no_bookable_service_alternative_stays_automatic():
    walk, centro, _, _ = _clinic(no_alternative=True)
    result = await _pick(walk, centro)
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_insurance == "Particular"
    assert result.flow_insurance_auto is True
    assert any(fr.INSURANCE_NONE_ACCEPTED_UNIT in b.body for b in result.bubbles)


async def test_particular_previously_chosen_needs_no_card_when_there_are_alternatives():
    walk, centro, _, _ = _clinic(known="Particular")
    walk.state["flow_insurance_auto"] = False
    result = await _pick(walk, centro)
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_insurance == "Particular"
    assert result.flow_insurance_auto is False


async def test_final_guard_offers_other_units_with_the_known_refusal_notice():
    centro, sul, _ = matrix._units()
    doctor = matrix._doctor(units=[centro, sul])
    refusals = matrix._matrix(
        "shared", doctor, (matrix.ECO_ID, sul, matrix.UNIMED), (matrix.ECO_ID, sul, matrix.BRADESCO)
    )
    result = await fr.route(
        matrix._confirming(sul, doctor),
        matrix._tenant("shared", refusals, [doctor]),
        matrix._Agenda(),
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=[doctor],
        gate=matrix._Gate(armed=False),
    )
    assert result.flow_step == NO_PLAN_STEP
    assert result.appointment is None
    [card] = [b for b in result.bubbles if isinstance(b, ButtonBubble)]
    assert "Nessa unidade esse convênio não é aceito para o serviço." in card.body


async def test_alternatives_include_only_eligible_units_and_refilter_after_switch():
    walk, centro, sul, _ = _clinic(known="Bradesco")
    await _pick(walk, centro)
    result = await walk.say(OTHER_UNITS)
    assert result.flow_step == fu.STEP_AWAITING_UNIT
    assert result.flow_selected_unit_id == centro.id
    rows = _rows(result)
    alternative = f"unit|{sul.id}"
    assert [row for row in rows if row[0].startswith("unit|")] == [
        (alternative, sul.name, "Convênios: Unimed"),
    ]
    assert any(row[0] == f"unitpart|{centro.id}" and row[1] == PARTICULAR for row in rows)
    assert any(row[0] == "dayback|service" for row in rows)

    selected = await walk.tap(alternative)
    assert selected.flow_step == fr.STEP_AWAITING_INSURANCE
    assert selected.flow_selected_unit_id == sul.id
    assert [row[0] for row in _rows(selected) if row[0].startswith("ins|")] == [
        "ins|Unimed", "ins|particular", "ins|outro",
    ]


@pytest.mark.parametrize("from_list", [False, True])
async def test_particular_is_chosen_on_the_original_unit_from_either_control(from_list):
    walk, centro, _, _ = _clinic()
    await _pick(walk, centro)
    if from_list:
        await walk.say(OTHER_UNITS)
        result = await walk.tap(f"unitpart|{centro.id}")
    else:
        result = await walk.say(PARTICULAR)
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_unit_id == centro.id
    assert result.flow_selected_insurance == "Particular"
    assert result.flow_insurance_auto is False
    assert all(fr.INSURANCE_NONE_ACCEPTED_UNIT not in b.body for b in result.bubbles)


async def test_alternative_pages_reach_every_eligible_unit_and_fit_channel_limits():
    walk, centro, sul, others = _clinic(extra=11)
    walk.tenant.insurances[0] = walk.tenant.insurance_plans[0]["name"] = "Ó" * 90
    await _pick(walk, centro)
    result = await walk.say(OTHER_UNITS)
    seen = set()
    for _ in range(3):
        rows = _rows(result)
        assert len(rows) <= MAX_LIST_ROWS
        assert all(len(row[1]) <= MAX_LIST_ROW_TITLE_CHARS for row in rows)
        assert all(len(row[2]) <= MAX_LIST_ROW_DESCRIPTION_CHARS for row in rows if len(row) == 3)
        assert any(row[0] == f"unitpart|{centro.id}" for row in rows)
        for row in rows:
            if row[0].startswith("unit|"):
                seen.add(row[0])
                assert row[2].endswith("…")
        more = next((row[0] for row in rows if row[0].startswith("unitmore|")), None)
        if more is None:
            break
        result = await walk.tap(more)
    assert seen == {f"unit|{unit.id}" for unit in [sul, *others]}


async def test_free_text_delegates_once_and_the_recent_card_keeps_working():
    walk, centro, _, _ = _clinic()
    await _pick(walk, centro)
    delegation = await walk.say("Quanto tempo dura?")
    assert delegation.action == "delegate_llm"
    assert delegation.flow_step == NO_PLAN_STEP
    assert delegation.flow_selected_unit_id == centro.id
    assert (await walk.say(PARTICULAR)).flow_step == fr.STEP_AWAITING_DAY


async def test_resume_and_stale_configuration_check_the_same_alternatives():
    walk, centro, sul, _ = _clinic()
    await _pick(walk, centro)
    await walk.say(OTHER_UNITS)
    resumed = await fr.resume_bubbles(walk.conversation(), walk.tenant, walk.agenda, walk.doctors)
    assert [row for row in _rows(resumed) if row[0].startswith("unit|")] == [
        (f"unit|{sul.id}", sul.name, "Convênios: Unimed"),
    ]
    walk.tenant.insurance_refused = _matrix(
        "shared", walk.doctors[0],
        *((ECO_ID, unit, plan) for unit in (centro, sul) for plan in (UNIMED, BRADESCO, AMIL)),
    )
    result = await walk.say(OTHER_UNITS)
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_insurance_auto is True
    assert result.flow_selected_unit_id == centro.id


@pytest.mark.parametrize("reactivated", [False, True])
async def test_stale_card_resumes_the_parked_draft_at_the_original_unit(reactivated):
    walk, centro, _, _ = _clinic(no_alternative=True)
    walk.state.update(
        flow_step=NO_PLAN_STEP,
        flow_selected_unit_id=centro.id,
        flow_draft=draft_record(
            BookingDraft(service="Ecocardiograma", day=DAY, time=time(10)),
            saved_at=datetime.now(UTC),
        ),
    )
    if reactivated:
        result = await fr.resume_bubbles(
            walk.conversation(), walk.tenant, walk.agenda, walk.doctors
        )
    else:
        result = await walk.say(OTHER_UNITS)
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_unit_id == centro.id
    assert result.flow_insurance_auto is True
    assert result.resume_draft


@pytest.mark.parametrize("browsing", [False, True])
async def test_removed_service_returns_to_the_catalog_without_selecting_a_substitute(browsing):
    walk, centro, sul, _ = _clinic()
    await _pick(walk, centro)
    if browsing:
        await walk.say(OTHER_UNITS)
    walk.doctors[0].appointment_types = walk.tenant.appointment_types = [
        service for service in walk.tenant.appointment_types
        if service["name"] != "Ecocardiograma"
    ]
    if browsing:
        result = await walk.tap(f"unit|{sul.id}")
    else:
        result = await walk.say(PARTICULAR)
    assert result.flow_step == fr.STEP_AWAITING_SERVICE
    assert result.flow_selected_type is None
    assert result.flow_selected_unit_id is None
    assert result.flow_selected_insurance is None
    assert result.appointment is None


async def test_portal_records_the_same_rows_and_payload_used_by_whatsapp():
    walk, centro, _, _ = _clinic()
    await _pick(walk, centro)
    alternatives = await walk.say(OTHER_UNITS)
    [bubble] = alternatives.bubbles
    card = _bubble_interactive(bubble)
    assert offered_reply_ids([card]) == {row[0] for row in _rows(alternatives)}
    row = next(o for o in card["options"] if o["title"] == PARTICULAR)
    assert row["id"] == f"unitpart|{centro.id}"
    assert inbound_routing_text(row["title"], row["id"]) == f"{PARTICULAR} ({centro.id})"


async def test_refused_ai_draft_parks_and_continues_through_the_selected_alternative():
    centro, sul = _unit("Centro"), _unit("Zona Sul")
    doctor = _with_units(_sole(), centro, sul)
    tenant = _two_plans(
        _refuse((str(CONSULTA), str(centro.id), PLAN), (str(CONSULTA), str(centro.id), PLAN_B))
    )
    draft = BookingDraft(service="Consulta", insurance="Unimed", day=DAY, time=time(10))
    resolution = await _resolve(
        draft, conv=_conv(flow_selected_unit_id=centro.id), tenant=tenant, pros=[doctor],
    )
    assert resolution.landing_step == NO_PLAN_STEP
    assert resolution.result.flow_selected_unit_id == centro.id
    assert resolution.result.flow_selected_insurance is None
    parked = draft_from_record(resolution.result.flow_draft, now=NOW)
    assert (parked.insurance, parked.day, parked.time) == (None, DAY, time(10))

    walk = _Walk(
        tenant, [doctor],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=resolution.landing_step,
        flow_selected_type="Consulta",
        flow_selected_unit_id=centro.id,
        flow_attendee_name=fr.ATTENDEE_SELF,
        flow_draft=resolution.result.flow_draft,
    )
    browsing = await walk.say(OTHER_UNITS)
    assert browsing.flow_draft == resolution.result.flow_draft
    assert not browsing.resume_draft
    selected = await walk.tap(f"unit|{sul.id}")
    assert selected.flow_step == fr.STEP_AWAITING_INSURANCE
    assert selected.flow_draft == resolution.result.flow_draft
    assert selected.flow_selected_unit_id == sul.id
    answer = await walk.tap("ins|Unimed")
    assert answer.resume_draft
    resumed = await _resolve(
        parked,
        conv=_conv(flow_selected_unit_id=sul.id, flow_selected_insurance="Unimed"),
        tenant=tenant, pros=[doctor],
    )
    assert resumed.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert resumed.result.flow_selected_unit_id == sul.id
    assert resumed.result.flow_selected_insurance == "Unimed"


@pytest.mark.parametrize("from_list", [False, True])
async def test_worker_resumes_the_draft_with_the_explicit_particular_answer(from_list, monkeypatch):
    centro, sul = _unit("Centro"), _unit("Zona Sul")
    doctor = _with_units(_sole(), centro, sul)
    tenant = _two_plans(
        _refuse((str(CONSULTA), str(centro.id), PLAN), (str(CONSULTA), str(centro.id), PLAN_B)),
        id=uuid4(),
    )
    walk = _Walk(
        tenant, [doctor],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=NO_PLAN_STEP,
        flow_selected_type="Consulta",
        flow_selected_unit_id=centro.id,
        flow_attendee_name=fr.ATTENDEE_SELF,
        flow_draft=draft_record(
            BookingDraft(service="Consulta", insurance="Unimed", day=DAY, time=time(10)),
            saved_at=datetime.now(UTC),
        ),
    )
    if from_list:
        await walk.say(OTHER_UNITS)
    waiting = walk.conversation()

    async def load_context(_reply, _tenant):
        return SimpleNamespace(conversation=waiting)

    async def resolve(_reply, _tenant, draft, ctx, **_kwargs):
        return await _resolve(draft, conv=ctx.conversation, tenant=tenant, pros=[doctor])

    monkeypatch.setattr(draft_resolution, "_load_draft_context", load_context)
    monkeypatch.setattr(draft_resolution, "_resolve_draft", resolve)
    if from_list:
        answer = await walk.tap(f"unitpart|{centro.id}")
    else:
        answer = await walk.say(PARTICULAR)
    assert answer.resume_draft
    result = await draft_resolution._resume_booking_draft(
        SimpleNamespace(conversation_id=waiting.id), tenant, answer
    )
    assert result.flow_step == fr.STEP_AWAITING_CONFIRMATION
    assert result.flow_selected_unit_id == centro.id
    assert result.flow_selected_insurance == "Particular"
    assert result.flow_insurance_auto is False


@pytest.mark.parametrize("expired", [False, True])
async def test_worker_continue_sends_only_the_current_draft_landing_or_fallback(wired, expired):
    tenant = await _seed_tenant(
        wired, collect=False, doctors=[("Dra. Ana", ["Primeira Consulta"])]
    )
    await _onboard(tenant)
    doctor = await _professional(wired, tenant, "Dra. Ana")
    parked = await _conversation(wired, tenant)
    centro = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro")
    sul = Unit(id=uuid4(), tenant_id=tenant.id, name="Sul")
    saved_at = datetime.now(UTC) - timedelta(days=2) if expired else datetime.now(UTC)
    async with wired() as session:
        session.add_all([centro, sul])
        await session.flush()
        session.add_all([
            ProfessionalUnit(professional_id=doctor.id, unit_id=unit.id) for unit in (centro, sul)
        ])
        row = await session.get(Conversation, parked.id)
        row.flow_state = FlowState.SERVICE_CATALOG
        row.flow_step = NO_PLAN_STEP
        row.flow_selected_type = "Primeira Consulta"
        row.flow_selected_unit_id = centro.id
        row.flow_attendee_name = fr.ATTENDEE_SELF
        row.flow_draft = draft_record(
            BookingDraft(service="Primeira Consulta", day=CURRENT_DAY, time=time(10)),
            saved_at=saved_at,
        )
        row.reactivation_origin = FlowState.SERVICE_CATALOG.value
        await session.commit()
    _WireClient.sends.clear()

    await _wa_turn(tenant, "Sim")

    current = await _conversation(wired, tenant)
    assert current.flow_selected_unit_id == centro.id
    assert current.flow_draft is None
    if expired:
        assert current.flow_step == fr.STEP_AWAITING_DAY
        assert [kind for kind, _ in _WireClient.sends] == ["list"]
    else:
        assert current.flow_step == fr.STEP_AWAITING_CONFIRMATION
        assert current.flow_selected_slot == f"{CURRENT_DAY.isoformat()}T10:00"
        assert [kind for kind, _ in _WireClient.sends] == ["text", "buttons"]
