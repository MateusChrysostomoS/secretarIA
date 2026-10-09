"""PATCH /appointments/{id}/status guards (TASK-032 R7, spec 2026-10-09 §1/§3)."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from secretaria.api.hub import calendar as hub_calendar
from secretaria.models import AppointmentStatus
from secretaria.services.appointment_status import StaffTransitionRefused, staff_transition
from tests._r7_support import acting, patch_status, r7, set_appointment, setup_world  # noqa: F401
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment

S = AppointmentStatus
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
FUTURE = NOW + timedelta(hours=2)
PAST = NOW - timedelta(hours=2)


# ------------------------------------------------------------------ the pure rule


@pytest.mark.parametrize("current", [S.SCHEDULED, S.CONFIRMED, S.RESCHEDULED])
@pytest.mark.parametrize("target", [S.SCHEDULED, S.CONFIRMED, S.RESCHEDULED, S.ATTENDED])
def test_a_live_appointment_accepts_any_live_target_and_attended(current, target):
    assert staff_transition(current, target, start_at=FUTURE, now=NOW) is True


def test_no_show_before_the_start_is_refused_and_after_it_is_accepted():
    with pytest.raises(StaffTransitionRefused) as err:
        staff_transition(S.SCHEDULED, S.NO_SHOW, start_at=FUTURE, now=NOW)
    assert err.value.code == "no_show_before_start"
    assert staff_transition(S.SCHEDULED, S.NO_SHOW, start_at=PAST, now=NOW) is True
    assert staff_transition(S.SCHEDULED, S.NO_SHOW, start_at=None, now=NOW) is True


@pytest.mark.parametrize("target", [S.SCHEDULED, S.CONFIRMED, S.RESCHEDULED, S.ATTENDED, S.NO_SHOW])
def test_a_cancelled_appointment_never_comes_back(target):
    with pytest.raises(StaffTransitionRefused) as err:
        staff_transition(S.CANCELLED, target, start_at=PAST, now=NOW)
    assert err.value.code == "not_live"


def test_attended_and_no_show_correct_each_other_and_repeat_as_a_no_op():
    assert staff_transition(S.ATTENDED, S.NO_SHOW, start_at=PAST, now=NOW) is True
    assert staff_transition(S.NO_SHOW, S.ATTENDED, start_at=PAST, now=NOW) is True
    assert staff_transition(S.ATTENDED, S.ATTENDED, start_at=PAST, now=NOW) is False
    assert staff_transition(S.NO_SHOW, S.NO_SHOW, start_at=PAST, now=NOW) is False
    with pytest.raises(StaffTransitionRefused):
        staff_transition(S.ATTENDED, S.CONFIRMED, start_at=FUTURE, now=NOW)
    with pytest.raises(StaffTransitionRefused):
        staff_transition(S.ATTENDED, S.NO_SHOW, start_at=FUTURE, now=NOW)  # still before start


@pytest.mark.parametrize("current", list(AppointmentStatus))
def test_cancelled_keeps_todays_unguarded_behaviour(current):
    assert staff_transition(current, S.CANCELLED, start_at=FUTURE, now=NOW) is True


# ------------------------------------------------------------------ the endpoint


async def test_no_show_before_the_start_is_409_and_changes_nothing(
    client: AsyncClient, db, acting, monkeypatch
):
    world = await setup_world(db, acting)
    calls: list = []

    async def _spy(*args, **kwargs):
        calls.append(1)

    monkeypatch.setattr(hub_calendar.deposit_lifecycle, "on_no_show", _spy)

    response = await patch_status(client, world.appointment.id, "no_show")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "no_show_before_start" and detail["status"] == "scheduled"
    assert detail["start_at"].startswith(world.start_at.strftime("%Y-%m-%dT%H:%M"))
    assert (await reload_appointment(db, world.appointment.id)).status == S.SCHEDULED
    assert calls == []


async def test_no_show_after_the_start_is_accepted(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, start_at=datetime.now(UTC) - timedelta(hours=1))

    response = await patch_status(client, world.appointment.id, "no_show")

    assert response.status_code == 200 and response.json()["status"] == "no_show"
    assert response.json()["patient_notice"] is None


@pytest.mark.parametrize("target", ["confirmed", "attended", "scheduled", "no_show"])
async def test_a_cancelled_appointment_is_never_resurrected(
    client: AsyncClient, db, acting, target
):
    world = await setup_world(db, acting, status=S.CANCELLED)

    response = await patch_status(client, world.appointment.id, target)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_live"
    assert response.json()["detail"]["status"] == "cancelled"
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == S.CANCELLED and row.confirmation_count == 0


async def test_attended_twice_is_a_quiet_no_op(client: AsyncClient, db, acting, monkeypatch):
    world = await setup_world(db, acting, start_at=datetime.now(UTC) - timedelta(hours=1))
    await set_appointment(db, world.appointment.id, status=S.ATTENDED)
    logged: list = []
    monkeypatch.setattr(hub_calendar, "log_status_transition", lambda **kw: logged.append(kw))

    response = await patch_status(client, world.appointment.id, "attended")

    assert response.status_code == 200 and response.json()["status"] == "attended"
    assert response.json()["patient_notice"] is None and logged == []


async def test_attended_can_be_corrected_to_no_show_after_the_start(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, start_at=datetime.now(UTC) - timedelta(hours=1))
    await set_appointment(db, world.appointment.id, status=S.ATTENDED)

    response = await patch_status(client, world.appointment.id, "no_show")

    assert response.status_code == 200 and response.json()["status"] == "no_show"


async def test_the_action_response_carries_the_notice_keys(client: AsyncClient, db, acting):
    world = await setup_world(db, acting)

    body = (await patch_status(client, world.appointment.id, "scheduled")).json()

    assert body["patient_notice"] is None and body["whatsapp_link"] is None
    assert body["status"] == "scheduled"
