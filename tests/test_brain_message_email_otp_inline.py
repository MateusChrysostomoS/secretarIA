"""The two inline identity steps, and the WhatsApp path proving it never sees them.

Origin: z_prompts/PROMPT_BRAIN_MESSAGE_SECRETARIA_EMAIL_OTP_INLINE.md (wave 2 of
PLANO_LOGIN_SEM_GATE_PACIENTE_NOVO.md). The owner fixed the order:

    link -> chat -> greeting (no LGPD) -> e-mail -> LGPD -> booking -> code

What most of this file defends is the NEGATIVE space around that sentence. Three
things must stay exactly as they were, and each has a test whose only job is to
fail loudly if this feature leaks into it:

  * WhatsApp still greets and asks for consent in the same two messages, in the
    same order, and never makes a single brain-api call on the identity leg;
  * a visitor who already proved an address is never asked again;
  * the post-booking PreCheck handoff (feat36-40) still fires on exactly the
    bookings it fired on before, including on the same event that now also
    triggers the code notice.

Ordering assertions read the outbound `Message` rows rather than a mock's call
list. On Brain-Message the row IS the delivery (`services/channel_sender.py`),
so the rows are literally what the patient's console renders, in the order it
renders them — a stronger statement than "the sender was called".

Fixtures follow tests/test_brain_message_pipeline.py (in-memory SQLite on a
StaticPool).
"""

import json
import os

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

import httpx  # noqa: E402
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
    ConsentEvent,
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    Patient,
    ProcessedEvent,
    Tenant,
)
from secretaria.plugins import (
    pending_identity as plugin,  # noqa: E402
    precheck_handoff,  # noqa: E402
)
from secretaria.plugins.base import PostBookingContext  # noqa: E402
from secretaria.services import pending_identity as pending_service  # noqa: E402
from secretaria.services.channel_sender import (  # noqa: E402
    CHANNEL_BRAIN_MESSAGE,
    CHANNEL_WHATSAPP,
    interactive_history_body,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.greeting_template import (  # noqa: E402
    CONSENT_ACCEPTED_MESSAGE,
    CONSENT_BUTTON_LABEL,
    LGPD_CONSENT_MESSAGE,
)
from secretaria.services.pending_identity import (  # noqa: E402
    CODE_ACCEPTED_MESSAGE,
    CODE_GIVE_UP_MESSAGE,
    CODE_INVALID_MESSAGE,
    CODE_NOTICE_MESSAGE,
    EMAIL_CLAIM_RETRY_MESSAGE,
    EMAIL_INVALID_MESSAGE,
    EMAIL_REQUEST_MESSAGE,
    ClaimOutcome,
    IdentityState,
    RequestCodeOutcome,
    VerifyOutcome,
    VerifyResult,
    parse_code,
    parse_email,
)
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
EXTERNAL_ID = "00000000-0000-4000-8000-000000000001"
EMAIL = "maria@exemplo.com"

# The LGPD notice AS STORED on the Brain-Message channel. A card is recorded
# flattened — body plus "(opções: ...)" — because the next agent turn is
# rebuilt from the `messages` table and a bare body would hide what was
# offered (`services/channel_sender.py::interactive_history_body`). So this,
# not the bare constant, is what the patient's console renders, and asserting
# on it is the stronger statement. WhatsApp assertions still use the raw
# constant: there the row is a history copy and the SEND is the delivery.
LGPD_ROW = interactive_history_body(LGPD_CONSENT_MESSAGE, [CONSENT_BUTTON_LABEL])


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
    """What the identity leg was asked to do this test, in order.

    A recorder rather than a Mock so each assertion names the thing it cares
    about — `calls.claimed == [EMAIL]` reads as the claim that was made.
    """

    def __init__(self) -> None:
        self.probed: list[str] = []
        self.claimed: list[str] = []
        self.verified: list[str] = []
        self.code_requests: list[str] = []


@pytest.fixture
def calls() -> _Calls:
    return _Calls()


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db, calls):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    monkeypatch.setattr(plugin, "async_session_factory", db)
    monkeypatch.setattr(tasks, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))

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

    monkeypatch.setattr(tasks, "resolve_patient_opening_state", _fake_resolve)
    monkeypatch.setattr(tasks, "get_waba_token", _fake_token)
    monkeypatch.setattr(tasks, "get_entitlements", _fake_entitlements)

    # Default identity leg: a brand-new visit that owes an address, every call
    # succeeding. Individual tests override one function at a time, so what a
    # test overrides is exactly what it is about.
    async def _probe(tenant_id, external_id):
        calls.probed.append(external_id)
        return IdentityState.PENDING_UNCLAIMED

    async def _claim(tenant_id, external_id, email):
        calls.claimed.append(email)
        return ClaimOutcome.CLAIMED

    async def _verify(tenant_id, external_id, code):
        calls.verified.append(code)
        return VerifyResult(VerifyOutcome.VERIFIED if code == "123456" else VerifyOutcome.INVALID)

    async def _request(tenant_id, external_id):
        calls.code_requests.append(external_id)
        return RequestCodeOutcome.SENT

    monkeypatch.setattr(tasks, "probe_identity", _probe)
    monkeypatch.setattr(tasks, "claim_email", _claim)
    monkeypatch.setattr(tasks, "verify_code", _verify)
    monkeypatch.setattr(tasks, "request_code", _request)
    monkeypatch.setattr(plugin, "request_code", _request)
    yield


