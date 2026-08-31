"""Tests for the universal floor on how long a conversation stays in LLM mode.

`route()` keeps `FlowState.LLM` until something explicitly resets it, and every
reset is patient- or agent-initiated (`/menu`, or one of the four hand-back
tools). The only time-based exit, `_reactivation_offer`, is gated on
`reactivation_enabled(tenant)` — true only for clinics that filled in
`returning_greeting_message` in the hub. `Conversation` is ONE row per patient
forever (`uq_conversations_tenant_patient`), so for a DEFAULT tenant nothing
bounded the stay: one "Outro" tap parked that patient on the free LLM for every
future contact.

`tests/test_reactivation.py` unit-tests `_expire_stale_llm_state` itself. This
file pins the CALL SITE — the guard inside `_persist_inbound_message` that the
bug actually lived in — against a real (in-memory sqlite) DB, mirroring the
engine/StaticPool pattern from test_bot_allowlist.py.
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
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services.flow_router import (  # noqa: E402
    DEFAULT_CONTINUE_PROMPT,
    DEFAULT_REACTIVATION_GAP_MINUTES,
    FlowRouterResult,
)
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"


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
def _wire_db(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    yield


async def _seed(
    db,
    *,
    flow_state: FlowState,
    silent_for: timedelta,
    returning_greeting_message: str | None = None,
    initial_flows: dict | None = None,
) -> tuple[Tenant, Conversation]:
    """A tenant + a returning patient whose conversation last moved `silent_for` ago."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            greeting_message="Olá! Bem-vindo à Clínica.",
            returning_greeting_message=returning_greeting_message,
            initial_flows=initial_flows or {},
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id=WA_ID, name="Maria")
        conversation = Conversation(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_state=flow_state,
            flow_selected_professional_id=None,
            flow_selected_insurance="Unimed",
        )
        # A prior message makes this NOT a first contact (so no verbatim
        # greeting short-circuit) and sets the silence gap the guard measures.
        session.add_all([tenant, patient, conversation])
        await session.flush()
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                wam_id="wamid.seed.old",
                body="quanto custa?",
                created_at=datetime.now(UTC) - silent_for,
            )
        )
        await session.commit()
        return tenant, conversation


async def _flow_state(db, conversation_id) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, conversation_id)


async def _inbound(wam_id: str = "wamid.new.1", body: str = "bom dia"):
    return await tasks._persist_inbound_message(
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id=wam_id,
        body=body,
    )


# --------------------------------------------------------------------------
# The gap: a default tenant (no returning greeting -> reactivation disabled)
# --------------------------------------------------------------------------


async def test_default_tenant_stale_llm_conversation_is_released(db):
    """The bug: nothing used to bound this cohort's stay in LLM mode.

    The release itself is the invariant and is unchanged. What changed on
    2026-08-25 is that this cohort is now ASKED rather than silently rerouted —
    the prompt stopped being gated on `reactivation_enabled`. Both facts are
    asserted together on purpose: the prompt must never be the thing that keeps
    the state alive.
    """
    _, conversation = await _seed(
        db,
        flow_state=FlowState.LLM,
        silent_for=timedelta(days=7),
    )

    reply = await _inbound()

    assert reply is not None
    conv = await _flow_state(db, conversation.id)
    # RELEASED — the floor ran before the prompt was built.
    assert conv.flow_state == FlowState.IDLE
    # ...but WHO they were dealing with survives, because the prompt they are
    # about to get offers to resume exactly that. Only "Não" drops it.
    assert conv.flow_selected_insurance == "Unimed"
    # ...and ASKED, with the product default text and the gate armed so "Sim"
    # can still put them back where they were.
    assert reply.greeting_override == DEFAULT_CONTINUE_PROMPT
    assert reply.greeting_buttons == ["✅ Sim", "❌ Não"]
    assert conv.reactivation_origin == FlowState.LLM.value


async def test_default_tenant_active_llm_conversation_is_left_alone(db):
    """Mid-conversation stickiness is the point of FlowState.LLM — keep it."""
    _, conversation = await _seed(
        db,
        flow_state=FlowState.LLM,
        silent_for=timedelta(minutes=DEFAULT_REACTIVATION_GAP_MINUTES - 5),
    )

    await _inbound()

    conv = await _flow_state(db, conversation.id)
    assert conv.flow_state == FlowState.LLM
    assert conv.flow_selected_insurance == "Unimed"


async def test_a_no_answer_still_drops_everything(db):
    """The expiry keeps the selection; the explicit "Não" is what wipes it."""
    _, conversation = await _seed(
        db,
        flow_state=FlowState.LLM,
        silent_for=timedelta(days=7),
    )
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        conv.reactivation_origin = FlowState.LLM.value  # prompt already sent
        await session.commit()

    reply = await _inbound(wam_id="wamid.new.no", body="Não")

    assert reply is not None
    assert reply.reactivation is not None
    assert reply.reactivation.kind == "reset"
    conv = await _flow_state(db, conversation.id)
    assert conv.flow_state == FlowState.IDLE
    assert conv.flow_selected_insurance is None  # "Não" drops it all


