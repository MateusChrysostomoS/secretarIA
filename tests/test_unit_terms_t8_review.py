# ruff: noqa: F811
"""TASK-085 T8 final review - the backend findings fixed after the review.

I1 (D7): the AI v1 booking tools (the default; `ai_draft_v2_enabled` off) wrote the window the
model sent, so a unit with its OWN duration got the base one (Centro 40 min booked as 30). Now
the server sizes the event with the service's duration at that unit, the free slots the model
reads are walked in that duration, and `get_availability` says each window's duration when the
units differ. Without an own duration (or without a unit) nothing changes (characterization).

The clinic (tests/test_unit_terms_ai_db.py): Dra. Ana does "Consulta" (base R$ 200,00 / 30
min) at Sede (08-09h) and Centro (10-11h), every day.
"""

import os

os.environ.setdefault("APP_ENV", "test")

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.ai import scoped_help, tools as ai_tools  # noqa: E402
from secretaria.ai.tool_output import allowlisted  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
)
from secretaria.plugins import multi_professional as mp, multi_unit as mu  # noqa: E402
from secretaria.services import booking_hold, flow_router as fr  # noqa: E402
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_MULTI  # noqa: E402
from secretaria.services.local_calendar import LocalCalendarService  # noqa: E402
from secretaria.services.payments import service_change  # noqa: E402
from secretaria.services.service_terms import UnitTerms  # noqa: E402
from secretaria.services.unit_booking import UnitOption, unit_place_label  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import actions  # noqa: E402
from tests import (  # noqa: E402
    test_t086_flow_matrix as m086,
    test_unit_terms_ai_db as base,
)
from tests._local_agenda import wire_local_agenda  # noqa: E402
from tests._patching import workers_ns  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

CENTRO_40 = {"Centro": (None, 40)}
SEDE_HOURS, CENTRO_HOURS = base.SEDE_HOURS, base.CENTRO_HOURS


def _day() -> str:
    """A day well inside the booking window, in the clinic's calendar (never "today")."""
    return (base._clinic_today() + timedelta(days=2)).isoformat()


def _window(start: str, end: str) -> dict:
    day = _day()
    return {"start": f"{day}T{start}:00", "end": f"{day}T{end}:00", "summary": "Consulta"}


async def _wired(db, monkeypatch, w):
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(booking_hold, "async_session_factory", db)
    wire_local_agenda(monkeypatch, db)
    config = await base._config(db, w)
    return config, LocalCalendarService.for_config(config, professional_id=w.ana.id)


async def _invoke(db, monkeypatch, w, tool, args: dict, *, topology=None):
    config, agenda = await _wired(db, monkeypatch, w)
    tokens = (
        base._ctx(config) if topology is None else base._ctx(config, topology=topology)
    )
    tokens.append((ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(agenda)))
    try:
        return await tool.ainvoke(args)
    finally:
        base._reset(tokens)