async def _seed_tenant(db, phone_number_id: str = PHONE_NUMBER_ID) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=phone_number_id,
            is_active=True,
            clinic_description="Oftalmologia.",
            initial_flows={},
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _bm_turn(tenant: Tenant, body: str, external_id: str = EXTERNAL_ID):
    """One inbound on the Brain-Message channel, decided AND delivered."""
    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id,
        external_id=external_id,
        text=body,
        patient_name="Maria",
    )
    if reply is not None:
        await tasks._send_bot_reply(reply, redis=None)
    return reply


async def _outbound(db, tenant: Tenant) -> list[str]:
    """Every bot message this tenant's conversation has sent, in order."""
    async with db() as session:
        rows = (
            await session.scalars(
                select(Message.body)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.tenant_id == tenant.id,
                    Message.direction == MessageDirection.OUTBOUND,
                )
                # `created_at` alone is not a total order: two bubbles of the
                # same turn land in the same millisecond, and `Message.id` is a
                # random UUID, so tie-breaking on it shuffles them. SQLite's
                # implicit `rowid` is insertion order, which IS the order the
                # patient's console renders — and render order is the whole
                # point of this file. Test-only, and SQLite-only by
                # construction (these fixtures never run on Postgres).
                .order_by(Message.created_at, text("messages.rowid"))
            )
        ).all()
    return list(rows)


async def _inbound(db, tenant: Tenant) -> list[str]:
    async with db() as session:
        rows = (
            await session.scalars(
                select(Message.body)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.tenant_id == tenant.id,
                    Message.direction == MessageDirection.INBOUND,
                )
                .order_by(Message.created_at, text("messages.rowid"))
            )
        ).all()
    return list(rows)


async def _flow_state(db, tenant: Tenant) -> FlowState:
    async with db() as session:
        return await session.scalar(
            select(Conversation.flow_state).where(Conversation.tenant_id == tenant.id)
        )


async def _patient(db, tenant: Tenant, channel: str = CHANNEL_BRAIN_MESSAGE) -> Patient:
    async with db() as session:
        return await session.scalar(
            select(Patient).where(Patient.tenant_id == tenant.id, Patient.channel == channel)
        )


# --------------------------------------------------------------------------
# 1. Pure: what counts as an address, what counts as a code
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("maria@exemplo.com", "maria@exemplo.com"),
        ("  Maria@Exemplo.COM  ", "maria@exemplo.com"),
        ("mailto:maria@exemplo.com", "maria@exemplo.com"),
        ("meu e-mail é maria@exemplo.com", None),
        ("maria@exemplo", None),
        ("maria arroba exemplo", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_email_accepts_an_address_and_only_an_address(raw, expected) -> None:
    """A sentence CONTAINING an address is not an answer to "qual é o seu e-mail?".

    "meu e-mail antigo era x@y.com, não use esse" is the case that makes
    fishing an address out of prose wrong, not merely imprecise.
    """
    assert parse_email(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("123456", "123456"),
        ("123 456", "123456"),
        ("123-456", "123456"),
        ("12345", None),
        ("1234567", None),
        ("o código é 123456", None),
        ("oi", None),
    ],
)
def test_parse_code_accepts_six_digits_and_nothing_else(raw, expected) -> None:
    """A None is what lets a patient say something else instead of the code."""
    assert parse_code(raw) == expected