async def test_default_tenant_stale_non_llm_state_is_left_alone(db):
    """Deterministic steps re-prompt on unexpected input; only LLM has no way back."""
    _, conversation = await _seed(
        db,
        flow_state=FlowState.SERVICE_CATALOG,
        silent_for=timedelta(days=7),
    )

    await _inbound()

    conv = await _flow_state(db, conversation.id)
    assert conv.flow_state == FlowState.SERVICE_CATALOG


# --------------------------------------------------------------------------
# The opted-in cohort keeps the behavior it already had
# --------------------------------------------------------------------------


async def test_reactivation_tenant_still_gets_the_resume_prompt_first(db):
    """A configured returning greeting still leads the prompt — but is not a stay.

    This cohort's MESSAGE is unchanged (their returning greeting + the
    question). What changed is that the state is now dropped before the question
    goes out, for them too. It used to be preserved "so a Sim can resume it",
    which quietly made the prompt the only time-based exit — a patient who never
    answered stayed in LLM mode forever, the very hole the floor exists to
    close. "Sim" now restores the state instead (see `_send_bot_reply`).
    """
    _, conversation = await _seed(
        db,
        flow_state=FlowState.LLM,
        silent_for=timedelta(days=7),
        returning_greeting_message="Oi de novo, {{name}}!",
    )

    reply = await _inbound()

    assert reply is not None
    assert reply.greeting_override is not None
    assert "Oi de novo, Maria!" in reply.greeting_override
    assert DEFAULT_CONTINUE_PROMPT in reply.greeting_override
    conv = await _flow_state(db, conversation.id)
    assert conv.flow_state == FlowState.IDLE  # released, NOT parked
    assert conv.reactivation_origin == FlowState.LLM.value  # "Sim" can undo it


async def test_an_explicit_disable_still_releases_silently(db):
    """`enabled: false` turns the QUESTION off, never the floor underneath it."""
    _, conversation = await _seed(
        db,
        flow_state=FlowState.LLM,
        silent_for=timedelta(days=7),
        initial_flows={"reactivation": {"enabled": False}},
    )

    reply = await _inbound()

    assert reply is not None
    assert reply.greeting_override is None  # no question
    conv = await _flow_state(db, conversation.id)
    assert conv.flow_state == FlowState.IDLE  # but still released
    assert conv.reactivation_origin is None


async def test_delegate_llm_result_writes_the_state_back_and_delegates(db):
    """The seam "Sim" leans on to undo the expiry.

    The prompt is only safe to send AFTER the state is dropped, which means the
    "Sim" answer has to write `FlowState.LLM` back. It does that by handing a
    `delegate_llm` result to `_apply_flow_result` rather than touching
    `conv.flow_*` by hand — the one persistence seam. This pins the two
    properties that branch depends on: the flow fields ARE persisted, and the
    call still returns False so the turn falls through to the agent.

    (The `_send_bot_reply` branch that makes the call is not exercised here —
    it needs the full WhatsApp/redis send path. This covers the contract it
    relies on.)
    """
    _, conversation = await _seed(
        db,
        flow_state=FlowState.IDLE,  # as the expiry left it
        silent_for=timedelta(days=7),
    )
    reply = tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_wa_id=WA_ID,
        inbound_body="Sim",
    )

    handled = await tasks._apply_flow_result(
        reply,
        FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM),
        WA_ID,
    )

    assert handled is False  # falls through to the agent
    conv = await _flow_state(db, conversation.id)
    assert conv.flow_state == FlowState.LLM  # ...with the state put back
    # And the trap this seam sets: EVERY flow field is written from the result,
    # so a resume that does not name the selection drops it. The real caller in
    # `_send_bot_reply` carries both forward from `conv_snapshot` for exactly
    # this reason.
    assert conv.flow_selected_insurance is None  # not named -> wiped

    handled = await tasks._apply_flow_result(
        reply,
        FlowRouterResult(
            action="delegate_llm",
            flow_state=FlowState.LLM,
            flow_selected_insurance="Unimed",
        ),
        WA_ID,
    )
    assert handled is False
    conv = await _flow_state(db, conversation.id)
    assert conv.flow_selected_insurance == "Unimed"  # named -> kept


async def test_a_stale_menu_state_is_asked_but_not_expired(db):
    """Only LLM is expired. MENU re-prompts on its own, so it is left intact."""
    _, conversation = await _seed(
        db,
        flow_state=FlowState.MENU,
        silent_for=timedelta(days=7),
    )

    reply = await _inbound()

    assert reply is not None
    assert reply.greeting_override == DEFAULT_CONTINUE_PROMPT
    conv = await _flow_state(db, conversation.id)
    assert conv.flow_state == FlowState.MENU  # untouched by the floor
    assert conv.reactivation_origin == FlowState.MENU.value
