"""How a `before` reservation coexists with what already reads appointments (spec sections 15, 16).

The design choice of TASK-017 (approach A) is that NOTHING exists in `appointments` until the
deposit is paid. This file proves the consequences the other subsystems rely on: metering never
bills a reservation and bills the promoted consultation exactly once (replays included), the
reminder sweep ignores a reservation and reminds the promoted consultation, and the whole
timeline - hold, expiry, late payment - never leaves the agenda in two states.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from secretaria.models import (
    Appointment,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    PixDeposit,
    Professional,
)
from secretaria.models.pix_deposit import FULFILLMENT_PROMOTING, PixDepositStatus
from secretaria.plugins import reminders
from secretaria.services import booking_hold as holds, booking_promotion
from secretaria.services.entitlements_client import EntitlementSummary
from secretaria.services.payments import (
    booking_charge,
    fulfillment,
    jobs,
    notify,
    registry,
)
from secretaria.services.payments.deposit_gate import DepositQuote
from secretaria.services.payments.provider import ChargeState, PaymentStatus
from secretaria.workers import onboarding_cron
from tests.before_support import make_world, new_charge
from tests.payments_support import FakeProvider, make_sessionmaker, recording_enqueue

QUOTE = DepositQuote(
    "required", "required", amount_cents=6000, provider="asaas", ttl_minutes=30,
    service_label="Consulta",
)
MONTH_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    for module in (holds, booking_charge, notify, reminders):
        monkeypatch.setattr(module, "async_session_factory", maker)
    yield maker
    await engine.dispose()


class Calendar:
    def __init__(self) -> None:
        self.created: list = []

    async def check_availability(self, start, end, max_results=50):
        return []

    async def create_event(self, start, end, summary, description="", *, event_id=None):
        self.created.append(event_id)
        return {"id": event_id, "htmlLink": "https://cal/x"}


@pytest.fixture
def env(monkeypatch):
    provider = FakeProvider()
    provider.created_charge = new_charge()
    provider.state = ChargeState(PaymentStatus.PENDING)
    calendar = Calendar()

    async def calendar_for_booking(session, tenant, professional_id):
        return calendar

    async def deliver(session, tenant, patient, messages, **kwargs):
        return None

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", calendar_for_booking)
    monkeypatch.setattr(notify, "deliver", deliver)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", provider)
    return type("Env", (), {"provider": provider, "calendar": calendar})


async def _reserve(db, world, *, start, professional_id=None):
    started = await booking_charge.begin_payment(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        quote=QUOTE,
        professional_id=professional_id,
        appointment_type="Consulta",
        insurance=None,
        attendee_name=None,
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )
    return started.deposit_id


async def _pay(db, deposit_id):
    async with db() as session:
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.id == deposit_id)
            .values(
                status=PixDepositStatus.PAID,
                paid_at=datetime.now(UTC),
                paid_amount_cents=6000,
                fulfillment_status=FULFILLMENT_PROMOTING,
            )
        )
        await session.commit()


async def _fulfill(db, deposit_id):
    async with db() as session:
        return await fulfillment.fulfill_paid_charge(session, deposit_id=deposit_id)


# ---- metering (onboarding_cron.py: one billable event per (professional, patient, month)) ---


def _capture_usage(monkeypatch):
    calls: list[dict] = []

    async def fake(*, tenant_id, feature, amount, event_id):
        calls.append({"feature": feature, "amount": amount, "event_id": event_id})
        return True

    monkeypatch.setattr(onboarding_cron, "emit_usage_event", fake)
    return calls


async def test_metering_never_bills_a_reservation_and_bills_the_promotion_exactly_once(
    db, env, monkeypatch
):
    async with db() as session:
        world = await make_world(session)
        professional = Professional(
            id=uuid4(), tenant_id=world.tenant_id, name="Dr A", is_active=True
        )
        session.add(professional)
        await session.commit()
    usage = _capture_usage(monkeypatch)
    deposit_id = await _reserve(
        db, world, start=datetime(2026, 7, 20, 13, 0, tzinfo=UTC), professional_id=professional.id
    )

    async with db() as session:  # a live reservation: no Appointment, so nothing to bill
        assert list(await session.scalars(select(Appointment))) == []
        counts = await onboarding_cron._tally_tenant_month(session, world.tenant, MONTH_NOW)
    assert counts["emitted"] == 0 and usage == []

    await _pay(db, deposit_id)
    assert await _fulfill(db, deposit_id) == "promoted"
    assert await _fulfill(db, deposit_id) == "noop"  # a replay creates nothing to bill twice
    async with db() as session:
        counts = await onboarding_cron._tally_tenant_month(session, world.tenant, MONTH_NOW)
    assert counts["emitted"] == 1
    assert [call["feature"] for call in usage] == ["billable_patients"]


async def test_an_expired_or_abandoned_reservation_never_reaches_metering(db, env, monkeypatch):
    async with db() as session:
        world = await make_world(session)
    usage = _capture_usage(monkeypatch)
    deposit_id = await _reserve(db, world, start=datetime(2026, 7, 20, 13, 0, tzinfo=UTC))
    async with db() as session:
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.id == deposit_id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()
    sent, enqueue = recording_enqueue()
    async with db() as session:
        report = await jobs.expire_due_charges(session, now=datetime.now(UTC), enqueue=enqueue)
    assert report.expired == 1
    async with db() as session:
        counts = await onboarding_cron._tally_tenant_month(session, world.tenant, MONTH_NOW)
    assert counts["emitted"] == 0 and usage == []


# ---- reminders (plugins/reminders.py reads LIVE appointments only) ---------------------------


class WhatsApp:
    persists_outbound = False
    sent: list[tuple] = []

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        WhatsApp.sent.append(("text", to, body))
        return {"messages": [{"id": "wamid.1"}]}

    async def send_template(self, to, template, lang, variables, button_payloads=None):
        WhatsApp.sent.append(("template", to, template))
        return {"messages": [{"id": "wamid.1"}]}

    async def send_buttons(self, to, body, buttons):
        WhatsApp.sent.append(("buttons", to, body))
        return {"messages": [{"id": "wamid.1"}]}


async def test_the_reminder_sweep_ignores_a_reservation_and_reminds_the_promoted_consultation(
    db, env, monkeypatch
):
    WhatsApp.sent = []

    async def token(session, tenant_id):
        return "wt"

    async def entitlements(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id), status="active", active=True, secretaria_enabled=True,
            plan="bronze", secretaria_tier="basico", addons={}, limits={},
        )

    monkeypatch.setattr(reminders, "WhatsAppClient", WhatsApp)
    monkeypatch.setattr(reminders, "get_waba_token", token)
    monkeypatch.setattr(reminders, "get_entitlements", entitlements)
    async with db() as session:
        world = await make_world(session)
        world.tenant.is_active = True
        session.add(
            Message(
                conversation_id=world.conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                body="oi",
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await session.commit()
    deposit_id = await _reserve(db, world, start=datetime.now(UTC) + timedelta(hours=24))

    await reminders.send_appointment_reminders({"redis": None})
    assert WhatsApp.sent == []  # a reservation is not a consultation: nothing to remind, no crash

    await _pay(db, deposit_id)
    assert await _fulfill(db, deposit_id) == "promoted"
    await reminders.send_appointment_reminders({"redis": None})
    assert len(WhatsApp.sent) == 1  # the promoted consultation is reminded, once
    await reminders.send_appointment_reminders({"redis": None})
    assert len(WhatsApp.sent) == 1


# ---- the whole timeline ----------------------------------------------------------------------


async def test_hold_then_expiry_then_a_late_payment_ends_in_exactly_one_consultation(db, env):
    """The Review Focus race, sequentially: the Pix window ends and the slot is released; the
    money arrives afterwards; the slot is still free, so the reservation is reopened."""
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _reserve(db, world, start=datetime(2030, 3, 4, 13, 0, tzinfo=UTC))
    async with db() as session:
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.id == deposit_id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()
    sent, enqueue = recording_enqueue()
    async with db() as session:
        assert (await jobs.expire_due_charges(session, now=datetime.now(UTC), enqueue=enqueue)
                ).expired == 1
    async with db() as session:
        assert list(await session.scalars(select(Conversation.id))) != []
        assert list(await session.scalars(select(holds.BookingHold))) == []  # the slot is free

    from secretaria.services.payments import event_processor
    from tests.payments_support import make_event

    env.provider.state = ChargeState(
        PaymentStatus.PAID, paid_amount_cents=6000, external_reference=str(deposit_id)
    )
    async with db() as session:
        deposit = await session.get(PixDeposit, deposit_id)
        tenant = await session.get(type(world.tenant), world.tenant_id)
        from secretaria.services import tenant_config as cfg

        account = await cfg.get_payment_account(session, world.tenant_id, "asaas")
        result = await event_processor.apply_normalized_event(
            session,
            tenant=tenant,
            account=account,
            provider=env.provider,
            event=make_event(
                charge_id=deposit.provider_charge_id,
                reference=str(deposit_id),
                amount=6000,
            ),
        )
        await session.commit()
    assert result.outcome == "paid_before" and result.fulfill_deposit_id == deposit_id

    assert await _fulfill(db, deposit_id) == "promoted_late"
    async with db() as session:
        appointments = list(await session.scalars(select(Appointment)))
    assert len(appointments) == 1 and len(env.calendar.created) == 1
