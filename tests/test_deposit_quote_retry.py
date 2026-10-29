"""Deciding the deposit is retried before giving up (owner, 2026-10-29).

A throw while deciding/calculating the deposit (a database hiccup) used to go straight to
`DepositQuote("blocked", "quote_failed")` - the booking went on WITHOUT the deposit and the clinic
got an e-mail. Now the calculation is tried up to 3 times in a row (`deposit_gate.
quote_with_retries`, each attempt on a FRESH session so a failed transaction is never reused);
only when every attempt fails does the old fallback run.
"""

import pytest

from secretaria.services.payments import deposit_gate


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    waits: list[float] = []

    async def no_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(deposit_gate, "_retry_sleep", no_sleep)
    return waits


def _flaky(failures: int, value: str = "quote"):
    calls: list[int] = []

    async def attempt() -> str:
        calls.append(len(calls) + 1)
        if len(calls) <= failures:
            raise RuntimeError("database went away")
        return value

    return attempt, calls


async def test_a_first_try_that_works_is_not_retried(slept):
    attempt, calls = _flaky(0)
    assert await deposit_gate.quote_with_retries(attempt, event="t") == "quote"
    assert calls == [1] and slept == []


@pytest.mark.parametrize("failures", [1, 2])
async def test_a_transient_failure_yields_the_real_answer(slept, failures):
    attempt, calls = _flaky(failures)
    assert await deposit_gate.quote_with_retries(attempt, event="t") == "quote"
    assert len(calls) == failures + 1
    assert slept == list(deposit_gate.QUOTE_RETRY_DELAYS[:failures])


async def test_three_failures_raise_the_last_error(slept):
    attempt, calls = _flaky(3)
    with pytest.raises(RuntimeError):
        await deposit_gate.quote_with_retries(attempt, event="t")
    assert calls == [1, 2, 3]  # 3 attempts in total, never more
    assert slept == list(deposit_gate.QUOTE_RETRY_DELAYS)


def test_the_backoff_is_tiny():
    assert len(deposit_gate.QUOTE_RETRY_DELAYS) == 2
    assert sum(deposit_gate.QUOTE_RETRY_DELAYS) <= 1.0
