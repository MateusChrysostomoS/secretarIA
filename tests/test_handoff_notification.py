"""E-mail de passagem para humano: destinatários, dedupe, retentativa (MVP Portal, Task 5)."""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from secretaria.ai.tools import HANDOFF_REASONS  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.services import handoff_notification as hn  # noqa: E402
from secretaria.services.email import EmailOutcome, is_known_template  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def _db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(hn, "async_session_factory", maker)
    yield
    await engine.dispose()


def _tenant(email="clinica@example.com"):
    return SimpleNamespace(id=uuid4(), name="Clínica Teste", contact_email=email)


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def _send(to, template, variables):
        calls.append((to, template, variables))
        return EmailOutcome.SENT

    async def _emails(_tenant_id):
        return {}

    monkeypatch.setattr(hn, "send_transactional_email_result", _send)
    monkeypatch.setattr(hn, "fetch_professional_emails", _emails)
    monkeypatch.setattr(hn, "_retry_sleep", lambda _n: 0)
    return calls


def test_template_is_registered_and_reasons_are_labelled():
    assert is_known_template(hn.TEMPLATE_ID)
    assert set(HANDOFF_REASONS) == set(hn._REASON_TEXT)


async def test_clinic_and_doctor_are_emailed_without_patient_content(sent, monkeypatch):
    doctor_id = uuid4()

    async def _emails(_tenant_id):
        return {str(doctor_id): "medico@example.com"}

    monkeypatch.setattr(hn, "fetch_professional_emails", _emails)
    n = await hn.notify_human_handoff(
        tenant=_tenant(),
        conversation_id=uuid4(),
        professional_id=doctor_id,
        reason="patient_requested_human",
    )
    assert n == 2
    recipients = {to for to, _t, _v in sent}
    assert recipients == {"clinica@example.com", "medico@example.com"}
    body_vars = " ".join(str(v) for _to, _t, v in sent)
    assert "pediu para falar" in body_vars


async def test_same_conversation_is_not_emailed_twice(sent):
    conv = uuid4()
    tenant = _tenant()
    first = await hn.notify_human_handoff(
        tenant=tenant, conversation_id=conv, professional_id=None, reason="could_not_help"
    )
    second = await hn.notify_human_handoff(
        tenant=tenant, conversation_id=conv, professional_id=None, reason="could_not_help"
    )
    assert first == 1 and second == 0


async def test_transient_failure_retries_then_releases_the_claim(monkeypatch):
    attempts = []
    outcome = {"value": EmailOutcome.SEND_FAILED}

    async def _send(to, template, variables):
        attempts.append(to)
        return outcome["value"]

    async def _emails(_tenant_id):
        return {}

    monkeypatch.setattr(hn, "send_transactional_email_result", _send)
    monkeypatch.setattr(hn, "fetch_professional_emails", _emails)
    monkeypatch.setattr(hn, "_retry_sleep", lambda _n: 0)
    conv = uuid4()
    tenant = _tenant()
    n = await hn.notify_human_handoff(
        tenant=tenant, conversation_id=conv, professional_id=None, reason="could_not_help"
    )
    assert n == 0 and len(attempts) == 3
    # claim released: a later attempt (mailer back up) goes through
    outcome["value"] = EmailOutcome.SENT
    again = await hn.notify_human_handoff(
        tenant=tenant, conversation_id=conv, professional_id=None, reason="could_not_help"
    )
    assert again == 1


async def test_no_recipient_is_a_quiet_noop(sent):
    n = await hn.notify_human_handoff(
        tenant=_tenant(email=None),
        conversation_id=uuid4(),
        professional_id=None,
        reason="could_not_help",
    )
    assert n == 0 and sent == []


async def test_partial_failure_replay_only_retries_failed_recipient(sent, monkeypatch):
    doctor_id = uuid4()
    async def emails(_tid):
        return {str(doctor_id): "medico@example.com"}
    attempts = []
    recovered = False
    async def send(to, template, variables):
        attempts.append(to)
        if to == "medico@example.com" and not recovered:
            return EmailOutcome.SEND_FAILED
        return EmailOutcome.SENT
    monkeypatch.setattr(hn, "fetch_professional_emails", emails)
    monkeypatch.setattr(hn, "send_transactional_email_result", send)
    tenant, conv = _tenant(), uuid4()
    assert await hn.notify_human_handoff(tenant=tenant, conversation_id=conv,
        professional_id=doctor_id, reason="could_not_help") == 1
    recovered = True
    assert await hn.notify_human_handoff(tenant=tenant, conversation_id=conv,
        professional_id=doctor_id, reason="could_not_help") == 1
    assert attempts.count("clinica@example.com") == 1
    assert attempts.count("medico@example.com") == 4


