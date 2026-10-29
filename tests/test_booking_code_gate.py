"""The 6-digit code as a GATE: no verified code, no appointment.

Origin: `z_prompts/PROMPT_BRAIN_MESSAGE_OTP_PORTAO_ANTES_DA_CONSULTA.md`, which
reverses the "the appointment is committed before the code" decision that
`test_brain_message_email_otp_inline.py` still pins for every path the gate
does NOT cover. Both files are correct at once, and the thing that decides
which one applies is whether a reservation exists (`services/booking_hold.py`).

What each section proves, in the order the owner's checklist asks for it:

  1. the gate notice says exactly what the owner wrote, and can name the inbox
  2. a code-phase hold reserves NOTHING (TASK-072, owner 2026-10-29): other
     patients still see and book the slot; only a Pix (payment phase) blocks
  3. confirming a slot creates nothing on Google and nothing in `appointments`
  4. WhatsApp is untouched: same turn, same event, same row, no reservation
  5. the appointment is created at the moment the code is verified, and the
     post_booking hooks (PreCheck handoff included) fire from THAT point
  6. a late code still books a free slot (the hold's clock is memory, not a
     reservation), and a Pix-held slot is never booked over (TASK-072)
  7. the LLM cannot announce a verification no tool performed
  8. free text during a held wait repeats the card instead of costing the slot

Fixtures follow tests/test_brain_message_email_otp_inline.py (in-memory SQLite
on a StaticPool), because these two files describe two halves of one flow and
a second harness would let them drift.
"""
import os

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
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
    BookingHold,
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services import (
    booking_hold as holds,  # noqa: E402
    flow_router as fr,  # noqa: E402
    reminder_hooks,  # noqa: E402
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.greeting_template import (  # noqa: E402
    CONSENT_BUTTON_LABEL,
    LGPD_TERMS_URL,
)
from secretaria.services.patient_name import NAME_REQUEST_AFTER_EMAIL_MESSAGE  # noqa: E402
from secretaria.services.pending_identity import (  # noqa: E402
    BOOKING_GATE_REPROMPT_SENTENCE,
    BOOKING_GATE_SENTENCE,
    CODE_GIVE_UP_UNCONFIRMED_MESSAGE,
    CODE_NOTICE_BUTTONS,
    IDENTITY_CHANGE_EMAIL_ACTION,
    IDENTITY_RESEND_ACTION,
    ClaimOutcome,
    ClaimResult,
    RequestCodeOutcome,
    RequestCodeResult,
    VerifyOutcome,
    VerifyResult,
    booking_gate_body,
    provider_link,
)
from secretaria.services.sensitive_claim_guard import (  # noqa: E402
    ACTION_IDENTITY_VERIFIED,
    UNBACKED_CLAIM_MESSAGE,
    guard_reply,
    unbacked_claim,
)
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import booking_hold as worker_holds  # noqa: E402
from secretaria.workers.shared.booking_hold import SlotTakenAtPromotion  # noqa: E402
from tests._reminders_v2 import HookSpy  # noqa: E402

EXTERNAL_ID = "00000000-0000-4000-8000-000000000001"
OTHER_EXTERNAL_ID = "00000000-0000-4000-8000-000000000002"
EMAIL_MASKED = "m***a@gmail.com"
TZ = ZoneInfo("America/Sao_Paulo")

# The exact hallucination the owner saw in production on 2026-09-17. It exists
# nowhere in the source tree - the model wrote it because the conversation
# looked like it was time for it.
HALLUCINATION = "Pronto — código verificado e sua conta foi ativada. ✅"


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


class _Calls:
    def __init__(self) -> None:
        self.code_requests: list[str] = []
        self.verified: list[str] = []
        self.events: list[tuple[datetime, datetime, str]] = []
        self.hooks: list[tuple[str, str]] = []


@pytest.fixture
def calls() -> _Calls:
    return _Calls()


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db, calls):
    # Both modules bind the factory at import time, so both are substituted:
    # the worker leg (promotion, the inbound transaction) and the service leg
    # (the gate's own hold writes, which run from the router).
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(holds, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))

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

    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _fake_resolve)
    monkeypatch.setattr(workers_ns, "get_waba_token", _fake_token)
    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_entitlements)

    async def _request(tenant_id, external_id):
        calls.code_requests.append(external_id)
        return RequestCodeResult(RequestCodeOutcome.SENT, email_masked=EMAIL_MASKED)

    async def _verify(tenant_id, external_id, code):
        calls.verified.append(code)
        return VerifyResult(VerifyOutcome.VERIFIED if code == "123456" else VerifyOutcome.INVALID)

    monkeypatch.setattr(holds, "request_code", _request)
    monkeypatch.setattr(workers_ns, "request_code", _request)
    monkeypatch.setattr(workers_ns, "verify_code", _verify)

    async def _enqueue(redis, tenant_id, appointment_id, source):
        calls.hooks.append((str(appointment_id), source))

    monkeypatch.setattr(workers_ns, "enqueue_post_booking_hooks", _enqueue)
    yield


class _FakeCalendar:
    """A calendar that records what it was asked to create.

    Never called on a gated turn - which is what half the assertions here are
    about.
    """

    tzinfo = TZ

    def __init__(self, calls: _Calls) -> None:
        self._calls = calls

    async def create_event(self, start, end, summary, description=""):
        self._calls.events.append((start, end, summary))
        return {"id": "evt-1", "htmlLink": "https://calendar.google.com/evt-1"}


