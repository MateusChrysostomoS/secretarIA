"""TASK-088 review round (front "F-flow"): five findings of the reviewers, each with a failing test.

1. A stale "Seguir particular" / "Unidades c/ convênio" / unit row tapped at the convênio list is
   NEVER stored as the patient's insurance.
2. Nothing accepted for the combination at a unit the patient did NOT choose (no units / only one)
   and a typed out-of-catalog convênio is held: Particular, saying so - never kept in silence.
3. A single-doctor clinic that gets a convênio early follows the same path as a multi-doctor one
   (the gate: filtered list or Particular with a line) - no mid-flow "não atende" card.
4. A typed "ver mais unidades" pages the unit-with-convênio list.
5. "Marcar outra consulta": the appointment being replaced never pads its own replacement.
"""

import os

os.environ.setdefault("APP_ENV", "test")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, SlotsBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402
from secretaria.services import flow_router as fr, flow_units as fu  # noqa: E402
from secretaria.workers.whatsapp import typed_more as tm  # noqa: E402
from tests import test_booking_buffer_paths as bp  # noqa: E402
from tests.test_booking_order_service_first import (  # noqa: E402
    AMIL,
    BRADESCO,
    ECO_ID,
    UNIMED,
    _doctor,
    _matrix,
    _tenant,
    _Walk,
)
from tests.test_units_with_insurance import OTHER_UNITS, PARTICULAR, _clinic, _pick  # noqa: E402


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(fr, "_notice_now", lambda tz: bp.NOW.astimezone(tz))


def _texts(result) -> str:
    return " ".join(getattr(b, "body", "") or "" for b in result.bubbles)


def _ins_rows(result) -> list[str]:
    return [
        row[0]
        for b in result.bubbles
        if isinstance(b, SlotsBubble)
        for row in b.rows
        if row[0].startswith("ins|")
    ]


# --------------------------------------------------------------------------
# 1. A stale unit-card control at the convênio list
# --------------------------------------------------------------------------


async def _at_sul_list():
    """Centro (no plan) -> new card -> "Unidades c/ convênio" -> Zona Sul: its convênio list."""
    walk, centro, sul, _ = _clinic()
    await _pick(walk, centro)
    await walk.say(OTHER_UNITS)
    selected = await walk.tap(f"unit|{sul.id}")
    assert selected.flow_step == fr.STEP_AWAITING_INSURANCE
    assert selected.flow_selected_unit_id == sul.id
    return walk, centro, sul


async def _assert_list_asked_again(walk, result, sul):
    assert result.flow_step == fr.STEP_AWAITING_INSURANCE
    assert result.flow_selected_insurance is None
    assert result.flow_selected_unit_id == sul.id
    assert _ins_rows(result) == ["ins|Unimed", "ins|particular", "ins|outro"]
    assert walk.agenda.log["days"] == 0


@pytest.mark.parametrize(
    "stale",
    [
        pytest.param(PARTICULAR, id="old-button-no-payload"),
        pytest.param(OTHER_UNITS, id="other-units-button"),
        pytest.param("Seguir particular (bad-payload)", id="particular-row-unreadable-payload"),
    ],
)
async def test_stale_unit_card_labels_reask_the_list_and_never_store_an_insurance(stale):
    walk, _centro, sul = await _at_sul_list()
    result = await walk.say(stale)
    await _assert_list_asked_again(walk, result, sul)


async def test_stale_particular_row_of_the_left_unit_reasks_the_current_list():
    walk, centro, sul = await _at_sul_list()
    result = await walk.say(inbound_routing_text(PARTICULAR, f"unitpart|{centro.id}"))
    await _assert_list_asked_again(walk, result, sul)


async def test_stale_unit_rows_reask_the_current_list():
    walk, centro, sul = await _at_sul_list()
    for stale in (
        inbound_routing_text("Zona Sul", f"unit|{sul.id}"),
        inbound_routing_text("Centro", f"unit|{centro.id}"),
        inbound_routing_text(fu.LABEL_MORE_UNITS, "unitmore|1"),
    ):
        result = await walk.say(stale)
        await _assert_list_asked_again(walk, result, sul)


