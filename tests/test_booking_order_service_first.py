"""TASK-088 Task 6 - the booking order: pra-quem -> servico -> medico -> unidade -> convenio -> dia.

Router level, no DB, no network (same style as tests/test_t086_flow_matrix.py): SimpleNamespace
snapshots, a fake agenda and a fake booking gate, with `route()` driven one tap at a time and the
conversation advanced the way `_apply_flow_result` leaves it.

The owner (2026-11-20): the patient solves HIS problem first - a service; who does it and where
come next; the convenio is the last question, because it only means something once service,
doctor and unit are known, and the list shows ONLY the plans accepted for that combination.
Nothing accepted -> Particular straight away, with one short line, never a list.
"""

import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

os.environ.setdefault("APP_ENV", "test")

import pytest  # noqa: E402

from secretaria.ai.formatter import SlotsBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402
from secretaria.services import flow_router as fr, flow_units as fu, list_options  # noqa: E402
from secretaria.services.attendee import LABEL_ATTENDEE_SELF  # noqa: E402
from secretaria.services.booking_hold import GateDecision  # noqa: E402
from secretaria.services.insurance_acceptance import AcceptanceExceptions  # noqa: E402
from secretaria.services.unit_booking import UnitOption  # noqa: E402
from tests.before_support import recorded_logs  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
CLINIC_HOURS = {d: [{"start": "08:00", "end": "18:00"}] for d in ("monday", "tuesday")}
CENTRO_HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}
SUL_HOURS = {"tuesday": [{"start": "14:00", "end": "18:00"}]}

CONSULTA_ID = uuid4()
ECO_ID = uuid4()
UNIMED = str(uuid4())
BRADESCO = str(uuid4())
AMIL = str(uuid4())
MODES = ("shared", "independent")
SEEN_PLANS = ("Unimed", "Bradesco", "Amil")


def _services():
    return [
        {
            "name": "Consulta",
            "duration_min": 30,
            "is_active": True,
            "sort_order": 0,
            "service_id": str(CONSULTA_ID),
        },
        {
            "name": "Ecocardiograma",
            "duration_min": 30,
            "is_active": True,
            "sort_order": 1,
            "service_id": str(ECO_ID),
        },
    ]


def _units():
    return (
        UnitOption(id=uuid4(), name="Centro", address="Rua A, 10", business_hours=CENTRO_HOURS),
        UnitOption(id=uuid4(), name="Zona Sul", address="Av. B, 20", business_hours=SUL_HOURS),
    )


def _doctor(name="Dra. Ana", units=(), services=None):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty="Cardiologia",
        about=None,
        appointment_types=services if services is not None else _services(),
        business_hours=None,
        min_booking_notice_hours=None,
        units=list(units),
        service_units={},
    )


def _key(service_id, unit, plan):
    return (str(service_id), None if unit is None else str(unit.id), plan)


def _matrix(mode, doctor, *cells):
    """`cells` refused for `doctor`: the clinic matrix (shared) or that doctor's (independent)."""
    keys = frozenset(_key(*cell) for cell in cells)
    if mode == "shared":
        return AcceptanceExceptions(clinic=keys)
    return AcceptanceExceptions(by_professional={str(doctor.id): keys})


def _tenant(mode="shared", refused=None, doctors=(), *, collect=True, accepted_by=None):
    plans = [
        {"id": UNIMED, "name": "Unimed", "mechanism": None},
        {"id": BRADESCO, "name": "Bradesco", "mechanism": None},
        {"id": AMIL, "name": "Amil", "mechanism": None},
    ]
    everyone = {UNIMED, BRADESCO, AMIL}
    if accepted_by is None:
        accepted_by = {str(d.id): everyone for d in doctors} if mode == "independent" else {}
    return SimpleNamespace(
        initial_flows={"enabled": True},
        appointment_types=_services(),
        appointment_duration_min=30,
        business_hours=CLINIC_HOURS,
        collect_insurance=collect,
        insurance_mode=mode,
        insurances=[plan["name"] for plan in plans] if collect else [],
        insurance_plans=plans if collect else [],
        insurance_accepted_by=accepted_by,
        insurance_refused=refused,
        timezone="America/Sao_Paulo",
    )


