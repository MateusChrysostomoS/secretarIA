# ruff: noqa: F811
"""TASK-088 Task 8 fix round 3 (R41): a tap that picks the doctor in the SAME turn builds its days
on THAT doctor's agenda.

Before Task 8 the service card took one more turn, so the day list was always drawn after the
doctor was stored (the worker resolves `flow_calendar` from the STORED doctor, before `route()`).
Now the clinic-catalog tap with one doctor offering the service, and the doctor-of-a-service tap,
go straight to the days: the worker hands `route()` the TENANT calendar (nobody is stored yet) and
the list would be computed on the clinic's hours and busy time - the wrong owner for the holds and
for the buffer.

The fix is worker-side (the router stays pure): after `route()`, when the result selects another
professional and lands on a step that consumed the calendar, the doctor's calendar is resolved
exactly as the next turn would and `route()` runs again with the same snapshot / inbound / gate -
only for a turn that cannot call an LLM. The simulator does the same with `calendar_for`.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai.formatter import SlotsBubble  # noqa: E402
from secretaria.models import FlowState, Professional, Tenant  # noqa: E402
from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.attendee import ATTENDEE_SELF  # noqa: E402
from secretaria.services.calendar import CalendarService  # noqa: E402
from secretaria.workers.shared import flow_runner  # noqa: E402
from secretaria.workers.shared.context import _ReplyContext  # noqa: E402
from tests._patching import workers_ns  # noqa: E402
from tests.payments_support import make_sessionmaker  # noqa: E402
from tests.test_booking_buffer_paths import (  # noqa: E402
    TZ,
    _Agenda,
    _pinned_clock,  # noqa: F401
    _win,
)
from tests.test_booking_order_service_first import _doctor, _tenant  # noqa: E402

TODAY = datetime.now(TZ).date()
D1, D2, D3 = (TODAY + timedelta(days=n) for n in (1, 2, 3))
CONSULTA = "Consulta"
RETORNO = "Retorno"


def _services():
    return [
        {"name": CONSULTA, "duration_min": 30, "is_active": True, "sort_order": 0},
        {"name": RETORNO, "duration_min": 30, "is_active": True, "sort_order": 1},
    ]


class _BusyAwareAgenda(_Agenda):
    """`_Agenda` whose day list drops a day with no free time - what the real agenda does (the
    plain fake lists every open day)."""

    async def list_available_days(self, start_day, days, slot_minutes=None):
        listed = await super().list_available_days(start_day, days, slot_minutes)
        return [d for d in listed if await self.list_free_slots(d, slot_minutes)]


def _clinic_agenda() -> _Agenda:
    """The TENANT calendar: open D1..D3, nothing booked - what the stale turn would use."""
    return _Agenda(open_days=(D1, D2, D3), hours=(8, 10))


def _doctor_a(buffer_minutes=None, services=None):
    doctor = _doctor("Dra. Ana", services=services if services is not None else _services())
    doctor.buffer_minutes = buffer_minutes
    return doctor


def _doctor_a_agenda(*, buffer_case=False) -> _Agenda:
    """Ana's own agenda: open only D2 and D3. By default D2 is fully booked by a patient
    appointment (08:00-10:00); with `buffer_case`, D2 is open 08:00-09:00 with a visit at 08:30."""
    if buffer_case:
        return _BusyAwareAgenda(
            open_days=(D2, D3), hours=(8, 9), consultations=[_win("08:30", D2)]
        )
    return _BusyAwareAgenda(
        open_days=(D2, D3), hours=(8, 10), consultations=[_win("08:00", D2, 120)]
    )


@pytest.fixture
async def world(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(flow_runner, "async_session_factory", maker)
    tenant_id = uuid4()
    async with maker() as session:
        session.add(
            Tenant(
                id=tenant_id,
                clinic_name="Clinica",
                phone_number_id=str(uuid4())[:12],
                is_active=True,
            )
        )
        await session.commit()
    resolved: list = []
    agendas: dict = {}

    async def _resolve(session, tenant, professional, *, tenant_config=None, **kw):
        resolved.append(professional.id)
        return agendas[professional.id]

    monkeypatch.setattr(flow_runner, "resolve_professional_calendar", _resolve)
    clinic = _clinic_agenda()
    monkeypatch.setattr(CalendarService, "from_tenant_config", staticmethod(lambda cfg: clinic))
    sent: list = []

    async def _apply(reply, result, patient_wa, **kw):
        sent.append(result)
        return True

    monkeypatch.setattr(workers_ns, "_apply_flow_result", _apply)
    yield SimpleNamespace(
        maker=maker,
        tenant_id=tenant_id,
        resolved=resolved,
        agendas=agendas,
        clinic=clinic,
        sent=sent,
    )
    await engine.dispose()


async def _seed_doctors(world, *doctors):
    async with world.maker() as session:
        for doctor in doctors:
            session.add(
                Professional(
                    id=doctor.id, tenant_id=world.tenant_id, name=doctor.name, is_active=True
                )
            )
        await session.commit()


def _conv(step, **kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=step,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_selected_unit_id=None,
        flow_managing_appointment_id=None,
        flow_attendee_name=ATTENDEE_SELF,
        flow_draft=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


async def _turn(world, conv, tenant_snapshot, doctors, body, **kw):
    """The REAL worker entry for a flow turn: `_run_flow` (calendar resolved before `route()`)."""
    async with world.maker() as session:
        tenant = await session.get(Tenant, world.tenant_id)
    reply = _ReplyContext(
        conversation_id=conv.id, patient_ref="5511988887777", inbound_body=body
    )
    await flow_runner._run_flow(
        reply,
        conv,
        tenant_snapshot,
        SimpleNamespace(id=world.tenant_id),
        "Maria",
        "5511988887777",
        tenant=tenant,
        professionals=doctors,
        flow_calendar=kw.pop("flow_calendar", None),
        **kw,
    )
    return world.sent[-1]


def _day_ids(result) -> list[str]:
    [bubble] = [b for b in result.bubbles if isinstance(b, SlotsBubble)]
    return [row[0].split("|")[1] for row in bubble.rows if row[0].startswith("day|")]


def _router_tenant(doctors):
    tenant = _tenant(collect=False, doctors=doctors)
    tenant.appointment_types = _services()
    return tenant


async def _catalog_tap(world, doctors):
    tenant = _router_tenant(doctors)
    conv = _conv(fr.STEP_AWAITING_CATALOG_SERVICE)
    tap = inbound_routing_text(CONSULTA, f"svc|{CONSULTA}")
    return await _turn(world, conv, tenant, doctors, tap)


# --------------------------------------------------------------------------
# 1. The clinic catalog: ONE doctor offers the service
# --------------------------------------------------------------------------


async def test_catalog_tap_with_one_doctor_lists_that_doctors_days(world):
    ana = _doctor_a()
    bruno = _doctor("Dr. Bruno", services=[s for s in _services() if s["name"] != CONSULTA])
    await _seed_doctors(world, ana, bruno)
    world.agendas[ana.id] = _doctor_a_agenda()
    result = await _catalog_tap(world, [ana, bruno])
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_professional_id == ana.id
    # Ana opens D2 and D3 only, and D2 is fully booked by a patient: her days are [D3] - the
    # clinic's own list would have been D1, D2, D3.
    assert _day_ids(result) == [D3.isoformat()]
    assert world.resolved == [ana.id]


# --------------------------------------------------------------------------
# 2. The doctors of a service: TWO offer it, the patient taps one
# --------------------------------------------------------------------------


async def test_doctor_of_service_tap_lists_that_doctors_days(world):
    ana, bruno = _doctor_a(), _doctor("Dr. Bruno")
    await _seed_doctors(world, ana, bruno)
    world.agendas[ana.id] = _doctor_a_agenda()
    tenant = _router_tenant([ana, bruno])
    conv = _conv(fr.STEP_AWAITING_SERVICE_PROFESSIONAL, flow_selected_type=CONSULTA)
    result = await _turn(
        world, conv, tenant, [ana, bruno], inbound_routing_text("Dra. Ana", f"prof|{ana.id}")
    )
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_professional_id == ana.id
    assert _day_ids(result) == [D3.isoformat()]


# --------------------------------------------------------------------------
# 3. The buffer of the doctor (Task 5) is already on the day list of that turn
# --------------------------------------------------------------------------


@pytest.mark.parametrize("buffer_minutes, expected", [(0, [D2, D3]), (30, [D3])])
async def test_the_doctors_buffer_pads_the_day_list_of_the_same_turn(
    world, buffer_minutes, expected
):
    ana = _doctor_a(buffer_minutes=buffer_minutes)
    bruno = _doctor("Dr. Bruno", services=[s for s in _services() if s["name"] != CONSULTA])
    await _seed_doctors(world, ana, bruno)
    world.agendas[ana.id] = _doctor_a_agenda(buffer_case=True)
    result = await _catalog_tap(world, [ana, bruno])
    assert result.flow_step == fr.STEP_AWAITING_DAY
    # D2 is open 08:00-09:00 with a visit at 08:30: its only free time (08:00) sits next to it,
    # so a 30-minute buffer drops the day - on the doctor's agenda, never the clinic's.
    assert _day_ids(result) == [d.isoformat() for d in expected]


# --------------------------------------------------------------------------
# What must NOT change
# --------------------------------------------------------------------------


async def test_a_stored_doctor_costs_no_second_route_and_no_re_resolution(world):
    ana, bruno = _doctor_a(), _doctor("Dr. Bruno")
    await _seed_doctors(world, ana, bruno)
    agenda = _doctor_a_agenda()
    tenant = _router_tenant([ana, bruno])
    conv = _conv(
        fr.STEP_AWAITING_SERVICE,
        flow_selected_professional_id=ana.id,
    )
    result = await _turn(
        world,
        conv,
        tenant,
        [ana, bruno],
        inbound_routing_text(CONSULTA, f"svc|{CONSULTA}"),
        flow_calendar=agenda,
    )
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert _day_ids(result) == [D3.isoformat()]
    assert world.resolved == []  # the stored doctor's calendar was already the turn's


async def test_a_turn_that_does_not_reach_the_days_resolves_nothing(world):
    ana, bruno = _doctor_a(), _doctor("Dr. Bruno")
    await _seed_doctors(world, ana, bruno)
    tenant = _router_tenant([ana, bruno])
    # Two doctors do it: the tap lands on the doctor list, which consumes no calendar.
    conv = _conv(fr.STEP_AWAITING_CATALOG_SERVICE)
    result = await _turn(
        world, conv, tenant, [ana, bruno], inbound_routing_text(CONSULTA, f"svc|{CONSULTA}")
    )
    assert result.flow_step == fr.STEP_AWAITING_SERVICE_PROFESSIONAL
    assert world.resolved == []


async def test_a_failed_calendar_resolution_degrades_like_the_next_turn(world, monkeypatch):
    ana = _doctor_a()
    bruno = _doctor("Dr. Bruno", services=[s for s in _services() if s["name"] != CONSULTA])
    await _seed_doctors(world, ana, bruno)

    async def _boom(*args, **kwargs):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(flow_runner, "resolve_professional_calendar", _boom)
    result = await _catalog_tap(world, [ana, bruno])
    # No agenda for Ana: the router answers "calendar unavailable", never the clinic's list.
    assert result.action != "reply" or not _day_ids(result)


async def test_a_help_node_turn_is_never_run_twice(world, monkeypatch):
    # Documented choice (R41): a turn on a help step (it can call the LLM) is never re-run. It also
    # never needs to be: the service help is entered with the doctor already stored (the doctor's
    # own list), so its calendar is already the doctor's; the doctor help lands on that doctor's
    # service LIST, which consumes no calendar.
    ana, bruno = _doctor_a(), _doctor("Dr. Bruno")
    await _seed_doctors(world, ana, bruno)
    calls = []

    async def _help(**kwargs):
        calls.append(kwargs["patient_message"])
        return SimpleNamespace(
            kind="pick", choice="Dra. Ana", message=None, question=None
        )

    monkeypatch.setattr(fr, "run_professional_help", _help)
    tenant = _router_tenant([ana, bruno])
    conv = _conv(fr.STEP_PROFESSIONAL_HELP)
    result = await _turn(world, conv, tenant, [ana, bruno], "quero a Dra. Ana")
    assert calls == ["quero a Dra. Ana"]  # one LLM call, one route()
    assert result.flow_step == fr.STEP_AWAITING_SERVICE  # a LIST: no calendar consumed
    assert world.resolved == []


async def test_manage_turns_never_switch_calendars(world):
    ana, bruno = _doctor_a(), _doctor("Dr. Bruno")
    await _seed_doctors(world, ana, bruno)
    owned = _Agenda(open_days=(D1,), hours=(8, 10))
    tenant = _router_tenant([ana, bruno])
    conv = _conv(fr.STEP_AWAITING_CATALOG_SERVICE)
    result = await _turn(
        world,
        conv,
        tenant,
        [ana, bruno],
        inbound_routing_text(CONSULTA, f"svc|{CONSULTA}"),
        manage_calendar=owned,
        manage_calendar_owned=True,
    )
    assert result is not None
    assert world.resolved == []


# --------------------------------------------------------------------------
# 4. The simulator: the same journey through `simulate_turn`
# --------------------------------------------------------------------------


async def test_the_simulator_lists_the_picked_doctors_days_too():
    from secretaria.workers.shared.flow_simulation import (  # noqa: PLC0415
        initial_state,
        simulate_turn,
    )

    ana = _doctor_a()
    bruno = _doctor("Dr. Bruno", services=[s for s in _services() if s["name"] != CONSULTA])
    tenant = _router_tenant([ana, bruno])
    clinic, anas = _clinic_agenda(), _doctor_a_agenda()
    asked: list = []

    def calendar_for(professional_id):
        asked.append(professional_id)
        return anas if professional_id == str(ana.id) or professional_id == ana.id else clinic

    state = initial_state()
    state.update(
        flow_state=FlowState.SERVICE_CATALOG.value,
        flow_step=fr.STEP_AWAITING_CATALOG_SERVICE,
        flow_attendee_name=ATTENDEE_SELF,
    )
    turn = await simulate_turn(
        tenant=tenant,
        professionals=[ana, bruno],
        calendar_for=calendar_for,
        state=state,
        tap={"title": CONSULTA, "id": f"svc|{CONSULTA}"},
    )
    days = [
        option["id"].split("|")[1]
        for bubble in turn.bubbles
        if bubble["kind"] == "list"
        for option in bubble["options"]
        if option["id"].startswith("day|")
    ]
    assert days == [D3.isoformat()]
    assert str(ana.id) in [str(p) for p in asked]
