"""Debounced clinic alerts about the payment connection (services/payments/alerts.py)."""

from uuid import uuid4

import pytest

from secretaria.models import Tenant
from secretaria.services import email
from secretaria.services.payments import alerts
from tests.payments_support import FakeRedis


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_send(to_email, clinic_name, kind):
        calls.append((to_email, clinic_name, kind))

    monkeypatch.setattr(alerts.email, "send_payment_alert", fake_send)
    return calls


def _tenant(**kw) -> Tenant:
    fields = dict(id=uuid4(), clinic_name="Clinica", contact_email="dono@example.test")
    fields.update(kw)
    return Tenant(**fields)


async def test_first_alert_is_sent_and_repeats_inside_the_window_are_swallowed(sent):
    redis, tenant = FakeRedis(), _tenant()
    item_id = uuid4()
    assert await alerts.send_payment_alert(
        redis, tenant, "patient_cancelled_paid", item_id=item_id
    ) is True
    assert await alerts.send_payment_alert(
        redis, tenant, "patient_cancelled_paid", item_id=item_id
    ) is False
    assert sent == [("dono@example.test", "Clinica", "patient_cancelled_paid")]
    ttl = redis.ttls[f"payalert:{tenant.id}:{item_id}"]
    assert ttl == alerts.ALERT_DEBOUNCE_SECONDS == 21600


async def test_kinds_and_tenants_are_debounced_independently(sent):
    redis, a, b = FakeRedis(), _tenant(), _tenant()
    assert await alerts.send_payment_alert(redis, a, "overdue_kept") is False
    assert await alerts.send_payment_alert(redis, a, "webhooks_not_arriving") is False
    assert await alerts.send_payment_alert(
        redis, b, "patient_cancelled_paid", item_id=uuid4()
    ) is True
    assert sent == [("dono@example.test", "Clinica", "patient_cancelled_paid")]


async def test_no_contact_email_no_redis_or_unknown_kind_sends_nothing(sent):
    no_email = _tenant(contact_email=None)
    assert await alerts.send_payment_alert(FakeRedis(), no_email, "overdue_kept") is False
    assert await alerts.send_payment_alert(None, _tenant(), "overdue_kept") is False
    assert await alerts.send_payment_alert(FakeRedis(), _tenant(), "invented_kind") is False
    assert sent == []


def test_every_alert_kind_has_its_own_text_and_no_personal_or_secret_word():
    assert set(email._PAYMENT_ALERTS) == alerts.ALERT_KINDS
    for subject, what, todo in email._PAYMENT_ALERTS.values():
        text = f"{subject} {what} {todo}".lower()
        for forbidden in ("cpf", "senha", "$aact", "token", "api_key", "http"):
            assert forbidden not in text


async def test_the_email_is_silent_without_smtp(monkeypatch):
    class _NoSmtp:
        SMTP_HOST = ""

    monkeypatch.setattr(email, "get_settings", lambda: _NoSmtp())
    assert await email.send_payment_alert("a@b.c", "Clinica", "overdue_kept") is None


async def test_the_email_never_raises_when_smtp_fails(monkeypatch):
    class _Smtp:
        SMTP_HOST = "smtp.example.test"

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(email, "get_settings", lambda: _Smtp())
    monkeypatch.setattr(email, "_send_sync", boom)
    assert await email.send_payment_alert("a@b.c", "Clinica", "key_expired_or_disabled") is None
