"""Tests for the deterministic flow router (no DB / network)."""

import os
from types import SimpleNamespace

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import timedelta as _timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble  # noqa: E402
from secretaria.ai.scoped_help import ScopedHelpOutcome  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router  # noqa: E402
from secretaria.services.calendar import CalendarUnavailableError  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    LABEL_BOOK,
    LABEL_CANCEL_APPT,
    LABEL_DONT_KNOW,
    LABEL_MANAGE_APPOINTMENT,
    LABEL_OTHER,
    LABEL_RESCHEDULE,
    SCOPED_HELP_ESCALATE_MESSAGE,
    SERVICE_HELP_OPENER,
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_DAY,
    STEP_AWAITING_SERVICE,
    STEP_AWAITING_SERVICE_CONFIRM,
    STEP_AWAITING_SLOT,
    STEP_MANAGE_ACTION,
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_CONFIRM,
    STEP_MANAGE_DAY,
    STEP_MANAGE_PICK,
    STEP_MANAGE_PICK_CANCEL,
    STEP_MANAGE_PICK_RESCHEDULE,
    STEP_MANAGE_SLOT,
    STEP_SERVICE_HELP,
    STEP_SERVICE_HELP_FINAL,
    MenuBubble,
    enter_booking,
    enter_manage_action,
    route,
)


def _tenant(enabled=True):
    return SimpleNamespace(
        initial_flows={
            "enabled": enabled,
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
            }
        ],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
    )


def _conversation(**kw):
    base = dict(
        flow_state=FlowState.IDLE,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_managing_appointment_id=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeCalendar:
    """Stand-in for CalendarService.

    `days` pins exactly which days `list_available_days` reports (the day
    picker's input); left None, every day of the requested window is free,
    which is the simplest shape for tests that only care about later steps.
    `day_scans` records the calls so "one availability read per picker" is
    testable.
    """

    def __init__(self, slots=None, unavailable=False, days=None):
        self._slots = slots or []
        self._unavailable = unavailable
        self._days = days
        self.tzinfo = ZoneInfo("America/Sao_Paulo")
        self.created: list = []
        self.listed_days: list = []
        self.day_scans: list = []
        self.cancelled: list = []
        self.updated: list = []

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.listed_days.append(day.date() if hasattr(day, "date") else day)
        return self._slots

    async def list_available_days(self, start_day, days, slot_minutes=None):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.day_scans.append((start_day, days, slot_minutes))
        if self._days is not None:
            return list(self._days)
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + _timedelta(days=offset) for offset in range(days)]

    async def create_event(self, start, end, summary, description=""):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.created.append((start, end, summary))
        return {
            "id": "evt123",
            "htmlLink": "https://cal/evt123",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }

    async def cancel_event(self, event_id):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.cancelled.append(event_id)

    async def update_event(self, event_id, start, end):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.updated.append((event_id, start, end))
        return {"id": event_id}


async def test_flows_run_even_when_never_configured():
    """The deterministic flows are unconditional now (flow_router.flows_enabled).

    Regression guard for the gate coming back: a tenant carrying the MODEL DEFAULT
    `initial_flows={}` — which is every clinic at provisioning time, since nothing
    seeds the key and no hub screen writes it — must still be routed deterministically
    instead of falling through to the LLM. `{"enabled": False}` is asserted alongside
    it because an explicitly-stored false is exactly what a stale row would carry.
    """
    for initial_flows in ({}, {"enabled": False}):
        tenant = _tenant()
        tenant.initial_flows = initial_flows
        res = await route(_conversation(), tenant, None, "oi")
        assert res.action == "reply", initial_flows
        assert res.flow_state == FlowState.MENU, initial_flows
        # No configured buttons on these rows -> the router's own defaults.
        assert res.bubbles[0].labels == ["Serviços e Custo", "Remarcar/Cancelar", "Outro"]


async def test_idle_unmatched_shows_menu():
    res = await route(_conversation(), _tenant(), None, "olá tudo bem?")
    assert res.action == "reply"
    assert res.flow_state == FlowState.MENU
    assert isinstance(res.bubbles[0], MenuBubble)
    assert res.bubbles[0].labels == ["Serviços e Custo", "Horários", "Outro"]


async def test_menu_select_services_lists_catalog():
    res = await route(_conversation(flow_state=FlowState.MENU), _tenant(), None, "Serviços e Custo")
    assert res.action == "reply"
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_AWAITING_SERVICE
    assert isinstance(res.bubbles[0], SlotsBubble)


async def test_menu_select_hours_one_shot():
    res = await route(_conversation(flow_state=FlowState.MENU), _tenant(), None, "Horários")
    assert res.action == "reply"
    assert res.flow_state == FlowState.IDLE
    assert isinstance(res.bubbles[0], TextBubble)
    assert "Segunda" in res.bubbles[0].body


