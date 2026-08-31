"""Tests for the `/menu` (non-destructive) command family — PROMPT_FIX_18.

`/menu`, `/reset`, `/recomeçar` and `/inicio` used to route to a "dev reset"
that DELETED the patient row, their conversation, every message and their
appointments — off words a patient types by accident. They are now what their
name says: a way back to the main menu, reusing the exact same seam as the
agent's `show_main_menu` tool. The destructive handler still exists but only
behind the exact literal `/dangerously-remove-context`
(tests/test_remove_context_command.py).

Covers:
  - both predicates, including the near-misses that must NOT fire and the
    exact-match strictness of the destructive one;
  - the integration path: `/menu` preserves every Patient / Appointment /
    Message / ConsentEvent / PixDeposit row AND their ids, changing only the
    conversation's transient flow fields;
  - the gates: active human, tenant off, off-allowlist, unentitled;
  - idempotency: repeat deliveries -> one menu each, no deletes;
  - the load-bearing negative test: NO `delete(...)` statement is reachable
    from the webhook path.

DB tests mirror the in-memory-sqlite pattern from test_bot_allowlist.py /
test_bot_reply_gating.py: a real aiosqlite engine on StaticPool, monkeypatched
in place of `workers.tasks.async_session_factory`.
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
    Appointment,
    AppointmentStatus,
    ConsentEvent,
    Conversation,
    FlowState,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    PixDeposit,
    PixDepositStatus,
    ProcessedEvent,
    Tenant,
)
from secretaria.schemas.webhook import WebhookValue  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"

# --------------------------------------------------------------------------
# Predicates (pure)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "/menu",
        "/MENU",
        "  /menu  ",
        "/reset",
        "/recomecar",
        "/recomeçar",
        "/inicio",
        "/início",
        # Slash-less variants, added with the greeting frame. Not cosmetic: the
        # frame tells every patient "Errou? Digite *voltar*", and no patient
        # types a slash command — the promise was dead copy without these.
        "menu",
        "voltar",
        "VOLTAR",
        "  voltar  ",
        "recomeçar",
        "início",
    ],
)
def test_recognised_menu_triggers(body: str) -> None:
    assert tasks.is_menu_command(body) is True


@pytest.mark.parametrize(
    "body",
    [
        # The bare words match only as the WHOLE body, so ordinary sentences
        # that merely contain them keep routing normally.
        "quero voltar na segunda",
        "posso voltar depois?",
        "menu de serviços",
        "/menus",
        "/menu agora",
        "olá /menu",
        "marcar consulta",
        "",
        None,
    ],
)
def test_non_triggers(body: str | None) -> None:
    assert tasks.is_menu_command(body) is False


def test_menu_aliases_are_not_the_destructive_command() -> None:
    """The whole point of the rename: no `/menu`-ish word reaches the wipe."""
    for body in ("/menu", "/MENU", "  /menu  ", "/reset", "/recomeçar", "/inicio", "/início"):
        assert tasks.is_remove_context_command(body) is False


def test_remove_context_command_matches_exactly() -> None:
    assert tasks.is_remove_context_command("/dangerously-remove-context") is True
    assert tasks.REMOVE_CONTEXT_COMMAND == "/dangerously-remove-context"


@pytest.mark.parametrize(
    "body",
    [
        # No case folding, no trimming, no partial/prefix match, no aliases.
        "/DANGEROUSLY-REMOVE-CONTEXT",
        "/Dangerously-Remove-Context",
        " /dangerously-remove-context",
        "/dangerously-remove-context ",
        "/dangerously-remove-context now",
        "por favor /dangerously-remove-context",
        "/dangerously-remove-contexts",
        "/dangerously_remove_context",
        "/dangerously-remove",
        "dangerously-remove-context",
        "",
        None,
    ],
)
def test_remove_context_near_misses_do_not_fire(body: str | None) -> None:
    assert tasks.is_remove_context_command(body) is False


# --------------------------------------------------------------------------
# Integration harness
# --------------------------------------------------------------------------

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


class _FakeWhatsAppClient:
    """Records every send; installed in place of the real client."""

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

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        _FakeWhatsAppClient.sent.append(("list", to, body, rows))
        return {"messages": [{"id": f"wamid.out.{len(_FakeWhatsAppClient.sent)}"}]}


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

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary()

    monkeypatch.setattr(tasks, "get_entitlements", _fake_get_entitlements)

    async def _fake_get_waba_token(session, tenant_id):
        return "tenant-waba-token"

    monkeypatch.setattr(tasks, "get_waba_token", _fake_get_waba_token)
    yield


def _set_allowlist(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    fake_settings = Settings(BOT_ALLOWLIST_WA_IDS=raw)
    monkeypatch.setattr(tasks, "get_settings", lambda: fake_settings)


def _value(body: str, *, wam_id: str, wa_id: str = WA_ID) -> WebhookValue:
    """One inbound text message, in the exact shape the worker receives."""
    return WebhookValue.model_validate(
        {
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "contacts": [{"wa_id": wa_id, "profile": {"name": "Maria"}}],
            "messages": [
                {
                    "id": wam_id,
                    "from": wa_id,
                    "type": "text",
                    "text": {"body": body},
                }
            ],
        }
    )


async def _seed(
    db,
    *,
    flow_state: FlowState = FlowState.SERVICE_CATALOG,
    handover: HandoverState = HandoverState.BOT_ACTIVE,
    is_active: bool = True,
) -> dict:
    """A tenant + patient + conversation mid-flow, with a booking and a deposit.

    Deliberately NOT a clean slate: the point of these tests is that a rich,
    real conversation survives `/menu` untouched.
    """
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=is_active,
            greeting_message="Olá! Bem-vindo à Clínica.",
            initial_flows={},
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id=WA_ID, name="Maria")
        session.add_all([tenant, patient])
        await session.flush()
        conversation = Conversation(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_state=flow_state,
            flow_step="pick_day",
            flow_selected_type="Consulta Geral",
            handover_state=handover,
        )
        session.add(conversation)
        await session.flush()
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                wam_id="wamid.history.1",
                body="quero marcar",
            )
        )
        session.add(
            ConsentEvent(
                tenant_id=tenant.id,
                wa_id=WA_ID,
                kind="first_contact_service",
                legal_basis="test",
            )
        )
        start_at = datetime.now(UTC) + timedelta(days=3)
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            conversation_id=conversation.id,
            google_event_id="evt-keep-me",
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
        await session.commit()
        return {
            "tenant": tenant,
            "patient": patient,
            "conversation": conversation,
            "appointment": appointment,
            "deposit": deposit,
        }


async def _count(db, model) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def _snapshot(db) -> dict:
    """Row counts for everything `/menu` must never touch."""
    return {
        "patients": await _count(db, Patient),
        "conversations": await _count(db, Conversation),
        "messages": await _count(db, Message),
        "appointments": await _count(db, Appointment),
        "consent_events": await _count(db, ConsentEvent),
        "deposits": await _count(db, PixDeposit),
    }


# --------------------------------------------------------------------------
# `/menu` is non-destructive
# --------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["/menu", "/reset", "/recomeçar", "/inicio"])
async def test_menu_preserves_every_row_and_id(
    db, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    _set_allowlist(monkeypatch, "")
    seeded = await _seed(db)
    before = await _snapshot(db)

    await tasks._handle_patient_messages(_value(command, wam_id=f"wamid.{command}"))

    after = await _snapshot(db)
    # The inbound command and the outbound menu card are themselves persisted
    # as conversation history - everything ELSE is untouched.
    assert after["messages"] == before["messages"] + 2
    for key in ("patients", "conversations", "appointments", "consent_events", "deposits"):
        assert after[key] == before[key], key

    async with db() as session:
        patient = await session.get(Patient, seeded["patient"].id)
        appointment = await session.get(Appointment, seeded["appointment"].id)
        deposit = await session.get(PixDeposit, seeded["deposit"].id)
        assert patient is not None and patient.wa_id == WA_ID
        # Same appointment id, still pointing at the same patient AND the same
        # Google Calendar event: the DB and Google cannot diverge here.
        assert appointment is not None
        assert appointment.patient_id == seeded["patient"].id
        assert appointment.google_event_id == "evt-keep-me"
        assert appointment.status == AppointmentStatus.SCHEDULED
        assert deposit is not None and deposit.status == PixDepositStatus.PAID


async def test_menu_resets_only_transient_flow_state(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_allowlist(monkeypatch, "")
    seeded = await _seed(db)

    await tasks._handle_patient_messages(_value("/menu", wam_id="wamid.menu.state"))

    async with db() as session:
        conversation = await session.get(Conversation, seeded["conversation"].id)
        # Same conversation row (same id), moved to MENU - not a new one.
        assert conversation is not None
        assert conversation.flow_state == FlowState.MENU
        assert conversation.flow_step is None
        assert conversation.flow_selected_type is None


async def test_menu_renders_the_menu_card(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_allowlist(monkeypatch, "")
    await _seed(db)

    await tasks._handle_patient_messages(_value("/menu", wam_id="wamid.menu.card"))

    assert len(_FakeWhatsAppClient.sent) == 1
    kind, to, _body, buttons = _FakeWhatsAppClient.sent[0]
    assert kind == "buttons"
    assert to == WA_ID
    assert buttons  # the effective menu labels


async def test_menu_is_idempotent_across_two_deliveries(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two DIFFERENT events -> two identical menus, no deletes; the SAME event
    twice -> exactly one menu (ProcessedEvent dedupe)."""
    _set_allowlist(monkeypatch, "")
    seeded = await _seed(db)
    before = await _snapshot(db)

    await tasks._handle_patient_messages(_value("/menu", wam_id="wamid.menu.a"))
    await tasks._handle_patient_messages(_value("/menu", wam_id="wamid.menu.b"))
    assert len(_FakeWhatsAppClient.sent) == 2
    assert _FakeWhatsAppClient.sent[1] == _FakeWhatsAppClient.sent[0]

    # Redelivery of an already-processed event: dropped, no third menu.
    await tasks._handle_patient_messages(_value("/menu", wam_id="wamid.menu.a"))
    assert len(_FakeWhatsAppClient.sent) == 2

    after = await _snapshot(db)
    for key in ("patients", "conversations", "appointments", "consent_events", "deposits"):
        assert after[key] == before[key], key
    async with db() as session:
        assert await session.get(Appointment, seeded["appointment"].id) is not None


