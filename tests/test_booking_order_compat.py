"""TASK-088 Task 7 - every path that is not the plain button flow follows the new booking order.

The owner's order (2026-11-20): pra-quem -> servico -> medico -> unidade -> convenio -> dia.
Task 6 reordered the buttons; this file pins the rest: the AI draft resolver and its hand-back
into the buttons, the back rows, the "Alterar dados" reschedule, and chats parked on the old
steps on deploy day. One rule runs through all of them (SPEC, designer decisions 1 and 4): a
convenio that arrives BEFORE the step (a draft, a reschedule, a parked chat) and that the
combination refuses is never swapped for Particular in silence - the filtered list with one
line, or, with nothing else accepted, Particular saying so.

Router and resolver level: no database, no network.
"""

import datetime as dt
import os
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")

from secretaria.ai.formatter import SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import Conversation, FlowState  # noqa: E402
from secretaria.services import (  # noqa: E402
    appointment_edit as ae,
    appointment_edit_flow as aef,
    flow_router as fr,
    flow_units as fu,
)
from secretaria.services.booking_draft import BookingDraft, draft_from_record  # noqa: E402
from secretaria.services.insurance_acceptance import AcceptanceExceptions  # noqa: E402
from secretaria.workers.shared.draft_resolution import _fold_answer  # noqa: E402
from tests._edit_flow_support import TZ as EDIT_TZ  # noqa: E402
from tests.test_booking_capture_worker import _off as _switch_off  # noqa: E402
from tests.test_booking_draft_resolver import (  # noqa: E402  # noqa: E402
    DAY,
    NOW,
    _conv,
    _doctor as _draft_doctor,
    _resolve,
    _sole,
    _tenant as _tenant_base,  # noqa: E402
)
from tests.test_booking_draft_resume_questions import (  # noqa: E402
    _conversation,
    _hand_back,
    _onboard,
    _professional,
    _seed_tenant,
    _wa_turn,
    db as _db_fixture,
    wired as _wired_fixture,
)
from tests.test_booking_order_service_first import (  # noqa: E402
    AMIL,
    BRADESCO,
    CONSULTA_ID,
    ECO_ID,
    UNIMED,
    _Agenda,
    _doctor,
    _list,
    _matrix,
    _tenant,
    _units,
    _Walk,
)
from tests.test_t086_ai_draft import (  # noqa: E402
    CONSULTA,
    PLAN,
    RETORNO,
    SERVICES,
    _unit,
    _with_units,
)
from tests.test_t086_edit_flow import (  # noqa: E402
    CENTRO,
    NORTE,
    P_AMIL,
    P_UNIMED,
    SUL,
    SVC_C,
    SVC_R,
    _appt,
    _ctx,
    _doctor as _edit_doctor,  # noqa: E402
    _draft,
    _shared,
    _step,
)

TEN = dt.time(10, 0)
db = _db_fixture
wired = _wired_fixture
PLAN_B = "plan-bradesco"


def _two_plans(refused=None, **kw):
    """A v2 clinic that collects two convenios (Unimed + Bradesco)."""
    base = dict(
        initial_flows={"ai_draft_v2": True},
        appointment_types=[dict(entry) for entry in SERVICES],
        collect_insurance=True,
        insurance_mode="shared",
        insurances=["Unimed", "Bradesco"],
        insurance_plans=[{"id": PLAN, "name": "Unimed"}, {"id": PLAN_B, "name": "Bradesco"}],
        insurance_refused=refused,
    )
    base.update(kw)
    return _tenant_base(**base)


def _refuse(*cells):
    return AcceptanceExceptions(clinic=frozenset(cells))


def _texts(result) -> list[str]:
    return [b.body for b in result.bubbles if isinstance(b, TextBubble)]


# --------------------------------------------------------------------------
# 1. The AI draft: pra-quem -> servico -> medico -> unidade -> convenio -> dia
# --------------------------------------------------------------------------


async def test_ai_draft_with_service_and_insurance_lands_on_next_unanswered_step():
    # (a) Service + an accepted convenio, a doctor with two units: the UNIT is the next open
    # question (never the convenio again, never the doctor), and the answer rides along.
    sole = _with_units(_sole(), _unit("Centro"), _unit("Sul"))
    res = await _resolve(
        BookingDraft(service="Consulta", insurance="unimed"),
        tenant=_two_plans(),
        pros=[sole],
    )
    assert res.landing_step == fu.STEP_AWAITING_UNIT
    assert res.result.flow_selected_insurance == "Unimed"
    assert res.result.flow_selected_type == "Consulta"
    # It waits on the unit question: the service and the convenio stay in the parked draft.
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.service, parked.insurance) == ("Consulta", "Unimed")

    # (b) One unit (implied): the accepted convenio is kept and the DAYS open.
    one = _with_units(_sole(), _unit("Centro"))
    res = await _resolve(
        BookingDraft(service="Consulta", insurance="unimed"), tenant=_two_plans(), pros=[one]
    )
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_insurance == "Unimed"

    # (c) Two doctors do the service: the doctor of THAT service comes before anything else,
    # and the convenio is not asked in front of it.
    a, b = _draft_doctor("Dra. A", ["Consulta"]), _draft_doctor("Dr. B", ["Consulta"])
    res = await _resolve(
        BookingDraft(service="Consulta", insurance="unimed"),
        tenant=_two_plans(),
        pros=[a, b],
    )
    assert res.landing_step == fr.STEP_AWAITING_SERVICE_PROFESSIONAL
    assert res.result.flow_selected_type == "Consulta"
    assert res.result.flow_selected_professional_id is None

    # (d) Nothing but the convenio: the SERVICE is asked first (the clinic's catalog on a
    # multi-doctor clinic), the convenio waits for its turn.
    res = await _resolve(BookingDraft(insurance="unimed"), tenant=_two_plans(), pros=[a, b])
    assert res.landing_step == fr.STEP_AWAITING_CATALOG_SERVICE
    res = await _resolve(BookingDraft(insurance="unimed"), tenant=_two_plans(), pros=[_sole()])
    assert res.landing_step == fr.STEP_AWAITING_SERVICE


