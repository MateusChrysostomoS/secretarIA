"""Tests for the deterministic multi-doctor branch of the flow router.

Mirrors tests/test_flow_router.py (no DB / no network — SimpleNamespace
snapshots + a fake calendar). Tenants with 0/1 active professional are covered
there and must stay byte-identical; everything here uses 2+ professionals.
"""

import os
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from secretaria.ai.formatter import SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services.calendar import CalendarUnavailableError  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    BTN_CHOOSE_PROFESSIONAL,
    BTN_FIND_PROFESSIONAL,
    FIND_PROFESSIONAL_OPENER,
    LABEL_INSURANCE_OTHER,
    LABEL_INSURANCE_PARTICULAR,
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_DAY,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_PROFESSIONAL,
    STEP_AWAITING_SERVICE,
    STEP_AWAITING_SERVICE_CONFIRM,
    STEP_AWAITING_SLOT,
    STEP_MANAGE_PICK,
    MenuBubble,
    menu_buttons_for,
    resume_bubbles,
    route,
)

_TZ = ZoneInfo("America/Sao_Paulo")


def _tenant(collect_insurance=False, insurances=None):
    return SimpleNamespace(
        initial_flows={
            "enabled": True,
            "buttons": ["Serviços e Custo", "Horários", "Outro"],
            "menu_label": "Como posso ajudar?",
        },
        appointment_types=[
            {
                "name": "Consulta Geral",
                "duration_min": 30,
                "is_active": True,
                "sort_order": 0,
            }
        ],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        collect_insurance=collect_insurance,
        insurances=insurances,
    )


def _professionals():
    ana = SimpleNamespace(
        id=uuid4(),
        name="Dra. Ana",
        specialty="Cardiologia",
        about="Atendo com foco em prevenção.",
        appointment_types=[
            {"name": "Consulta Cardio", "duration_min": 45, "is_active": True, "sort_order": 0}
        ],
    )
    bruno = SimpleNamespace(
        id=uuid4(),
        name="Dr. Bruno",
        specialty=None,
        about=None,
        appointment_types=None,  # falls back to the tenant's services
    )
    return [ana, bruno]


