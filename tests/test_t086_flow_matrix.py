"""TASK-086 T4a - the convênio acceptance matrix on the patient's booking flow (SPEC E6).

Router level, no DB, no network (same style as tests/test_flow_router_units.py): SimpleNamespace
snapshots carrying `insurance_refused` (services/insurance_acceptance.py::AcceptanceExceptions),
a fake agenda and a fake booking gate. Every matrix test runs in both modes: `shared` (one
matrix for the whole clinic) and `independent` (one matrix per doctor).

Covered (inventory A1-A7, A13): doctor marks, service rows, the convênio list of a known
service, the unit filter (single-unit confirm text, zero units without the clinic alert, a
doctor without units), the alternatives offered, the final guard at "Confirmar" (including the
race where the clinic edits the matrix mid-conversation) and the Portal pages re-resolved by
`list_options`. With no refused cell every path is the same as before (characterization).
"""

import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

os.environ.setdefault("APP_ENV", "test")

import pytest  # noqa: E402

from secretaria.ai.formatter import SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr, flow_units as fu, list_options  # noqa: E402
from secretaria.services.booking_hold import GateDecision  # noqa: E402
from secretaria.services.insurance_acceptance import EMPTY, AcceptanceExceptions  # noqa: E402
from secretaria.services.unit_booking import UnitOption  # noqa: E402
from tests.before_support import recorded_logs  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
CLINIC_HOURS = {d: [{"start": "08:00", "end": "18:00"}] for d in ("monday", "tuesday")}
CENTRO_HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}
SUL_HOURS = {"tuesday": [{"start": "14:00", "end": "18:00"}]}
NORTE_HOURS = {"monday": [{"start": "14:00", "end": "18:00"}]}

CONSULTA_ID = uuid4()
ECO_ID = uuid4()
UNIMED = str(uuid4())
BRADESCO = str(uuid4())
MODES = ("shared", "independent")


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
        UnitOption(id=uuid4(), name="Norte", address="Rua C, 30", business_hours=NORTE_HOURS),
    )


def _doctor(name="Dra. Ana", units=(), marks=None, *, specialty="Cardiologia"):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=specialty,
        about=None,
        appointment_types=_services(),
        business_hours=None,
        min_booking_notice_hours=None,
        units=list(units),
        service_units=marks or {},
    )


def _key(service_id, unit, plan):
    return (str(service_id), None if unit is None else str(unit.id), plan)


def _matrix(mode, doctor, *cells):
    """`cells` refused for `doctor`: the clinic matrix (shared) or that doctor's (independent)."""
    keys = frozenset(_key(*cell) for cell in cells)
    if mode == "shared":
        return AcceptanceExceptions(clinic=keys)
    return AcceptanceExceptions(by_professional={str(doctor.id): keys})


def _merge(*matrices):
    clinic = frozenset().union(*(m.clinic for m in matrices))
    by_prof: dict = {}
    for m in matrices:
        for pid, keys in m.by_professional.items():
            by_prof[pid] = by_prof.get(pid, frozenset()) | keys
    return AcceptanceExceptions(clinic=clinic, by_professional=by_prof)


def _tenant(mode="shared", refused=None, doctors=(), *, drop_field=False):
    tenant = SimpleNamespace(
        initial_flows={"enabled": True},
        appointment_types=_services(),
        appointment_duration_min=30,
        business_hours=CLINIC_HOURS,
        collect_insurance=True,
        insurance_mode=mode,
        insurances=["Unimed", "Bradesco"],
        insurance_plans=[
            {"id": UNIMED, "name": "Unimed", "mechanism": None},
            {"id": BRADESCO, "name": "Bradesco", "mechanism": None},
        ],
        # Independent mode: every doctor's own list takes both plans (only the matrix differs).
        insurance_accepted_by=(
            {str(d.id): {UNIMED, BRADESCO} for d in doctors} if mode == "independent" else {}
        ),
        insurance_refused=refused,
        timezone="America/Sao_Paulo",
    )
    if drop_field:
        del tenant.insurance_refused
    return tenant


