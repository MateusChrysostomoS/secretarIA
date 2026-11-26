"""TASK-090 (owner, 2026-11-22) + Adendo 1 (owner, 2026-11-24) - a convênio payment-rule
question with NO rule registered: the secretária says so and ASKS.

Owner (2026-11-24): the payment rule is optional per convênio. When the patient asks about one
that has none (shared mode: the clinic's line; independent mode: the selected doctor's line) -
or the rule cannot be read - the secretária (1) tells the patient in a FIXED sentence that there
is no payment rule registered, (2) asks with the usual ✅ Sim / ❌ Não whether a person from the
team should help, (3) "✅ Sim" -> the conversation goes HUMAN_ACTIVE and the clinic gets the same
hand-off e-mail as before for `insurance_payment_question` (no rule text, nothing the patient
typed), (4) "❌ Não" -> the conversation goes on. It is no longer a direct transfer.

The model still names the reason (`request_human_handoff(reason="insurance_payment_question")`,
or `get_insurance_payment_rule` hands back itself when there is no rule); the worker turns that
hand-back into the card (flow_router.enter_insurance_payment_offer, its own step
STEP_HUMAN_OFFER_INSURANCE carries the reason through the tap).
"""

import os

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from datetime import UTC, date, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import graph, prompts, prompts_v2  # noqa: E402
from secretaria.ai.graph import HUMAN_HANDOFF_SENTINEL_PREFIX, run_agent  # noqa: E402
from secretaria.ai.prompts import secretary_system_prompt  # noqa: E402
from secretaria.ai.prompts_v2 import secretary_system_prompt_v2  # noqa: E402
from secretaria.ai.tools import (  # noqa: E402
    HANDOFF_REASONS,
    HumanHandoffOfferRequested,
    HumanHandoffRequested,
    request_human_handoff,
)
from secretaria.config import Settings  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    FlowState,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services import (  # noqa: E402
    flow_router as fr,
    handoff_notification as hn,
    pii_pseudonymization as pii_store,
)
from secretaria.services.email import EmailOutcome  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import SCOPED_HELP_ESCALATE_MESSAGE  # noqa: E402
from secretaria.services.tenant_config import ClinicFeatures, TenantRuntimeConfig  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

REASON = "insurance_payment_question"
REASON_TEXT = "O paciente perguntou sobre a regra de pagamento do convênio."
PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511999999999"


# --------------------------------------------------------------------------
# The tool and the agent run
# --------------------------------------------------------------------------


def test_the_reason_is_registered_with_its_mail_text():
    assert REASON in HANDOFF_REASONS
    assert hn._REASON_TEXT[REASON] == REASON_TEXT


async def test_handoff_reason_insurance_payment_question_accepted():
    # The tool's own hand-back (the worker turns it into the Sim / Não card, below).
    with pytest.raises(HumanHandoffRequested) as exc:
        await request_human_handoff.ainvoke({"reason": f" {REASON} "})
    assert exc.value.reason == REASON
    assert not isinstance(exc.value, HumanHandoffOfferRequested)


def test_the_tool_description_names_the_reason_and_the_payment_rule():
    doc = request_human_handoff.description
    assert REASON in doc
    for word in ("reembolso", "coparticipação", "regra de pagamento"):
        assert word in doc


async def test_run_agent_maps_the_new_reason_to_the_handoff_sentinel(monkeypatch):
    async def _history(_cid):
        return [HumanMessage(content="o convênio reembolsa a consulta?")]

    async def _raise(_messages, _cid):
        raise HumanHandoffRequested(REASON)

    monkeypatch.setattr(graph, "_load_history", _history)
    monkeypatch.setattr(graph, "_invoke_agent_with_retry", _raise)
    reply = await run_agent("oi", context={"conversation_id": str(uuid4())})
    assert reply == f"{HUMAN_HANDOFF_SENTINEL_PREFIX}{REASON}"


# --------------------------------------------------------------------------
# The worker: HUMAN_ACTIVE, the e-mail with the new text, the patient's reply
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


class _WireClient:
    """The WhatsApp client: records what was sent (buttons with their labels)."""

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
        _WireClient.sends.append(("list", body, list(rows)))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}