async def test_particular_row_naming_the_unit_the_patient_is_at_is_honoured():
    walk, _centro, sul = await _at_sul_list()
    result = await walk.say(inbound_routing_text(PARTICULAR, f"unitpart|{sul.id}"))
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_unit_id == sul.id
    assert result.flow_selected_insurance == "Particular"
    assert result.flow_insurance_auto is False


async def test_a_real_typed_plan_name_is_still_kept_at_the_list():
    walk, _centro, sul = await _at_sul_list()
    result = await walk.say("Unimed")
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_insurance == "Unimed"
    assert result.flow_selected_unit_id == sul.id


# --------------------------------------------------------------------------
# 2. Nothing accepted + a typed out-of-catalog convênio held: Particular, saying so
# --------------------------------------------------------------------------


def _single_doctor_walk(*, accept_none=True, known="Golden Cross", **state):
    doctor = _doctor()
    cells = [(ECO_ID, None, plan) for plan in (UNIMED, BRADESCO, AMIL)] if accept_none else [
        (ECO_ID, None, UNIMED)
    ]
    tenant = _tenant(refused=_matrix("shared", doctor, *cells), doctors=[doctor])
    walk = _Walk(
        tenant,
        [doctor],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE,
        flow_selected_insurance=known,
        flow_attendee_name=fr.ATTENDEE_SELF,
        **state,
    )
    return walk, doctor


async def test_typed_out_of_catalog_plan_with_nothing_accepted_goes_particular_and_says_so():
    walk, _doctor_row = _single_doctor_walk()
    result = await walk.say("Ecocardiograma")
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_insurance == "Particular"
    assert result.flow_insurance_auto is True
    assert fr.INSURANCE_REFUSED_PARTICULAR in _texts(result)


async def test_typed_out_of_catalog_plan_is_kept_when_some_plan_is_accepted():
    walk, _doctor_row = _single_doctor_walk(accept_none=False)
    result = await walk.say("Ecocardiograma")
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_insurance == "Golden Cross"
    assert fr.INSURANCE_REFUSED_PARTICULAR not in _texts(result)


async def test_typed_particular_with_nothing_accepted_stays_without_a_notice():
    walk, _doctor_row = _single_doctor_walk(known="Particular")
    walk.state["flow_insurance_auto"] = False
    result = await walk.say("Ecocardiograma")
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_insurance == "Particular"
    assert result.flow_insurance_auto is False
    assert fr.INSURANCE_REFUSED_PARTICULAR not in _texts(result)


# --------------------------------------------------------------------------
# 3. A single-doctor service tap with an early convênio the matrix refuses: the gate, no card
# --------------------------------------------------------------------------


async def test_single_doctor_refused_early_plan_with_others_accepted_gets_the_filtered_list():
    walk, _doctor_row = _single_doctor_walk(accept_none=False, known="Unimed")
    result = await walk.say("Ecocardiograma")
    assert result.flow_step == fr.STEP_AWAITING_INSURANCE
    assert fr.INSURANCE_REFUSED_LIST in _texts(result)
    assert _ins_rows(result) == ["ins|Bradesco", "ins|Amil", "ins|particular", "ins|outro"]
    assert not any(isinstance(b, ButtonBubble) for b in result.bubbles)


async def test_single_doctor_refused_early_plan_with_none_accepted_goes_particular():
    walk, _doctor_row = _single_doctor_walk(known="Unimed")
    result = await walk.say("Ecocardiograma")
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_insurance == "Particular"
    assert result.flow_insurance_auto is True
    assert fr.INSURANCE_REFUSED_PARTICULAR in _texts(result)


