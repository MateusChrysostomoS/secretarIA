"""The reusable day/time branch (PROMPT_FEAT_32 part B).

Two layers, mirroring how the rest of this suite is split:

  - `CalendarService.list_available_days` against a fake Google client: which
    days qualify, and — the performance invariant the whole design rests on —
    that scanning a three-week window costs exactly ONE events.list call.
  - `flow_router`'s picker itself with a fake calendar: the WhatsApp row
    budget, pagination, `Voltar`, the bounded free-text escalation, and the
    two calendar-failure modes that must reach a human instead of the model.

What this branch replaced: a free-text question ("Para quando você gostaria?
(ex: amanhã, sexta, 12/06)") whose parser fell through to `delegate_llm` on
anything it could not read, plus a second `delegate_llm` when the calendar was
missing. Several tests below exist specifically so neither can come back.
"""

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai.formatter import MAX_LIST_ROWS, SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services.calendar import (  # noqa: E402
    DAY_SCAN_MAX_EVENTS,
    CalendarService,
    CalendarUnavailableError,
)
from secretaria.services.flow_router import (  # noqa: E402
    BACK_TARGET_PROFESSIONAL,
    BACK_TARGET_SERVICE,
    BOOKING_DAY_BRANCH,
    DAY_PICKER_PAGE_SIZE,
    DAY_PICKER_WINDOW_DAYS,
    LABEL_ANOTHER_DAY,
    LABEL_BACK,
    LABEL_MORE_DAYS,
    LABEL_OTHER,
    MANAGE_DAY_BRANCH,
    STEP_AWAITING_DAY,
    STEP_AWAITING_DAY_ESCAPE,
    STEP_AWAITING_DAY_RETRY,
    STEP_AWAITING_PROFESSIONAL,
    STEP_AWAITING_SERVICE,
    STEP_AWAITING_SLOT,
    STEP_MANAGE_DAY,
    STEP_MANAGE_DAY_ESCAPE,
    STEP_MANAGE_DAY_RETRY,
    STEP_MANAGE_SLOT,
    MenuBubble,
    _handle_day_back,
    enter_day_picker,
    route,
)

_TZ = ZoneInfo("America/Sao_Paulo")
_SERVICE = "Consulta Geral"
_APPT_ID = "11111111-1111-4111-8111-111111111111"


# ---------------------------------------------------------------------------
# Layer 1 — CalendarService.list_available_days (the ONE-call scan)
# ---------------------------------------------------------------------------


def _stub_settings() -> SimpleNamespace:
    return SimpleNamespace(
        CLINIC_TIMEZONE="America/Sao_Paulo",
        GOOGLE_CALENDAR_ID="primary",
        GOOGLE_CLIENT_ID="client-id",
        GOOGLE_CLIENT_SECRET="client-secret",
        GOOGLE_REFRESH_TOKEN="refresh-token",
    )


class _FakeGoogleEvents:
    """Records every events.list call and replays a fixed busy list."""

    def __init__(self, items=None):
        self.items = items or []
        self.calls: list[dict] = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        events = self

        class _Request:
            def execute(self_inner):
                return {"items": events.items}

        return _Request()


class _FakeGoogleService:
    def __init__(self, items=None):
        self.events_stub = _FakeGoogleEvents(items)

    def events(self):
        return self.events_stub


def _calendar_service(monkeypatch, *, business_hours, items=None, duration=30):
    service = CalendarService(settings=_stub_settings())
    service._business_hours = business_hours
    service._default_slot_minutes = duration
    monkeypatch.setattr(service, "_service", _FakeGoogleService(items))
    return service