def _conversation(**kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Ecocardiograma",
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance="Unimed",
        flow_managing_appointment_id=None,
        flow_attendee_name="",
        flow_selected_unit_id=None,
        flow_draft=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


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
    def __init__(self, *, armed: bool):
        self.armed = armed
        self.deposit_capable = True
        self.decided: list[dict] = []

    async def busy_windows(self, professional_id):
        return []

    async def decide(self, **kwargs):
        self.decided.append(kwargs)
        if self.armed:
            return GateDecision("held", email_masked="m***@x.com", hold_id=uuid4())
        return GateDecision("commit")


def _list(result) -> SlotsBubble:
    [bubble] = [b for b in result.bubbles if isinstance(b, SlotsBubble)]
    return bubble


def _menu(result) -> fr.MenuBubble:
    [bubble] = [b for b in result.bubbles if isinstance(b, fr.MenuBubble)]
    return bubble


def _row(bubble: SlotsBubble, row_id: str) -> tuple:
    [row] = [r for r in bubble.rows if r[0] == row_id]
    return row


def _portal_matches(bubble: SlotsBubble, tenant, roster) -> None:
    """A13: page 1 (the bubble) and the re-resolved pages/taps render the same rows."""
    source = bubble.source
    assert list_options.resolve(source.kind, source.scope, tenant, roster) == source.items
    for row_id, *_ in source.items:
        assert list_options.contains(source.kind, source.scope, tenant, roster, row_id)


# --------------------------------------------------------------------------
# flow_units: the single unit x convênio filter (A5)
# --------------------------------------------------------------------------


def test_flow_units_without_kwargs_or_with_an_empty_matrix_is_todays():
    centro, sul, _norte = _units()
    doctor = _doctor(units=[centro, sul], marks={ECO_ID: frozenset({sul.id})})
    for service_id in (None, CONSULTA_ID, ECO_ID):
        base = fu.unit_choice(doctor, service_id)
        for kwargs in ({}, {"plan_id": UNIMED, "refused": EMPTY}, {"plan_id": None}):
            assert fu.unit_choice(doctor, service_id, **kwargs) == base
            assert fu.units_for_service(doctor, service_id, **kwargs) == fu.units_for_service(
                doctor, service_id
            )
        assert base.by_plan is False


@pytest.mark.parametrize("mode", MODES)
def test_flow_units_unit_choice_by_plan(mode):
    centro, sul, norte = _units()
    two = _doctor(units=[centro, sul])
    plan = {"plan_id": UNIMED, "refused": _matrix(mode, two, (ECO_ID, centro, UNIMED))}
    choice = fu.unit_choice(two, ECO_ID, **plan)
    assert (choice.kind, [u.id for u in choice.units], choice.by_plan) == (
        "confirm",
        [sul.id],
        True,
    )
    # Another plan, another service: untouched.
    assert fu.unit_choice(two, ECO_ID, plan_id=BRADESCO, refused=plan["refused"]).kind == "list"
    assert fu.unit_choice(two, CONSULTA_ID, **plan).kind == "list"

    both = {
        "plan_id": UNIMED,
        "refused": _matrix(mode, two, (ECO_ID, centro, UNIMED), (ECO_ID, sul, UNIMED)),
    }
    assert fu.unit_choice(two, ECO_ID, **both) == fu.UnitChoice("none", [], by_plan=True)

    three = _doctor(units=[centro, sul, norte])
    narrowed = fu.unit_choice(
        three, ECO_ID, plan_id=UNIMED, refused=_matrix(mode, three, (ECO_ID, norte, UNIMED))
    )
    assert (narrowed.kind, narrowed.by_plan) == ("list", True)
    assert [u.id for u in narrowed.units] == [centro.id, sul.id]

    # A doctor without units: the "no unit" slice decides.
    free = _doctor(units=[])
    refused_free = _matrix(mode, free, (ECO_ID, None, UNIMED))
    assert fu.unit_choice(free, ECO_ID, plan_id=UNIMED, refused=refused_free) == fu.UnitChoice(
        "none", [], by_plan=True
    )
    assert fu.unit_choice(free, CONSULTA_ID, plan_id=UNIMED, refused=refused_free).kind == "free"


def test_flow_units_a_service_restricted_to_one_unit_is_not_blamed_on_the_plan():
    centro, sul, _norte = _units()
    doctor = _doctor(units=[centro, sul], marks={ECO_ID: frozenset({sul.id})})
    refused = _matrix("shared", doctor, (CONSULTA_ID, centro, UNIMED))
    choice = fu.unit_choice(doctor, ECO_ID, plan_id=UNIMED, refused=refused)
    assert (choice.kind, choice.by_plan) == ("confirm", False)
    # The config gap stays the config gap (never by_plan): the service at no open unit.
    closed = UnitOption(id=uuid4(), name="Fechada", address=None, business_hours={})
    gap = _doctor(units=[centro, closed], marks={ECO_ID: frozenset({closed.id})})
    assert fu.unit_choice(gap, ECO_ID, plan_id=UNIMED, refused=refused) == fu.UnitChoice("none", [])


def test_flow_units_effective_and_find_unit_drop_a_refused_pick():
    centro, sul, _norte = _units()
    doctor = _doctor(units=[centro, sul])
    plan = {"plan_id": UNIMED, "refused": _matrix("shared", doctor, (ECO_ID, centro, UNIMED))}
    conversation = SimpleNamespace(flow_selected_unit_id=centro.id)
    assert fu.effective_unit(conversation, doctor, ECO_ID).id == centro.id
    assert fu.effective_unit(conversation, doctor, ECO_ID, **plan) is None
    assert fu.find_unit(doctor, centro.id, ECO_ID, **plan) is None
    assert fu.find_unit(doctor, sul.id, ECO_ID, **plan).id == sul.id


# --------------------------------------------------------------------------
# Characterization: no refused cell -> every touched path is today's
# --------------------------------------------------------------------------


async def _paths(tenant, doctors, agenda_log):
    """The outputs of every path this task touched, for one tenant snapshot."""
    ana, bruno = doctors
    out = []
    out.append(fr._enter_professional_list(tenant, doctors, insurance="Unimed"))
    out.append(
        fr._enter_service_professional_list(tenant, doctors, "Ecocardiograma", insurance="Unimed")
    )
    out.append(fr._enter_professional_services(ana, tenant, insurance="Unimed"))
    out.append(
        fr._enter_insurance(
            tenant,
            _conversation(flow_selected_professional_id=ana.id),
            professionals=doctors,
        )
    )
    for step, body in (
        (fr.STEP_AWAITING_SERVICE_CONFIRM, fr.LABEL_BOOK_SERVICE),
        (fr.STEP_AWAITING_SERVICE, "Ecocardiograma"),
    ):
        out.append(
            await fr.route(
                _conversation(flow_step=step, flow_selected_professional_id=ana.id),
                tenant,
                _Agenda(log=agenda_log),
                body,
                professionals=doctors,
            )
        )
    out.append(
        await fr.route(
            _conversation(
                flow_step=fr.STEP_AWAITING_CONFIRMATION,
                flow_selected_professional_id=ana.id,
                flow_selected_day="2099-01-06",
                flow_selected_slot="2099-01-06T09:00",
                flow_selected_unit_id=ana.units[1].id,
            ),
            tenant,
            _Agenda(log=agenda_log),
            fr.LABEL_CONFIRM,
            patient_name="Maria",
            professionals=doctors,
            gate=_Gate(armed=False),
        )
    )
    return out


async def test_without_refused_cells_every_touched_path_is_unchanged():
    centro, sul, _norte = _units()
    ana = _doctor(units=[centro, sul])
    bruno = _doctor("Dr. Bruno", units=[])
    doctors = [ana, bruno]
    for mode in MODES:
        logs = [{"days": 0, "created": []} for _ in range(3)]
        legacy = await _paths(_tenant(mode, None, doctors, drop_field=True), doctors, logs[0])
        none = await _paths(_tenant(mode, None, doctors), doctors, logs[1])
        empty = await _paths(_tenant(mode, EMPTY, doctors), doctors, logs[2])
        assert legacy == none == empty
        assert logs[0] == logs[1] == logs[2]
        # The scopes did not grow any TASK-086 key.
        for result in legacy:
            for bubble in result.bubbles:
                source = getattr(bubble, "source", None)
                if source is not None:
                    assert "unit_marks" not in source.scope
                    assert not list_options.needs_unit_options(source.scope)
        # The booking really happened (the guard let it through).
        assert legacy[-1].appointment is not None


# --------------------------------------------------------------------------
# A1/A2: doctor marks
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_a_doctor_the_matrix_refuses_is_marked_not_hidden(mode):
    centro, sul, _norte = _units()
    ana = _doctor(units=[centro])
    # Another unit: the shared (clinic) matrix refuses the cell for EVERY doctor at Centro.
    bruno = _doctor("Dr. Bruno", units=[sul])
    doctors = [ana, bruno]
    refused = _matrix(mode, ana, (CONSULTA_ID, centro, UNIMED), (ECO_ID, centro, UNIMED))
    tenant = _tenant(mode, refused, doctors)

    result = fr._enter_professional_list(tenant, doctors, insurance="Unimed")
    bubble = _list(result)
    assert _row(bubble, f"prof|{ana.id}")[2] == f"{fr.INSURANCE_REFUSED_MARK} · Cardiologia"
    assert _row(bubble, f"prof|{bruno.id}")[2] == f"{fr.INSURANCE_ACCEPTED_MARK} · Cardiologia"
    assert bubble.source.scope == {"insurance": "Unimed", "unit_marks": True}
    assert list_options.needs_unit_options(bubble.source.scope)
    _portal_matches(bubble, tenant, doctors)

    # Bradesco is not refused anywhere: both doctors keep the accepted mark.
    other = _list(fr._enter_professional_list(tenant, doctors, insurance="Bradesco"))
    assert _row(other, f"prof|{ana.id}")[2] == f"{fr.INSURANCE_ACCEPTED_MARK} · Cardiologia"
    # Particular is never refused.
    particular = _list(fr._enter_professional_list(tenant, doctors, insurance="Particular"))
    assert _row(particular, f"prof|{ana.id}")[2] == "Cardiologia"


@pytest.mark.parametrize("mode", MODES)
def test_a_doctor_refused_for_one_service_only_is_marked_per_service(mode):
    centro, sul, _norte = _units()
    ana = _doctor(units=[centro])
    bruno = _doctor("Dr. Bruno", units=[sul])
    doctors = [ana, bruno]
    tenant = _tenant(mode, _matrix(mode, ana, (ECO_ID, centro, UNIMED)), doctors)

    # Service unknown: Ana still takes Unimed for Consulta -> accepted.
    general = _list(fr._enter_professional_list(tenant, doctors, insurance="Unimed"))
    assert _row(general, f"prof|{ana.id}")[2].startswith(fr.INSURANCE_ACCEPTED_MARK)
    # Service known (service-first list): refused for Ecocardiograma.
    result = fr._enter_service_professional_list(
        tenant, doctors, "Ecocardiograma", insurance="Unimed"
    )
    bubble = _list(result)
    assert _row(bubble, f"prof|{ana.id}")[2].startswith(fr.INSURANCE_REFUSED_MARK)
    assert _row(bubble, f"prof|{bruno.id}")[2].startswith(fr.INSURANCE_ACCEPTED_MARK)
    assert bubble.source.scope == {
        "insurance": "Unimed",
        "service": "Ecocardiograma",
        "unit_marks": True,
    }
    _portal_matches(bubble, tenant, doctors)


# --------------------------------------------------------------------------
# A4: the doctor's service list
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_a_refused_service_row_says_so_on_whatsapp_and_on_every_portal_page(mode):
    centro, sul, _norte = _units()
    ana = _doctor(units=[centro, sul])
    bruno = _doctor("Dr. Bruno", units=[centro])
    doctors = [ana, bruno]
    refused = _matrix(mode, ana, (ECO_ID, centro, UNIMED), (ECO_ID, sul, UNIMED))
    tenant = _tenant(mode, refused, doctors)

    bubble = _list(fr._enter_professional_services(ana, tenant, insurance="Unimed"))
    assert _row(bubble, "svc|Ecocardiograma")[2] == fr.INSURANCE_REFUSED_MARK
    assert len(_row(bubble, "svc|Consulta")) == 2  # untouched rows stay 2-tuples
    assert bubble.source.scope == {
        "list": "doctor",
        "professional_id": str(ana.id),
        "insurance": "Unimed",
        "unit_marks": True,
    }
    _portal_matches(bubble, tenant, doctors)
    # Another convênio: no mark.
    plain = _list(fr._enter_professional_services(ana, tenant, insurance="Bradesco"))
    assert len(_row(plain, "svc|Ecocardiograma")) == 2


@pytest.mark.parametrize("mode", MODES)
async def test_tapping_a_refused_service_goes_on_to_the_unit_without_the_not_accepted_card(mode):
    centro, sul, norte = _units()
    ana = _doctor(units=[centro, sul])
    bruno = _doctor("Dr. Bruno", units=[norte])
    doctors = [ana, bruno]
    refused = _matrix(mode, ana, (ECO_ID, centro, UNIMED), (ECO_ID, sul, UNIMED))
    tenant = _tenant(mode, refused, doctors)
    agenda = _Agenda()

    result = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_SERVICE,
            flow_selected_type=None,
            flow_selected_professional_id=ana.id,
        ),
        tenant,
        agenda,
        "Ecocardiograma",
        professionals=doctors,
    )
    # TASK-088 review round: no mid-flow "não atende" card - the tap goes on (the unit, then the
    # convênio gate judges the early Unimed with service, doctor and unit known), exactly as a
    # clinic with several doctors does.
    assert result.flow_step == fu.STEP_AWAITING_UNIT
    assert result.notify_no_unit is None
    assert result.flow_selected_type == "Ecocardiograma"
    assert result.flow_selected_professional_id == ana.id
    assert result.flow_selected_insurance == "Unimed"
    assert result.flow_step != fr.STEP_AWAITING_PLAN_REFUSED
    assert agenda.log["days"] == 0