def _conversation_snapshot(slot_iso: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Consulta",
        flow_selected_day="2099-01-05",
        flow_selected_slot=slot_iso,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
    )


async def _seed_tenant(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id="1234567890",
            is_active=True,
            clinic_description="Oftalmologia.",
            timezone="America/Sao_Paulo",
            initial_flows={},
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _seed_conversation(
    db, tenant: Tenant, *, external_id: str = EXTERNAL_ID, flow_state=FlowState.IDLE
):
    async with db() as session:
        patient = Patient(
            id=uuid4(),
            tenant_id=tenant.id,
            channel=CHANNEL_BRAIN_MESSAGE,
            external_id=external_id,
            name="Maria",
            lgpd_accepted_at=datetime.now(UTC),
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_state=flow_state,
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        await session.refresh(patient)
        return patient, conversation


async def _place(db, tenant, conversation, start, *, minutes=30, ttl=10, patient=None):
    return await holds.place_hold(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        patient_id=getattr(patient, "id", None),
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=start,
        end_at=start + timedelta(minutes=minutes),
        ttl_minutes=ttl,
    )


async def _expire(db, conversation) -> None:
    """Push this conversation's reservation into the past.

    NOTHING else runs. That is the point: ten minutes after a real hold the
    table is in exactly this state, and the slot has to be free purely because
    every reader compares `expires_at` to now.
    """
    async with db() as session:
        async with session.begin():
            row = await session.scalar(
                select(BookingHold).where(BookingHold.conversation_id == conversation.id)
            )
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)


async def _appointments(db, tenant) -> list[Appointment]:
    async with db() as session:
        return list(
            (
                await session.scalars(
                    select(Appointment).where(Appointment.tenant_id == tenant.id)
                )
            ).all()
        )


async def _hold_rows(db, tenant) -> list[BookingHold]:
    async with db() as session:
        return list(
            (
                await session.scalars(
                    select(BookingHold).where(BookingHold.tenant_id == tenant.id)
                )
            ).all()
        )


async def _outbound(db, tenant) -> list[str]:
    async with db() as session:
        return list(
            (
                await session.scalars(
                    select(Message.body)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(
                        Conversation.tenant_id == tenant.id,
                        Message.direction == MessageDirection.OUTBOUND,
                    )
                    .order_by(Message.created_at, text("messages.rowid"))
                )
            ).all()
        )


def _async(value):
    async def _run():
        return value

    return _run()


# --------------------------------------------------------------------------
# 1. The notice says what the owner wrote
# --------------------------------------------------------------------------


def test_the_gate_notice_opens_with_the_owners_exact_sentence():
    body = booking_gate_body(EMAIL_MASKED, when="05/01/2099 às 10:00")
    # Verbatim, not paraphrased: the owner dictated this line.
    assert body.startswith(
        "Para registrá-lo(a) no sistema e confirmar a sua consulta, verifique o "
        "*código de 6 dígitos* que chegou no seu e-mail."
    )
    assert BOOKING_GATE_SENTENCE in body


def test_the_notice_names_the_masked_inbox_the_chosen_slot_and_the_webmail():
    body = booking_gate_body(EMAIL_MASKED, when="05/01/2099 às 10:00")
    assert EMAIL_MASKED in body
    # The "Abrir e-mail" affordance, as a link: the card is capped at three
    # buttons and a URL button would need frontend work that is out of scope.
    assert "https://mail.google.com/" in body
    assert "Gmail" in body
    assert "05/01/2099 às 10:00" in body
    # TASK-072 (owner, 2026-10-29): no slot is kept for anybody for a period of time,
    # so the notice never promises one - it says what is still missing instead.
    assert "10 minutos" not in body
    assert "Guardei" not in body
    assert "Só falta confirmar o seu e-mail para finalizar o agendamento." in body


def test_the_webmail_is_derived_from_the_mask_never_from_an_address():
    assert provider_link("m***a@gmail.com") == ("Gmail", "https://mail.google.com/")
    assert provider_link("j***o@hotmail.com")[0] == "Outlook"
    # An UNMASKED address is refused outright, so the link can never become a
    # second way for a real inbox to reach the transcript.
    assert provider_link("maria@gmail.com") is None
    # An inbox we have no URL for degrades to no link, never a guess.
    assert provider_link("a***b@clinicaqualquer.com.br") is None


def test_without_a_mask_the_notice_is_just_the_sentence():
    assert booking_gate_body(None) == BOOKING_GATE_SENTENCE


# --------------------------------------------------------------------------
# 2. A code-phase hold reserves nothing (TASK-072); only a Pix blocks
# --------------------------------------------------------------------------


async def _place_payment(db, tenant, conversation, start, *, minutes=30, professional_id=None):
    return await holds.place_hold(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        patient_id=None,
        professional_id=professional_id,
        appointment_type="Consulta",
        insurance=None,
        start_at=start,
        end_at=start + timedelta(minutes=minutes),
        ttl_minutes=30,
        phase="payment",
        charge_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_a_code_wait_blocks_nobody_else(db):
    """TASK-072 bug (a): patient A at the code step must not hide the slot from B.

    Owner, 2026-10-29: "nenhum horário fica guardado para um paciente por tempo".
    The row still carries A's choice (A can still read it back), but every
    "is this busy?" question - the slot lists, the AI tools, the hub's manual
    booking, another patient's own hold - answers "free".
    """
    tenant = await _seed_tenant(db)
    _, mine = await _seed_conversation(db, tenant)
    patient_b, theirs = await _seed_conversation(db, tenant, external_id=OTHER_EXTERNAL_ID)
    start = datetime(2099, 1, 5, 13, 0, tzinfo=UTC)

    assert await _place(db, tenant, mine, start) is not None
    assert (await holds.live_hold(mine.id)).start_at == start  # A's choice is carried

    # B's slot lists: nothing hidden.
    assert await holds.held_windows(tenant.id, None, exclude_conversation=theirs.id) == []
    gate_b = holds.BookingGate(
        tenant_id=tenant.id,
        conversation_id=theirs.id,
        patient_id=patient_b.id,
        external_id=OTHER_EXTERNAL_ID,
        armed=True,
    )
    assert await gate_b.busy_windows(None) == []
    # The clinic's manual booking / reschedule checks: not held.
    async with db() as session:
        assert not await holds.live_hold_overlaps(
            session, tenant.id, start, start + timedelta(minutes=30)
        )
    # And B can carry the very same slot through their own code step.
    assert await _place(db, tenant, theirs, start) is not None
    assert len(await _hold_rows(db, tenant)) == 2


@pytest.mark.asyncio
async def test_a_pix_hold_still_blocks_everybody_else(db):
    """Out of TASK-072's scope and unchanged: a slot waiting for a deposit stays busy."""
    tenant = await _seed_tenant(db)
    _, payer = await _seed_conversation(db, tenant)
    _, other = await _seed_conversation(db, tenant, external_id=OTHER_EXTERNAL_ID)
    start = datetime(2099, 1, 5, 13, 0, tzinfo=UTC)

    assert await _place_payment(db, tenant, payer, start) is not None

    assert await holds.held_windows(tenant.id, None, exclude_conversation=other.id) == [
        (start, start + timedelta(minutes=30))
    ]
    async with db() as session:
        assert await holds.live_hold_overlaps(
            session, tenant.id, start, start + timedelta(minutes=30)
        )
    assert await _place(db, tenant, other, start) is None


@pytest.mark.asyncio
async def test_a_code_hold_cannot_become_a_pix_hold_over_another_pix(db):
    """Two patients may carry the same slot through the code step now; the second one to
    reach the payment step must not get a second Pix hold on top of the first."""
    tenant = await _seed_tenant(db)
    _, first = await _seed_conversation(db, tenant)
    _, second = await _seed_conversation(db, tenant, external_id=OTHER_EXTERNAL_ID)
    start = datetime(2099, 1, 5, 13, 0, tzinfo=UTC)
    held = await _place(db, tenant, second, start)
    assert held is not None
    assert await _place_payment(db, tenant, first, start) is not None

    assert await holds.extend_to_payment(held.id, ttl_minutes=30, charge_id=uuid4()) is None


@pytest.mark.asyncio
async def test_a_pix_hold_on_one_agenda_does_not_hide_another_professionals_slot(db):
    tenant = await _seed_tenant(db)
    _, mine = await _seed_conversation(db, tenant)
    _, theirs = await _seed_conversation(db, tenant, external_id=OTHER_EXTERNAL_ID)
    await _place_payment(db, tenant, mine, datetime(2099, 1, 5, 13, 0, tzinfo=UTC))
    # A different professional's agenda is a different set of windows.
    assert await holds.held_windows(tenant.id, uuid4(), exclude_conversation=theirs.id) == []


# --------------------------------------------------------------------------
# 3-4. Confirming a slot: gated creates nothing, WhatsApp is untouched
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gated_confirmation_touches_neither_google_nor_appointments(db, calls):
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(db, tenant)
    snapshot = _conversation_snapshot("2099-01-05T10:00")
    snapshot.id = conversation.id
    gate = holds.BookingGate(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        patient_id=patient.id,
        external_id=EXTERNAL_ID,
        armed=True,
    )

    result = await fr._handle_confirmation(
        snapshot, tenant, _FakeCalendar(calls), fr.LABEL_CONFIRM, "Maria", gate=gate
    )

    # Nothing on Google. Nothing in `appointments`. A reservation instead.
    assert calls.events == []
    assert await _appointments(db, tenant) == []
    assert result.appointment is None
    assert result.booking_hold is not None
    assert result.booking_hold["email_masked"] == EMAIL_MASKED
    assert result.flow_state is FlowState.AWAITING_EMAIL_CODE
    assert calls.code_requests == [EXTERNAL_ID]
    rows = await _hold_rows(db, tenant)
    assert len(rows) == 1
    assert rows[0].appointment_type == "Consulta"


@pytest.mark.asyncio
async def test_whatsapp_confirmation_is_unchanged_and_reserves_nothing(db, calls):
    """The non-regression the prompt makes mandatory.

    An unarmed gate is what every WhatsApp turn gets. The booking has to come
    out as it did before the gate existed: one event, one appointment dict,
    the confirmation bubble, and no row in `booking_holds` at all.
    """
    tenant = await _seed_tenant(db)
    _, conversation = await _seed_conversation(db, tenant)
    snapshot = _conversation_snapshot("2099-01-05T10:00")
    snapshot.id = conversation.id
    gate = holds.BookingGate(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        patient_id=None,
        external_id="5511988887777",
        armed=False,
    )

    result = await fr._handle_confirmation(
        snapshot, tenant, _FakeCalendar(calls), fr.LABEL_CONFIRM, "Maria", gate=gate
    )

    assert len(calls.events) == 1
    assert result.booking_hold is None
    assert result.appointment is not None
    assert result.appointment["google_event_id"] == "evt-1"
    assert result.flow_state is FlowState.IDLE
    assert "Pronto! Seu agendamento está confirmado." in result.bubbles[0].body
    assert await _hold_rows(db, tenant) == []
    # And the identity leg was never asked for anything.
    assert calls.code_requests == []


@pytest.mark.asyncio
async def test_the_gate_stands_down_when_no_code_can_be_mailed(db, calls, monkeypatch):
    """FAIL-OPEN. A brain-api outage must not stop a clinic taking bookings."""

    async def _unavailable(tenant_id, external_id):
        calls.code_requests.append(external_id)
        return RequestCodeResult(RequestCodeOutcome.UNAVAILABLE)

    monkeypatch.setattr(holds, "request_code", _unavailable)
    tenant = await _seed_tenant(db)
    _, conversation = await _seed_conversation(db, tenant)
    snapshot = _conversation_snapshot("2099-01-05T10:00")
    snapshot.id = conversation.id
    gate = holds.BookingGate(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        patient_id=None,
        external_id=EXTERNAL_ID,
        armed=True,
    )

    result = await fr._handle_confirmation(
        snapshot, tenant, _FakeCalendar(calls), fr.LABEL_CONFIRM, "Maria", gate=gate
    )

    assert result.booking_hold is None
    assert result.appointment is not None  # the patient keeps their booking
    assert await _hold_rows(db, tenant) == []  # and the reservation is handed back


# --------------------------------------------------------------------------
# 4-bis. The WIRING. Proved in production, the hard way.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_gate_arms_on_the_real_reply_path(db, calls, monkeypatch):
    """A Brain-Message booking turn must reach `route()` with an ARMED gate.

    This is the test that was missing, and its absence cost a real booking on a
    real clinic's calendar on 2026-09-21. Every other test in this file builds
    the `BookingGate` by hand, with a tenant id in it — so all of them passed
    while the gate was, in production, never arming at all.

    The bug: `_run_flow` read `reply.tenant_id`, which `_ReplyContext` populates
    only on the identity legs and the degrade paths. The ORDINARY turn — the one
    that books — leaves it None (the terminal `_ReplyContext` of
    `_route_inbound_turn`). `BookingGate.__init__` treats a missing tenant as
    "not armed", so `_handle_confirmation` skipped the gate branch.

    What made it invisible is what makes this test necessary: an unarmed gate
    emits NO log line. Production looked byte-identical to the pre-gate build,
    and the only symptom was an appointment that should not have existed yet.
    So the assertion is on the gate the router is HANDED, not on any message.
    """
    tenant = await _seed_tenant(db)
    _, conversation = await _seed_conversation(db, tenant)

    seen = {}

    async def _capture(*args, **kwargs):
        seen["gate"] = kwargs.get("gate")
        return fr.FlowRouterResult(action="delegate_llm")

    monkeypatch.setattr(workers_ns, "route", _capture)

    # Exactly the shape production builds for an ordinary turn: no tenant_id.
    reply = tasks._ReplyContext(
        channel=CHANNEL_BRAIN_MESSAGE,
        conversation_id=conversation.id,
        patient_ref=EXTERNAL_ID,
        inbound_body="✅ Confirmar",
    )
    assert reply.tenant_id is None, "the regression only reproduces without it"

    await tasks._run_flow(
        reply,
        _conversation_snapshot("2099-01-05T10:00"),
        SimpleNamespace(id=tenant.id),
        None,
        "Maria",
        None,
        tenant=tenant,
    )

    gate = seen["gate"]
    assert gate is not None, "route() must be given a gate"
    assert gate.armed is True, "a Brain-Message booking turn must arm the gate"


@pytest.mark.asyncio
async def test_the_gate_stays_disarmed_on_whatsapp(db, calls, monkeypatch):
    """The same wiring, from the other side: WhatsApp never arms it."""
    tenant = await _seed_tenant(db)
    _, conversation = await _seed_conversation(db, tenant)

    seen = {}

    async def _capture(*args, **kwargs):
        seen["gate"] = kwargs.get("gate")
        return fr.FlowRouterResult(action="delegate_llm")

    monkeypatch.setattr(workers_ns, "route", _capture)
    reply = tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_ref="5511988887777",
        inbound_body="✅ Confirmar",
    )
    await tasks._run_flow(
        reply,
        _conversation_snapshot("2099-01-05T10:00"),
        SimpleNamespace(id=tenant.id),
        None,
        "Maria",
        "5511988887777",
        tenant=tenant,
    )
    assert seen["gate"].armed is False


# --------------------------------------------------------------------------
# 5-6. Promotion: the appointment is born when the code is verified
# --------------------------------------------------------------------------


def _reply(conversation) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        channel=CHANNEL_BRAIN_MESSAGE,
        conversation_id=conversation.id,
        tenant_id=None,
        patient_ref=EXTERNAL_ID,
        inbound_body="123456",
    )