async def test_wire_contract_matches_brain_api_internal_endpoints(monkeypatch) -> None:
    """Consumer contract: exact paths, key header and minimal typed payloads."""
    seen: list[tuple[str, dict, str | None]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append((request.url.path, payload, request.headers.get("X-Internal-Api-Key")))
        status = {
            "/internal/brain-message/pending-identity": "pending_unclaimed",
            "/internal/brain-message/pending-email": "pending",
            "/internal/brain-message/pending-otp/request": "sent",
            "/internal/brain-message/pending-otp/verify": "verified",
        }[request.url.path]
        return httpx.Response(200, json={"status": status})

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(_handler)
    monkeypatch.setattr(
        pending_service,
        "get_settings",
        lambda: Settings(
            BRAIN_API_BASE_URL="https://brain-api.test",
            INTERNAL_API_KEY="contract-key",
        ),
    )
    monkeypatch.setattr(
        pending_service.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    tenant_id = uuid4()

    assert (
        await pending_service.probe_identity(tenant_id, EXTERNAL_ID)
        is IdentityState.PENDING_UNCLAIMED
    )
    assert await pending_service.claim_email(tenant_id, EXTERNAL_ID, EMAIL) is ClaimOutcome.CLAIMED
    assert await pending_service.request_code(tenant_id, EXTERNAL_ID) is RequestCodeOutcome.SENT
    result = await pending_service.verify_code(tenant_id, EXTERNAL_ID, "123456")
    assert result.outcome is VerifyOutcome.VERIFIED

    assert [row[0] for row in seen] == [
        "/internal/brain-message/pending-identity",
        "/internal/brain-message/pending-email",
        "/internal/brain-message/pending-otp/request",
        "/internal/brain-message/pending-otp/verify",
    ]
    assert all(row[2] == "contract-key" for row in seen)
    assert seen[2][1] == {"tenant_id": str(tenant_id), "external_id": EXTERNAL_ID}
    assert seen[3][1] == {
        "tenant_id": str(tenant_id),
        "external_id": EXTERNAL_ID,
        "code": "123456",
    }


# --------------------------------------------------------------------------
# 2. The happy path, message by message, in order
# --------------------------------------------------------------------------


async def test_first_contact_asks_the_email_between_the_greeting_and_the_lgpd(db, calls) -> None:
    """CHECKLIST: greeting without LGPD -> e-mail question -> LGPD -> normal flow.

    The exact messages, in the exact order. This is the owner's sentence
    rendered as an assertion, and the reason the bodies are compared in full
    rather than with `in`: a regression that appended the LGPD text to the
    greeting would pass a substring check.
    """
    tenant = await _seed_tenant(db)

    await _bm_turn(tenant, "oi")

    sent = await _outbound(db, tenant)
    assert len(sent) == 2, sent
    assert sent[0].startswith("👋 Olá! Bem-vindo(a) à Clinic!")
    assert LGPD_CONSENT_MESSAGE not in sent[0]
    assert sent[1] == EMAIL_REQUEST_MESSAGE
    assert calls.probed == [EXTERNAL_ID]
    assert await _flow_state(db, tenant) == FlowState.AWAITING_EMAIL

    await _bm_turn(tenant, EMAIL)

    sent = await _outbound(db, tenant)
    assert calls.claimed == [EMAIL], "the address must reach brain-api before consent"
    assert sent[2] == LGPD_ROW
    assert await _flow_state(db, tenant) == FlowState.IDLE

    # ...and the ordinary flow resumes: accepting the terms opens the menu,
    # exactly as it does on WhatsApp. The identity step added messages BEFORE
    # the booking flow and changed nothing inside it.
    await _bm_turn(tenant, CONSENT_BUTTON_LABEL)
    sent = await _outbound(db, tenant)
    assert sent[3].startswith(CONSENT_ACCEPTED_MESSAGE)
    patient = await _patient(db, tenant)
    assert patient.lgpd_accepted_at is not None


async def test_an_unreadable_answer_reasks_and_does_not_advance(db, calls) -> None:
    """No address claimed, no consent sent, still waiting. The re-ask is the turn."""
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")

    await _bm_turn(tenant, "prefiro não dizer")

    sent = await _outbound(db, tenant)
    assert sent[2] == EMAIL_INVALID_MESSAGE
    assert LGPD_ROW not in sent
    assert calls.claimed == []
    assert await _flow_state(db, tenant) == FlowState.AWAITING_EMAIL

    # And a correct address afterwards still works — the re-ask is a loop, not
    # a dead end.
    await _bm_turn(tenant, EMAIL)
    assert calls.claimed == [EMAIL]
    assert (await _outbound(db, tenant))[3] == LGPD_ROW


async def test_a_brain_api_outage_on_the_claim_does_not_advance_to_consent(
    db, calls, monkeypatch
) -> None:
    """The owner's claim -> LGPD order is strict, including during an outage."""
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")

    async def _down(tenant_id, external_id, email):
        calls.claimed.append(email)
        return ClaimOutcome.UNAVAILABLE

    monkeypatch.setattr(tasks, "claim_email", _down)
    await _bm_turn(tenant, EMAIL)

    assert calls.claimed == [EMAIL]
    sent = await _outbound(db, tenant)
    assert sent[2] == EMAIL_CLAIM_RETRY_MESSAGE
    assert LGPD_ROW not in sent
    assert await _flow_state(db, tenant) == FlowState.AWAITING_EMAIL


async def test_claim_race_reprobes_and_accepts_an_already_verified_account(
    db, calls, monkeypatch
) -> None:
    """Another tab can verify between the initial probe and the e-mail claim."""
    states = iter([IdentityState.PENDING_UNCLAIMED, IdentityState.VERIFIED])

    async def _probe(tenant_id, external_id):
        calls.probed.append(external_id)
        return next(states)

    async def _already_done(tenant_id, external_id, email):
        calls.claimed.append(email)
        return ClaimOutcome.NOT_PENDING

    monkeypatch.setattr(tasks, "probe_identity", _probe)
    monkeypatch.setattr(tasks, "claim_email", _already_done)
    tenant = await _seed_tenant(db)

    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)

    sent = await _outbound(db, tenant)
    assert calls.probed == [EXTERNAL_ID, EXTERNAL_ID]
    assert calls.claimed == [EMAIL]
    assert LGPD_ROW not in sent
    assert sent[-1].startswith("Como posso te ajudar?")
    assert await _flow_state(db, tenant) == FlowState.MENU


# --------------------------------------------------------------------------
# 3. The patient who already proved an address
# --------------------------------------------------------------------------


async def test_a_verified_visitor_skips_both_email_and_account_lgpd(db, calls, monkeypatch) -> None:
    """CHECKLIST: a verified account enters the normal menu immediately."""

    async def _probe(tenant_id, external_id):
        calls.probed.append(external_id)
        return IdentityState.VERIFIED

    monkeypatch.setattr(tasks, "probe_identity", _probe)
    tenant = await _seed_tenant(db)

    await _bm_turn(tenant, "oi")

    sent = await _outbound(db, tenant)
    assert len(sent) == 2, sent
    assert sent[1].startswith("Como posso te ajudar?")
    assert EMAIL_REQUEST_MESSAGE not in sent
    assert LGPD_ROW not in sent
    assert await _flow_state(db, tenant) == FlowState.MENU
    assert (await _patient(db, tenant)).lgpd_accepted_at is not None
    async with db() as session:
        event = await session.scalar(
            select(ConsentEvent).where(ConsentEvent.kind == "account_terms_verified")
        )
    assert event is not None


@pytest.mark.parametrize("state", [IdentityState.PENDING_CLAIMED, IdentityState.UNKNOWN])
async def test_a_non_verified_visitor_with_no_email_step_still_gets_lgpd(
    db, calls, monkeypatch, state
) -> None:
    async def _probe(tenant_id, external_id):
        calls.probed.append(external_id)
        return state

    monkeypatch.setattr(tasks, "probe_identity", _probe)
    tenant = await _seed_tenant(db)

    await _bm_turn(tenant, "oi")

    sent = await _outbound(db, tenant)
    assert sent[1] == LGPD_ROW
    assert EMAIL_REQUEST_MESSAGE not in sent


async def test_an_unreachable_probe_falls_back_to_todays_behaviour(db, monkeypatch) -> None:
    """If we cannot ask, consent is the thing that must still happen."""

    async def _probe(tenant_id, external_id):
        return IdentityState.UNAVAILABLE

    monkeypatch.setattr(tasks, "probe_identity", _probe)
    tenant = await _seed_tenant(db)

    await _bm_turn(tenant, "oi")

    assert (await _outbound(db, tenant))[1] == LGPD_ROW


# --------------------------------------------------------------------------
# 4. WhatsApp: the non-regression this feature is measured against
# --------------------------------------------------------------------------


async def test_whatsapp_first_contact_is_byte_for_byte_unchanged(db, calls) -> None:
    """CHECKLIST: the WhatsApp channel does not change at all.

    Greeting then LGPD, in that order, and — the load-bearing part — the
    identity leg is never called. Not "called and ignored": never reached. On
    WhatsApp the phone number IS the identity, and a brain-api round trip per
    first contact would be a cost and a failure mode this channel never had.
    """
    tenant = await _seed_tenant(db)

    sent_bodies: list[str] = []

    class _RecordingClient:
        persists_outbound = False

        async def send_text_message(self, to, body):
            sent_bodies.append(body)
            return {}

        async def send_buttons(self, to, body, buttons):
            sent_bodies.append(body)
            return {}

    original = tasks._tenant_client
    tasks._tenant_client = lambda tenant_, waba_token: _RecordingClient()
    try:
        reply = await tasks._persist_inbound_message(
            phone_number_id=tenant.phone_number_id,
            wa_id=WA_ID,
            patient_name="Maria",
            wam_id="wamid.identity.regression",
            body="oi",
        )
        assert reply is not None
        await tasks._send_bot_reply(reply, redis=None)
    finally:
        tasks._tenant_client = original

    assert len(sent_bodies) == 2, sent_bodies
    assert sent_bodies[0].startswith("👋 Olá! Bem-vindo(a) à Clinic!")
    assert sent_bodies[1] == LGPD_CONSENT_MESSAGE
    assert EMAIL_REQUEST_MESSAGE not in sent_bodies
    assert calls.probed == [], "WhatsApp reached the identity leg"
    assert calls.claimed == []
    assert calls.verified == []


async def test_the_whatsapp_gate_ignores_a_stranded_identity_state(db, calls) -> None:
    """A WhatsApp conversation carrying one of the new states is not intercepted.

    Unreachable in production — nothing writes these states on the WhatsApp
    path — but the gate's guard is a channel check, and this pins that the
    check is what does the work, not the state value. If the guard were ever
    loosened, a WhatsApp patient's "123456" would be eaten as a code.
    """
    tenant = await _seed_tenant(db)
    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id="wamid.stranded.1",
        body="oi",
    )
    assert reply is not None

    async with db() as session:
        async with session.begin():
            conversation = await session.scalar(
                select(Conversation).where(Conversation.tenant_id == tenant.id)
            )
            conversation.flow_state = FlowState.AWAITING_EMAIL_CODE
            patient = await session.scalar(select(Patient).where(Patient.tenant_id == tenant.id))
            patient.lgpd_accepted_at = datetime.now(UTC)

    follow_up = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id="wamid.stranded.2",
        body="123456",
    )

    assert follow_up is not None
    assert follow_up.pending_code is None
    assert calls.verified == []