async def test_draft_insurance_refused_reasks_filtered_list_with_notice():
    sole = _with_units(_sole(), (unit := _unit("Centro")))
    refused = _refuse((str(CONSULTA), str(unit.id), PLAN))
    draft = BookingDraft(service="Consulta", insurance="unimed", day=DAY, time=TEN)

    # Unimed refused, Bradesco accepted: the filtered list, one line on top, the day/time
    # wait. Never the express card, never Particular in silence.
    res = await _resolve(draft, tenant=_two_plans(refused), pros=[sole])
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    [bubble] = [b for b in res.result.bubbles if isinstance(b, SlotsBubble)]
    assert bubble.body == fr.INSURANCE_REFUSED_LIST
    assert [row[0] for row in bubble.rows if row[0].startswith("ins|")] == [
        "ins|Bradesco",
        "ins|particular",
        "ins|outro",
    ]
    assert res.result.flow_selected_insurance is None
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.day, parked.time) == (DAY, TEN)

    # The answer resumes the draft: the express card, with the chosen plan.
    resumed = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=TEN),
        conv=_conv(flow_selected_insurance="Bradesco"),
        tenant=_two_plans(refused),
        pros=[sole],
    )
    assert resumed.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert resumed.result.flow_selected_insurance == "Bradesco"

    # Nothing else accepted: Particular, SAYING so, in front of whatever lands next.
    none = _refuse((str(CONSULTA), str(unit.id), PLAN), (str(CONSULTA), str(unit.id), PLAN_B))
    res = await _resolve(draft, tenant=_two_plans(none), pros=[sole])
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert res.result.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    assert fr.INSURANCE_REFUSED_PARTICULAR in _texts(res.result)[0]


async def test_ai_draft_landing_never_skips_the_unit_or_the_convenio():
    """R16: a draft holding a day and a time cannot reach the slot list or the express card
    with the convenio unanswered (clinic collects it) - the question comes first, the day waits."""
    sole = _with_units(_sole(), (unit := _unit("Centro")))
    draft = BookingDraft(service="Consulta", day=DAY, time=TEN)

    res = await _resolve(draft, tenant=_two_plans(), pros=[sole])
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.service, parked.day, parked.time) == ("Consulta", DAY, TEN)

    # Nothing accepted for the combination: Particular, with the line before the card.
    none = _refuse((str(CONSULTA), str(unit.id), PLAN), (str(CONSULTA), str(unit.id), PLAN_B))
    res = await _resolve(draft, tenant=_two_plans(none), pros=[sole])
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert res.result.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    assert fr.INSURANCE_NONE_ACCEPTED in _texts(res.result)[0]

    # A clinic that does not collect it: no question at all (today's landing).
    plain = _two_plans(collect_insurance=False, insurances=[], insurance_plans=[])
    res = await _resolve(draft, tenant=plain, pros=[sole])
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION


async def test_ai_draft_waits_through_the_unit_question_and_resumes():
    """R17: two units, the draft holds a day - it is parked on the unit question, and the
    unit answer is what the resumed resolver reads."""
    centro, sul = _unit("Centro"), _unit("Sul")
    sole = _with_units(_sole(), centro, sul)
    draft = BookingDraft(service="Consulta", day=DAY, time=TEN)
    plain = _two_plans(collect_insurance=False, insurances=[], insurance_plans=[])
    res = await _resolve(draft, tenant=plain, pros=[sole])
    assert res.landing_step == fu.STEP_AWAITING_UNIT
    assert res.result.flow_step in fr.DRAFT_WAIT_STEPS
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.service, parked.day, parked.time) == ("Consulta", DAY, TEN)

    # The unit answer, as the worker re-runs the resolver: the conversation now holds the unit.
    resumed = await _resolve(
        parked,
        conv=_conv(flow_selected_unit_id=centro.id),
        tenant=_two_plans(collect_insurance=False, insurances=[], insurance_plans=[]),
        pros=[sole],
    )
    assert resumed.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert resumed.result.flow_selected_unit_id == centro.id


def test_the_parking_set_and_the_folds_cover_the_new_questions():
    for step in (
        fr.STEP_AWAITING_CATALOG_SERVICE,
        fr.STEP_AWAITING_SERVICE_PROFESSIONAL,
        fu.STEP_AWAITING_UNIT,
        fu.STEP_AWAITING_UNIT_CONFIRM,
        fr.STEP_AWAITING_INSURANCE,
        fr.STEP_AWAITING_PROFESSIONAL,
        fr.STEP_AWAITING_SERVICE,
    ):
        assert step in fr.DRAFT_WAIT_STEPS
    pro_id = uuid4()
    draft = BookingDraft(service="Retorno", day=DAY)
    # The clinic catalog answer replaces the service (and the doctor, when it implies one).
    answer = fr.FlowRouterResult(
        action="reply",
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Consulta",
        flow_selected_professional_id=pro_id,
    )
    folded = _fold_answer(draft, fr.STEP_AWAITING_CATALOG_SERVICE, answer)
    assert (folded.service, folded.professional_id, folded.day) == ("Consulta", pro_id, DAY)
    # The doctor-of-a-service answer replaces the doctor and keeps the service.
    folded = _fold_answer(draft, fr.STEP_AWAITING_SERVICE_PROFESSIONAL, answer)
    assert (folded.service, folded.professional_id) == ("Retorno", pro_id)


# --------------------------------------------------------------------------
# 2. The hand-back into the buttons (`enter_guided_booking`)
# --------------------------------------------------------------------------


async def test_the_hand_back_follows_the_new_order_unit_then_convenio_then_day():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    tenant = _tenant(doctors=[ana])
    agenda = _Agenda()

    # Two units: the unit first - the convenio was the first question in the old order.
    result = await fr.enter_guided_booking(
        tenant, agenda, "Consulta", insurance=None, professionals=[ana]
    )
    assert result.flow_step == fu.STEP_AWAITING_UNIT

    # One unit (implied): the convenio list of that combination, then the days.
    one = _doctor(units=[centro])
    result = await fr.enter_guided_booking(
        _tenant(doctors=[one]), agenda, "Consulta", insurance=None, professionals=[one]
    )
    assert result.flow_step == fr.STEP_AWAITING_INSURANCE
    assert result.flow_selected_unit_id == centro.id

    # An accepted convenio is kept: straight to the days.
    result = await fr.enter_guided_booking(
        _tenant(doctors=[one]), agenda, "Consulta", insurance="Unimed", professionals=[one]
    )
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_insurance == "Unimed"

    # A refused one is asked again with the line - never Particular in silence.
    refused = _matrix("shared", one, (CONSULTA_ID, centro, UNIMED))
    result = await fr.enter_guided_booking(
        _tenant("shared", refused, [one]),
        agenda,
        "Consulta",
        insurance="Unimed",
        professionals=[one],
    )
    assert result.flow_step == fr.STEP_AWAITING_INSURANCE
    assert result.flow_selected_insurance is None
    assert _list(result).body == fr.INSURANCE_REFUSED_LIST