async def test_menu_outro_delegates_llm():
    res = await route(_conversation(flow_state=FlowState.MENU), _tenant(), None, "Outro")
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.LLM


async def test_menu_freetext_delegates_llm():
    res = await route(_conversation(flow_state=FlowState.MENU), _tenant(), None, "quero remarcar")
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.LLM


async def test_select_service_shows_detail_with_price():
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_SERVICE)
    res = await route(conv, _tenant(), None, "Primeira Consulta")
    assert res.action == "reply"
    assert res.flow_step == STEP_AWAITING_SERVICE_CONFIRM
    assert res.flow_selected_type == "Primeira Consulta"
    assert isinstance(res.bubbles[0], ButtonBubble)
    assert "R$ 250" in res.bubbles[0].body


async def test_unmatched_service_delegates_preserving_state():
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_SERVICE)
    res = await route(conv, _tenant(), None, "blá blá")
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_AWAITING_SERVICE


async def test_service_confirm_opens_the_day_picker():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Primeira Consulta",
    )
    cal = _FakeCalendar()
    res = await route(conv, _tenant(), cal, "Sim")
    assert res.action == "reply"
    assert res.flow_step == STEP_AWAITING_DAY
    # A tappable day list, not the old free-text "Para quando você gostaria?".
    bubble = res.bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    assert bubble.rows[0][0].startswith("day|")
    # The service's own 40 minutes drive which days count as having room.
    assert cal.day_scans[0][2] == 40


async def test_day_with_slots_lists_them():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_DAY,
        flow_selected_type="Primeira Consulta",
    )
    cal = _FakeCalendar(
        slots=[{"start": "2026-06-15T08:00", "end": "2026-06-15T08:40", "label": "08:00"}]
    )
    res = await route(conv, _tenant(), cal, "segunda")
    assert res.action == "reply"
    assert res.flow_step == STEP_AWAITING_SLOT
    assert isinstance(res.bubbles[0], SlotsBubble)
    assert res.bubbles[0].rows[0][0] == "slot|2026-06-15T08:00"


async def test_day_calendar_unavailable():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_DAY,
        flow_selected_type="Primeira Consulta",
    )
    res = await route(conv, _tenant(), _FakeCalendar(unavailable=True), "segunda")
    assert res.action == "calendar_unavailable"


async def test_slot_tap_shows_confirmation():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SLOT,
        flow_selected_type="Primeira Consulta",
        flow_selected_day="2026-06-15",
    )
    res = await route(conv, _tenant(), _FakeCalendar(), "08:00 (2026-06-15T08:00)")
    assert res.action == "reply"
    assert res.flow_step == STEP_AWAITING_CONFIRMATION
    assert res.flow_selected_slot == "2026-06-15T08:00"
    assert isinstance(res.bubbles[0], ButtonBubble)


async def test_confirmation_books_and_resets():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Primeira Consulta",
        flow_selected_slot="2026-06-15T08:00",
    )
    cal = _FakeCalendar()
    res = await route(conv, _tenant(), cal, "Confirmar", patient_name="João")
    assert res.action == "reply"
    assert res.flow_state == FlowState.IDLE
    assert res.appointment is not None
    assert res.appointment["google_event_id"] == "evt123"
    assert res.appointment["appointment_type"] == "Primeira Consulta"
    # 40-min duration applied (08:00 -> 08:40).
    start, end, summary = cal.created[0]
    assert (end - start).total_seconds() == 40 * 60
    assert summary == "Primeira Consulta - João"


async def test_confirmation_cancel_offers_retry():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Primeira Consulta",
        flow_selected_slot="2026-06-15T08:00",
    )
    res = await route(conv, _tenant(), _FakeCalendar(), "Cancelar")
    assert res.action == "reply"
    assert isinstance(res.bubbles[0], MenuBubble)
    assert res.appointment is None


async def test_retry_yes_relists_the_stored_day_not_a_misparsed_one():
    # Regression: flow_selected_day is an ISO string (YYYY-MM-DD); the retry
    # path must re-list that exact day, not reparse it as DMY.
    from datetime import date

    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step="awaiting_retry_choice",
        flow_selected_type="Primeira Consulta",
        flow_selected_day="2026-12-05",
    )
    cal = _FakeCalendar(
        slots=[{"start": "2026-12-05T08:00", "end": "2026-12-05T08:40", "label": "08:00"}]
    )
    res = await route(conv, _tenant(), cal, "Sim")
    assert res.action == "reply"
    assert res.flow_step == STEP_AWAITING_SLOT
    assert cal.listed_days == [date(2026, 12, 5)]