@pytest.mark.asyncio
async def test_the_verified_code_is_what_creates_the_appointment(db, calls, monkeypatch):
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    start = datetime(2099, 1, 5, 13, 0, tzinfo=UTC)
    await _place(db, tenant, conversation, start, patient=patient)
    monkeypatch.setattr(
        workers_ns, "_appointment_calendar", lambda *a, **k: _async(_FakeCalendar(calls))
    )

    extra = await tasks._promote_booking_hold(
        _reply(conversation), tenant=tenant, waba_token=None, redis=None
    )

    # The event and the row exist NOW, and not one moment earlier.
    assert len(calls.events) == 1
    rows = await _appointments(db, tenant)
    assert len(rows) == 1
    assert rows[0].google_event_id == "evt-1"
    assert rows[0].patient_id == patient.id
    # A Portal patient has no phone number; writing the 36-char patient_ref
    # into VARCHAR(32) is the defect 697c24a fixed.
    assert rows[0].phone is None
    # The reservation is gone only after the row exists.
    assert await _hold_rows(db, tenant) == []
    # The post_booking hooks - the PreCheck handoff among them - fire from
    # HERE, which is the first moment the appointment they need exists.
    assert calls.hooks == [(str(rows[0].id), "flow")]
    assert "Pronto! Seu agendamento está confirmado." in extra
    # Rendered in the clinic's timezone, not raw UTC (10:00 local, not 13:00).
    assert "05/01/2099 às 10:00" in extra


