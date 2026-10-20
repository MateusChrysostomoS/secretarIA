"""`/menu3` - the test-only reset that puts one patient back at the LGPD notice.

What is pinned, in order:
  1. pure: the command match (case, whitespace) and the tenant allowlist setting;
  2. Portal, allowed tenant: messages, holds and the PII map deleted, every
     conversation field back to a fresh row, consent owed again, the frame + the
     LGPD notice (with its buttons) as the whole new transcript, the appointment,
     the consent ledger and the OTHER patient untouched - and "✅ Concordo" works
     right after, so the founder can go again;
  3. the command sits above human mode and the identity gate;
  4. any other tenant (or an emptied setting): `/menu3` is an ordinary message;
  5. WhatsApp, allowed tenant: the same reset, the same two messages on the wire.

DB tests use the in-memory-sqlite-on-StaticPool pattern of test_lgpd_consent_gate.py.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.config import Settings
from secretaria.core.database import Base
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    ConsentEvent,
    Conversation,
    ConversationPiiTokenMap,
    FlowState,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
    interactive_history_body,
)
from secretaria.services.entitlements_client import EntitlementSummary
from secretaria.services.greeting_template import (
    CONSENT_ACCEPTED_MESSAGE,
    CONSENT_BUTTON_LABEL,
    CONSENT_EVENT_KIND,
    LGPD_CONSENT_MESSAGE,
    render_greeting,
)
from secretaria.services.patient_name import NAME_CHANGE_ACTION, NAME_CHANGE_BUTTON_LABEL
from secretaria.services.pending_identity import IdentityState
from secretaria.workers import tasks, turn_router
from secretaria.workers.shared.debug_reset import is_debug_reset_command
from tests._patching import workers_ns

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
EXTERNAL_ID = "bm-founder-visit"
OTHER_EXTERNAL_ID = "bm-someone-else"
# The first of the two test clinics in the setting's default.
TEST_CLINIC_ID = UUID("9c4fa6a5-ffdb-4adb-9fe1-9036270f1246")
DEFAULT_ALLOWLIST = Settings.model_fields["DEBUG_RESET_TENANT_IDS"].default


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


class _Probe:
    """The brain-api identity probe, answering VERIFIED and counting its calls.

    VERIFIED is the hard case: the ordinary consent path mirrors a verified
    account's consent and skips the notice. `/menu3` must never ask.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []


@pytest.fixture
def probe() -> _Probe:
    return _Probe()


def _settings(allowlist: str = DEFAULT_ALLOWLIST) -> Settings:
    return Settings(BOT_ALLOWLIST_WA_IDS="", DEBUG_RESET_TENANT_IDS=allowlist)


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db, probe):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "get_settings", _settings)

    async def _fake_resolve(session, tenant_id, patient_id, **kwargs):
        return None

    async def _fake_token(session, tenant_id):
        return "decrypted-waba-token"

    async def _fake_entitlements(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id),
            status="active",
            active=True,
            secretaria_enabled=True,
            plan="bronze",
            secretaria_tier="basico",
            addons={},
            limits={},
        )

    async def _probe(tenant_id, external_id):
        probe.calls.append(external_id)
        return IdentityState.VERIFIED

    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _fake_resolve)
    monkeypatch.setattr(workers_ns, "get_waba_token", _fake_token)
    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_entitlements)
    monkeypatch.setattr(workers_ns, "probe_identity", _probe)
    yield


# --------------------------------------------------------------------------
# Seeding: a patient deep inside a booking, with everything `/menu3` must clear
# --------------------------------------------------------------------------


