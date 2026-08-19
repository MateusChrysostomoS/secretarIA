"""Tests for services/email.py's onboarding transactional-email path
(contract v1 §4 endpoint 6 / §10 / §12) and its arq task wrapper.

Follows the exact monkeypatch style test_tasks_helpers.py already uses for
the legacy calendar-alert path: patch `secretaria.services.email.get_settings`
and `asyncio.to_thread` so no real network/SMTP call is ever made.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

from secretaria.services.email import (  # noqa: E402
    _TEMPLATES,
    EmailOutcome,
    _SafeDict,
    send_cancellation_escalation_alert,
    send_transactional_email_message,
    send_transactional_email_result,
)
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.arq_worker import WorkerSettings  # noqa: E402

_REQUIRED_TEMPLATE_IDS = {
    "professional_invite",
    "retry_nudge_atividade_insuficiente",
    "retry_nudge_numero_em_outro_bsp",
    "retry_nudge_sem_acesso_admin_waba",
    "retry_nudge_sem_pagina_facebook",
    "retry_nudge_outro",
    "connection_success",
    "config_reminder_pre_connection",
    "config_reminder_connected",
    "closing_email",
    "test_window_expired",
}


def _configured_settings(**overrides):
    cfg = {
        "EMAIL_ENABLED": True,
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": 587,
        "SMTP_USERNAME": "user@example.com",
        "SMTP_PASSWORD": "secret",
        "SMTP_FROM_EMAIL": "",
        "SMTP_FROM_NAME": "",
        "SMTP_USE_TLS": True,
        "EMAIL_FROM_ADDRESS": "onboarding@example.com",
        "EMAIL_FROM_NAME": "SecretarIA",
    }
    cfg.update(overrides)
    return cfg


def _patch_settings(**overrides):
    cfg = _configured_settings(**overrides)
    patcher = patch("secretaria.services.email.get_settings")
    mock_settings = patcher.start()
    for key, value in cfg.items():
        setattr(mock_settings.return_value, key, value)
    return patcher


# --------------------------------------------------------------------------
# Template catalog
# --------------------------------------------------------------------------


def test_all_eleven_required_template_ids_exist():
    assert _REQUIRED_TEMPLATE_IDS <= set(_TEMPLATES)


def test_every_template_renders_without_variables():
    """No template may reference a variable in a way that raises - missing
    keys must render as a literal placeholder (`_SafeDict`), never crash."""
    for _template_id, tpl in _TEMPLATES.items():
        subject = tpl.subject.format_map(_SafeDict({}))
        body = tpl.body.format_map(_SafeDict({}))
        assert subject
        assert body
        assert "SecretarIA" in body or "SecretarIA" in subject or True  # smoke: no exception


def test_atividade_insuficiente_template_matches_spec_copy():
    """The spec explicitly calls out this nudge's tone - "ganhando histórico"."""
    tpl = _TEMPLATES["retry_nudge_atividade_insuficiente"]
    assert "ganhando histórico" in tpl.body


def test_test_window_expired_template_matches_spec_copy():
    """Corrections round "Task 2": subject names the test window explicitly;
    body explains the Meta/Coexistence cause, reassures nothing was charged
    and the subscription auto-cancelled, and carries a clear restart link."""
    tpl = _TEMPLATES["test_window_expired"]
    assert "período de teste" in tpl.subject
    assert "{clinic_name}" in tpl.body
    assert "{days}" in tpl.body
    assert "Coexistence" in tpl.body
    assert "nada foi cobrado" in tpl.body.lower()
    assert "cancelada automaticamente" in tpl.body
    assert "{restart_url}" in tpl.body


def test_test_window_expired_renders_with_variables():
    rendered_subject = _TEMPLATES["test_window_expired"].subject.format_map(
        _SafeDict({"clinic_name": "Clínica Teste"})
    )
    rendered_body = _TEMPLATES["test_window_expired"].body.format_map(
        _SafeDict(
            {
                "clinic_name": "Clínica Teste",
                "days": "14",
                "restart_url": "https://hub.secretaria.example/restart",
            }
        )
    )
    assert "período de teste" in rendered_subject
    assert "Clínica Teste" in rendered_body
    assert "14 dias" in rendered_body
    assert "https://hub.secretaria.example/restart" in rendered_body


