"""Tests for the convênio step across every professional topology.

`collect_insurance` and `insurances` are CLINIC-wide settings and the answer
is informational only, so the step must depend on the clinic's configuration
and nothing else. It used to additionally require a multi-doctor selection
(`flow_selected_professional_id`), which meant the single-professional clinic
— the common case — could switch the toggle on in the hub and never be asked.

Same no-DB/no-network style as tests/test_flow_router.py: SimpleNamespace
snapshots plus a fake calendar.
"""

import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402

from secretaria.ai.formatter import SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    INSURANCE_SKIP_DISABLED,
    INSURANCE_SKIP_EMPTY_CATALOG,
    LABEL_INSURANCE_OTHER,
    LABEL_INSURANCE_PARTICULAR,
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_DAY,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_SERVICE_CONFIRM,
    resume_bubbles,
    route,
)

_SERVICE = "Consulta Geral"
_TZ = ZoneInfo("America/Sao_Paulo")


def _tenant(collect_insurance=False, insurances=None):
    return SimpleNamespace(
        initial_flows={"buttons": ["Serviços e Custo", "Horários", "Outro"]},
        appointment_types=[
            {"name": _SERVICE, "duration_min": 30, "is_active": True, "sort_order": 0}
        ],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        collect_insurance=collect_insurance,
        insurances=insurances,
    )


def _professionals(count):
    """`count` active professionals, each offering the tenant's own catalog."""
    return [
        SimpleNamespace(
            id=uuid4(),
            name=f"Dr(a). {index}",
            specialty=None,
            about=None,
            appointment_types=None,
            # NULL on both config columns = inherit the clinic's, which is the
            # state the real row is in until someone gives this doctor their
            # own. `business_hours` matters because the day picker now refuses
            # to open for a professional with no window anywhere.
            business_hours=None,
        )
        for index in range(count)
    ]


