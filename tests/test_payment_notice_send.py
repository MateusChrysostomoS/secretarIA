"""TASK-093 Plano 2 / Task 2: envio do aviso neutro, no máximo 1 por dia por paciente."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, CHANNEL_WHATSAPP
from secretaria.services.entitlements_client import EntitlementSummary
from secretaria.services.payment_notice import PAYMENT_NOTICE_TEXT
from secretaria.workers.shared import payment_notice as mod
from secretaria.workers.shared.context import _ReplyContext
from tests.payments_support import FakeRedis

TENANT_ID = UUID("22222222-2222-2222-2222-222222222222")


def _summary(*, active=True, secretaria_enabled=True) -> EntitlementSummary:
    return EntitlementSummary(
        tenant_id=str(TENANT_ID),
        status="active" if active else "past_due",
        active=active,
        secretaria_enabled=secretaria_enabled,
        plan=None,
        secretaria_tier=None,
        addons={},
        limits={},
    )


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.seen: list[_ReplyContext] = []

    async def send_text_message(self, *, to: str, body: str, **_kw) -> None:
        self.sent.append((to, body))


class BrokenRedis(FakeRedis):
    async def set(self, key, value, ex=None, nx=False):
        raise ConnectionError("redis down")


@pytest.fixture
def sender(monkeypatch):
    fake = FakeSender()

    def _fake_reply_sender(reply, tenant, waba_token):
        fake.seen.append(reply)
        return fake

    monkeypatch.setattr(mod, "_reply_sender", _fake_reply_sender)
    return fake


@pytest.fixture
def clock(monkeypatch):
    state = {"now": datetime(2026, 11, 24, 15, 0, tzinfo=UTC)}
    monkeypatch.setattr(mod, "_utcnow", lambda: state["now"])
    return state


def _tenant():
    return SimpleNamespace(id=TENANT_ID, timezone="America/Sao_Paulo")


def _reply(channel=CHANNEL_BRAIN_MESSAGE) -> _ReplyContext:
    return _ReplyContext(
        conversation_id=UUID("33333333-3333-3333-3333-333333333333"),
        patient_ref="paciente-ref-1",
        inbound_body="oi",
        channel=channel,
        tenant_id=TENANT_ID,
    )


async def _send(redis, *, summary=None, reply=None):
    return await mod.send_payment_notice_once(
        reply or _reply(),
        tenant=_tenant(),
        waba_token=None,
        summary=summary if summary is not None else _summary(active=False),
        redis=redis,
    )


async def test_first_call_sends_the_fixed_text(sender, clock):
    redis = FakeRedis()
    assert await _send(redis) is True
    assert sender.sent == [("paciente-ref-1", PAYMENT_NOTICE_TEXT)]


async def test_second_call_same_day_is_silent(sender, clock):
    redis = FakeRedis()
    assert await _send(redis) is True
    assert await _send(redis) is False
    assert await _send(redis) is False
    assert len(sender.sent) == 1


async def test_next_clinic_day_sends_again(sender, clock):
    redis = FakeRedis()
    assert await _send(redis) is True
    # 24/11 12h em SP -> 25/11 12h em SP: outro dia da clínica.
    clock["now"] = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    assert await _send(redis) is True
    assert len(sender.sent) == 2


async def test_day_boundary_is_the_clinic_midnight_not_utc(sender, clock):
    redis = FakeRedis()
    # 23h30 em SP de 24/11 (= 02h30 UTC de 25/11).
    clock["now"] = datetime(2026, 11, 25, 2, 30, tzinfo=UTC)
    assert await _send(redis) is True
    # 23h50 em SP, ainda 24/11 (já 02h50 UTC): mesmo dia da clínica.
    clock["now"] = datetime(2026, 11, 25, 2, 50, tzinfo=UTC)
    assert await _send(redis) is False


async def test_ttl_runs_until_clinic_midnight(sender, clock):
    redis = FakeRedis()
    await _send(redis)
    (ttl,) = redis.ttls.values()
    assert ttl == 12 * 3600  # 12h em SP -> meia-noite


async def test_summary_none_is_silence(sender, clock):
    redis = FakeRedis()
    result = await mod.send_payment_notice_once(
        _reply(), tenant=_tenant(), waba_token=None, summary=None, redis=redis
    )
    assert result is False
    assert sender.sent == []
    assert redis.store == {}


async def test_paid_clinic_is_silence(sender, clock):
    redis = FakeRedis()
    assert await _send(redis, summary=_summary()) is False
    assert sender.sent == []
    assert redis.store == {}


async def test_redis_failure_is_silence_and_does_not_raise(sender, clock):
    assert await _send(BrokenRedis()) is False
    assert sender.sent == []


async def test_no_redis_is_silence(sender, clock):
    assert await _send(None) is False
    assert sender.sent == []


async def test_send_failure_does_not_raise(monkeypatch, clock):
    class Boom:
        async def send_text_message(self, **_kw):
            raise RuntimeError("meta down")

    monkeypatch.setattr(mod, "_reply_sender", lambda *a, **k: Boom())
    # O envio engole o erro (como `_send_simple_text`); não levanta.
    await _send(FakeRedis())


async def test_no_sender_is_silence(monkeypatch, clock):
    monkeypatch.setattr(mod, "_reply_sender", lambda *a, **k: None)
    assert await _send(FakeRedis()) is False


@pytest.mark.parametrize("channel", [CHANNEL_BRAIN_MESSAGE, CHANNEL_WHATSAPP])
async def test_uses_the_sender_of_the_conversation_channel(sender, clock, channel):
    await _send(FakeRedis(), reply=_reply(channel))
    assert [r.channel for r in sender.seen] == [channel]
    assert len(sender.sent) == 1