# --------------------------------------------------------------------------
# A3: the convênio list when the service is already known
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_known_service_lists_only_plans_accepted_somewhere(mode):
    centro, sul, _norte = _units()
    ana = _doctor(units=[centro, sul])
    bruno = _doctor("Dr. Bruno", units=[centro])
    doctors = [ana, bruno]
    refused = _merge(
        _matrix(mode, ana, (ECO_ID, centro, UNIMED), (ECO_ID, sul, UNIMED)),
        _matrix(mode, bruno, (ECO_ID, centro, BRADESCO)) if mode == "independent" else EMPTY,
    )
    tenant = _tenant(mode, refused, doctors)

    # Doctor chosen (TASK-088 order): the "Sim" on the service card asks the UNIT first, then the
    # convênio, filtered by Ana's matrix at that unit.
    asked = await fr.route(
        _conversation(flow_selected_professional_id=ana.id, flow_selected_insurance=None),
        tenant,
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=doctors,
    )
    assert asked.flow_step == fu.STEP_AWAITING_UNIT
    result = await fr.route(
        _conversation(
            flow_step=fu.STEP_AWAITING_UNIT,
            flow_selected_professional_id=ana.id,
            flow_selected_insurance=None,
        ),
        tenant,
        _Agenda(),
        f"Centro (unit|{centro.id})",
        professionals=doctors,
    )
    assert result.flow_step == fr.STEP_AWAITING_INSURANCE
    assert result.flow_selected_unit_id == centro.id
    bubble = _list(result)
    ids = [r[0] for r in bubble.rows]
    assert "ins|Unimed" not in ids and "ins|Bradesco" in ids
    # The fixed rows, then (TASK-088 R18) the way back to the unit list the patient chose from.
    back = f"{fr.ROW_DAY_BACK_PREFIX}{fr.BACK_TARGET_UNIT}"
    assert ids[-3:] == ["ins|particular", "ins|outro", back]
    assert bubble.source.scope == {
        "service": "Ecocardiograma",
        "unit_marks": True,
        "professional_id": str(ana.id),
        "unit_id": str(centro.id),
    }
    _portal_matches(bubble, tenant, doctors)
    # Doctor not chosen (an AI draft with the service only): accepted by ANY doctor offering it.
    anyone = _list(fr._enter_insurance(tenant, _conversation(), professionals=doctors))
    anyone_ids = [r[0] for r in anyone.rows]
    if mode == "shared":
        # The clinic matrix refuses Unimed everywhere Eco is done -> gone for everyone.
        assert "ins|Unimed" not in anyone_ids
    else:
        assert "ins|Unimed" in anyone_ids  # Bruno takes it
    assert "ins|Bradesco" in anyone_ids
    _portal_matches(anyone, tenant, doctors)
    # No booking yet (the retired first question): every plan, scope as before.
    start = _list(fr._enter_insurance(tenant))
    assert {"ins|Unimed", "ins|Bradesco"} <= {r[0] for r in start.rows}
    assert start.source.scope == {}


