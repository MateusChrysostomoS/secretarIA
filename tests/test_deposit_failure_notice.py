"""E-mail to the clinic (and the doctor) when a deposit could not be charged (TASK-071, Plan 3,
task 2).

The failure is ALWAYS recorded on the payment account; the e-mail is best effort, at most
`DAILY_EMAIL_CAP` per local day per recipient (a per-recipient atomic Redis counter keyed by a
hash of the address), and the whole call never raises into the booking.
"""

import asyncio
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest_asyncio

from secretaria.models import Tenant
from secretaria.services import email as email_module
from secretaria.services.payments import accounts, deposit_failure_notice as notice
from secretaria.services.payments.failure_reason import FailureReason, action_text, user_text
from tests.payments_support import FakeRedis, make_sessionmaker

CLINIC_EMAIL = "Contato@Clinica.example.com"
DOCTOR_EMAIL = "dra.ana@clinica.example.com"
PATIENT_CPF = "52998224725"
PATIENT_PHONE = "5521999998888"
PATIENT_EMAIL = "paciente.privado@example.org"
START_AT = datetime(2026, 10, 29, 17, 30, tzinfo=UTC)  # 14:30 in America/Sao_Paulo
NOON_UTC = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)


_REAL_SEND = email_module.send_deposit_unavailable_alert  # captured before any fixture patches it


class Env(SimpleNamespace):
    pass


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(notice, "async_session_factory", maker)
    tenant_id = uuid4()
    async with maker() as session:
        session.add(
            Tenant(
                id=tenant_id,
                clinic_name="Clinica Teste",
                phone_number_id=str(uuid4())[:12],
                contact_email=CLINIC_EMAIL,
                timezone="America/Sao_Paulo",
            )
        )
        await session.flush()
        await accounts.get_or_create_account_row(session, tenant_id=tenant_id, provider="asaas")
        await session.commit()

    sent: list[dict] = []
    send_ok = {"value": True}

    async def fake_send(to_email, clinic_name, **kwargs):
        sent.append({"to": to_email, "clinic": clinic_name, **kwargs})
        return send_ok["value"]

    monkeypatch.setattr(email_module, "send_deposit_unavailable_alert", fake_send)

    doctor_id = uuid4()
    emails: dict[str, str] = {str(doctor_id): DOCTOR_EMAIL}

    async def fake_emails(_tenant_id):
        return emails

    monkeypatch.setattr(notice, "fetch_professional_emails", fake_emails)
    clock = {"now": NOON_UTC}
    monkeypatch.setattr(notice, "_now", lambda: clock["now"])
    yield Env(
        maker=maker,
        tenant_id=tenant_id,
        doctor_id=doctor_id,
        emails=emails,
        sent=sent,
        send_ok=send_ok,
        clock=clock,
        redis=FakeRedis(),
    )
    await engine.dispose()


async def _notify(env, **overrides):
    kwargs = dict(
        tenant_id=env.tenant_id,
        patient_name="Maria Souza",
        service="Consulta de rotina",
        start_at=START_AT,
        reason=FailureReason.customer_document_invalid,
        stage="customer",
        redis=env.redis,
    )
    kwargs.update(overrides)
    return await notice.notify_deposit_failure(**kwargs)


async def _account_row(env):
    async with env.maker() as session:
        return await accounts.get_account_row(session, tenant_id=env.tenant_id, provider="asaas")


async def test_first_three_failures_send_email_fourth_does_not(env):
    outcomes = [(await _notify(env)).outcome for _ in range(4)]
    assert outcomes == ["sent", "sent", "sent", "capped"]
    assert len(env.sent) == 3
    row = await _account_row(env)
    assert row.last_charge_failure_reason == "customer_document_invalid"  # recorded every time


async def test_counter_resets_next_local_day(env):
    # 23:59 in Sao Paulo = 02:59 UTC the next day: still the SAME local day as 23:00 before it.
    env.clock["now"] = datetime(2026, 10, 29, 2, 0, tzinfo=UTC)  # 23:00 local, Oct 28
    for _ in range(3):
        assert (await _notify(env)).outcome == "sent"
    env.clock["now"] = datetime(2026, 10, 29, 2, 59, tzinfo=UTC)  # 23:59 local, Oct 28
    assert (await _notify(env)).outcome == "capped"
    env.clock["now"] = datetime(2026, 10, 29, 3, 1, tzinfo=UTC)  # 00:01 local, Oct 29
    assert (await _notify(env)).outcome == "sent"
    assert len(env.sent) == 4


