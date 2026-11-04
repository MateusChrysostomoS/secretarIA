"""The "Outra data" screen (owner, 2026-11-04): a date far ahead, picked on a calendar.

The short day list stops at DAY_PICKER_WINDOW_DAYS, so a patient who wanted a date further out
had no way to reach it. "Outra data" scans DAY_CALENDAR_WINDOW_DAYS under the same rules. The
Portal draws the free days on a calendar; WhatsApp keeps a paged list of the same days.
"""

import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402

from secretaria.ai.formatter import MAX_LIST_ROWS, SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.schemas.conversation import InteractiveRead  # noqa: E402
from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.calendar import CalendarUnavailableError  # noqa: E402
from secretaria.services.channel_sender import BrainMessageSender  # noqa: E402
from secretaria.services.whatsapp import WhatsAppClient, interactive_list_record  # noqa: E402
from secretaria.workers.portal.inbound import offered_reply_ids  # noqa: E402
from secretaria.workers.shared.bubbles import _send_bubble  # noqa: E402

_TZ = ZoneInfo("America/Sao_Paulo")
_SERVICE = "Consulta Geral"
_NEAR = fr.DAY_PICKER_WINDOW_DAYS
_FAR = fr.DAY_CALENDAR_WINDOW_DAYS


def _tenant():
    return SimpleNamespace(
        initial_flows={"buttons": ["Serviços e Custo", "Horários", "Outro"]},
        appointment_types=[
            {"name": _SERVICE, "duration_min": 30, "is_active": True, "sort_order": 0}
        ],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        collect_insurance=False,
        insurances=None,
    )


