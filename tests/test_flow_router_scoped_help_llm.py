"""Scoped help delegates unresolved choices with the booking draft intact."""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from secretaria.ai.scoped_help import ScopedHelpOutcome  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router  # noqa: E402
from tests.test_flow_router import _conversation, _tenant  # noqa: E402


@pytest.mark.parametrize("scope", ["service", "professional"])
async def test_dont_know_opener_stays_administrative_before_scoped_answer(monkeypatch, scope):
    doctor = SimpleNamespace(
        id=uuid4(), name="Dra. Ana", specialty=None, about=None,
        appointment_types=_tenant().appointment_types, business_hours={},
    )
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=getattr(flow_router, f"STEP_AWAITING_{scope.upper()}"),
        flow_selected_professional_id=doctor.id if scope == "service" else None,
        flow_selected_insurance="Unimed",
    )
    opener = await flow_router.route(
        conv, _tenant(), None, flow_router.LABEL_DONT_KNOW, professionals=[doctor, doctor]
    )
    assert opener.action == "reply"
    assert opener.flow_step == getattr(flow_router, f"STEP_{scope.upper()}_HELP")
    text = opener.bubbles[0].body.casefold()
    assert "opções" in text
    assert "clínica" in text
    assert "sentindo" not in text
    assert "precisa" not in text
    assert "certo" not in text

    async def fake_help(**kwargs):
        return ScopedHelpOutcome(kind="escalate")

    monkeypatch.setattr(flow_router, f"run_{scope}_help", fake_help)
    conv.flow_step = opener.flow_step
    result = await flow_router.route(
        conv, _tenant(), None, "Quero ver as opções de consulta", professionals=[doctor, doctor]
    )
    assert result.action == "delegate_llm"
    assert result.flow_state == FlowState.LLM
    assert result.flow_selected_insurance == "Unimed"


@pytest.mark.parametrize("scope", ["service", "professional"])
@pytest.mark.parametrize("kind", ["escalate", "pick", "clarify"])
async def test_unresolved_scoped_help_delegates_with_booking_draft(monkeypatch, scope, kind):
    async def fake_help(**kwargs):
        return ScopedHelpOutcome(
            kind=kind,
            choice="Inexistente" if kind == "pick" else None,
            question="Qual área?" if kind == "clarify" else None,
        )

    monkeypatch.setattr(flow_router, f"run_{scope}_help", fake_help)
    professional_id = uuid4()
    doctor = SimpleNamespace(
        id=professional_id, name="Dra. Ana", specialty=None, about=None,
        appointment_types=_tenant().appointment_types, business_hours={},
    )
    step = getattr(
        flow_router, f"STEP_{scope.upper()}_HELP" + ("_FINAL" if kind == "clarify" else "")
    )
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=step,
        flow_selected_type="Primeira Consulta",
        flow_selected_professional_id=professional_id if scope == "service" else None,
        flow_selected_insurance="Unimed",
        flow_selected_day="2026-10-05",
        flow_selected_slot="2026-10-05T10:00:00-03:00",
        flow_attendee_name="Pessoa atendida",
    )
    res = await flow_router.route(conv, _tenant(), None, "não sei", professionals=[doctor, doctor])
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.LLM
    assert res.flow_selected_type == "Primeira Consulta"
    assert res.flow_selected_professional_id == (professional_id if scope == "service" else None)
    assert res.flow_selected_insurance == "Unimed"
    assert res.flow_selected_day == "2026-10-05"
    assert res.flow_attendee_name == "Pessoa atendida"
    assert res.flow_step is None
    assert res.flow_selected_slot is None
    assert res.bubbles == []