async def test_concurrent_failures_never_exceed_cap(env):
    results = await asyncio.gather(*[_notify(env) for _ in range(4)])
    assert sorted(r.outcome for r in results) == ["capped", "sent", "sent", "sent"]
    assert len(env.sent) == 3


async def test_email_has_patient_name_service_time_and_reason_but_no_document_phone_or_email(
    env, monkeypatch
):
    captured: list[tuple[str, str, str]] = []

    def fake_send_sync(to_email, subject, body):
        captured.append((to_email, subject, body))

    monkeypatch.setattr(email_module, "_send_sync", fake_send_sync)
    monkeypatch.setattr(
        email_module, "get_settings", lambda: SimpleNamespace(SMTP_HOST="smtp.example.com")
    )
    # The fixture swapped in a recording fake; this test goes through the REAL sender.
    monkeypatch.setattr(email_module, "send_deposit_unavailable_alert", _REAL_SEND)
    _link_settings(monkeypatch)
    result = await _notify(env, reason=FailureReason.pix_key_missing)
    assert result.outcome == "sent"
    assert len(captured) == 1
    to_email, subject, body = captured[0]
    assert to_email == CLINIC_EMAIL
    assert subject == "Sinal do Pix não funcionou: consulta marcada sem sinal"
    assert "Maria Souza" in body
    assert "Consulta de rotina" in body
    assert "29/10/2026 às 14:30" in body
    assert user_text(FailureReason.pix_key_missing).rstrip(".") in body
    assert action_text(FailureReason.pix_key_missing).rstrip(".") in body
    assert "no máximo 3 vezes por dia" in body
    # Owner's wording (2026-10-29): the cause is not blamed on Asaas, and the e-mail links to
    # where the fix happens - the Asaas panel for a missing Pix key.
    assert "por causa de um problema na cobrança do sinal" in body
    assert "problemas técnicos com o Asaas" not in body
    assert "Abrir o painel do Asaas: https://www.asaas.com/login" in body
    assert "Portal" not in body
    for private in (PATIENT_CPF, PATIENT_PHONE, PATIENT_EMAIL):
        assert private not in body
        assert private not in subject


def _link_settings(monkeypatch, *, portal_base: str = "https://portal.example.com"):
    from secretaria.config import Settings

    settings = Settings(
        ASAAS_BASE_URL="https://api.asaas.com/v3", BRAIN_MESSAGE_PUBLIC_URL=portal_base
    )
    monkeypatch.setattr(notice, "get_settings", lambda: settings)


def test_links_follow_the_reason(monkeypatch):
    _link_settings(monkeypatch)
    asaas = ("Abrir o painel do Asaas", "https://www.asaas.com/login")
    portal = (
        "Abrir a configuração do sinal no Portal",
        "https://portal.example.com/contexto/#pix",
    )
    assert notice._email_links(FailureReason.pix_key_missing) == [asaas]
    assert notice._email_links(FailureReason.value_too_low) == [portal]
    assert notice._email_links(FailureReason.no_account) == [portal]
    assert notice._email_links(FailureReason.api_key_invalid) == [asaas, portal]
    for reason in (
        FailureReason.customer_document_invalid,
        FailureReason.rate_limited,
        FailureReason.provider_unavailable,
        FailureReason.unknown,
    ):
        assert notice._email_links(reason) == []


def test_portal_link_is_dropped_without_a_portal_address(monkeypatch):
    _link_settings(monkeypatch, portal_base="")
    assert notice._email_links(FailureReason.value_too_low) == []
    assert notice._email_links(FailureReason.api_key_invalid) == [
        ("Abrir o painel do Asaas", "https://www.asaas.com/login")
    ]


def test_sandbox_api_links_the_sandbox_panel():
    from secretaria.config import Settings

    settings = Settings(ASAAS_BASE_URL="https://sandbox.asaas.com/api/v3")
    assert settings.ASAAS_PANEL_URL == "https://sandbox.asaas.com/login"


async def test_missing_contact_email_records_failure_without_sending(env):
    async with env.maker() as session:
        tenant = await session.get(Tenant, env.tenant_id)
        tenant.contact_email = None
        await session.commit()
    result = await _notify(env)
    assert result.outcome == "no_recipient"
    assert env.sent == []
    row = await _account_row(env)
    assert row.last_charge_failure_reason == "customer_document_invalid"


