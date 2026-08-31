"""Tests for `/dangerously-remove-context` — the destructive reset (PROMPT_FIX_18).

The behaviour that used to hide behind `/menu` is KEPT, renamed to a literal
nobody types by accident, and fixed on the two points that made it dangerous
beyond the accidental trigger:

  1. **Orphans.** `appointments.google_event_id` is NOT NULL, so deleting an
     appointment row would leave the database saying "no consultation" while
     Google Calendar still shows one — the doctor turning up for a slot the
     system forgot. The reset therefore PRESERVES appointments and merely
     detaches them (`patient_id`/`conversation_id` -> NULL), which also saves
     the PixDeposit row that `ON DELETE CASCADE` would otherwise have taken
     with it, money and all. It never calls Google or Asaas.
  2. **Audit.** Every invocation writes a durable, sanitized `analytics_events`
     row: tenant, conversation, per-type counts, server timestamp — and never
     the phone number or any deleted content.

Same in-memory-sqlite pattern as tests/test_menu_command.py.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    AnalyticsEvent,
    Appointment,
    AppointmentStatus,
    ConsentEvent,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Tenant,
)
from secretaria.schemas.webhook import WebhookValue  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
COMMAND = "/dangerously-remove-context"


class _FakeWhatsAppClient:
    sent: list[tuple] = []

    def __init__(self, **kwargs):
        pass

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        _FakeWhatsAppClient.sent.append(("text", to, body))
        return {"messages": [{"id": f"wamid.out.{len(_FakeWhatsAppClient.sent)}"}]}

    async def send_buttons(self, to, body, buttons):
        _FakeWhatsAppClient.sent.append(("buttons", to, body, buttons))
        return {"messages": [{"id": f"wamid.out.{len(_FakeWhatsAppClient.sent)}"}]}


class _RecordingLogger:
    """Captures what the CALL SITE passes to the logger.

    Deliberately upstream of `core.logging.redact_secrets`: the redactor is a
    backstop (tested separately), but personal data must not be handed to the
    logger in the first place — "remover campos perigosos na origem".
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def _record(self, event, **kwargs):
        self.records.append((event, kwargs))

    info = warning = error = debug = _record

    def all_values(self) -> list[str]:
        return [str(v) for _event, fields in self.records for v in fields.values()]

    def all_keys(self) -> set[str]:
        return {k for _event, fields in self.records for k in fields}


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
def _wire(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    _FakeWhatsAppClient.sent = []
    monkeypatch.setattr(tasks, "WhatsAppClient", _FakeWhatsAppClient)

    async def _fake_get_waba_token(session, tenant_id):
        return "tenant-waba-token"

    monkeypatch.setattr(tasks, "get_waba_token", _fake_get_waba_token)

    async def _entitled(tenant_id, redis):
        return SimpleNamespace(active=True, secretaria_enabled=True, status="active")

    monkeypatch.setattr(tasks, "get_entitlements", _entitled)

    # No external system may be touched by a chat command.
    class _NoCalendar:
        def __init__(self, *args, **kwargs):
            raise AssertionError("the reset must never call Google Calendar")

        @classmethod
        def from_tenant_config(cls, *args, **kwargs):
            raise AssertionError("the reset must never call Google Calendar")

    monkeypatch.setattr(tasks, "CalendarService", _NoCalendar)
    yield


def _value(body: str, *, wam_id: str) -> WebhookValue:
    return WebhookValue.model_validate(
        {
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "contacts": [{"wa_id": WA_ID, "profile": {"name": "Maria"}}],
            "messages": [{"id": wam_id, "from": WA_ID, "type": "text", "text": {"body": body}}],
        }
    )


async def _seed(db, *, with_appointment: bool = True) -> dict:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            greeting_message="Olá! Bem-vindo à Clínica.",
            initial_flows={},
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id=WA_ID, name="Maria")
        session.add_all([tenant, patient])
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        for index in range(3):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND,
                    sender=MessageSender.PATIENT,
                    wam_id=f"wamid.history.{index}",
                    body="conteudo clinico sensivel",
                )
            )
        session.add(
            ConsentEvent(
                tenant_id=tenant.id, wa_id=WA_ID, kind="first_contact_service", legal_basis="test"
            )
        )
        seeded = {"tenant": tenant, "patient": patient, "conversation": conversation}
        if with_appointment:
            start_at = datetime.now(UTC) + timedelta(days=3)
            appointment = Appointment(
                id=uuid4(),
                tenant_id=tenant.id,
                patient_id=patient.id,
                conversation_id=conversation.id,
                google_event_id="evt-live-on-google",
                appointment_type="Consulta Geral",
                start_at=start_at,
                end_at=start_at + timedelta(minutes=30),
                phone=WA_ID,
                status=AppointmentStatus.SCHEDULED,
            )
            session.add(appointment)
            await session.flush()
            deposit = PixDeposit(
                id=uuid4(),
                tenant_id=tenant.id,
                appointment_id=appointment.id,
                patient_id=patient.id,
                asaas_payment_id="pay_1",
                amount_cents=7900,
                percent_applied=0,
                status=PixDepositStatus.PAID,
            )
            session.add(deposit)
            seeded["appointment"] = appointment
            seeded["deposit"] = deposit
        await session.commit()
        return seeded