class _Agenda:
    def __init__(self, hours=CLINIC_HOURS, *, unit_id=None, log=None):
        self.tzinfo = TZ
        self.hours = hours
        self.unit_id = unit_id
        self.log = log if log is not None else {"days": 0, "created": []}

    def with_unit(self, unit_id, location, *, business_hours=None):
        return _Agenda(
            business_hours if business_hours is not None else self.hours,
            unit_id=unit_id,
            log=self.log,
        )

    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.log["days"] += 1
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(1, 4)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6, **_kw):
        start = day.replace(hour=9, minute=0, tzinfo=None)
        return [{"start": start.isoformat(timespec="minutes"), "label": "09:00"}]

    async def has_conflict(self, start, end, ignore_event_id=None):
        return False

    async def create_event(self, start, end, summary, description=""):
        self.log["created"].append((self.unit_id, summary))
        return {"id": "evt-1", "htmlLink": None}


class _Gate:
    deposit_capable = True
    armed = False

    async def busy_windows(self, professional_id):
        return []

    async def decide(self, **kwargs):
        return GateDecision("commit")


class _Walk:
    """`route()` one turn at a time, the conversation advanced like `_apply_flow_result`."""

    def __init__(self, tenant, doctors, **state):
        self.tenant = tenant
        self.doctors = doctors
        self.agenda = _Agenda()
        self.state = dict(
            flow_state=FlowState.IDLE,
            flow_step=None,
            flow_selected_type=None,
            flow_selected_day=None,
            flow_selected_slot=None,
            flow_selected_professional_id=None,
            flow_selected_insurance=None,
            flow_insurance_auto=None,
            flow_selected_unit_id=None,
            flow_managing_appointment_id=None,
            flow_attendee_name=None,
            flow_draft=None,
        )
        self.state.update(state)
        self.steps: list[str | None] = []
        self.last = None

    def conversation(self):
        return SimpleNamespace(id=uuid4(), patient_id=None, **self.state)

    async def say(self, body, **kw):
        result = await fr.route(
            self.conversation(),
            self.tenant,
            self.agenda,
            body,
            professionals=self.doctors,
            patient_name="Maria",
            **kw,
        )
        self.last = result
        self.steps.append(result.flow_step)
        self.state.update(
            flow_state=result.flow_state,
            flow_step=result.flow_step,
            flow_selected_type=result.flow_selected_type,
            flow_selected_day=result.flow_selected_day,
            flow_selected_slot=result.flow_selected_slot,
            flow_selected_professional_id=result.flow_selected_professional_id,
            flow_selected_insurance=result.flow_selected_insurance,
            flow_insurance_auto=result.flow_insurance_auto,
            flow_selected_unit_id=result.flow_selected_unit_id,
            flow_attendee_name=result.flow_attendee_name,
        )
        return result

    async def tap(self, row_id):
        [bubble] = [b for b in self.last.bubbles if isinstance(b, SlotsBubble)]
        [row] = [r for r in bubble.rows if r[0] == row_id]
        return await self.say(inbound_routing_text(row[1], row[0]))

    async def to_service_card(self, service="Ecocardiograma", doctor=None):
        """Agendar -> pra-quem Sim -> the service -> (the doctor): the step AFTER the service.

        TASK-088 Task 8: there is no service card any more - the tap goes straight on (the unit,
        the convenio or the days). The name is kept so the older walks read as before."""
        await self.say(fr.LABEL_BOOK)
        await self.say(LABEL_ATTENDEE_SELF)
        await self.tap(f"svc|{service}")
        if doctor is not None and self.last.flow_step == fr.STEP_AWAITING_SERVICE_PROFESSIONAL:
            await self.tap(f"prof|{doctor.id}")
        return self.last

    async def to_unit_list(self, doctor):
        await self.to_service_card(doctor=doctor)