async def test_lookup_failure_still_emails_clinic(sent, monkeypatch):
    async def emails(_tid):
        raise RuntimeError("sensitive upstream payload")
    monkeypatch.setattr(hn, "fetch_professional_emails", emails)
    assert await hn.notify_human_handoff(tenant=_tenant(), conversation_id=uuid4(),
        professional_id=uuid4(), reason="could_not_help") == 1


async def test_deduplicates_case_insensitive_recipient(sent, monkeypatch):
    doctor_id = uuid4()
    async def emails(_tid):
        return {str(doctor_id): " CLINICA@example.com "}
    monkeypatch.setattr(hn, "fetch_professional_emails", emails)
    assert await hn.notify_human_handoff(tenant=_tenant(), conversation_id=uuid4(),
        professional_id=doctor_id, reason="could_not_help") == 1


async def test_email_variables_only_contain_ids_and_reason(sent):
    tenant, conv = _tenant(), uuid4()
    await hn.notify_human_handoff(tenant=tenant, conversation_id=conv,
        professional_id=None, reason="private patient text")
    variables = sent[0][2]
    assert set(variables) == {"tenant_id", "conversation_id", "reason"}
    assert variables["tenant_id"] == str(tenant.id)
    assert variables["conversation_id"] == str(conv)
    assert "private patient text" not in str(variables)


async def test_sender_exception_retries_and_releases_claim(sent, monkeypatch):
    attempts = []
    async def send(to, template, variables):
        attempts.append(to)
        raise RuntimeError("patient@example.com private payload")
    monkeypatch.setattr(hn, "send_transactional_email_result", send)
    tenant, conv = _tenant(), uuid4()
    assert await hn.notify_human_handoff(tenant=tenant, conversation_id=conv,
        professional_id=None, reason="could_not_help") == 0
    assert len(attempts) == 3
    async def recovered(to, template, variables):
        return EmailOutcome.SENT
    monkeypatch.setattr(hn, "send_transactional_email_result", recovered)
    assert await hn.notify_human_handoff(tenant=tenant, conversation_id=conv,
        professional_id=None, reason="could_not_help") == 1


async def test_ledger_failure_does_not_escape(sent, monkeypatch):
    async def claim(_key):
        raise RuntimeError("private database payload")
    monkeypatch.setattr(hn, "_claim", claim)
    assert await hn.notify_human_handoff(tenant=_tenant(), conversation_id=uuid4(),
        professional_id=None, reason="could_not_help") == 0
    assert sent == []


async def test_occurrence_replay_survives_hour_change(sent, monkeypatch):
    from datetime import UTC, datetime, timedelta
    instant = datetime(2026, 9, 30, 23, 59, tzinfo=UTC)
    class Clock:
        @staticmethod
        def now(_tz):
            return instant
    monkeypatch.setattr(hn, "datetime", Clock)
    tenant, conv = _tenant(), uuid4()
    assert await hn.notify_human_handoff(tenant=tenant, conversation_id=conv,
        professional_id=None, reason="could_not_help") == 1
    instant += timedelta(hours=2)
    assert await hn.notify_human_handoff(tenant=tenant, conversation_id=conv,
        professional_id=None, reason="could_not_help") == 0
    assert len(sent) == 1


async def test_new_occurrence_can_notify_same_conversation(sent):
    tenant, conv = _tenant(), uuid4()
    first, second = uuid4(), uuid4()
    for occurrence, expected in [(first, 1), (first, 0), (second, 1), (second, 0)]:
        assert await hn.notify_human_handoff(tenant=tenant, conversation_id=conv,
            professional_id=None, reason="could_not_help", occurrence_id=occurrence) == expected
    assert len(sent) == 2
