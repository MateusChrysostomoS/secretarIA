"""Unit tests for worker helper functions (no DB / network)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

from secretaria.workers.tasks import (  # noqa: E402
    _is_rate_limited,
    _render_greeting_template,
    extract_patient_name,
)
from secretaria.services.email import send_calendar_alert  # noqa: E402


@pytest.mark.parametrize(
    "body,expected",
    [
        ("meu nome é João", "João"),
        ("oi, meu nome é João Silva e quero marcar", "João Silva"),
        ("me chamo MARIA", "Maria"),
        ("pode me chamar de Zé", "Zé"),
        ("meu nome é ana paula maria de souza", "Ana Paula Maria"),
        ("bom dia", None),
        ("quero agendar amanhã", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_patient_name(body, expected):
    assert extract_patient_name(body) == expected


def test_render_greeting_template_with_name():
    out = _render_greeting_template("Olá {{name}}, que bom te ver!", "João")
    assert out == "Olá João, que bom te ver!"


def test_render_greeting_template_without_name_cleans_spacing():
    out = _render_greeting_template("Olá {{name}}, que bom te ver!", None)
    assert out == "Olá, que bom te ver!"


class _FakeRedis:
    """Minimal async Redis stub covering the commands _is_rate_limited uses."""

    def __init__(self):
        self.store: dict[str, object] = {}

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def expire(self, key, seconds):
        return True

    async def setex(self, key, seconds, value):
        self.store[key] = value


async def test_rate_limit_allows_under_cap_then_silences():
    redis = _FakeRedis()
    # Default RATE_LIMIT_MAX_MESSAGES is 10: first 10 pass, 11th trips silence.
    results = [await _is_rate_limited(redis, "pn", "55119999") for _ in range(11)]
    assert results[:10] == [False] * 10
    assert results[10] is True
    # Once silenced, further messages stay blocked.
    assert await _is_rate_limited(redis, "pn", "55119999") is True


async def test_rate_limit_disabled_without_redis():
    assert await _is_rate_limited(None, "pn", "55119999") is False


async def test_rate_limit_is_per_sender():
    redis = _FakeRedis()
    for _ in range(11):
        await _is_rate_limited(redis, "pn", "aaa")
    # A different wa_id has its own counter and is not affected.
    assert await _is_rate_limited(redis, "pn", "bbb") is False


# ---------------------------------------------------------------------------
# Calendar alert email (send_calendar_alert)
# ---------------------------------------------------------------------------


async def test_calendar_alert_no_smtp_host_skips_silently():
    """When SMTP_HOST is empty the function returns without trying to connect."""
    with patch("secretaria.services.email.get_settings") as mock_settings:
        mock_settings.return_value.SMTP_HOST = ""
        # asyncio.to_thread must NOT be called — if it is the test would hang.
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            await send_calendar_alert("owner@example.com", "Clínica Teste")
            mock_thread.assert_not_called()


async def test_calendar_alert_sends_when_smtp_configured():
    """When SMTP_HOST is set, asyncio.to_thread is called with the right args."""
    with patch("secretaria.services.email.get_settings") as mock_settings:
        cfg = mock_settings.return_value
        cfg.SMTP_HOST = "smtp.example.com"
        cfg.SMTP_PORT = 587
        cfg.SMTP_USERNAME = "user@example.com"
        cfg.SMTP_PASSWORD = "secret"
        cfg.SMTP_FROM_EMAIL = "noreply@example.com"
        cfg.SMTP_FROM_NAME = "SecretarIA"
        cfg.SMTP_USE_TLS = True

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            await send_calendar_alert("owner@clinic.com", "Clínica Exemplo")
            mock_thread.assert_called_once()
            # First positional arg to to_thread is the sync function
            sync_fn = mock_thread.call_args.args[0]
            assert sync_fn.__name__ == "_send_sync"
            assert mock_thread.call_args.args[1] == "owner@clinic.com"


async def test_calendar_alert_swallows_smtp_error():
    """A send failure does not raise — it is logged and swallowed."""
    with patch("secretaria.services.email.get_settings") as mock_settings:
        mock_settings.return_value.SMTP_HOST = "smtp.example.com"
        mock_settings.return_value.SMTP_PORT = 587
        mock_settings.return_value.SMTP_USERNAME = ""
        mock_settings.return_value.SMTP_PASSWORD = ""
        mock_settings.return_value.SMTP_FROM_EMAIL = ""
        mock_settings.return_value.SMTP_FROM_NAME = ""
        mock_settings.return_value.SMTP_USE_TLS = False

        with patch("asyncio.to_thread", new_callable=AsyncMock, side_effect=OSError("conn refused")):
            # Must not raise
            await send_calendar_alert("owner@clinic.com", "Clínica Exemplo")