@pytest.mark.asyncio
async def test_a_late_code_still_books_a_free_slot(db, calls, monkeypatch):
    """TASK-072: the hold's clock is how long the CHOICE is remembered, never a
    reservation - so a code typed after it ran out still books, when the
    commit-time double check finds the slot free."""
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    await _place(db, tenant, conversation, datetime(2099, 1, 5, 13, 0, tzinfo=UTC), patient=patient)
    await _expire(db, conversation)
    monkeypatch.setattr(
        workers_ns, "_appointment_calendar", lambda *a, **k: _async(_FakeCalendar(calls))
    )

    extra = await tasks._promote_booking_hold(
        _reply(conversation), tenant=tenant, waba_token=None, redis=None
    )

    assert "Pronto! Seu agendamento está confirmado." in extra
    assert len(calls.events) == 1
    assert len(await _appointments(db, tenant)) == 1


@pytest.mark.asyncio
async def test_promotion_never_books_over_another_patients_pix(db, calls, monkeypatch):
    """The double check at commit also asks the holds that DO block (a Pix)."""
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    _, payer = await _seed_conversation(db, tenant, external_id=OTHER_EXTERNAL_ID)
    start = datetime(2099, 1, 5, 13, 0, tzinfo=UTC)
    await _place(db, tenant, conversation, start, patient=patient)
    assert await _place_payment(db, tenant, payer, start) is not None
    monkeypatch.setattr(
        workers_ns, "_appointment_calendar", lambda *a, **k: _async(_FakeCalendar(calls))
    )
    offered = []

    async def _fake_reoffer(reply, tenant_, held, calendar):
        offered.append(held.start_at)
        return SlotTakenAtPromotion(reoffer=fr.FlowRouterResult(action="reply"))

    monkeypatch.setattr(worker_holds, "_slot_taken_reoffer", _fake_reoffer)

    result = await tasks._promote_booking_hold(
        _reply(conversation), tenant=tenant, waba_token=None, redis=None
    )

    assert isinstance(result, SlotTakenAtPromotion)
    assert offered == [start]
    assert calls.events == []
    assert await _appointments(db, tenant) == []


