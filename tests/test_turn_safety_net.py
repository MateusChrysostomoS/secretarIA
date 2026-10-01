"""The patient is never left in silence - and the net cannot be abused."""

import os
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import pytest  # noqa: E402

from secretaria.services import turn_safety_net as net  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

# conftest neutralizes the real net for every other test; this module is the one
# that exercises it, so it keeps a handle captured BEFORE any fixture runs.
_real_send_turn_fallback = tasks._send_turn_fallback


class _FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key):  # noqa: ANN001
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, seconds):  # noqa: ANN001
        self.ttls[key] = seconds


async def test_fallback_is_capped_per_conversation() -> None:
    redis = _FakeRedis()
    cid = uuid4()
    results = [await net.fallback_allowed(redis, cid) for _ in range(5)]
    assert results == [True, True, True, False, False]
    # Another conversation has its own budget.
    assert await net.fallback_allowed(redis, uuid4()) is True


async def test_fallback_window_ttl_is_set_once() -> None:
    redis = _FakeRedis()
    await net.fallback_allowed(redis, uuid4())
    assert list(redis.ttls.values()) == [600]


async def test_fallback_fails_open_without_redis() -> None:
    assert await net.fallback_allowed(None, uuid4()) is True


def test_ledger_counts_only_inside_a_turn() -> None:
    net.note_send()  # outside a turn: ignored
    assert net.sends_in_turn() == 0
    token = net.begin_turn()
    try:
        net.note_send()
        net.note_send()
        assert net.sends_in_turn() == 2
    finally:
        net.end_turn(token)
    assert net.sends_in_turn() == 0


def _reply(conversation_id=None):
    return tasks._ReplyContext(
        conversation_id=conversation_id or uuid4(),
        patient_ref="ref",
        inbound_body="oi",
        tenant_id=uuid4(),
    )


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def _fallback(reply, redis, *, cause):  # noqa: ANN001
        calls.append(cause)

    monkeypatch.setattr(tasks, "_send_turn_fallback", _fallback)
    return calls


async def test_exception_in_the_pipeline_is_answered(monkeypatch, spy) -> None:
    async def _boom(reply, redis=None):  # noqa: ANN001
        raise RuntimeError("downstream exploded")

    monkeypatch.setattr(tasks, "_send_bot_reply_inner", _boom)
    await tasks._send_bot_reply(_reply())  # must not raise
    assert spy == ["exception"]


async def test_silent_return_is_answered(monkeypatch, spy) -> None:
    async def _quiet(reply, redis=None):  # noqa: ANN001
        return None

    monkeypatch.setattr(tasks, "_send_bot_reply_inner", _quiet)
    await tasks._send_bot_reply(_reply())
    assert spy == ["silent_return"]


async def test_a_turn_that_sent_something_gets_no_extra_apology(monkeypatch, spy) -> None:
    async def _sends(reply, redis=None):  # noqa: ANN001
        net.note_send()

    monkeypatch.setattr(tasks, "_send_bot_reply_inner", _sends)
    await tasks._send_bot_reply(_reply())
    assert spy == []


async def test_a_pipeline_that_sent_then_crashed_is_not_apologised_to(monkeypatch, spy) -> None:
    async def _partial(reply, redis=None):  # noqa: ANN001
        net.note_send()
        raise RuntimeError("second bubble failed")

    monkeypatch.setattr(tasks, "_send_bot_reply_inner", _partial)
    await tasks._send_bot_reply(_reply())
    assert spy == []


async def test_a_turn_without_a_conversation_is_left_alone(monkeypatch, spy) -> None:
    async def _quiet(reply, redis=None):  # noqa: ANN001
        return None

    monkeypatch.setattr(tasks, "_send_bot_reply_inner", _quiet)
    reply = tasks._ReplyContext(
        conversation_id=None, patient_ref="ref", inbound_body="oi", tenant_id=uuid4()
    )
    await tasks._send_bot_reply(reply)
    assert spy == []


# --- the apology itself ----------------------------------------------------


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002
        return False

    async def get(self, _model, _id):  # noqa: ANN001
        return SimpleNamespace(id=uuid4())


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    sent: list[str] = []

    async def _token(_session, _tenant_id):  # noqa: ANN001
        return "tok"

    async def _plain(reply, *, tenant, waba_token, body, event):  # noqa: ANN001
        sent.append(body)

    monkeypatch.setattr(tasks, "async_session_factory", lambda: _FakeSession())
    monkeypatch.setattr(tasks, "get_waba_token", _token)
    monkeypatch.setattr(tasks, "_send_plain_reply", _plain)
    return sent


def _entitled(monkeypatch: pytest.MonkeyPatch, summary) -> None:
    async def _ents(_tenant_id, _redis):  # noqa: ANN001
        return summary

    monkeypatch.setattr(tasks, "get_entitlements", _ents)


async def test_apology_is_sent_to_an_entitled_tenant(monkeypatch, wired) -> None:
    _entitled(monkeypatch, SimpleNamespace(active=True, secretaria_enabled=True, status="active"))
    await _real_send_turn_fallback(_reply(), _FakeRedis(), cause="exception")
    assert wired == [net.TURN_FALLBACK_MESSAGE]


async def test_apology_never_goes_out_for_an_unentitled_tenant(monkeypatch, wired) -> None:
    _entitled(
        monkeypatch, SimpleNamespace(active=False, secretaria_enabled=True, status="canceled")
    )
    await _real_send_turn_fallback(_reply(), _FakeRedis(), cause="silent_return")
    assert wired == []


async def test_apology_fails_closed_when_entitlement_cannot_be_read(monkeypatch, wired) -> None:
    _entitled(monkeypatch, None)
    await _real_send_turn_fallback(_reply(), _FakeRedis(), cause="silent_return")
    assert wired == []


async def test_a_looping_sender_gets_at_most_three_apologies(monkeypatch, wired) -> None:
    _entitled(monkeypatch, SimpleNamespace(active=True, secretaria_enabled=True, status="active"))
    redis = _FakeRedis()
    reply = _reply()
    for _ in range(10):
        await _real_send_turn_fallback(reply, redis, cause="exception")
    assert len(wired) == 3
