"""Manage request v2: wire format, the appointment reference and the tool (TASK-030 P3)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import datetime as dt  # noqa: E402
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.ai.tools import (  # noqa: E402
    ManageAppointmentRequested,
    manage_existing_appointment,
    manage_existing_appointment_v2,
)
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_SOLE  # noqa: E402
from secretaria.services.manage_request import (  # noqa: E402
    ManageRequest,
    appointment_ref,
    find_appointment,
    manage_target,
    parse_appointment_ref,
)
from secretaria.workers.shared.llm_context import (  # noqa: E402
    _appointment_context_text,
    _flow_handback_tools,
)

TZ = ZoneInfo("America/Sao_Paulo")


def _appt(local: dt.datetime, kind: str = "Consulta") -> dict:
    start = local.replace(tzinfo=TZ).astimezone(dt.UTC)
    return {
        "id": str(uuid4()),
        "google_event_id": f"evt-{uuid4()}",
        "appointment_type": kind,
        "start_at": start,
        "end_at": start + dt.timedelta(minutes=30),
        "professional_id": None,
    }


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------


def test_a_bare_action_is_the_v1_sentinel_byte_for_byte():
    assert ManageRequest("cancel").to_payload() == "cancel"
    assert ManageRequest.from_payload("reschedule") == ManageRequest("reschedule")


def test_the_v2_payload_round_trips():
    request = ManageRequest(
        "reschedule",
        appointment=dt.datetime(2026, 10, 8, 10, 0),
        day=dt.date(2026, 10, 15),
        time=dt.time(14, 0),
    )
    assert json.loads(request.to_payload()) == {
        "a": "reschedule",
        "ap": "2026-10-08 10:00",
        "d": "2026-10-15",
        "h": "14:00",
    }
    assert ManageRequest.from_payload(request.to_payload()) == request


@pytest.mark.parametrize(
    "raw",
    [
        "excluir",
        "",
        "[]",
        '{"a": "delete"}',
        '{"a": "cancel", "ap": "08/10/2026 10:00"}',
        '{"a": "cancel", "ap": 7}',
        '{"a": "reschedule", "d": "2026-13-01"}',
        '{"a": "reschedule", "h": "10h"}',
        '{"a": "reschedule", "h": "25:00"}',
    ],
)
def test_a_corrupt_payload_raises_value_error(raw):
    with pytest.raises(ValueError):
        ManageRequest.from_payload(raw)


def test_supplied_fields_name_only_what_is_present_in_order():
    request = ManageRequest("reschedule", day=dt.date(2026, 10, 15))
    assert request.supplied_fields() == ("action", "day")


# --------------------------------------------------------------------------
# The appointment reference
# --------------------------------------------------------------------------


def test_the_reference_is_the_clinic_local_minute():
    aware = dt.datetime(2026, 10, 8, 13, 0, tzinfo=dt.UTC)
    assert appointment_ref(aware, TZ) == "2026-10-08 10:00"
    # SQLite hands timestamps back naive: they are UTC (patient_context.as_utc).
    assert appointment_ref(aware.replace(tzinfo=None), TZ) == "2026-10-08 10:00"


@pytest.mark.parametrize("text", ["2026-10-08 10:00", "2026-10-08T10:00", " 2026-10-08 10:00 "])
def test_parse_appointment_ref(text):
    assert parse_appointment_ref(text) == dt.datetime(2026, 10, 8, 10, 0)


def test_find_appointment_matches_only_the_patients_own_minute():
    first = _appt(dt.datetime(2026, 10, 8, 10, 0))
    second = _appt(dt.datetime(2026, 10, 15, 14, 0))
    appointments = [first, second]
    assert find_appointment(appointments, dt.datetime(2026, 10, 15, 14, 0), TZ) is second
    assert find_appointment(appointments, dt.datetime(2026, 10, 9, 10, 0), TZ) is None
    assert find_appointment(appointments, None, TZ) is None
    twin = _appt(dt.datetime(2026, 10, 8, 10, 0))
    # Two at the same minute is ambiguous, and ambiguous is never guessed.
    assert find_appointment([first, twin], dt.datetime(2026, 10, 8, 10, 0), TZ) is None


def test_manage_target_never_replaces_a_wrong_reference_with_a_guess():
    only = _appt(dt.datetime(2026, 10, 8, 10, 0))
    two = [only, _appt(dt.datetime(2026, 10, 15, 14, 0))]
    assert manage_target(ManageRequest("reschedule"), [only], TZ) is only
    assert manage_target(ManageRequest("reschedule"), two, TZ) is None
    assert manage_target(ManageRequest("cancel"), [only], TZ) is None  # enter_manage_action decides
    wrong = ManageRequest("reschedule", appointment=dt.datetime(2026, 10, 9, 9, 0))
    assert manage_target(wrong, [only], TZ) is None


# --------------------------------------------------------------------------
# The tool (v2) and the clinic switch
# --------------------------------------------------------------------------


def test_the_v2_tool_has_the_v1_name_and_its_args():
    assert manage_existing_appointment_v2.name == manage_existing_appointment.name
    # `message` (TASK-038): the agent's words for the patient, sent before the card.
    assert set(manage_existing_appointment_v2.args) == {
        "action",
        "appointment",
        "day",
        "time",
        "message",
    }


async def test_the_v2_tool_carries_every_field_of_a_reschedule():
    with pytest.raises(ManageAppointmentRequested) as exc:
        await manage_existing_appointment_v2.ainvoke(
            {
                "action": "Remarcar",
                "appointment": "2026-10-08 10:00",
                "day": "2026-10-15",
                "time": "14:00",
            }
        )
    assert exc.value.request == ManageRequest(
        "reschedule",
        appointment=dt.datetime(2026, 10, 8, 10, 0),
        day=dt.date(2026, 10, 15),
        time=dt.time(14, 0),
    )


async def test_the_v2_tool_drops_day_and_time_on_a_cancel():
    with pytest.raises(ManageAppointmentRequested) as exc:
        await manage_existing_appointment_v2.ainvoke(
            {
                "action": "cancel",
                "appointment": "2026-10-08 10:00",
                "day": "2026-10-15",
                "time": "14:00",
            }
        )
    assert exc.value.request == ManageRequest("cancel", appointment=dt.datetime(2026, 10, 8, 10, 0))


@pytest.mark.parametrize(
    "args, reason",
    [
        ({"action": "excluir Maria"}, "bad_action"),
        ({"action": "cancel", "appointment": "quinta 10h"}, "bad_appointment"),
        ({"action": "reschedule", "day": "15/10/2026"}, "bad_day"),
        ({"action": "reschedule", "day": "2026-10-15", "time": "14h"}, "bad_time"),
    ],
)
async def test_a_bad_format_is_a_recoverable_error_that_echoes_nothing(monkeypatch, args, reason):
    log = _Log()
    monkeypatch.setattr(ai_tools, "logger", log)
    out = await manage_existing_appointment_v2.ainvoke(args)
    assert "error" in out
    assert "Maria" not in out["error"] and "quinta" not in out["error"]
    assert [fields["reason"] for event, fields in log.events if event == "agent_tool_blocked"] == [
        reason
    ]


def test_the_clinic_switch_picks_the_manage_tool():
    off = _flow_handback_tools(SimpleNamespace(initial_flows={}), BOOKING_TOPOLOGY_SOLE, [])
    on = _flow_handback_tools(
        SimpleNamespace(initial_flows={"ai_draft_v2": True}), BOOKING_TOPOLOGY_SOLE, []
    )
    assert manage_existing_appointment in off and manage_existing_appointment_v2 not in off
    assert manage_existing_appointment_v2 in on and manage_existing_appointment not in on


# --------------------------------------------------------------------------
# The appointment context block shows the reference the tool takes
# --------------------------------------------------------------------------


def test_the_appointment_context_carries_refs_only_when_asked():
    future = [
        _appt(dt.datetime(2026, 10, 8, 10, 0)),
        _appt(dt.datetime(2026, 10, 15, 14, 0), "Retorno"),
    ]
    plain = _appointment_context_text(future, "America/Sao_Paulo", {}, [])
    assert "(ref " not in plain
    with_refs = _appointment_context_text(future, "America/Sao_Paulo", {}, [], with_refs=True)
    assert with_refs.splitlines() == [
        "Próxima consulta: 08/10 às 10:00 — Consulta (ref 2026-10-08 10:00)",
        "15/10 às 14:00 — Retorno (ref 2026-10-15 14:00)",
    ]