@pytest.mark.asyncio
async def test_an_ungated_conversation_is_left_exactly_as_it_was(db, calls):
    """No reservation ever existed - so promotion has nothing to say and says
    nothing. This is the path every pre-gate booking still takes."""
    tenant = await _seed_tenant(db)
    _, conversation = await _seed_conversation(db, tenant)
    assert (
        await tasks._promote_booking_hold(
            _reply(conversation), tenant=tenant, waba_token=None, redis=None
        )
        is None
    )
    assert calls.events == []
    assert await _appointments(db, tenant) == []


# --------------------------------------------------------------------------
# 7. Bug 1: the LLM may not announce a verification it never made
# --------------------------------------------------------------------------


def test_the_production_hallucination_is_recognised_as_an_unbacked_claim():
    assert unbacked_claim(HALLUCINATION) == ACTION_IDENTITY_VERIFIED
    assert guard_reply(HALLUCINATION) == UNBACKED_CLAIM_MESSAGE


@pytest.mark.parametrize(
    "claim",
    [
        "Pronto — código verificado e sua conta foi ativada. ✅",
        "Verifiquei o seu código, está tudo certo!",
        "Sua conta já está ativa.",
        "Ativei sua conta agora.",
        "Seu e-mail foi verificado com sucesso.",
        "Pagamento confirmado, obrigado!",
        "Recebemos o seu pagamento.",
    ],
)
def test_every_shape_of_the_claim_is_blocked(claim):
    assert guard_reply(claim) == UNBACKED_CLAIM_MESSAGE


@pytest.mark.parametrize(
    "honest",
    [
        "Assim que o código for verificado eu confirmo a sua consulta.",
        "Me manda os 6 dígitos que chegaram no seu e-mail.",
        "Posso te ajudar com mais alguma coisa?",
    ],
)
def test_the_honest_sentences_are_not_touched(honest):
    assert guard_reply(honest) == honest