async def _count(db, model) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(model))


# --------------------------------------------------------------------------
# It still wipes the conversational trail
# --------------------------------------------------------------------------


async def test_removes_patient_conversation_and_messages(db) -> None:
    seeded = await _seed(db)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.1"))

    async with db() as session:
        assert await session.get(Patient, seeded["patient"].id) is None
        assert await session.get(Conversation, seeded["conversation"].id) is None
        # A fresh, empty patient + conversation replaced them.
        fresh = await session.scalar(select(Patient).where(Patient.wa_id == WA_ID))
        assert fresh is not None and fresh.id != seeded["patient"].id
    # Only the greeting the new "first contact" receives remains as history.
    assert await _count(db, Message) == 1


async def test_sends_the_first_contact_greeting(db) -> None:
    await _seed(db, with_appointment=False)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.greeting"))

    bodies = [call[2] for call in _FakeWhatsAppClient.sent]
    # Since the greeting-frame round the first-contact message is the rendered
    # product frame, not the tenant's own `greeting_message`. Asserted by its
    # obligation lines rather than the whole literal: this test is about the
    # command replaying a real first contact, and pinning the full copy here
    # would make every future wording tweak fail an unrelated test (the frame
    # itself is pinned in tests/test_greeting_template.py).
    assert any("assistente virtual automatizado" in body for body in bodies)
    assert any("Em emergência, não use este canal" in body for body in bodies)


# --------------------------------------------------------------------------
# Orphans: bookings and money survive
# --------------------------------------------------------------------------


async def test_appointment_is_preserved_and_detached_not_deleted(db) -> None:
    seeded = await _seed(db)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.appt"))

    async with db() as session:
        appointment = await session.get(Appointment, seeded["appointment"].id)
        assert appointment is not None, "deleting it would orphan the Google event"
        # Detached from the deleted rows...
        assert appointment.patient_id is None
        assert appointment.conversation_id is None
        # ...but still a complete, actionable clinic record pointing at the
        # SAME Google Calendar event. DB and Google stay in agreement.
        assert appointment.google_event_id == "evt-live-on-google"
        assert appointment.status == AppointmentStatus.SCHEDULED
        assert appointment.phone == WA_ID
        assert appointment.start_at is not None


async def test_paid_deposit_survives_with_its_appointment(db) -> None:
    """`pix_deposits.appointment_id` is ON DELETE CASCADE — a deleted
    appointment would take a PAID deposit's record with it."""
    seeded = await _seed(db)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.money"))

    async with db() as session:
        deposit = await session.get(PixDeposit, seeded["deposit"].id)
        assert deposit is not None
        assert deposit.status == PixDepositStatus.PAID
        assert deposit.amount_cents == 7900
        assert deposit.appointment_id == seeded["appointment"].id
        assert deposit.patient_id is None  # only the patient pointer is cleared


async def test_consent_ledger_is_untouched(db) -> None:
    await _seed(db)
    before = await _count(db, ConsentEvent)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.consent"))

    # The new patient row adds its own first-contact consent event; the
    # original is never deleted.
    assert await _count(db, ConsentEvent) >= before


async def test_explains_what_was_left_behind(db) -> None:
    await _seed(db)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.notice"))

    notices = [c[2] for c in _FakeWhatsAppClient.sent if "PRESERVADOS" in c[2]]
    assert len(notices) == 1
    assert "1 agendamento(s)" in notices[0]


async def test_clean_slate_sends_no_preservation_notice(db) -> None:
    """With nothing to preserve the reset looks exactly like a first contact."""
    await _seed(db, with_appointment=False)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.clean"))

    assert not [c for c in _FakeWhatsAppClient.sent if "PRESERVADOS" in c[2]]


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