async def test_send_failure_does_not_raise(env):
    env.send_ok["value"] = False
    result = await _notify(env)
    assert result.outcome == "send_failed"
    row = await _account_row(env)
    assert row.last_charge_failure_reason is not None


async def test_unexpected_exceptions_never_escape(env, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("smtp exploded")

    monkeypatch.setattr(email_module, "send_deposit_unavailable_alert", boom)
    result = await _notify(env)
    assert result.outcome == "send_failed"

    def broken_factory():
        raise RuntimeError("database gone")

    monkeypatch.setattr(notice, "async_session_factory", broken_factory)
    result = await _notify(env)  # must not raise even when the database is gone
    assert result.outcome in {"send_failed", "no_recipient"}


async def test_doctor_also_receives_and_has_own_cap(env):
    # Three failures for the clinic only (no professional): clinic reaches its cap.
    for _ in range(3):
        assert (await _notify(env)).outcome == "sent"
    assert [m["to"] for m in env.sent] == [CLINIC_EMAIL] * 3
    # The 4th failure has a doctor: the clinic is capped, the doctor (own counter) still gets it.
    result = await _notify(env, professional_id=env.doctor_id)
    assert result.outcome == "sent"
    assert [m["to"] for m in env.sent][-1] == DOCTOR_EMAIL
    assert len(env.sent) == 4


async def test_doctor_without_email_is_skipped(env):
    env.emails.clear()  # the doctor has no linked user / e-mail
    result = await _notify(env, professional_id=env.doctor_id)
    assert result.outcome == "sent"
    assert [m["to"] for m in env.sent] == [CLINIC_EMAIL]


async def test_same_address_for_clinic_and_doctor_sends_once(env):
    env.emails[str(env.doctor_id)] = CLINIC_EMAIL.upper()
    result = await _notify(env, professional_id=env.doctor_id)
    assert result.outcome == "sent"
    assert len(env.sent) == 1


async def test_redis_key_never_contains_a_plain_email(env):
    await _notify(env, professional_id=env.doctor_id)
    keys = list(env.redis.store)
    assert len(keys) == 2
    for key in keys:
        assert key.startswith(f"deposit_fail_mail:{env.tenant_id}:")
        assert "@" not in key
        assert "clinica" not in key.lower()
    # Salted with the tenant: the same address hashes differently per clinic, and the bare
    # (unsalted) digest of the address is never what the key carries.
    material = f"{env.tenant_id}:{CLINIC_EMAIL.strip().lower()}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:16]
    assert f"deposit_fail_mail:{env.tenant_id}:{digest}:20261028" in env.redis.store
    unsalted = hashlib.sha256(CLINIC_EMAIL.strip().lower().encode()).hexdigest()[:16]
    assert not any(unsalted in key for key in keys)
    assert all(ttl == 48 * 3600 for ttl in env.redis.ttls.values())


async def test_redis_down_does_not_send_and_does_not_raise(env):
    class DownRedis:
        async def incr(self, key):
            raise ConnectionError("redis down")

    result = await _notify(env, redis=DownRedis())
    assert result.outcome == "send_failed"
    assert env.sent == []
    row = await _account_row(env)
    assert row.last_charge_failure_reason is not None  # still recorded


def test_every_failure_reason_has_an_action():
    for reason in FailureReason:
        text = action_text(reason)
        assert text and text.strip()


async def test_without_a_redis_argument_an_unreachable_redis_still_does_not_raise(env, monkeypatch):
    monkeypatch.setattr(
        notice, "get_settings", lambda: SimpleNamespace(REDIS_URL="redis://127.0.0.1:1/0")
    )
    result = await _notify(env, redis=None)
    assert result.outcome == "send_failed"
    assert env.sent == []
    row = await _account_row(env)
    assert row.last_charge_failure_reason is not None


def test_counter_key_is_salted_with_the_tenant():
    from uuid import uuid4

    from secretaria.services.payments.deposit_failure_notice import _counter_key

    a, b = uuid4(), uuid4()
    key_a = _counter_key(a, "Someone@Example.com ", "20261028")
    key_b = _counter_key(b, "someone@example.com", "20261028")
    assert key_a.split(":")[2] != key_b.split(":")[2]  # same address, different clinic
    assert key_a == _counter_key(a, "someone@example.com", "20261028")  # normalized
    assert "@" not in key_a and "example" not in key_a