def test_a_tool_backed_claim_is_allowed_through():
    """The guard is a ratchet on EVIDENCE, not a blocklist of words.

    Today the agent has no identity tool, so nothing can ever prove this - but
    the day one exists it passes its key and the sentence becomes sayable
    without anybody editing a regex.
    """
    assert (
        guard_reply(HALLUCINATION, proven_actions=frozenset({ACTION_IDENTITY_VERIFIED}))
        == HALLUCINATION
    )


@pytest.mark.asyncio
async def test_the_worker_never_delivers_an_llm_verification_claim(db, monkeypatch):
    """The end-to-end form of the checklist item: if the LLM answered this
    turn, what reaches the patient cannot confirm a code or an account.

    Drives the real reply path, so the guard is asserted where it actually has
    to hold - on the `messages` row the patient's console renders.
    """
    tenant = await _seed_tenant(db)
    _, conversation = await _seed_conversation(db, tenant)
    # NOT a first contact: the opening turn is the verbatim greeting and never
    # reaches the model at all, so a greeting would prove nothing here.
    async with db() as session:
        async with session.begin():
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.OUTBOUND,
                    sender=MessageSender.BOT,
                    body="Olá!",
                )
            )

    async def _delegate(*args, **kwargs):
        return False  # the deterministic router hands this turn to the LLM

    async def _hallucinate(*args, **kwargs):
        return HALLUCINATION

    monkeypatch.setattr(workers_ns, "_run_flow", _delegate)
    monkeypatch.setattr(workers_ns, "run_agent", _hallucinate)

    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id, external_id=EXTERNAL_ID, text="123456", patient_name="Maria"
    )
    await tasks._send_bot_reply(reply, redis=None)

    sent = await _outbound(db, tenant)
    assert sent, "the turn must still be answered"
    assert HALLUCINATION not in sent
    assert "verificado" not in sent[-1]
    assert "conta foi ativada" not in sent[-1]
    assert sent[-1] == UNBACKED_CLAIM_MESSAGE


# --------------------------------------------------------------------------
# 8. Bug 2: free text during a HELD wait repeats the card; it does not cost
#    the patient their slot
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_text_during_a_held_wait_repeats_the_card_and_keeps_the_slot(db, calls):
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    await _place(db, tenant, conversation, datetime(2099, 1, 5, 13, 0, tzinfo=UTC), patient=patient)

    # The exact message from the production report: not six digits.
    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id,
        external_id=EXTERNAL_ID,
        text="não recebi o código, pode reenviar?",
        patient_name="Maria",
    )
    await tasks._send_bot_reply(reply, redis=None)

    sent = await _outbound(db, tenant)
    assert BOOKING_GATE_REPROMPT_SENTENCE in sent[-1]
    assert "minutos" not in sent[-1]  # TASK-072: no time-boxed promise
    # Still TASK-003's three-button card, not a bare line: the flattened row
    # carries every label the patient was offered.
    for _, label in CODE_NOTICE_BUTTONS:
        assert label in sent[-1]
    # The window the patient chose is still theirs, and the wait is still on.
    assert await holds.live_hold(conversation.id) is not None
    async with db() as session:
        state = await session.scalar(
            select(Conversation.flow_state).where(Conversation.id == conversation.id)
        )
    assert state is FlowState.AWAITING_EMAIL_CODE
    # No code was verified, and no success was announced.
    assert calls.verified == []
    assert "verificado" not in sent[-1]


@pytest.mark.asyncio
async def test_without_a_reservation_free_text_still_ends_the_wait(db):
    """The pre-gate behaviour, unchanged, for a conversation whose appointment
    was already committed: the account is an offer, so a patient with another
    question must not have to answer this one first."""
    tenant = await _seed_tenant(db)
    _, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )

    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id,
        external_id=EXTERNAL_ID,
        text="quais são os horários da clínica?",
        patient_name="Maria",
    )
    assert reply is not None
    assert reply.pending_code_reprompt is None

    async with db() as session:
        state = await session.scalar(
            select(Conversation.flow_state).where(Conversation.id == conversation.id)
        )
    assert state is FlowState.IDLE


@pytest.mark.asyncio
@pytest.mark.parametrize("v2", [True, False])
async def test_a_promoted_reservation_plans_its_reminders_only_with_the_switch_on(
    db, calls, monkeypatch, v2
):
    """TASK-032 R2: the Portal booking is born here, so its reminders are planned here."""
    spy = HookSpy().install(monkeypatch, reminder_hooks)
    tenant = await _seed_tenant(db)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.reminders_v2_enabled = v2
        await session.commit()
    tenant.reminders_v2_enabled = v2
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    await _place(db, tenant, conversation, datetime(2099, 1, 5, 13, 0, tzinfo=UTC), patient=patient)
    monkeypatch.setattr(
        workers_ns, "_appointment_calendar", lambda *a, **k: _async(_FakeCalendar(calls))
    )

    await tasks._promote_booking_hold(
        _reply(conversation), tenant=tenant, waba_token=None, redis=None
    )

    rows = await _appointments(db, tenant)
    assert spy.calls == ([("booked", rows[0].id)] if v2 else [])


# --------------------------------------------------------------------------
# 9. What a code dead end may say: only a booking that EXISTS "continua
#    marcada" (live test, 2026-10-05)
# --------------------------------------------------------------------------

HELD_START = datetime(2099, 1, 5, 13, 0, tzinfo=UTC)