async def test_a_conversation_parked_on_the_not_accepted_card_still_answers_it():
    walk, _doctor_row = _single_doctor_walk(known="Unimed")
    walk.state.update(
        flow_step=fr.STEP_AWAITING_PLAN_REFUSED, flow_selected_type="Ecocardiograma"
    )
    result = await walk.say(fr.LABEL_PLAN_OTHER_SERVICE)
    assert result.flow_step == fr.STEP_AWAITING_SERVICE


# --------------------------------------------------------------------------
# 4. A typed "ver mais unidades"
# --------------------------------------------------------------------------


def test_typed_more_kind_knows_the_unit_list():
    assert tm.typed_more_kind("ver mais unidades") == "unidades"
    assert tm.typed_more_kind("Ver mais Unidades!") == "unidades"


def test_typed_more_picks_the_unit_more_row_only_for_a_bare_or_unit_phrase():
    card = {
        "options": [
            {"id": "unit|abc", "title": "Zona Sul"},
            {"id": "unitmore|1", "title": fu.LABEL_MORE_UNITS},
        ]
    }
    assert tm.more_row(card, "unidades") == ("unitmore|1", fu.LABEL_MORE_UNITS)
    assert tm.more_row(card, "") == ("unitmore|1", fu.LABEL_MORE_UNITS)
    assert tm.more_row(card, "servicos") is None


# --------------------------------------------------------------------------
# 5. "Marcar outra consulta": the replaced appointment never pads its replacement
# --------------------------------------------------------------------------

REPLACED_ID = bp._APPT_ID


def _replacing(**extra):
    return bp._conversation(
        flow_step=fr.STEP_AWAITING_DAY,
        flow_replaces_appointment_id=REPLACED_ID,
        **extra,
    )


def _mine():
    # The visit being replaced: 08:30-09:00 with event "evt-mine"; the day is open 08:00-10:00.
    return bp._Agenda(consultations=[(*bp._win("08:30"), "evt-mine")])


def _appointment_of(doctor):
    return bp._appointment(doctor, bp._win("08:30")[0])


async def test_marcar_outra_slot_list_ignores_the_replaced_appointment():
    doctor = bp._doctor(30)
    tap = bp._day_tap(bp.D1)
    replacing = await fr.route(
        _replacing(), bp._tenant(), _mine(), tap,
        upcoming_appointments=[_appointment_of(doctor)], professionals=[doctor],
    )
    # Its own 08:30-09:00 never pads: 08:00 and 09:00 stay, like an ordinary reschedule.
    assert bp._slot_rows(replacing) == ["08:00", "09:00", "09:30"]
    # A new booking that replaces nothing is still padded by the visit.
    ordinary = await fr.route(
        bp._conversation(flow_step=fr.STEP_AWAITING_DAY), bp._tenant(), _mine(), tap,
        upcoming_appointments=[_appointment_of(doctor)], professionals=[doctor],
    )
    assert bp._slot_rows(ordinary) == ["09:30"]


async def test_marcar_outra_day_list_ignores_the_replaced_appointment():
    doctor = bp._doctor(30)
    narrow = dict(hours=(8, 9), consultations=[(*bp._win("08:30"), "evt-mine")])
    with fr.replacing_booking_scope("evt-mine"):
        assert await bp._day_list(bp._Agenda(**narrow), doctor) == [
            bp.D1.isoformat(), bp.D2.isoformat()
        ]
    assert await bp._day_list(bp._Agenda(**narrow), doctor) == [bp.D2.isoformat()]
    assert fr._REPLACED_EVENT.get() is None