def _list(result) -> SlotsBubble:
    [bubble] = [b for b in result.bubbles if isinstance(b, SlotsBubble)]
    return bubble


def _ids(result) -> list[str]:
    return [row[0] for row in _list(result).rows]


def _ins_ids(result) -> list[str]:
    """The convenio list's rows without its back row (TASK-088 R18: it ends with one)."""
    return [row_id for row_id in _ids(result) if not row_id.startswith(fr.ROW_DAY_BACK_PREFIX)]


def _portal_matches(bubble: SlotsBubble, tenant, roster) -> None:
    """The Portal re-resolves the SAME rows from the stored scope (page 1 == the route)."""
    source = bubble.source
    assert list_options.resolve(source.kind, source.scope, tenant, roster) == source.items
    for row_id, *_ in source.items:
        assert list_options.contains(source.kind, source.scope, tenant, roster, row_id)


# --------------------------------------------------------------------------
# 1. The whole journey, in the owner's order
# --------------------------------------------------------------------------


async def test_order_service_then_doctor_then_unit_then_insurance_then_day():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    bruno = _doctor("Dr. Bruno", units=[centro])
    doctors = [ana, bruno]
    walk = _Walk(_tenant(doctors=doctors), doctors)

    await walk.say(fr.LABEL_BOOK)
    assert walk.last.flow_step == fr.STEP_AWAITING_ATTENDEE_CHOICE
    await walk.say(LABEL_ATTENDEE_SELF)
    # The service comes first: the clinic's whole catalog, never the convenio, never a doctor.
    assert walk.last.flow_step == fr.STEP_AWAITING_CATALOG_SERVICE
    assert {"svc|Consulta", "svc|Ecocardiograma"} <= set(_ids(walk.last))
    assert walk.last.flow_selected_insurance is None

    await walk.tap("svc|Ecocardiograma")
    # Two doctors do it: the doctor, still no convenio.
    assert walk.last.flow_step == fr.STEP_AWAITING_SERVICE_PROFESSIONAL
    assert walk.last.flow_selected_type == "Ecocardiograma"
    await walk.tap(f"prof|{ana.id}")
    # TASK-088 Task 8: no service card in between - the doctor tap goes to the unit.
    assert walk.last.flow_step == fu.STEP_AWAITING_UNIT
    assert walk.last.flow_selected_professional_id == ana.id
    await walk.tap(f"unit|{sul.id}")
    # Service, doctor and unit are known: NOW the convenio, and the unit rides along.
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
    assert walk.last.flow_selected_unit_id == sul.id
    assert walk.last.flow_selected_type == "Ecocardiograma"
    assert walk.last.flow_selected_professional_id == ana.id

    await walk.tap("ins|Unimed")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_insurance == "Unimed"
    assert walk.last.flow_selected_unit_id == sul.id
    assert walk.steps == [
        fr.STEP_AWAITING_ATTENDEE_CHOICE,
        fr.STEP_AWAITING_CATALOG_SERVICE,
        fr.STEP_AWAITING_SERVICE_PROFESSIONAL,
        fu.STEP_AWAITING_UNIT,
        fr.STEP_AWAITING_INSURANCE,
        fr.STEP_AWAITING_DAY,
    ]


async def test_single_doctor_single_unit_no_insurance_goes_service_to_day():
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    walk = _Walk(_tenant(collect=False, doctors=[ana]), [ana])

    await walk.say(fr.LABEL_BOOK)
    await walk.say(LABEL_ATTENDEE_SELF)
    # One doctor: his own service list - no doctor step, no convenio.
    assert walk.last.flow_step == fr.STEP_AWAITING_SERVICE
    await walk.tap("svc|Consulta")
    # One unit: implied, nothing to ask - straight to the days (no empty step, no loop).
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_unit_id == centro.id
    assert walk.steps == [
        fr.STEP_AWAITING_ATTENDEE_CHOICE,
        fr.STEP_AWAITING_SERVICE,
        fr.STEP_AWAITING_DAY,
    ]