async def _booked_minutes(db) -> list[tuple[str, int]]:
    async with db() as session:
        rows = list(await session.scalars(select(Appointment).order_by(Appointment.start_at)))
    return [
        (row.start_at.strftime("%H:%M"), int((row.end_at - row.start_at).total_seconds() // 60))
        for row in rows
    ]


# ============================================================ I1 create_event_at_unit (v1)


@pytest.mark.asyncio
async def test_i1_create_event_at_unit_books_the_units_own_duration(db, monkeypatch):
    """SPEC §10 item 4 on the AI v1 path: Centro 40 min (base 30) -> a 40-min event."""
    w = await base._world(db, exceptions=CENTRO_40)
    booked = await _invoke(
        db,
        monkeypatch,
        w,
        mu.create_event_at_unit,
        {"unit_name": "Centro", "appointment_type": "Consulta", **_window("10:00", "10:30")},
    )
    assert "id" in booked, booked
    assert await _booked_minutes(db) == [("10:00", 40)]


@pytest.mark.asyncio
async def test_i1_create_event_at_unit_without_own_duration_keeps_the_window(db, monkeypatch):
    """Characterization: Sede has no row -> the model's window, exactly as before."""
    w = await base._world(db, exceptions=CENTRO_40)
    booked = await _invoke(
        db,
        monkeypatch,
        w,
        mu.create_event_at_unit,
        {"unit_name": "Sede", "appointment_type": "Consulta", **_window("08:00", "08:30")},
    )
    assert "id" in booked, booked
    assert await _booked_minutes(db) == [("08:00", 30)]


@pytest.mark.asyncio
async def test_i1_create_event_at_unit_refuses_when_the_own_duration_leaves_the_hours(
    db, monkeypatch
):
    """10:30 + 40 min ends 11:10, after Centro closes at 11:00: refused, nothing written."""
    w = await base._world(db, exceptions=CENTRO_40)
    refused = await _invoke(
        db,
        monkeypatch,
        w,
        mu.create_event_at_unit,
        {"unit_name": "Centro", "appointment_type": "Consulta", **_window("10:30", "11:00")},
    )
    assert refused["error"].startswith("Esse horário está fora do atendimento")
    assert await _booked_minutes(db) == []


# ================================================================ I1 base create_event (v1)


@pytest.mark.asyncio
async def test_i1_base_create_event_books_the_only_units_own_duration(db, monkeypatch):
    w = await base._world(db, units=("Centro",), exceptions=CENTRO_40)
    booked = await _invoke(
        db,
        monkeypatch,
        w,
        ai_tools.create_event,
        {"appointment_type": "Consulta", **_window("10:00", "10:30")},
    )
    assert "id" in booked, booked
    assert await _booked_minutes(db) == [("10:00", 40)]
    # The patient's add-to-calendar link carries the same 40 minutes.
    dates = booked["patient_calendar_link"].split("dates=")[1].split("&")[0]
    start, end = dates.split("/")
    assert (start[9:13], end[9:13]) == ("1300", "1340")  # 10:00-10:40 in Sao Paulo, as UTC


@pytest.mark.asyncio
async def test_i1_base_create_event_without_rows_keeps_the_window(db, monkeypatch):
    """Characterization: one unit, no exception -> the model's 30 minutes."""
    w = await base._world(db, units=("Centro",))
    booked = await _invoke(
        db,
        monkeypatch,
        w,
        ai_tools.create_event,
        {"appointment_type": "Consulta", **_window("10:00", "10:30")},
    )
    assert "id" in booked, booked
    assert await _booked_minutes(db) == [("10:00", 30)]


@pytest.mark.asyncio
async def test_i1_base_create_event_without_a_unit_is_the_base(db, monkeypatch):
    """D7: no unit known -> the base, even with a Centro row stored for the service."""
    w = await base._world(db, units=(), exceptions=CENTRO_40)
    booked = await _invoke(
        db,
        monkeypatch,
        w,
        ai_tools.create_event,
        {"appointment_type": "Consulta", **_window("10:00", "10:30")},
    )
    assert "id" in booked, booked
    assert await _booked_minutes(db) == [("10:00", 30)]


# ============================================= I1 create_event_for_professional (multi, v1)


async def _invoke_professional(db, monkeypatch, w, tool, args: dict):
    config, agenda = await _wired(db, monkeypatch, w)

    async def _calendar(_tenant_id, _professional):
        return agenda

    monkeypatch.setattr(mp, "_professional_calendar", _calendar)
    tokens = base._ctx(config, topology=BOOKING_TOPOLOGY_MULTI)
    try:
        return await tool.ainvoke(args)
    finally:
        base._reset(tokens)


@pytest.mark.asyncio
async def test_i1_create_event_for_professional_books_the_units_own_duration(db, monkeypatch):
    w = await base._world(db, second=True, exceptions=CENTRO_40)
    booked = await _invoke_professional(
        db,
        monkeypatch,
        w,
        mp.create_event_for_professional,
        {
            "professional_name": "Dra. Ana",
            "unit_name": "Centro",
            "appointment_type": "Consulta",
            **_window("10:00", "10:30"),
        },
    )
    assert "id" in booked, booked
    assert await _booked_minutes(db) == [("10:00", 40)]


@pytest.mark.asyncio
async def test_i1_create_event_for_professional_at_sede_keeps_the_window(db, monkeypatch):
    w = await base._world(db, second=True, exceptions=CENTRO_40)
    booked = await _invoke_professional(
        db,
        monkeypatch,
        w,
        mp.create_event_for_professional,
        {
            "professional_name": "Dra. Ana",
            "unit_name": "Sede",
            "appointment_type": "Consulta",
            **_window("08:00", "08:30"),
        },
    )
    assert "id" in booked, booked
    assert await _booked_minutes(db) == [("08:00", 30)]


# =========================================================== I1 list_free_slots (v1 reads)


def _times(result: dict) -> list[tuple[str, str]]:
    return [(s["start"][11:16], s["end"][11:16]) for s in result["slots"]]


@pytest.mark.asyncio
async def test_i1_list_free_slots_walks_the_only_units_own_duration(db, monkeypatch):
    w = await base._world(db, units=("Centro",), exceptions=CENTRO_40)
    result = await _invoke(db, monkeypatch, w, ai_tools.list_free_slots, {"day": _day()})
    assert _times(result) == [("10:00", "10:40")]


@pytest.mark.asyncio
async def test_i1_list_free_slots_without_rows_is_todays(db, monkeypatch):
    w = await base._world(db, units=("Centro",))
    result = await _invoke(db, monkeypatch, w, ai_tools.list_free_slots, {"day": _day()})
    assert _times(result) == [("10:00", "10:30"), ("10:30", "11:00")]


@pytest.mark.asyncio
async def test_i1_list_free_slots_for_professional_walks_the_units_duration(db, monkeypatch):
    w = await base._world(db, second=True, exceptions=CENTRO_40)
    centro = await _invoke_professional(
        db,
        monkeypatch,
        w,
        mp.list_free_slots_for_professional,
        {"professional_name": "Dra. Ana", "day": _day(), "unit_name": "Centro"},
    )
    sede = await _invoke_professional(
        db,
        monkeypatch,
        w,
        mp.list_free_slots_for_professional,
        {"professional_name": "Dra. Ana", "day": _day(), "unit_name": "Sede"},
    )
    assert _times(centro) == [("10:00", "10:40")]
    assert _times(sede) == [("08:00", "08:30"), ("08:30", "09:00")]


# ================================================== I1 get_availability per-window duration


@pytest.mark.asyncio
async def test_i1_availability_says_each_windows_duration_when_units_differ(db, monkeypatch):
    w = await base._world(db, exceptions=CENTRO_40)
    result = await base._availability(db, monkeypatch, w)
    out = allowlisted("get_availability", result)
    assert out == result  # every key declared
    by_unit = {(x["unit"], x.get("slot_minutes")) for x in out["windows"]}
    assert by_unit == {("Sede", 30), ("Centro", 40)}
    assert out["slot_minutes"] == 30  # the top level keeps the base (D7)


@pytest.mark.asyncio
async def test_i1_availability_same_duration_everywhere_has_no_per_window_duration(
    db, monkeypatch
):
    """Characterization: no row -> today's windows, without a per-window duration."""
    result = await base._availability(db, monkeypatch, await base._world(db))
    assert all("slot_minutes" not in x for x in result["windows"])
    assert result["slot_minutes"] == 30
    one = await base._availability(
        db, monkeypatch, await base._world(db, units=("Centro",), exceptions=CENTRO_40)
    )
    assert all("slot_minutes" not in x for x in one["windows"])
    assert one["slot_minutes"] == 40


def test_i1_the_per_window_duration_is_declared_for_the_model():
    answer = {
        "windows": [
            {"day": "2026-10-06", "start": "10:00", "end": "11:00", "unit": "Centro",
             "slot_minutes": 40}
        ],
        "slot_minutes": 30,
    }
    assert allowlisted("get_availability", answer) == answer


# ================================================== (b) rebooking after a clinic cancellation
#
# "Outro horario" / "Outro medico" after the clinic cancelled: the day list was scanned with the
# clinic's base duration on an agenda without the unit, while the next step (the slot list) is
# sized at the unit - a day could look free and open an empty slot list. Now the cancelled
# consultation's unit (when the doctor still offers it) or the doctor's only unit sizes and
# scopes the day list, and that unit is kept for the next step.

_SERVICE_ID = uuid4()
_CENTRO = UnitOption(id=uuid4(), name="Centro", address="Rua B, 2", business_hours=CENTRO_HOURS)
_SEDE = UnitOption(id=uuid4(), name="Sede", address="Rua A, 1", business_hours=SEDE_HOURS)


def _rb_tenant():
    return SimpleNamespace(
        initial_flows={"buttons": ["Agendar", "Outro"]},
        appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
        appointment_duration_min=30,
        business_hours=base.ALL_DAY,
        collect_insurance=False,
        insurances=None,
    )


def _rb_doctor(name="Dra. Ana", *, units=(_SEDE, _CENTRO), centro_minutes=40):
    terms = {_SERVICE_ID: {_CENTRO.id: UnitTerms(None, centro_minutes)}} if centro_minutes else {}
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        min_booking_notice_hours=None,
        business_hours=None,
        appointment_types=[
            {
                "name": "Consulta",
                "service_id": str(_SERVICE_ID),
                "duration_min": 30,
                "is_active": True,
            }
        ],
        units=list(units),
        service_units={},
        service_unit_terms=terms,
    )


def _rb_conversation():
    return SimpleNamespace(
        id=uuid4(),
        flow_state=FlowState.IDLE,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_selected_unit_id=None,
        flow_managing_appointment_id=None,
        patient_id=None,
    )


class _ScopedDays:
    """Has days free; records the slot length and the unit each day scan was made at."""

    def __init__(self, unit=None, log=None):
        self.tzinfo = ZoneInfo(base.TZ)
        self.unit = unit
        self.log = log if log is not None else []

    def with_unit(self, unit_id, location, *, business_hours=None):
        return _ScopedDays(unit_id, self.log)

    async def list_available_days(self, start_day, days, slot_minutes=None, **_kw):
        self.log.append((self.unit, slot_minutes))
        first = datetime(2027, 3, 1, tzinfo=self.tzinfo)
        return [first + timedelta(days=n) for n in range(3)]


async def _rebook(doctors, *, same, cancelled, unit_id, candidates=None):
    calendar = _ScopedDays()
    result = await fr.enter_rebooking(
        _rb_conversation(),
        _rb_tenant(),
        calendar,
        professionals=doctors,
        cancelled_professional_id=cancelled.id,
        service_name="Consulta",
        same_professional=same,
        candidates=candidates,
        cancelled_unit_id=unit_id,
    )
    return result, calendar.log


@pytest.mark.asyncio
async def test_b_rebooking_same_doctor_at_centro_scans_its_own_duration():
    ana = _rb_doctor()
    result, scans = await _rebook([ana], same=True, cancelled=ana, unit_id=_CENTRO.id)
    assert scans and all(scan == (_CENTRO.id, 40) for scan in scans)
    assert result.flow_selected_unit_id == _CENTRO.id
    assert result.flow_selected_professional_id == ana.id


@pytest.mark.asyncio
async def test_b_rebooking_same_doctor_at_sede_keeps_the_base():
    ana = _rb_doctor()
    result, scans = await _rebook([ana], same=True, cancelled=ana, unit_id=_SEDE.id)
    assert scans and all(scan == (_SEDE.id, 30) for scan in scans)
    assert result.flow_selected_unit_id == _SEDE.id


@pytest.mark.asyncio
async def test_b_rebooking_without_a_unit_and_two_units_is_todays():
    """Characterization: no unit on the booking, two units -> base, no unit kept (D7)."""
    ana = _rb_doctor()
    result, scans = await _rebook([ana], same=True, cancelled=ana, unit_id=None)
    assert scans and all(scan == (None, 30) for scan in scans)
    assert result.flow_selected_unit_id is None


@pytest.mark.asyncio
async def test_b_rebooking_the_only_unit_uses_its_duration():
    ana = _rb_doctor(units=(_CENTRO,))
    result, scans = await _rebook([ana], same=True, cancelled=ana, unit_id=None)
    assert scans and all(scan == (_CENTRO.id, 40) for scan in scans)
    assert result.flow_selected_unit_id == _CENTRO.id


@pytest.mark.asyncio
async def test_b_rebooking_with_a_doctor_without_units_is_todays():
    """Characterization: no unit link at all -> the clinic's base and the agenda as is."""
    ana = _rb_doctor(units=(), centro_minutes=None)
    result, scans = await _rebook([ana], same=True, cancelled=ana, unit_id=_CENTRO.id)
    assert scans and all(scan == (None, 30) for scan in scans)
    assert result.flow_selected_unit_id is None


@pytest.mark.asyncio
async def test_b_rebooking_another_doctor_at_the_cancelled_unit_uses_theirs():
    ana, bia = _rb_doctor(), _rb_doctor("Dra. Bia", centro_minutes=50)
    result, scans = await _rebook(
        [ana, bia], same=False, cancelled=ana, unit_id=_CENTRO.id, candidates=[bia]
    )
    assert scans and all(scan == (_CENTRO.id, 50) for scan in scans)
    assert (result.flow_selected_professional_id, result.flow_selected_unit_id) == (
        bia.id,
        _CENTRO.id,
    )


@pytest.mark.asyncio
async def test_b_the_rebooking_tap_hands_the_router_the_units_and_the_cancelled_unit(
    db, monkeypatch
):
    """The worker's snapshot carries the units and the per-unit terms (attach_unit_options),
    and the router gets the cancelled consultation's unit."""
    w = await base._world(db, exceptions=CENTRO_40)
    async with db() as session:
        patient = Patient(id=uuid4(), tenant_id=w.tenant.id, wa_id="5511999990000")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=w.tenant.id, patient_id=patient.id)
        session.add(conversation)
        start = datetime.now(UTC) + timedelta(days=2)
        appointment = Appointment(
            tenant_id=w.tenant.id,
            patient_id=patient.id,
            professional_id=w.ana.id,
            unit_id=w.centro.id,
            google_event_id="",
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=40),
            status=AppointmentStatus.CANCELLED,
        )
        session.add(appointment)
        await session.commit()

    captured: dict = {}

    async def _enter(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(flow_state=FlowState.SERVICE_CATALOG)

    async def _noop(*_args, **_kwargs):
        return None

    async def _token(_session, _tenant_id):
        return "token"

    class _Client:
        persists_outbound = False

        @classmethod
        def for_tenant(cls, tenant, waba_token):
            return cls()

        async def send_text_message(self, to, body):
            return {}

    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "WhatsAppClient", _Client)
    monkeypatch.setattr(workers_ns, "get_waba_token", _token)
    monkeypatch.setattr(actions, "enter_rebooking", _enter)
    monkeypatch.setattr(actions, "_apply_flow_result", _noop)
    monkeypatch.setattr(actions, "_appointment_calendar", _noop)
    reply = tasks._ReplyContext(
        conversation_id=conversation.id, patient_ref="5511999990000", inbound_body=""
    )
    await actions._handle_action_button(reply, "rebooksame", str(appointment.id))

    assert captured["cancelled_unit_id"] == w.centro.id
    [ana] = captured["professionals"]
    assert {unit.name for unit in ana.units} == {"Sede", "Centro"}
    assert ana.service_unit_terms[w.service.id][w.centro.id].duration_min == 40