@pytest.fixture
def wired(monkeypatch, db):
    """The real turn pipeline on the test DB; the mail transport, the channel client, the
    entitlement read and the model are fakes. `agent` holds what the "model" answers next."""
    mails: list[tuple[str, str, dict]] = []
    agent = {"reply": "Oi!", "calls": 0}

    async def _send(to, template, variables):
        mails.append((to, template, variables))
        return EmailOutcome.SENT

    async def _emails(_tenant_id):
        return {}

    async def _run_agent(message, context, **kwargs):
        agent["calls"] += 1
        return agent["reply"]

    async def _entitled(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id), status="active", active=True, secretaria_enabled=True,
            plan="bronze", secretaria_tier="basico", addons={}, limits={},
        )

    async def _token(_session, _tenant_id):
        return "decrypted-waba-token"

    async def _none(*_a, **_k):
        return None

    _WireClient.sends = []
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(graph, "async_session_factory", db)
    monkeypatch.setattr(pii_store, "async_session_factory", db)
    monkeypatch.setattr(hn, "async_session_factory", db)
    monkeypatch.setattr(hn, "send_transactional_email_result", _send)
    monkeypatch.setattr(hn, "fetch_professional_emails", _emails)
    monkeypatch.setattr(hn, "_retry_sleep", lambda _n: 0)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    monkeypatch.setattr(workers_ns, "WhatsAppClient", _WireClient)
    monkeypatch.setattr(workers_ns, "get_entitlements", _entitled)
    monkeypatch.setattr(workers_ns, "get_waba_token", _token)
    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _none)
    monkeypatch.setattr(workers_ns, "run_agent", _run_agent)
    return mails, agent


async def _seed(db, *, phone_number_id: str | None = None, wa_id: str = WA_ID):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica Sol",
            phone_number_id=phone_number_id or str(uuid4().int)[:12],
            contact_email="recepcao@example.com",
            is_active=True,
            initial_flows={},
            timezone="America/Sao_Paulo",
        )
        session.add(tenant)
        await session.flush()
        patient = Patient(
            tenant_id=tenant.id, wa_id=wa_id, name="Maria", lgpd_accepted_at=datetime.now(UTC)
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, flow_state=FlowState.LLM
        )
        session.add(conversation)
        await session.flush()
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.BOT,
                body="Como posso ajudar?",
            )
        )
        await session.commit()
        return tenant, conversation


_seq = iter(range(1, 100_000))


async def _turn(tenant, body: str, *, wa_id: str = WA_ID) -> None:
    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=wa_id,
        patient_name="Maria",
        wam_id=f"wamid.in.{next(_seq)}",
        body=body,
        interactive_reply_id=None,
    )
    assert reply is not None
    await tasks._send_bot_reply(reply, redis=None)


async def _row(db, conversation_id) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, conversation_id)


async def _ask_without_rule(db, wired):
    """The patient asks how payment works; the model's hand-back names the reason."""
    _mails, agent = wired
    tenant, conversation = await _seed(db, phone_number_id=PHONE_NUMBER_ID)
    agent["reply"] = f"{HUMAN_HANDOFF_SENTINEL_PREFIX}{REASON}"
    await _turn(tenant, "o convênio reembolsa a consulta?")
    agent["reply"] = "Claro, sigo te ajudando."
    return tenant, conversation


async def test_no_rule_shows_the_fixed_sentence_and_the_sim_nao_offer(db, wired):
    mails, _agent = wired
    _tenant, conversation = await _ask_without_rule(db, wired)
    kind, body, labels = _WireClient.sends[-1]
    assert kind == "buttons"
    assert body == fr.HUMAN_OFFER_INSURANCE_BODY
    assert body.startswith("Não tenho cadastrada a regra de pagamento desse convênio.")
    assert labels == [fr.LABEL_YES, fr.LABEL_NO]
    row = await _row(db, conversation.id)
    assert (row.flow_state, row.flow_step) == (FlowState.LLM, fr.STEP_HUMAN_OFFER_INSURANCE)
    # Asked, not transferred: still the bot's, and nobody was e-mailed yet.
    assert row.handover_state == HandoverState.BOT_ACTIVE
    assert mails == []


async def test_yes_hands_over_and_mails_the_insurance_reason_once(db, wired):
    mails, agent = wired
    tenant, conversation = await _ask_without_rule(db, wired)
    calls_before = agent["calls"]
    await _turn(tenant, fr.LABEL_YES)
    row = await _row(db, conversation.id)
    assert row.handover_state == HandoverState.HUMAN_ACTIVE
    assert agent["calls"] == calls_before  # the tap is the flow's, never the model's
    assert [(to, template) for to, template, _ in mails] == [
        ("recepcao@example.com", hn.TEMPLATE_ID)
    ]
    variables = mails[0][2]
    assert variables["reason"] == REASON_TEXT
    # Nothing the patient typed, no rule text.
    assert "reembolsa" not in " ".join(str(value) for value in variables.values())
    assert _WireClient.sends[-1][1] == SCOPED_HELP_ESCALATE_MESSAGE
    assert "alguém da nossa equipe" in SCOPED_HELP_ESCALATE_MESSAGE


async def test_no_keeps_the_conversation_with_the_secretaria_and_mails_nobody(db, wired):
    mails, _agent = wired
    tenant, conversation = await _ask_without_rule(db, wired)
    await _turn(tenant, fr.LABEL_NO)
    row = await _row(db, conversation.id)
    assert row.handover_state == HandoverState.BOT_ACTIVE
    assert row.flow_step != fr.STEP_HUMAN_OFFER_INSURANCE
    assert _WireClient.sends[-1][1] == fr.HUMAN_OFFER_DECLINED_BODY
    assert mails == []


