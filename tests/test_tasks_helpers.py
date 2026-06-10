"""Unit tests for worker helper functions (no DB / network)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402

from secretaria.workers.tasks import (  # noqa: E402
    _is_rate_limited,
    _render_greeting_template,
    extract_patient_name,
)


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