def _conversation(**kw):
    base = dict(
        flow_state=FlowState.IDLE,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeCalendar:
    def __init__(self, slots=None, unavailable=False):
        self._slots = slots or []
        self._unavailable = unavailable
        self.tzinfo = _TZ
        self.created: list = []
        self.listed: list = []

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.listed.append((day.date() if hasattr(day, "date") else day, slot_minutes))
        return self._slots

    async def create_event(self, start, end, summary, description=""):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.created.append((start, end, summary))
        return {
            "id": "evt-prof",
            "htmlLink": "https://cal/evt-prof",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }


def _tap(professional) -> str:
    """The body a prof-row tap produces (see schemas.webhook.extract_inbound_body)."""
    return f"{str(professional.name)[:24]} ({professional.id})"


# --------------------------------------------------------------------------
# Effective menu
# --------------------------------------------------------------------------


async def test_multi_menu_replaces_buttons():
    profs = _professionals()
    res = await route(_conversation(), _tenant(), None, "oi", professionals=profs)
    assert res.action == "reply"
    assert res.flow_state == FlowState.MENU
    assert isinstance(res.bubbles[0], MenuBubble)
    assert res.bubbles[0].labels == [BTN_CHOOSE_PROFESSIONAL, BTN_FIND_PROFESSIONAL, "Outro"]
    # Labels must survive WhatsApp's 20-char button-title cap untruncated.
    assert all(len(label) <= 20 for label in res.bubbles[0].labels)


async def test_single_professional_keeps_configured_menu():
    profs = _professionals()[:1]
    res = await route(_conversation(), _tenant(), None, "oi", professionals=profs)
    assert res.bubbles[0].labels == ["Serviços e Custo", "Horários", "Outro"]


def test_menu_buttons_for_helper():
    assert menu_buttons_for(_tenant(), True) == [
        BTN_CHOOSE_PROFESSIONAL,
        BTN_FIND_PROFESSIONAL,
        "Outro",
    ]
    assert menu_buttons_for(_tenant(), False) == ["Serviços e Custo", "Horários", "Outro"]


async def test_multi_menu_freetext_delegates_llm():
    res = await route(
        _conversation(flow_state=FlowState.MENU),
        _tenant(),
        None,
        "quero saber os preços",
        professionals=_professionals(),
    )
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.LLM


async def test_multi_menu_outro_delegates_llm():
    res = await route(
        _conversation(flow_state=FlowState.MENU),
        _tenant(),
        None,
        "Outro",
        professionals=_professionals(),
    )
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.LLM


async def test_manage_label_still_typed_reachable_on_multi_menu():
    res = await route(
        _conversation(flow_state=FlowState.MENU),
        _tenant(),
        _FakeCalendar(),
        "Remarcar/Cancelar",
        upcoming_appointments=[
            {
                "id": "a1",
                "google_event_id": "evt-a1",
                "appointment_type": "Consulta",
                "start_at": datetime(2026, 8, 3, 14, 0, tzinfo=_TZ),
                "end_at": datetime(2026, 8, 3, 14, 30, tzinfo=_TZ),
            }
        ],
        professionals=_professionals(),
    )
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_PICK


# --------------------------------------------------------------------------
# "Escolher médico": tappable list -> greeting -> services
# --------------------------------------------------------------------------


async def test_choose_doctor_lists_professionals_with_specialty_description():
    profs = _professionals()
    res = await route(
        _conversation(flow_state=FlowState.MENU),
        _tenant(),
        None,
        BTN_CHOOSE_PROFESSIONAL,
        professionals=profs,
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_AWAITING_PROFESSIONAL
    bubble = res.bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    assert bubble.rows[0] == (f"prof|{profs[0].id}", "Dra. Ana", "Cardiologia")
    # No specialty -> no row description.
    assert bubble.rows[1] == (f"prof|{profs[1].id}", "Dr. Bruno", None)


async def test_professional_tap_sends_greeting_then_own_services():
    profs = _professionals()
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_PROFESSIONAL
    )
    res = await route(conv, _tenant(), None, _tap(profs[0]), professionals=profs)
    assert res.action == "reply"
    assert res.flow_selected_professional_id == profs[0].id
    assert res.flow_step == STEP_AWAITING_SERVICE
    greeting, services = res.bubbles
    assert isinstance(greeting, TextBubble)
    assert greeting.body == "Cardiologia\n\nAtendo com foco em prevenção."
    assert isinstance(services, SlotsBubble)
    # The selected professional's OWN services, not the tenant's.
    assert services.rows[0][1] == "Consulta Cardio"


async def test_professional_without_profile_skips_greeting_bubble():
    profs = _professionals()
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_PROFESSIONAL
    )
    res = await route(conv, _tenant(), None, _tap(profs[1]), professionals=profs)
    assert len(res.bubbles) == 1
    assert isinstance(res.bubbles[0], SlotsBubble)
    # Falls back to the tenant-wide services.
    assert res.bubbles[0].rows[0][1] == "Consulta Geral"
    assert res.flow_selected_professional_id == profs[1].id


async def test_professional_typed_name_matches_too():
    profs = _professionals()
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_PROFESSIONAL
    )
    res = await route(conv, _tenant(), None, "dra. ana", professionals=profs)
    assert res.flow_selected_professional_id == profs[0].id


async def test_unmatched_professional_delegates_preserving_state():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_PROFESSIONAL
    )
    res = await route(conv, _tenant(), None, "blá blá", professionals=_professionals())
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_AWAITING_PROFESSIONAL


async def test_stale_selected_professional_delegates_llm():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE,
        flow_selected_professional_id=uuid4(),  # not in the active snapshot
    )
    res = await route(conv, _tenant(), None, "Consulta Geral", professionals=_professionals())
    assert res.action == "delegate_llm"


# --------------------------------------------------------------------------
# "Procurar médico": deterministic opener -> sticky LLM
# --------------------------------------------------------------------------