async def test_retry_menu_returns_to_menu():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step="awaiting_retry_choice",
        flow_selected_day="2026-12-05",
    )
    res = await route(conv, _tenant(), _FakeCalendar(), "Menu principal")
    assert res.action == "reply"
    assert res.flow_state == FlowState.MENU
    assert isinstance(res.bubbles[0], MenuBubble)


async def test_menu_matches_truncated_long_label():
    # WhatsApp truncates button titles to 20 chars; the tap echoes the
    # truncated label. A >20-char menu label must still route.
    tenant = _tenant()
    tenant.initial_flows = {
        "enabled": True,
        "buttons": ["Agendar minha consulta", "Horários", "Outro"],  # 22 chars
    }
    truncated = "Agendar minha consulta"[:20]  # what the tap returns
    res = await route(_conversation(flow_state=FlowState.MENU), tenant, None, truncated)
    assert res.action == "reply"
    assert res.flow_state == FlowState.SERVICE_CATALOG


# --------------------------------------------------------------------------
# Manage flow (cancel / reschedule)
# --------------------------------------------------------------------------

from datetime import datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

_TZ = ZoneInfo("America/Sao_Paulo")

# Appointment ids are real UUIDs in production (Appointment.id is a UUID PK) -
# flow_managing_appointment_id is a proper FK now, so fixtures need valid UUID
# strings (the old flow_selected_type overload got away with "a1").
_APPT_A1_ID = "11111111-1111-4111-8111-111111111111"


def _appt_window(
    appt_id=_APPT_A1_ID, event_id="evt-a1", start=None, minutes=40, type_="Primeira Consulta"
):
    start = start or datetime(2026, 6, 20, 14, 0, tzinfo=_TZ)
    return {
        "id": appt_id,
        "google_event_id": event_id,
        "appointment_type": type_,
        "start_at": start,
        "end_at": start + timedelta(minutes=minutes),
    }


async def test_manage_entry_lists_appointments():
    # 2+ appointments: there IS something to disambiguate, so the pick list
    # shows (the single-appointment case shortcuts - see the test below).
    appts = [
        _appt_window(),
        _appt_window(
            appt_id=str(uuid4()),
            event_id="evt-a2",
            start=datetime(2026, 6, 21, 9, 0, tzinfo=_TZ),
        ),
    ]
    res = await route(
        _conversation(flow_state=FlowState.MENU),
        _tenant(),
        _FakeCalendar(),
        "Remarcar/Cancelar",
        upcoming_appointments=appts,
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_PICK
    assert isinstance(res.bubbles[0], SlotsBubble)
    # The row id smuggles the start ISO so the tap round-trips to the appt.
    assert res.bubbles[0].rows[0][0] == "slot|2026-06-20T14:00:00-03:00"


async def test_manage_entry_single_appointment_skips_pick_to_action_card():
    """Exactly one upcoming appointment: `_enter_manage` skips the one-row
    "qual consulta?" list (a question whose answer the system already knows)
    and lands straight on that appointment's Remarcar/Cancelar/Voltar action
    card - same shortcut precedent as enter_manage_action's single branch."""
    res = await route(
        _conversation(flow_state=FlowState.MENU),
        _tenant(),
        _FakeCalendar(),
        "Remarcar/Cancelar",
        upcoming_appointments=[_appt_window()],
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_ACTION
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)
    assert isinstance(res.bubbles[0], MenuBubble)
    assert res.bubbles[0].labels == ["Remarcar", "Cancelar", "Voltar"]


async def test_manage_entry_empty_returns_menu():
    res = await route(
        _conversation(flow_state=FlowState.MENU),
        _tenant(),
        _FakeCalendar(),
        "Remarcar/Cancelar",
        upcoming_appointments=[],
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.MENU
    assert isinstance(res.bubbles[0], TextBubble)
    assert "não tem" in res.bubbles[0].body.lower()


async def test_manage_pick_shows_action_card():
    appt = _appt_window()
    conv = _conversation(flow_state=FlowState.MANAGE_BOOKING, flow_step=STEP_MANAGE_PICK)
    body = f"20/06 14:00 ({appt['start_at'].isoformat()})"
    res = await route(conv, _tenant(), _FakeCalendar(), body, upcoming_appointments=[appt])
    assert res.action == "reply"
    assert res.flow_step == STEP_MANAGE_ACTION
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)  # appointment id stored
    assert res.flow_selected_type is None  # never the overloaded column anymore
    assert isinstance(res.bubbles[0], MenuBubble)
    assert res.bubbles[0].labels == ["Remarcar", "Cancelar", "Voltar"]


async def test_manage_cancel_confirm_then_apply():
    appt = _appt_window()
    # ACTION -> tap "Cancelar"
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_ACTION,
        flow_managing_appointment_id=UUID(_APPT_A1_ID),
    )
    res = await route(conv, _tenant(), _FakeCalendar(), "Cancelar", upcoming_appointments=[appt])
    assert res.flow_step == STEP_MANAGE_CANCEL_CONFIRM
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)
    assert isinstance(res.bubbles[0], MenuBubble)

    # CANCEL_CONFIRM -> "Sim" -> calls calendar + flags the row for cancel.
    conv2 = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CANCEL_CONFIRM,
        flow_managing_appointment_id=UUID(_APPT_A1_ID),
    )
    cal = _FakeCalendar()
    res2 = await route(conv2, _tenant(), cal, "Sim", upcoming_appointments=[appt])
    assert res2.action == "reply"
    assert res2.flow_state == FlowState.IDLE
    assert res2.flow_managing_appointment_id is None  # cleared: cancel complete
    assert res2.flow_selected_type is None
    assert res2.appointment_cancel_id == "evt-a1"
    assert cal.cancelled == ["evt-a1"]


