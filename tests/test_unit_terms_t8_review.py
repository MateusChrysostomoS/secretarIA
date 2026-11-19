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

from datetime import timedelta  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.ai.tool_output import allowlisted  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.models import Appointment  # noqa: E402
from secretaria.plugins import multi_professional as mp, multi_unit as mu  # noqa: E402
from secretaria.services import booking_hold  # noqa: E402
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_MULTI  # noqa: E402
from secretaria.services.local_calendar import LocalCalendarService  # noqa: E402
from tests import test_unit_terms_ai_db as base  # noqa: E402
from tests._local_agenda import wire_local_agenda  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

CENTRO_40 = {"Centro": (None, 40)}


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
