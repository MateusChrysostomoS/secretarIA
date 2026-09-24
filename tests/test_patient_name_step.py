"""The explicit name question (services/patient_name.py), on both channels.

Origin: z_prompts/PROMPT_BRAIN_MESSAGE_ABERTURA_EMAIL_NOME_2_SECRETARIA.md. The
owner's decisions, pinned here one test at a time:

    WhatsApp:  greeting -> NAME -> LGPD      (always on a first contact, even
                                              with a Meta profile name)
    Portal:    greeting -> e-mail -> NAME -> LGPD   (new address)
               greeting -> e-mail -> CODE            (address already an account)

and the requirement that made the owner ask for this explicitly: the name the
patient types must pass through `pseudonymize-core` before any LLM prompt.
The last section proves that with the REAL pipeline — the history loaded from
the database and scrubbed by the pseudonymizer `load_pseudonymizer` builds —
not by reading the code.

Fixtures follow tests/test_brain_message_email_otp_inline.py (in-memory SQLite
on a StaticPool, rows read back as the delivery on Brain-Message).
"""

import json
import os
import re

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import graph  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    ConsentEvent,
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    Patient,
    Tenant,
)
from secretaria.services import (
    pending_identity as pending_service,  # noqa: E402
    pii_pseudonymization as pii_store,  # noqa: E402
)
from secretaria.services.channel_sender import (  # noqa: E402
    CHANNEL_BRAIN_MESSAGE,
    CHANNEL_WHATSAPP,
    interactive_history_body,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.greeting_template import (  # noqa: E402
    CONSENT_BUTTON_LABEL,
    CONSENT_REMINDER_MESSAGE,
    LGPD_CONSENT_MESSAGE,
)
from secretaria.services.patient_name import (  # noqa: E402
    NAME_ANSWER_LLM_PLACEHOLDER,
    NAME_INVALID_MESSAGE,
    NAME_PAUSED_MESSAGE,
    NAME_REQUEST_AFTER_EMAIL_MESSAGE,
    NAME_REQUEST_MESSAGE,
    parse_patient_name,
)
from secretaria.services.pending_identity import (  # noqa: E402
    ACCOUNT_CODE_GIVE_UP_MESSAGE,
    CODE_ACCEPTED_MESSAGE,
    CODE_NOTICE_BUTTONS,
    EMAIL_REQUEST_MESSAGE,
    EXISTING_ACCOUNT_SENTENCE,
    ClaimOutcome,
    ClaimResult,
    IdentityState,
    RequestCodeOutcome,
    RequestCodeResult,
    VerifyOutcome,
    VerifyResult,
    existing_account_code_body,
)
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
EXTERNAL_ID = "00000000-0000-4000-8000-000000000001"
EMAIL = "maria@exemplo.com"
EMAIL_MASKED = "m***a@exemplo.com"
# Meta's `contact.profile.name` — the value the owner does NOT trust.
PROFILE_NAME = "Mari 🌸"
LGPD_ROW = interactive_history_body(LGPD_CONSENT_MESSAGE, [CONSENT_BUTTON_LABEL])


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


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
        self.probed: list[str] = []
        self.claimed: list[str] = []
        self.verified: list[str] = []
        self.code_requests: list[str] = []
        # What the NEXT claim answers; tests flip it to "already an account".
        self.claim_result = ClaimResult(ClaimOutcome.CLAIMED)
        self.probe_states: list[IdentityState] = []
        # The name brain-api returns with a verified code (the ACCOUNT's, from
        # another clinic), and every name this side reported back to it.
        self.account_name: str | None = None
        self.reported: list[tuple] = []


@pytest.fixture
def calls() -> _Calls:
    return _Calls()


class _WireClient:
    """The WhatsApp side: records every send, never touches the network."""

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


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db, calls):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    monkeypatch.setattr(graph, "async_session_factory", db)
    monkeypatch.setattr(pii_store, "async_session_factory", db)
    monkeypatch.setattr(tasks, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    _WireClient.sends = []
    monkeypatch.setattr(tasks, "WhatsAppClient", _WireClient)

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

    async def _probe(tenant_id, external_id):
        calls.probed.append(external_id)
        if calls.probe_states:
            return calls.probe_states.pop(0)
        return IdentityState.PENDING_UNCLAIMED

    async def _claim(tenant_id, external_id, email):
        calls.claimed.append(email)
        return calls.claim_result

    async def _verify(tenant_id, external_id, code):
        calls.verified.append(code)
        if code != "123456":
            return VerifyResult(VerifyOutcome.INVALID)
        return VerifyResult(VerifyOutcome.VERIFIED, patient_name=calls.account_name)

    async def _request(tenant_id, external_id):
        calls.code_requests.append(external_id)
        return RequestCodeResult(RequestCodeOutcome.SENT, email_masked=EMAIL_MASKED)

    async def _report(tenant_id, external_id, name):
        calls.reported.append((external_id, name))
        return True

    monkeypatch.setattr(tasks, "report_name", _report)
    monkeypatch.setattr(tasks, "probe_identity", _probe)
    monkeypatch.setattr(tasks, "claim_email", _claim)
    monkeypatch.setattr(tasks, "verify_code", _verify)
    monkeypatch.setattr(tasks, "request_code", _request)
    yield


async def _seed_tenant(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            clinic_description="Oftalmologia.",
            initial_flows={},
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


_wam_seq = iter(range(1, 10_000))


async def _wa_turn(tenant: Tenant, body: str, *, profile_name: str | None = PROFILE_NAME):
    """One WhatsApp inbound, decided AND delivered through the fake client."""
    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name=profile_name,
        wam_id=f"wamid.in.{next(_wam_seq)}",
        body=body,
    )
    if reply is not None:
        await tasks._send_bot_reply(reply, redis=None)
    return reply


async def _bm_turn(tenant: Tenant, body: str):
    """One Brain-Message inbound, decided AND delivered (the row IS delivery)."""
    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id, external_id=EXTERNAL_ID, text=body, patient_name=None
    )
    if reply is not None:
        await tasks._send_bot_reply(reply, redis=None)
    return reply