async def test_no_delete_statement_is_reachable_from_the_webhook(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing negative test.

    Fails loudly if ANY `delete(...)` is wired back into the inbound patient
    path — the exact regression PROMPT_FIX_18 exists to prevent.
    """
    _set_allowlist(monkeypatch, "")
    await _seed(db)

    def _explode(*args, **kwargs):
        raise AssertionError("a DELETE is reachable from the patient webhook path")

    monkeypatch.setattr(tasks, "delete", _explode)

    for command in ("/menu", "/reset", "/recomeçar", "/inicio", "oi", "Agendar"):
        await tasks._handle_patient_messages(_value(command, wam_id=f"wamid.nodelete.{command}"))


# --------------------------------------------------------------------------
# The gates: `/menu` is a normal turn, not a bypass
# --------------------------------------------------------------------------


async def test_menu_is_ignored_while_a_human_is_active(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit contract: with a human on the line `/menu` is recorded and
    IGNORED. It is never a way for the patient to take the bot back."""
    _set_allowlist(monkeypatch, "")
    seeded = await _seed(db, handover=HandoverState.HUMAN_ACTIVE)

    await tasks._handle_patient_messages(_value("/menu", wam_id="wamid.menu.human"))

    assert _FakeWhatsAppClient.sent == []
    async with db() as session:
        conversation = await session.get(Conversation, seeded["conversation"].id)
        # Handover untouched, and the flow state NOT reset behind the human.
        assert conversation.handover_state == HandoverState.HUMAN_ACTIVE
        assert conversation.flow_state == FlowState.SERVICE_CATALOG


async def test_menu_off_allowlist_is_dropped(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_allowlist(monkeypatch, "5511000000000")
    await _seed(db)

    await tasks._handle_patient_messages(_value("/menu", wam_id="wamid.menu.deny"))

    assert _FakeWhatsAppClient.sent == []


async def test_menu_for_inactive_tenant_gets_the_fallback_not_the_menu(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_allowlist(monkeypatch, "")
    await _seed(db, is_active=False)

    await tasks._handle_patient_messages(_value("/menu", wam_id="wamid.menu.inactive"))

    assert len(_FakeWhatsAppClient.sent) == 1
    kind, _to, body = _FakeWhatsAppClient.sent[0]
    assert kind == "text"
    assert body == tasks.SERVICE_UNAVAILABLE_MESSAGE


async def test_menu_unentitled_sends_nothing(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_allowlist(monkeypatch, "")
    await _seed(db)

    async def _unentitled(tenant_id, redis):
        return _summary(active=False, status="past_due")

    monkeypatch.setattr(tasks, "get_entitlements", _unentitled)

    await tasks._handle_patient_messages(_value("/menu", wam_id="wamid.menu.unpaid"))

    assert _FakeWhatsAppClient.sent == []


async def test_menu_records_the_processed_event(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """`/menu` is a normal turn now: its event id is claimed like any other."""
    _set_allowlist(monkeypatch, "")
    await _seed(db)

    await tasks._handle_patient_messages(_value("/menu", wam_id="wamid.menu.claim"))

    async with db() as session:
        claimed = await session.scalar(
            select(ProcessedEvent.event_id).where(ProcessedEvent.event_id == "wamid.menu.claim")
        )
    assert claimed == "wamid.menu.claim"