# ======================================== (c) the AI service help reads the unit already chosen
#
# D3 still holds BEFORE the unit (a price that differs between the units is never said); once
# the patient picked the unit, the help may say that unit's price - as the service card does.

SEDE_200_CENTRO_250 = {"Centro": ("R$ 250,00", None)}


def _priced_doctor():
    ana = _rb_doctor(centro_minutes=None)
    ana.appointment_types[0]["price"] = "R$ 200,00"
    ana.service_unit_terms = {_SERVICE_ID: {_CENTRO.id: UnitTerms("R$ 250,00", None)}}
    return ana


async def _router_help(monkeypatch, *, unit_id):
    seen: dict = {}

    async def _fake_help(**kwargs):
        seen.update(kwargs)
        from secretaria.ai.scoped_help import ScopedHelpOutcome

        return ScopedHelpOutcome(kind="escalate")

    monkeypatch.setattr(fr, "run_service_help", _fake_help)
    ana = _priced_doctor()
    conversation = _rb_conversation()
    conversation.flow_step = fr.STEP_SERVICE_HELP
    conversation.flow_selected_professional_id = ana.id
    conversation.flow_selected_unit_id = unit_id
    await fr._handle_service_help(
        conversation, _rb_tenant(), "quanto custa?", ana.appointment_types, ana
    )
    [service] = seen["services"]
    return service.get("price")