async def test_the_hand_back_rejudges_an_automatic_particular():
    """An automatic Particular (nothing was accepted where the booking was) is never carried into
    a unit that accepts something: the question is asked."""
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    # Nothing accepted at Zona Sul for the consulta, everything at Centro.
    refused = _matrix(
        "shared",
        ana,
        (CONSULTA_ID, sul, UNIMED),
        (CONSULTA_ID, sul, BRADESCO),
        (CONSULTA_ID, sul, AMIL),
    )
    tenant = _tenant("shared", refused, [ana])
    result = await fr.enter_guided_booking(
        tenant,
        _Agenda(),
        "Consulta",
        insurance=fr.LABEL_INSURANCE_PARTICULAR,
        professionals=[ana],
        recorded=SimpleNamespace(flow_insurance_auto=True),
    )
    # No unit chosen yet: the unit is asked; the Particular must not decide for the next unit.
    assert result.flow_step == fu.STEP_AWAITING_UNIT
    assert result.flow_selected_insurance is None


# --------------------------------------------------------------------------
# 3. Back rows in the new order
# --------------------------------------------------------------------------


async def _to_insurance_list(tenant, doctors, doctor, unit):
    walk = _Walk(tenant, doctors)
    await walk.to_unit_list(doctor)
    if unit is not None:
        await walk.tap(f"unit|{unit.id}")
    return walk


async def test_back_from_insurance_goes_to_unit_or_doctor():
    centro, sul = _units()
    # Two units: the patient CHOSE one -> back is the unit list.
    ana = _doctor(units=[centro, sul])
    walk = await _to_insurance_list(_tenant(doctors=[ana]), [ana], ana, sul)
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
    rows = _list(walk.last).rows
    assert rows[-1] == (f"{fr.ROW_DAY_BACK_PREFIX}{fr.BACK_TARGET_UNIT}", fu.LABEL_ANOTHER_UNIT)
    assert len(rows) <= 10  # WhatsApp's limit, back row included
    await walk.tap(rows[-1][0])
    assert walk.last.flow_step == fu.STEP_AWAITING_UNIT
    assert walk.last.flow_selected_insurance is None

    # One unit (implied, nothing to go back to): back is the service step of the doctor.
    solo = _doctor(units=[centro])
    walk = await _to_insurance_list(_tenant(doctors=[solo]), [solo], solo, None)
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
    rows = _list(walk.last).rows
    assert rows[-1] == (
        f"{fr.ROW_DAY_BACK_PREFIX}{fr.BACK_TARGET_SERVICE}",
        fr.LABEL_ANOTHER_SERVICE,
    )
    await walk.tap(rows[-1][0])
    assert walk.last.flow_step == fr.STEP_AWAITING_SERVICE
    assert fr.STEP_AWAITING_INSURANCE not in walk.steps[-1:]

    # A TYPED back label works too (it is a control row, not a plan name).
    walk = await _to_insurance_list(_tenant(doctors=[ana]), [ana], ana, sul)
    await walk.say(fu.LABEL_ANOTHER_UNIT)
    assert walk.last.flow_step == fu.STEP_AWAITING_UNIT


async def test_the_insurance_list_keeps_all_plans_and_stays_within_ten_rows():
    """The back row eats one of the ten WhatsApp rows: the page shrinks, no plan is cut."""
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    tenant = _tenant(doctors=[ana])
    names = [f"Plano {n:02d}" for n in range(1, 10)]
    tenant.insurance_plans = [{"id": str(uuid4()), "name": name} for name in names]
    tenant.insurances = list(names)
    tenant.insurance_accepted_by = {str(ana.id): {p["id"] for p in tenant.insurance_plans}}
    walk = _Walk(tenant, [ana])
    await walk.to_unit_list(ana)
    seen: list[str] = []
    for _ in range(4):
        rows = _list(walk.last).rows
        assert len(rows) <= 10
        assert rows[-1][0].startswith(fr.ROW_DAY_BACK_PREFIX)
        seen += [row[0] for row in rows if row[0] in {f"ins|{n}" for n in names}]
        more = [row for row in rows if row[0].startswith(fr.ROW_INSURANCE_MORE_PREFIX)]
        if not more:
            break
        await walk.tap(more[0][0])
    assert sorted(set(seen)) == sorted(f"ins|{n}" for n in names)


async def test_back_from_day_goes_to_insurance_when_asked():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    walk = await _to_insurance_list(_tenant(doctors=[ana]), [ana], ana, sul)
    await walk.tap("ins|Unimed")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    back = _list(walk.last).rows[-1]
    assert back == (
        f"{fr.ROW_DAY_BACK_PREFIX}{fr.BACK_TARGET_INSURANCE}",
        fr.LABEL_ANOTHER_INSURANCE,
    )
    assert len(back[1]) <= 24 and len(_list(walk.last).rows) <= 10
    await walk.tap(back[0])
    # The convenio list again, the unit kept; Unimed is not kept behind the question.
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
    assert walk.last.flow_selected_unit_id == sul.id
    assert walk.last.flow_selected_insurance is None
    # ...and the slot picker of the chosen day goes back to the convenio too.
    await walk.tap("ins|Bradesco")
    await walk.tap(_list(walk.last).rows[0][0])
    assert walk.last.flow_step == fr.STEP_AWAITING_SLOT
    assert _list(walk.last).rows[-1][0] == f"{fr.ROW_DAY_BACK_PREFIX}{fr.BACK_TARGET_INSURANCE}"

    # No convenio asked (the clinic does not collect it): the back row is the unit's, as before.
    plain = _Walk(_tenant(collect=False, doctors=[ana]), [ana])
    await plain.to_unit_list(ana)
    await plain.tap(f"unit|{sul.id}")
    assert plain.last.flow_step == fr.STEP_AWAITING_DAY
    assert _list(plain.last).rows[-1][1] == fu.LABEL_ANOTHER_UNIT

    # Nothing accepted at either unit (automatic Particular): no question to go back to.
    refused = _matrix(
        "shared", ana,
        *((ECO_ID, unit, plan) for unit in (centro, sul) for plan in (UNIMED, BRADESCO, AMIL)),
    )
    auto = _Walk(_tenant("shared", refused, [ana]), [ana])
    await auto.to_unit_list(ana)
    await auto.tap(f"unit|{sul.id}")
    assert auto.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    assert _list(auto.last).rows[-1][1] == fu.LABEL_ANOTHER_UNIT


# --------------------------------------------------------------------------
# 4. "Alterar dados" (reschedule): the convenio is kept and re-judged
# --------------------------------------------------------------------------


def _edit_unit_change(*refused_plans):
    """Doctor A at Centro with Unimed; Dra. B at Zona Sul. `refused_plans` are refused for the
    consulta at Zona Sul - the appointment moves to Dra. B there."""
    a, b = _edit_doctor("Dr. A", [CENTRO]), _edit_doctor("Dra. B", [SUL])
    snapshot = _shared(*[(SVC_C, SUL, plan) for plan in refused_plans])
    appt = _appt(a)
    draft = ae.EditDraft.from_appointment(appt, EDIT_TZ).with_changes(
        professional_id=str(b.id), unit_id=str(SUL.id)
    )
    return a, b, snapshot, appt, draft


