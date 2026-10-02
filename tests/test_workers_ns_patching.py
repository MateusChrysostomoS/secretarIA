"""`workers_ns` must patch a name in every split module that has it, and undo it (TASK-023)."""

from __future__ import annotations

import pytest

from secretaria.workers import tasks
from secretaria.workers.shared import handover, sender
from tests._patching import workers_ns


def test_setattr_reaches_every_module_that_has_the_name(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(workers_ns, "async_session_factory", sentinel)
    assert tasks.async_session_factory is sentinel
    assert sender.async_session_factory is sentinel
    assert handover.async_session_factory is sentinel


def test_setattr_is_undone_in_every_module(monkeypatch: pytest.MonkeyPatch) -> None:
    before = tasks.async_session_factory
    with monkeypatch.context() as scoped:
        scoped.setattr(workers_ns, "async_session_factory", object())
    assert tasks.async_session_factory is before
    assert sender.async_session_factory is before
    assert handover.async_session_factory is before


def test_undo_gives_each_module_its_own_original_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every split module owns a distinct `logger`. Undoing a patch must not collapse
    them into the facade's (that leaked between tests and hid a real failure)."""
    from secretaria.workers import orchestrator

    facade_logger, orch_logger = tasks.logger, orchestrator.logger
    assert facade_logger is not orch_logger
    with monkeypatch.context() as scoped:
        scoped.setattr(workers_ns, "logger", object())
        assert orchestrator.logger is not orch_logger
    assert tasks.logger is facade_logger
    assert orchestrator.logger is orch_logger


def test_nested_patches_of_one_name_unwind_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    before = tasks.async_session_factory
    first, second = object(), object()
    with monkeypatch.context() as outer:
        outer.setattr(workers_ns, "async_session_factory", first)
        with monkeypatch.context() as inner:
            inner.setattr(workers_ns, "async_session_factory", second)
            assert sender.async_session_factory is second
        assert sender.async_session_factory is first
    assert sender.async_session_factory is before
    assert handover.async_session_factory is before


def test_reading_goes_through_the_facade() -> None:
    assert workers_ns._send_bot_reply is tasks._send_bot_reply


def test_unknown_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AttributeError):
        monkeypatch.setattr(workers_ns, "a_name_nobody_defines", 1)