@pytest.mark.asyncio
async def test_c_router_help_hides_a_differing_price_before_the_unit(monkeypatch):
    assert await _router_help(monkeypatch, unit_id=None) is None


@pytest.mark.asyncio
async def test_c_router_help_never_hands_the_model_a_price_even_at_a_chosen_unit(monkeypatch):
    # TASK-088 R38 ("valor só no fim"): it used to hand the chosen unit's price; now none.
    assert await _router_help(monkeypatch, unit_id=_CENTRO.id) is None
    assert await _router_help(monkeypatch, unit_id=_SEDE.id) is None


async def _help_block(db, monkeypatch, w, *, unit=None) -> str:
    """The scoped-help options block for Ana's list over a real conversation."""
    async with db() as session:
        patient = Patient(tenant_id=w.tenant.id, wa_id=f"55119{uuid4().int % 10**8:08d}")
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=w.tenant.id,
            patient_id=patient.id,
            flow_selected_professional_id=w.ana.id,
            flow_selected_unit_id=unit.id if unit is not None else None,
        )
        session.add(conversation)
        await session.commit()
    fake = base._FakeModel()
    monkeypatch.setattr(scoped_help, "_get_decision_model", lambda: fake)
    monkeypatch.setattr(scoped_help, "async_session_factory", db)

    async def _load(_cid):
        from pseudonymize_core import Pseudonymizer

        return Pseudonymizer()

    async def _persist(_cid, _p):
        return None

    monkeypatch.setattr(scoped_help, "load_pseudonymizer", _load)
    monkeypatch.setattr(scoped_help, "persist_pseudonymizer", _persist)
    entry = {**base._ana_entry(w.service.id)}
    if unit is not None and unit.name == "Centro":
        entry["price"] = "R$ 250,00"  # what the router's _priced_entry hands at Centro
    await scoped_help.run_service_help(
        conversation_id=conversation.id,
        services=[entry],
        patient_message="quanto custa?",
        final_round=False,
    )
    prompt = fake.calls[0][0].content
    return prompt.split("Serviços disponíveis (os ÚNICOS que existem):\n")[1].split("\n\n")[0]


