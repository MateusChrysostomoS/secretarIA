"""The chat vendor (ai/chat_model.py): Claude with an Anthropic key, OpenAI only without one.

No network: the models are only built and inspected, never invoked. The real-model proof lives
in the task ledger (TASK-069).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from secretaria.ai import chat_model, trace


def _settings(**over):
    base = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_SECRETARIA_MODEL": "gpt-5-mini",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "ANTHROPIC_SECRETARIA_MODEL": "claude-opus-5-5",
        "ANTHROPIC_MAX_TOKENS": 16000,
        "ANTHROPIC_EFFORT": "",
        "ANTHROPIC_THINKING": "adaptive",
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def use(monkeypatch):
    def _use(**over):
        monkeypatch.setattr(chat_model, "get_settings", lambda: _settings(**over))

    return _use


class _Shape(BaseModel):
    answer: str


def test_openai_answers_only_without_an_anthropic_key_with_the_callers_token_cap(use):
    use(ANTHROPIC_API_KEY="")
    model = chat_model.build_chat_model(openai_max_tokens=2500, max_retries=5, timeout=60)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-5-mini"
    assert model.max_retries == 5
    assert chat_model.model_name() == "gpt-5-mini"
    assert chat_model.token_cap(2500) == 2500


def test_an_anthropic_key_gets_claude_with_its_own_budget_and_refusal_fallback(use):
    use()
    model = chat_model.build_chat_model(openai_max_tokens=2500, max_retries=5, timeout=60)
    assert isinstance(model, ChatAnthropic)
    assert model.model == "claude-opus-5-5"
    assert model.max_tokens == 16000  # never the OpenAI cap: Claude's thinking counts against it
    assert model.max_retries == 5
    assert model.default_request_timeout == 60
    assert model.betas == ["server-side-fallback-2026-07-01"]
    assert model.model_kwargs == {"fallbacks": "default"}
    assert not model.output_config  # empty effort = the model's own default
    assert chat_model.model_name() == "claude-opus-5-5"
    assert chat_model.token_cap(2500) == 16000


def test_effort_is_passed_and_haiku_gets_no_fallback(use):
    use(
        ANTHROPIC_SECRETARIA_MODEL="claude-haiku-5-5",
        ANTHROPIC_EFFORT="LOW",
    )
    model = chat_model.build_chat_model(openai_max_tokens=2500, max_retries=2, timeout=30)
    assert model.output_config == {"effort": "low"}
    assert not model.betas
    assert "fallbacks" not in (model.model_kwargs or {})


def test_no_key_at_all_fails_loudly(use):
    use(OPENAI_API_KEY="", ANTHROPIC_API_KEY="")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        chat_model.build_chat_model(openai_max_tokens=2500, max_retries=5, timeout=60)


def test_claude_without_its_key_falls_back_to_openai_and_says_so(use, monkeypatch):
    # A deploy that forgot ANTHROPIC_API_KEY must not leave patients unanswered.
    errors = []
    monkeypatch.setattr(chat_model, "_fallback_logged", False)
    monkeypatch.setattr(chat_model.logger, "error", lambda event, **kw: errors.append(event))
    use(ANTHROPIC_API_KEY="")
    model = chat_model.build_chat_model(openai_max_tokens=2500, max_retries=5, timeout=60)
    assert isinstance(model, ChatOpenAI)
    assert chat_model.model_name() == "gpt-5-mini"
    assert errors == ["llm_provider_fallback"]  # once per process, not once per turn


def test_claude_haiku_is_the_shipped_default():
    from secretaria.config import Settings

    fields = Settings.model_fields
    assert "LLM_PROVIDER" not in fields  # the key decides, not a switch
    assert fields["ANTHROPIC_SECRETARIA_MODEL"].default == "claude-haiku-5-5"


def test_a_leftover_llm_provider_setting_cannot_pull_the_chat_back_to_openai(use):
    # Owner rule: with an Anthropic key set, Claude answers - whatever the old switch says.
    use(LLM_PROVIDER="openai")
    model = chat_model.build_chat_model(openai_max_tokens=2500, max_retries=5, timeout=60)
    assert isinstance(model, ChatAnthropic)


@pytest.mark.parametrize(
    ("provider", "method"), [("openai", "function_calling"), ("anthropic", "json_schema")]
)
def test_structured_output_method_per_vendor(use, monkeypatch, provider, method):
    # Claude Opus/Sonnet 5.5 reject a forced tool call, which is what function_calling does.
    use(ANTHROPIC_API_KEY="sk-ant-test" if provider == "anthropic" else "")
    seen = {}
    cls = ChatAnthropic if provider == "anthropic" else ChatOpenAI

    def fake(self, schema, *, method, **kw):
        seen.update(schema=schema, method=method)
        return "structured"

    monkeypatch.setattr(cls, "with_structured_output", fake)
    out = chat_model.build_structured_model(_Shape, openai_max_tokens=100, max_retries=1, timeout=5)
    assert out == "structured"
    assert seen == {"schema": _Shape, "method": method}


def test_reply_text_reads_only_visible_text():
    assert chat_model.reply_text(AIMessage(content="  Olá! ")) == "Olá!"
    claude = AIMessage(
        content=[
            {"type": "thinking", "thinking": "o paciente quer remarcar", "signature": "x"},
            {"type": "text", "text": "Claro, "},
            {"type": "text", "text": "posso ajudar."},
        ]
    )
    assert chat_model.reply_text(claude) == "Claro, posso ajudar."
    only_tool = AIMessage(content=[{"type": "tool_use", "id": "t1", "name": "x", "input": {}}])
    assert chat_model.reply_text(only_tool) == ""
    assert chat_model.reply_text(None) == ""


def test_trace_understands_claude_stop_reasons_and_blocks():
    cut = AIMessage(
        content=[{"type": "thinking", "thinking": "...", "signature": "x"}],
        response_metadata={"stop_reason": "max_tokens"},
    )
    summary = trace.summarize_turn([cut])
    assert summary["finish_reasons"] == ["max_tokens"]
    assert summary["truncated"] is True
    assert summary["final_reply_len"] == 0
    assert trace.diagnose(summary) == "truncated_no_text"

    tool_call = AIMessage(
        content=[{"type": "text", "text": "Vou verificar."}],
        tool_calls=[{"name": "check_availability", "args": {"day": "x"}, "id": "t1"}],
        response_metadata={"stop_reason": "tool_use"},
    )
    result = ToolMessage(content="ok", tool_call_id="t1", name="check_availability")
    done = AIMessage(
        content=[{"type": "text", "text": "Temos horário às 10h."}],
        response_metadata={"stop_reason": "end_turn"},
    )
    summary = trace.summarize_turn([tool_call, result, done])
    assert summary["empty_model_calls"] == 0
    assert summary["final_reply_len"] == len("Temos horário às 10h.")
    assert trace.diagnose(summary) == "ok"


def test_refusal_is_a_failed_turn_not_ok():
    refused = AIMessage(content=[], response_metadata={"stop_reason": "refusal"})
    assert trace.diagnose(trace.summarize_turn([refused])) == "truncated_no_text"


def test_markdown_bold_becomes_whatsapp_bold():
    text = "Vá **agora** ao pronto-socorro ou ligue **192 (SAMU)**."
    expected = "Vá *agora* ao pronto-socorro ou ligue *192 (SAMU)*."
    assert chat_model.whatsapp_emphasis(text) == expected
    # Already WhatsApp-shaped, lone asterisks and empty pairs are left alone.
    for same in ["*já* em negrito", "2 * 3 = 6", "****", "** solto **"]:
        assert chat_model.whatsapp_emphasis(same) == same


def test_big_schemas_can_opt_out_of_claudes_constrained_output(use, monkeypatch):
    # The context extraction's schema is refused by Claude's constrained output ("Schema is
    # too complex"); it asks for the tool instead.
    use()
    seen = {}

    def fake(self, schema, *, method, **kw):
        seen["method"] = method
        return "structured"

    monkeypatch.setattr(ChatAnthropic, "with_structured_output", fake)
    chat_model.build_structured_model(
        _Shape, openai_max_tokens=100, max_retries=1, timeout=5, native_schema=False
    )
    assert seen["method"] == "function_calling"


def test_thinking_stays_the_models_default_unless_switched_off(monkeypatch):
    base = dict(
        ANTHROPIC_API_KEY="k",
        OPENAI_API_KEY="",
        ANTHROPIC_SECRETARIA_MODEL="claude-haiku-5-5",
        ANTHROPIC_MAX_TOKENS=4096,
        ANTHROPIC_EFFORT="",
        ANTHROPIC_THINKING="adaptive",
    )

    def build(**over):
        from types import SimpleNamespace

        monkeypatch.setattr(chat_model, "get_settings", lambda: SimpleNamespace(**{**base, **over}))
        return chat_model.build_chat_model(openai_max_tokens=2500, max_retries=2, timeout=30)

    assert build().thinking is None
    assert build(ANTHROPIC_THINKING="disabled").thinking == {"type": "disabled"}


def test_claude_caches_the_system_prompt_and_openai_gets_plain_text(monkeypatch):
    from types import SimpleNamespace

    def use(anthropic_key="k"):
        monkeypatch.setattr(
            chat_model,
            "get_settings",
            lambda: SimpleNamespace(ANTHROPIC_API_KEY=anthropic_key, OPENAI_API_KEY="o"),
        )

    use()
    assert chat_model.system_message("regras").content == [
        {"type": "text", "text": "regras", "cache_control": {"type": "ephemeral"}}
    ]
    # No Anthropic key -> OpenAI answers, so the prompt is plain text there.
    use(anthropic_key="")
    assert chat_model.system_message("regras").content == "regras"