async def test_manage_cancel_no_keeps_appointment():
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CANCEL_CONFIRM,
        flow_managing_appointment_id=UUID(_APPT_A1_ID),
    )
    res = await route(
        conv, _tenant(), _FakeCalendar(), "Não", upcoming_appointments=[_appt_window()]
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.MENU
    assert res.appointment_cancel_id is None


async def test_manage_cancel_calendar_unavailable():
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CANCEL_CONFIRM,
        flow_managing_appointment_id=UUID(_APPT_A1_ID),
    )
    res = await route(
        conv,
        _tenant(),
        _FakeCalendar(unavailable=True),
        "Sim",
        upcoming_appointments=[_appt_window()],
    )
    assert res.action == "calendar_unavailable"
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)  # carried for retry


async def test_manage_reschedule_full_flow():
    appt = _appt_window(minutes=40)
    tenant = _tenant()

    # ACTION -> "Remarcar" asks for the new day.
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_ACTION,
        flow_managing_appointment_id=UUID(_APPT_A1_ID),
    )
    res = await route(conv, tenant, _FakeCalendar(), "Remarcar", upcoming_appointments=[appt])
    assert res.flow_step == STEP_MANAGE_DAY
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)

    # DAY -> list new slots (40-min duration honoured).
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_DAY,
        flow_managing_appointment_id=UUID(_APPT_A1_ID),
    )
    cal = _FakeCalendar(
        slots=[{"start": "2026-06-22T08:00", "end": "2026-06-22T08:40", "label": "08:00"}]
    )
    res = await route(conv, tenant, cal, "segunda", upcoming_appointments=[appt])
    assert res.flow_step == STEP_MANAGE_SLOT
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)
    assert isinstance(res.bubbles[0], SlotsBubble)

    # SLOT -> recap confirmation card.
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_SLOT,
        flow_managing_appointment_id=UUID(_APPT_A1_ID),
        flow_selected_day="2026-06-22",
    )
    res = await route(
        conv, tenant, _FakeCalendar(), "08:00 (2026-06-22T08:00)", upcoming_appointments=[appt]
    )
    assert res.flow_step == STEP_MANAGE_CONFIRM
    assert res.flow_selected_slot == "2026-06-22T08:00"
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)
    assert isinstance(res.bubbles[0], ButtonBubble)

    # CONFIRM -> update_event with the original 40-min duration.
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CONFIRM,
        flow_managing_appointment_id=UUID(_APPT_A1_ID),
        flow_selected_slot="2026-06-22T08:00",
    )
    cal = _FakeCalendar()
    res = await route(conv, tenant, cal, "Confirmar", upcoming_appointments=[appt])
    assert res.action == "reply"
    assert res.flow_state == FlowState.IDLE
    assert res.flow_managing_appointment_id is None  # cleared: reschedule complete
    assert res.flow_selected_type is None
    assert res.appointment_reschedule["google_event_id"] == "evt-a1"
    event_id, start, end = cal.updated[0]
    assert event_id == "evt-a1"
    assert (end - start).total_seconds() == 40 * 60


async def test_manage_freetext_delegates_preserving_state():
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_PICK,
        flow_managing_appointment_id=UUID(_APPT_A1_ID),
    )
    res = await route(
        conv, _tenant(), _FakeCalendar(), "blá blá", upcoming_appointments=[_appt_window()]
    )
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)  # preserved, not lost


# --------------------------------------------------------------------------
# New pick steps (direct "Remarcar"/"Cancelar" entries with 2+ appointments)
# --------------------------------------------------------------------------