@pytest.mark.asyncio
async def test_c_scoped_help_before_the_unit_keeps_d3(db, monkeypatch):
    w = await base._world(db, exceptions=SEDE_200_CENTRO_250)
    block = await _help_block(db, monkeypatch, w)
    assert block.startswith("- Consulta — valor depende da unidade escolhida")
    assert "R$" not in block


@pytest.mark.asyncio
async def test_c_scoped_help_after_the_unit_says_that_units_price(db, monkeypatch):
    w = await base._world(db, exceptions=SEDE_200_CENTRO_250)
    assert (await _help_block(db, monkeypatch, w, unit=w.centro)).startswith(
        "- Consulta — R$ 250,00"
    )
    assert (await _help_block(db, monkeypatch, w, unit=w.sede)).startswith(
        "- Consulta — R$ 200,00"
    )


# ========================================== M3: one rule for "na Unidade X" (never doubled)


@pytest.mark.parametrize(
    ("name", "place"),
    [
        ("Centro", "Unidade Centro"),
        ("  Sede ", "Unidade Sede"),
        ("Unidade Centro", "Unidade Centro"),
        ("unidade norte", "unidade norte"),
        ("Unidades Reunidas", "Unidades Reunidas"),
        ("Unidadeum", "Unidade Unidadeum"),
    ],
)
def test_m3_unit_place_label(name, place):
    assert unit_place_label(name) == place