# --------------------------------------------------------------------------
# 5. The code, after the appointment is already committed
# --------------------------------------------------------------------------


async def _ready_for_code(db, tenant: Tenant) -> None:
    """Get a Brain-Message conversation to the state the post-booking hook leaves."""
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)
    await _bm_turn(tenant, CONSENT_BUTTON_LABEL)
    async with db() as session:
        async with session.begin():
            conversation = await session.scalar(
                select(Conversation).where(Conversation.tenant_id == tenant.id)
            )
            conversation.flow_state = FlowState.AWAITING_EMAIL_CODE


async def test_the_right_code_activates_the_account_and_ends_the_wait(db, calls) -> None:
    """CHECKLIST: the correct code confirms the account."""
    tenant = await _seed_tenant(db)
    await _ready_for_code(db, tenant)

    await _bm_turn(tenant, "123456")

    assert calls.verified == ["123456"]
    assert (await _outbound(db, tenant))[-1] == CODE_ACCEPTED_MESSAGE
    assert await _flow_state(db, tenant) == FlowState.IDLE
    assert "123456" not in await _inbound(db, tenant)
    assert (await _inbound(db, tenant))[-1] == "[código oculto]"


async def test_a_wrong_code_reprompts_and_keeps_the_patient_able_to_retry(db, calls) -> None:
    """CHECKLIST: a wrong code allows another attempt, within brain-api's budget.

    The state SURVIVES a rejection — that is what makes the next six digits
    read as a code rather than a menu tap. The attempt budget itself is
    brain-api's and is deliberately not mirrored here, so this test asserts the
    retry is POSSIBLE, never how many remain.
    """
    tenant = await _seed_tenant(db)
    await _ready_for_code(db, tenant)

    await _bm_turn(tenant, "000000")

    assert calls.verified == ["000000"]
    assert (await _outbound(db, tenant))[-1] == CODE_INVALID_MESSAGE
    assert await _flow_state(db, tenant) == FlowState.AWAITING_EMAIL_CODE

    await _bm_turn(tenant, "123456")

    assert calls.verified == ["000000", "123456"]
    assert (await _outbound(db, tenant))[-1] == CODE_ACCEPTED_MESSAGE
    assert await _flow_state(db, tenant) == FlowState.IDLE