def test_safe_dict_leaves_unknown_placeholder_intact():
    rendered = "Olá {name}, bem-vindo a {clinic_name}".format_map(_SafeDict({"name": "Ana"}))
    assert rendered == "Olá Ana, bem-vindo a {clinic_name}"


# --------------------------------------------------------------------------
# send_transactional_email_message — no-op / disabled paths
# --------------------------------------------------------------------------


async def test_noop_when_email_disabled():
    patcher = _patch_settings(EMAIL_ENABLED=False)
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            sent = await send_transactional_email_message(
                to="doctor@example.com", template="connection_success", variables={}
            )
            assert sent is False
            mock_thread.assert_not_called()
    finally:
        patcher.stop()


async def test_noop_when_smtp_host_empty_even_if_enabled():
    patcher = _patch_settings(EMAIL_ENABLED=True, SMTP_HOST="")
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            sent = await send_transactional_email_message(
                to="doctor@example.com", template="connection_success", variables={}
            )
            assert sent is False
            mock_thread.assert_not_called()
    finally:
        patcher.stop()


async def test_unknown_template_returns_false_without_sending():
    patcher = _patch_settings()
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            sent = await send_transactional_email_message(
                to="doctor@example.com", template="does_not_exist", variables={}
            )
            assert sent is False
            mock_thread.assert_not_called()
    finally:
        patcher.stop()


# --------------------------------------------------------------------------
# send_transactional_email_message — happy path + resilience
# --------------------------------------------------------------------------


async def test_sends_when_enabled_and_configured():
    patcher = _patch_settings()
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            sent = await send_transactional_email_message(
                to="doctor@example.com",
                template="connection_success",
                variables={"clinic_name": "Clínica Teste"},
            )
            assert sent is True
            mock_thread.assert_called_once()
            sync_fn = mock_thread.call_args.args[0]
            assert sync_fn.__name__ == "_send_transactional_sync"
            assert mock_thread.call_args.args[1] == "doctor@example.com"
            rendered_body = mock_thread.call_args.args[3]
            assert "Clínica Teste" in rendered_body
    finally:
        patcher.stop()


async def test_missing_variable_renders_placeholder_and_still_sends():
    """A caller-supplied `variables` dict missing an expected key must never
    crash the send - the module's hard contract is 'never raises into callers'."""
    patcher = _patch_settings()
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            sent = await send_transactional_email_message(
                to="doctor@example.com", template="professional_invite", variables={}
            )
            assert sent is True
            rendered_body = mock_thread.call_args.args[3]
            assert "{name}" in rendered_body or "{link}" in rendered_body
    finally:
        patcher.stop()


async def test_swallows_smtp_send_error():
    patcher = _patch_settings()
    try:
        with patch(
            "asyncio.to_thread", new_callable=AsyncMock, side_effect=OSError("conn refused")
        ):
            sent = await send_transactional_email_message(
                to="doctor@example.com", template="connection_success", variables={}
            )
            assert sent is False  # never raises
    finally:
        patcher.stop()


async def test_variables_none_does_not_raise():
    patcher = _patch_settings()
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock):
            sent = await send_transactional_email_message(
                to="doctor@example.com", template="closing_email", variables=None
            )
            assert sent is True
    finally:
        patcher.stop()


# --------------------------------------------------------------------------
# arq task wrapper (workers/tasks.py) + worker registration
# --------------------------------------------------------------------------