async def _outbound(db, tenant: Tenant) -> list[str]:
    async with db() as session:
        rows = await session.scalars(
            select(Message.body)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.tenant_id == tenant.id,
                Message.direction == MessageDirection.OUTBOUND,
            )
            .order_by(Message.created_at, text("messages.rowid"))
        )
        return list(rows.all())


async def _conversation(db, tenant: Tenant) -> Conversation:
    async with db() as session:
        return await session.scalar(select(Conversation).where(Conversation.tenant_id == tenant.id))


async def _patient(db, tenant: Tenant, channel: str) -> Patient:
    async with db() as session:
        return await session.scalar(
            select(Patient).where(Patient.tenant_id == tenant.id, Patient.channel == channel)
        )


async def _age_conversation(db, tenant: Tenant, minutes: int) -> None:
    """Push every message back in time: the silence the TTL floor measures."""
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            await session.execute(
                update(Message)
                .where(Message.conversation_id == conversation.id)
                .values(created_at=datetime.now(UTC) - timedelta(minutes=minutes))
            )


# --------------------------------------------------------------------------
# 1. Pure: what counts as a name
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Maria Silva", "Maria Silva"),
        ("  maria   DA silva ", "Maria da Silva"),
        ("joão pedro", "João Pedro"),
        ("Meu nome é Ana Paula.", "Ana Paula"),
        ("me chamo carlos", "Carlos"),
        ("sou a Beatriz", "Beatriz"),
        ("Maria-Clara D'Ávila", "Maria-Clara D'Ávila"),
        ("Zé", "Zé"),
        # Refused: not a name, so the patient is asked once more.
        ("123456", None),
        ("😀😀", None),
        ("maria@exemplo.com", None),
        ("/menu", None),
        ("oi", None),
        ("Bom dia", None),
        ("quero agendar uma consulta", None),
        ("Maria obrigada", None),
        ("da Silva", None),
        ("a b c d e f g", None),
        # Sentences that the first version stored as names (review finding 1).
        ("Vocês atendem Unimed", None),
        ("Tenho uma dúvida", None),
        ("Pode ser amanhã", None),
        ("sou a maria, e você?", None),
        ("É a Maria", None),
        ("Eh isso", None),
        ("Ana, tudo bem?", None),
        ("Maria Clara da Silva Santos", "Maria Clara da Silva Santos"),
        # The menu's own words (2026-09-24): what the patient wants, not who they are.
        ("Serviços e Custo", None),
        ("servicos e custo", None),
        ("Outro", None),
        ("Escolher médico", None),
        ("Escolher serviço", None),
        ("Particular", None),
        ("Outro convênio", None),
        # ... without catching real names that merely CONTAIN one of those words.
        ("Custódio Lima", "Custódio Lima"),
        ("Outília Souza", "Outília Souza"),
        ("", None),
        (None, None),
    ],
)
def test_parse_patient_name(raw, expected) -> None:
    assert parse_patient_name(raw) == expected