def test_reschedule_keeps_insurance_and_refilters_on_unit_change():
    a, b, snapshot, _appt_row, draft = _edit_unit_change(P_UNIMED)
    result = aef._confirm_result(draft, snapshot, [a, b], _ctx([a, b]))

    # The new place refuses Unimed, Amil is accepted: the filtered list, one line on top.
    assert result.flow_step == fr.STEP_EDIT_INSURANCE
    [bubble] = [x for x in result.bubbles if isinstance(x, SlotsBubble)]
    assert bubble.body.startswith(fr.INSURANCE_REFUSED_LIST_UNIT)
    assert [row[0] for row in bubble.rows] == ["ins|Amil", "ins|particular", "ins|outro"]
    # The doctor and unit the patient chose stay; the convenio is the open question.
    kept = _draft(result)
    assert (kept.current["professional_id"], kept.current["unit_id"]) == (
        str(b.id),
        str(SUL.id),
    )

    # Nothing accepted there: Particular, saying so - never a silent swap, never a dead end.
    a, b, snapshot, _appt_row, draft = _edit_unit_change(P_UNIMED, P_AMIL)
    result = aef._confirm_result(draft, snapshot, [a, b], _ctx([a, b]))
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(result).current["insurance"] == fr.LABEL_INSURANCE_PARTICULAR
    assert fr.INSURANCE_REFUSED_PARTICULAR_UNIT in _texts(result)[0]

    # The convenio that stays accepted at the new place is kept, silently (nothing to say).
    a, b, snapshot, _appt_row, draft = _edit_unit_change()
    result = aef._confirm_result(draft, snapshot, [a, b], _ctx([a, b]))
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(result).current["insurance"] == "Unimed"


async def test_reschedule_service_change_into_a_refused_cell_refilters_the_convenio():
    """The patient changes the SERVICE; the unit stays and the convenio is judged there."""
    a = _edit_doctor("Dr. A", [CENTRO, SUL])
    appt = _appt(a)
    snapshot = _shared((SVC_R, CENTRO, P_UNIMED))
    result = await _step(snapshot, fr.STEP_EDIT_SERVICE, "Retorno", appt=appt, doctors=[a])
    assert result.flow_step == fr.STEP_EDIT_INSURANCE
    assert _draft(result).current["service"] == "Retorno"
    [bubble] = [x for x in result.bubbles if isinstance(x, SlotsBubble)]
    assert "ins|Unimed" not in [row[0] for row in bubble.rows]
    assert "ins|Amil" in [row[0] for row in bubble.rows]

    # The patient answers the filtered list: the card, with the chosen plan.
    done = await _step(
        snapshot, fr.STEP_EDIT_INSURANCE, "Amil", appt=appt, doctors=[a], draft=_draft(result)
    )
    assert done.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(done).current["insurance"] == "Amil"
    assert _draft(done).current["service"] == "Retorno"


async def test_reschedule_unit_lists_are_not_narrowed_by_the_convenio():
    """The new doctor's units are ALL offered (the convenio is judged after the pick), and a
    unit that refuses the convenio is judged by the same re-ask."""
    a = _edit_doctor("Dr. A", [CENTRO])
    b = _edit_doctor("Dra. B", [SUL, NORTE])
    snapshot = _shared((SVC_C, NORTE, P_UNIMED))
    appt = _appt(a)
    listed = await _step(
        snapshot,
        fr.STEP_EDIT_DOCTOR,
        f"{fr._professional_row_title(b.name)} ({b.id})",
        appt=appt,
        doctors=[a, b],
    )
    assert listed.flow_step == fr.STEP_EDIT_UNIT
    assert [row[0] for row in listed.bubbles[0].rows] == [f"unit|{SUL.id}", f"unit|{NORTE.id}"]

    # The patient picks the one that refuses Unimed: services there, then the convenio is re-asked.
    services = await _step(
        snapshot, fr.STEP_EDIT_UNIT, "Norte", appt=appt, doctors=[a, b], draft=_draft(listed)
    )
    picked = await _step(
        snapshot,
        fr.STEP_EDIT_SERVICE,
        "Consulta",
        appt=appt,
        doctors=[a, b],
        draft=_draft(services),
    )
    assert picked.flow_step == fr.STEP_EDIT_INSURANCE
    assert _draft(picked).current["unit_id"] == str(NORTE.id)


# --------------------------------------------------------------------------
# 5. Chats parked in the old order on deploy day
# --------------------------------------------------------------------------


