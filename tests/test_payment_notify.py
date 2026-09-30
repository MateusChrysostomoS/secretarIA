"""Channel-neutral delivery of the money messages (services/payments/notify.py, spec section 11).

The Portal patient has no phone (`wa_id = None` by design): the Pix request, the paid notice and
the expiry notice must reach them as Portal messages, and a WhatsApp patient must keep getting
exactly what they got, through the clinic's own client. A failed delivery is RAISED, never
swallowed, because in `before` timing a Pix nobody received must free the slot.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import literal_column, select

from secretaria.models import Conversation, Message, Patient, Tenant
from secretaria.models.message import MessageDirection, MessageSender
from secretaria.services.payments import deposit_lifecycle, notify
from secretaria.services.whatsapp import TenantWhatsAppCredentialMissing
from tests.payments_support import make_sessionmaker


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(notify, "async_session_factory", maker)
    yield maker
    await engine.dispose()


class FakeWhatsApp:
    """Mirrors `WhatsAppClient`: the CALLER records the outbound row (persists_outbound False)."""

    persists_outbound = False
    built: list = []
    fail_on: str | None = None
    missing_credential = False

    def __init__(self):
        self.sent: list[tuple] = []
        FakeWhatsApp.built.append(self)

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        if cls.missing_credential:
            raise TenantWhatsAppCredentialMissing(tenant.id, ("access_token",))
        return cls()

    async def send_text_message(self, to, body):
        if self.fail_on == "text":
            raise RuntimeError("meta down")
        self.sent.append(("text", to, body))
        return {"messages": [{"id": f"wamid.{len(self.sent)}"}]}

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", to, body, buttons))
        return {"messages": [{"id": f"wamid.{len(self.sent)}"}]}


@pytest.fixture(autouse=True)
def _reset():
    FakeWhatsApp.built = []
    FakeWhatsApp.fail_on = None
    FakeWhatsApp.missing_credential = False


async def _world(session, *, channel="whatsapp"):
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    if channel == "whatsapp":
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
    else:
        patient = Patient(
            id=uuid4(),
            tenant_id=tenant.id,
            wa_id=None,
            channel="brain_message",
            external_id=str(uuid4()),
            name="Maria",
        )
    session.add_all([tenant, patient])
    await session.flush()
    conversation = Conversation(id=uuid4(), tenant_id=tenant.id, patient_id=patient.id)
    session.add(conversation)
    await session.commit()
    return tenant, patient, conversation


async def _rows(maker, conversation):
    async with maker() as session:
        return list(
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(literal_column("rowid"))  # SQLite ties created_at to the second
            )
        )


REQUEST = [
    notify.Text("Para reservar Consulta ..."),
    notify.Text("00020126COPIAECOLA"),
    notify.Buttons("Estou aguardando o pagamento.", deposit_lifecycle.DEPOSIT_WAIT_BUTTONS),
]


async def test_a_portal_patient_with_no_phone_receives_the_pix_as_portal_messages(db):
    async with db() as session:
        tenant, patient, conversation = await _world(session, channel="brain_message")
        assert patient.wa_id is None
        await notify.deliver(session, tenant, patient, REQUEST, whatsapp_cls=FakeWhatsApp)
    rows = await _rows(db, conversation)
    assert [row.body for row in rows[:2]] == ["Para reservar Consulta ...", "00020126COPIAECOLA"]
    assert all(
        row.direction == MessageDirection.OUTBOUND and row.sender == MessageSender.BOT
        for row in rows
    )
    assert rows[2].interactive["kind"] == "buttons"
    assert [o["title"] for o in rows[2].interactive["options"]] == [
        "Já paguei",
        "Trocar horário",
        "Cancelar reserva",
    ]
    assert FakeWhatsApp.built == []  # the Graph API was never involved


async def test_a_whatsapp_patient_is_reached_by_wa_id_and_the_history_is_recorded(db):
    async with db() as session:
        tenant, patient, conversation = await _world(session)
        await notify.deliver(session, tenant, patient, REQUEST, whatsapp_cls=FakeWhatsApp)
    (client,) = FakeWhatsApp.built
    assert [call[:2] for call in client.sent] == [
        ("text", "5511999999999"),
        ("text", "5511999999999"),
        ("buttons", "5511999999999"),
    ]
    rows = await _rows(db, conversation)
    assert [row.wam_id for row in rows] == ["wamid.1", "wamid.2", "wamid.3"]
    assert rows[2].interactive["kind"] == "buttons"


async def test_a_clinic_without_a_whatsapp_credential_fails_closed_and_writes_nothing(db):
    FakeWhatsApp.missing_credential = True
    async with db() as session:
        tenant, patient, conversation = await _world(session)
        with pytest.raises(notify.DeliveryError) as raised:
            await notify.deliver(session, tenant, patient, REQUEST, whatsapp_cls=FakeWhatsApp)
    assert raised.value.code == "no_whatsapp_credential"
    assert await _rows(db, conversation) == []


async def test_a_whatsapp_patient_without_a_number_cannot_be_reached(db):
    async with db() as session:
        tenant, patient, _conversation = await _world(session)
        patient.wa_id = None
        with pytest.raises(notify.DeliveryError) as raised:
            await notify.deliver(session, tenant, patient, REQUEST, whatsapp_cls=FakeWhatsApp)
    assert raised.value.code == "no_address"


async def test_a_portal_patient_without_a_conversation_cannot_be_reached(db):
    async with db() as session:
        tenant, patient, conversation = await _world(session, channel="brain_message")
        await session.delete(conversation)
        await session.commit()
        with pytest.raises(notify.DeliveryError) as raised:
            await notify.deliver(session, tenant, patient, REQUEST)
    assert raised.value.code == "no_conversation"


async def test_a_failed_send_is_raised_and_the_bubbles_before_it_are_kept(db):
    async with db() as session:
        tenant, patient, conversation = await _world(session)
        FakeWhatsApp.fail_on = None

        original = FakeWhatsApp.send_text_message
        calls = {"n": 0}

        async def flaky(self, to, body):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("meta down")
            return await original(self, to, body)

        FakeWhatsApp.send_text_message = flaky
        try:
            with pytest.raises(notify.DeliveryError) as raised:
                await notify.deliver(session, tenant, patient, REQUEST, whatsapp_cls=FakeWhatsApp)
        finally:
            FakeWhatsApp.send_text_message = original
    assert raised.value.code == "send_failed"
    assert [row.body for row in await _rows(db, conversation)] == ["Para reservar Consulta ..."]


async def test_the_delivery_error_never_carries_a_phone_or_a_body(db):
    async with db() as session:
        tenant, patient, _conversation = await _world(session)
        FakeWhatsApp.fail_on = "text"
        with pytest.raises(notify.DeliveryError) as raised:
            await notify.deliver(session, tenant, patient, REQUEST, whatsapp_cls=FakeWhatsApp)
    assert "5511999999999" not in str(raised.value)
    assert "Para reservar" not in str(raised.value)


async def test_the_after_flow_now_reaches_a_portal_patient_too(db, monkeypatch):
    """Before this task `_send_deposit_request` built a WhatsApp client for `wa_id=None`."""
    monkeypatch.setattr(deposit_lifecycle, "WhatsAppClient", FakeWhatsApp)
    async with db() as session:
        tenant, patient, conversation = await _world(session, channel="brain_message")
        from secretaria.models import Appointment

        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id="evt",
            appointment_type="Consulta",
        )
        session.add(appointment)
        await session.commit()
        await deposit_lifecycle._send_deposit_request(
            session, tenant, appointment, patient, 6000, "COPYPASTE", waba_token="wt"
        )
    rows = await _rows(db, conversation)
    assert len(rows) == 1 and "COPYPASTE" in rows[0].body
    assert FakeWhatsApp.built == []


def _copy_lines() -> list[str]:
    names = [n for n in dir(deposit_lifecycle) if n.startswith("BEFORE_") or n.startswith("LABEL_")]
    lines = [getattr(deposit_lifecycle, n) for n in names]
    lines.append(deposit_lifecycle.before_late_reopened_text())
    lines.append(
        deposit_lifecycle.before_request_text(
            "Clinic",
            service="Consulta",
            when_text="05/10/2026 às 10:00",
            amount_cents=6000,
            minutes=30,
            until_text="10:30",
            payment_note="nota",
        )
    )
    lines.append(deposit_lifecycle.before_wait_card_text("10:30"))
    return [line for line in lines if isinstance(line, str)]


@pytest.mark.parametrize("word", ["estorn", "reembols", "devolv", "devolu", "refund"])
def test_no_before_copy_promises_or_mentions_a_refund(word):
    for line in _copy_lines():
        assert word not in line.lower(), line


def test_the_late_taken_notice_says_the_clinic_will_call_and_promises_no_money_movement():
    text = deposit_lifecycle.BEFORE_LATE_TAKEN_TEXT
    assert "A clínica vai falar com você" in text
    assert "resolver o valor" in text