# --------------------------------------------------------------------------
# 2. The convenio list: only what the combination accepts
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_insurance_list_only_accepted_for_service_doctor_unit(mode):
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    doctors = [ana]
    # Bradesco is refused for the echo at Zona Sul only; Amil for the consulta at Centro only.
    refused = _matrix(mode, ana, (ECO_ID, sul, BRADESCO), (CONSULTA_ID, centro, AMIL))
    tenant = _tenant(mode, refused, doctors)

    at_sul = _Walk(tenant, doctors)
    await at_sul.to_unit_list(ana)
    await at_sul.tap(f"unit|{sul.id}")
    assert at_sul.last.flow_step == fr.STEP_AWAITING_INSURANCE
    assert _ins_ids(at_sul.last) == ["ins|Unimed", "ins|Amil", "ins|particular", "ins|outro"]
    # The patient CHOSE the unit among two: the list's way back is the unit list.
    assert _ids(at_sul.last)[-1] == f"{fr.ROW_DAY_BACK_PREFIX}{fr.BACK_TARGET_UNIT}"
    assert _list(at_sul.last).source.scope == {
        "service": "Ecocardiograma",
        "professional_id": str(ana.id),
        "unit_id": str(sul.id),
        "unit_marks": True,
    }
    _portal_matches(_list(at_sul.last), tenant, doctors)

    # The SAME service and doctor at the other unit: another combination, another list.
    at_centro = _Walk(tenant, doctors)
    await at_centro.to_unit_list(ana)
    await at_centro.tap(f"unit|{centro.id}")
    assert _ins_ids(at_centro.last) == [
        "ins|Unimed",
        "ins|Bradesco",
        "ins|Amil",
        "ins|particular",
        "ins|outro",
    ]
    _portal_matches(_list(at_centro.last), tenant, doctors)

    # Another service at Centro: Amil is the one refused.
    other = _Walk(tenant, doctors)
    await other.to_service_card("Consulta", doctor=ana)
    await other.tap(f"unit|{centro.id}")
    assert _ins_ids(other.last) == ["ins|Unimed", "ins|Bradesco", "ins|particular", "ins|outro"]


async def test_a_plan_the_doctor_does_not_take_is_not_listed():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    doctors = [ana]
    # `independent`: Ana's own list takes Unimed and Amil only (Bradesco was never ticked).
    tenant = _tenant(
        "independent",
        AcceptanceExceptions(),
        doctors,
        accepted_by={str(ana.id): {UNIMED, AMIL}},
    )
    walk = _Walk(tenant, doctors)
    await walk.to_unit_list(ana)
    await walk.tap(f"unit|{sul.id}")
    assert _ins_ids(walk.last) == ["ins|Unimed", "ins|Amil", "ins|particular", "ins|outro"]
    _portal_matches(_list(walk.last), tenant, doctors)


async def test_one_accepted_plan_lists_plan_particular_outro():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    doctors = [ana]
    refused = _matrix(
        "shared", ana, (ECO_ID, sul, BRADESCO), (ECO_ID, sul, AMIL)
    )
    walk = _Walk(_tenant("shared", refused, doctors), doctors)
    await walk.to_unit_list(ana)
    await walk.tap(f"unit|{sul.id}")
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
    # The one plan, then the two fixed rows ("Outro" stays: there IS something to be "other" to).
    assert _ins_ids(walk.last) == ["ins|Unimed", "ins|particular", "ins|outro"]
    assert [row[1] for row in _list(walk.last).rows][-3:-1] == [
        fr.LABEL_INSURANCE_PARTICULAR,
        fr.LABEL_INSURANCE_OTHER,
    ]