@pytest.mark.parametrize("name", ["Centro", "Unidade Centro", "Unidades Reunidas"])
def test_m3_the_three_texts_name_the_unit_alike(name):
    place = unit_place_label(name)
    assert service_change.unit_change_label("Consulta", name) == f"Consulta na {place}"
    line = fr._unit_terms_text("Consulta", SimpleNamespace(name=name), None, 30)
    assert line.startswith(f"Consulta na {place}:")
    assert service_change._unit_label("u1", {"u1": name}) == place


# ========================== cosmetic: "R$" and "min" never broken from their number (NBSP)

NBSP = "\u00a0"


def test_terms_line_keeps_the_money_and_minutes_together():
    centro = SimpleNamespace(name="Centro")
    assert fr._unit_terms_text("Consulta", centro, "R$ 250,00", 40) == (
        f"Consulta na Unidade Centro: R${NBSP}250,00 · 40{NBSP}min"
    )
    assert fr._unit_terms_text("Consulta", centro, None, 40) == (
        f"Consulta na Unidade Centro: 40{NBSP}min"
    )


# ============== TASK-086 T9 review minor 6: the final convenio guard with the step turned off
#
# The clinic turned the convenio question off while the conversation still carried a convenio
# that the matrix now refuses at the confirmation. The guard used to reopen the convenio list -
# a question the clinic no longer asks. It now opens the "nao atende este convenio" step (other
# doctor / other service; never "Trocar convenio" with the step off), nothing booked.