async def test_marcar_outra_confirm_recheck_ignores_the_replaced_appointment():
    doctor = bp._doctor(30)
    slot = f"{bp.D1.isoformat()}T09:00:00"

    def conversation(**extra):
        return bp._conversation(
            flow_step=fr.STEP_AWAITING_CONFIRMATION, flow_selected_slot=slot,
            flow_selected_day=slot[:10], **extra,
        )

    agenda = _mine()
    await fr.route(
        conversation(flow_replaces_appointment_id=REPLACED_ID), bp._tenant(), agenda,
        fr.LABEL_CONFIRM, upcoming_appointments=[_appointment_of(doctor)],
        professionals=[doctor],
    )
    assert len(agenda.created) == 1
    # Without the marker the same 09:00 touches the visit: refused like a slot taken.
    plain = _mine()
    await fr.route(
        conversation(), bp._tenant(), plain, fr.LABEL_CONFIRM,
        upcoming_appointments=[_appointment_of(doctor)], professionals=[doctor],
    )
    assert plain.created == []


async def test_the_replaced_event_is_unknown_without_its_appointment_row():
    doctor = bp._doctor(30)
    agenda = _mine()
    result = await fr.route(
        _replacing(), bp._tenant(), agenda, bp._day_tap(bp.D1),
        upcoming_appointments=[], professionals=[doctor],
    )
    assert bp._slot_rows(result) == ["09:30"]


async def test_code_gate_promotion_recheck_ignores_the_replaced_appointment(monkeypatch):
    from secretaria.workers.shared import booking_hold as worker_holds

    async def _no_pix_hold(*_a, **_k):
        return False

    async def _buffer_of_30(_reply, _tenant, _held):
        return 30, []

    async def _replaced(_reply, _tenant):
        return "evt-mine"

    monkeypatch.setattr(worker_holds, "live_hold_overlaps", _no_pix_hold)
    monkeypatch.setattr(worker_holds, "_held_buffer_rule", _buffer_of_30)
    monkeypatch.setattr(worker_holds, "_replaced_event_id", _replaced)
    reply = SimpleNamespace(conversation_id=uuid4())
    tenant = SimpleNamespace(id=uuid4())
    start, end = bp._win("09:00")
    held = SimpleNamespace(start_at=start, end_at=end, professional_id=uuid4())
    mine = [(*bp._win("08:30"), "evt-mine")]
    assert await worker_holds._held_slot_unavailable(
        reply, tenant, held, bp._Agenda(consultations=mine)
    ) is False
    # Another patient's visit at the same place still pads it.
    other = [(*bp._win("08:30"), "evt-other")]
    assert await worker_holds._held_slot_unavailable(
        reply, tenant, held, bp._Agenda(consultations=other)
    ) is True


async def test_replaced_event_id_reads_the_conversation_marker_and_stays_tenant_scoped(
    monkeypatch,
):
    from secretaria.workers.shared import booking_hold as worker_holds

    tenant = SimpleNamespace(id=uuid4())
    appointment = SimpleNamespace(tenant_id=tenant.id, google_event_id="evt-mine")
    conversation = SimpleNamespace(flow_replaces_appointment_id=REPLACED_ID)

    class _Session:
        def __init__(self, conv, appt):
            self.conv, self.appt = conv, appt

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, model, _key):
            return self.conv if model is worker_holds.Conversation else self.appt

    def factory(conv, appt):
        return lambda: _Session(conv, appt)

    reply = SimpleNamespace(conversation_id=uuid4())
    monkeypatch.setattr(worker_holds, "async_session_factory", factory(conversation, appointment))
    assert await worker_holds._replaced_event_id(reply, tenant) == "evt-mine"
    foreign = SimpleNamespace(tenant_id=uuid4(), google_event_id="evt-x")
    monkeypatch.setattr(worker_holds, "async_session_factory", factory(conversation, foreign))
    assert await worker_holds._replaced_event_id(reply, tenant) is None
    monkeypatch.setattr(
        worker_holds,
        "async_session_factory",
        factory(SimpleNamespace(flow_replaces_appointment_id=None), appointment),
    )
    assert await worker_holds._replaced_event_id(reply, tenant) is None

    def boom():
        raise RuntimeError("database down")

    monkeypatch.setattr(worker_holds, "async_session_factory", boom)
    assert await worker_holds._replaced_event_id(reply, tenant) is None

