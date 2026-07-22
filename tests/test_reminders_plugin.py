"""Tests for plugins/reminders.py — the appointment-reminder cron sweep.

In-memory-sqlite pattern established by test_bot_reply_gating.py /
test_waba_encryption.py (a real DB, monkeypatched in place of the
Postgres-backed `async_session_factory`). get_entitlements, get_waba_token,
WhatsAppClient and emit_usage_event are faked so no LLM/network/brain-api
call is ever made; only the sweep's own gating + send + ledger logic is
under test.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
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
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    PixDeposit,
    PixDepositStatus,
    ProcessedEvent,
    Tenant,
)
from secretaria.plugins import reminders  # noqa: E402
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
        secretaria_tier="basico",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


def _entitled_fake(**overrides):
    async def _fake(tenant_id, redis):
        return _summary(**overrides)

    return _fake


class _FakeWhatsAppClient:
    """Records constructed instances + sends; installed in place of the real client."""

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

    async def send_template(self, to, template, lang, variables, button_payloads=None):
        self.sent.append(("template", to, template, lang, variables, button_payloads))
        return {"messages": [{"id": "wamid.test"}]}

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", to, body, buttons))
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


async def _fake_get_waba_token(session, tenant_id):
    return "decrypted-waba-token"


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(reminders, "async_session_factory", db)
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(reminders, "WhatsAppClient", _FakeWhatsAppClient)
    monkeypatch.setattr(reminders, "get_waba_token", _fake_get_waba_token)
    yield


async def _make_scenario(
    db,
    *,
    lead: timedelta,
    status: AppointmentStatus = AppointmentStatus.SCHEDULED,
    opt_out: bool = False,
    last_inbound_ago: timedelta | None = None,
    tenant_language: str = "pt-BR",
):
    """Tenant + patient (+ optional inbound message) + one appointment `lead` from now."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
            language=tenant_language,
        )
        patient = Patient(
            id=uuid4(),
            tenant_id=tenant.id,
            wa_id="5511999999",
            name="Maria",
            reminder_opt_out=opt_out,
        )
        session.add_all([tenant, patient])
        await session.flush()

        if last_inbound_ago is not None:
            conversation = Conversation(id=uuid4(), tenant_id=tenant.id, patient_id=patient.id)
            session.add(conversation)
            await session.flush()
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND,
                    sender=MessageSender.PATIENT,
                    body="oi",
                    created_at=datetime.now(UTC) - last_inbound_ago,
                )
            )

        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=datetime.now(UTC) + lead,
            end_at=datetime.now(UTC) + lead + timedelta(minutes=30),
            status=status,
        )
        session.add(appointment)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(patient)
        await session.refresh(appointment)
        return tenant, patient, appointment


async def _ledger_count(db, key: str) -> int:
    async with db() as session:
        rows = (
            await session.scalars(select(ProcessedEvent).where(ProcessedEvent.event_id == key))
        ).all()
        return len(rows)


# --------------------------------------------------------------------------
# Core sweep + idempotency
# --------------------------------------------------------------------------


async def test_entitled_24h_appointment_sends_once_and_ledger_row_written(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant, patient, appointment = await _make_scenario(
        db, lead=timedelta(hours=24), last_inbound_ago=timedelta(hours=1)
    )
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})

    assert len(_FakeWhatsAppClient.created) == 1
    key = reminders._reminder_key("24h", appointment.id)
    assert await _ledger_count(db, key) == 1


async def test_second_sweep_does_not_resend(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, appointment = await _make_scenario(
        db, lead=timedelta(hours=24), last_inbound_ago=timedelta(hours=1)
    )
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})
    await reminders.send_appointment_reminders({"redis": None})

    assert len(_FakeWhatsAppClient.created) == 1  # not 2
    key = reminders._reminder_key("24h", appointment.id)
    assert await _ledger_count(db, key) == 1