async def test_a_dead_end_says_the_appointment_stands_and_frees_the_conversation(
    db, monkeypatch
) -> None:
    """NOT_PENDING / NO_EMAIL / UNAVAILABLE: nothing the patient can act on.

    The state is dropped rather than held, because holding it would trap a
    patient in a prompt no answer can satisfy.
    """

    async def _gone(tenant_id, external_id, code):
        return VerifyResult(VerifyOutcome.UNAVAILABLE)

    monkeypatch.setattr(tasks, "verify_code", _gone)
    tenant = await _seed_tenant(db)
    await _ready_for_code(db, tenant)

    await _bm_turn(tenant, "123456")

    assert (await _outbound(db, tenant))[-1] == CODE_GIVE_UP_MESSAGE
    assert await _flow_state(db, tenant) == FlowState.IDLE


async def test_saying_something_else_leaves_the_code_state_immediately(db, calls) -> None:
    """The second, non-clock exit: the account is an offer, not a gate.

    A patient who wants to ask something after booking must not have to answer
    the code prompt first. Nothing is verified, no code message is re-sent, and
    the turn routes normally.
    """
    tenant = await _seed_tenant(db)
    await _ready_for_code(db, tenant)
    before = await _outbound(db, tenant)

    await _bm_turn(tenant, "quero remarcar")

    assert calls.verified == []
    after = await _outbound(db, tenant)
    assert CODE_INVALID_MESSAGE not in after[len(before) :]
    assert CODE_NOTICE_MESSAGE not in after[len(before) :]
    assert await _flow_state(db, tenant) != FlowState.AWAITING_EMAIL_CODE


