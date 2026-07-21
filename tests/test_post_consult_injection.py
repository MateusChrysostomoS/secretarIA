"""Unit tests for workers/tasks.py::_should_inject_post_consult_knowledge -
the pure decision gate behind turn-scoped `post_consult_knowledge` injection
(see ai/prompts.py::_format_post_consult_knowledge and
ai/graph.py::run_agent's `include_post_consult_knowledge` parameter).

No DB/HTTP involved: this is a pure function of already-resolved turn facts,
so a plain unit test (no fixtures) is enough - see the orchestration wiring
in workers/tasks.py::_send_bot_reply for how those facts get resolved.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from secretaria.models import FlowState  # noqa: E402
from secretaria.services.patient_context import PatientOpeningState  # noqa: E402
from secretaria.workers.tasks import _should_inject_post_consult_knowledge  # noqa: E402

_KNOWLEDGE = "Retorno em 7 dias. Resultados de exame saem em 48h pelo portal."


def test_none_knowledge_is_always_false():
    """Nothing to inject regardless of how "qualifying" the turn otherwise is."""
    assert (
        _should_inject_post_consult_knowledge(
            None, PatientOpeningState.JUST_HAD_CONSULT, FlowState.LLM, True
        )
        is False
    )


def test_blank_knowledge_is_always_false():
    assert (
        _should_inject_post_consult_knowledge(
            "   ", PatientOpeningState.JUST_HAD_CONSULT, FlowState.LLM, True
        )
        is False
    )


def test_just_had_consult_qualifies():
    assert (
        _should_inject_post_consult_knowledge(
            _KNOWLEDGE, PatientOpeningState.JUST_HAD_CONSULT, FlowState.IDLE, False
        )
        is True
    )


def test_flow_state_llm_qualifies():
    """Conversation already in full-LLM ("Outro"/deviated) mode."""
    assert _should_inject_post_consult_knowledge(_KNOWLEDGE, None, FlowState.LLM, False) is True


def test_delegated_to_llm_this_turn_qualifies():
    """The deterministic router just delegated THIS turn (e.g. the "Outro" tap)."""
    assert _should_inject_post_consult_knowledge(_KNOWLEDGE, None, FlowState.MENU, True) is True


def test_new_state_idle_not_delegated_is_false():
    assert (
        _should_inject_post_consult_knowledge(
            _KNOWLEDGE, PatientOpeningState.NEW, FlowState.IDLE, False
        )
        is False
    )


def test_none_state_idle_not_delegated_is_false():
    """opening_state unresolved (e.g. patient_id unknown), ordinary IDLE turn."""
    assert _should_inject_post_consult_knowledge(_KNOWLEDGE, None, FlowState.IDLE, False) is False


def test_other_opening_states_alone_do_not_qualify():
    """RETURNING_NO_APPOINTMENT / HAS_UPCOMING(_SOON) are not post-consult states."""
    for state in (
        PatientOpeningState.RETURNING_NO_APPOINTMENT,
        PatientOpeningState.HAS_UPCOMING,
        PatientOpeningState.HAS_UPCOMING_SOON,
    ):
        assert (
            _should_inject_post_consult_knowledge(_KNOWLEDGE, state, FlowState.IDLE, False) is False
        )