# --------------------------------------------------------------------------
# A5 (TASK-088 order): the unit comes BEFORE the convênio, so it is never narrowed by it - the
# plan is judged AT the unit the patient picks (a convênio that arrived early: kept when accepted
# there, asked again with a line when not, Particular saying so when nothing else is accepted).
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_a_known_convenio_does_not_narrow_the_unit_it_is_judged_at_the_unit(mode):
    centro, sul, _norte = _units()
    ana = _doctor(units=[centro, sul])
    tenant = _tenant(mode, _matrix(mode, ana, (ECO_ID, centro, UNIMED)), [ana])
    agenda = _Agenda()
    result = await fr.route(
        _conversation(), tenant, agenda, fr.LABEL_BOOK_SERVICE, professionals=[ana]
    )
    # Both units are offered - the convênio does not leave one (the old convênio-specific
    # Sim/Não card is gone from this path) - and nothing is read or sent to the clinic.
    assert result.flow_step == fu.STEP_AWAITING_UNIT
    assert [r[0] for r in _list(result).rows][:2] == [f"unit|{centro.id}", f"unit|{sul.id}"]
    assert result.notify_no_unit is None and agenda.log["days"] == 0
    # Zona Sul accepts the plan: kept, and its days open.
    at_sul = await fr.route(
        _conversation(flow_step=fu.STEP_AWAITING_UNIT),
        tenant,
        agenda,
        f"Zona Sul (unit|{sul.id})",
        professionals=[ana],
    )
    assert at_sul.flow_step == fr.STEP_AWAITING_DAY
    assert at_sul.flow_selected_unit_id == sul.id
    assert at_sul.flow_selected_insurance == "Unimed"
    # Centro does not: never Particular in silence - the filtered list, with a line on top.
    at_centro = await fr.route(
        _conversation(flow_step=fu.STEP_AWAITING_UNIT),
        tenant,
        agenda,
        f"Centro (unit|{centro.id})",
        professionals=[ana],
    )
    assert at_centro.flow_step == fr.STEP_AWAITING_INSURANCE
    assert at_centro.flow_selected_unit_id == centro.id
    assert at_centro.flow_selected_insurance is None
    assert "ins|Unimed" not in [r[0] for r in _list(at_centro).rows]
    assert _list(at_centro).body == fr.INSURANCE_REFUSED_LIST_UNIT