def _conversation(**kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE_CONFIRM,
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


class _FakeCalendar:
    tzinfo = _TZ

    async def create_event(self, start, end, summary, description=""):
        return {
            "id": "evt-ins",
            "htmlLink": "https://cal/evt-ins",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }

    async def list_available_days(self, start_day, days, slot_minutes=None):
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(days)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        return [{"start": "2026-08-03T08:00", "end": "2026-08-03T08:30", "label": "08:00"}]


async def _confirm_service(tenant, professionals, selected_professional_id=None):
    """Tap "Sim" on the service-detail card — the step right before convênio."""
    conversation = _conversation(flow_selected_professional_id=selected_professional_id)
    return await route(
        conversation, tenant, _FakeCalendar(), "Sim", professionals=professionals
    )


# --------------------------------------------------------------------------
# The table: collect_insurance on/off x plans empty/filled x 0/1/many doctors
# --------------------------------------------------------------------------


@pytest.mark.parametrize("professional_count", [0, 1, 2, 5])
async def test_insurance_step_asked_on_every_topology_when_configured(professional_count):
    """A clinic-wide setting produces a clinic-wide step, whatever the roster."""
    professionals = _professionals(professional_count)
    selected = professionals[0].id if professional_count > 1 else None
    res = await _confirm_service(
        _tenant(collect_insurance=True, insurances=["Unimed", "Amil"]),
        professionals,
        selected_professional_id=selected,
    )
    assert res.action == "reply"
    assert res.flow_step == STEP_AWAITING_INSURANCE
    bubble = res.bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    assert [row[1] for row in bubble.rows] == [
        "Unimed",
        "Amil",
        LABEL_INSURANCE_PARTICULAR,
        LABEL_INSURANCE_OTHER,
    ]


@pytest.mark.parametrize("professional_count", [0, 1, 2])
@pytest.mark.parametrize(
    ("collect_insurance", "insurances"),
    [(False, ["Unimed"]), (True, []), (True, None), (False, None)],
)
async def test_insurance_step_skipped_when_not_configured(
    professional_count, collect_insurance, insurances
):
    """Toggle off OR an empty plan catalog skips the step — on every topology."""
    professionals = _professionals(professional_count)
    selected = professionals[0].id if professional_count > 1 else None
    res = await _confirm_service(
        _tenant(collect_insurance=collect_insurance, insurances=insurances),
        professionals,
        selected_professional_id=selected,
    )
    assert res.flow_step == STEP_AWAITING_DAY


def test_skip_reason_names_which_configuration_silenced_the_step():
    assert (
        flow_router._insurance_step_skip_reason(_tenant(collect_insurance=False))
        == INSURANCE_SKIP_DISABLED
    )
    assert (
        flow_router._insurance_step_skip_reason(_tenant(collect_insurance=True, insurances=[]))
        == INSURANCE_SKIP_EMPTY_CATALOG
    )
    assert (
        flow_router._insurance_step_skip_reason(
            _tenant(collect_insurance=True, insurances=["Unimed"])
        )
        is None
    )


async def test_tenant_snapshot_without_the_fields_at_all_skips_cleanly():
    """An old snapshot predating the columns must not explode (getattr default)."""
    tenant = _tenant()
    del tenant.collect_insurance
    del tenant.insurances
    res = await _confirm_service(tenant, _professionals(1))
    assert res.flow_step == STEP_AWAITING_DAY


# --------------------------------------------------------------------------
# Single professional: the full service -> convenio -> day sub-path
# --------------------------------------------------------------------------


async def test_single_professional_records_a_listed_plan_then_asks_the_day():
    tenant = _tenant(collect_insurance=True, insurances=["Unimed", "Amil"])
    professionals = _professionals(1)
    conversation = _conversation(flow_step=STEP_AWAITING_INSURANCE)
    res = await route(conversation, tenant, _FakeCalendar(), "Unimed", professionals=professionals)
    assert res.flow_step == STEP_AWAITING_DAY
    assert res.flow_selected_insurance == "Unimed"
    # The service survives the step, so the day question prices the right one.
    assert res.flow_selected_type == _SERVICE


async def test_single_professional_records_particular():
    tenant = _tenant(collect_insurance=True, insurances=["Unimed"])
    res = await route(
        _conversation(flow_step=STEP_AWAITING_INSURANCE),
        tenant,
        _FakeCalendar(),
        LABEL_INSURANCE_PARTICULAR,
        professionals=_professionals(1),
    )
    assert res.flow_selected_insurance == LABEL_INSURANCE_PARTICULAR


async def test_single_professional_other_plan_prompts_then_stores_free_text():
    tenant = _tenant(collect_insurance=True, insurances=["Unimed"])
    professionals = _professionals(1)
    conversation = _conversation(flow_step=STEP_AWAITING_INSURANCE)

    asked = await route(
        conversation, tenant, _FakeCalendar(), LABEL_INSURANCE_OTHER, professionals=professionals
    )
    assert asked.action == "reply"
    assert asked.flow_step == STEP_AWAITING_INSURANCE  # stays: waiting for the name
    assert isinstance(asked.bubbles[0], TextBubble)

    answered = await route(
        conversation, tenant, _FakeCalendar(), "Bradesco Saúde", professionals=professionals
    )
    assert answered.flow_step == STEP_AWAITING_DAY
    assert answered.flow_selected_insurance == "Bradesco Saúde"


async def test_free_text_plan_is_truncated_not_stored_whole():
    """Arbitrary free text is bounded before it reaches the appointment row."""
    tenant = _tenant(collect_insurance=True, insurances=["Unimed"])
    res = await route(
        _conversation(flow_step=STEP_AWAITING_INSURANCE),
        tenant,
        _FakeCalendar(),
        "x" * 500,
        professionals=_professionals(1),
    )
    assert res.flow_selected_insurance is not None
    assert len(res.flow_selected_insurance) == 120


async def test_single_professional_booking_carries_the_insurance_through():
    """service -> convênio -> day -> slot -> confirm keeps the answer on the row."""
    tenant = _tenant(collect_insurance=True, insurances=["Unimed"])
    professionals = _professionals(1)
    conversation = _conversation(
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_slot="2026-08-03T08:00",
        flow_selected_insurance="Unimed",
    )
    res = await route(
        conversation, tenant, _FakeCalendar(), "Confirmar", "João", professionals=professionals
    )
    assert res.appointment is not None
    assert res.appointment["insurance"] == "Unimed"
    # ... and the booking still belongs to the clinic's single professional.
    assert res.appointment["professional_id"] == professionals[0].id


# --------------------------------------------------------------------------
# Resume / re-entry
# --------------------------------------------------------------------------


async def test_resume_rerenders_the_insurance_list_for_a_single_professional():
    tenant = _tenant(collect_insurance=True, insurances=["Unimed"])
    res = await resume_bubbles(
        _conversation(flow_step=STEP_AWAITING_INSURANCE),
        tenant,
        None,
        professionals=_professionals(1),
    )
    assert res.flow_step == STEP_AWAITING_INSURANCE
    assert isinstance(res.bubbles[0], SlotsBubble)


async def test_multi_professional_keeps_its_selection_through_the_step():
    tenant = _tenant(collect_insurance=True, insurances=["Unimed"])
    professionals = _professionals(3)
    res = await _confirm_service(
        tenant, professionals, selected_professional_id=professionals[1].id
    )
    assert res.flow_step == STEP_AWAITING_INSURANCE
    assert res.flow_selected_professional_id == professionals[1].id

    answered = await route(
        _conversation(
            flow_step=STEP_AWAITING_INSURANCE,
            flow_selected_professional_id=professionals[1].id,
        ),
        tenant,
        _FakeCalendar(),
        "Unimed",
        professionals=professionals,
    )
    assert answered.flow_selected_professional_id == professionals[1].id


async def test_tenant_b_never_sees_tenant_a_plans():
    """The plan list comes from the tenant snapshot in hand, nothing global."""
    professionals = _professionals(1)
    a = await _confirm_service(
        _tenant(collect_insurance=True, insurances=["Plano A"]), professionals
    )
    b = await _confirm_service(
        _tenant(collect_insurance=True, insurances=["Plano B"]), professionals
    )
    assert [row[1] for row in a.bubbles[0].rows][0] == "Plano A"
    assert [row[1] for row in b.bubbles[0].rows][0] == "Plano B"