_WEEKDAY_HOURS = {
    day: [{"start": "08:00", "end": "12:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
}


async def test_day_scan_costs_exactly_one_calendar_read(monkeypatch):
    """The whole point of `list_available_days`: one events.list for the whole
    window. A day-at-a-time implementation would turn one patient tap into ~20
    Google round-trips, on the hot path of every booking."""
    service = _calendar_service(monkeypatch, business_hours=_WEEKDAY_HOURS)
    start = datetime(2027, 3, 1, tzinfo=_TZ)  # a Monday, safely in the future

    await service.list_available_days(start_day=start, days=DAY_PICKER_WINDOW_DAYS)

    calls = service._service.events_stub.calls
    assert len(calls) == 1
    # ...and it is sized for a multi-week window, not the single-day default.
    assert calls[0]["maxResults"] == DAY_SCAN_MAX_EVENTS
    # One span covering the first and last OPEN day of the window.
    assert calls[0]["timeMin"].startswith("2027-03-01T08:00")
    assert calls[0]["timeMax"].startswith("2027-03-19T12:00")


async def test_day_scan_skips_closed_weekdays(monkeypatch):
    service = _calendar_service(monkeypatch, business_hours=_WEEKDAY_HOURS)
    start = datetime(2027, 3, 1, tzinfo=_TZ)

    days = await service.list_available_days(start_day=start, days=7)

    # Mon-Fri only: the weekend has no configured window at all.
    assert [d.date().isoformat() for d in days] == [
        "2027-03-01",
        "2027-03-02",
        "2027-03-03",
        "2027-03-04",
        "2027-03-05",
    ]


async def test_day_scan_drops_a_fully_booked_day(monkeypatch):
    """A day with no room left must not be offered — that is the difference
    between a tappable list and a list that wastes the patient's tap."""
    busy = [
        {
            "id": "evt-full",
            "summary": "Bloqueio",
            "start": {"dateTime": "2027-03-02T08:00:00-03:00"},
            "end": {"dateTime": "2027-03-02T12:00:00-03:00"},
        }
    ]
    service = _calendar_service(monkeypatch, business_hours=_WEEKDAY_HOURS, items=busy)
    start = datetime(2027, 3, 1, tzinfo=_TZ)

    days = await service.list_available_days(start_day=start, days=3, slot_minutes=60)

    assert [d.date().isoformat() for d in days] == ["2027-03-01", "2027-03-03"]


async def test_day_scan_without_any_open_day_reads_nothing(monkeypatch):
    """No configured window inside the range -> no calendar call either."""
    service = _calendar_service(monkeypatch, business_hours={"sunday": []})
    days = await service.list_available_days(
        start_day=datetime(2027, 3, 1, tzinfo=_TZ), days=5
    )
    assert days == []
    assert service._service.events_stub.calls == []


# ---------------------------------------------------------------------------
# Layer 2 — the picker in flow_router
# ---------------------------------------------------------------------------


def _tenant(**kw):
    base = dict(
        initial_flows={"buttons": ["Serviços e Custo", "Horários", "Outro"]},
        appointment_types=[
            {"name": _SERVICE, "duration_min": 30, "is_active": True, "sort_order": 0}
        ],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        collect_insurance=False,
        insurances=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _conversation(**kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_DAY,
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
    """Fake calendar with an explicit count of free days and a fixed slot list."""

    def __init__(self, day_count=DAY_PICKER_WINDOW_DAYS, slots=None, unavailable=False):
        self._day_count = day_count
        self._slots = (
            slots
            if slots is not None
            else [{"start": "2027-03-01T08:00", "end": "2027-03-01T08:30", "label": "08:00"}]
        )
        self._unavailable = unavailable
        self.tzinfo = _TZ
        self.day_scans: list = []
        self.slot_calls: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.day_scans.append((start_day, days, slot_minutes))
        base = datetime(2027, 3, 1, tzinfo=_TZ)
        return [base + timedelta(days=offset) for offset in range(self._day_count)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.slot_calls.append((day, slot_minutes, max_slots))
        return list(self._slots)


def _day_tap(bubble: SlotsBubble, index: int = 0) -> str:
    """The body a day-row tap produces (schemas.webhook.extract_inbound_body)."""
    row_id, title = bubble.rows[index][0], bubble.rows[index][1]
    return f"{title} ({row_id.split('|', 1)[1]})"


def _control_tap(bubble: SlotsBubble, label: str) -> str:
    for row in bubble.rows:
        if row[1] == label:
            return f"{row[1]} ({row[0].split('|', 1)[1]})"
    raise AssertionError(f"no {label!r} row in {[r[1] for r in bubble.rows]}")


def _row_labels(bubble: SlotsBubble) -> list[str]:
    return [row[1] for row in bubble.rows]


def _roster():
    """Two active professionals, both offering the tenant-wide service."""
    return [
        SimpleNamespace(
            id=uuid4(), name="Dra. Ana", specialty="Cardio", about=None, appointment_types=None
        ),
        SimpleNamespace(
            id=uuid4(), name="Dr. Bruno", specialty=None, about=None, appointment_types=None
        ),
    ]


# --- the row budget ---------------------------------------------------------


@pytest.mark.parametrize("day_count", [1, 8, 9, 20, 60])
@pytest.mark.parametrize("back_target", [None, BACK_TARGET_SERVICE, BACK_TARGET_PROFESSIONAL])
@pytest.mark.parametrize(
    "step", [STEP_AWAITING_DAY, STEP_AWAITING_DAY_RETRY, STEP_AWAITING_DAY_ESCAPE]
)
async def test_day_picker_never_exceeds_the_whatsapp_row_cap(day_count, back_target, step):
    """WhatsApp hard-caps a list at 10 rows and silently drops the rest, so any
    combination of days + paging + Voltar + the escape row that went over would
    lose a row without warning. The original "17 dias úteis + Outro" shape was
    exactly that: 18 rows in a 10-row message."""
    result = await enter_day_picker(
        _conversation(),
        _tenant(),
        _Calendar(day_count=day_count),
        duration_minutes=30,
        back_target=back_target,
        step=step,
    )
    bubble = result.bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    assert 1 <= len(bubble.rows) <= MAX_LIST_ROWS
    # Row titles must survive the 24-char cap untruncated ("Seg, 01/03").
    assert all(len(title) <= 24 for title in _row_labels(bubble))


async def test_day_picker_shows_a_full_page_plus_its_two_controls():
    result = await enter_day_picker(
        _conversation(),
        _tenant(),
        _Calendar(day_count=20),
        duration_minutes=30,
        back_target=BACK_TARGET_SERVICE,
    )
    labels = _row_labels(result.bubbles[0])
    assert len(labels) == MAX_LIST_ROWS
    assert labels[:DAY_PICKER_PAGE_SIZE] == [
        "Seg, 01/03",
        "Ter, 02/03",
        "Qua, 03/03",
        "Qui, 04/03",
        "Sex, 05/03",
        "Sáb, 06/03",
        "Dom, 07/03",
        "Seg, 08/03",
    ]
    assert labels[-2:] == [LABEL_MORE_DAYS, LABEL_BACK]


async def test_day_picker_hides_ver_mais_on_the_last_page():
    result = await enter_day_picker(
        _conversation(),
        _tenant(),
        _Calendar(day_count=5),
        duration_minutes=30,
        back_target=BACK_TARGET_SERVICE,
    )
    labels = _row_labels(result.bubbles[0])
    assert LABEL_MORE_DAYS not in labels
    assert labels[-1] == LABEL_BACK


async def test_day_picker_only_lists_days_the_calendar_reported_free():
    """The picker never invents a day: exactly what `list_available_days`
    returned, in order."""
    result = await enter_day_picker(
        _conversation(), _tenant(), _Calendar(day_count=3), duration_minutes=30
    )
    assert [row[0] for row in result.bubbles[0].rows] == [
        "day|2027-03-01|0",
        "day|2027-03-02|0",
        "day|2027-03-03|0",
    ]


async def test_day_picker_with_no_free_day_offers_the_menu_not_an_empty_list():
    result = await enter_day_picker(
        _conversation(), _tenant(), _Calendar(day_count=0), duration_minutes=30
    )
    assert result.action == "reply"
    assert result.flow_state == FlowState.MENU
    assert isinstance(result.bubbles[0], TextBubble)
    assert isinstance(result.bubbles[1], MenuBubble)


async def test_day_picker_builds_from_one_availability_read():
    """Performance regression guard: rendering the picker must not fan out into
    one call per day."""
    calendar = _Calendar()
    await enter_day_picker(_conversation(), _tenant(), calendar, duration_minutes=45)
    assert len(calendar.day_scans) == 1
    assert calendar.day_scans[0][1] == DAY_PICKER_WINDOW_DAYS
    assert calendar.day_scans[0][2] == 45  # the caller's own duration


# --- pagination -------------------------------------------------------------


async def test_ver_mais_dias_pages_forward_and_the_next_page_still_books():
    tenant, calendar = _tenant(), _Calendar(day_count=20)
    first = await enter_day_picker(
        _conversation(), tenant, calendar, duration_minutes=30, back_target=BACK_TARGET_SERVICE
    )

    second = await route(
        _conversation(), tenant, calendar, _control_tap(first.bubbles[0], LABEL_MORE_DAYS)
    )
    labels = _row_labels(second.bubbles[0])
    assert labels[0] == "Ter, 09/03"  # page 2 starts where page 1 stopped
    assert second.flow_step == STEP_AWAITING_DAY
    assert LABEL_MORE_DAYS in labels  # 20 days -> a third page exists

    third = await route(
        _conversation(), tenant, calendar, _control_tap(second.bubbles[0], LABEL_MORE_DAYS)
    )
    assert _row_labels(third.bubbles[0])[0] == "Qua, 17/03"
    assert LABEL_MORE_DAYS not in _row_labels(third.bubbles[0])  # last page

    # A day tapped on the last page books like any other.
    picked = await route(_conversation(), tenant, calendar, _day_tap(third.bubbles[0]))
    assert picked.flow_step == STEP_AWAITING_SLOT
    assert picked.flow_selected_day == "2027-03-17"


async def test_escolher_outro_dia_returns_to_the_page_it_came_from():
    """The cursor rides in the row id, so "Escolher outro dia" lands the patient
    back where they were reading — not at day 1 with 8 taps to redo."""
    tenant, calendar = _tenant(), _Calendar(day_count=20)
    page_two = await enter_day_picker(
        _conversation(), tenant, calendar, duration_minutes=30, page=1
    )
    slots = await route(_conversation(), tenant, calendar, _day_tap(page_two.bubbles[0]))
    assert slots.flow_step == STEP_AWAITING_SLOT

    back_to_days = await route(
        _conversation(flow_step=STEP_AWAITING_SLOT),
        tenant,
        calendar,
        _control_tap(slots.bubbles[0], LABEL_ANOTHER_DAY),
    )
    assert back_to_days.flow_step == STEP_AWAITING_DAY
    assert _row_labels(back_to_days.bubbles[0])[0] == "Ter, 09/03"  # page 2 again


async def test_a_stale_page_cursor_restarts_instead_of_showing_nothing():
    result = await enter_day_picker(
        _conversation(), _tenant(), _Calendar(day_count=3), duration_minutes=30, page=7
    )
    assert _row_labels(result.bubbles[0])[0] == "Seg, 01/03"


# --- Voltar -----------------------------------------------------------------


async def test_voltar_preserves_service_professional_and_insurance():
    profs = _roster()
    tenant, calendar = _tenant(), _Calendar()
    conv = _conversation(
        flow_selected_professional_id=profs[0].id, flow_selected_insurance="Unimed"
    )
    picker = await enter_day_picker(
        conv, tenant, calendar, duration_minutes=30, back_target=BACK_TARGET_SERVICE
    )

    result = await route(
        conv,
        tenant,
        calendar,
        _control_tap(picker.bubbles[0], LABEL_BACK),
        professionals=profs,
    )
    assert result.action == "reply"
    assert result.flow_step == STEP_AWAITING_SERVICE
    assert result.flow_selected_type == _SERVICE
    assert result.flow_selected_professional_id == profs[0].id
    assert result.flow_selected_insurance == "Unimed"


async def test_voltar_from_the_slot_list_preserves_the_same_fields():
    profs = _roster()
    tenant, calendar = _tenant(), _Calendar()
    conv = _conversation(
        flow_selected_professional_id=profs[0].id, flow_selected_insurance="Unimed"
    )
    picker = await enter_day_picker(
        conv, tenant, calendar, duration_minutes=30, back_target=BACK_TARGET_SERVICE
    )
    slots = await route(
        conv, tenant, calendar, _day_tap(picker.bubbles[0]), professionals=profs
    )
    assert slots.flow_step == STEP_AWAITING_SLOT

    conv.flow_step = STEP_AWAITING_SLOT
    result = await route(
        conv,
        tenant,
        calendar,
        _control_tap(slots.bubbles[0], LABEL_BACK),
        professionals=profs,
    )
    assert result.flow_step == STEP_AWAITING_SERVICE
    assert result.flow_selected_professional_id == profs[0].id
    assert result.flow_selected_insurance == "Unimed"


async def test_voltar_to_the_professional_list_is_supported():
    """The destination a rebooking-after-cancellation flow needs: back to the
    doctor list, keeping everything already chosen."""
    profs = _roster()
    tenant, calendar = _tenant(), _Calendar()
    picker = await enter_day_picker(
        _conversation(flow_selected_professional_id=profs[0].id),
        tenant,
        calendar,
        duration_minutes=30,
        back_target=BACK_TARGET_PROFESSIONAL,
        professionals=profs,
    )
    # The destination rides in the row id, so the tap is self-describing.
    assert _control_tap(picker.bubbles[0], LABEL_BACK).endswith(
        f"({BACK_TARGET_PROFESSIONAL})"
    )

    result = _handle_day_back(
        _conversation(flow_selected_insurance="Unimed"),
        tenant,
        [],
        profs,
        BACK_TARGET_PROFESSIONAL,
    )
    assert result.flow_step == STEP_AWAITING_PROFESSIONAL
    assert result.flow_selected_type == _SERVICE
    assert result.flow_selected_insurance == "Unimed"


# --- free text: still a shortcut, no longer a leak --------------------------


async def test_understood_free_text_still_lists_that_days_slots():
    tenant, calendar = _tenant(), _Calendar()
    result = await route(_conversation(), tenant, calendar, "amanhã")
    assert result.action == "reply"
    assert result.flow_step == STEP_AWAITING_SLOT
    assert calendar.slot_calls  # it really asked the calendar for that day


@pytest.mark.parametrize("gibberish", ["blá blá", "sei lá", "quando der"])
async def test_unreadable_free_text_never_delegates_on_the_first_two_tries(gibberish):
    """The single biggest LLM leak this feature closes. Try 1 re-asks; try 2
    re-asks AND surfaces an explicit, countable escape. Neither spends an
    agent call."""
    tenant, calendar = _tenant(), _Calendar()

    first = await route(_conversation(flow_step=STEP_AWAITING_DAY), tenant, calendar, gibberish)
    assert first.action == "reply"
    assert first.flow_step == STEP_AWAITING_DAY_RETRY
    assert first.bubbles[0].body.startswith("Não entendi a data.")
    assert LABEL_OTHER not in _row_labels(first.bubbles[0])

    second = await route(
        _conversation(flow_step=STEP_AWAITING_DAY_RETRY), tenant, calendar, gibberish
    )
    assert second.action == "reply"
    assert second.flow_step == STEP_AWAITING_DAY_ESCAPE
    assert _row_labels(second.bubbles[0])[-1] == LABEL_OTHER


async def test_the_escape_row_is_the_only_way_the_day_step_reaches_the_llm():
    tenant, calendar = _tenant(), _Calendar()

    # Typed before the escape is offered: still just a re-ask.
    early = await route(_conversation(flow_step=STEP_AWAITING_DAY), tenant, calendar, LABEL_OTHER)
    assert early.action == "reply"
    assert early.flow_step == STEP_AWAITING_DAY_RETRY

    # Tapped once it IS offered: a deliberate hand-off, logged under a step
    # name that tells it apart from a silent leak at `awaiting_day`.
    escape = await route(
        _conversation(flow_step=STEP_AWAITING_DAY_ESCAPE), tenant, calendar, LABEL_OTHER
    )
    assert escape.action == "delegate_llm"
    assert escape.flow_state == FlowState.LLM


async def test_further_misses_stay_on_the_escape_render_forever():
    """Bounded, not looping: the escalation tops out at the escape card and
    keeps re-offering it instead of eventually giving up to the model."""
    tenant, calendar = _tenant(), _Calendar()
    result = await route(
        _conversation(flow_step=STEP_AWAITING_DAY_ESCAPE), tenant, calendar, "ainda não sei"
    )
    assert result.action == "reply"
    assert result.flow_step == STEP_AWAITING_DAY_ESCAPE


async def test_a_day_that_filled_up_between_taps_refreshes_the_picker():
    tenant = _tenant()
    calendar = _Calendar(slots=[])
    picker = await enter_day_picker(_conversation(), tenant, calendar, duration_minutes=30)
    result = await route(_conversation(), tenant, calendar, _day_tap(picker.bubbles[0]))
    assert result.action == "reply"
    assert result.flow_step == STEP_AWAITING_DAY
    assert result.bubbles[0].body.startswith("Esse dia não tem mais horário livre.")


# --- calendar failures reach a human, never the model -----------------------


@pytest.mark.parametrize("step", [STEP_AWAITING_DAY, STEP_AWAITING_DAY_RETRY])
async def test_missing_calendar_is_calendar_unavailable_not_delegate_llm(step):
    result = await route(_conversation(flow_step=step), _tenant(), None, "amanhã")
    assert result.action == "calendar_unavailable"
    assert result.flow_step == step
    assert result.flow_selected_type == _SERVICE


async def test_calendar_outage_is_calendar_unavailable_not_delegate_llm():
    result = await route(_conversation(), _tenant(), _Calendar(unavailable=True), "amanhã")
    assert result.action == "calendar_unavailable"
    assert result.flow_step == STEP_AWAITING_DAY


async def test_manage_branch_calendar_failures_behave_identically():
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_DAY,
        flow_managing_appointment_id=UUID(_APPT_ID),
    )
    result = await route(conv, _tenant(), None, "amanhã", upcoming_appointments=[])
    assert result.action == "calendar_unavailable"
    assert result.flow_step == STEP_MANAGE_DAY
    assert result.flow_managing_appointment_id == UUID(_APPT_ID)


# --- one branch, several flows ---------------------------------------------


def _appt(minutes=40):
    start = datetime(2027, 3, 8, 14, 0, tzinfo=_TZ)
    return {
        "id": _APPT_ID,
        "google_event_id": "evt-a1",
        "appointment_type": _SERVICE,
        "start_at": start,
        "end_at": start + timedelta(minutes=minutes),
    }


async def test_the_same_branch_serves_booking_and_reschedule_identically():
    """"Consolidate, don't copy": the reschedule path used to be a near-clone of
    the booking path. Both now render the same rows, from the same function,
    with only the copy and the flow coordinates differing."""
    tenant, calendar = _tenant(), _Calendar(day_count=20)

    booking = await enter_day_picker(
        _conversation(), tenant, calendar, duration_minutes=30, branch=BOOKING_DAY_BRANCH
    )
    manage = await enter_day_picker(
        _conversation(flow_state=FlowState.MANAGE_BOOKING, flow_step=STEP_MANAGE_DAY),
        tenant,
        calendar,
        duration_minutes=30,
        branch=MANAGE_DAY_BRANCH,
    )

    assert _row_labels(booking.bubbles[0]) == _row_labels(manage.bubbles[0])
    assert booking.flow_state == FlowState.SERVICE_CATALOG
    assert manage.flow_state == FlowState.MANAGE_BOOKING
    assert (booking.flow_step, manage.flow_step) == (STEP_AWAITING_DAY, STEP_MANAGE_DAY)


async def test_the_manage_branch_escalates_free_text_the_same_bounded_way():
    tenant, calendar = _tenant(), _Calendar()
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_DAY,
        flow_managing_appointment_id=UUID(_APPT_ID),
    )
    first = await route(conv, tenant, calendar, "blá blá", upcoming_appointments=[_appt()])
    assert first.action == "reply"
    assert first.flow_step == STEP_MANAGE_DAY_RETRY
    assert first.flow_managing_appointment_id == UUID(_APPT_ID)

    conv.flow_step = STEP_MANAGE_DAY_RETRY
    second = await route(conv, tenant, calendar, "blá blá", upcoming_appointments=[_appt()])
    assert second.flow_step == STEP_MANAGE_DAY_ESCAPE
    assert _row_labels(second.bubbles[0])[-1] == LABEL_OTHER


async def test_the_manage_branch_slots_on_the_original_appointment_length():
    tenant, calendar = _tenant(), _Calendar()
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_DAY,
        flow_managing_appointment_id=UUID(_APPT_ID),
    )
    result = await route(
        conv, tenant, calendar, "amanhã", upcoming_appointments=[_appt(minutes=60)]
    )
    assert result.flow_step == STEP_MANAGE_SLOT
    assert calendar.slot_calls[0][1] == 60
