"""ai/chat_model.py: one setting picks the provider; both providers' replies read alike.

Owner decision 2026-10-21: the conversational model moves to Claude Haiku 5.5
(LLM_PROVIDER=anthropic). No network here: models are only constructed.
"""

from types import SimpleNamespace

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from secretaria.ai import chat_model, scoped_help, trace


def _settings(**over):
    base = dict(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="sk-test",
        OPENAI_SECRETARIA_MODEL="gpt-5-mini",
        OPENAI_MAX_TOKENS=2500,
        ANTHROPIC_API_KEY="sk-ant-test",
        ANTHROPIC_SECRETARIA_MODEL="claude-haiku-5-5",
        ANTHROPIC_MAX_TOKENS=4096,
        ANTHROPIC_EFFORT="low",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def use(monkeypatch: pytest.MonkeyPatch):
    def _use(**over):
        monkeypatch.setattr(chat_model, "get_settings", lambda: _settings(**over))

    return _use


def test_openai_stays_the_default_provider(use):
    use()
    model = chat_model.build_chat_model(timeout=60)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-5-mini"
    assert chat_model.active_model_name() == "gpt-5-mini"


def test_anthropic_builds_haiku_with_adaptive_thinking_at_low_effort(use):
    use(LLM_PROVIDER="anthropic")
    model = chat_model.build_chat_model(timeout=30)
    assert isinstance(model, ChatAnthropic)
    assert model.model == "claude-haiku-5-5"
    assert model.max_tokens == 4096
    assert model.thinking == {"type": "adaptive"}
    assert model.output_config == {"effort": "low"}
    assert model.default_request_timeout == 30
    assert chat_model.active_model_name() == "claude-haiku-5-5"
    assert chat_model.active_max_tokens() == 4096


def test_a_provider_without_its_key_fails_loudly(use):
    use(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        chat_model.build_chat_model(timeout=60)


def test_claude_caches_the_system_prompt_and_openai_gets_plain_text(use):
    use(LLM_PROVIDER="anthropic")
    blocks = chat_model.system_message("regras").content
    assert blocks == [{"type": "text", "text": "regras", "cache_control": {"type": "ephemeral"}}]
    use()
    assert chat_model.system_message("regras").content == "regras"


def test_the_visible_text_skips_thinking_and_tool_blocks():
    claude = AIMessage(
        content=[
            {"type": "thinking", "thinking": "", "signature": "x"},
            {"type": "text", "text": " A clínica fica na Rua A. "},
            {"type": "tool_use", "id": "t1", "name": "show_main_menu", "input": {}},
        ]
    )
    assert chat_model.message_text(claude) == "A clínica fica na Rua A."
    assert chat_model.message_text(AIMessage(content=" Olá ")) == "Olá"
    assert chat_model.message_text(AIMessage(content=[{"type": "thinking", "thinking": ""}])) == ""


def test_the_trace_reads_a_claude_turn():
    thinking_only = AIMessage(
        content=[{"type": "thinking", "thinking": "", "signature": "x"}],
        response_metadata={"stop_reason": "max_tokens"},
    )
    summary = trace.summarize_turn([thinking_only])
    assert summary["empty_model_calls"] == 1
    assert summary["finish_reasons"] == ["max_tokens"]
    assert trace.diagnose(summary) == "truncated_no_text"

    answered = AIMessage(
        content=[{"type": "text", "text": "Pronto."}], response_metadata={"stop_reason": "end_turn"}
    )
    assert trace.diagnose(trace.summarize_turn([answered])) == "ok"


def test_a_refusal_reads_as_a_declined_turn():
    refused = AIMessage(content=[], response_metadata={"stop_reason": "refusal"})
    assert trace.summarize_turn([refused])["truncated"] is True


def test_scoped_help_uses_structured_output_on_claude(use, monkeypatch: pytest.MonkeyPatch):
    """Claude cannot force a tool call while thinking: the decision comes as JSON schema."""
    seen = {}

    class _Model:
        def with_structured_output(self, schema, *, method):
            seen["method"] = method
            return object()

    monkeypatch.setattr(scoped_help, "build_chat_model", lambda **kw: _Model())
    monkeypatch.setattr(scoped_help, "_decision_model", None)
    use(LLM_PROVIDER="anthropic")
    scoped_help._get_decision_model()
    assert seen["method"] == "json_schema"

    monkeypatch.setattr(scoped_help, "_decision_model", None)
    use()
    scoped_help._get_decision_model()
    assert seen["method"] == "function_calling"