async def _book(db, tenant, patient, conversation, start, *, status=AppointmentStatus.SCHEDULED):
    async with db() as session:
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            conversation_id=conversation.id,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            appointment_type="Consulta",
            google_event_id=f"gcal-{uuid4().hex[:12]}",
            status=status,
        )
        session.add(appointment)
        await session.commit()
        return appointment


async def _stands(db, tenant, patient, conversation) -> bool:
    async with db() as session:
        return await holds.appointment_stands_in(
            session,
            tenant_id=tenant.id,
            patient_id=patient.id,
            conversation_id=conversation.id,
        )


@pytest.mark.asyncio
async def test_a_held_slot_alone_is_not_an_appointment(db):
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    await _place(db, tenant, conversation, HELD_START, patient=patient)
    assert await _stands(db, tenant, patient, conversation) is False


@pytest.mark.asyncio
async def test_a_booking_committed_before_the_code_stands(db):
    """The paths that still book first: the gate stood down, or a pre-gate booking."""
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    await _book(db, tenant, patient, conversation, HELD_START)
    assert await _stands(db, tenant, patient, conversation) is True


@pytest.mark.asyncio
async def test_an_older_booking_does_not_vouch_for_a_newer_held_slot(db):
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    await _book(db, tenant, patient, conversation, HELD_START - timedelta(days=2))
    await _place(db, tenant, conversation, HELD_START, patient=patient)
    assert await _stands(db, tenant, patient, conversation) is False


@pytest.mark.asyncio
async def test_a_cancelled_booking_does_not_stand(db):
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    await _book(db, tenant, patient, conversation, HELD_START, status=AppointmentStatus.CANCELLED)
    assert await _stands(db, tenant, patient, conversation) is False


@pytest.mark.asyncio
async def test_a_dead_end_on_a_held_slot_says_it_was_not_confirmed(db, calls, monkeypatch):
    """brain-api down while the slot is held: nothing was booked, so say so.

    An older, unrelated booking of the same patient must not turn the answer
    into "sua consulta continua marcada" - the slot they just chose is not it.
    """

    async def _unavailable(tenant_id, external_id, code):
        return VerifyResult(VerifyOutcome.UNAVAILABLE)

    monkeypatch.setattr(workers_ns, "verify_code", _unavailable)
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    await _book(db, tenant, patient, conversation, HELD_START - timedelta(days=2))
    await _place(db, tenant, conversation, HELD_START, patient=patient)

    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id, external_id=EXTERNAL_ID, text="123456", patient_name="Maria"
    )
    await tasks._send_bot_reply(reply, redis=None)

    last = (await _outbound(db, tenant))[-1]
    assert last == CODE_GIVE_UP_UNCONFIRMED_MESSAGE
    assert "continua marcada" not in last


@pytest.mark.asyncio
async def test_resending_the_code_on_a_held_slot_keeps_the_gate_wording(db, calls):
    """"Reenviar código" while the slot is held re-sends the GATE card.

    The card used to come back as the pre-gate notice, opening with "Sua
    consulta já está confirmada!" while nothing had been booked.
    """
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    await _place(db, tenant, conversation, HELD_START, patient=patient)
    # The card the gate put on screen: its ids are what makes the tap valid.
    await tasks._send_booking_gate_notice(
        _reply(conversation),
        {"email_masked": EMAIL_MASKED, "when": "05/01/2099 às 10:00"},
        tenant=tenant,
        waba_token=None,
    )

    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id,
        external_id=EXTERNAL_ID,
        text="↩️ Reenviar código",
        interactive_reply_id=IDENTITY_RESEND_ACTION,
    )
    await tasks._send_bot_reply(reply, redis=None)

    card = (await _outbound(db, tenant))[-1]
    assert calls.code_requests == [EXTERNAL_ID]
    assert "já está confirmada" not in card
    assert BOOKING_GATE_SENTENCE in card
    assert "05/01/2099 às 10:00" in card


# --------------------------------------------------------------------------
# 10. TASK-072 (owner, 2026-10-29): "📩 Mudar e-mail" keeps the booking, and
#     an accepted LGPD consent is never asked again (`Patient.lgpd_accepted`)
# --------------------------------------------------------------------------

NEW_EMAIL_MASKED = "i***o@gmail.com"


def _wire_email_change(monkeypatch, calls):
    """brain-api's two calls on the "Mudar e-mail" leg, faked: cancel the old code, claim
    the new address (a NEW inbox, so `account_exists` is False)."""
    claimed: list[str] = []

    async def _cancel(tenant_id, external_id):
        return True

    async def _claim(tenant_id, external_id, email):
        claimed.append(email)
        return ClaimResult(ClaimOutcome.CLAIMED)

    async def _request(tenant_id, external_id):
        calls.code_requests.append(external_id)
        return RequestCodeResult(RequestCodeOutcome.SENT, email_masked=NEW_EMAIL_MASKED)

    monkeypatch.setattr(workers_ns, "cancel_pending_code", _cancel)
    monkeypatch.setattr(workers_ns, "claim_email", _claim)
    monkeypatch.setattr(workers_ns, "request_code", _request)
    return claimed


async def _portal_turn(tenant, text_, reply_id=None):
    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id,
        external_id=EXTERNAL_ID,
        text=text_,
        interactive_reply_id=reply_id,
        patient_name="Maria",
    )
    await tasks._send_bot_reply(reply, redis=None)


async def _state(db, conversation):
    async with db() as session:
        return await session.scalar(
            select(Conversation.flow_state).where(Conversation.id == conversation.id)
        )