@pytest.mark.parametrize("mode", MODES)
async def test_the_unit_list_offers_every_unit_of_the_service_whatever_the_convenio(mode):
    centro, sul, norte = _units()
    ana = _doctor(units=[centro, sul, norte])
    tenant = _tenant(mode, _matrix(mode, ana, (ECO_ID, norte, UNIMED)), [ana])
    result = await fr.route(
        _conversation(), tenant, _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[ana]
    )
    assert result.flow_step == fu.STEP_AWAITING_UNIT
    ids = [r[0] for r in _list(result).rows]
    assert ids[:3] == [f"unit|{centro.id}", f"unit|{sul.id}", f"unit|{norte.id}"]
    # A tapped unit is taken even where the plan is refused (the plan is judged at it, next).
    tapped = await fr.route(
        _conversation(flow_step=fu.STEP_AWAITING_UNIT),
        tenant,
        _Agenda(),
        f"Norte (unit|{norte.id})",
        professionals=[ana],
    )
    # Norte refuses the known Unimed: the list is asked again at that unit (Unimed dropped).
    assert tapped.flow_step == fr.STEP_AWAITING_INSURANCE
    assert tapped.flow_selected_unit_id == norte.id


@pytest.mark.parametrize("mode", MODES)
async def test_a_known_convenio_refused_at_every_unit_never_alerts_the_clinic(mode):
    centro, sul, _norte = _units()
    ana = _doctor(units=[centro, sul])
    bruno = _doctor("Dr. Bruno", units=[centro])
    carla = _doctor("Dra. Carla", units=[sul])
    doctors = [ana, bruno, carla]
    refused = _merge(
        _matrix(mode, ana, (ECO_ID, centro, UNIMED), (ECO_ID, sul, UNIMED)),
        _matrix(mode, bruno, (ECO_ID, centro, UNIMED)),
    )
    tenant = _tenant(mode, refused, doctors)
    result = await fr.route(
        _conversation(flow_selected_professional_id=ana.id),
        tenant,
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=doctors,
    )
    # The units first (TASK-088); then, at one of them, the plan is asked again - R4: nothing is
    # misconfigured, the clinic is NOT told.
    assert result.flow_step == fu.STEP_AWAITING_UNIT
    assert result.notify_no_unit is None
    at_sul = await fr.route(
        _conversation(flow_step=fu.STEP_AWAITING_UNIT, flow_selected_professional_id=ana.id),
        tenant,
        _Agenda(),
        f"Zona Sul (unit|{sul.id})",
        professionals=doctors,
    )
    assert at_sul.flow_step == fr.STEP_AWAITING_INSURANCE
    assert at_sul.notify_no_unit is None
    assert at_sul.flow_selected_unit_id == sul.id

    # The "não atende este convênio" step itself (still reached by a refused service row and by
    # the final guard) keeps its buttons and its alternatives.
    card = fr._enter_plan_refused(
        _conversation(flow_selected_professional_id=ana.id),
        tenant,
        doctors,
        ana,
        "Ecocardiograma",
    )
    assert card.notify_no_unit is None and card.flow_selected_unit_id is None
    if mode == "shared":
        # The clinic matrix refuses Unimed for Eco at Centro AND Zona Sul: nobody else either.
        assert _menu(card).labels == ["Outro serviço", "Trocar convênio"]
    else:
        assert _menu(card).labels == ["Outro médico", "Outro serviço", "Trocar convênio"]
        # A6: "Outro médico" lists only Carla (Bruno is refused too).
        other = await fr.route(
            _conversation(
                flow_step=fr.STEP_AWAITING_PLAN_REFUSED, flow_selected_professional_id=ana.id
            ),
            tenant,
            _Agenda(),
            "Outro médico",
            professionals=doctors,
        )
        bubble = _list(other)
        assert [r[0] for r in bubble.rows if r[0].startswith("prof|")] == [f"prof|{carla.id}"]
        assert bubble.source.scope["only_ids"] == [str(carla.id)]
        assert other.flow_selected_type == "Ecocardiograma"
        assert other.flow_selected_insurance == "Unimed"
        _portal_matches(bubble, tenant, doctors)