def test_a_clinics_custom_menu_label_is_never_a_name() -> None:
    """Labels come from `initial_flows`; matched ignoring case, accents and emoji."""
    # Labels that WOULD pass as a name without `not_names` (no refused word in them).
    labels = ["Lentes Esclerais", "✅ Harmonização"]
    assert parse_patient_name("Lentes Esclerais") == "Lentes Esclerais"

    assert parse_patient_name("lentes esclerais", not_names=labels) is None
    assert parse_patient_name("Harmonizacao", not_names=labels) is None
    # Only the WHOLE answer: a real name is not refused for sharing a word.
    assert parse_patient_name("Ana Lentes", not_names=labels) == "Ana Lentes"


async def test_whatsapp_a_menu_label_as_the_name_answer_is_reasked(db, calls) -> None:
    """The live symptom: "Serviços e Custo" answered to "qual é o seu nome?"."""
    tenant = await _seed_tenant(db)
    await _wa_turn(tenant, "oi")

    await _wa_turn(tenant, "Serviços e Custo")

    assert _WireClient.sends[-1] == ("text", NAME_INVALID_MESSAGE, None)
    assert (await _patient(db, tenant, CHANNEL_WHATSAPP)).name == PROFILE_NAME
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_NAME


async def test_portal_a_custom_menu_label_as_the_name_answer_is_reasked(db, calls) -> None:
    """A clinic's own button text is refused too, and nothing reaches brain-api."""
    tenant = await _seed_tenant(db)
    async with db() as session:
        async with session.begin():
            row = await session.get(Tenant, tenant.id)
            row.initial_flows = {**(row.initial_flows or {}), "buttons": ["Lentes Esclerais"]}
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)

    await _bm_turn(tenant, "lentes esclerais")

    assert (await _outbound(db, tenant))[-1] == NAME_INVALID_MESSAGE
    assert (await _patient(db, tenant, CHANNEL_BRAIN_MESSAGE)).name is None
    assert calls.reported == []


def test_the_known_address_card_names_the_mask_and_no_consultation() -> None:
    """The KNOWN-address wording: owner's sentence, the mask, no appointment."""
    body = existing_account_code_body(EMAIL_MASKED)

    assert body.startswith(EXISTING_ACCOUNT_SENTENCE)
    assert EMAIL_MASKED in body
    assert "consulta" not in body.casefold()
    # A raw address handed in by mistake is refused, never rendered.
    assert EMAIL not in existing_account_code_body(EMAIL)
    assert "o seu e-mail" in existing_account_code_body(EMAIL)


