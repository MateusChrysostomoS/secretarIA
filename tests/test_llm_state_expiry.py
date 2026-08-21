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
from secretaria.services.flow_router import DEFAULT_REACTIVATION_GAP_MINUTES  # noqa: E402
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
    """The bug: nothing used to bound this cohort's stay in LLM mode."""
    _, conversation = await _seed(
        db,
        flow_state=FlowState.LLM,
        silent_for=timedelta(days=7),
    )

    reply = await _inbound()

    assert reply is not None
    assert reply.greeting_override is None  # nothing extra is sent
    assert reply.reactivation is None  # and no "quer continuar?" prompt
    conv = await _flow_state(db, conversation.id)
    assert conv.flow_state == FlowState.IDLE  # this turn now routes to the menu
    assert conv.flow_selected_insurance is None
    assert conv.reactivation_origin is None


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
    """A configured returning greeting means the offer wins; nothing is expired."""
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
    conv = await _flow_state(db, conversation.id)
    # State PRESERVED so a "Sim" can resume it, and the gate is armed.
    assert conv.flow_state == FlowState.LLM
    assert conv.reactivation_origin == FlowState.LLM.value