async def test_the_not_accepted_step_buttons_lead_to_service_and_convenio():
    centro, _sul, _norte = _units()
    ana = _doctor(units=[centro])
    tenant = _tenant("shared", _matrix("shared", ana, (ECO_ID, centro, UNIMED)), [ana])
    conv = dict(flow_step=fr.STEP_AWAITING_PLAN_REFUSED, flow_selected_professional_id=ana.id)

    service = await fr.route(
        _conversation(**conv), tenant, _Agenda(), "Outro serviço", professionals=[ana]
    )
    assert service.flow_step == fr.STEP_AWAITING_SERVICE
    assert _row(_list(service), "svc|Ecocardiograma")[2] == fr.INSURANCE_REFUSED_MARK

    change = await fr.route(
        _conversation(**conv), tenant, _Agenda(), "Trocar convênio", professionals=[ana]
    )
    assert change.flow_step == fr.STEP_AWAITING_INSURANCE
    assert change.flow_selected_type == "Ecocardiograma"
    assert "ins|Unimed" not in [r[0] for r in _list(change).rows]

    talk = await fr.route(
        _conversation(**conv), tenant, _Agenda(), "qual o endereço?", professionals=[ana]
    )
    assert talk.action == "delegate_llm"
    # Resumed later (TASK-088): the day step decides afresh - the plan is still refused at the
    # doctor's only unit, so it asks the convênio again, filtered, with its line (the card is only
    # reached by a refused service row now).
    resumed = await fr.resume_bubbles(_conversation(**conv), tenant, _Agenda(), [ana])
    assert resumed.flow_step == fr.STEP_AWAITING_INSURANCE
    assert "ins|Unimed" not in [r[0] for r in _list(resumed).rows]