async def test_claim_email_reads_account_exists_and_the_mask_best_effort(monkeypatch) -> None:
    """The consumer side of brain-api's CHECKPOINT §3, over a mocked wire."""
    bodies = iter(
        [
            {"status": "claimed", "account_exists": True, "email_masked": EMAIL_MASKED},
            {"status": "claimed", "account_exists": False, "email_masked": None},
            # A mask that is not visibly masked (a masking bug upstream).
            {"status": "claimed", "account_exists": True, "email_masked": EMAIL},
            # Not a boolean: read as "a brain-api that predates the field".
            {"status": "claimed", "account_exists": "yes"},
            {"status": "claimed"},
        ]
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["email"] == EMAIL
        return httpx.Response(200, json=next(bodies))

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(_handler)
    monkeypatch.setattr(
        pending_service,
        "get_settings",
        lambda: Settings(BRAIN_API_BASE_URL="https://brain-api.test", INTERNAL_API_KEY="k"),
    )
    monkeypatch.setattr(
        pending_service.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    tid = uuid4()

    known = await pending_service.claim_email(tid, EXTERNAL_ID, EMAIL)
    assert (known.outcome, known.account_exists, known.email_masked) == (
        ClaimOutcome.CLAIMED,
        True,
        EMAIL_MASKED,
    )
    new = await pending_service.claim_email(tid, EXTERNAL_ID, EMAIL)
    assert (new.account_exists, new.email_masked) == (False, None)
    leaky = await pending_service.claim_email(tid, EXTERNAL_ID, EMAIL)
    assert (leaky.account_exists, leaky.email_masked) == (True, None)
    for _ in range(2):
        old = await pending_service.claim_email(tid, EXTERNAL_ID, EMAIL)
        assert (old.outcome, old.account_exists) == (ClaimOutcome.CLAIMED, False)


# --------------------------------------------------------------------------
# 2. WhatsApp: greeting -> name -> LGPD, never an e-mail
# --------------------------------------------------------------------------


async def test_whatsapp_first_contact_asks_the_name_even_with_a_profile_name(db, calls) -> None:
    """CHECKLIST: name question between greeting and LGPD; the typed name wins."""
    tenant = await _seed_tenant(db)

    await _wa_turn(tenant, "oi")

    assert len(_WireClient.sends) == 2, _WireClient.sends
    assert _WireClient.sends[0][1].startswith("👋 Olá! Bem-vindo(a) à Clinic!")
    assert _WireClient.sends[1] == ("text", NAME_REQUEST_MESSAGE, None)
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_NAME
    # The profile name is still there as a fallback until the answer arrives.
    assert (await _patient(db, tenant, CHANNEL_WHATSAPP)).name == PROFILE_NAME

    await _wa_turn(tenant, "maria  da silva")

    assert (await _patient(db, tenant, CHANNEL_WHATSAPP)).name == "Maria da Silva"
    assert _WireClient.sends[2] == ("buttons", LGPD_CONSENT_MESSAGE, [CONSENT_BUTTON_LABEL])
    assert (await _conversation(db, tenant)).flow_state == FlowState.IDLE

    # A later profile name never overwrites the typed one.
    await _wa_turn(tenant, CONSENT_BUTTON_LABEL, profile_name="Outro Perfil")
    assert (await _patient(db, tenant, CHANNEL_WHATSAPP)).name == "Maria da Silva"

    # CHECKLIST: WhatsApp gained no e-mail step — the identity leg is untouched.
    bodies = [body for _kind, body, _buttons in _WireClient.sends]
    assert EMAIL_REQUEST_MESSAGE not in bodies
    # ... and never reports the name to brain-api: WhatsApp has no Portal account.
    assert calls.reported == []
    assert NAME_REQUEST_AFTER_EMAIL_MESSAGE not in bodies
    assert (calls.probed, calls.claimed, calls.code_requests, calls.verified) == ([], [], [], [])


async def test_whatsapp_an_unreadable_answer_reasks_once_then_moves_on(db) -> None:
    """Asked twice at most: the name must never become a wall before consent."""
    tenant = await _seed_tenant(db)
    await _wa_turn(tenant, "oi")

    await _wa_turn(tenant, "quero agendar uma consulta")

    assert _WireClient.sends[-1] == ("text", NAME_INVALID_MESSAGE, None)
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_NAME

    await _wa_turn(tenant, "12345")

    assert _WireClient.sends[-1] == ("buttons", LGPD_CONSENT_MESSAGE, [CONSENT_BUTTON_LABEL])
    conversation = await _conversation(db, tenant)
    assert (conversation.flow_state, conversation.flow_step) == (FlowState.IDLE, None)
    # No name typed: the profile name stays as the only thing we have.
    assert (await _patient(db, tenant, CHANNEL_WHATSAPP)).name == PROFILE_NAME


async def test_whatsapp_returning_patient_with_a_name_is_never_asked_again(db) -> None:
    """CHECKLIST: a patient we already know is not asked their name again.

    Both ways a known patient can come back: after consent (ordinary return),
    and a first contact AGAIN on an existing row whose history was wiped.
    """
    tenant = await _seed_tenant(db)
    await _wa_turn(tenant, "oi")
    await _wa_turn(tenant, "Ana Souza")
    await _wa_turn(tenant, CONSENT_BUTTON_LABEL)
    before = len(_WireClient.sends)

    await _wa_turn(tenant, "oi de novo")

    bodies = [body for _kind, body, _buttons in _WireClient.sends[before:]]
    assert NAME_REQUEST_MESSAGE not in bodies
    assert (await _conversation(db, tenant)).flow_state != FlowState.AWAITING_NAME

    # History wiped, consent not given: the row exists AND has a name.
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            await session.execute(
                Message.__table__.delete().where(Message.conversation_id == conversation.id)
            )
            patient = await session.get(Patient, conversation.patient_id)
            patient.lgpd_accepted_at = None
    before = len(_WireClient.sends)

    await _wa_turn(tenant, "oi")

    assert _WireClient.sends[before + 1] == (
        "buttons",
        LGPD_CONSENT_MESSAGE,
        [CONSENT_BUTTON_LABEL],
    )
    assert (await _patient(db, tenant, CHANNEL_WHATSAPP)).name == "Ana Souza"


async def test_whatsapp_silence_expires_the_name_wait_and_offers_to_resume(db) -> None:
    """CHECKLIST: silence during AWAITING_NAME expires within the ceiling."""
    tenant = await _seed_tenant(db)
    await _wa_turn(tenant, "oi")
    await _age_conversation(db, tenant, minutes=tasks.pending_identity_ttl_minutes(tenant) + 5)

    await _wa_turn(tenant, "Ana Souza")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_state == FlowState.IDLE
    assert conversation.reactivation_origin == FlowState.AWAITING_NAME.value
    kind, body, buttons = _WireClient.sends[-1]
    assert body.startswith("Seu atendimento ficou pausado antes da etapa de privacidade.")
    assert buttons, "the Sim/Não choice must ride on the offer"
    # The late answer is NOT silently taken as the name.
    assert (await _patient(db, tenant, CHANNEL_WHATSAPP)).name == PROFILE_NAME

    await _wa_turn(tenant, "sim")

    assert _WireClient.sends[-1] == ("text", NAME_REQUEST_MESSAGE, None)
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_NAME


async def test_a_free_answer_to_the_name_offer_never_loops(db) -> None:
    """Review finding 2: typing instead of tapping consumes the offer, both ways."""
    tenant = await _seed_tenant(db)
    await _wa_turn(tenant, "oi")
    await _age_conversation(db, tenant, minutes=tasks.pending_identity_ttl_minutes(tenant) + 5)
    await _wa_turn(tenant, "oi")
    assert (await _conversation(db, tenant)).reactivation_origin == FlowState.AWAITING_NAME.value

    # The natural reply to the offer is the name: taken, and consent follows.
    await _wa_turn(tenant, "Ana Souza")

    assert (await _patient(db, tenant, CHANNEL_WHATSAPP)).name == "Ana Souza"
    assert _WireClient.sends[-1] == ("buttons", LGPD_CONSENT_MESSAGE, [CONSENT_BUTTON_LABEL])
    assert (await _conversation(db, tenant)).reactivation_origin is None


async def test_a_non_name_answer_to_the_name_offer_meets_consent(db) -> None:
    tenant = await _seed_tenant(db)
    await _wa_turn(tenant, "oi")
    await _age_conversation(db, tenant, minutes=tasks.pending_identity_ttl_minutes(tenant) + 5)
    await _wa_turn(tenant, "oi")

    await _wa_turn(tenant, "quero agendar")

    assert _WireClient.sends[-1] == (
        "buttons",
        CONSENT_REMINDER_MESSAGE,
        [CONSENT_BUTTON_LABEL],
    )
    conversation = await _conversation(db, tenant)
    assert (conversation.flow_state, conversation.reactivation_origin) == (FlowState.IDLE, None)


async def test_a_first_contact_that_sends_nothing_does_not_arm_the_name_wait(
    db, monkeypatch
) -> None:
    """Review finding 4: suppressed by entitlement -> no question -> no state."""

    async def _lapsed(tenant_id, redis):
        return None

    monkeypatch.setattr(tasks, "get_entitlements", _lapsed)
    tenant = await _seed_tenant(db)

    await _wa_turn(tenant, "oi")

    assert _WireClient.sends == []
    assert (await _conversation(db, tenant)).flow_state == FlowState.IDLE


async def test_whatsapp_declining_to_resume_pauses_and_then_meets_consent(db) -> None:
    tenant = await _seed_tenant(db)
    await _wa_turn(tenant, "oi")
    await _age_conversation(db, tenant, minutes=tasks.pending_identity_ttl_minutes(tenant) + 5)
    await _wa_turn(tenant, "oi")

    await _wa_turn(tenant, "não")

    assert _WireClient.sends[-1] == ("text", NAME_PAUSED_MESSAGE, None)
    assert (await _conversation(db, tenant)).flow_state == FlowState.IDLE

    await _wa_turn(tenant, "voltei")

    assert _WireClient.sends[-1] == (
        "buttons",
        CONSENT_REMINDER_MESSAGE,
        [CONSENT_BUTTON_LABEL],
    )


# --------------------------------------------------------------------------
# 3. Portal: the e-mail decides who is new
# --------------------------------------------------------------------------


async def test_portal_new_email_asks_the_name_before_the_lgpd(db, calls) -> None:
    """CHECKLIST: greeting -> e-mail -> NAME -> LGPD for an address with no account."""
    tenant = await _seed_tenant(db)

    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)

    sent = await _outbound(db, tenant)
    assert sent[1] == EMAIL_REQUEST_MESSAGE
    assert sent[2] == NAME_REQUEST_AFTER_EMAIL_MESSAGE
    assert LGPD_ROW not in sent
    assert calls.code_requests == [], "a new address must not be mailed a code here"
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_NAME

    await _bm_turn(tenant, "Beatriz Lima")

    sent = await _outbound(db, tenant)
    assert sent[3] == LGPD_ROW
    # Portal: the typed name also goes to brain-api, for this account's next clinic.
    assert calls.reported == [(EXTERNAL_ID, "Beatriz Lima")]
    assert (await _patient(db, tenant, CHANNEL_BRAIN_MESSAGE)).name == "Beatriz Lima"
    assert (await _conversation(db, tenant)).flow_state == FlowState.IDLE


async def test_portal_known_email_skips_the_name_and_asks_the_code(db, calls) -> None:
    """CHECKLIST: no name question; the code, citing the MASKED inbox, not the raw one."""
    calls.claim_result = ClaimResult(
        ClaimOutcome.CLAIMED, account_exists=True, email_masked=EMAIL_MASKED
    )
    tenant = await _seed_tenant(db)

    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)

    sent = await _outbound(db, tenant)
    assert NAME_REQUEST_AFTER_EMAIL_MESSAGE not in sent
    assert NAME_REQUEST_MESSAGE not in sent
    assert LGPD_ROW not in sent
    assert sent[2] == interactive_history_body(
        existing_account_code_body(EMAIL_MASKED), [title for _, title in CODE_NOTICE_BUTTONS]
    )
    assert EMAIL_MASKED in sent[2]
    assert all(EMAIL not in body for body in sent), "the raw address reached the transcript"
    assert "consulta" not in sent[2].casefold()
    assert calls.code_requests == [EXTERNAL_ID]
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_EMAIL_CODE

    # The right code makes the visit a verified account: the account's consent
    # is mirrored (never forged as a click here) and the menu opens. brain-api
    # returns the name the ACCOUNT gave at another clinic (2026-09-24), so the
    # known person is still not asked it — and the clinic has it.
    calls.account_name = "Maria Silva"
    calls.probe_states = [IdentityState.VERIFIED]
    await _bm_turn(tenant, "123456")

    sent = await _outbound(db, tenant)
    assert calls.verified == ["123456"]
    assert CODE_ACCEPTED_MESSAGE in sent
    assert sent[-1].startswith("Como posso te ajudar?")
    assert NAME_REQUEST_MESSAGE not in sent
    patient = await _patient(db, tenant, CHANNEL_BRAIN_MESSAGE)
    assert patient.lgpd_accepted_at is not None
    assert patient.name == "Maria Silva"
    assert calls.reported == [], "a name learned from brain-api is not sent back to it"
    async with db() as session:
        kinds = (await session.scalars(select(ConsentEvent.kind))).all()
    assert "account_terms_verified" in kinds
    assert (await _conversation(db, tenant)).flow_state == FlowState.MENU