async def test_find_doctor_sends_opener_and_enters_llm():
    res = await route(
        _conversation(flow_state=FlowState.MENU),
        _tenant(),
        None,
        BTN_FIND_PROFESSIONAL,
        professionals=_professionals(),
    )
    assert res.action == "reply"
    assert res.flow_state == FlowState.LLM
    assert isinstance(res.bubbles[0], TextBubble)
    assert res.bubbles[0].body == FIND_PROFESSIONAL_OPENER


async def test_llm_mode_preserves_selected_professional():
    profs = _professionals()
    conv = _conversation(
        flow_state=FlowState.LLM, flow_selected_professional_id=profs[0].id
    )
    res = await route(conv, _tenant(), None, "qualquer coisa", professionals=profs)
    assert res.action == "delegate_llm"
    assert res.flow_selected_professional_id == profs[0].id


# --------------------------------------------------------------------------
# Convênio step (multi-doctor branch only; informational only)
# --------------------------------------------------------------------------


def _conv_at_service_confirm(professional, insurance_tenant=True):
    return _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Consulta Cardio",
        flow_selected_professional_id=professional.id,
    )


async def test_service_confirm_enters_insurance_step_when_collected():
    profs = _professionals()
    tenant = _tenant(collect_insurance=True, insurances=["Unimed", "Amil"])
    res = await route(
        _conv_at_service_confirm(profs[0]), tenant, None, "Sim", professionals=profs
    )
    assert res.action == "reply"
    assert res.flow_step == STEP_AWAITING_INSURANCE
    assert res.flow_selected_professional_id == profs[0].id
    bubble = res.bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    titles = [row[1] for row in bubble.rows]
    assert titles == ["Unimed", "Amil", LABEL_INSURANCE_PARTICULAR, LABEL_INSURANCE_OTHER]


async def test_service_confirm_skips_insurance_when_not_collected():
    profs = _professionals()
    res = await route(
        _conv_at_service_confirm(profs[0]), _tenant(), None, "Sim", professionals=profs
    )
    assert res.flow_step == STEP_AWAITING_DAY


async def test_single_professional_flow_never_asks_insurance():
    # Even with collect_insurance on, the tenant-level (no selected
    # professional) flow keeps today's behavior: straight to the day question.
    tenant = _tenant(collect_insurance=True, insurances=["Unimed"])
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Consulta Geral",
    )
    res = await route(conv, tenant, None, "Sim")
    assert res.flow_step == STEP_AWAITING_DAY


async def test_insurance_tap_records_and_asks_day():
    profs = _professionals()
    tenant = _tenant(collect_insurance=True, insurances=["Unimed", "Amil"])
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_INSURANCE,
        flow_selected_type="Consulta Cardio",
        flow_selected_professional_id=profs[0].id,
    )
    res = await route(conv, tenant, None, "Unimed", professionals=profs)
    assert res.flow_step == STEP_AWAITING_DAY
    assert res.flow_selected_insurance == "Unimed"
    assert res.flow_selected_professional_id == profs[0].id


async def test_insurance_particular_records_label():
    profs = _professionals()
    tenant = _tenant(collect_insurance=True, insurances=["Unimed"])
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_INSURANCE,
        flow_selected_type="Consulta Cardio",
        flow_selected_professional_id=profs[0].id,
    )
    res = await route(conv, tenant, None, LABEL_INSURANCE_PARTICULAR, professionals=profs)
    assert res.flow_step == STEP_AWAITING_DAY
    assert res.flow_selected_insurance == LABEL_INSURANCE_PARTICULAR


async def test_insurance_other_prompts_then_stores_free_text():
    profs = _professionals()
    tenant = _tenant(collect_insurance=True, insurances=["Unimed"])
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_INSURANCE,
        flow_selected_type="Consulta Cardio",
        flow_selected_professional_id=profs[0].id,
    )
    res = await route(conv, tenant, None, LABEL_INSURANCE_OTHER, professionals=profs)
    assert res.action == "reply"
    assert res.flow_step == STEP_AWAITING_INSURANCE  # stays: waiting for the name
    assert isinstance(res.bubbles[0], TextBubble)

    res2 = await route(conv, tenant, None, "Bradesco Saúde", professionals=profs)
    assert res2.flow_step == STEP_AWAITING_DAY
    assert res2.flow_selected_insurance == "Bradesco Saúde"