@pytest.mark.parametrize("mode", MODES)
async def test_a_doctor_without_units_asks_the_no_unit_slice(mode):
    ana = _doctor(units=[])
    tenant = _tenant(mode, _matrix(mode, ana, (ECO_ID, None, UNIMED)), [ana])
    # The plan the patient already gave is refused in the "no unit" slice: asked again, filtered,
    # and the line has no "nessa unidade" (the doctor has no units).
    refused = await fr.route(
        _conversation(), tenant, _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[ana]
    )
    assert refused.flow_step == fr.STEP_AWAITING_INSURANCE
    assert refused.notify_no_unit is None
    assert "ins|Unimed" not in [r[0] for r in _list(refused).rows]
    assert _list(refused).body == fr.INSURANCE_REFUSED_LIST
    ok = await fr.route(
        _conversation(flow_selected_type="Consulta"),
        tenant,
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=[ana],
    )
    assert ok.flow_step == fr.STEP_AWAITING_DAY


# --------------------------------------------------------------------------
# A6: the "0 unidades" alternatives only offer doctors that take the convênio
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_no_unit_alternatives_skip_doctors_the_matrix_refuses(mode):
    centro, sul, _norte = _units()
    closed = UnitOption(id=uuid4(), name="Fechada", address=None, business_hours={})
    # Ana: Eco only at a closed unit -> the config gap ("0 unidades", clinic alerted).
    ana = _doctor(units=[centro, closed], marks={ECO_ID: frozenset({closed.id})})
    bruno = _doctor("Dr. Bruno", units=[centro])
    carla = _doctor("Dra. Carla", units=[sul])
    doctors = [ana, bruno, carla]
    refused = (
        _matrix(mode, bruno, (ECO_ID, centro, UNIMED))
        if mode == "independent"
        else AcceptanceExceptions(clinic=frozenset({_key(ECO_ID, centro, UNIMED)}))
    )
    tenant = _tenant(mode, refused, doctors)
    result = await fr.route(
        _conversation(flow_selected_professional_id=ana.id),
        tenant,
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=doctors,
    )
    assert result.notify_no_unit == (ana.id, ECO_ID, "Ecocardiograma")
    ids = [r[0] for r in _list(result).rows if r[0].startswith("prof|")]
    assert ids == [f"prof|{carla.id}"]


# --------------------------------------------------------------------------
# A7: the final guard at "Confirmar" (+ the race)
# --------------------------------------------------------------------------