async def test_portal_known_email_whose_account_has_no_name_is_asked_it_after_the_code(
    db, calls
) -> None:
    """The clinic needs the name (owner, 2026-09-24): asked once, then the menu — no LGPD."""
    calls.claim_result = ClaimResult(
        ClaimOutcome.CLAIMED, account_exists=True, email_masked=EMAIL_MASKED
    )
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)
    calls.probe_states = [IdentityState.VERIFIED]

    await _bm_turn(tenant, "123456")

    sent = await _outbound(db, tenant)
    assert sent[-2:] == [CODE_ACCEPTED_MESSAGE, NAME_REQUEST_MESSAGE]
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_NAME

    await _bm_turn(tenant, "Maria Silva")

    sent = await _outbound(db, tenant)
    assert sent[-1].startswith("Como posso te ajudar?")
    assert LGPD_ROW not in sent
    assert (await _patient(db, tenant, CHANNEL_BRAIN_MESSAGE)).name == "Maria Silva"
    assert calls.reported == [(EXTERNAL_ID, "Maria Silva")]
    assert (await _conversation(db, tenant)).flow_state == FlowState.MENU


async def test_portal_known_email_whose_code_cannot_be_checked_continues_to_consent(
    db, calls, monkeypatch
) -> None:
    """A dead end never promises a consultation that does not exist."""
    calls.claim_result = ClaimResult(
        ClaimOutcome.CLAIMED, account_exists=True, email_masked=EMAIL_MASKED
    )

    async def _down(tenant_id, external_id, code):
        calls.verified.append(code)
        return VerifyResult(VerifyOutcome.UNAVAILABLE)

    monkeypatch.setattr(tasks, "verify_code", _down)
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)

    await _bm_turn(tenant, "123456")

    sent = await _outbound(db, tenant)
    assert sent[-2:] == [ACCOUNT_CODE_GIVE_UP_MESSAGE, LGPD_ROW]
    assert (await _conversation(db, tenant)).flow_state == FlowState.IDLE