async def test_zero_accepted_goes_particular_direct_with_notice():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    doctors = [ana]
    refused = _matrix(
        "shared", ana,
        *((ECO_ID, unit, plan) for unit in (centro, sul) for plan in (UNIMED, BRADESCO, AMIL)),
    )
    walk = _Walk(_tenant("shared", refused, doctors), doctors)
    await walk.to_unit_list(ana)
    with recorded_logs() as logs:
        await walk.tap(f"unit|{sul.id}")
    # No list, no question: the day picker, with ONE short line saying why.
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    assert walk.last.flow_selected_unit_id == sul.id
    body = _list(walk.last).body
    assert body == (
        "Para esse serviço nessa unidade, o atendimento é particular. "
        + fr.DAY_PICKER_BODY
    )
    # The day picker keeps its back row to the unit list.
    assert _list(walk.last).rows[-1][1] == fu.LABEL_ANOTHER_UNIT
    assert fr.STEP_AWAITING_INSURANCE not in walk.steps
    # The log never names a plan.
    assert not any(name in repr(logs) for name in SEEN_PLANS)

    # If Centro accepts plans, choosing it still opens its convênio picker.
    only_sul_refused = _matrix(
        "shared", ana, *((ECO_ID, sul, plan) for plan in (UNIMED, BRADESCO, AMIL))
    )
    centro_walk = _Walk(_tenant("shared", only_sul_refused, doctors), doctors)
    await centro_walk.to_unit_list(ana)
    await centro_walk.tap(f"unit|{centro.id}")
    assert centro_walk.last.flow_step == fr.STEP_AWAITING_INSURANCE


async def test_zero_accepted_without_units_says_it_without_a_unit():
    ana = _doctor(units=[])
    doctors = [ana]
    refused = _matrix(
        "shared", ana, (ECO_ID, None, UNIMED), (ECO_ID, None, BRADESCO), (ECO_ID, None, AMIL)
    )
    walk = _Walk(_tenant("shared", refused, doctors), doctors)
    await walk.to_service_card("Ecocardiograma", doctor=ana)
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    assert _list(walk.last).body == (
        "Para esse serviço, o atendimento é particular. " + fr.DAY_PICKER_BODY
    )


async def test_particular_recorded_when_none_accepted():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    doctors = [ana]
    refused = _matrix(
        "shared", ana,
        *((ECO_ID, unit, plan) for unit in (centro, sul) for plan in (UNIMED, BRADESCO, AMIL)),
    )
    walk = _Walk(_tenant("shared", refused, doctors), doctors)
    await walk.to_unit_list(ana)
    await walk.tap(f"unit|{sul.id}")
    await walk.tap(_list(walk.last).rows[0][0])  # the first day
    assert walk.last.flow_step == fr.STEP_AWAITING_SLOT
    assert walk.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    await walk.tap(_list(walk.last).rows[0][0])  # the first time
    assert walk.last.flow_step == fr.STEP_AWAITING_CONFIRMATION
    done = await walk.say(fr.LABEL_CONFIRM, gate=_Gate())
    # The appointment is written as Particular - the patient was never asked, nothing else fits.
    assert done.appointment is not None
    assert done.appointment["insurance"] == fr.LABEL_INSURANCE_PARTICULAR


async def test_answering_the_list_goes_to_the_day_of_that_unit():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    doctors = [ana]
    walk = _Walk(_tenant(doctors=doctors), doctors)
    await walk.to_unit_list(ana)
    await walk.tap(f"unit|{sul.id}")
    walk.agenda.log["days"] = 0
    await walk.tap("ins|particular")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    assert walk.last.flow_selected_unit_id == sul.id
    assert walk.agenda.log["days"] == 1
    # Back from the days is the convenio list the patient was just asked (TASK-088 R18); from
    # there, one more step back is the unit list.
    assert _list(walk.last).rows[-1][1] == fr.LABEL_ANOTHER_INSURANCE
    # "Outro convênio" keeps asking on the same step and keeps the unit.
    again = _Walk(_tenant(doctors=doctors), doctors)
    await again.to_unit_list(ana)
    await again.tap(f"unit|{sul.id}")
    other = await again.tap("ins|outro")
    assert other.flow_step == fr.STEP_AWAITING_INSURANCE
    assert other.flow_selected_unit_id == sul.id