async def _seed(
    db,
    *,
    tenant_id: UUID = TEST_CLINIC_ID,
    channel: str = CHANNEL_BRAIN_MESSAGE,
    handover: HandoverState = HandoverState.HUMAN_ACTIVE,
    flow_state: FlowState = FlowState.SERVICE_CATALOG,
) -> dict:
    now = datetime.now(UTC)
    async with db() as session:
        tenant = Tenant(
            id=tenant_id,
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            clinic_description="Oftalmologia.",
            initial_flows={},
        )
        if channel == CHANNEL_BRAIN_MESSAGE:
            patient = Patient(
                tenant_id=tenant_id,
                channel=CHANNEL_BRAIN_MESSAGE,
                external_id=EXTERNAL_ID,
                name="Maria Souza",
                email="maria@example.com",
                lgpd_accepted_at=now - timedelta(days=1),
            )
            ref = EXTERNAL_ID
        else:
            patient = Patient(
                tenant_id=tenant_id,
                wa_id=WA_ID,
                name="Maria Souza",
                lgpd_accepted_at=now - timedelta(days=1),
            )
            ref = WA_ID
        other = Patient(
            tenant_id=tenant_id,
            channel=CHANNEL_BRAIN_MESSAGE,
            external_id=OTHER_EXTERNAL_ID,
            name="Outra Pessoa",
            lgpd_accepted_at=now - timedelta(days=2),
        )
        session.add_all([tenant, patient, other])
        await session.flush()

        conversation = Conversation(
            tenant_id=tenant_id,
            patient_id=patient.id,
            handover_state=handover,
            last_human_message_at=now - timedelta(minutes=5),
            last_bot_message_at=now - timedelta(minutes=6),
            flow_state=flow_state,
            flow_step="pick_day",
            flow_selected_type="Consulta Geral",
            flow_selected_day="2026-10-22",
            flow_selected_slot="2026-10-22T14:00:00-03:00",
            flow_selected_insurance="Particular",
            flow_attendee_name="João Souza",
            flow_draft={"t": "Consulta Geral", "w": "other"},
            flow_edit_draft={"stage": "menu"},
            reactivation_origin=FlowState.SERVICE_CATALOG.value,
        )
        other_conversation = Conversation(tenant_id=tenant_id, patient_id=other.id)
        session.add_all([conversation, other_conversation])
        await session.flush()

        start_at = now + timedelta(days=3)
        appointment = Appointment(
            tenant_id=tenant_id,
            patient_id=patient.id,
            conversation_id=conversation.id,
            google_event_id="evt-keep-me",
            appointment_type="Consulta Geral",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appointment)
        await session.flush()
        conversation.flow_managing_appointment_id = appointment.id
        conversation.flow_replaces_appointment_id = appointment.id

        for direction, sender, body in (
            (MessageDirection.INBOUND, MessageSender.PATIENT, "quero marcar"),
            (MessageDirection.OUTBOUND, MessageSender.BOT, "Qual dia?"),
            (MessageDirection.OUTBOUND, MessageSender.HUMAN, "Oi, aqui é a Ana da clínica"),
        ):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=direction,
                    sender=sender,
                    body=body,
                )
            )
        session.add(
            Message(
                conversation_id=other_conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                body="mensagem de outra pessoa",
            )
        )
        session.add(
            BookingHold(
                tenant_id=tenant_id,
                conversation_id=conversation.id,
                patient_id=patient.id,
                start_at=start_at,
                end_at=start_at + timedelta(minutes=30),
                expires_at=now + timedelta(minutes=10),
            )
        )
        session.add(
            ConversationPiiTokenMap(
                conversation_id=conversation.id, tokens={"[PACIENTE_1]": "Maria Souza"}
            )
        )
        session.add(
            ConsentEvent(
                tenant_id=tenant_id,
                wa_id=ref,
                kind=CONSENT_EVENT_KIND,
                legal_basis="test",
            )
        )
        await session.commit()
        return {
            "tenant": tenant,
            "patient_id": patient.id,
            "other_patient_id": other.id,
            "conversation_id": conversation.id,
            "other_conversation_id": other_conversation.id,
            "appointment_id": appointment.id,
        }


async def _bm_turn(tenant_id: UUID, body: str, *, send: bool = True):
    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant_id, external_id=EXTERNAL_ID, text=body
    )
    if send and reply is not None:
        await tasks._send_bot_reply(reply, redis=None)
    return reply


async def _rows(db, conversation_id: UUID) -> list[Message]:
    async with db() as session:
        return list(
            (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    # SQLite's rowid is insertion order; `created_at` ties within a turn.
                    .order_by(Message.created_at, text("messages.rowid"))
                )
            ).all()
        )