async def test_manage_pick_reschedule_step_resolves_to_begin_reschedule():
    appt = _appt_window()
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING, flow_step=STEP_MANAGE_PICK_RESCHEDULE
    )
    body = f"20/06 14:00 ({appt['start_at'].isoformat()})"
    cal = _FakeCalendar()
    res = await route(conv, _tenant(), cal, body, upcoming_appointments=[appt])
    assert res.action == "reply"
    assert res.flow_step == STEP_MANAGE_DAY
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)
    # The reschedule opens the SAME tappable day picker the first booking uses.
    assert isinstance(res.bubbles[0], SlotsBubble)
    assert res.bubbles[0].rows[0][0].startswith("day|")
    # ...slotted on the ORIGINAL appointment's own 40-minute length.
    assert cal.day_scans[0][2] == 40


async def test_manage_pick_cancel_step_resolves_to_begin_cancel():
    appt = _appt_window()
    conv = _conversation(flow_state=FlowState.MANAGE_BOOKING, flow_step=STEP_MANAGE_PICK_CANCEL)
    body = f"20/06 14:00 ({appt['start_at'].isoformat()})"
    res = await route(conv, _tenant(), _FakeCalendar(), body, upcoming_appointments=[appt])
    assert res.action == "reply"
    assert res.flow_step == STEP_MANAGE_CANCEL_CONFIRM
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)
    assert isinstance(res.bubbles[0], MenuBubble)


async def test_manage_pick_reschedule_garbage_delegates_preserving_state():
    conv = _conversation(
        flow_state=FlowState.MANAGE_BOOKING, flow_step=STEP_MANAGE_PICK_RESCHEDULE
    )
    res = await route(
        conv, _tenant(), _FakeCalendar(), "blá blá", upcoming_appointments=[_appt_window()]
    )
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_PICK_RESCHEDULE


async def test_manage_pick_cancel_garbage_delegates_preserving_state():
    conv = _conversation(flow_state=FlowState.MANAGE_BOOKING, flow_step=STEP_MANAGE_PICK_CANCEL)
    res = await route(
        conv, _tenant(), _FakeCalendar(), "blá blá", upcoming_appointments=[_appt_window()]
    )
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_PICK_CANCEL


# --------------------------------------------------------------------------
# enter_manage_action (new public entry)
# --------------------------------------------------------------------------


async def test_enter_manage_action_empty_matches_enter_manage_empty_reply():
    res = await enter_manage_action("reschedule", _tenant(), [])
    assert res.action == "reply"
    assert res.flow_state == FlowState.MENU
    assert isinstance(res.bubbles[0], TextBubble)
    assert "não tem" in res.bubbles[0].body.lower()

    res_cancel = await enter_manage_action("cancel", _tenant(), [])
    assert res_cancel.flow_state == FlowState.MENU
    assert isinstance(res_cancel.bubbles[0], TextBubble)


async def test_enter_manage_action_single_future_reschedule_begins_directly():
    appt = _appt_window()
    res = await enter_manage_action("reschedule", _tenant(), [appt], calendar=_FakeCalendar())
    assert res.action == "reply"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_DAY
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)
    assert isinstance(res.bubbles[0], SlotsBubble)


async def test_enter_manage_action_reschedule_without_calendar_is_never_the_llm():
    """No agenda in hand (resolution failed, or the appointment's owner could
    not be identified) must hand to a human - never let the model improvise
    availability, and never quietly list the tenant's calendar instead."""
    res = await enter_manage_action("reschedule", _tenant(), [_appt_window()], calendar=None)
    assert res.action == "calendar_unavailable"
    assert res.flow_step == STEP_MANAGE_DAY
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)


async def test_enter_manage_action_single_future_cancel_begins_directly():
    appt = _appt_window()
    res = await enter_manage_action("cancel", _tenant(), [appt])
    assert res.action == "reply"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_CANCEL_CONFIRM
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)
    assert isinstance(res.bubbles[0], MenuBubble)
    assert res.bubbles[0].labels == ["Sim", "Não"]


async def test_enter_manage_action_multiple_shows_intent_pick_list():
    appts = [
        _appt_window(appt_id=str(uuid4()), start=datetime(2026, 6, 20, 14, 0, tzinfo=_TZ)),
        _appt_window(appt_id=str(uuid4()), start=datetime(2026, 6, 21, 9, 0, tzinfo=_TZ)),
    ]
    res = await enter_manage_action("reschedule", _tenant(), appts)
    assert res.action == "reply"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_PICK_RESCHEDULE
    assert isinstance(res.bubbles[0], SlotsBubble)
    assert res.bubbles[0].body == "Qual consulta você quer remarcar?"
    assert len(res.bubbles[0].rows) == 2

    res_cancel = await enter_manage_action("cancel", _tenant(), appts)
    assert res_cancel.flow_step == STEP_MANAGE_PICK_CANCEL
    assert res_cancel.bubbles[0].body == "Qual consulta você quer cancelar?"