async def test_1h_window_independent_of_24h(db, monkeypatch: pytest.MonkeyPatch):
    tenant_a, _, appt_24h = await _make_scenario(db, lead=timedelta(hours=24))
    tenant_b, _, appt_1h = await _make_scenario(db, lead=timedelta(hours=1))
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})

    assert len(_FakeWhatsAppClient.created) == 2
    assert await _ledger_count(db, reminders._reminder_key("24h", appt_24h.id)) == 1
    assert await _ledger_count(db, reminders._reminder_key("1h", appt_1h.id)) == 1
    # cross-kind keys were never claimed
    assert await _ledger_count(db, reminders._reminder_key("1h", appt_24h.id)) == 0
    assert await _ledger_count(db, reminders._reminder_key("24h", appt_1h.id)) == 0


async def test_cancelled_appointment_is_skipped(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, appointment = await _make_scenario(
        db, lead=timedelta(hours=1), status=AppointmentStatus.CANCELLED
    )
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})

    assert _FakeWhatsAppClient.created == []
    assert await _ledger_count(db, reminders._reminder_key("1h", appointment.id)) == 0


# --------------------------------------------------------------------------
# Entitlement gating (silent no-op) + opt-out
# --------------------------------------------------------------------------


async def test_inactive_subscription_is_silent_no_op(db, monkeypatch: pytest.MonkeyPatch):
    """Reminders are CORE now (PROMPT S3) — the only subscription-level gate
    left is active + secretaria_enabled, not any tier/addon."""
    tenant, patient, appointment = await _make_scenario(db, lead=timedelta(hours=1))
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake(active=False))

    await reminders.send_appointment_reminders({"redis": None})

    assert _FakeWhatsAppClient.created == []
    assert await _ledger_count(db, reminders._reminder_key("1h", appointment.id)) == 0


async def test_secretaria_disabled_is_silent_no_op(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, appointment = await _make_scenario(db, lead=timedelta(hours=1))
    monkeypatch.setattr(
        reminders, "get_entitlements", _entitled_fake(secretaria_enabled=False)
    )

    await reminders.send_appointment_reminders({"redis": None})

    assert _FakeWhatsAppClient.created == []
    assert await _ledger_count(db, reminders._reminder_key("1h", appointment.id)) == 0


async def test_basico_tier_zero_addons_now_sends(db, monkeypatch: pytest.MonkeyPatch):
    """The ungating's whole point: a bare basico tenant with every addon off
    (no bronze_1 tier, no reactivation_pack addon — the OLD gate, since retired)
    still gets reminders."""
    tenant, patient, appointment = await _make_scenario(db, lead=timedelta(hours=1))
    monkeypatch.setattr(
        reminders,
        "get_entitlements",
        _entitled_fake(secretaria_tier="basico", addons=dict(_ALL_ADDONS_OFF)),
    )

    await reminders.send_appointment_reminders({"redis": None})

    assert len(_FakeWhatsAppClient.created) == 1
    key = reminders._reminder_key("1h", appointment.id)
    assert await _ledger_count(db, key) == 1


async def test_opt_out_patient_is_skipped(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, appointment = await _make_scenario(db, lead=timedelta(hours=1), opt_out=True)
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})

    assert _FakeWhatsAppClient.created == []
    assert await _ledger_count(db, reminders._reminder_key("1h", appointment.id)) == 0


async def test_entitlements_fetched_once_per_tenant_per_sweep(db, monkeypatch: pytest.MonkeyPatch):
    """Two due appointments for the SAME tenant -> one get_entitlements call."""
    tenant, patient, appt1 = await _make_scenario(db, lead=timedelta(hours=1))
    async with db() as session:
        appt2 = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Retorno",
            start_at=datetime.now(UTC) + timedelta(minutes=59),
            end_at=datetime.now(UTC) + timedelta(minutes=89),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appt2)
        await session.commit()

    calls: list = []

    async def _counting_fake(tenant_id, redis):
        calls.append(tenant_id)
        return _summary()

    monkeypatch.setattr(reminders, "get_entitlements", _counting_fake)

    await reminders.send_appointment_reminders({"redis": None})

    assert calls == [tenant.id]  # exactly one call, not two
    assert len(_FakeWhatsAppClient.created) == 2


# --------------------------------------------------------------------------
# Free text vs. billable template + usage emission
# --------------------------------------------------------------------------