async def test_writes_a_sanitized_durable_audit_row(db) -> None:
    seeded = await _seed(db)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.audit"))

    async with db() as session:
        rows = (
            await session.scalars(
                select(AnalyticsEvent).where(AnalyticsEvent.event_type == "context_removed")
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == seeded["tenant"].id
    # The conversation that was DESTROYED (what earlier log lines carry), plus
    # the replacement created in its place.
    assert row.payload["conversation_id"] == str(seeded["conversation"].id)
    assert row.payload["replacement_conversation_id"] != str(seeded["conversation"].id)
    assert row.payload["patients"] == 1
    assert row.payload["conversations"] == 1
    assert row.payload["messages"] == 3
    assert row.payload["appointments_preserved"] == 1
    assert row.payload["deposits_preserved"] == 1
    assert row.created_at is not None  # server-side timestamp

    # Never the phone number, never the deleted content.
    serialized = str(row.payload)
    assert WA_ID not in serialized
    assert "conteudo clinico sensivel" not in serialized


async def test_audit_row_is_written_even_with_nothing_to_delete(db) -> None:
    """A second run finds (almost) nothing left, and still records the attempt."""
    await _seed(db, with_appointment=False)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.first"))
    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.second"))

    async with db() as session:
        rows = (
            await session.scalars(
                select(AnalyticsEvent)
                .where(AnalyticsEvent.event_type == "context_removed")
                .order_by(AnalyticsEvent.created_at)
            )
        ).all()
    assert len(rows) == 2
    # The second run had a freshly-created (near-empty) patient to remove.
    assert rows[1].payload["messages"] <= 1
    assert rows[1].payload["appointments_preserved"] == 0


async def test_logs_carry_no_phone_number(db, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RecordingLogger()
    monkeypatch.setattr(tasks, "logger", recorder)
    await _seed(db)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.logs"))

    assert any(event == "conversation_context_removed" for event, _ in recorder.records)
    for value in recorder.all_values():
        assert WA_ID not in value
        assert "conteudo clinico sensivel" not in value
    assert "wa_id" not in recorder.all_keys()


# --------------------------------------------------------------------------
# Reachability
# --------------------------------------------------------------------------


async def test_duplicate_delivery_wipes_once(db) -> None:
    seeded = await _seed(db)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.dup"))
    async with db() as session:
        fresh_before = await session.scalar(select(Patient.id).where(Patient.wa_id == WA_ID))

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.dup"))

    async with db() as session:
        fresh_after = await session.scalar(select(Patient.id).where(Patient.wa_id == WA_ID))
        # The second (duplicate) delivery was dropped: the patient created by
        # the first run is still there, not replaced by a third one.
        assert fresh_after == fresh_before
        assert await session.get(Appointment, seeded["appointment"].id) is not None


async def test_off_allowlist_sender_gets_nothing_and_wipes_nothing(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same allowlist boundary as any other turn: during the restricted
    Coexistence window an off-allowlist number cannot make the platform act."""
    seeded = await _seed(db)
    fake_settings = Settings(BOT_ALLOWLIST_WA_IDS="5521900000000")
    monkeypatch.setattr(tasks, "get_settings", lambda: fake_settings)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.denied"))

    assert _FakeWhatsAppClient.sent == []
    async with db() as session:
        assert await session.get(Patient, seeded["patient"].id) is not None
        assert await session.get(Conversation, seeded["conversation"].id) is not None
        audit = (
            await session.scalars(
                select(AnalyticsEvent).where(AnalyticsEvent.event_type == "context_removed")
            )
        ).all()
    assert audit == []


async def test_unentitled_tenant_wipes_but_sends_nothing(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entitlement gates the OUTBOUND, not the operator's own wipe: a send
    costs money, so it fails closed exactly like every other reply."""
    seeded = await _seed(db)

    async def _unentitled(tenant_id, redis):
        return SimpleNamespace(active=False, secretaria_enabled=False, status="past_due")

    monkeypatch.setattr(tasks, "get_entitlements", _unentitled)

    await tasks._handle_patient_messages(_value(COMMAND, wam_id="wamid.wipe.unpaid"))

    assert _FakeWhatsAppClient.sent == []
    async with db() as session:
        # The wipe still happened, and is still audited.
        assert await session.get(Patient, seeded["patient"].id) is None
        audit = (
            await session.scalars(
                select(AnalyticsEvent).where(AnalyticsEvent.event_type == "context_removed")
            )
        ).all()
        assert len(audit) == 1
        # And the booking is still preserved.
        assert await session.get(Appointment, seeded["appointment"].id) is not None


@pytest.mark.parametrize(
    "body",
    ["/menu", "/reset", "/recomeçar", "/inicio", "/DANGEROUSLY-REMOVE-CONTEXT", COMMAND + " "],
)
async def test_near_miss_never_reaches_the_destructive_handler(
    db, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    seeded = await _seed(db)

    async def _explode(**kwargs):
        raise AssertionError(f"{body!r} reached the destructive handler")

    monkeypatch.setattr(tasks, "_handle_remove_context_command", _explode)

    # The non-destructive path needs the normal turn dependencies; stub the
    # reply leg out entirely - this test is only about WHICH handler fires.
    async def _noop_reply(reply, redis=None):
        return None

    monkeypatch.setattr(tasks, "_send_bot_reply", _noop_reply)

    await tasks._handle_patient_messages(_value(body, wam_id=f"wamid.nearmiss.{body}"))

    async with db() as session:
        assert await session.get(Patient, seeded["patient"].id) is not None
