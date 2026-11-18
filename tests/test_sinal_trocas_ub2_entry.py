"""TASK-085 R / UB2 - D-R14 end to end: the e-mail's link opens the Portal conversation and the
FIRST message is the price action (mapa-u §2.3, §7.16, §7.17).

`process_brain_message_enter` (the job the Portal fires on every entry, with
`entry_context={"source": "reminder_link", "reminder_id": <row>}`) recognizes a `price_change`
row and calls `workers/shared/price_change_opening.send_price_change_opening`, even with the
reminders v2 switch OFF and right after recent activity. Characterization: an ordinary reminder
link keeps today's rules; a human-owned conversation stays silent.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    PixDeposit,
    PixDepositStatus,
    Tenant,
)
from secretaria.models.appointment_reminder import (
    REMINDER_CHANNEL_EMAIL,
    REMINDER_KIND_PRICE_CHANGE,
    REMINDER_STATUS_SENT,
    AppointmentReminder,
)
from secretaria.models.deposit_adjustment import (
    KIND_INCREASE,
    MODE_SUSPEND,
    STATUS_AWAITING_PAYMENT,
    STATUS_REFUND_REQUESTED,
    DepositAdjustment,
)
from secretaria.services import reminder_delivery, tenant_config as cfg
from secretaria.services.brain_patients import PatientEmailResult
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome
from secretaria.services.payments import (
    accounts,
    deposit_lifecycle,
    notify,
    price_change,
    registry,
)
from secretaria.services.payments.provider import ChargeState, CreatedCharge, PaymentStatus
from secretaria.workers.portal.inbound import process_brain_message_inbound
from secretaria.workers.portal.open import process_brain_message_enter
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, set_conversation, wire
from tests._reminders_v2 import add_reminder, outbound_messages, seed_world
from tests.payments_support import FakeProvider, FakeRedis

CATALOG_200 = [{"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 200,00"}]


def _catalog(price: str) -> list[dict]:
    return [dict(CATALOG_200[0], price=price)]


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider(name="asaas", state=ChargeState(PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(
        provider_charge_id="pay_diff", copy_paste="COPYDIFF", provider_customer_id="cus_1"
    )
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


@pytest.fixture
def portal(db, monkeypatch):  # noqa: F811
    wire(monkeypatch, db)
    for module in (price_change, deposit_lifecycle, notify):
        monkeypatch.setattr(module, "async_session_factory", db)
    return db


async def seed(
    db,  # noqa: F811
    *,
    v2=False,
    awaiting=False,
    days=5,
    last_inbound_at=None,
    email=None,
    deposit_created_at=None,
):
    """A Portal patient who just talked (the quiet window would silence an ordinary entry),
    reminders v2 OFF (the price link must not depend on it), a sinal of R$ 60 at Consulta
    R$ 200, a connected payment account."""
    now = datetime.now(UTC)
    world = await seed_world(
        db,
        v2=v2,
        channel=CHANNEL_BRAIN_MESSAGE,
        phone_number_id=None,
        email=email,
        start_at=now + timedelta(days=days),
        # Inside the quiet window (an ordinary entry would stay silent); a minute back so the
        # outbound rows (stamped by the database, to the second) sort after it.
        last_inbound_at=last_inbound_at or now - timedelta(minutes=1),
    )
    await consent(db, world)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.pix_deposit_enabled = True
        tenant.pix_deposit_percent = 30
        tenant.payment_provider = "asaas"
        tenant.appointment_types = CATALOG_200
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
        row.status = "connected"
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=tenant.id,
            appointment_id=world.appointment.id,
            patient_id=world.patient.id,
            provider="asaas",
            provider_charge_id="pay_sinal",
            amount_cents=6000,
            percent_applied=30,
            status=PixDepositStatus.AWAITING if awaiting else PixDepositStatus.PAID,
            created_at=deposit_created_at or now - timedelta(days=3),
        )
        session.add(deposit)
        await session.commit()
    return world, deposit


async def reprice(db, world, price: str) -> None:  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.appointment_types = _catalog(price)
        await session.commit()


async def add_notice(db, world) -> UUID:  # noqa: F811
    async with db() as session:
        row = AppointmentReminder(
            id=uuid4(),
            tenant_id=world.tenant.id,
            appointment_id=world.appointment.id,
            patient_id=world.patient.id,
            kind=REMINDER_KIND_PRICE_CHANGE,
            appointment_start_at=world.start_at,
            due_at=datetime.now(UTC),
            status=REMINDER_STATUS_SENT,
            channel=REMINDER_CHANNEL_EMAIL,
            with_prompt=False,
            sent_at=datetime.now(UTC),
        )
        session.add(row)
        await session.commit()
        return row.id


async def enter(world, reminder_id, ctx=None) -> None:
    await process_brain_message_enter(
        ctx if ctx is not None else {"redis": FakeRedis()},
        str(world.tenant.id),
        world.patient.external_id,
        entry_context={"source": "reminder_link", "reminder_id": str(reminder_id)},
    )


async def bodies(db, world) -> list[str]:  # noqa: F811
    return [m.body for m in await outbound_messages(db, world.conversation.id)]


async def test_the_link_opens_with_the_pix_of_the_difference_even_with_reminders_off(
    portal, provider
):
    world, _deposit = await seed(portal)
    rid = await add_notice(portal, world)
    await reprice(portal, world, "R$ 300,00")

    await enter(world, rid)

    [body] = await bodies(portal, world)
    assert body.startswith("O valor da sua consulta (Consulta, ")
    assert "Falta pagar a diferença do sinal: R$ 30,00." in body and "COPYDIFF" in body
    assert provider.called("create_pix_charge") == 1


async def test_reloads_stay_silent_and_a_later_visit_points_at_the_pix_above(portal, provider):
    """Fix round 1 (review M1): the URL keeps `lembrete=` - every reload / tab switch re-sends
    the entry. Answered notice + the bot spoke last, after it -> silence. Once the patient
    wrote something, opening the link again points at the Pix above (never a 2nd charge)."""
    world, _deposit = await seed(portal)
    rid = await add_notice(portal, world)
    await reprice(portal, world, "R$ 300,00")
    await enter(world, rid)

    await enter(world, rid)  # F5
    await enter(world, rid)  # tab switch secretarIA <-> PreCheck

    assert len(await bodies(portal, world)) == 1
    async with portal() as session:
        session.add(
            Message(
                conversation_id=world.conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                body="oi",
                created_at=datetime.now(UTC) + timedelta(seconds=2),
            )
        )
        await session.commit()

    await enter(world, rid)  # the e-mail link clicked again, later

    sent = await bodies(portal, world)
    assert len(sent) == 2
    assert sent[-1].startswith("O Pix da diferença já está na mensagem acima e vale até ")
    assert provider.called("create_pix_charge") == 1


async def test_a_drop_asks_on_the_portal_and_the_tap_is_answered(portal, provider):
    world, _deposit = await seed(portal, days=10)
    rid = await add_notice(portal, world)
    await reprice(portal, world, "R$ 100,00")

    await enter(world, rid)

    [question] = await outbound_messages(portal, world.conversation.id)
    assert question.body.startswith("O valor da sua consulta (Consulta) baixou")
    options = question.interactive["options"]
    assert [o["title"] for o in options] == ["Sim, quero de volta", "Não, abater no valor"]
    assert options[0]["id"].startswith("svcdiffyes|")
    await process_brain_message_inbound(
        {},
        str(world.tenant.id),
        world.patient.external_id,
        text=options[0]["title"],
        interactive_reply_id=options[0]["id"],
    )
    async with portal() as session:
        [adjustment] = (await session.scalars(select(DepositAdjustment))).all()
    assert adjustment.status == STATUS_REFUND_REQUESTED
    answer = (await bodies(portal, world))[-1]
    assert "a clínica vai entrar em contato para combinar a devolução" in answer


async def test_an_unpaid_sinal_opens_with_the_new_pix(portal, provider):
    world, _deposit = await seed(portal, awaiting=True)
    rid = await add_notice(portal, world)
    await reprice(portal, world, "R$ 300,00")

    await enter(world, rid)

    [body] = await bodies(portal, world)
    assert body.startswith(deposit_lifecycle.PRICE_REQUOTE_OPENING_TEXT)
    assert "R$ 90,00" in body and "COPYDIFF" in body
    assert provider.called("cancel_charge") == 1 and provider.called("create_pix_charge") == 1


async def test_a_cancelled_consultation_says_nothing_changes(portal, provider):
    world, _deposit = await seed(portal)
    rid = await add_notice(portal, world)
    await reprice(portal, world, "R$ 300,00")
    async with portal() as session:
        row = await session.get(Appointment, world.appointment.id)
        row.status = AppointmentStatus.CANCELLED
        await session.commit()

    await enter(world, rid)

    assert await bodies(portal, world) == [price_change.PRICE_CLOSED_TEXT]
    assert provider.calls == []


async def test_a_broken_link_falls_back_to_the_usual_opening(portal, provider):
    """No row behind the id (the e-mail went out, its row was never written): an ordinary
    entry - here, after a quiet day, the usual opening (the consultation's card)."""
    world, _deposit = await seed(
        portal, v2=True, last_inbound_at=datetime.now(UTC) - timedelta(days=1)
    )

    await enter(world, uuid4())

    [body] = await bodies(portal, world)
    assert body.startswith("LEMBRE-SE:")
    assert provider.calls == []


async def test_a_suspended_consultation_gets_the_usual_opening_not_the_price(portal, provider):
    world, deposit = await seed(portal)
    async with portal() as session:
        session.add(
            DepositAdjustment(
                id=uuid4(),
                tenant_id=world.tenant.id,
                deposit_id=deposit.id,
                appointment_id=world.appointment.id,
                kind=KIND_INCREASE,
                mode=MODE_SUSPEND,
                status=STATUS_AWAITING_PAYMENT,
                old_service="Consulta",
                new_service="Exame",
                diff_cents=3000,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
        await session.commit()
    rid = await add_notice(portal, world)
    await reprice(portal, world, "R$ 300,00")

    await enter(world, rid)

    sent = await bodies(portal, world)
    assert len(sent) == 1 and not sent[0].startswith("O valor da sua consulta")
    assert provider.calls == []


async def test_a_human_owned_conversation_stays_silent(portal, provider):
    world, _deposit = await seed(portal)
    rid = await add_notice(portal, world)
    await reprice(portal, world, "R$ 300,00")
    await set_conversation(portal, world, handover_state=HandoverState.HUMAN_ACTIVE)

    await enter(world, rid)

    assert await bodies(portal, world) == [] and provider.calls == []
    async with portal() as session:
        assert (await session.get(AppointmentReminder, rid)).answered_at is None


async def test_an_ordinary_reminder_link_keeps_the_reminders_off_policy(portal, provider):
    """Characterization (test_reminder_link_entry): only the price notice bypasses the switch."""
    world, _deposit = await seed(portal)
    rid = await add_reminder(portal, world, kind="day", due_at=datetime.now(UTC))

    await enter(world, rid)

    assert await bodies(portal, world) == []


async def test_the_e_mail_link_leads_to_the_action(portal, provider, monkeypatch):
    """E-mail -> link -> Portal: the sweep mails the patient a link to its row; entering through
    that link makes the first message the action with the price of the OPENING."""
    now = datetime.now(UTC)
    world, _deposit = await seed(portal, email="paciente@example.test")
    mails: list = []

    async def _send(to, template, variables):
        mails.append(variables["link_line"])
        return EmailOutcome.SENT

    monkeypatch.setattr(price_change, "send_transactional_email_result", _send)
    monkeypatch.setattr(
        price_change,
        "portal_conversation_link",
        lambda tenant_id, reminder_id: f"https://portal.test/?lembrete={reminder_id}",
    )

    async def _no_brain_api(tenant_id, external_id):
        return PatientEmailResult()

    monkeypatch.setattr(reminder_delivery, "fetch_patient_email_result", _no_brain_api)
    await reprice(portal, world, "R$ 300,00")
    assert (
        await price_change.sweep_price_changes(
            world.tenant.id, changed_at=now - timedelta(minutes=1), now=now, redis=FakeRedis()
        )
        == 1
    )
    [link] = mails
    reminder_id = UUID(link.rsplit("=", 1)[1])
    # The clinic edits once more before the patient opens it: the OPENING's price counts.
    await reprice(portal, world, "R$ 400,00")  # sinal R$ 120: + R$ 60

    await enter(world, reminder_id)

    [body] = await bodies(portal, world)
    assert "Falta pagar a diferença do sinal: R$ 60,00." in body
    assert provider.called("create_pix_charge") == 1