@pytest.mark.asyncio
async def test_t086_final_guard_with_the_convenio_step_off_never_reopens_the_list():
    centro, sul, _norte = m086._units()
    ana = m086._doctor(units=[centro, sul])
    tenant = m086._tenant("shared", m086._matrix("shared", ana, (m086.ECO_ID, sul, m086.UNIMED)))
    tenant.collect_insurance = False
    agenda = m086._Agenda()
    gate = m086._Gate(armed=False)
    result = await fr.route(
        m086._confirming(sul, ana),
        tenant,
        agenda,
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=[ana],
        gate=gate,
    )
    assert result.appointment is None and result.booking_hold is None
    assert agenda.log["created"] == [] and gate.decided == []
    assert result.flow_step == fr.STEP_AWAITING_PLAN_REFUSED
    assert (result.flow_selected_type, result.flow_selected_professional_id) == (
        "Ecocardiograma",
        ana.id,
    )
    assert result.flow_selected_unit_id is None
    [menu] = result.bubbles
    assert fr.LABEL_PLAN_CHANGE_INSURANCE not in menu.labels
    assert fr.LABEL_PLAN_OTHER_SERVICE in menu.labels
    assert menu.body.startswith(
        "Desculpe, o convênio Unimed não é mais aceito para Ecocardiograma com Dra. Ana na "
        "unidade Zona Sul. Nada foi agendado."
    )
    assert "Escolha outro convênio" not in menu.body


@pytest.mark.asyncio
async def test_t086_final_guard_with_the_convenio_step_on_is_unchanged():
    """Characterization: the step on -> the convenio list reopens, as before."""
    centro, sul, _norte = m086._units()
    ana = m086._doctor(units=[centro, sul])
    tenant = m086._tenant("shared", m086._matrix("shared", ana, (m086.ECO_ID, sul, m086.UNIMED)))
    result = await fr.route(
        m086._confirming(sul, ana),
        tenant,
        m086._Agenda(),
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=[ana],
        gate=m086._Gate(armed=False),
    )
    assert result.flow_step == fr.STEP_AWAITING_INSURANCE
    assert result.bubbles[0].body.endswith("Escolha outro convênio ou Particular:")