# --------------------------------------------------------------------------
# 6. The time-bounded exits (conversation-flow-state's invariant)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", [FlowState.AWAITING_EMAIL, FlowState.AWAITING_EMAIL_CODE])
def test_both_new_states_expire_on_silence_alone(state) -> None:
    """CHECKLIST: each state has an exit that needs neither patient nor agent.

    `Conversation` is one row per (tenant, patient) forever, so a state nobody
    clears is permanent, not merely long-lived — the failure the LLM floor was
    added to close. Asserted directly on the floor function so the clock is an
    argument rather than something to freeze.
    """
    conversation = SimpleNamespace(flow_state=state)
    tenant = SimpleNamespace(initial_flows={})

    fresh = datetime.now(UTC) - timedelta(minutes=5)
    assert tasks._expire_stale_pending_identity_state(conversation, tenant, fresh) is False
    assert conversation.flow_state == state

    stale = datetime.now(UTC) - timedelta(hours=3)
    assert tasks._expire_stale_pending_identity_state(conversation, tenant, stale) is True
    assert conversation.flow_state == FlowState.IDLE


@pytest.mark.parametrize("state", [FlowState.IDLE, FlowState.LLM, FlowState.SERVICE_CATALOG])
def test_the_floor_touches_nothing_it_does_not_own(state) -> None:
    """`FlowState.LLM` has its OWN floor with its own budget; this one must not race it."""
    conversation = SimpleNamespace(flow_state=state)
    tenant = SimpleNamespace(initial_flows={})
    stale = datetime.now(UTC) - timedelta(days=7)

    assert tasks._expire_stale_pending_identity_state(conversation, tenant, stale) is False
    assert conversation.flow_state == state