def _no_consent_notice(sent: list[str]) -> None:
    for body in sent:
        assert CONSENT_BUTTON_LABEL not in body
        assert LGPD_TERMS_URL not in body


@pytest.mark.asyncio
async def test_changing_the_email_at_the_code_step_keeps_the_booking(db, calls, monkeypatch):
    """The "Ivo" report: "Mudar e-mail" -> new address -> LGPD asked AGAIN, then the
    booking (16/11 09:30, convênio, serviço) lost and the patient dumped on the menu.

    Now: straight to the code sent to the NEW address, same slot on the card, no LGPD,
    no name question, no menu - and the right code books exactly the chosen slot."""
    claimed = _wire_email_change(monkeypatch, calls)
    monkeypatch.setattr(
        workers_ns, "_appointment_calendar", lambda *a, **k: _async(_FakeCalendar(calls))
    )
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    held = await _place(db, tenant, conversation, HELD_START, patient=patient)
    async with db() as session:
        row = await session.get(BookingHold, held.id)
        row.insurance = "Unimed"
        row.appointment_type = "Consulta de retorno"
        await session.commit()
    await tasks._send_booking_gate_notice(
        _reply(conversation),
        {"email_masked": EMAIL_MASKED, "when": "05/01/2099 às 10:00"},
        tenant=tenant,
        waba_token=None,
    )

    await _portal_turn(tenant, "📩 Mudar e-mail", IDENTITY_CHANGE_EMAIL_ACTION)
    assert await _state(db, conversation) is FlowState.AWAITING_EMAIL
    await _portal_turn(tenant, "ivo.novo@gmail.com")

    sent = await _outbound(db, tenant)
    assert claimed == ["ivo.novo@gmail.com"]
    _no_consent_notice(sent)
    assert NAME_REQUEST_AFTER_EMAIL_MESSAGE not in sent
    # The code card for the NEW inbox, naming the same slot.
    assert BOOKING_GATE_SENTENCE in sent[-1]
    assert NEW_EMAIL_MASKED in sent[-1]
    assert "05/01/2099 às 10:00" in sent[-1]
    assert await _state(db, conversation) is FlowState.AWAITING_EMAIL_CODE
    assert calls.code_requests == [EXTERNAL_ID]
    [kept] = await _hold_rows(db, tenant)
    assert kept.id == held.id
    assert kept.insurance == "Unimed"

    await _portal_turn(tenant, "123456")

    [appointment] = await _appointments(db, tenant)
    assert holds._aware(appointment.start_at) == HELD_START
    assert appointment.insurance == "Unimed"
    assert appointment.appointment_type == "Consulta de retorno"
    assert "Pronto! Seu agendamento está confirmado." in (await _outbound(db, tenant))[-1]
    _no_consent_notice(await _outbound(db, tenant))


@pytest.mark.asyncio
async def test_resending_the_code_refreshes_the_carried_choice(db, calls):
    """A new code for the same booking gives the carried choice a fresh lease."""
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(
        db, tenant, flow_state=FlowState.AWAITING_EMAIL_CODE
    )
    await _place(db, tenant, conversation, HELD_START, patient=patient, ttl=1)
    await tasks._send_booking_gate_notice(
        _reply(conversation),
        {"email_masked": EMAIL_MASKED, "when": "05/01/2099 às 10:00"},
        tenant=tenant,
        waba_token=None,
    )

    await _portal_turn(tenant, "↩️ Reenviar código", IDENTITY_RESEND_ACTION)

    held = await holds.live_hold(conversation.id)
    assert held is not None
    left = (held.expires_at - datetime.now(UTC)).total_seconds() / 60
    assert left > holds.HOLD_TTL_MINUTES - 1


def test_stamping_consent_sets_the_flag_and_clearing_it_unsets_it():
    patient = Patient(tenant_id=uuid4(), external_id=EXTERNAL_ID, lgpd_accepted_at=None)
    assert not patient.lgpd_accepted
    patient.lgpd_accepted_at = datetime.now(UTC)
    assert patient.lgpd_accepted is True
    patient.lgpd_accepted_at = None
    assert patient.lgpd_accepted is False


@pytest.mark.asyncio
async def test_the_flag_alone_is_what_the_consent_gate_reads(db, calls):
    """`lgpd_accepted` True (even with no timestamp) -> the LGPD notice never comes back."""
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(db, tenant)
    async with db() as session:
        row = await session.get(Patient, patient.id)
        row.lgpd_accepted = True
        # Bypass the validator: the timestamp is audit only, the flag is the truth.
        session.add(row)
        await session.execute(
            text("UPDATE patients SET lgpd_accepted_at = NULL WHERE id = :id"),
            {"id": patient.id.hex},
        )
        await session.commit()

    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id, external_id=EXTERNAL_ID, text="oi", patient_name="Maria"
    )
    assert reply is not None
    assert not reply.send_consent_reminder
    assert not reply.send_consent_notice


@pytest.mark.asyncio
async def test_without_the_flag_the_consent_gate_still_asks(db, calls):
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(db, tenant)
    async with db() as session:
        row = await session.get(Patient, patient.id)
        row.lgpd_accepted_at = None
        await session.commit()
        await session.refresh(row)
        assert row.lgpd_accepted is False

    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id, external_id=EXTERNAL_ID, text="oi", patient_name="Maria"
    )
    assert reply is not None
    assert reply.send_consent_reminder or reply.send_consent_notice