async def test_enter_manage_action_caps_pick_list_at_ten():
    appts = [
        _appt_window(
            appt_id=str(uuid4()),
            start=datetime(2026, 6, 20, 8, 0, tzinfo=_TZ) + timedelta(hours=i),
        )
        for i in range(12)
    ]
    res = await enter_manage_action("cancel", _tenant(), appts)
    assert res.action == "reply"
    assert len(res.bubbles[0].rows) == 10


# --------------------------------------------------------------------------
# route() at IDLE: direct "Remarcar"/"Cancelar" vs the classic manage_label
# --------------------------------------------------------------------------


def test_enter_booking_single_professional_lists_services():
    res = enter_booking(_tenant())
    assert res.action == "reply"
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_AWAITING_SERVICE
    assert isinstance(res.bubbles[0], SlotsBubble)


def test_enter_booking_no_services_replies_deterministically():
    tenant = _tenant()
    tenant.appointment_types = []
    res = enter_booking(tenant)
    assert res.action == "reply"
    assert res.flow_state == FlowState.IDLE
    assert isinstance(res.bubbles[0], TextBubble)
    assert "não há serviços" in res.bubbles[0].body.lower()


async def test_route_idle_book_label_enters_directly():
    res = await route(_conversation(flow_state=FlowState.IDLE), _tenant(), None, LABEL_BOOK)
    assert res.action == "reply"
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_AWAITING_SERVICE
    assert isinstance(res.bubbles[0], SlotsBubble)