async def test_portal_consented_patient_retyping_an_email_is_not_asked_the_name(db, calls) -> None:
    """CHECKLIST (returning, Portal): a patient past the opening is never asked again."""
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)
    await _bm_turn(tenant, "Beatriz Lima")
    await _bm_turn(tenant, CONSENT_BUTTON_LABEL)
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            row = await session.get(Conversation, conversation.id)
            row.flow_state = FlowState.AWAITING_EMAIL
    before = len(await _outbound(db, tenant))

    await _bm_turn(tenant, "outro@exemplo.com")

    new = (await _outbound(db, tenant))[before:]
    assert NAME_REQUEST_AFTER_EMAIL_MESSAGE not in new
    assert (await _conversation(db, tenant)).flow_state != FlowState.AWAITING_NAME
    assert (await _patient(db, tenant, CHANNEL_BRAIN_MESSAGE)).name == "Beatriz Lima"


async def test_portal_silence_expires_the_name_wait(db) -> None:
    """CHECKLIST: the same ceiling on the Portal, with the same resume offer."""
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)
    await _age_conversation(db, tenant, minutes=tasks.pending_identity_ttl_minutes(tenant) + 5)

    await _bm_turn(tenant, "Beatriz Lima")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_state == FlowState.IDLE
    assert conversation.reactivation_origin == FlowState.AWAITING_NAME.value
    assert (await _outbound(db, tenant))[-1].startswith(
        "Seu atendimento ficou pausado antes da etapa de privacidade."
    )
    assert (await _patient(db, tenant, CHANNEL_BRAIN_MESSAGE)).name is None

    await _bm_turn(tenant, "sim")

    assert (await _outbound(db, tenant))[-1] == NAME_REQUEST_MESSAGE
    assert (await _conversation(db, tenant)).flow_state == FlowState.AWAITING_NAME