async def _count(db, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    async with db() as session:
        return await session.scalar(stmt)


def _expected_opening(tenant: Tenant) -> list[tuple[str, str]]:
    """The transcript a reset leaves on the Portal: frame, then the LGPD notice."""
    return [
        ("bot", render_greeting(tenant.clinic_name, tenant.clinic_description)),
        (
            "bot",
            interactive_history_body(
                LGPD_CONSENT_MESSAGE, [CONSENT_BUTTON_LABEL, NAME_CHANGE_BUTTON_LABEL]
            ),
        ),
    ]


# --------------------------------------------------------------------------
# 1. Pure
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["/menu3", "/MENU3", "  /Menu3  ", "\n/mEnU3\t", "/menu3\n"])
def test_the_command_ignores_case_and_surrounding_whitespace(body: str) -> None:
    assert is_debug_reset_command(body) is True


@pytest.mark.parametrize(
    "body", [None, "", "/menu", "menu3", "/menu 3", "/menu3 de novo", "quero /menu3", "/menu33"]
)
def test_anything_else_is_not_the_command(body) -> None:
    assert is_debug_reset_command(body) is False


def test_the_default_allowlist_is_exactly_the_two_test_clinics() -> None:
    assert DEFAULT_ALLOWLIST == (
        "9c4fa6a5-ffdb-4adb-9fe1-9036270f1246,a31bf7b7-3727-48bb-b1eb-342847cd6cd6"
    )
    assert _settings().debug_reset_tenant_ids == frozenset(
        {
            UUID("9c4fa6a5-ffdb-4adb-9fe1-9036270f1246"),
            UUID("a31bf7b7-3727-48bb-b1eb-342847cd6cd6"),
        }
    )


def test_an_empty_setting_turns_the_command_off() -> None:
    assert _settings("").debug_reset_tenant_ids == frozenset()


def test_the_setting_tolerates_quotes_blanks_and_typos() -> None:
    parsed = _settings(
        ' "9c4fa6a5-ffdb-4adb-9fe1-9036270f1246" , ,not-a-uuid,'
        "'A31BF7B7-3727-48BB-B1EB-342847CD6CD6'"
    ).debug_reset_tenant_ids
    assert parsed == frozenset(
        {
            UUID("9c4fa6a5-ffdb-4adb-9fe1-9036270f1246"),
            UUID("a31bf7b7-3727-48bb-b1eb-342847cd6cd6"),
        }
    )


# --------------------------------------------------------------------------
# 2-3. Portal, allowed tenant
# --------------------------------------------------------------------------


