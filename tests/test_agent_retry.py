"""Tests for the transient-network retry inside run_agent.

Verifies that a single APIConnectionError / SSLEOFError is swallowed and
the agent is re-invoked, that a persistent failure falls through to the
FALLBACK_REPLY, and that the failure log carries the exception class name
for future debugging.
"""

import os
import ssl

# Match the env conftest sets so secretaria imports succeed.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import pytest  # noqa: E402

from secretaria.ai import graph  # noqa: E402
from secretaria.services.calendar import CalendarUnavailableError  # noqa: E402


class _FakeError(Exception):
    """Stand-in for a non-transient runtime error (e.g. a bug in the agent)."""


@pytest.fixture(autouse=True)
def _skip_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass DB history loading so the tests don't need Postgres."""

    async def _empty(_conversation_id):  # noqa: ANN001
        return []

    monkeypatch.setattr(graph, "_load_history", _empty)


async def test_retry_recovers_after_one_ssl_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def _flaky(_messages):  # noqa: ANN001
        calls.append(1)
        if len(calls) == 1:
            raise ssl.SSLEOFError("unexpected eof while reading")
        return "Ok, vou marcar."

    monkeypatch.setattr(graph, "invoke_agent", _flaky)
    monkeypatch.setattr(graph.asyncio, "sleep", _noop_sleep)

    reply = await graph.run_agent(
        "olá",
        context={"conversation_id": "11111111-2222-3333-4444-555555555555"},
    )
    assert reply == "Ok, vou marcar."
    assert len(calls) == 2


async def test_persistent_ssl_eof_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def _always_ssl_eof(_messages):  # noqa: ANN001
        calls.append(1)
        raise ssl.SSLEOFError("unexpected eof while reading")

    monkeypatch.setattr(graph, "invoke_agent", _always_ssl_eof)
    monkeypatch.setattr(graph.asyncio, "sleep", _noop_sleep)

    reply = await graph.run_agent(
        "olá",
        context={"conversation_id": "11111111-2222-3333-4444-555555555555"},
    )
    assert reply == graph.FALLBACK_REPLY
    # First attempt + one retry = exactly two invocations.
    assert len(calls) == 2


async def test_non_transient_error_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def _runtime_bug(_messages):  # noqa: ANN001
        calls.append(1)
        raise _FakeError("boom")

    monkeypatch.setattr(graph, "invoke_agent", _runtime_bug)
    monkeypatch.setattr(graph.asyncio, "sleep", _noop_sleep)

    reply = await graph.run_agent(
        "olá",
        context={"conversation_id": "11111111-2222-3333-4444-555555555555"},
    )
    assert reply == graph.FALLBACK_REPLY
    # Non-transient → no retry: only one attempt should have been made.
    assert len(calls) == 1


async def test_failure_log_records_exception_class(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _runtime_bug(_messages):  # noqa: ANN001
        raise _FakeError("boom")

    monkeypatch.setattr(graph, "invoke_agent", _runtime_bug)
    monkeypatch.setattr(graph.asyncio, "sleep", _noop_sleep)

    captured: dict = {}
    original_error = graph.logger.error

    def _capture(event, **kwargs):  # noqa: ANN001
        captured["event"] = event
        captured.update(kwargs)
        return original_error(event, **kwargs)

    monkeypatch.setattr(graph.logger, "error", _capture)

    await graph.run_agent(
        "olá",
        context={"conversation_id": "11111111-2222-3333-4444-555555555555"},
    )
    assert captured.get("event") == "ai_run_agent_failed"
    assert captured.get("error_type") == "_FakeError"


async def test_calendar_unavailable_returns_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CalendarUnavailableError from a tool must surface the degradation
    sentinel (not the generic retry fallback) so the worker hands off to a human.
    """
    calls: list[int] = []

    async def _calendar_down(_messages):  # noqa: ANN001
        calls.append(1)
        raise CalendarUnavailableError("refresh token revoked")

    monkeypatch.setattr(graph, "invoke_agent", _calendar_down)
    monkeypatch.setattr(graph.asyncio, "sleep", _noop_sleep)

    reply = await graph.run_agent(
        "quero marcar amanhã",
        context={"conversation_id": "11111111-2222-3333-4444-555555555555"},
    )
    assert reply == graph.CALENDAR_UNAVAILABLE_SENTINEL
    # Not a transient network error -> no retry.
    assert len(calls) == 1


async def _noop_sleep(_seconds: float) -> None:
    """Skip the 1-second backoff so the test suite stays fast."""
    return None