async def test_parked_awaiting_insurance_with_empty_service_continues():
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    bruno = _doctor("Dr. Bruno", units=[centro])
    doctors = [ana, bruno]
    walk = _Walk(
        _tenant(doctors=doctors),
        doctors,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_INSURANCE,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    await walk.say("Unimed")
    assert walk.last.action == "reply"
    # The old first answer is kept and the booking goes service-first.
    assert walk.last.flow_step == fr.STEP_AWAITING_CATALOG_SERVICE
    assert walk.last.flow_selected_insurance == "Unimed"

    # A single-doctor clinic: his service list.
    solo = _doctor(units=[centro])
    walk = _Walk(
        _tenant(doctors=[solo]),
        [solo],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_INSURANCE,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    await walk.say("Particular")
    assert walk.last.flow_step == fr.STEP_AWAITING_SERVICE


async def test_parked_awaiting_insurance_on_a_clinic_with_no_services_never_books_typeless():
    """Several doctors and no service anywhere: the "sem serviços" answer and the menu - never a
    day picker with no doctor and no service."""
    centro, _sul = _units()
    ana = _doctor(units=[centro], services=[])
    bruno = _doctor("Dr. Bruno", units=[centro], services=[])
    doctors = [ana, bruno]
    tenant = _tenant(doctors=doctors)
    tenant.appointment_types = []
    walk = _Walk(
        tenant,
        doctors,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_INSURANCE,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    await walk.say("Unimed")
    assert walk.last.flow_step != fr.STEP_AWAITING_DAY
    assert walk.last.flow_state is FlowState.MENU


async def test_parked_awaiting_service_confirm_continues():
    """The card is still there (Task 8 removes it): "Sim" on an old card goes on - the unit,
    then the convenio, then the days - without error."""
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    walk = _Walk(
        _tenant(doctors=[ana]),
        [ana],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Consulta",
        flow_selected_professional_id=ana.id,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    await walk.say(fr.LABEL_BOOK_SERVICE)
    assert walk.last.action == "reply"
    assert walk.last.flow_step == fu.STEP_AWAITING_UNIT
    await walk.tap(f"unit|{sul.id}")
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE

    # The same card on a clinic with one unit and no convenio: straight to the days.
    solo = _doctor(units=[centro])
    walk = _Walk(
        _tenant(collect=False, doctors=[solo]),
        [solo],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Consulta",
        flow_selected_professional_id=solo.id,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    await walk.say(fr.LABEL_BOOK_SERVICE)
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY


async def test_parked_on_the_old_doctor_list_keeps_working():
    """`awaiting_professional` with no service (the old doctor-first list): the tap opens that
    doctor's services, as before."""
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    bruno = _doctor("Dr. Bruno", units=[centro])
    walk = _Walk(
        _tenant(doctors=[ana, bruno]),
        [ana, bruno],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_PROFESSIONAL,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    await walk.say(f"{ana.name} ({ana.id})")
    assert walk.last.flow_step == fr.STEP_AWAITING_SERVICE
    assert walk.last.flow_selected_professional_id == ana.id


# --------------------------------------------------------------------------
# 6. The other places that drew the list: nothing accepted is never a list of one
# --------------------------------------------------------------------------


async def test_reopened_insurance_with_nothing_accepted_goes_particular():
    """A chat resumed on `awaiting_insurance` (or a re-ask) whose combination now accepts
    nothing: Particular, saying so - not a list with only Particular on it."""
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    refused = _matrix(
        "shared", ana, (ECO_ID, centro, UNIMED), (ECO_ID, centro, BRADESCO), (ECO_ID, centro, AMIL)
    )
    walk = _Walk(
        _tenant("shared", refused, [ana]),
        [ana],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_INSURANCE,
        flow_selected_type="Ecocardiograma",
        flow_selected_professional_id=ana.id,
        flow_selected_unit_id=centro.id,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    resumed = await fr.resume_bubbles(walk.conversation(), walk.tenant, walk.agenda, [ana])
    assert resumed.flow_step == fr.STEP_AWAITING_DAY
    assert resumed.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    # An empty answer (a media message) re-asks through the same decision.
    again = await walk.say("")
    assert again.flow_step == fr.STEP_AWAITING_DAY
    assert again.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR


async def test_no_plan_name_ever_reaches_a_log_in_the_new_paths():
    """R21: counts and booleans only - the draft path is the new caller of the gate."""
    from tests.before_support import recorded_logs

    sole = _with_units(_sole(), _unit("Centro"))
    tenant = _two_plans(_refuse((str(CONSULTA), None, PLAN)))
    with recorded_logs() as logs:
        await _resolve(
            BookingDraft(service="Consulta", insurance="unimed", day=DAY),
            tenant=tenant,
            pros=[sole],
        )
    assert "Unimed" not in repr(logs) and "Bradesco" not in repr(logs)


# --------------------------------------------------------------------------
# 7. The final guard and the "nao atende este convenio" card: the same decision, no list of one
# --------------------------------------------------------------------------


async def test_the_final_guard_with_nothing_accepted_goes_particular_and_asks_the_day():
    from tests import test_t086_flow_matrix as m

    centro, sul, _norte = m._units()
    ana = m._doctor(units=[centro, sul])
    doctors = [ana]
    # The clinic stopped taking every plan at both units while the patient booked.
    refused = m._matrix(
        "shared", ana,
        *((m.ECO_ID, unit, plan) for unit in (centro, sul) for plan in (m.UNIMED, m.BRADESCO)),
    )
    tenant = m._tenant("shared", refused, doctors)
    result = await fr.route(
        m._confirming(sul, ana),
        tenant,
        m._Agenda(),
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=doctors,
        gate=m._Gate(armed=False),
    )
    assert result.appointment is None
    # Not a list that holds only Particular: Particular, with the line, and the days again.
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    assert result.flow_selected_unit_id == sul.id
    assert fr.INSURANCE_REFUSED_PARTICULAR_UNIT in _list(result).body


async def test_the_not_accepted_card_change_insurance_with_nothing_accepted_goes_particular():
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    refused = _matrix(
        "shared", ana, (ECO_ID, centro, UNIMED), (ECO_ID, centro, BRADESCO), (ECO_ID, centro, AMIL)
    )
    walk = _Walk(
        _tenant("shared", refused, [ana]),
        [ana],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_PLAN_REFUSED,
        flow_selected_type="Ecocardiograma",
        flow_selected_professional_id=ana.id,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    await walk.say(fr.LABEL_PLAN_CHANGE_INSURANCE)
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR



# --------------------------------------------------------------------------
# 8. Fix round 1 (Opus review): stored Particular, legacy landing (switch OFF), "Outro serviço"
# --------------------------------------------------------------------------


async def test_q1_handback_to_another_service_drops_a_stored_particular():
    """R29: Particular set automatically for the echo (nothing accepted) is NOT carried into the
    consulta (everything accepted) by a hand-back - the question is asked."""
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    refused = _matrix(
        "shared", ana, (ECO_ID, centro, UNIMED), (ECO_ID, centro, BRADESCO), (ECO_ID, centro, AMIL)
    )
    tenant = _tenant("shared", refused, [ana])
    walk = _Walk(tenant, [ana])
    await walk.to_service_card("Ecocardiograma", doctor=ana)
    assert walk.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR  # automatic
    stored = walk.last.flow_selected_insurance

    other = await fr.enter_guided_booking(
        tenant,
        _Agenda(),
        "Consulta",
        insurance=stored,
        professionals=[ana],
        recorded=SimpleNamespace(
            flow_insurance_auto=True,
            flow_selected_type="Ecocardiograma",
            flow_selected_professional_id=ana.id,
            flow_selected_unit_id=centro.id,
        ),
    )
    assert other.flow_step == fr.STEP_AWAITING_INSURANCE
    assert other.flow_selected_insurance is None

    # The SAME service (nothing changed): the stored answer stays, no repeated question.
    same = await fr.enter_guided_booking(
        tenant,
        _Agenda(),
        "Ecocardiograma",
        insurance=stored,
        professionals=[ana],
        recorded=SimpleNamespace(
            flow_insurance_auto=True,
            flow_selected_type="Ecocardiograma",
            flow_selected_professional_id=ana.id,
            flow_selected_unit_id=centro.id,
        ),
    )
    assert same.flow_step == fr.STEP_AWAITING_DAY
    assert same.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    # A chosen PLAN is kept and judged, never dropped.
    plan = await fr.enter_guided_booking(
        tenant,
        _Agenda(),
        "Consulta",
        insurance="Unimed",
        professionals=[ana],
        recorded=SimpleNamespace(
            flow_selected_type="Ecocardiograma",
            flow_selected_professional_id=ana.id,
            flow_selected_unit_id=centro.id,
        ),
    )
    assert plan.flow_step == fr.STEP_AWAITING_DAY
    assert plan.flow_selected_insurance == "Unimed"


async def test_q2_resolver_drops_a_stored_particular_for_another_service():
    centro = _unit("Centro")
    sole = _with_units(_sole(), centro)
    refused = _refuse(
        (str(RETORNO), str(centro.id), PLAN), (str(RETORNO), str(centro.id), PLAN_B)
    )
    tenant = _two_plans(refused)
    conv = _conv(
        flow_state=FlowState.LLM,
        flow_selected_type="Retorno",
        flow_selected_unit_id=centro.id,
        flow_insurance_auto=True,
        flow_selected_insurance=fr.LABEL_INSURANCE_PARTICULAR,  # the automatic one of Retorno
    )
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=TEN), conv=conv, tenant=tenant, pros=[sole]
    )
    # Not the express card with Particular: the combination accepts plans, so it asks.
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    assert res.result.flow_selected_insurance is None

    # Same service: the stored Particular stays (the patient's own, or still automatic there).
    same = await _resolve(
        BookingDraft(service="Retorno", day=DAY, time=TEN), conv=conv, tenant=tenant, pros=[sole]
    )
    assert same.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert same.result.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR


async def _legacy_clinic(wired):
    tenant = await _seed_tenant(
        wired,
        doctors=[("Dra. Ana", ["Primeira Consulta"]), ("Dr. Beto", ["Primeira Consulta"])],
    )
    await _switch_off(wired, tenant)
    await _onboard(tenant)
    conv = await _conversation(wired, tenant)
    async with wired() as session:
        row = await session.get(Conversation, conv.id)
        row.flow_attendee_name = ""  # pra-quem answered: "para mim"
        await session.commit()
    return tenant


async def test_legacy_landing_multi_doctor_selection_only_opens_the_service_catalog(wired):
    tenant = await _legacy_clinic(wired)
    await _hand_back(wired, tenant, {"i": None})
    current = await _conversation(wired, tenant)
    # The service first - never the doctor list with "Aceita seu convênio" marks.
    assert current.flow_step == fr.STEP_AWAITING_CATALOG_SERVICE


async def test_legacy_landing_multi_doctor_service_without_doctor_asks_the_doctors_of_it(wired):
    tenant = await _legacy_clinic(wired)
    await _hand_back(wired, tenant, {"t": "Primeira Consulta"})
    current = await _conversation(wired, tenant)
    # Not the menu: the doctors of THAT service, the service kept.
    assert current.flow_step == fr.STEP_AWAITING_SERVICE_PROFESSIONAL
    assert current.flow_selected_type == "Primeira Consulta"


async def test_a_multi_doctor_outro_servico_reopens_the_clinic_catalog():
    """R18: the service was the first choice, so its back row is the catalog; the doctor and the
    service are cleared and the automatic Particular is dropped. Reached from the convenio list
    and from the day picker alike."""
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    bruno = _doctor("Dr. Bruno", units=[centro])
    doctors = [ana, bruno]
    walk = _Walk(_tenant(doctors=doctors), doctors)
    await walk.to_service_card("Ecocardiograma", doctor=ana)
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
    back = _list(walk.last).rows[-1]
    assert back == (f"{fr.ROW_DAY_BACK_PREFIX}{fr.BACK_TARGET_SERVICE}", fr.LABEL_ANOTHER_SERVICE)
    await walk.tap(back[0])
    assert walk.last.flow_step == fr.STEP_AWAITING_CATALOG_SERVICE
    assert walk.last.flow_selected_professional_id is None
    assert walk.last.flow_selected_type is None

    # From the day picker: its back row is the convenio list, whose back row is the catalog.
    await walk.tap("svc|Ecocardiograma")
    await walk.tap(f"prof|{ana.id}")
    await walk.tap("ins|Unimed")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    await walk.tap(_list(walk.last).rows[-1][0])
    await walk.tap(_list(walk.last).rows[-1][0])
    assert walk.last.flow_step == fr.STEP_AWAITING_CATALOG_SERVICE

    # Nothing accepted for the first service (automatic Particular): dropped on the way back.
    refused = _matrix(
        "shared", ana, (ECO_ID, centro, UNIMED), (ECO_ID, centro, BRADESCO), (ECO_ID, centro, AMIL)
    )
    auto = _Walk(_tenant("shared", refused, doctors), doctors)
    await auto.to_service_card("Ecocardiograma", doctor=ana)
    assert auto.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    await auto.tap(_list(auto.last).rows[-1][0])
    assert auto.last.flow_step == fr.STEP_AWAITING_CATALOG_SERVICE
    assert auto.last.flow_selected_insurance is None


async def test_rebooking_outro_servico_keeps_the_candidates_own_service_list():
    """The rebooking after a doctor's cancellation keeps its OWN destination (candidate's list)."""
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    bruno = _doctor("Dr. Bruno", units=[centro])
    doctors = [ana, bruno]
    tenant = _tenant(doctors=doctors)
    day = await fr.enter_rebooking(
        _Walk(tenant, doctors).conversation(),
        tenant,
        _Agenda(),
        professionals=doctors,
        cancelled_professional_id=ana.id,
        service_name="Consulta",
        same_professional=False,
        candidates=[bruno],
    )
    back = _list(day).rows[-1]
    assert back[0] == f"{fr.ROW_DAY_BACK_PREFIX}{fr.BACK_TARGET_DOCTOR_SERVICES}"
    assert back[1] == fr.LABEL_ANOTHER_SERVICE
    walk = _Walk(
        tenant,
        doctors,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_DAY,
        flow_selected_type="Consulta",
        flow_selected_professional_id=bruno.id,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    walk.last = day
    await walk.tap(back[0])
    assert walk.last.flow_step == fr.STEP_AWAITING_SERVICE
    assert walk.last.flow_selected_professional_id == bruno.id


async def test_a_stale_convenio_back_row_never_draws_a_list_of_only_particular():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    refused_now = _matrix(
        "shared", ana, (ECO_ID, sul, UNIMED), (ECO_ID, sul, BRADESCO), (ECO_ID, sul, AMIL)
    )
    walk = _Walk(
        _tenant("shared", refused_now, [ana]),
        [ana],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_DAY,
        flow_selected_type="Ecocardiograma",
        flow_selected_professional_id=ana.id,
        flow_selected_unit_id=sul.id,
        flow_selected_insurance="Unimed",
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    # An old card whose "Trocar convênio" row is tapped after the matrix changed.
    await walk.say(f"{fr.LABEL_ANOTHER_INSURANCE} ({fr.BACK_TARGET_INSURANCE})")
    assert walk.last.flow_step == fu.STEP_AWAITING_UNIT  # one step further back
    assert walk.last.flow_step != fr.STEP_AWAITING_INSURANCE


async def test_a_parked_draft_resumes_on_the_unit_pick_and_waits_on_the_convenio_list():
    """M7: the unit pick resumes the parked draft when the days open next, and does NOT when the
    convenio list opens (the draft stays parked on it)."""
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    parked = {"t": "Ecocardiograma", "d": "2026-10-08", "h": "10:00"}

    async def pick(tenant):
        walk = _Walk(
            tenant,
            [ana],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=fu.STEP_AWAITING_UNIT,
            flow_selected_type="Ecocardiograma",
            flow_selected_professional_id=ana.id,
            flow_attendee_name=fr.ATTENDEE_SELF,
            flow_draft=parked,
        )
        walk.last = fr._enter_unit_list(walk.conversation(), ana, units=[centro, sul])
        await walk.tap(f"unit|{sul.id}")
        return walk.last

    no_convenio = await pick(_tenant(collect=False, doctors=[ana]))
    assert no_convenio.flow_step == fr.STEP_AWAITING_DAY
    assert no_convenio.resume_draft is True
    assert no_convenio.flow_selected_unit_id == sul.id

    collecting = await pick(_tenant(doctors=[ana]))
    assert collecting.flow_step == fr.STEP_AWAITING_INSURANCE
    assert collecting.resume_draft is False
    assert collecting.flow_draft == parked  # still parked, on the convenio question



# --------------------------------------------------------------------------
# 9. Fix round 2 (R30): ONE helper decides whether a stored Particular survives a landing
# --------------------------------------------------------------------------


def _doc(name, *units):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=[dict(entry) for entry in SERVICES],
        business_hours={"thursday": [{"start": "08:00", "end": "12:00"}]},
        units=list(units),
        service_units={},
    )


def test_automatic_particular_survives_only_for_the_same_triple_or_when_named_now():
    centro, sul = _unit("Centro"), _unit("Sul")
    ana, bruno = _doc("Dra. Ana", centro), _doc("Dr. Bruno", sul)
    tenant = _two_plans()
    recorded = _conv(
        flow_selected_type="Consulta",
        flow_selected_professional_id=ana.id,
        flow_selected_unit_id=centro.id,
        flow_insurance_auto=True,
    )
    particular = fr.LABEL_INSURANCE_PARTICULAR

    def survives(**kw):
        base = dict(
            recorded=recorded,
            professionals=[ana, bruno],
            landing_type="Consulta",
            landing_owner=ana,
            landing_unit=centro,
        )
        base.update(kw)
        return fr.particular_survives(tenant, particular, **base)

    assert survives()  # (service, doctor, unit) all the recorded ones
    assert not survives(landing_type="Retorno")
    assert not survives(landing_owner=bruno, landing_unit=sul)
    assert not survives(landing_unit=sul)
    assert not survives(opens_choice=True)  # a list opens: the combination is chosen anew
    assert survives(recorded=None)
    assert survives(named_now=True, landing_type="Retorno")  # the patient said it this turn
    # Not a stored Particular: nothing to decide.
    assert fr.particular_survives(tenant, "Unimed", opens_choice=True)
    assert fr.particular_survives(tenant, None, opens_choice=True)
    plain = _two_plans(collect_insurance=False, insurances=[], insurance_plans=[])
    assert fr.particular_survives(plain, particular, opens_choice=True)


async def test_p1_an_ai_draft_that_changes_doctor_and_unit_drops_a_stored_particular():
    centro, sul = _unit("Centro"), _unit("Sul")
    ana, bruno = _doc("Dra. Ana", centro), _doc("Dr. Bruno", sul)
    refused = _refuse(
        (str(CONSULTA), str(centro.id), PLAN), (str(CONSULTA), str(centro.id), PLAN_B)
    )
    tenant = _two_plans(refused)
    conv = _conv(
        flow_state=FlowState.LLM,
        flow_selected_type="Consulta",
        flow_selected_professional_id=ana.id,
        flow_selected_unit_id=centro.id,
        flow_insurance_auto=True,
        flow_selected_insurance=fr.LABEL_INSURANCE_PARTICULAR,  # automatic: Centro accepts nothing
    )
    moved = await _resolve(
        BookingDraft(professional_id=bruno.id, day=DAY, time=TEN),
        conv=conv,
        tenant=tenant,
        pros=[ana, bruno],
    )
    # Sul accepts both plans: asked, not the express card with a carried Particular.
    assert moved.landing_step == fr.STEP_AWAITING_INSURANCE
    assert moved.result.flow_selected_insurance is None

    # The same doctor and unit: the stored answer stays.
    same = await _resolve(
        BookingDraft(professional_id=ana.id, day=DAY, time=TEN),
        conv=conv,
        tenant=tenant,
        pros=[ana, bruno],
    )
    assert same.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert same.result.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR


async def test_p2_the_patients_own_particular_tap_resumes_without_asking_again():
    centro, sul = _unit("Centro"), _unit("Sul")
    sole = _doc("Dra. Única", centro, sul)
    conv = _conv(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_INSURANCE,
        flow_selected_type="Consulta",
        flow_selected_unit_id=sul.id,
        flow_selected_insurance=fr.LABEL_INSURANCE_PARTICULAR,
    )
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=TEN),
        conv=conv,
        tenant=_two_plans(),
        pros=[sole],
    )
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert res.result.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR


async def test_the_hand_back_compares_doctor_and_unit_too():
    centro, sul = _units()
    ana = _doctor(units=[centro])
    refused = _matrix(
        "shared", ana, (CONSULTA_ID, centro, UNIMED), (CONSULTA_ID, centro, BRADESCO),
        (CONSULTA_ID, centro, AMIL),
    )
    tenant = _tenant("shared", refused, [ana])
    recorded = SimpleNamespace(
        flow_insurance_auto=True,
        flow_selected_type="Consulta",
        flow_selected_professional_id=ana.id,
        flow_selected_unit_id=centro.id,
    )
    kept = await fr.enter_guided_booking(
        tenant,
        _Agenda(),
        "Consulta",
        insurance=fr.LABEL_INSURANCE_PARTICULAR,
        professionals=[ana],
        recorded=recorded,
    )
    assert kept.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    # The conversation was at another unit: not the recorded triple any more.
    elsewhere = SimpleNamespace(
        flow_insurance_auto=True,
        flow_selected_type="Consulta",
        flow_selected_professional_id=ana.id,
        flow_selected_unit_id=sul.id,
    )
    ana.units = [centro, sul]
    moved = await fr.enter_guided_booking(
        tenant,
        _Agenda(),
        "Consulta",
        insurance=fr.LABEL_INSURANCE_PARTICULAR,
        professionals=[ana],
        recorded=elsewhere,
    )
    assert moved.flow_selected_insurance is None


async def _legacy_with_stored_particular(wired):
    tenant = await _seed_tenant(
        wired,
        collect=True,
        doctors=[
            ("Dra. Ana", ["Primeira Consulta", "Retorno"]),
            ("Dr. Beto", ["Primeira Consulta", "Retorno"]),
        ],
    )
    await _switch_off(wired, tenant)
    await _onboard(tenant)
    conv = await _conversation(wired, tenant)
    async with wired() as session:
        row = await session.get(Conversation, conv.id)
        row.flow_attendee_name = ""
        row.flow_state = FlowState.LLM
        row.flow_step = None
        row.flow_selected_type = "Retorno"
        row.flow_selected_insurance = fr.LABEL_INSURANCE_PARTICULAR
        row.flow_insurance_auto = True
        await session.commit()
    return tenant


async def test_p4_legacy_service_without_doctor_drops_a_stored_particular(wired):
    tenant = await _legacy_with_stored_particular(wired)
    await _hand_back(wired, tenant, {"t": "Primeira Consulta"})
    landed = await _conversation(wired, tenant)
    assert landed.flow_step == fr.STEP_AWAITING_SERVICE_PROFESSIONAL
    assert landed.flow_selected_insurance is None
    await _wa_turn(tenant, "Dra. Ana")
    # TASK-088 Task 8: the doctor tap goes straight on. The question is asked - the old
    # automatic answer did not ride along.
    assert (await _conversation(wired, tenant)).flow_step == fr.STEP_AWAITING_INSURANCE


async def test_legacy_selection_only_drops_a_stored_particular(wired):
    tenant = await _legacy_with_stored_particular(wired)
    await _hand_back(wired, tenant, {"i": None})
    catalog = await _conversation(wired, tenant)
    assert catalog.flow_step in (
        fr.STEP_AWAITING_CATALOG_SERVICE,  # the clinic catalog (the stored service is not in it)
        fr.STEP_AWAITING_SERVICE_PROFESSIONAL,  # or the doctors of the stored service
    )
    assert catalog.flow_selected_insurance is None


async def test_legacy_doctor_only_drops_a_stored_particular(wired):
    tenant = await _legacy_with_stored_particular(wired)
    doctor = await _professional(wired, tenant, "Dra. Ana")
    await _hand_back(wired, tenant, {"p": str(doctor.id)})
    landed = await _conversation(wired, tenant)
    assert landed.flow_step == fr.STEP_AWAITING_SERVICE
    assert landed.flow_selected_insurance is None


# --------------------------------------------------------------------------
# 10. Fix round 3: a Particular the patient names THIS turn is never dropped (R30 a)
# --------------------------------------------------------------------------


async def _legacy_named(wired, doctors, payload_fn, *, stored_type=None):
    tenant = await _seed_tenant(wired, collect=True, doctors=doctors)
    await _switch_off(wired, tenant)
    await _onboard(tenant)
    conv = await _conversation(wired, tenant)
    async with wired() as session:
        row = await session.get(Conversation, conv.id)
        row.flow_attendee_name = ""
        row.flow_state = FlowState.LLM
        row.flow_step = None
        row.flow_selected_type = stored_type
        await session.commit()
    await _hand_back(wired, tenant, await payload_fn(tenant))
    return await _conversation(wired, tenant)


_SOLE_CLINIC = [("Dra. Ana", ["Primeira Consulta", "Retorno"])]
_MULTI_CLINIC = [
    ("Dra. Ana", ["Primeira Consulta", "Retorno"]),
    ("Dr. Beto", ["Primeira Consulta"]),
]


async def test_n1_legacy_particular_named_now_on_a_fresh_conversation_is_kept(wired):
    async def payload(tenant):
        return {"t": "Primeira Consulta", "i": "particular"}

    landed = await _legacy_named(wired, _SOLE_CLINIC, payload)
    assert landed.flow_step == fr.STEP_AWAITING_DAY  # not "Você vai usar convênio?"
    assert landed.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR


async def test_n2_legacy_particular_named_now_with_another_service_recorded_is_kept(wired):
    async def payload(tenant):
        return {"t": "Primeira Consulta", "i": "particular"}

    landed = await _legacy_named(wired, _SOLE_CLINIC, payload, stored_type="Retorno")
    assert landed.flow_step == fr.STEP_AWAITING_DAY
    assert landed.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR


async def test_n3_legacy_plan_named_now_is_kept_and_judged(wired):
    async def payload(tenant):
        return {"t": "Primeira Consulta", "i": "Unimed"}

    landed = await _legacy_named(wired, _SOLE_CLINIC, payload)
    assert landed.flow_step == fr.STEP_AWAITING_DAY
    assert landed.flow_selected_insurance == "Unimed"


async def test_n4_legacy_multi_doctor_particular_named_now_is_kept(wired):
    async def payload(tenant):
        doctor = await _professional(wired, tenant, "Dra. Ana")
        return {"t": "Primeira Consulta", "p": str(doctor.id), "i": "particular"}

    landed = await _legacy_named(wired, _MULTI_CLINIC, payload)
    assert landed.flow_step == fr.STEP_AWAITING_DAY
    assert landed.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR


async def test_a_hand_back_named_now_and_a_clinic_without_a_catalog_keep_particular():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    refused = _matrix(
        "shared", ana, (CONSULTA_ID, sul, UNIMED), (CONSULTA_ID, sul, BRADESCO),
        (CONSULTA_ID, sul, AMIL),
    )
    tenant = _tenant("shared", refused, [ana])
    elsewhere = SimpleNamespace(
        flow_insurance_auto=True,
        flow_selected_type="Ecocardiograma",
        flow_selected_professional_id=ana.id,
        flow_selected_unit_id=centro.id,
    )
    # The unit is still to be chosen (a list), the recorded triple is another one - but the patient
    # said "particular" now: kept.
    named = await fr.enter_guided_booking(
        tenant,
        _Agenda(),
        "Consulta",
        insurance=fr.LABEL_INSURANCE_PARTICULAR,
        professionals=[ana],
        recorded=elsewhere,
        named_now=True,
    )
    assert named.flow_step == fu.STEP_AWAITING_UNIT
    assert named.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    # Stored (not named now): dropped, as R30 wants.
    stored = await fr.enter_guided_booking(
        tenant,
        _Agenda(),
        "Consulta",
        insurance=fr.LABEL_INSURANCE_PARTICULAR,
        professionals=[ana],
        recorded=elsewhere,
    )
    assert stored.flow_selected_insurance is None
    # No service at all (a clinic without a catalog): no Particular there can be the automatic
    # one - nothing to re-ask.
    solo = _doctor(units=[])
    typeless = await fr.enter_guided_booking(
        _tenant(doctors=[solo]),
        _Agenda(),
        None,
        insurance=fr.LABEL_INSURANCE_PARTICULAR,
        professionals=[solo],
        recorded=elsewhere,
    )
    assert typeless.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
