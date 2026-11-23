"""TASK-090 (owner, 2026-11-22) - a convênio payment-rule question goes to a person.

Owner (SPEC §3 item 2): "Quando o paciente perguntar a regra de pagamento do convênio, a secretária
chama uma pessoa por e-mail (conversa passa para o humano); o sistema não responde isso."

Built on the existing hand-off machinery, nothing new: `request_human_handoff` accepts the new
reason `insurance_payment_question` and transfers DIRECTLY (like `patient_requested_human`, not
the Sim/Não offer), the worker turns the conversation HUMAN_ACTIVE, the `human_handoff_alert`
e-mail says why, and the patient gets the usual "someone from the team" reply. The prompts tell
the model to do this instead of explaining.
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

from datetime import date  # noqa: E402
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
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    FlowState,
    HandoverState,
    Patient,
    Tenant,
)
from secretaria.services import handoff_notification as hn  # noqa: E402
from secretaria.services.email import EmailOutcome  # noqa: E402
from secretaria.services.flow_router import SCOPED_HELP_ESCALATE_MESSAGE  # noqa: E402
from secretaria.services.tenant_config import ClinicFeatures, TenantRuntimeConfig  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

REASON = "insurance_payment_question"
REASON_TEXT = "O paciente perguntou sobre a regra de pagamento do convênio."


# --------------------------------------------------------------------------
# The tool and the agent run
# --------------------------------------------------------------------------


def test_the_reason_is_registered_with_its_mail_text():
    assert REASON in HANDOFF_REASONS
    assert hn._REASON_TEXT[REASON] == REASON_TEXT


async def test_handoff_reason_insurance_payment_question_accepted():
    # Direct transfer (like patient_requested_human), never the Sim/Não offer.
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


@pytest.fixture
def wired(monkeypatch, db):
    """Real hand-off code on the test DB; only the mail transport and the channel are fakes."""
    mails: list[tuple[str, str, dict]] = []
    bubbles: list = []

    async def _send(to, template, variables):
        mails.append((to, template, variables))
        return EmailOutcome.SENT

    async def _emails(_tenant_id):
        return {}

    async def _dispatch(reply, out, tenant=None, waba_token=None):
        bubbles.extend(out)
        return len(out)

    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(hn, "async_session_factory", db)
    monkeypatch.setattr(hn, "send_transactional_email_result", _send)
    monkeypatch.setattr(hn, "fetch_professional_emails", _emails)
    monkeypatch.setattr(hn, "_retry_sleep", lambda _n: 0)
    monkeypatch.setattr(workers_ns, "_dispatch_bubbles", _dispatch)
    return mails, bubbles


async def _seed(db) -> tuple[Tenant, Conversation]:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica Sol",
            phone_number_id=str(uuid4())[:12],
            contact_email="recepcao@example.com",
        )
        session.add(tenant)
        await session.flush()
        patient = Patient(tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, flow_state=FlowState.LLM
        )
        session.add(conversation)
        await session.commit()
        return tenant, conversation


async def test_the_worker_hands_the_conversation_to_a_person_and_mails_why(db, wired):
    mails, bubbles = wired
    tenant, conversation = await _seed(db)
    reply = tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_ref="5511999999999",
        inbound_body="o convênio reembolsa a consulta?",
    )

    await tasks._handle_human_handoff(reply, REASON, tenant, None)

    async with db() as session:
        row = await session.get(Conversation, conversation.id)
        assert row.handover_state == HandoverState.HUMAN_ACTIVE
    assert [(to, template) for to, template, _ in mails] == [
        ("recepcao@example.com", hn.TEMPLATE_ID)
    ]
    variables = mails[0][2]
    assert variables["reason"] == REASON_TEXT
    # Nothing the patient typed rides into the mail.
    assert "reembolsa" not in " ".join(str(value) for value in variables.values())
    # The patient gets the usual hand-off reply: someone from the team continues.
    assert [bubble.body for bubble in bubbles] == [SCOPED_HELP_ESCALATE_MESSAGE]
    assert "alguém da nossa equipe" in SCOPED_HELP_ESCALATE_MESSAGE


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
        "explique só o que ela diz",
        "sem regra, não explique",
        f'reason "{REASON}"',
    ):
        assert words in rule
    # v2 (TASK-090 Adendo 1): the read tool's own line, then the hand-off only without a rule.
    v2_rule_line = next(
        line for line in v2.splitlines() if line.startswith("- get_insurance_payment_rule")
    )
    assert v2_rule_line == prompts_v2.INSURANCE_PAYMENT_RULE_V2
    for words in ("reembolso", "coparticipação", "cobertura", "só a regra devolvida"):
        assert words in prompts_v2.INSURANCE_PAYMENT_RULE_V2
    for words in ("Convênio sem regra", f'reason "{REASON}"', "sem explicar"):
        assert words in prompts_v2.INSURANCE_PAYMENT_HANDOFF_RULE_V2
    full = prompts.INSURANCE_PAYMENT_HANDOFF
    for words in ("Não invente", "não dê exemplos", "get_insurance_payment_rule", REASON):
        assert words in full