async def test_an_abandoned_email_step_offers_a_bounded_resume(db) -> None:
    """After the budget, exit the state and ask explicitly before re-entering."""
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")
    assert await _flow_state(db, tenant) == FlowState.AWAITING_EMAIL

    # Age every message in the conversation: `last_activity_at` is
    # max(Message.created_at), read before this turn's inbound is written.
    async with db() as session:
        async with session.begin():
            for message in (await session.scalars(select(Message))).all():
                message.created_at = datetime.now(UTC) - timedelta(hours=5)

    await _bm_turn(tenant, "oi de novo")

    offer = (await _outbound(db, tenant))[-1]
    assert "última conversa" in offer
    assert "✅ Sim" in offer and "❌ Não" in offer
    assert await _flow_state(db, tenant) == FlowState.IDLE

    await _bm_turn(tenant, "Sim")

    assert (await _outbound(db, tenant))[-1] == EMAIL_REQUEST_MESSAGE
    assert await _flow_state(db, tenant) == FlowState.AWAITING_EMAIL


async def test_an_abandoned_code_step_can_stop_without_touching_the_booking(db) -> None:
    """The post-booking wait expires, offers Sim/Não, and a No keeps the visit."""
    tenant = await _seed_tenant(db)
    await _ready_for_code(db, tenant)
    patient = await _patient(db, tenant)
    appointment = await _booked(db, tenant, patient)

    async with db() as session:
        async with session.begin():
            for message in (await session.scalars(select(Message))).all():
                message.created_at = datetime.now(UTC) - timedelta(hours=5)

    await _bm_turn(tenant, "voltei")

    offer = (await _outbound(db, tenant))[-1]
    assert "consulta continua marcada" in offer
    assert await _flow_state(db, tenant) == FlowState.IDLE

    await _bm_turn(tenant, "Não")

    assert (await _outbound(db, tenant))[-1] == CODE_GIVE_UP_MESSAGE
    async with db() as session:
        assert await session.get(Appointment, appointment.id) is not None


# --------------------------------------------------------------------------
# 7. The post-booking hook, and the PreCheck handoff beside it
# --------------------------------------------------------------------------


async def _booked(db, tenant: Tenant, patient: Patient) -> Appointment:
    async with db() as session:
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            start_at=datetime.now(UTC) + timedelta(days=1),
            end_at=datetime.now(UTC) + timedelta(days=1, minutes=30),
            appointment_type="Consulta",
            # NOT NULL on this table: an appointment exists because it is on a
            # calendar. Synthetic here — nothing in these tests reads it.
            google_event_id=f"gcal-{uuid4().hex[:12]}",
        )
        session.add(appointment)
        await session.commit()
        await session.refresh(appointment)
        return appointment


def _ctx(tenant, patient, appointment) -> PostBookingContext:
    return PostBookingContext(
        tenant=tenant,
        patient=patient,
        appointment=appointment,
        waba_token=None,
        source="flow",
    )


async def test_a_booking_by_an_unverified_visitor_gets_the_code_notice(db, calls) -> None:
    """CHECKLIST: appointment committed with no proven address -> the code notice.

    And the conversation ENTERS the state in the same breath, so the next six
    digits the patient types are read as a code.
    """
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)
    patient = await _patient(db, tenant)
    appointment = await _booked(db, tenant, patient)

    await plugin._post_booking(_ctx(tenant, patient, appointment))

    assert calls.code_requests == [EXTERNAL_ID]
    assert (await _outbound(db, tenant))[-1] == CODE_NOTICE_MESSAGE
    assert await _flow_state(db, tenant) == FlowState.AWAITING_EMAIL_CODE


async def test_the_code_notice_is_sent_at_most_once_per_appointment(db, calls) -> None:
    """An arq retry of run_post_booking_hooks must not mail a second code."""
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)
    patient = await _patient(db, tenant)
    appointment = await _booked(db, tenant, patient)
    ctx = _ctx(tenant, patient, appointment)

    await plugin._post_booking(ctx)
    await plugin._post_booking(ctx)

    assert calls.code_requests == [EXTERNAL_ID]
    assert (await _outbound(db, tenant)).count(CODE_NOTICE_MESSAGE) == 1


