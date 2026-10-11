"""The flow simulator walks the REAL booking flow with no side effects (TASK-027)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from secretaria.core import database as core_database
from secretaria.workers.shared.flow_simulation import (
    SimCalendar,
    SimulationTurn,
    initial_state,
    simulate_turn,
)

ALL_DAYS = {
    day: [{"start": "08:00", "end": "17:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


def _tenant(business_hours=None, **overrides) -> SimpleNamespace:
    base = dict(
        initial_flows={
            "enabled": True,
            "buttons": ["Serviços e Custo", "Horários", "Outro"],
            "menu_label": "Como posso ajudar?",
        },
        appointment_types=[
            {
                "name": "Primeira Consulta",
                "duration_min": 40,
                "is_active": True,
                "sort_order": 0,
                "price": "R$ 250",
                "long_description": "Avaliação completa.",
                "requirements": ["Trazer exames anteriores"],
            }
        ],
        appointment_duration_min=30,
        business_hours=ALL_DAYS if business_hours is None else business_hours,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Calendars:
    """Hands out one SimCalendar per call and remembers them, to inspect their writes."""

    def __init__(self, tenant) -> None:
        self.tenant = tenant
        self.made: list[SimCalendar] = []

    def __call__(self, professional_id):
        calendar = SimCalendar(
            timezone="America/Sao_Paulo",
            business_hours=self.tenant.business_hours,
            slot_minutes=self.tenant.appointment_duration_min,
        )
        self.made.append(calendar)
        return calendar


@pytest.fixture(autouse=True)
def _no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("the simulation must never open a database session")

    monkeypatch.setattr(core_database, "async_session_factory", forbidden)


async def _turn(tenant, calendars, state=None, tap=None, text=None) -> SimulationTurn:
    return await simulate_turn(
        tenant=tenant, professionals=None, calendar_for=calendars, state=state, tap=tap, text=text
    )


def _first_option(turn: SimulationTurn) -> dict[str, str]:
    option = turn.bubbles[-1]["options"][0]
    return {"id": option["id"], "title": option["title"]}


async def _walk_to_the_end(tenant, calendars, limit: int = 12) -> list[SimulationTurn]:
    turns = [await _turn(tenant, calendars)]
    while not turns[-1].finished and len(turns) < limit:
        tap = _first_option(turns[-1])
        turns.append(await _turn(tenant, calendars, state=turns[-1].state, tap=tap))
    return turns


async def test_starting_the_flow_asks_who_the_appointment_is_for() -> None:
    tenant = _tenant()
    turn = await _turn(tenant, _Calendars(tenant))
    assert turn.bubbles and turn.bubbles[-1]["kind"] in {"buttons", "list"}
    assert not turn.finished


async def test_tapping_the_first_option_each_time_reaches_a_simulated_booking() -> None:
    tenant = _tenant()
    turns = await _walk_to_the_end(tenant, _Calendars(tenant))
    assert turns[-1].finished and "nada foi agendado de verdade" in turns[-1].note
    kinds = [b["kind"] for t in turns for b in t.bubbles]
    assert "list" in kinds and "buttons" in kinds  # days/slots are lists, confirm is buttons


async def test_the_only_write_is_the_fake_calendar_event_and_it_never_leaves_memory() -> None:
    tenant = _tenant()
    calendars = _Calendars(tenant)
    await _walk_to_the_end(tenant, calendars)
    writes = [w for calendar in calendars.made for w in calendar.writes]
    assert writes == ["create_event"]  # recorded by the fake; no Google client exists at all


async def test_the_slots_come_from_the_configured_opening_hours() -> None:
    tenant = _tenant(business_hours={"monday": [{"start": "09:00", "end": "10:00"}]})
    turns = await _walk_to_the_end(tenant, _Calendars(tenant))
    slots = [
        option["id"]
        for turn in turns
        for bubble in turn.bubbles
        if bubble["kind"] == "list"
        for option in bubble["options"]
        if option["id"].startswith("slot|")
    ]
    assert slots and all("T09:" in slot for slot in slots)


async def test_a_clinic_with_no_opening_hours_cannot_offer_days() -> None:
    tenant = _tenant(business_hours={"monday": []})
    turns = await _walk_to_the_end(tenant, _Calendars(tenant))
    assert not turns[-1].finished


async def test_the_state_is_plain_json_and_survives_a_round_trip() -> None:
    tenant = _tenant()
    calendars = _Calendars(tenant)
    first = await _turn(tenant, calendars)
    revived = json.loads(json.dumps(first.state))
    second = await _turn(tenant, calendars, state=revived, tap=_first_option(first))
    assert second.state != first.state and second.bubbles


async def test_free_text_that_would_reach_the_llm_is_reported_not_run() -> None:
    tenant = _tenant()
    calendars = _Calendars(tenant)
    first = await _turn(tenant, calendars, state=initial_state(), text="olá")
    second = await _turn(tenant, calendars, state=first.state, text="qual o endereço de vocês?")
    assert second.note and "IA livre" in second.note


@pytest.mark.parametrize(
    "step", ["professional_help", "professional_help_final", "service_help", "service_help_final"]
)
async def test_the_scoped_help_steps_never_reach_the_llm(monkeypatch, step) -> None:
    # A hostile or stale `state` can name a help step; route() would call ChatOpenAI there.
    async def _boom(*args, **kwargs):
        raise AssertionError("the simulation must never call the LLM")

    monkeypatch.setattr("secretaria.services.flow_router.run_professional_help", _boom)
    monkeypatch.setattr("secretaria.services.flow_router.run_service_help", _boom)
    tenant = _tenant()
    state = {**initial_state(), "flow_state": "service_catalog", "flow_step": step}
    turn = await _turn(tenant, _Calendars(tenant), state=state, text="x")
    assert turn.note and "IA" in turn.note
    assert turn.state["flow_step"] == step and not turn.finished


async def test_entering_the_help_step_explains_that_the_ai_is_not_run() -> None:
    from secretaria.services.flow_router import _enter_service_help

    convo = SimpleNamespace(flow_selected_professional_id=None, flow_selected_insurance=None)
    result = _enter_service_help(convo)
    from secretaria.workers.shared.flow_simulation import note_for

    note = note_for(result.action, result.flow_step)
    assert note and "IA" in note