def _conversation(**kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_DAY,
        flow_selected_type=_SERVICE,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _Calendar:
    """A doctor free every `every` days, from `first_offset` days after 2027-03-01."""

    def __init__(self, *, every=1, first_offset=0, unavailable_on_long_scan=False):
        self._every = every
        self._first_offset = first_offset
        self._unavailable_on_long_scan = unavailable_on_long_scan
        self.tzinfo = _TZ
        self.day_scans: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.day_scans.append((start_day, days, slot_minutes))
        if self._unavailable_on_long_scan and days > _NEAR:
            raise CalendarUnavailableError("down")
        base = datetime(2027, 3, 1, tzinfo=_TZ)
        return [
            base + timedelta(days=offset)
            for offset in range(self._first_offset, days, self._every)
        ]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        return [{"start": "2027-08-02T08:00", "end": "2027-08-02T08:30", "label": "08:00"}]


def _tap(bubble: SlotsBubble, label: str) -> str:
    for row in bubble.rows:
        if row[1] == label:
            return inbound_routing_text(row[1], row[0])
    raise AssertionError(f"no {label!r} row in {[r[1] for r in bubble.rows]}")


async def _short_list(calendar=None, **kw):
    return await fr.enter_day_picker(
        _conversation(), _tenant(), calendar or _Calendar(), duration_minutes=30, **kw
    )


async def _calendar_screen(calendar=None):
    calendar = calendar or _Calendar()
    first = await _short_list(calendar, back_target=fr.BACK_TARGET_SERVICE)
    return (
        await fr.route(
            _conversation(), _tenant(), calendar, _tap(first.bubbles[0], fr.LABEL_OTHER_DATE)
        ),
        calendar,
    )


# --- the short list ---------------------------------------------------------


@pytest.mark.parametrize("every", [1, 3, 15])
async def test_the_short_list_always_offers_outra_data(every):
    result = await _short_list(_Calendar(every=every), back_target=fr.BACK_TARGET_SERVICE)
    labels = [row[1] for row in result.bubbles[0].rows]
    assert fr.LABEL_OTHER_DATE in labels
    assert labels[-1] == fr.LABEL_ANOTHER_SERVICE
    assert len(labels) <= MAX_LIST_ROWS


async def test_the_short_list_does_not_pay_for_the_long_scan():
    calendar = _Calendar()
    result = await _short_list(calendar)
    assert [scan[1] for scan in calendar.day_scans] == [_NEAR]
    assert result.bubbles[0].calendar_days == []


# --- the calendar screen ----------------------------------------------------


async def test_outra_data_scans_far_ahead_once_and_lists_every_free_day():
    result, calendar = await _calendar_screen(_Calendar(every=7))
    assert [scan[1] for scan in calendar.day_scans] == [_NEAR, _FAR]
    bubble = result.bubbles[0]
    assert bubble.calendar_days[0] == "2027-03-01"
    assert bubble.calendar_days[-1] == (
        datetime(2027, 3, 1) + timedelta(days=(_FAR - 1) // 7 * 7)
    ).date().isoformat()
    assert len(bubble.calendar_days) == (_FAR - 1) // 7 + 1
    assert bubble.body == fr.DAY_CALENDAR_BODY
    assert bubble.button_label == "Ver datas"
    assert result.flow_step == fr.STEP_AWAITING_DAY


async def test_the_screen_keeps_the_row_cap_for_whatsapp():
    result, _ = await _calendar_screen()
    assert len(result.bubbles[0].rows) <= MAX_LIST_ROWS
    assert fr.LABEL_OTHER_DATE not in [row[1] for row in result.bubbles[0].rows]


async def test_whatsapp_pages_through_the_long_scan_and_stays_in_it():
    result, calendar = await _calendar_screen()
    bubble = result.bubbles[0]
    assert bubble.rows[0][1] == "🗓️ Ter, 09/03"  # continues where the short list stopped
    following = await fr.route(
        _conversation(), _tenant(), calendar, _tap(bubble, fr.LABEL_MORE_DAYS)
    )
    assert following.bubbles[0].rows[0][1] == "🗓️ Qua, 17/03"
    assert calendar.day_scans[-1][1] == _FAR
    assert following.bubbles[0].calendar_days  # still the long scan


async def test_a_ver_mais_row_from_a_card_already_on_screen_keeps_working():
    calendar = _Calendar()
    result = await fr.route(
        _conversation(), _tenant(), calendar, inbound_routing_text("Ver mais dias", "daymore|1")
    )
    assert result.bubbles[0].rows[0][1] == "🗓️ Ter, 09/03"
    assert calendar.day_scans[-1][1] == _NEAR


async def test_typing_outra_data_opens_the_same_screen():
    calendar = _Calendar()
    result = await fr.route(_conversation(), _tenant(), calendar, "outra data")
    assert result.bubbles[0].calendar_days
    assert calendar.day_scans[-1][1] == _FAR


async def test_a_day_tapped_on_the_calendar_goes_to_its_times():
    calendar = _Calendar()
    body = inbound_routing_text("Seg, 02/08", fr.calendar_day_row_id("2027-08-02"))
    result = await fr.route(_conversation(), _tenant(), calendar, body)
    assert result.flow_step == fr.STEP_AWAITING_SLOT
    assert result.flow_selected_day == "2027-08-02"


async def test_nothing_soon_opens_the_far_scan_instead_of_saying_the_agenda_is_full():
    calendar = _Calendar(first_offset=_NEAR + 10)
    result = await _short_list(calendar)
    assert [scan[1] for scan in calendar.day_scans] == [_NEAR, _FAR]
    assert result.bubbles[0].calendar_days[0] == "2027-03-31"


async def test_no_free_day_at_all_still_ends_at_the_menu():
    calendar = _Calendar(first_offset=_FAR + 1)
    result = await _short_list(calendar)
    assert result.flow_state == FlowState.MENU
    assert isinstance(result.bubbles[0], TextBubble)


async def test_the_long_scan_failing_reaches_a_human():
    result, _ = await _calendar_screen(_Calendar(unavailable_on_long_scan=True))
    assert result.action == "calendar_unavailable"


async def test_the_doctors_minimum_notice_applies_to_the_long_scan_too(monkeypatch):
    floor = datetime(2027, 3, 3, 8, 0, tzinfo=_TZ)
    seen = {}

    async def fake_days(calendar, *, start, window_days, duration_minutes, not_before=None):
        seen.update(window_days=window_days, not_before=not_before)
        return [datetime(2027, 3, 3, tzinfo=_TZ)]

    monkeypatch.setattr(fr, "available_day_starts", fake_days)
    monkeypatch.setattr(fr, "_booking_floor", lambda *a, **kw: floor)
    await _short_list(_Calendar(), calendar_view=True)
    assert seen == {"window_days": _FAR, "not_before": floor}


async def test_rescheduling_opens_the_calendar_from_today_not_around_the_appointment():
    calendar = _Calendar()
    anchor = datetime(2027, 6, 1, 10, 0, tzinfo=_TZ)
    await fr.enter_day_picker(
        _conversation(),
        _tenant(),
        calendar,
        duration_minutes=30,
        branch=fr.MANAGE_DAY_BRANCH,
        calendar_view=True,
    )
    start = calendar.day_scans[-1][0]
    assert start.date() < anchor.date()
    assert abs(start - datetime.now(_TZ)) < timedelta(minutes=1)


# --- from the bubble to the wire ---------------------------------------------


def test_the_card_records_its_days_apart_from_the_rows():
    rows = [(f"day|2027-03-{d:02d}|0", f"Dia {d}", None) for d in range(1, 13)]
    days = [f"2027-03-{d:02d}" for d in range(1, 13)]
    record = interactive_list_record("Corpo", "Ver datas", rows, "Datas", calendar_days=days)
    assert record["calendar"] == {"days": days}
    assert len(record["options"]) == MAX_LIST_ROWS
    assert InteractiveRead.model_validate(record).calendar.days == days


def test_a_card_without_days_is_byte_for_byte_what_it_was():
    record = interactive_list_record("Corpo", "Ver", [("a", "A", None)], "S")
    assert "calendar" not in record
    assert InteractiveRead.model_validate(record).calendar is None


def test_a_calendar_day_is_a_valid_tap_and_a_day_not_offered_is_not():
    cards = [{"kind": "list", "options": [], "calendar": {"days": ["2027-08-02"]}}]
    offered = offered_reply_ids(cards)
    assert fr.calendar_day_row_id("2027-08-02") in offered
    assert fr.calendar_day_row_id("2027-08-03") not in offered


@pytest.mark.parametrize(
    "calendar", [None, "x", {"days": None}, {"days": "2027-08-02"}, {"days": [1, None]}]
)
def test_a_malformed_calendar_offers_nothing(calendar):
    assert offered_reply_ids([{"options": [{"id": "a"}], "calendar": calendar}]) == {"a"}


class _Recorder:
    def __init__(self, **attrs):
        self.calls = []
        for key, value in attrs.items():
            setattr(self, key, value)

    async def send_list(self, **kw):
        self.calls.append(kw)
        return {}


async def test_only_a_channel_that_draws_a_calendar_receives_the_days():
    bubble = SlotsBubble(body="b", rows=[("a", "A")], calendar_days=["2027-08-02"])
    portal, whatsapp = _Recorder(SUPPORTS_CALENDAR=True), _Recorder()
    await _send_bubble(portal, "to", bubble)
    await _send_bubble(whatsapp, "to", bubble)
    assert portal.calls[0]["calendar_days"] == ["2027-08-02"]
    assert "calendar_days" not in whatsapp.calls[0]


def test_the_senders_declare_the_capability():
    assert BrainMessageSender.SUPPORTS_CALENDAR is True
    assert getattr(WhatsAppClient, "SUPPORTS_CALENDAR", False) is False
