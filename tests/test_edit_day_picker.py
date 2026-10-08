"""The day picker's anchor, the edit branch and the draft carry (TASK-032 R6, spec §5.4)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from tests.test_flow_router import _conversation, _FakeCalendar, _tenant  # noqa: E402

DRAFT = {"appointment_id": "a", "current": {}, "original": {}, "stage": {}}


def _carrier(draft=DRAFT):
    return fr._DayPickerState(flow_managing_appointment_id=uuid4(), flow_edit_draft=draft)


async def test_with_an_anchor_the_nearest_days_come_first_and_are_shown_in_order():
    days = [datetime(2026, 10, d) for d in (9, 10, 12, 14, 15, 16, 17, 19, 20, 21, 22, 23)]
    calendar = _FakeCalendar(days=days)

    result = await fr.enter_day_picker(
        _carrier(),
        _tenant(),
        calendar,
        duration_minutes=40,
        branch=fr.EDIT_DAY_BRANCH,
        anchor=datetime(2026, 10, 16, 15, 20),
    )

    rows = result.bubbles[0].rows
    day_ids = [row[0] for row in rows if row[0].startswith(fr.ROW_DAY_PREFIX)]
    assert day_ids == [f"day|2026-10-{d}|0" for d in (12, 14, 15, 16, 17, 19, 20, 21)]
    assert any(row[1] == fr.LABEL_MORE_DAYS for row in rows)  # 12 days, 8 shown


async def test_without_an_anchor_the_picker_is_unchanged():
    days = [datetime(2026, 10, d) for d in (9, 10, 12, 14)]
    result = await fr.enter_day_picker(
        _conversation(), _tenant(), _FakeCalendar(days=days), duration_minutes=40
    )
    day_ids = [row[0] for row in result.bubbles[0].rows if row[0].startswith(fr.ROW_DAY_PREFIX)]
    assert day_ids == [f"day|2026-10-{d:02d}|0" for d in (9, 10, 12, 14)]


async def test_a_far_anchor_moves_the_scan_window_to_it():
    calendar = _FakeCalendar()
    anchor = datetime.now() + timedelta(days=60)

    await fr.enter_day_picker(
        _carrier(),
        _tenant(),
        calendar,
        duration_minutes=40,
        branch=fr.EDIT_DAY_BRANCH,
        anchor=anchor,
    )

    start_day = calendar.day_scans[0][0]
    assert start_day.astimezone(calendar.tzinfo).date() == (anchor - timedelta(days=10)).date()


async def test_the_edit_branch_carries_the_appointment_and_the_draft():
    result = await fr.enter_day_picker(
        _carrier(),
        _tenant(),
        _FakeCalendar(),
        duration_minutes=40,
        branch=fr.EDIT_DAY_BRANCH,
    )
    assert result.flow_state == FlowState.EDIT_BOOKING
    assert result.flow_step == fr.STEP_EDIT_DAY
    assert result.flow_edit_draft == DRAFT
    assert result.flow_managing_appointment_id is not None


def _in_edit(**kw):
    return _conversation(
        flow_state=FlowState.EDIT_BOOKING, flow_edit_draft={"appointment_id": "x"}, **kw
    )


def test_the_draft_is_carried_only_while_the_edit_continues():
    stays = fr.FlowRouterResult(action="reply", flow_state=FlowState.EDIT_BOOKING)
    assert fr._carry_edit_draft(_in_edit(), stays).flow_edit_draft == {"appointment_id": "x"}

    leaves = fr.FlowRouterResult(action="reply", flow_state=FlowState.MENU)
    assert fr._carry_edit_draft(_in_edit(), leaves).flow_edit_draft is None

    named = fr.FlowRouterResult(
        action="reply", flow_state=FlowState.EDIT_BOOKING, flow_edit_draft={"appointment_id": "y"}
    )
    assert fr._carry_edit_draft(_in_edit(), named).flow_edit_draft == {"appointment_id": "y"}

    idle = _conversation(flow_state=FlowState.IDLE, flow_edit_draft={"appointment_id": "stale"})
    into = fr.FlowRouterResult(action="reply", flow_state=FlowState.EDIT_BOOKING)
    assert fr._carry_edit_draft(idle, into).flow_edit_draft is None


def test_an_ai_detour_inside_the_edit_keeps_the_draft():
    kept = fr._preserve(_in_edit(flow_step=fr.STEP_EDIT_CONFIRM), "delegate_llm")
    assert kept.flow_edit_draft == {"appointment_id": "x"}
    assert kept.flow_state == FlowState.EDIT_BOOKING and kept.flow_step == fr.STEP_EDIT_CONFIRM


def test_every_edit_step_fits_the_flow_step_column():
    steps = [value for name, value in vars(fr).items() if name.startswith("STEP_EDIT_")]
    assert len(steps) == 15
    assert all(len(step) <= 32 for step in steps)
    assert SimpleNamespace(steps=steps).steps  # non-empty