def _confirming(unit, doctor, **kw):
    return _conversation(
        flow_step=fr.STEP_AWAITING_CONFIRMATION,
        flow_selected_professional_id=doctor.id,
        flow_selected_day="2099-01-06",
        flow_selected_slot="2099-01-06T09:00",
        flow_selected_unit_id=getattr(unit, "id", None),
        **kw,
    )


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("armed", [False, True])
async def test_the_final_guard_refuses_and_reopens_the_convenio(mode, armed):
    centro, sul, _norte = _units()
    ana = _doctor(units=[centro, sul])
    bruno = _doctor("Dr. Bruno", units=[centro])
    doctors = [ana, bruno]
    tenant = _tenant(mode, _matrix(mode, ana, (ECO_ID, sul, UNIMED)), doctors)
    agenda = _Agenda()
    gate = _Gate(armed=armed)
    with recorded_logs() as logs:
        result = await fr.route(
            _confirming(sul, ana),
            tenant,
            agenda,
            fr.LABEL_CONFIRM,
            patient_name="Maria",
            professionals=doctors,
            gate=gate,
        )
    assert result.appointment is None and result.booking_hold is None
    assert agenda.log["created"] == [] and gate.decided == []
    assert result.flow_step == fr.STEP_AWAITING_INSURANCE
    assert result.flow_selected_type == "Ecocardiograma"
    assert result.flow_selected_professional_id == ana.id
    # TASK-088: the unit is chosen BEFORE the convênio, so reopening the convênio keeps it (the
    # list is for that unit); day and time are the ones dropped.
    assert result.flow_selected_unit_id == sul.id
    assert result.flow_selected_slot is None
    notice = result.bubbles[0]
    assert isinstance(notice, TextBubble)
    assert notice.body == (
        "Desculpe, o convênio Unimed não é mais aceito para Ecocardiograma com Dra. Ana na "
        "unidade Zona Sul. Nada foi agendado. Escolha outro convênio ou Particular:"
    )
    # TASK-088: the reopened list is the one for THIS unit (Zona Sul), where Unimed is now
    # refused: only what Zona Sul accepts is offered (Ana still takes it at Centro).
    assert [r[0] for r in _list(result).rows] == [
        "ins|Bradesco",
        "ins|particular",
        "ins|outro",
        f"{fr.ROW_DAY_BACK_PREFIX}{fr.BACK_TARGET_UNIT}",
    ]
    [event] = [e for e in logs if e["event"] == "flow_booking_insurance_refused"]
    assert event["plan_id"] == UNIMED and event["unit_id"] == str(sul.id)
    assert "Unimed" not in str(event)


@pytest.mark.parametrize("mode", MODES)
async def test_race_matrix_changes_between_the_list_and_the_confirmation(mode):
    centro, _sul, _norte = _units()
    ana = _doctor(units=[centro])
    doctors = [ana]
    # Turn N: no refused cell - the service list shows Eco plainly, the booking goes on.
    before = _tenant(mode, None, doctors)
    listed = _list(fr._enter_professional_services(ana, before, insurance="Unimed"))
    assert len(_row(listed, "svc|Ecocardiograma")) == 2
    # Turn N+k: the clinic refused Unimed for Eco at Centro; the snapshot is reloaded.
    after = _tenant(mode, _matrix(mode, ana, (ECO_ID, centro, UNIMED)), doctors)
    agenda = _Agenda()
    gate = _Gate(armed=False)
    result = await fr.route(
        _confirming(centro, ana),
        after,
        agenda,
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=doctors,
        gate=gate,
    )
    assert result.appointment is None
    assert agenda.log["created"] == [] and gate.decided == []
    assert result.flow_step == fr.STEP_AWAITING_INSURANCE
    assert "ins|Unimed" not in [r[0] for r in _list(result).rows]
    # The same confirmation on the unchanged snapshot books.
    booked = await fr.route(
        _confirming(centro, ana),
        before,
        _Agenda(),
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=doctors,
        gate=_Gate(armed=False),
    )
    assert booked.appointment is not None


@pytest.mark.parametrize("insurance", ["Particular", "Golden Cross"])
async def test_the_guard_never_refuses_particular_or_a_typed_convenio(insurance):
    centro, _sul, _norte = _units()
    ana = _doctor(units=[centro])
    tenant = _tenant("shared", _matrix("shared", ana, (ECO_ID, centro, UNIMED)), [ana])
    agenda = _Agenda()
    result = await fr.route(
        _confirming(centro, ana, flow_selected_insurance=insurance),
        tenant,
        agenda,
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=[ana],
        gate=_Gate(armed=False),
    )
    assert result.appointment is not None
    assert len(agenda.log["created"]) == 1


async def test_the_guard_for_a_doctor_without_units_reads_the_no_unit_slice():
    ana = _doctor(units=[])
    tenant = _tenant("shared", _matrix("shared", ana, (ECO_ID, None, UNIMED)), [ana])
    agenda = _Agenda()
    result = await fr.route(
        _confirming(None, ana),
        tenant,
        agenda,
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=[ana],
        gate=_Gate(armed=False),
    )
    assert result.appointment is None and agenda.log["created"] == []
    assert result.bubbles[0].body.startswith(
        "Desculpe, o convênio Unimed não é mais aceito para Ecocardiograma com Dra. Ana."
    )


# --------------------------------------------------------------------------
# A13: list_options flags
# --------------------------------------------------------------------------


def test_needs_unit_options_for_matrix_marked_scopes_only():
    assert list_options.needs_unit_options({"list": "doctor_unit"})
    assert list_options.needs_unit_options({"insurance": "X", "unit_marks": True})
    assert not list_options.needs_unit_options({"insurance": "X"})
    assert not list_options.needs_unit_options({"list": "doctor", "unit_marks": "yes"})
    assert not list_options.needs_unit_options(None)
