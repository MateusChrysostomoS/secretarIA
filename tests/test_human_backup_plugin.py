"""Tests for plugins/human_backup.py — the human_backup_24_7 on_inbound hook.

In-memory-sqlite pattern established by test_bot_reply_gating.py. WhatsAppClient
and the alert-email helper are faked so no network call is ever made; the
handover mutation itself runs against a real (in-memory) session, so
`Conversation.handover_state` is asserted post-hoc exactly as production code
would observe it.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Conversation, HandoverState, Patient, Tenant  # noqa: E402
from secretaria.plugins import human_backup  # noqa: E402
from secretaria.plugins.base import InboundContext  # noqa: E402
from secretaria.plugins.registry import run_on_inbound  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "human_backup_24_7": False,
}


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="bronze_1",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


class _FakeWhatsAppClient:
    created: list["_FakeWhatsAppClient"] = []

    def __init__(self, access_token=None, phone_number_id=None):
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self.sent: list[tuple] = []
        _FakeWhatsAppClient.created.append(self)

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls(access_token=waba_token, phone_number_id=tenant.phone_number_id)

    async def send_text_message(self, to, body):
        self.sent.append(("text", to, body))
        return {"messages": [{"id": "wamid.test"}]}


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
    monkeypatch.setattr(human_backup, "async_session_factory", db)
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(human_backup, "WhatsAppClient", _FakeWhatsAppClient)
    yield


async def _make_conversation(
    db, *, business_hours: dict, timezone: str = "America/Sao_Paulo", contact_email=None
):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
            business_hours=business_hours,
            timezone=timezone,
            contact_email=contact_email,
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999", name="Maria")
        session.add_all([tenant, patient])
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(conversation)
        return tenant, patient, conversation


def _ctx(tenant: Tenant, conversation: Conversation) -> InboundContext:
    return InboundContext(
        tenant=tenant,
        conversation_id=conversation.id,
        patient_wa_id="5511999999",
        inbound_body="oi",
        waba_token="decrypted-waba-token",
    )


def _today_key() -> str:
    return human_backup._WEEKDAY_KEYS[datetime.now(UTC).weekday()]


# --------------------------------------------------------------------------
# _is_outside_business_hours — pure predicate
# --------------------------------------------------------------------------


def test_outside_hours_true_when_closed_all_day():
    # Hours ARE configured (on a different weekday) but today's own list is
    # empty -> closed today specifically, distinct from "nothing configured".
    other_day = human_backup._WEEKDAY_KEYS[(datetime.now(UTC).weekday() + 1) % 7]
    tenant = Tenant(
        clinic_name="C",
        phone_number_id="1",
        business_hours={_today_key(): [], other_day: [{"start": "08:00", "end": "18:00"}]},
        timezone="UTC",
    )
    assert human_backup._is_outside_business_hours(tenant) is True


def test_outside_hours_false_inside_window():
    tenant = Tenant(
        clinic_name="C",
        phone_number_id="1",
        business_hours={_today_key(): [{"start": "00:00", "end": "23:59"}]},
        timezone="UTC",
    )
    assert human_backup._is_outside_business_hours(tenant) is False


def test_outside_hours_true_outside_window():
    tenant = Tenant(
        clinic_name="C",
        phone_number_id="1",
        business_hours={_today_key(): [{"start": "00:00", "end": "00:01"}]},
        timezone="UTC",
    )
    assert human_backup._is_outside_business_hours(tenant) is True


def test_empty_business_hours_never_outside():
    tenant = Tenant(clinic_name="C", phone_number_id="1", business_hours={}, timezone="UTC")
    assert human_backup._is_outside_business_hours(tenant) is False


# --------------------------------------------------------------------------
# _on_inbound — full hook behavior
# --------------------------------------------------------------------------


async def test_on_inbound_outside_hours_flips_handover_and_acks(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant, patient, conversation = await _make_conversation(
        db,
        business_hours={_today_key(): [{"start": "00:00", "end": "00:01"}]},
        contact_email="owner@example.com",
    )
    alert_calls: list[tuple] = []

    async def _fake_alert(to_email, clinic_name):
        alert_calls.append((to_email, clinic_name))

    monkeypatch.setattr(human_backup, "send_human_backup_alert", _fake_alert)

    handled = await human_backup._on_inbound(_ctx(tenant, conversation))

    assert handled is True
    assert len(_FakeWhatsAppClient.created) == 1
    client = _FakeWhatsAppClient.created[0]
    assert client.sent[0][0] == "text"
    assert client.sent[0][2] == human_backup.get_settings().HUMAN_BACKUP_ACK_TEXT

    async with db() as session:
        refreshed = await session.get(Conversation, conversation.id)
        assert refreshed.handover_state == HandoverState.HUMAN_ACTIVE

    assert alert_calls == [("owner@example.com", "Clinic")]


async def test_on_inbound_inside_hours_returns_false(db):
    tenant, patient, conversation = await _make_conversation(
        db, business_hours={_today_key(): [{"start": "00:00", "end": "23:59"}]}
    )

    handled = await human_backup._on_inbound(_ctx(tenant, conversation))

    assert handled is False
    assert _FakeWhatsAppClient.created == []
    async with db() as session:
        refreshed = await session.get(Conversation, conversation.id)
        assert refreshed.handover_state == HandoverState.BOT_ACTIVE


async def test_on_inbound_empty_business_hours_never_fires(db):
    tenant, patient, conversation = await _make_conversation(db, business_hours={})

    handled = await human_backup._on_inbound(_ctx(tenant, conversation))

    assert handled is False
    assert _FakeWhatsAppClient.created == []


async def test_on_inbound_no_contact_email_skips_alert_but_still_handles(db):
    tenant, patient, conversation = await _make_conversation(
        db,
        business_hours={_today_key(): [{"start": "00:00", "end": "00:01"}]},
        contact_email=None,
    )

    handled = await human_backup._on_inbound(_ctx(tenant, conversation))

    assert handled is True  # alert email is best-effort, never blocks the hook


# --------------------------------------------------------------------------
# Wiring through the registry (entitlement gate on top of the hook)
# --------------------------------------------------------------------------


async def test_addon_off_hook_never_runs(db):
    tenant, patient, conversation = await _make_conversation(
        db, business_hours={_today_key(): [{"start": "00:00", "end": "00:01"}]}
    )
    summary = _summary(addons=dict(_ALL_ADDONS_OFF))  # human_backup_24_7 = False

    handled = await run_on_inbound(summary, _ctx(tenant, conversation))

    assert handled is False
    assert _FakeWhatsAppClient.created == []
    async with db() as session:
        refreshed = await session.get(Conversation, conversation.id)
        assert refreshed.handover_state == HandoverState.BOT_ACTIVE


async def test_addon_on_and_outside_hours_handles_via_registry(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, conversation = await _make_conversation(
        db, business_hours={_today_key(): [{"start": "00:00", "end": "00:01"}]}
    )

    async def _noop_alert(*args, **kwargs):
        return None

    monkeypatch.setattr(human_backup, "send_human_backup_alert", _noop_alert)
    summary = _summary(addons={**_ALL_ADDONS_OFF, "human_backup_24_7": True})

    handled = await run_on_inbound(summary, _ctx(tenant, conversation))

    assert handled is True
    assert len(_FakeWhatsAppClient.created) == 1
