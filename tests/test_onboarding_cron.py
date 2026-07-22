"""Integration tests for workers/onboarding_cron.py — both onboarding crons
(contract v1 §11).

Same in-memory-sqlite pattern established by test_reminders_plugin.py: a real
DB (monkeypatched in place of the Postgres-backed `async_session_factory`),
with `list_onboarding_tenants` / `post_onboarding_event` / `get_entitlements`
/ `emit_usage_event` / `send_transactional_email_message` all faked so no
LLM/network/brain-api/SMTP call is ever made — only the crons' own gating +
send + tally logic is under test. `within_send_window` is patched to always
True by default (per-test override for the dedicated window-gating tests) so
these tests never depend on the real wall-clock hour.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Patient,
    Professional,
    Tenant,
)
from secretaria.services.brain_onboarding import OnboardingTenant  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.workers import onboarding_cron  # noqa: E402

FIXED_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Shared fixtures / builders
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(onboarding_cron, "async_session_factory", db)
    # Deterministic by default: real wall-clock hour must never affect these
    # tests. The dedicated window-gating tests override this back to False.
    monkeypatch.setattr(onboarding_cron, "within_send_window", lambda now_local, window: True)
    yield


async def _make_tenant(db, tenant_id: UUID, timezone: str = "America/Sao_Paulo") -> Tenant:
    async with db() as session:
        tenant = Tenant(id=tenant_id, clinic_name="Clinic", timezone=timezone)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


def _item(**overrides) -> OnboardingTenant:
    """A fully-eligible-and-due retry-nudge candidate by default (D+3, no
    blocker, not paused). `config_status="completa"` by default so a plain
    `_item()` never ALSO triggers a config reminder as a side effect —
    config-focused tests override it explicitly.
    """
    now = datetime.now(UTC)
    base = dict(
        tenant_id=uuid4(),
        onboarding_state="aquecimento",
        blocker_reason=None,
        config_status="completa",
        onboarding_anchor_at=now - timedelta(days=3),
        next_retry_at=now - timedelta(minutes=5),
        retry_paused=False,
        config_reminder_paused=False,
        config_reminder_anchor_at=now - timedelta(days=3),
        last_config_reminder_at=None,
        closing_email_sent_at=None,
        manual_review_flagged_at=None,
        owner_email="owner@example.com",
        owner_name="Owner",
        clinic_name="Clinic",
        subscription_active=True,
    )
    base.update(overrides)
    return OnboardingTenant(**base)


def _fake_list(result):
    async def _fake():
        return result

    return _fake


def _capture_email(monkeypatch, sent: bool = True):
    calls: list[tuple] = []

    async def _fake(*, to, template, variables):
        calls.append((to, template, variables))
        return sent

    monkeypatch.setattr(onboarding_cron, "send_transactional_email_message", _fake)
    return calls


def _capture_events(monkeypatch, applied: bool = True):
    calls: list[tuple] = []

    async def _fake(tenant_id, event, at, next_retry_at=None):
        calls.append((tenant_id, event, at, next_retry_at))
        return applied

    monkeypatch.setattr(onboarding_cron, "post_onboarding_event", _fake)
    return calls


def _capture_usage(monkeypatch, recorded: bool = True):
    calls: list[dict] = []

    async def _fake(*, tenant_id, feature, amount, event_id):
        calls.append(
            {"tenant_id": tenant_id, "feature": feature, "amount": amount, "event_id": event_id}
        )
        return recorded

    monkeypatch.setattr(onboarding_cron, "emit_usage_event", _fake)
    return calls


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons={},
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


# --------------------------------------------------------------------------
# run_onboarding_nudges — brain-api pull / missing local tenant
# --------------------------------------------------------------------------


async def test_brain_api_pull_failure_is_fail_soft(db, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list(None))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})  # must not raise

    assert email_calls == []
    assert event_calls == []


async def test_empty_list_is_a_clean_noop(db, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([]))
    email_calls = _capture_email(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []


async def test_tenant_missing_locally_is_skipped_without_crashing(
    db, monkeypatch: pytest.MonkeyPatch
):
    item = _item()  # no matching local Tenant row ever created
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})  # must not raise

    assert email_calls == []
    assert event_calls == []


# --------------------------------------------------------------------------
# run_onboarding_nudges — retry nudge eligibility + send + next_retry_at
# --------------------------------------------------------------------------


async def test_eligible_tenant_gets_nudged_and_event_posted_with_next_retry_at(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    anchor = datetime.now(UTC) - timedelta(days=3)
    item = _item(tenant_id=tenant_id, onboarding_anchor_at=anchor)
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert len(email_calls) == 1
    to, template, variables = email_calls[0]
    assert to == "owner@example.com"
    assert template == "retry_nudge_atividade_insuficiente"
    assert variables == {"clinic_name": "Clinic"}

    assert len(event_calls) == 1
    tid, event, at, next_retry_at = event_calls[0]
    assert tid == tenant_id
    assert event == "retry_nudge_sent"
    expected_next = anchor + timedelta(days=7)  # D+3 -> next checkpoint D+7
    assert abs((next_retry_at - expected_next).total_seconds()) < 5


async def test_blocker_selects_matching_template(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(tenant_id=tenant_id, blocker_reason="outro")
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls[0][1] == "retry_nudge_outro"


async def test_retry_paused_tenant_is_skipped(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(tenant_id=tenant_id, retry_paused=True)
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_manual_action_blocker_is_skipped(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_state="aguardando_acao_manual",
        blocker_reason="sem_pagina_facebook",
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_non_eligible_state_is_skipped(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(tenant_id=tenant_id, onboarding_state="ativo")
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_subscription_inactive_is_skipped(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(tenant_id=tenant_id, subscription_active=False)
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_next_retry_at_not_yet_due_is_skipped(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(tenant_id=tenant_id, next_retry_at=datetime.now(UTC) + timedelta(days=1))
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_next_retry_at_null_is_skipped(db, monkeypatch: pytest.MonkeyPatch):
    """No next_retry_at scheduled (e.g. already exhausted the window) -> no send."""
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(tenant_id=tenant_id, next_retry_at=None)
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_retry_nudge_outside_window_is_not_sent(db, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(onboarding_cron, "within_send_window", lambda now_local, window: False)
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(tenant_id=tenant_id)
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


# --------------------------------------------------------------------------
# run_onboarding_nudges — D+30 manual-review flag (NOT window-gated)
# --------------------------------------------------------------------------


async def test_d30_flags_manual_review_once(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_anchor_at=datetime.now(UTC) - timedelta(days=31),
        next_retry_at=None,
        manual_review_flagged_at=None,
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []  # no message is sent for this event
    assert len(event_calls) == 1
    tid, event, at, next_retry_at = event_calls[0]
    assert (tid, event) == (tenant_id, "manual_review_flagged")


async def test_d30_flag_fires_even_outside_send_window(db, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(onboarding_cron, "within_send_window", lambda now_local, window: False)
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_anchor_at=datetime.now(UTC) - timedelta(days=31),
        next_retry_at=None,
        manual_review_flagged_at=None,
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert [e for _, e, _, _ in event_calls] == ["manual_review_flagged"]


async def test_d30_flag_not_reposted_once_already_set(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_anchor_at=datetime.now(UTC) - timedelta(days=31),
        next_retry_at=None,
        manual_review_flagged_at=datetime.now(UTC) - timedelta(days=1),
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert "manual_review_flagged" not in [e for _, e, _, _ in event_calls]


# --------------------------------------------------------------------------
# run_onboarding_nudges — D+60 closing email (auto-retry class only)
# --------------------------------------------------------------------------


async def test_d60_sends_closing_email_for_auto_retry_class(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_anchor_at=datetime.now(UTC) - timedelta(days=61),
        next_retry_at=None,
        closing_email_sent_at=None,
        manual_review_flagged_at=datetime.now(UTC) - timedelta(days=30),
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert [t for _, t, _ in email_calls] == ["closing_email"]
    assert [e for _, e, _, _ in event_calls] == ["closing_email_sent"]


async def test_d60_closing_email_not_sent_for_manual_action_class(
    db, monkeypatch: pytest.MonkeyPatch
):
    """Manual-action blockers are excluded from the eligible population
    entirely, so they never reach D+60 auto-closing (contract v1 §11: "the
    auto-retry class only - manual-action blockers get no closing email")."""
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_state="aguardando_acao_manual",
        blocker_reason="numero_em_outro_bsp",
        onboarding_anchor_at=datetime.now(UTC) - timedelta(days=61),
        next_retry_at=None,
        closing_email_sent_at=None,
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_d60_closing_email_not_resent_once_already_sent(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_anchor_at=datetime.now(UTC) - timedelta(days=61),
        next_retry_at=None,
        closing_email_sent_at=datetime.now(UTC) - timedelta(days=1),
        manual_review_flagged_at=datetime.now(UTC) - timedelta(days=30),
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_d60_closing_email_outside_window_is_deferred(db, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(onboarding_cron, "within_send_window", lambda now_local, window: False)
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_anchor_at=datetime.now(UTC) - timedelta(days=61),
        next_retry_at=None,
        closing_email_sent_at=None,
        manual_review_flagged_at=datetime.now(UTC) - timedelta(days=30),
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


# --------------------------------------------------------------------------
# run_onboarding_nudges — config reminders (independent of state/blocker)
# --------------------------------------------------------------------------


async def test_config_reminder_template_connected(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_state="conectado",
        onboarding_anchor_at=None,  # isolate: no retry-branch interference
        config_status="incompleta",
        config_reminder_anchor_at=datetime.now(UTC) - timedelta(days=1),
        last_config_reminder_at=None,
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert [t for _, t, _ in email_calls] == ["config_reminder_connected"]
    assert [e for _, e, _, _ in event_calls] == ["config_reminder_sent"]


async def test_config_reminder_template_pre_connection(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_state="aguardando_elegibilidade",
        onboarding_anchor_at=None,  # isolate: no retry-branch interference
        config_status="incompleta",
        config_reminder_anchor_at=datetime.now(UTC) - timedelta(days=1),
        last_config_reminder_at=None,
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert [t for _, t, _ in email_calls] == ["config_reminder_pre_connection"]
    assert [e for _, e, _, _ in event_calls] == ["config_reminder_sent"]


async def test_config_reminder_skipped_when_status_completa(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_anchor_at=None,
        config_status="completa",
        config_reminder_anchor_at=datetime.now(UTC) - timedelta(days=1),
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_config_reminder_paused_is_skipped(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_anchor_at=None,
        config_status="incompleta",
        config_reminder_anchor_at=datetime.now(UTC) - timedelta(days=1),
        config_reminder_paused=True,
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_config_reminder_skipped_when_subscription_inactive(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_anchor_at=None,
        config_status="incompleta",
        config_reminder_anchor_at=datetime.now(UTC) - timedelta(days=1),
        subscription_active=False,
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_config_reminder_not_yet_due_is_skipped(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    item = _item(
        tenant_id=tenant_id,
        onboarding_anchor_at=None,
        config_status="incompleta",
        config_reminder_anchor_at=datetime.now(UTC) - timedelta(hours=1),  # < D+1
        last_config_reminder_at=None,
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert email_calls == []
    assert event_calls == []


async def test_config_reminder_due_after_prior_send(db, monkeypatch: pytest.MonkeyPatch):
    """D+3 checkpoint reached after a D+1 send already happened."""
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id)
    anchor = datetime.now(UTC) - timedelta(days=3)
    item = _item(
        tenant_id=tenant_id,
        onboarding_anchor_at=None,
        config_status="incompleta",
        config_reminder_anchor_at=anchor,
        last_config_reminder_at=anchor + timedelta(days=1),
    )
    monkeypatch.setattr(onboarding_cron, "list_onboarding_tenants", _fake_list([item]))
    email_calls = _capture_email(monkeypatch)
    event_calls = _capture_events(monkeypatch)

    await onboarding_cron.run_onboarding_nudges({})

    assert len(email_calls) == 1
    assert len(event_calls) == 1


# --------------------------------------------------------------------------
# run_onboarding_nudges — per-tenant isolation
# --------------------------------------------------------------------------


async def test_one_bad_tenant_does_not_abort_the_sweep(db, monkeypatch: pytest.MonkeyPatch):
    good_id, bad_id = uuid4(), uuid4()
    await _make_tenant(db, good_id)
    await _make_tenant(db, bad_id)
    good_item = _item(tenant_id=good_id)
    bad_item = _item(tenant_id=bad_id)
    monkeypatch.setattr(
        onboarding_cron, "list_onboarding_tenants", _fake_list([bad_item, good_item])
    )
    _capture_email(monkeypatch)
    event_calls: list[tuple] = []

    async def _flaky_post(tenant_id, event, at, next_retry_at=None):
        if tenant_id == bad_id:
            raise RuntimeError("boom")
        event_calls.append((tenant_id, event, at, next_retry_at))
        return True

    monkeypatch.setattr(onboarding_cron, "post_onboarding_event", _flaky_post)

    await onboarding_cron.run_onboarding_nudges({})  # must not raise

    assert len(event_calls) == 1
    assert event_calls[0][0] == good_id


# --------------------------------------------------------------------------
# run_patient_usage_metering — tally correctness (direct _tally_tenant_month)
# --------------------------------------------------------------------------


async def _seed_professional_and_patient(db, tenant_id: UUID) -> tuple[UUID, UUID]:
    prof_id, patient_id = uuid4(), uuid4()
    async with db() as session:
        session.add(Professional(id=prof_id, tenant_id=tenant_id, name="Dr A", is_active=True))
        session.add(Patient(id=patient_id, tenant_id=tenant_id, wa_id="551199999"))
        await session.commit()
    return prof_id, patient_id


async def test_tally_deduplicates_multiple_appointments_for_same_pair(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id, patient_id = await _seed_professional_and_patient(db, tenant_id)
    async with db() as session:
        for i in range(2):
            session.add(
                Appointment(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    patient_id=patient_id,
                    professional_id=prof_id,
                    google_event_id=f"e{i}",
                    start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
                    end_at=datetime(2026, 7, 10, 9, 30, tzinfo=UTC),
                    status=AppointmentStatus.SCHEDULED,
                )
            )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        counts = await onboarding_cron._tally_tenant_month(session, tenant, FIXED_NOW)

    assert counts["emitted"] == 1
    assert len(usage_calls) == 1
    assert usage_calls[0]["event_id"] == f"bp:{tenant_id}:{prof_id}:{patient_id}:2026-07"
    assert usage_calls[0]["feature"] == "billable_patients"
    assert usage_calls[0]["amount"] == 1


async def test_tally_cancelled_only_appointment_is_included(db, monkeypatch: pytest.MonkeyPatch):
    """A patient whose ONLY appointment this month was cancelled still counts:
    booking (and the work that went into it) happened even though the visit
    itself didn't - the tally query deliberately carries no status filter, so
    a cancelled-only month is no longer excluded (reversed from the old
    cancelled-exclusion design)."""
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id, patient_id = await _seed_professional_and_patient(db, tenant_id)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=prof_id,
                google_event_id="e1",
                start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 10, 9, 30, tzinfo=UTC),
                status=AppointmentStatus.CANCELLED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        counts = await onboarding_cron._tally_tenant_month(session, tenant, FIXED_NOW)

    assert counts["emitted"] == 1
    assert len(usage_calls) == 1
    assert usage_calls[0]["event_id"] == f"bp:{tenant_id}:{prof_id}:{patient_id}:2026-07"
    assert usage_calls[0]["feature"] == "billable_patients"


async def test_tally_rescheduled_appointment_is_billable(db, monkeypatch: pytest.MonkeyPatch):
    """RESCHEDULED is NOT cancelled-like - the same booking, just moved once."""
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id, patient_id = await _seed_professional_and_patient(db, tenant_id)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=prof_id,
                google_event_id="e1",
                start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 10, 9, 30, tzinfo=UTC),
                status=AppointmentStatus.RESCHEDULED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        counts = await onboarding_cron._tally_tenant_month(session, tenant, FIXED_NOW)

    assert counts["emitted"] == 1
    assert len(usage_calls) == 1


async def test_tally_excludes_block_slots_with_null_patient(db, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id, _patient_id = await _seed_professional_and_patient(db, tenant_id)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=None,
                professional_id=prof_id,
                google_event_id="block1",
                start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 10, 9, 30, tzinfo=UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        counts = await onboarding_cron._tally_tenant_month(session, tenant, FIXED_NOW)

    assert usage_calls == []
    assert counts["emitted"] == 0


async def test_tally_null_professional_attributed_to_sole_active_professional(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id, patient_id = await _seed_professional_and_patient(db, tenant_id)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=None,
                google_event_id="e1",
                start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 10, 9, 30, tzinfo=UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        counts = await onboarding_cron._tally_tenant_month(session, tenant, FIXED_NOW)

    assert counts["emitted"] == 1
    assert usage_calls[0]["event_id"] == f"bp:{tenant_id}:{prof_id}:{patient_id}:2026-07"


async def test_tally_null_professional_fallback_does_not_double_emit(
    db, monkeypatch: pytest.MonkeyPatch
):
    """One patient with TWO appointments this month: one directly attributed
    to the tenant's sole active professional, one left NULL (which resolves
    via the same sole-active-professional fallback to that SAME
    professional). `pairs` sees two distinct SQL tuples - (prof_id,
    patient_id) and (None, patient_id) - that collapse to the identical
    event_id only after the fallback substitution. Without the `seen` guard
    this would POST the same event_id twice in one sweep; with it, exactly
    one event is emitted."""
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id, patient_id = await _seed_professional_and_patient(db, tenant_id)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=prof_id,
                google_event_id="direct",
                start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 10, 9, 30, tzinfo=UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=None,
                google_event_id="null_attributed",
                start_at=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 12, 9, 30, tzinfo=UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        counts = await onboarding_cron._tally_tenant_month(session, tenant, FIXED_NOW)

    assert counts["emitted"] == 1
    assert len(usage_calls) == 1
    assert usage_calls[0]["event_id"] == f"bp:{tenant_id}:{prof_id}:{patient_id}:2026-07"


async def test_tally_null_professional_skipped_when_multiple_active_professionals(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="UTC")
    prof_a, prof_b = uuid4(), uuid4()
    patient_id = uuid4()
    async with db() as session:
        session.add(Professional(id=prof_a, tenant_id=tenant_id, name="Dr A", is_active=True))
        session.add(Professional(id=prof_b, tenant_id=tenant_id, name="Dr B", is_active=True))
        session.add(Patient(id=patient_id, tenant_id=tenant_id, wa_id="551"))
        await session.flush()
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=None,
                google_event_id="e1",
                start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 10, 9, 30, tzinfo=UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        counts = await onboarding_cron._tally_tenant_month(session, tenant, FIXED_NOW)

    assert usage_calls == []
    assert counts["emitted"] == 0
    assert counts["skipped_unattributed"] == 1


async def test_tally_null_professional_skipped_when_zero_active_professionals(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="UTC")
    inactive_prof = uuid4()
    patient_id = uuid4()
    async with db() as session:
        session.add(
            Professional(id=inactive_prof, tenant_id=tenant_id, name="Dr A", is_active=False)
        )
        session.add(Patient(id=patient_id, tenant_id=tenant_id, wa_id="551"))
        await session.flush()
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=None,
                google_event_id="e1",
                start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 10, 9, 30, tzinfo=UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        counts = await onboarding_cron._tally_tenant_month(session, tenant, FIXED_NOW)

    assert usage_calls == []
    assert counts["skipped_unattributed"] == 1


async def test_tally_event_id_stable_across_reruns(db, monkeypatch: pytest.MonkeyPatch):
    """Same month + same pair -> same event_id every run, which is exactly
    what makes the daily re-run idempotent server-side."""
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id, patient_id = await _seed_professional_and_patient(db, tenant_id)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=prof_id,
                google_event_id="e1",
                start_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 7, 10, 9, 30, tzinfo=UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        await onboarding_cron._tally_tenant_month(session, tenant, FIXED_NOW)
    async with db() as session:
        await onboarding_cron._tally_tenant_month(session, tenant, FIXED_NOW)

    assert len(usage_calls) == 2
    assert usage_calls[0]["event_id"] == usage_calls[1]["event_id"]


async def test_tally_month_boundary_respects_tenant_local_tz(db, monkeypatch: pytest.MonkeyPatch):
    """America/Sao_Paulo is UTC-3: a UTC timestamp just after midnight on the
    1st is still the PREVIOUS month locally, and one just after 21:00 UTC on
    the last day is still the SAME month locally."""
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="America/Sao_Paulo")
    tz = ZoneInfo("America/Sao_Paulo")
    now_local = datetime(2026, 7, 15, 12, 0, tzinfo=tz)

    prof_id = uuid4()
    patients = {name: uuid4() for name in ("prev", "edge", "curr", "late", "next")}
    async with db() as session:
        session.add(Professional(id=prof_id, tenant_id=tenant_id, name="Dr A", is_active=True))
        for pid in patients.values():
            session.add(Patient(id=pid, tenant_id=tenant_id, wa_id=str(pid)[:15]))
        await session.flush()

        def _appt(name: str, start_at: datetime) -> Appointment:
            return Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patients[name],
                professional_id=prof_id,
                google_event_id=name,
                start_at=start_at,
                end_at=start_at + timedelta(minutes=30),
                status=AppointmentStatus.SCHEDULED,
            )

        # 2026-07-01T01:30 UTC = 2026-06-30T22:30 local -> PREVIOUS month -> excluded
        session.add(_appt("prev", datetime(2026, 7, 1, 1, 30, tzinfo=UTC)))
        # 2026-07-01T03:00 UTC = 2026-07-01T00:00 local -> exactly month start -> included
        session.add(_appt("edge", datetime(2026, 7, 1, 3, 0, tzinfo=UTC)))
        # clearly mid-July local -> included
        session.add(_appt("curr", datetime(2026, 7, 15, 15, 0, tzinfo=UTC)))
        # 2026-08-01T02:30 UTC = 2026-07-31T23:30 local -> still July -> included
        session.add(_appt("late", datetime(2026, 8, 1, 2, 30, tzinfo=UTC)))
        # 2026-08-01T03:30 UTC = 2026-08-01T00:30 local -> NEXT month -> excluded
        session.add(_appt("next", datetime(2026, 8, 1, 3, 30, tzinfo=UTC)))
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        counts = await onboarding_cron._tally_tenant_month(session, tenant, now_local)

    included_patient_ids = {UUID(c["event_id"].split(":")[3]) for c in usage_calls}
    assert included_patient_ids == {patients["edge"], patients["curr"], patients["late"]}
    assert counts["emitted"] == 3
    assert all(c["event_id"].endswith(":2026-07") for c in usage_calls)


# --------------------------------------------------------------------------
# run_patient_usage_metering — active-professionals tally
# (direct _tally_active_professionals_month)
# --------------------------------------------------------------------------


async def test_tally_active_professionals_emits_one_event_per_active_professional(
    db, monkeypatch: pytest.MonkeyPatch
):
    """N active professionals -> N events, each with feature
    "active_professionals" and the exact prof:{tenant}:{professional}:{month}
    event id (same idempotency shape as the patient tally's bp: keys);
    inactive professionals are excluded entirely."""
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="UTC")
    prof_a, prof_b = uuid4(), uuid4()
    inactive_prof = uuid4()
    async with db() as session:
        session.add(Professional(id=prof_a, tenant_id=tenant_id, name="Dr A", is_active=True))
        session.add(Professional(id=prof_b, tenant_id=tenant_id, name="Dr B", is_active=True))
        session.add(
            Professional(id=inactive_prof, tenant_id=tenant_id, name="Dr C", is_active=False)
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        counts = await onboarding_cron._tally_active_professionals_month(session, tenant, "2026-07")

    assert counts["professionals_emitted"] == 2
    assert len(usage_calls) == 2
    assert {c["event_id"] for c in usage_calls} == {
        f"prof:{tenant_id}:{prof_a}:2026-07",
        f"prof:{tenant_id}:{prof_b}:2026-07",
    }
    assert all(c["feature"] == "active_professionals" for c in usage_calls)
    assert all(c["amount"] == 1 for c in usage_calls)


async def test_tally_active_professionals_event_id_stable_across_reruns(
    db, monkeypatch: pytest.MonkeyPatch
):
    """Same month + same professional -> same event_id every run, which is
    exactly what makes the daily re-run idempotent server-side (brain-api
    dedupes on event_id, not this code)."""
    tenant_id = uuid4()
    tenant = await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id = uuid4()
    async with db() as session:
        session.add(Professional(id=prof_id, tenant_id=tenant_id, name="Dr A", is_active=True))
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)
    async with db() as session:
        await onboarding_cron._tally_active_professionals_month(session, tenant, "2026-07")
    async with db() as session:
        await onboarding_cron._tally_active_professionals_month(session, tenant, "2026-07")

    assert len(usage_calls) == 2
    expected_event_id = f"prof:{tenant_id}:{prof_id}:2026-07"
    assert usage_calls[0]["event_id"] == usage_calls[1]["event_id"] == expected_event_id


# --------------------------------------------------------------------------
# run_patient_usage_metering — entitlement gating + sweep wiring
# --------------------------------------------------------------------------


async def test_metering_skips_tenant_with_inactive_entitlement(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id, patient_id = await _seed_professional_and_patient(db, tenant_id)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=prof_id,
                google_event_id="e1",
                start_at=datetime.now(UTC),
                end_at=datetime.now(UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)

    async def _inactive(tenant_id_arg, redis):
        return _summary(active=False)

    monkeypatch.setattr(onboarding_cron, "get_entitlements", _inactive)

    await onboarding_cron.run_patient_usage_metering({"redis": None})

    assert usage_calls == []


async def test_metering_skips_tenant_with_unresolvable_entitlement(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id, patient_id = await _seed_professional_and_patient(db, tenant_id)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=prof_id,
                google_event_id="e1",
                start_at=datetime.now(UTC),
                end_at=datetime.now(UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)

    async def _unresolvable(tenant_id_arg, redis):
        return None

    monkeypatch.setattr(onboarding_cron, "get_entitlements", _unresolvable)

    await onboarding_cron.run_patient_usage_metering({"redis": None})

    assert usage_calls == []


async def test_metering_processes_active_tenant_end_to_end(db, monkeypatch: pytest.MonkeyPatch):
    """The sweep runs BOTH tallies for an active tenant: one billable_patients
    event (the seeded appointment) and one active_professionals event (the
    seeded, active professional)."""
    tenant_id = uuid4()
    await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id, patient_id = await _seed_professional_and_patient(db, tenant_id)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=prof_id,
                google_event_id="e1",
                start_at=datetime.now(UTC),
                end_at=datetime.now(UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)

    async def _active(tenant_id_arg, redis):
        return _summary(active=True)

    monkeypatch.setattr(onboarding_cron, "get_entitlements", _active)

    await onboarding_cron.run_patient_usage_metering({"redis": None})

    assert len(usage_calls) == 2
    assert all(c["tenant_id"] == str(tenant_id) for c in usage_calls)
    assert {c["feature"] for c in usage_calls} == {"billable_patients", "active_professionals"}


async def test_metering_one_bad_tenant_does_not_abort_the_sweep(
    db, monkeypatch: pytest.MonkeyPatch
):
    good_id, bad_id = uuid4(), uuid4()
    await _make_tenant(db, good_id, timezone="UTC")
    await _make_tenant(db, bad_id, timezone="UTC")
    good_prof, good_patient = await _seed_professional_and_patient(db, good_id)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=good_id,
                patient_id=good_patient,
                professional_id=good_prof,
                google_event_id="e1",
                start_at=datetime.now(UTC),
                end_at=datetime.now(UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()

    usage_calls = _capture_usage(monkeypatch)

    async def _flaky(tenant_id_arg, redis):
        if tenant_id_arg == bad_id:
            raise RuntimeError("boom")
        return _summary(active=True)

    monkeypatch.setattr(onboarding_cron, "get_entitlements", _flaky)

    await onboarding_cron.run_patient_usage_metering({"redis": None})  # must not raise

    # Both tallies ran for the good tenant (billable_patients + active_professionals);
    # the bad tenant contributes nothing since it fails at the entitlement check,
    # before either tally runs.
    assert len(usage_calls) == 2
    assert all(c["tenant_id"] == str(good_id) for c in usage_calls)
    assert {c["feature"] for c in usage_calls} == {"billable_patients", "active_professionals"}


# --------------------------------------------------------------------------
# run_patient_usage_metering — the two tallies fail in independent domains
# --------------------------------------------------------------------------


async def _seed_active_tenant_with_appointment(db, tenant_id: UUID) -> tuple[UUID, UUID]:
    """An active-entitlement-ready tenant with one active professional, one
    patient, and one billable appointment - everything both tallies need."""
    await _make_tenant(db, tenant_id, timezone="UTC")
    prof_id, patient_id = await _seed_professional_and_patient(db, tenant_id)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=patient_id,
                professional_id=prof_id,
                google_event_id="e1",
                start_at=datetime.now(UTC),
                end_at=datetime.now(UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()
    return prof_id, patient_id


async def test_sweep_professionals_tally_still_runs_when_patient_tally_raises(
    db, monkeypatch: pytest.MonkeyPatch
):
    """The two tallies fail in independent domains: a raising
    _tally_tenant_month must not suppress _tally_active_professionals_month
    for the same tenant (each has its own inner try/except now)."""
    tenant_id = uuid4()
    await _seed_active_tenant_with_appointment(db, tenant_id)

    usage_calls = _capture_usage(monkeypatch)

    async def _active(tenant_id_arg, redis):
        return _summary(active=True)

    monkeypatch.setattr(onboarding_cron, "get_entitlements", _active)

    async def _boom(session, tenant, now_local):
        raise RuntimeError("boom")

    monkeypatch.setattr(onboarding_cron, "_tally_tenant_month", _boom)

    await onboarding_cron.run_patient_usage_metering({"redis": None})  # must not raise

    assert len(usage_calls) == 1
    assert usage_calls[0]["feature"] == "active_professionals"
    assert usage_calls[0]["tenant_id"] == str(tenant_id)


async def test_sweep_patient_tally_still_runs_when_professionals_tally_raises(
    db, monkeypatch: pytest.MonkeyPatch
):
    """And the reverse: a raising _tally_active_professionals_month must not
    suppress _tally_tenant_month for the same tenant."""
    tenant_id = uuid4()
    await _seed_active_tenant_with_appointment(db, tenant_id)

    usage_calls = _capture_usage(monkeypatch)

    async def _active(tenant_id_arg, redis):
        return _summary(active=True)

    monkeypatch.setattr(onboarding_cron, "get_entitlements", _active)

    async def _boom(session, tenant, month_key):
        raise RuntimeError("boom")

    monkeypatch.setattr(onboarding_cron, "_tally_active_professionals_month", _boom)

    await onboarding_cron.run_patient_usage_metering({"redis": None})  # must not raise

    assert len(usage_calls) == 1
    assert usage_calls[0]["feature"] == "billable_patients"
    assert usage_calls[0]["tenant_id"] == str(tenant_id)