async def test_a_typed_message_instead_of_a_tap_drops_the_offer(db, wired):
    mails, agent = wired
    tenant, conversation = await _ask_without_rule(db, wired)
    calls_before = agent["calls"]
    await _turn(tenant, "deixa pra lá, quero marcar uma consulta")
    row = await _row(db, conversation.id)
    assert agent["calls"] == calls_before + 1  # the model answers the patient
    assert row.flow_step != fr.STEP_HUMAN_OFFER_INSURANCE
    assert row.handover_state == HandoverState.BOT_ACTIVE
    assert mails == []


async def test_a_sim_in_another_clinics_conversation_is_not_this_offer(db, wired):
    """The offer lives on ONE conversation's step: a "✅ Sim" typed in another clinic's chat
    (another patient, no offer open) is just text for the model - nobody is handed over."""
    mails, agent = wired
    await _ask_without_rule(db, wired)
    other_tenant, other_conversation = await _seed(db, wa_id="5511988880000")
    calls_before = agent["calls"]
    await _turn(other_tenant, fr.LABEL_YES, wa_id="5511988880000")
    row = await _row(db, other_conversation.id)
    assert row.handover_state == HandoverState.BOT_ACTIVE
    assert agent["calls"] == calls_before + 1
    assert mails == []


async def test_other_reasons_still_transfer_directly(db, wired):
    """Only the payment-rule reason became an offer; "patient_requested_human" stays direct."""
    mails, agent = wired
    tenant, conversation = await _seed(db, phone_number_id=PHONE_NUMBER_ID)
    agent["reply"] = f"{HUMAN_HANDOFF_SENTINEL_PREFIX}patient_requested_human"
    await _turn(tenant, "quero falar com uma pessoa")
    row = await _row(db, conversation.id)
    assert row.handover_state == HandoverState.HUMAN_ACTIVE
    assert len(mails) == 1


# --------------------------------------------------------------------------
# The prompts
# --------------------------------------------------------------------------


def _config(plans: int) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=uuid4(),
        clinic_name="Clínica Teste",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="primary",
        google_refresh_token=None,
        clinic_features=ClinicFeatures(deposit_enabled=False, insurance_plan_count=plans),
    )


V2_NAMES = frozenset(
    {
        "set_booking_draft",
        "show_main_menu",
        "request_human_handoff",
        "offer_human_handoff",
        "get_insurance_payment_rule",
    }
)


@pytest.mark.parametrize("plans", [0, 3])
def test_prompt_tells_model_to_hand_off_on_insurance_payment_rules(monkeypatch, plans):
    monkeypatch.setattr(prompts_v2, "_clinic_today", lambda _tz: date(2026, 11, 22))
    v1 = secretary_system_prompt(_config(plans))
    v2 = secretary_system_prompt_v2(_config(plans), tool_names=V2_NAMES)
    # Every clinic, with or without convênios: the short rule next to the other hand-off reasons.
    assert prompts.INSURANCE_PAYMENT_HANDOFF_RULE in v1
    assert prompts_v2.INSURANCE_PAYMENT_HANDOFF_RULE_V2 in v2
    v2_line = next(line for line in v2.splitlines() if line.startswith("- request_human_handoff"))
    assert prompts_v2.INSURANCE_PAYMENT_HANDOFF_RULE_V2 in v2_line
    # Both prompts, with convênios: the full "never explain" sentence in the features block.
    for prompt in (v1, v2):
        assert (prompts.INSURANCE_PAYMENT_HANDOFF in prompt) is bool(plans)
    rule = prompts.INSURANCE_PAYMENT_HANDOFF_RULE
    for words in (
        "reembolso",
        "coparticipação",
        "get_insurance_payment_rule",
        # TASK-094 D6/D7: any "como funciona o X"; the system shows the rule, never the model.
        "como funciona",
        "o sistema mostra a regra",
        "não repita",
        "sem regra, não explique",
        "oferece Sim/Não",
        f'reason "{REASON}"',
    ):
        assert words in rule
    # v2 (TASK-090 Adendo 1): the read tool's own line, then the hand-off only without a rule.
    v2_rule_line = next(
        line for line in v2.splitlines() if line.startswith("- get_insurance_payment_rule")
    )
    assert v2_rule_line == prompts_v2.INSURANCE_PAYMENT_RULE_V2
    for words in ("reembolso", "coparticipação", "cobertura", "como funciona", "não repita"):
        assert words in prompts_v2.INSURANCE_PAYMENT_RULE_V2
    # Adendo 1 (owner, 2026-11-24): no rule -> the Sim / Não offer (net-neutral in the v2 cap).
    for words in ("Convênio sem regra", f'reason "{REASON}"', "Sim/Não"):
        assert words in prompts_v2.INSURANCE_PAYMENT_HANDOFF_RULE_V2
    full = prompts.INSURANCE_PAYMENT_HANDOFF
    for words in ("Não invente", "não dê exemplos", "get_insurance_payment_rule", REASON):
        assert words in full
