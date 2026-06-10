"""Tests for the deterministic flow router (no DB / network)."""

import os
from types import SimpleNamespace

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from zoneinfo import ZoneInfo  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services.calendar import CalendarUnavailableError  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_DAY,
    STEP_AWAITING_SERVICE,
    STEP_AWAITING_SERVICE_CONFIRM,
    STEP_AWAITING_SLOT,
    MenuBubble,
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
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeCalendar:
    def __init__(self, slots=None, unavailable=False):
        self._slots = slots or []
        self._unavailable = unavailable
        self.tzinfo = ZoneInfo("America/Sao_Paulo")
        self.created: list = []
        self.listed_days: list = []

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.listed_days.append(day.date() if hasattr(day, "date") else day)
        return self._slots

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


async def test_disabled_flows_delegate_llm():
    res = await route(_conversation(), _tenant(enabled=False), None, "oi")
    assert res.action == "delegate_llm"


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


async def test_service_confirm_asks_day():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Primeira Consulta",
    )
    res = await route(conv, _tenant(), None, "Sim")
    assert res.action == "reply"
    assert res.flow_step == STEP_AWAITING_DAY


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