# --------------------------------------------------------------------------
# 3. The step is only asked where the clinic collects it
# --------------------------------------------------------------------------


async def test_insurance_skipped_when_clinic_does_not_collect():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    bruno = _doctor("Dr. Bruno", units=[centro])
    doctors = [ana, bruno]
    walk = _Walk(_tenant(collect=False, doctors=doctors), doctors)
    await walk.to_unit_list(ana)
    await walk.tap(f"unit|{sul.id}")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert fr.STEP_AWAITING_INSURANCE not in walk.steps
    assert walk.last.flow_selected_insurance is None
    assert walk.last.flow_selected_unit_id == sul.id


# --------------------------------------------------------------------------
# 4. The menu door and the doctor list
# --------------------------------------------------------------------------


def test_menu_has_only_choose_service_door():
    tenant = _tenant()
    assert fr.menu_buttons_for(tenant, True) == [fr.BTN_CHOOSE_SERVICE, fr.LABEL_OTHER]
    assert fr.BTN_CHOOSE_PROFESSIONAL not in fr.menu_buttons_for(tenant, True)
    # What is DRAWN as the main menu is unchanged (Agendar / Outro).
    assert fr.LABEL_OTHER in fr.main_menu_buttons()


@pytest.mark.parametrize("typed", [fr.BTN_CHOOSE_PROFESSIONAL, fr.BTN_CHOOSE_SERVICE])
async def test_an_old_choose_doctor_label_opens_the_service_first_booking(typed):
    """A typed "Escolher médico" (or a tap on an old card that still has it) must not break."""
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    bruno = _doctor("Dr. Bruno", units=[centro])
    doctors = [ana, bruno]
    walk = _Walk(_tenant(doctors=doctors), doctors, flow_state=FlowState.MENU)
    await walk.say(typed)
    assert walk.last.flow_step == fr.STEP_AWAITING_ATTENDEE_CHOICE
    await walk.say(LABEL_ATTENDEE_SELF)
    assert walk.last.flow_step == fr.STEP_AWAITING_CATALOG_SERVICE


async def test_the_doctor_list_of_a_service_has_only_who_does_it_and_no_convenio_marks():
    centro, _sul = _units()
    only_consulta = [dict(_services()[0])]
    ana = _doctor(units=[centro])
    bruno = _doctor("Dr. Bruno", units=[centro], services=only_consulta)
    carla = _doctor("Dra. Carla", units=[centro])
    doctors = [ana, bruno, carla]
    tenant = _tenant(doctors=doctors)
    walk = _Walk(tenant, doctors)
    await walk.say(fr.LABEL_BOOK)
    await walk.say(LABEL_ATTENDEE_SELF)
    await walk.tap("svc|Ecocardiograma")
    assert walk.last.flow_step == fr.STEP_AWAITING_SERVICE_PROFESSIONAL
    bubble = _list(walk.last)
    assert [r[0] for r in bubble.rows] == [f"prof|{ana.id}", f"prof|{carla.id}"]
    # The convenio is not known here: no "aceita seu convênio" legend, no mark on a row.
    assert fr.INSURANCE_ACCEPTED_MARK not in bubble.body
    assert all(fr.INSURANCE_ACCEPTED_MARK not in str(row[2] or "") for row in bubble.rows)
    _portal_matches(bubble, tenant, doctors)


# --------------------------------------------------------------------------
# 5. A convenio that arrives before the step (AI draft, reschedule, a parked chat):
#    kept when accepted, never swapped for Particular in silence
# --------------------------------------------------------------------------