class _RecordingLogger:
    """Stands in for `turn_router.logger`; structlog caches loggers on first use."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __getattr__(self, level: str):
        def emit(event, *args, **kwargs):
            self.events.append({"event": event, "level": level, **kwargs})

        return emit


async def test_portal_reset_wipes_the_conversation_and_asks_for_consent_again(
    db, probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed(db)
    tenant = seeded["tenant"]
    conversation_id = seeded["conversation_id"]
    recorder = _RecordingLogger()
    monkeypatch.setattr(turn_router, "logger", recorder)

    reply = await _bm_turn(tenant.id, "  /MENU3 ")

    assert reply is not None and reply.debug_reset is True
    [logged] = [e for e in recorder.events if e["event"] == "debug_conversation_reset"]
    assert logged == {
        "event": "debug_conversation_reset",
        "level": "warning",
        "tenant_id": str(tenant.id),
        "conversation_id": str(conversation_id),
        "patient_id": str(seeded["patient_id"]),
        "channel": CHANNEL_BRAIN_MESSAGE,
        "messages_deleted": 3,
        "booking_holds_deleted": 1,
        "pii_maps_deleted": 1,
    }
    # The transcript the Portal reads is ONLY the new opening; `/menu3` itself and
    # the three earlier messages are gone.
    rows = await _rows(db, conversation_id)
    assert [(r.sender.value, r.body) for r in rows] == _expected_opening(tenant)
    assert all(r.direction == MessageDirection.OUTBOUND for r in rows)
    # The notice is a real card: "✅ Concordo" and "✏️ Mudar nome" are tappable.
    assert rows[0].interactive is None
    options = [(o["id"], o["title"]) for o in rows[1].interactive["options"]]
    assert options == [
        ("consent|accept", CONSENT_BUTTON_LABEL),
        (NAME_CHANGE_ACTION, NAME_CHANGE_BUTTON_LABEL),
    ]
    # Never probed: a VERIFIED answer would have skipped the notice.
    assert probe.calls == []

    async with db() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation.handover_state == HandoverState.BOT_ACTIVE
        assert conversation.last_human_message_at is None
        assert conversation.flow_state == FlowState.IDLE
        for field in (
            "flow_step",
            "flow_selected_type",
            "flow_selected_day",
            "flow_selected_slot",
            "flow_selected_professional_id",
            "flow_managing_appointment_id",
            "flow_selected_insurance",
            "flow_attendee_name",
            "flow_draft",
            "flow_replaces_appointment_id",
            "flow_edit_draft",
            "reactivation_origin",
        ):
            assert getattr(conversation, field) is None, field

        patient = await session.get(Patient, seeded["patient_id"])
        assert patient.lgpd_accepted_at is None
        # Identity kept, so the Portal session (brain-api account link) keeps working.
        assert patient.channel == CHANNEL_BRAIN_MESSAGE
        assert patient.external_id == EXTERNAL_ID
        assert patient.email == "maria@example.com"
        assert patient.name == "Maria Souza"

        appointment = await session.get(Appointment, seeded["appointment_id"])
        assert appointment.patient_id == seeded["patient_id"]
        assert appointment.conversation_id == conversation_id
        assert appointment.google_event_id == "evt-keep-me"
        assert appointment.status == AppointmentStatus.SCHEDULED

        other = await session.get(Patient, seeded["other_patient_id"])
        assert other.lgpd_accepted_at is not None

    assert await _count(db, BookingHold, BookingHold.conversation_id == conversation_id) == 0
    assert (
        await _count(
            db,
            ConversationPiiTokenMap,
            ConversationPiiTokenMap.conversation_id == conversation_id,
        )
        == 0
    )
    # The LGPD ledger is append-only: the earlier acceptance stays on record.
    assert await _count(db, ConsentEvent, ConsentEvent.kind == CONSENT_EVENT_KIND) == 1
    # Nobody else's conversation was touched.
    assert [r.body for r in await _rows(db, seeded["other_conversation_id"])] == [
        "mensagem de outra pessoa"
    ]
    assert await _count(db, Patient) == 2
    assert await _count(db, Conversation) == 2


async def test_after_the_reset_concordo_works_and_the_reset_repeats(db) -> None:
    seeded = await _seed(db)
    tenant = seeded["tenant"]
    conversation_id = seeded["conversation_id"]

    await _bm_turn(tenant.id, "/menu3")
    await _bm_turn(tenant.id, CONSENT_BUTTON_LABEL)

    async with db() as session:
        patient = await session.get(Patient, seeded["patient_id"])
        assert patient.lgpd_accepted_at is not None
    rows = await _rows(db, conversation_id)
    assert rows[-1].body.startswith(CONSENT_ACCEPTED_MESSAGE)
    assert await _count(db, ConsentEvent, ConsentEvent.kind == CONSENT_EVENT_KIND) == 2

    # And again: the founder re-tests as many times as needed.
    await _bm_turn(tenant.id, "/menu3")
    rows = await _rows(db, conversation_id)
    assert [(r.sender.value, r.body) for r in rows] == _expected_opening(tenant)
    async with db() as session:
        patient = await session.get(Patient, seeded["patient_id"])
        assert patient.lgpd_accepted_at is None


async def test_the_reset_runs_above_the_identity_gate(db, probe) -> None:
    """Mid e-mail question: `/menu3` is not read as a malformed address."""
    seeded = await _seed(
        db, handover=HandoverState.BOT_ACTIVE, flow_state=FlowState.AWAITING_EMAIL
    )

    reply = await _bm_turn(seeded["tenant"].id, "/menu3")

    assert reply.debug_reset is True
    assert reply.pending_email_invalid is False
    assert [(r.sender.value, r.body) for r in await _rows(db, seeded["conversation_id"])] == (
        _expected_opening(seeded["tenant"])
    )
    assert probe.calls == []


async def test_a_file_captioned_menu3_is_not_a_reset(db) -> None:
    seeded = await _seed(db, handover=HandoverState.BOT_ACTIVE)

    reply = await tasks._persist_brain_message_inbound(
        tenant_id=seeded["tenant"].id,
        external_id=EXTERNAL_ID,
        text="/menu3",
        attachment={
            "r2_object_key": "k/1",
            "content_type": "application/pdf",
            "size_bytes": 10,
            "filename": "exame.pdf",
        },
    )

    assert reply.debug_reset is False and reply.attachment_received is True
    assert len(await _rows(db, seeded["conversation_id"])) == 4


# --------------------------------------------------------------------------
# 4. Any other tenant: an ordinary message
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tenant_id", "allowlist"),
    [
        (uuid4(), DEFAULT_ALLOWLIST),  # not one of the test clinics
        (TEST_CLINIC_ID, ""),  # a test clinic, with the setting emptied by env
    ],
)
async def test_other_tenants_route_menu3_normally(
    db, monkeypatch: pytest.MonkeyPatch, tenant_id, allowlist
) -> None:
    monkeypatch.setattr(workers_ns, "get_settings", lambda: _settings(allowlist))
    seeded = await _seed(db, tenant_id=tenant_id)

    reply = await _bm_turn(tenant_id, "/menu3")

    # A human owns the conversation: the message is recorded and the bot stays
    # silent, exactly as for any other text.
    assert reply is None
    rows = await _rows(db, seeded["conversation_id"])
    assert [r.body for r in rows][-1] == "/menu3"
    assert len(rows) == 4
    async with db() as session:
        conversation = await session.get(Conversation, seeded["conversation_id"])
        assert conversation.handover_state == HandoverState.HUMAN_ACTIVE
        assert conversation.flow_state == FlowState.SERVICE_CATALOG
        patient = await session.get(Patient, seeded["patient_id"])
        assert patient.lgpd_accepted_at is not None
    assert await _count(db, BookingHold) == 1


async def test_other_tenants_bot_active_menu3_reaches_normal_dispatch(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = uuid4()
    seeded = await _seed(db, tenant_id=tenant_id, handover=HandoverState.BOT_ACTIVE)

    reply = await _bm_turn(tenant_id, "/menu3", send=False)

    assert reply is not None and reply.debug_reset is False
    assert reply.inbound_body == "/menu3"
    assert len(await _rows(db, seeded["conversation_id"])) == 4


# --------------------------------------------------------------------------
# 5. WhatsApp, allowed tenant
# --------------------------------------------------------------------------


class _WireClient:
    sends: list[tuple] = []

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        _WireClient.sends.append(("text", body, None))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}

    async def send_buttons(self, to, body, buttons):
        _WireClient.sends.append(("buttons", body, [label for _id, label in buttons]))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        _WireClient.sends.append(("list", body, None))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}


async def test_whatsapp_reset_sends_the_same_opening(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _WireClient.sends = []
    monkeypatch.setattr(workers_ns, "WhatsAppClient", _WireClient)
    seeded = await _seed(db, channel="whatsapp")
    tenant = seeded["tenant"]

    reply = await tasks._persist_inbound_message(
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id="wamid.menu3",
        body="/Menu3",
    )
    assert reply is not None and reply.debug_reset is True
    await tasks._send_bot_reply(reply, redis=None)

    frame = render_greeting(tenant.clinic_name, tenant.clinic_description)
    assert _WireClient.sends == [
        ("text", frame, None),
        ("buttons", LGPD_CONSENT_MESSAGE, [CONSENT_BUTTON_LABEL, NAME_CHANGE_BUTTON_LABEL]),
    ]
    rows = await _rows(db, seeded["conversation_id"])
    assert [r.direction for r in rows] == [MessageDirection.OUTBOUND, MessageDirection.OUTBOUND]
    assert rows[0].body == frame
    async with db() as session:
        patient = await session.get(Patient, seeded["patient_id"])
        assert patient.lgpd_accepted_at is None
        assert patient.wa_id == WA_ID
        assert await session.get(Appointment, seeded["appointment_id"]) is not None