async def test_inside_24h_window_sends_free_text_without_usage_emit(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant, patient, appointment = await _make_scenario(
        db, lead=timedelta(hours=1), last_inbound_ago=timedelta(hours=2)
    )
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())
    usage_calls: list[dict] = []

    async def _fake_emit(**kwargs):
        usage_calls.append(kwargs)
        return True

    monkeypatch.setattr(reminders, "emit_usage_event", _fake_emit)

    await reminders.send_appointment_reminders({"redis": None})

    client = _FakeWhatsAppClient.created[0]
    assert client.sent[0][0] == "text"
    assert usage_calls == []


async def test_outside_window_sends_template_and_emits_usage(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, appointment = await _make_scenario(
        db, lead=timedelta(hours=1), last_inbound_ago=timedelta(hours=30)
    )
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())
    usage_calls: list[dict] = []

    async def _fake_emit(**kwargs):
        usage_calls.append(kwargs)
        return True

    monkeypatch.setattr(reminders, "emit_usage_event", _fake_emit)

    await reminders.send_appointment_reminders({"redis": None})

    client = _FakeWhatsAppClient.created[0]
    kind, to, template, lang, variables, button_payloads = client.sent[0]
    assert kind == "template"
    assert to == "5511999999"
    assert template == "appointment_reminder"
    assert lang == "pt_BR"
    assert len(variables) == 1 and "Consulta" in variables[0]
    assert button_payloads is None

    assert usage_calls == [
        {
            "tenant_id": str(tenant.id),
            "feature": "reminders",
            "amount": 1,
            "event_id": reminders._reminder_key("1h", appointment.id),
        }
    ]


async def test_no_prior_inbound_message_is_treated_as_outside_window(
    db, monkeypatch: pytest.MonkeyPatch
):
    """No conversation/message at all -> last_inbound_at is None -> template path."""
    tenant, patient, appointment = await _make_scenario(db, lead=timedelta(hours=1))
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})

    assert _FakeWhatsAppClient.created[0].sent[0][0] == "template"


async def test_usage_emission_failure_is_fail_open(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, appointment = await _make_scenario(db, lead=timedelta(hours=1))
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    async def _boom(**kwargs):
        raise RuntimeError("brain-api unreachable")

    monkeypatch.setattr(reminders, "emit_usage_event", _boom)

    # Must not raise: the send already happened, emission failure is logged only.
    await reminders.send_appointment_reminders({"redis": None})

    assert len(_FakeWhatsAppClient.created) == 1
    assert _FakeWhatsAppClient.created[0].sent[0][0] == "template"
    key = reminders._reminder_key("1h", appointment.id)
    assert await _ledger_count(db, key) == 1  # ledger still claimed - no resend next tick


# --------------------------------------------------------------------------
# Deposit-aware 3-button variant (PROMPT S3 section 2)
# --------------------------------------------------------------------------


class _FailFirstTemplateClient(_FakeWhatsAppClient):
    """The deposit-template attempt (the only send_template call that ever
    passes `button_payloads`) raises — simulating a not-yet-Meta-approved
    template; the plain-template fallback call (no button_payloads) succeeds
    normally, proving a deposit tenant never silently loses a reminder."""

    async def send_template(self, to, template, lang, variables, button_payloads=None):
        if button_payloads:
            raise RuntimeError("template not approved by Meta yet")
        return await super().send_template(to, template, lang, variables, button_payloads)


async def _seed_deposit(
    db,
    appointment,
    *,
    status: PixDepositStatus = PixDepositStatus.PAID,
) -> PixDeposit:
    async with db() as session:
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=appointment.tenant_id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            asaas_payment_id=f"pay-{uuid4()}",
            amount_cents=10000,
            percent_applied=30,
            status=status,
        )
        session.add(deposit)
        await session.commit()
        await session.refresh(deposit)
        return deposit


def _deposit_button_ids(appointment_id) -> list[str]:
    return [
        f"apptconfirm|{appointment_id}",
        f"apptresched|{appointment_id}",
        f"apptcancel|{appointment_id}",
    ]


async def test_paid_deposit_inside_window_sends_three_buttons(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant, patient, appointment = await _make_scenario(
        db, lead=timedelta(hours=1), last_inbound_ago=timedelta(hours=2)
    )
    await _seed_deposit(db, appointment, status=PixDepositStatus.PAID)
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})

    client = _FakeWhatsAppClient.created[0]
    assert len(client.sent) == 1
    kind, to, body, buttons = client.sent[0]
    assert kind == "buttons"
    assert to == "5511999999"
    ids = [bid for bid, _label in buttons]
    assert ids == _deposit_button_ids(appointment.id)
    labels = [label for _bid, label in buttons]
    assert labels == ["Confirmar", "Reagendar", "Cancelar"]