@pytest.mark.parametrize(
    "outcome",
    [
        RequestCodeOutcome.NOT_PENDING,
        RequestCodeOutcome.NO_EMAIL,
        RequestCodeOutcome.UNAVAILABLE,
    ],
)
async def test_no_code_no_message_and_the_claim_goes_back(db, monkeypatch, outcome) -> None:
    """Silence is the failure mode: never promise a code that was not mailed.

    The ledger row is RELEASED so an arq retry gets a real second chance —
    asserted directly, because a held claim would turn one bad minute into a
    permanently missing account.
    """

    async def _no(tenant_id, external_id):
        return outcome

    monkeypatch.setattr(plugin, "request_code", _no)
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)
    patient = await _patient(db, tenant)
    appointment = await _booked(db, tenant, patient)

    await plugin._post_booking(_ctx(tenant, patient, appointment))

    assert CODE_NOTICE_MESSAGE not in await _outbound(db, tenant)
    assert await _flow_state(db, tenant) != FlowState.AWAITING_EMAIL_CODE
    async with db() as session:
        held = await session.scalar(
            select(ProcessedEvent.id).where(
                ProcessedEvent.event_id == f"pending_identity:{appointment.id}"
            )
        )
    assert held is None, "the claim was kept although nothing was sent"


async def test_a_whatsapp_booking_never_reaches_the_identity_leg(db, calls) -> None:
    """CHECKLIST (WhatsApp non-regression, post-booking half).

    The guard is the first statement in the hook, so a WhatsApp booking cannot
    cost a network call, a ledger row or a message.
    """
    tenant = await _seed_tenant(db)
    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id="wamid.booking.wa",
        body="oi",
    )
    assert reply is not None
    patient = await _patient(db, tenant, channel=CHANNEL_WHATSAPP)
    appointment = await _booked(db, tenant, patient)

    await plugin._post_booking(_ctx(tenant, patient, appointment))

    assert calls.code_requests == []
    async with db() as session:
        held = await session.scalar(
            select(ProcessedEvent.id).where(
                ProcessedEvent.event_id == f"pending_identity:{appointment.id}"
            )
        )
    assert held is None


async def test_the_precheck_handoff_still_fires_exactly_as_before(db, calls, monkeypatch) -> None:
    """CHECKLIST: feat36-40 is untouched, including on the shared event.

    Both hooks are run against the same committed appointment and each is
    asserted separately. The two are disjoint by channel — `precheck_handoff`
    needs a `wa_id`, which a Brain-Message row does not have — so this test
    runs the pair TWICE, once per channel, and pins that exactly one of them
    speaks each time. That is the property a future change would break
    silently.
    """
    handoffs: list[str] = []
    sent_wa: list[str] = []

    async def _handoff(tenant_id, phone, **kwargs):
        handoffs.append(phone)
        return SimpleNamespace(outcome=precheck_handoff.HandoffOutcome.SEEDED)

    class _Client:
        @classmethod
        def for_tenant(cls, tenant_, token):
            return cls()

        async def send_text_message(self, to, body):
            sent_wa.append(body)
            return {}

    monkeypatch.setattr(precheck_handoff, "async_session_factory", db)
    monkeypatch.setattr(precheck_handoff, "request_precheck_handoff", _handoff)
    monkeypatch.setattr(precheck_handoff, "WhatsAppClient", _Client)
    monkeypatch.setattr(
        precheck_handoff,
        "get_settings",
        lambda: Settings(PRECHECK_WHATSAPP_NUMBER="5511900000000"),
    )

    tenant = await _seed_tenant(db)

    # --- WhatsApp booking: PreCheck speaks, the identity hook is silent ---
    wa_reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id="wamid.pair.wa",
        body="oi",
    )
    assert wa_reply is not None
    wa_patient = await _patient(db, tenant, channel=CHANNEL_WHATSAPP)
    wa_appointment = await _booked(db, tenant, wa_patient)
    wa_ctx = _ctx(tenant, wa_patient, wa_appointment)

    await plugin._post_booking(wa_ctx)
    await precheck_handoff._post_booking(wa_ctx)

    assert handoffs == [WA_ID], "the PreCheck handoff stopped firing on WhatsApp"
    assert len(sent_wa) == 1
    assert calls.code_requests == []

    # --- Brain-Message booking: the identity hook speaks, PreCheck is silent ---
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)
    bm_patient = await _patient(db, tenant)
    bm_appointment = await _booked(db, tenant, bm_patient)
    bm_ctx = _ctx(tenant, bm_patient, bm_appointment)

    await plugin._post_booking(bm_ctx)
    await precheck_handoff._post_booking(bm_ctx)

    assert calls.code_requests == [EXTERNAL_ID]
    assert (await _outbound(db, tenant))[-1] == CODE_NOTICE_MESSAGE
    assert handoffs == [WA_ID], "PreCheck tried to reach a patient with no phone number"
    assert len(sent_wa) == 1