# --------------------------------------------------------------------------
# 4. The owner's requirement: the typed name reaches OpenAI only as a token
# --------------------------------------------------------------------------


async def test_the_captured_name_reaches_the_llm_only_as_a_pacient_token(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHECKLIST: masked (`PACIENTE_*`) in the history sent to OpenAI.

    The real pipeline end to end, not a reading of it: the name is captured by
    the WhatsApp flow (typed in lowercase, stored Title-Cased), the
    conversation is moved to `FlowState.LLM`, one more message is routed and
    delegated to the agent, and `ai/graph.py::run_agent` loads the history
    FROM THE DATABASE and scrubs it with the pseudonymizer
    `load_pseudonymizer` builds. Only `invoke_agent` — the call that becomes
    the OpenAI request — is replaced, by a spy.
    """
    tenant = await _seed_tenant(db)
    await _wa_turn(tenant, "oi")
    await _wa_turn(tenant, "joão da silva")
    await _wa_turn(tenant, CONSENT_BUTTON_LABEL)
    assert (await _patient(db, tenant, CHANNEL_WHATSAPP)).name == "João da Silva"

    async def _turn(body):
        reply = await tasks._persist_inbound_message(
            phone_number_id=tenant.phone_number_id,
            wa_id=WA_ID,
            patient_name=PROFILE_NAME,
            wam_id="wamid.llm.turn",
            body=body,
        )
        assert reply is not None and reply.greeting_override is None
        return reply

    history = await _llm_history(
        db,
        tenant,
        monkeypatch,
        channel_turn=_turn,
        body="Aqui é o João da Silva de novo, qual o valor da consulta?",
    )
    # The free-text mention in the LLM turn is masked by the pseudonymizer...
    assert "[PACIENTE_" in history
    assert "joão da silva" not in history.casefold()
    assert "joao da silva" not in history.casefold()
    # ...and the name step's own answer row reaches the model redacted.
    assert NAME_ANSWER_LLM_PLACEHOLDER in history


async def _llm_history(db, tenant, monkeypatch, *, channel_turn, body) -> str:
    """Move the conversation to LLM, route one more inbound, return what OpenAI gets.

    SQLite stores `created_at` to the second, so a whole test conversation
    ties and `_load_history`'s ORDER BY returns it shuffled (Postgres keeps
    microseconds). The rows are re-stamped in insertion order first, because
    the redaction under test depends on which bot row precedes an answer.
    """
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE messages SET created_at = "
                    "datetime('now', '-300 seconds', '+' || rowid || ' seconds')"
                )
            )
    async with db() as session:
        async with session.begin():
            row = await session.get(Conversation, conversation.id)
            row.flow_state = FlowState.LLM
            patient = await session.get(Patient, row.patient_id)
            patient.lgpd_accepted_at = patient.lgpd_accepted_at or datetime.now(UTC)
    reply = await channel_turn(body)
    assert reply is not None
    seen: list = []

    async def _spy(messages):  # noqa: ANN001
        seen.append(messages)
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _spy)
    await graph.run_agent(reply.inbound_body, context={"conversation_id": str(conversation.id)})
    return "\n".join(str(message.content) for message in seen[0])


async def test_an_unparsed_answer_never_reaches_the_llm_raw(db, monkeypatch) -> None:
    """Review finding 3: the partial "Ana" of a re-asked answer is not a registered
    identifier, so without the redaction it would reach OpenAI as plain text."""
    tenant = await _seed_tenant(db)
    await _wa_turn(tenant, "oi")
    await _wa_turn(tenant, "Ana, tudo bem?")
    assert _WireClient.sends[-1] == ("text", NAME_INVALID_MESSAGE, None)
    await _wa_turn(tenant, "Ana Souza")

    async def _turn(body):
        return await tasks._persist_inbound_message(
            phone_number_id=tenant.phone_number_id,
            wa_id=WA_ID,
            patient_name=PROFILE_NAME,
            wam_id="wamid.llm.partial",
            body=body,
        )

    history = await _llm_history(
        db, tenant, monkeypatch, channel_turn=_turn, body="qual o valor da consulta?"
    )
    assert re.search(r"ana", history, re.IGNORECASE) is None, history
    assert history.count(NAME_ANSWER_LLM_PLACEHOLDER) == 2


async def test_portal_two_unparsed_answers_leave_no_raw_name_for_the_llm(db, monkeypatch) -> None:
    """Nothing is registered at all when both answers fail — redaction still holds."""
    tenant = await _seed_tenant(db)
    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, EMAIL)
    await _bm_turn(tenant, "Beatriz 😊")
    await _bm_turn(tenant, "Beatriz 😊😊")
    assert (await _patient(db, tenant, CHANNEL_BRAIN_MESSAGE)).name is None

    async def _turn(body):
        return await tasks._persist_brain_message_inbound(
            tenant_id=tenant.id, external_id=EXTERNAL_ID, text=body, patient_name=None
        )

    history = await _llm_history(
        db, tenant, monkeypatch, channel_turn=_turn, body="qual o valor da consulta?"
    )
    # "Beatriz", not "Maria": the re-ask copy itself says "Maria Silva".
    assert "beatriz" not in history.casefold(), history
