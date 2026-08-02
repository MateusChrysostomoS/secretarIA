"""Tests for the scoped-help LLM nodes (ai/scoped_help.py) - no network.

The router-facing behavior (step machine, hand-back validation, escalation
bounds) is covered in test_flow_router.py / test_flow_router_multiprofessional
with the node monkeypatched; here we cover the node itself: grounding (the
prompt shows the real options and nothing else), the decision->outcome
normalization, and the empty-catalog / no-history degrades.
"""

import os
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from secretaria.ai import scoped_help  # noqa: E402
from secretaria.ai.scoped_help import (  # noqa: E402
    ScopedHelpOutcome,
    _normalize,
    _professional_help_prompt,
    _ScopedHelpDecision,
    _service_help_prompt,
    run_professional_help,
    run_service_help,
)


def _professionals():
    return [
        SimpleNamespace(
            id=uuid4(),
            name="Dra. Ana",
            specialty="Cardiologia",
            about="Foco em prevenção.",
        ),
        SimpleNamespace(id=uuid4(), name="Dr. Bruno", specialty=None, about=None),
    ]


def _services():
    return [
        {"name": "Primeira Consulta", "price": "R$ 250", "description": "Avaliação completa."},
        {"name": "Retorno"},
    ]


# --------------------------------------------------------------------------
# Prompts: grounded on the real options, scope-specific, final-round rule
# --------------------------------------------------------------------------


def test_professional_prompt_lists_only_real_options():
    prompt = _professional_help_prompt(_professionals(), final_round=False)
    assert "Dra. Ana — Cardiologia — Foco em prevenção." in prompt
    assert "- Dr. Bruno" in prompt
    assert "profissional" in prompt.lower()
    assert "ÚNICOS que existem" in prompt
    assert "ÚLTIMA troca" not in prompt


def test_service_prompt_lists_only_real_options_and_final_rule():
    prompt = _service_help_prompt(_services(), final_round=True)
    assert "Primeira Consulta — R$ 250 — Avaliação completa." in prompt
    assert "- Retorno" in prompt
    assert "serviço" in prompt.lower()
    assert "ÚLTIMA troca" in prompt


def test_prompts_are_scope_distinct():
    """Two nodes, two prompts - never one reused question for both scopes."""
    prof = _professional_help_prompt(_professionals(), final_round=False)
    svc = _service_help_prompt(_services(), final_round=False)
    assert prof != svc
    assert "profissional certo" in prof
    assert "serviço certo" in svc


# --------------------------------------------------------------------------
# _normalize: decision -> outcome, malformed combos collapse to escalate
# --------------------------------------------------------------------------


def test_normalize_valid_pick_and_clarify():
    pick = _normalize(_ScopedHelpDecision(action="pick", choice=" Dra. Ana "), False)
    assert pick == ScopedHelpOutcome(kind="pick", choice="Dra. Ana")
    clarify = _normalize(
        _ScopedHelpDecision(action="clarify", question=" Qual o motivo? "), False
    )
    assert clarify == ScopedHelpOutcome(kind="clarify", question="Qual o motivo?")


def test_normalize_malformed_and_final_round_collapse_to_escalate():
    assert _normalize(_ScopedHelpDecision(action="pick"), False).kind == "escalate"
    assert _normalize(_ScopedHelpDecision(action="pick", choice="  "), False).kind == "escalate"
    assert _normalize(_ScopedHelpDecision(action="clarify"), False).kind == "escalate"
    # The bound is enforced in code: clarify on the final round never survives.
    assert (
        _normalize(
            _ScopedHelpDecision(action="clarify", question="Mais uma?"), True
        ).kind
        == "escalate"
    )
    assert _normalize(_ScopedHelpDecision(action="escalate"), False).kind == "escalate"


# --------------------------------------------------------------------------
# Runners: empty catalog short-circuits; model wiring; history fallback
# --------------------------------------------------------------------------


class _FakeModel:
    def __init__(self, decision):
        self.decision = decision
        self.calls: list[list] = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return self.decision


async def test_empty_options_escalate_without_llm_call(monkeypatch):
    def _explode():
        raise AssertionError("model must not be built for an empty catalog")

    monkeypatch.setattr(scoped_help, "_get_decision_model", _explode)
    out_prof = await run_professional_help(
        conversation_id=None, professionals=[], patient_message="dor", final_round=False
    )
    out_svc = await run_service_help(
        conversation_id=None, services=[], patient_message="dor", final_round=False
    )
    assert out_prof.kind == "escalate"
    assert out_svc.kind == "escalate"


async def test_run_without_conversation_uses_patient_message(monkeypatch):
    fake = _FakeModel(_ScopedHelpDecision(action="pick", choice="Dra. Ana"))
    monkeypatch.setattr(scoped_help, "_get_decision_model", lambda: fake)

    out = await run_professional_help(
        conversation_id=None,
        professionals=_professionals(),
        patient_message="coração acelerado",
        final_round=False,
    )
    assert out == ScopedHelpOutcome(kind="pick", choice="Dra. Ana")
    (messages,) = fake.calls
    assert isinstance(messages[0], SystemMessage)
    assert "Dra. Ana" in messages[0].content
    assert messages[1] == HumanMessage(content="coração acelerado")


async def test_run_service_maps_decision_through_normalize(monkeypatch):
    fake = _FakeModel(_ScopedHelpDecision(action="clarify", question="Primeira vez?"))
    monkeypatch.setattr(scoped_help, "_get_decision_model", lambda: fake)

    out = await run_service_help(
        conversation_id=None,
        services=_services(),
        patient_message="quero marcar algo",
        final_round=False,
    )
    assert out == ScopedHelpOutcome(kind="clarify", question="Primeira vez?")
    (messages,) = fake.calls
    assert "Primeira Consulta" in messages[0].content