# --------------------------------------------------------------------------
# End-to-end: day -> slot -> confirm -> booked with professional_id/insurance
# --------------------------------------------------------------------------


async def test_day_uses_professionals_own_service_duration():
    profs = _professionals()
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_DAY,
        flow_selected_type="Consulta Cardio",
        flow_selected_professional_id=profs[0].id,
        flow_selected_insurance="Unimed",
    )
    cal = _FakeCalendar(
        slots=[{"start": "2026-08-03T08:00", "end": "2026-08-03T08:45", "label": "08:00"}]
    )
    res = await route(conv, _tenant(), cal, "segunda", professionals=profs)
    assert res.flow_step == STEP_AWAITING_SLOT
    # 45-min duration comes from the professional's own service catalog.
    assert cal.listed[0][1] == 45
    assert res.flow_selected_professional_id == profs[0].id
    assert res.flow_selected_insurance == "Unimed"


async def test_confirmation_books_with_professional_and_insurance():
    profs = _professionals()
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Consulta Cardio",
        flow_selected_slot="2026-08-03T08:00",
        flow_selected_professional_id=profs[0].id,
        flow_selected_insurance="Unimed",
    )
    cal = _FakeCalendar()
    res = await route(conv, _tenant(), cal, "Confirmar", patient_name="João", professionals=profs)
    assert res.action == "reply"
    assert res.flow_state == FlowState.IDLE
    assert res.appointment is not None
    assert res.appointment["professional_id"] == profs[0].id
    assert res.appointment["insurance"] == "Unimed"
    # The professional's own 45-min duration drives the event window.
    start, end, _summary = cal.created[0]
    assert (end - start).total_seconds() == 45 * 60
    # Selection cleared once booked.
    assert res.flow_selected_professional_id is None
    assert res.flow_selected_insurance is None


async def test_confirmation_without_professional_keeps_plain_appointment_dict():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Consulta Geral",
        flow_selected_slot="2026-08-03T08:00",
    )
    res = await route(conv, _tenant(), _FakeCalendar(), "Confirmar")
    assert res.appointment is not None
    assert "professional_id" not in res.appointment
    assert "insurance" not in res.appointment


async def test_retry_menu_shows_multi_menu():
    profs = _professionals()
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step="awaiting_retry_choice",
        flow_selected_professional_id=profs[0].id,
    )
    res = await route(conv, _tenant(), _FakeCalendar(), "Menu principal", professionals=profs)
    assert res.flow_state == FlowState.MENU
    assert res.bubbles[0].labels == [BTN_CHOOSE_PROFESSIONAL, BTN_FIND_PROFESSIONAL, "Outro"]


# --------------------------------------------------------------------------
# Resume (returning patient taps "continue")
# --------------------------------------------------------------------------


async def test_resume_rerenders_professional_list():
    profs = _professionals()
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_PROFESSIONAL
    )
    res = await resume_bubbles(conv, _tenant(), None, professionals=profs)
    assert res.flow_step == STEP_AWAITING_PROFESSIONAL
    assert isinstance(res.bubbles[0], SlotsBubble)
    assert res.bubbles[0].rows[0][1] == "Dra. Ana"


async def test_resume_rerenders_insurance_list():
    profs = _professionals()
    tenant = _tenant(collect_insurance=True, insurances=["Unimed"])
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_INSURANCE,
        flow_selected_type="Consulta Cardio",
        flow_selected_professional_id=profs[0].id,
    )
    res = await resume_bubbles(conv, tenant, None, professionals=profs)
    assert res.flow_step == STEP_AWAITING_INSURANCE
    assert res.flow_selected_professional_id == profs[0].id


async def test_resume_with_stale_professional_falls_back_to_menu():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE,
        flow_selected_professional_id=uuid4(),
    )
    res = await resume_bubbles(conv, _tenant(), None, professionals=_professionals())
    assert res.flow_state == FlowState.MENU
    assert isinstance(res.bubbles[0], MenuBubble)