async def _from_card(tenant, doctors, doctor, insurance):
    walk = _Walk(
        tenant,
        doctors,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Ecocardiograma",
        flow_selected_professional_id=doctor.id,
        flow_selected_insurance=insurance,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    await walk.say(fr.LABEL_BOOK_SERVICE)
    return walk


async def test_a_known_accepted_convenio_is_kept_without_asking_again():
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    doctors = [ana]
    walk = await _from_card(_tenant(doctors=doctors), doctors, ana, "Unimed")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_insurance == "Unimed"
    assert _list(walk.last).body == fr.DAY_PICKER_BODY


async def test_a_known_refused_convenio_with_others_accepted_asks_again_with_a_notice():
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    doctors = [ana]
    refused = _matrix("shared", ana, (ECO_ID, centro, UNIMED))
    walk = await _from_card(_tenant("shared", refused, doctors), doctors, ana, "Unimed")
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
    # Never Particular in silence: the refused answer is dropped and the patient chooses again.
    assert walk.last.flow_selected_insurance is None
    assert walk.last.flow_selected_unit_id == centro.id
    assert _ins_ids(walk.last) == ["ins|Bradesco", "ins|Amil", "ins|particular", "ins|outro"]
    # One unit, implied (the patient never saw or chose it): the unit is not named.
    assert _list(walk.last).body == (
        "O convênio que você informou não é aceito para esse serviço. Escolha uma opção:"
    )


async def test_a_known_refused_convenio_with_none_accepted_goes_particular_and_says_so():
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    doctors = [ana]
    refused = _matrix(
        "shared", ana, (ECO_ID, centro, UNIMED), (ECO_ID, centro, BRADESCO), (ECO_ID, centro, AMIL)
    )
    walk = await _from_card(_tenant("shared", refused, doctors), doctors, ana, "Unimed")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    assert _list(walk.last).body == (
        "Esse convênio não é aceito para o serviço; seguimos como Particular. "
        + fr.DAY_PICKER_BODY
    )


async def test_a_known_particular_is_never_second_guessed_and_a_typed_plan_is_not_kept_in_silence():
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    doctors = [ana]
    refused = _matrix(
        "shared", ana, (ECO_ID, centro, UNIMED), (ECO_ID, centro, BRADESCO), (ECO_ID, centro, AMIL)
    )
    walk = await _from_card(_tenant("shared", refused, doctors), doctors, ana, "Particular")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_insurance == "Particular"
    assert _list(walk.last).body == fr.DAY_PICKER_BODY
    # A typed plan outside the catalog with NOTHING accepted for the combination (review round:
    # SPEC acceptance 2 + designer decision 1): Particular, said out loud - never kept silently.
    walk = await _from_card(
        _tenant("shared", refused, doctors), doctors, ana, "Plano da Empresa"
    )
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_insurance == "Particular"
    assert walk.last.flow_insurance_auto is True
    assert _list(walk.last).body == f"{fr.INSURANCE_REFUSED_PARTICULAR} {fr.DAY_PICKER_BODY}"


async def test_a_chat_parked_on_the_old_first_question_takes_the_new_order():
    """`awaiting_insurance` with no service yet (the retired order): the answer is kept and the
    booking goes on service-first (SPEC A: "cai no fluxo novo na próxima escolha")."""
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    bruno = _doctor("Dr. Bruno", units=[centro])
    doctors = [ana, bruno]
    tenant = _tenant(doctors=doctors)
    walk = _Walk(
        tenant,
        doctors,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_INSURANCE,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    await walk.say("Unimed")
    assert walk.last.flow_step == fr.STEP_AWAITING_CATALOG_SERVICE
    assert walk.last.flow_selected_insurance == "Unimed"
    # ...and the kept answer is judged at the day step like any other.
    await walk.tap("svc|Ecocardiograma")
    await walk.tap(f"prof|{ana.id}")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_insurance == "Unimed"


# --------------------------------------------------------------------------
# 6. Back from the days: the AUTOMATIC Particular is dropped, a chosen plan is re-judged
# --------------------------------------------------------------------------


async def test_back_after_auto_particular_asks_the_convenio_at_an_accepting_unit():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    doctors = [ana]
    refused = _matrix(
        "shared", ana,
        *((ECO_ID, unit, plan) for unit in (centro, sul) for plan in (UNIMED, BRADESCO, AMIL)),
    )
    walk = _Walk(_tenant("shared", refused, doctors), doctors)
    await walk.to_unit_list(ana)
    await walk.tap(f"unit|{sul.id}")
    assert walk.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR  # automatic
    back = _list(walk.last).rows[-1]
    # Nothing was accepted, so no question was asked: the days go back to the unit list.
    assert back[1] == fu.LABEL_ANOTHER_UNIT
    # Centro starts accepting plans while the patient is still in the booking.
    walk.tenant.insurance_refused = _matrix(
        "shared", ana, *((ECO_ID, sul, plan) for plan in (UNIMED, BRADESCO, AMIL))
    )
    await walk.tap(back[0])
    assert walk.last.flow_step == fu.STEP_AWAITING_UNIT
    assert walk.last.flow_selected_insurance is None  # the automatic answer is gone
    await walk.tap(f"unit|{centro.id}")
    # Centro accepts the plans: the question is asked, not skipped.
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
    assert _ins_ids(walk.last) == [
        "ins|Unimed",
        "ins|Bradesco",
        "ins|Amil",
        "ins|particular",
        "ins|outro",
    ]


async def test_back_through_the_convenio_list_preserves_chosen_particular():
    """Opening Trocar convênio and then going back does not change the answer."""
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    doctors = [ana]
    walk = _Walk(_tenant(doctors=doctors), doctors)
    await walk.to_unit_list(ana)
    await walk.tap(f"unit|{sul.id}")
    await walk.tap("ins|particular")
    await walk.tap(_list(walk.last).rows[-1][0])  # back from the days: the convenio list
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
    await walk.tap(_list(walk.last).rows[-1][0])  # back from the list: the unit list
    assert walk.last.flow_step == fu.STEP_AWAITING_UNIT
    assert walk.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    assert walk.last.flow_insurance_auto is False
    await walk.tap(f"unit|{centro.id}")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    assert walk.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR
    assert walk.last.flow_insurance_auto is False
    assert walk.last.flow_selected_unit_id == centro.id


async def test_back_through_the_convenio_list_after_a_chosen_plan_judges_it_at_the_new_unit():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    doctors = [ana]
    # Unimed is refused at Zona Sul only (Bradesco/Amil still accepted there).
    refused = _matrix("shared", ana, (ECO_ID, sul, UNIMED))
    tenant = _tenant("shared", refused, doctors)

    walk = _Walk(tenant, doctors)
    await walk.to_unit_list(ana)
    await walk.tap(f"unit|{centro.id}")
    await walk.tap("ins|Unimed")
    assert walk.last.flow_step == fr.STEP_AWAITING_DAY
    await walk.tap(_list(walk.last).rows[-1][0])  # the convenio list again
    await walk.tap(_list(walk.last).rows[-1][0])  # the unit list
    await walk.tap(f"unit|{sul.id}")
    # Asked afresh at Zona Sul, with only what Zona Sul accepts: Unimed is not on it.
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
    assert walk.last.flow_selected_insurance is None
    assert "ins|Unimed" not in _ids(walk.last)
    assert _list(walk.last).body == fr.INSURANCE_QUESTION


async def test_back_to_the_service_after_an_automatic_particular_asks_again():
    """One unit (no unit list): back goes to the service list; the next service is judged afresh."""
    centro, _sul = _units()
    ana = _doctor(units=[centro])
    doctors = [ana]
    # Nothing accepted for the echo, everything for the consulta.
    refused = _matrix(
        "shared", ana, (ECO_ID, centro, UNIMED), (ECO_ID, centro, BRADESCO), (ECO_ID, centro, AMIL)
    )
    walk = _Walk(_tenant("shared", refused, doctors), doctors)
    await walk.to_service_card("Ecocardiograma", doctor=ana)
    assert walk.last.flow_selected_insurance == fr.LABEL_INSURANCE_PARTICULAR  # automatic
    await walk.tap(_list(walk.last).rows[-1][0])  # back (one unit: the service list)
    assert walk.last.flow_step == fr.STEP_AWAITING_SERVICE
    assert walk.last.flow_selected_insurance is None
    await walk.tap("svc|Consulta")
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