async def test_single_professional_agendar_adds_no_intermediate_card():
    """Non-regression for the extra turn (PROMPT_FEAT_32 §4.4).

    The multi-doctor menu now offers "Escolher médico" / "Escolher serviço".
    A clinic with ONE professional must NOT get that fork: with a single
    doctor it would be a card with one real option — a dead turn costing a
    round-trip, latency and noise to tell the patient something the system
    already knows. "Agendar" goes straight to the service list, in ONE bubble.
    """
    professional = SimpleNamespace(
        id=uuid4(), name="Dra. Ana", specialty="Cardiologia", about=None, appointment_types=None
    )
    res = await route(
        _conversation(flow_state=FlowState.IDLE),
        _tenant(),
        None,
        LABEL_BOOK,
        professionals=[professional],
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_AWAITING_SERVICE
    assert len(res.bubbles) == 1
    assert isinstance(res.bubbles[0], SlotsBubble)
    assert res.bubbles[0].rows[0][0].startswith("svc|")


async def test_route_idle_book_label_no_services_is_deterministic():
    """Tapping "Agendar" with no active services configured must NEVER fall
    back to the LLM for lack of data."""
    tenant = _tenant()
    tenant.appointment_types = []
    res = await route(_conversation(flow_state=FlowState.IDLE), tenant, None, LABEL_BOOK)
    assert res.action == "reply"
    assert res.flow_state == FlowState.IDLE
    assert isinstance(res.bubbles[0], TextBubble)
    assert "não há serviços" in res.bubbles[0].body.lower()


async def test_route_idle_reschedule_label_enters_directly():
    appt = _appt_window()
    res = await route(
        _conversation(flow_state=FlowState.IDLE),
        _tenant(),
        _FakeCalendar(),
        LABEL_RESCHEDULE,
        upcoming_appointments=[appt],
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_DAY
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)


async def test_route_idle_reschedule_label_no_active_appointment_is_deterministic():
    """Tapping "Remarcar" (the greeting's fixed trio) with NO active
    appointment must NEVER fall back to the LLM for lack of data - a fixed
    pt-BR reply + the menu, reusing enter_manage_action's existing empty-list
    handling."""
    res = await route(
        _conversation(flow_state=FlowState.IDLE),
        _tenant(),
        _FakeCalendar(),
        LABEL_RESCHEDULE,
        upcoming_appointments=[],
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.MENU
    assert isinstance(res.bubbles[0], TextBubble)
    assert "não tem" in res.bubbles[0].body.lower()
    assert isinstance(res.bubbles[1], MenuBubble)  # offers the (booking) menu right away


async def test_route_idle_cancel_label_enters_directly():
    appt = _appt_window()
    res = await route(
        _conversation(flow_state=FlowState.IDLE),
        _tenant(),
        _FakeCalendar(),
        LABEL_CANCEL_APPT,
        upcoming_appointments=[appt],
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_CANCEL_CONFIRM
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)


async def test_route_idle_outro_label_delegates_llm_even_off_menu():
    """"Outro" from the greeting trio reaches the LLM even when the tenant's
    configured single-doctor menu buttons don't include it — the index-based
    menu mapping only knows the configured labels, so the explicit match must
    win first (same place-it-anywhere semantics as the manage label)."""
    tenant = _tenant()
    tenant.initial_flows["buttons"] = ["Agendar", "Horários", "Falar com a equipe"]
    res = await route(_conversation(flow_state=FlowState.IDLE), tenant, None, LABEL_OTHER)
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.LLM


async def test_route_manage_label_still_opens_classic_pick_then_action_card():
    # manage_label ("Remarcar/Cancelar") keeps the neutral pick -> action-card
    # sequence (unlike the direct LABEL_RESCHEDULE/LABEL_CANCEL_APPT taps,
    # which carry their intent); with 2+ appointments the pick list shows.
    appts = [
        _appt_window(),
        _appt_window(
            appt_id=str(uuid4()),
            event_id="evt-a2",
            start=datetime(2026, 6, 22, 10, 0, tzinfo=_TZ),
        ),
    ]
    res = await route(
        _conversation(flow_state=FlowState.IDLE),
        _tenant(),
        _FakeCalendar(),
        "Remarcar/Cancelar",
        upcoming_appointments=appts,
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_PICK


# --------------------------------------------------------------------------
# "Gerenciar consulta" (trio-gerenciar round): the consolidated greeting slot
# --------------------------------------------------------------------------


async def test_route_idle_manage_appointment_single_goes_straight_to_action_card():
    """One upcoming appointment: identify it implicitly and ask ONLY the cheap
    binary question (the existing Remarcar/Cancelar/Voltar action card) -
    never a one-row pick list, never doctor/service again."""
    res = await route(
        _conversation(flow_state=FlowState.IDLE),
        _tenant(),
        _FakeCalendar(),
        LABEL_MANAGE_APPOINTMENT,
        upcoming_appointments=[_appt_window()],
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_ACTION
    assert res.flow_managing_appointment_id == UUID(_APPT_A1_ID)
    assert isinstance(res.bubbles[0], MenuBubble)
    assert res.bubbles[0].labels == [LABEL_RESCHEDULE, LABEL_CANCEL_APPT, "Voltar"]
    assert "O que você gostaria de fazer?" in res.bubbles[0].body


async def test_route_idle_manage_appointment_multiple_shows_pick_list():
    appts = [
        _appt_window(),
        _appt_window(
            appt_id=str(uuid4()),
            event_id="evt-b2",
            start=datetime(2026, 6, 25, 11, 0, tzinfo=_TZ),
        ),
    ]
    res = await route(
        _conversation(flow_state=FlowState.IDLE),
        _tenant(),
        _FakeCalendar(),
        LABEL_MANAGE_APPOINTMENT,
        upcoming_appointments=appts,
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_PICK
    assert isinstance(res.bubbles[0], SlotsBubble)
    assert len(res.bubbles[0].rows) == 2


async def test_route_idle_manage_appointment_empty_is_deterministic():
    """Tapping "Gerenciar consulta" with NO upcoming appointment must NEVER
    fall to the LLM for lack of data - same empty-state reply as the classic
    manage entry."""
    res = await route(
        _conversation(flow_state=FlowState.IDLE),
        _tenant(),
        _FakeCalendar(),
        LABEL_MANAGE_APPOINTMENT,
        upcoming_appointments=[],
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.MENU
    assert isinstance(res.bubbles[0], TextBubble)
    assert "não tem" in res.bubbles[0].body.lower()
    assert isinstance(res.bubbles[1], MenuBubble)


# --------------------------------------------------------------------------
# Scoped service help ("Não sei" on the service list)
# --------------------------------------------------------------------------


async def test_service_list_offers_dont_know_last_row():
    res = await route(_conversation(flow_state=FlowState.MENU), _tenant(), None, "Serviços e Custo")
    rows = res.bubbles[0].rows
    assert rows[-1] == ("svchelp|0", LABEL_DONT_KNOW)
    # The help row never masquerades as a service row.
    assert all(row[0].startswith("svc|") for row in rows[:-1])


async def test_service_list_reserves_help_slot_at_whatsapp_cap():
    """12 active services: 9 real rows + the reserved "Não sei" row = 10
    (WhatsApp's hard cap) - without the reserve, send_list's silent cap
    would drop the help row exactly when the catalog is fullest."""
    tenant = _tenant()
    tenant.appointment_types = [
        {"name": f"Serviço {i:02d}", "duration_min": 30, "is_active": True, "sort_order": i}
        for i in range(12)
    ]
    res = await route(_conversation(flow_state=FlowState.MENU), tenant, None, "Serviços e Custo")
    rows = res.bubbles[0].rows
    assert len(rows) == 10
    assert rows[-1] == ("svchelp|0", LABEL_DONT_KNOW)


async def test_service_dont_know_tap_asks_scoped_opener():
    """The tap itself is deterministic: fixed service-scope question, step
    flips to SERVICE_HELP. No LLM call on the tap."""
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_SERVICE)
    res = await route(conv, _tenant(), None, LABEL_DONT_KNOW)
    assert res.action == "reply"
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_SERVICE_HELP
    assert isinstance(res.bubbles[0], TextBubble)
    assert res.bubbles[0].body == SERVICE_HELP_OPENER


async def test_service_help_pick_hands_back_to_service_detail(monkeypatch):
    """A validated pick re-enters the deterministic flow at the service's own
    detail card - the same result a direct tap on that service produces."""
    captured = {}

    async def _fake_help(*, conversation_id, services, patient_message, final_round):
        captured["services"] = services
        captured["patient_message"] = patient_message
        captured["final_round"] = final_round
        return ScopedHelpOutcome(kind="pick", choice="Primeira Consulta")

    monkeypatch.setattr(flow_router, "run_service_help", _fake_help)
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_SERVICE_HELP)
    res = await route(conv, _tenant(), None, "preciso de uma avaliação completa")
    assert res.action == "reply"
    assert res.flow_step == STEP_AWAITING_SERVICE_CONFIRM
    assert res.flow_selected_type == "Primeira Consulta"
    assert isinstance(res.bubbles[0], ButtonBubble)
    assert "R$ 250" in res.bubbles[0].body
    # Grounding: the node saw the tenant's real catalog and the round flag.
    assert [s["name"] for s in captured["services"]] == ["Primeira Consulta"]
    assert captured["patient_message"] == "preciso de uma avaliação completa"
    assert captured["final_round"] is False


async def test_service_help_pick_outside_catalog_escalates(monkeypatch):
    """A pick that doesn't resolve against the real catalog is never offered
    back to the patient - bounded escalation instead."""

    async def _fake_help(**kwargs):
        return ScopedHelpOutcome(kind="pick", choice="Serviço Inventado")

    monkeypatch.setattr(flow_router, "run_service_help", _fake_help)
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_SERVICE_HELP)
    res = await route(conv, _tenant(), None, "qualquer coisa")
    assert res.action == "handover"
    assert res.flow_state == FlowState.IDLE
    assert res.bubbles[0].body == SCOPED_HELP_ESCALATE_MESSAGE


async def test_service_help_clarify_asks_once_then_final_step(monkeypatch):
    async def _fake_help(**kwargs):
        return ScopedHelpOutcome(kind="clarify", question="É a sua primeira vez na clínica?")

    monkeypatch.setattr(flow_router, "run_service_help", _fake_help)
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_SERVICE_HELP)
    res = await route(conv, _tenant(), None, "não sei bem")
    assert res.action == "reply"
    assert res.flow_step == STEP_SERVICE_HELP_FINAL
    assert res.bubbles[0].body == "É a sua primeira vez na clínica?"


async def test_service_help_clarify_on_final_round_escalates(monkeypatch):
    """The 1-2 exchange bound lives in the router: even if the node asks for
    another clarification on the final step, the flow escalates."""
    captured = {}

    async def _fake_help(**kwargs):
        captured["final_round"] = kwargs["final_round"]
        return ScopedHelpOutcome(kind="clarify", question="Mais uma pergunta?")

    monkeypatch.setattr(flow_router, "run_service_help", _fake_help)
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_SERVICE_HELP_FINAL)
    res = await route(conv, _tenant(), None, "continuo sem saber")
    assert captured["final_round"] is True
    assert res.action == "handover"
    assert res.bubbles[0].body == SCOPED_HELP_ESCALATE_MESSAGE


async def test_service_help_escalate_hands_over(monkeypatch):
    async def _fake_help(**kwargs):
        return ScopedHelpOutcome(kind="escalate")

    monkeypatch.setattr(flow_router, "run_service_help", _fake_help)
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_SERVICE_HELP)
    res = await route(conv, _tenant(), None, "quero falar de outra coisa")
    assert res.action == "handover"
    assert res.flow_state == FlowState.IDLE
    assert res.flow_step is None


async def test_service_help_llm_failure_delegates_to_general_agent(monkeypatch):
    """A scoped-node crash (network, provider outage) degrades to the general
    agent with state preserved - never a dead end for the patient."""

    async def _fake_help(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(flow_router, "run_service_help", _fake_help)
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_SERVICE_HELP)
    res = await route(conv, _tenant(), None, "dor de cabeça")
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_SERVICE_HELP
