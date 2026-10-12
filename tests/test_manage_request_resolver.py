"""Where an AI manage request lands: the buttons' own steps, never further (TASK-030 P3).

Pure (services/manage_request.py): fake agenda, a published fake gate, no database.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.booking_draft import (  # noqa: E402
    DRAFT_DAY_OUT_OF_WINDOW_PREFIX,
    DRAFT_DAY_UNAVAILABLE_PREFIX,
)
from secretaria.services.calendar import CalendarUnavailableError  # noqa: E402
from secretaria.services.manage_request import ManageRequest, resolve_manage_request  # noqa: E402
from tests.test_flow_router import _conversation, _tenant  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
NOW = dt.datetime(2026, 10, 5, 9, 0, tzinfo=TZ)
NEW_DAY = dt.date(2026, 10, 15)
TWO_PM = dt.time(14, 0)
OWNER = uuid4()


def _appt(local: dt.datetime, *, professional_id=OWNER, kind="Consulta") -> dict:
    start = local.replace(tzinfo=TZ).astimezone(dt.UTC)
    return {
        "id": str(uuid4()),
        "google_event_id": f"evt-{uuid4()}",
        "appointment_type": kind,
        "start_at": start,
        "end_at": start + dt.timedelta(minutes=30),
        "professional_id": str(professional_id) if professional_id else None,
    }


FIRST = _appt(dt.datetime(2026, 10, 7, 9, 0))
SECOND = _appt(dt.datetime(2026, 10, 9, 9, 0), kind="Retorno")
SOMEONE_ELSES = dt.datetime(2026, 10, 7, 11, 0)  # another patient's appointment time


class _Agenda:
    def __init__(self, free=None, unavailable=False):
        self.tzinfo = TZ
        self.free = free if free is not None else {NEW_DAY: ["14:00", "14:30"]}
        self.unavailable = unavailable
        self.updated: list = []
        self.cancelled: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        if self.unavailable:
            raise CalendarUnavailableError("down")
        return [
            dt.datetime(d.year, d.month, d.day, tzinfo=TZ)
            for d in sorted(self.free)
            if self.free[d]
        ]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self.unavailable:
            raise CalendarUnavailableError("down")
        times = self.free.get(day.date(), [])[:max_slots]
        return [{"start": f"{day.date().isoformat()}T{t}", "end": "", "label": t} for t in times]

    async def update_event(self, event_id, start, end):  # pragma: no cover - must never run
        self.updated.append(event_id)

    async def cancel_event(self, event_id):  # pragma: no cover - must never run
        self.cancelled.append(event_id)


class _Gate:
    armed = False

    def __init__(self, windows=None):
        self._windows = windows or {}
        self.asked: list = []

    async def busy_windows(self, professional_id):
        self.asked.append(professional_id)
        return list(self._windows.get(professional_id, []))


def _local(day: dt.date, hhmm: str) -> dt.datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


async def _resolve(request, *, appointments, agenda=None, gate=None, now=NOW):
    with fr.booking_gate_scope(gate or _Gate()):
        return await resolve_manage_request(
            request,
            tenant=_tenant(),
            appointments=appointments,
            professionals=[],
            calendar=agenda if agenda is not None else _Agenda(),
            conversation_id=uuid4(),
            tz=TZ,
            now=now,
        )


# --------------------------------------------------------------------------
# Cancel: always the "Confirmar o cancelamento?" card
# --------------------------------------------------------------------------


async def test_cancel_names_one_of_two_and_stops_at_the_confirmation_card():
    res = await _resolve(
        ManageRequest("cancel", appointment=dt.datetime(2026, 10, 9, 9, 0)),
        appointments=[FIRST, SECOND],
    )
    assert res.landing_step == fr.STEP_MANAGE_CANCEL_CONFIRM
    assert res.result.flow_managing_appointment_id == UUID(SECOND["id"])
    assert res.result.bubbles[0].body.startswith("Confirmar o cancelamento?")
    assert res.accepted == ("action", "appointment")
    assert res.dropped == {}


async def test_cancel_with_a_reference_that_is_not_the_patients_never_guesses():
    res = await _resolve(
        ManageRequest("cancel", appointment=SOMEONE_ELSES), appointments=[FIRST, SECOND]
    )
    assert res.landing_step == fr.STEP_MANAGE_PICK_CANCEL  # the patient picks, as with buttons
    assert res.result.flow_managing_appointment_id is None
    assert res.dropped == {"appointment": "unknown_appointment"}


async def test_cancel_never_cancels():
    agenda = _Agenda()
    res = await _resolve(ManageRequest("cancel"), appointments=[FIRST], agenda=agenda)
    assert res.landing_step == fr.STEP_MANAGE_CANCEL_CONFIRM
    assert res.result.appointment_cancel_id is None
    assert agenda.cancelled == []


# --------------------------------------------------------------------------
# Reschedule: day picker / slot list / reschedule card
# --------------------------------------------------------------------------


async def test_reschedule_to_a_free_time_lands_on_the_reschedule_card():
    agenda = _Agenda()
    res = await _resolve(
        ManageRequest(
            "reschedule", appointment=dt.datetime(2026, 10, 7, 9, 0), day=NEW_DAY, time=TWO_PM
        ),
        appointments=[FIRST, SECOND],
        agenda=agenda,
    )
    assert res.landing_step == fr.STEP_MANAGE_CONFIRM
    assert res.result.flow_state is FlowState.MANAGE_BOOKING
    assert res.result.flow_managing_appointment_id == UUID(FIRST["id"])
    assert (res.result.flow_selected_day, res.result.flow_selected_slot) == (
        "2026-10-15",
        "2026-10-15T14:00",
    )
    assert res.result.bubbles[0].body == "Remarcar para:\nConsulta\n15/10/2026 às 14:00"
    assert res.accepted == ("action", "appointment", "day", "time")
    assert res.result.appointment_reschedule is None
    assert agenda.updated == []


async def test_a_held_new_time_lands_on_that_days_slot_list():
    gate = _Gate({OWNER: [(_local(NEW_DAY, "14:00"), _local(NEW_DAY, "14:30"))]})
    res = await _resolve(
        ManageRequest("reschedule", day=NEW_DAY, time=TWO_PM), appointments=[FIRST], gate=gate
    )
    assert res.landing_step == fr.STEP_MANAGE_SLOT
    assert res.dropped == {"time": "no_free_slot"}
    rows = [row[0] for row in res.result.bubbles[0].rows]
    assert "slot|2026-10-15T14:00" not in rows and "slot|2026-10-15T14:30" in rows
    # The holds are looked up on the appointment's OWN agenda.
    assert set(gate.asked) == {OWNER}


async def test_a_new_day_outside_the_window_lands_on_the_reschedule_day_picker():
    res = await _resolve(
        ManageRequest("reschedule", day=dt.date(2026, 12, 1), time=TWO_PM), appointments=[FIRST]
    )
    assert res.landing_step == fr.STEP_MANAGE_DAY
    assert res.dropped == {"day": "out_of_window", "time": "missing_day"}
    assert res.result.bubbles[0].body.startswith(DRAFT_DAY_OUT_OF_WINDOW_PREFIX)
    assert res.result.flow_managing_appointment_id == UUID(FIRST["id"])


async def test_a_new_day_without_free_time_lands_on_the_day_picker():
    agenda = _Agenda({NEW_DAY: [], dt.date(2026, 10, 16): ["09:00"]})
    res = await _resolve(
        ManageRequest("reschedule", day=NEW_DAY), appointments=[FIRST], agenda=agenda
    )
    assert res.landing_step == fr.STEP_MANAGE_DAY
    assert res.dropped == {"day": "day_unavailable"}
    assert res.result.bubbles[0].body.startswith(DRAFT_DAY_UNAVAILABLE_PREFIX)


async def test_today_with_the_new_time_already_past():
    # The calendar's own walk never offers a started slot; the fake mirrors that.
    afternoon = dt.datetime(2026, 10, 15, 15, 0, tzinfo=TZ)
    agenda = _Agenda({NEW_DAY: ["15:30"]})
    res = await _resolve(
        ManageRequest("reschedule", day=NEW_DAY, time=TWO_PM),
        appointments=[_appt(dt.datetime(2026, 10, 20, 9, 0))],
        agenda=agenda,
        now=afternoon,
    )
    assert res.landing_step == fr.STEP_MANAGE_SLOT
    assert [row[0] for row in res.result.bubbles[0].rows][0] == "slot|2026-10-15T15:30"


async def test_reschedule_without_a_day_opens_the_day_picker():
    res = await _resolve(ManageRequest("reschedule"), appointments=[FIRST])
    assert res.landing_step == fr.STEP_MANAGE_DAY
    assert res.result.flow_managing_appointment_id == UUID(FIRST["id"])


async def test_two_appointments_and_none_named_show_the_pick_list():
    res = await _resolve(
        ManageRequest("reschedule", day=NEW_DAY, time=TWO_PM), appointments=[FIRST, SECOND]
    )
    assert res.landing_step == fr.STEP_MANAGE_PICK_RESCHEDULE
    assert res.dropped == {"day": "appointment_not_chosen", "time": "appointment_not_chosen"}
    assert res.accepted == ("action",)


async def test_nothing_to_reschedule_lands_on_the_menu():
    res = await _resolve(ManageRequest("reschedule"), appointments=[])
    assert res.landing_step == "menu"
    assert res.fallback == "no_appointments"


async def test_an_unreachable_agenda_hands_over():
    res = await _resolve(
        ManageRequest("reschedule", day=NEW_DAY, time=TWO_PM),
        appointments=[FIRST],
        agenda=_Agenda(unavailable=True),
    )
    assert res.result.action == "calendar_unavailable"
    assert (res.landing_step, res.fallback) == ("human_handover", "calendar_unavailable")


async def test_the_button_path_card_is_byte_for_byte_the_same_builder():
    conversation = SimpleNamespace(
        flow_managing_appointment_id=UUID(FIRST["id"]), flow_selected_day="2026-10-15"
    )
    tapped = fr._manage_handle_slot(conversation, [FIRST], "🗓️ 14:00 (2026-10-15T14:00)")
    built = fr._manage_confirm_result(
        UUID(FIRST["id"]), FIRST, dt.datetime(2026, 10, 15, 14, 0), "2026-10-15"
    )
    assert tapped == built


@pytest.mark.parametrize("action", ["cancel", "reschedule"])
async def test_an_unknown_reference_requires_a_pick_even_with_one_appointment(action):
    res = await _resolve(
        ManageRequest(action, appointment=SOMEONE_ELSES, day=NEW_DAY, time=TWO_PM),
        appointments=[FIRST],
    )
    expected = fr.STEP_MANAGE_PICK_CANCEL if action == "cancel" else fr.STEP_MANAGE_PICK_RESCHEDULE
    assert res.landing_step == expected
    assert res.result.flow_managing_appointment_id is None
    assert res.result.flow_selected_slot is None
    assert res.dropped["appointment"] == "unknown_appointment"


@pytest.mark.parametrize("action", ["cancel", "reschedule"])
async def test_an_ambiguous_reference_requires_an_explicit_pick(action):
    twin = _appt(dt.datetime(2026, 10, 7, 9, 0), kind="Retorno")
    res = await _resolve(
        ManageRequest(action, appointment=dt.datetime(2026, 10, 7, 9, 0)),
        appointments=[FIRST, twin],
    )
    assert res.result.flow_managing_appointment_id is None
    assert res.dropped == {"appointment": "unknown_appointment"}


@pytest.mark.parametrize("action", ["cancel", "reschedule"])
async def test_selecting_the_second_simultaneous_appointment_targets_that_one(action):
    twin = _appt(dt.datetime(2026, 10, 7, 9, 0), kind="Retorno")
    res = await _resolve(
        ManageRequest(action, appointment=dt.datetime(2026, 10, 7, 9, 0)),
        appointments=[FIRST, twin],
    )
    rows = res.result.bubbles[0].rows
    assert rows[0][0] != rows[1][0]
    selected = await fr.route(
        _conversation(flow_state=res.result.flow_state, flow_step=res.result.flow_step),
        _tenant(),
        _Agenda(),
        inbound_routing_text(rows[1][1], rows[1][0]),
        upcoming_appointments=[twin, FIRST],
    )
    assert selected.flow_managing_appointment_id == UUID(twin["id"])
    assert selected.appointment_cancel_id is None
    assert selected.appointment_reschedule is None


@pytest.mark.parametrize("action", ["cancel", "reschedule"])
async def test_an_appointment_specific_pick_rejects_a_stale_or_foreign_id(action):
    twin = _appt(dt.datetime(2026, 10, 7, 9, 0), kind="Retorno")
    res = await _resolve(ManageRequest(action), appointments=[FIRST, twin])
    row = res.result.bubbles[0].rows[1]
    result = await fr.route(
        _conversation(flow_state=res.result.flow_state, flow_step=res.result.flow_step),
        _tenant(),
        _Agenda(),
        inbound_routing_text(row[1], row[0]),
        upcoming_appointments=[FIRST],
    )
    assert result.flow_managing_appointment_id is None
    assert result.appointment_cancel_id is None
    assert result.appointment_reschedule is None


@pytest.mark.parametrize("action", ["cancel", "reschedule"])
@pytest.mark.parametrize("with_reference", [False, True])
async def test_indistinguishable_simultaneous_appointments_need_human_help(action, with_reference):
    twin = _appt(dt.datetime(2026, 10, 7, 9, 0))
    res = await _resolve(
        ManageRequest(
            action, appointment=dt.datetime(2026, 10, 7, 9, 0) if with_reference else None
        ),
        appointments=[FIRST, twin],
    )
    assert res.result.action == "handover"
    assert res.landing_step == "human_handover"
    assert res.fallback == "ambiguous_appointment"
    assert res.result.flow_managing_appointment_id is None
    assert res.result.appointment_cancel_id is None
    assert res.result.appointment_reschedule is None
    assert not any(hasattr(bubble, "rows") for bubble in res.result.bubbles)


async def test_simultaneous_appointments_with_different_doctors_have_distinct_descriptions():
    other_doctor = uuid4()
    twin = _appt(dt.datetime(2026, 10, 7, 9, 0), professional_id=other_doctor)
    with fr.booking_gate_scope(_Gate()):
        res = await resolve_manage_request(
            ManageRequest("cancel"),
            tenant=_tenant(),
            appointments=[FIRST, twin],
            professionals=[
                SimpleNamespace(id=OWNER, name="Dra. Ana"),
                SimpleNamespace(id=other_doctor, name="Dr. Bruno"),
            ],
            calendar=None,
            conversation_id=uuid4(),
            tz=TZ,
            now=NOW,
        )
    assert res.result.action == "reply"
    rows = res.result.bubbles[0].rows
    assert rows[0][2] == "Dra. Ana — Consulta"
    assert rows[1][2] == "Dr. Bruno — Consulta"