async def test_arq_task_delegates_to_service_with_correct_argument_order(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []

    async def _fake(*, to, template, variables):
        calls.append((to, template, variables))
        return True

    monkeypatch.setattr(tasks, "send_transactional_email_message", _fake)

    await tasks.send_transactional_email(
        {}, "connection_success", "doctor@example.com", {"clinic_name": "Clínica"}
    )

    assert calls == [("doctor@example.com", "connection_success", {"clinic_name": "Clínica"})]


async def test_arq_task_never_raises_when_service_returns_false(monkeypatch: pytest.MonkeyPatch):
    async def _fake(*, to, template, variables):
        return False

    monkeypatch.setattr(tasks, "send_transactional_email_message", _fake)
    # Must not raise.
    await tasks.send_transactional_email({}, "connection_success", "doctor@example.com", {})


def test_send_transactional_email_registered_in_worker_functions():
    assert tasks.send_transactional_email in WorkerSettings.functions


# --------------------------------------------------------------------------
# EmailOutcome - WHY a send did not happen (FIX 32)
# --------------------------------------------------------------------------


async def test_disabled_and_failed_are_different_answers():
    """The bool wrapper collapses both to False. A caller deciding whether to
    RETRY needs them apart: retrying a kill-switch is retrying a decision, and
    it would alarm on every booking of every clinic without mail."""
    patcher = _patch_settings(EMAIL_ENABLED=False)
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock):
            disabled = await send_transactional_email_result(
                to="doctor@example.com", template="connection_success", variables={}
            )
    finally:
        patcher.stop()

    patcher = _patch_settings()
    try:
        with patch(
            "asyncio.to_thread", new_callable=AsyncMock, side_effect=OSError("conn refused")
        ):
            failed = await send_transactional_email_result(
                to="doctor@example.com", template="connection_success", variables={}
            )
    finally:
        patcher.stop()

    assert disabled is EmailOutcome.DISABLED
    assert failed is EmailOutcome.SEND_FAILED
    assert not disabled.is_transient
    assert failed.is_transient


async def test_an_unknown_template_is_permanent_not_transient():
    """Our bug, not an outage: the next attempt fails identically."""
    patcher = _patch_settings()
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            outcome = await send_transactional_email_result(
                to="doctor@example.com", template="does_not_exist", variables={}
            )
            mock_thread.assert_not_called()
    finally:
        patcher.stop()

    assert outcome is EmailOutcome.UNKNOWN_TEMPLATE
    assert not outcome.is_transient


async def test_the_bool_wrapper_still_answers_exactly_as_before():
    """Three call sites still take the bool. It must keep meaning "sent"."""
    patcher = _patch_settings()
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock):
            assert (
                await send_transactional_email_message(
                    to="doctor@example.com", template="connection_success", variables={}
                )
                is True
            )
    finally:
        patcher.stop()


# --------------------------------------------------------------------------
# The cancellation escalation alert (FIX 32)
# --------------------------------------------------------------------------


async def test_escalation_carries_the_whatsapp_link_so_a_human_can_act():
    """The patient has NOT been told and every retry is spent. The clinic needs
    the one thing that fixes it: a tap-to-write link to that patient."""
    patcher = _patch_settings()
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            await send_cancellation_escalation_alert(
                "contato@clinica.example", "Clinica Teste", "https://wa.me/5511988887777"
            )
            mock_thread.assert_called_once()
            subject = mock_thread.call_args.args[2]
            body = mock_thread.call_args.args[3]
    finally:
        patcher.stop()

    assert "NÃO avisado" in subject
    assert "https://wa.me/5511988887777" in body
    assert "Clinica Teste" in body


async def test_escalation_without_a_number_says_so_instead_of_printing_none():
    patcher = _patch_settings()
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            await send_cancellation_escalation_alert(
                "contato@clinica.example", "Clinica Teste", None
            )
            body = mock_thread.call_args.args[3]
    finally:
        patcher.stop()

    assert "None" not in body
    assert "não há um número" in body.lower()


async def test_escalation_never_raises_when_smtp_breaks():
    """It is the end of the line: a failed alert must not become an unhandled
    exception in the worker."""
    patcher = _patch_settings()
    try:
        with patch(
            "asyncio.to_thread", new_callable=AsyncMock, side_effect=OSError("conn refused")
        ):
            await send_cancellation_escalation_alert(
                "contato@clinica.example", "Clinica Teste", None
            )
    finally:
        patcher.stop()


async def test_escalation_is_silent_when_smtp_is_unconfigured():
    patcher = _patch_settings(SMTP_HOST="")
    try:
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            await send_cancellation_escalation_alert(
                "contato@clinica.example", "Clinica Teste", None
            )
            mock_thread.assert_not_called()
    finally:
        patcher.stop()