async def test_paid_deposit_outside_window_sends_deposit_template_with_buttons(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant, patient, appointment = await _make_scenario(
        db, lead=timedelta(hours=1), last_inbound_ago=timedelta(hours=30)
    )
    await _seed_deposit(db, appointment, status=PixDepositStatus.PAID)
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())
    usage_calls: list[dict] = []

    async def _fake_emit(**kwargs):
        usage_calls.append(kwargs)
        return True

    monkeypatch.setattr(reminders, "emit_usage_event", _fake_emit)

    await reminders.send_appointment_reminders({"redis": None})

    client = _FakeWhatsAppClient.created[0]
    assert len(client.sent) == 1
    kind, to, template, lang, variables, button_payloads = client.sent[0]
    assert kind == "template"
    assert template == "appointment_reminder_deposit"
    assert button_payloads == _deposit_button_ids(appointment.id)
    assert usage_calls == [
        {
            "tenant_id": str(tenant.id),
            "feature": "reminders",
            "amount": 1,
            "event_id": reminders._reminder_key("1h", appointment.id),
        }
    ]


async def test_deposit_template_send_failure_falls_back_to_plain_template(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant, patient, appointment = await _make_scenario(
        db, lead=timedelta(hours=1), last_inbound_ago=timedelta(hours=30)
    )
    await _seed_deposit(db, appointment, status=PixDepositStatus.PAID)
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())
    monkeypatch.setattr(reminders, "WhatsAppClient", _FailFirstTemplateClient)
    usage_calls: list[dict] = []

    async def _fake_emit(**kwargs):
        usage_calls.append(kwargs)
        return True

    monkeypatch.setattr(reminders, "emit_usage_event", _fake_emit)

    # Must not raise — the deposit-template attempt fails internally and the
    # plain-template fallback takes over.
    await reminders.send_appointment_reminders({"redis": None})

    # _FakeWhatsAppClient.__init__ records every instance (subclasses
    # included) onto the PARENT class's list — see `_fakes`'s reset above.
    client = _FakeWhatsAppClient.created[0]
    assert len(client.sent) == 1  # only the successful fallback send is recorded
    kind, to, template, lang, variables, button_payloads = client.sent[0]
    assert kind == "template"
    assert template == "appointment_reminder"  # plain REMINDER_TEMPLATE_NAME
    assert button_payloads is None

    # Usage is still emitted exactly once, for the fallback send that actually delivered.
    assert usage_calls == [
        {
            "tenant_id": str(tenant.id),
            "feature": "reminders",
            "amount": 1,
            "event_id": reminders._reminder_key("1h", appointment.id),
        }
    ]


@pytest.mark.parametrize(
    "deposit_status",
    [
        PixDepositStatus.AWAITING,
        PixDepositStatus.CANCELLED_REFUNDED,
        PixDepositStatus.CANCELLED_RETAINED,
        PixDepositStatus.NO_SHOW_RETAINED,
        PixDepositStatus.EXPIRED,
    ],
)
async def test_non_paid_deposit_gets_todays_plain_behavior(
    db, monkeypatch: pytest.MonkeyPatch, deposit_status
):
    """Every deposit state OTHER than PAID (including AWAITING and every
    resolved/refunded/retained state) must behave EXACTLY like no deposit at
    all — no buttons, ever."""
    tenant, patient, appointment = await _make_scenario(
        db, lead=timedelta(hours=1), last_inbound_ago=timedelta(hours=2)
    )
    await _seed_deposit(db, appointment, status=deposit_status)
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})

    client = _FakeWhatsAppClient.created[0]
    assert client.sent[0][0] == "text"  # inside-window free text, not buttons


async def test_no_deposit_at_all_gets_todays_plain_behavior(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, appointment = await _make_scenario(
        db, lead=timedelta(hours=1), last_inbound_ago=timedelta(hours=2)
    )
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())

    await reminders.send_appointment_reminders({"redis": None})

    client = _FakeWhatsAppClient.created[0]
    assert client.sent[0][0] == "text"
