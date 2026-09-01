"""Tests for the LGPD consent gate — the sequenced first conversation.

The shape being pinned, message by message:

    patient's 1st message
      -> the greeting frame, as PLAIN TEXT, no buttons
      -> the LGPD notice, carrying the single "✅ Concordo" button
    anything that is not an acceptance
      -> the notice again (PreCheck's "Reenviar LGPD"), still with the button
    "✅ Concordo"
      -> `Patient.lgpd_accepted_at` is stamped, a ConsentEvent is written
      -> "O que você precisa?" with [🗓️ Agendar] [Outro] — the FIRST message
         of the conversation to carry action buttons
    from then on
      -> the gate is invisible

Two properties matter more than the happy path and get their own tests: the
gate must NOT block a human secretary who has taken the line, and it must be
idempotent, because WhatsApp redelivers and patients re-tap old buttons.

DB tests mirror the in-memory-sqlite pattern from test_bot_allowlist.py /
test_menu_command.py: a real aiosqlite engine on StaticPool, monkeypatched in
place of `workers.tasks.async_session_factory`.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

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
from secretaria.core.whatsapp_limits import EMOJI_SCHEDULE, decorate  # noqa: E402
from secretaria.models import (  # noqa: E402
    ConsentEvent,
    Conversation,
    HandoverState,
    Patient,
    Tenant,
)
from secretaria.services.flow_router import LABEL_BOOK, LABEL_OTHER  # noqa: E402
from secretaria.services.greeting_template import (  # noqa: E402
    CONSENT_ACCEPTED_MESSAGE,
    CONSENT_EVENT_KIND,
    render_greeting,
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
    monkeypatch.setattr(tasks, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))

    # The opening-state resolver does its own indexed reads; stubbed out so
    # these tests exercise the gate and nothing else.
    async def _fake_resolve(session, tenant_id, patient_id, **kwargs):
        return None

    monkeypatch.setattr(tasks, "resolve_patient_opening_state", _fake_resolve)
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


async def _inbound(tenant: Tenant, body: str, wam_id: str):
    return await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id=wam_id,
        body=body,
    )


async def _patient(db) -> Patient:
    async with db() as session:
        return await session.scalar(select(Patient).where(Patient.wa_id == WA_ID))


async def _consent_events(db) -> int:
    async with db() as session:
        return await session.scalar(
            select(func.count())
            .select_from(ConsentEvent)
            .where(ConsentEvent.kind == CONSENT_EVENT_KIND)
        )


# --------------------------------------------------------------------------
# Message 1 + 2: the frame goes out button-free, the notice carries the button
# --------------------------------------------------------------------------


async def test_first_contact_sends_the_frame_without_buttons(db) -> None:
    tenant = await _seed_tenant(db)

    reply = await _inbound(tenant, "oi", "wamid.first")

    assert reply is not None
    assert reply.greeting_override == render_greeting(tenant.clinic_name, tenant.clinic_description)
    # The load-bearing half: no action buttons on the frame. Offering [Agendar]
    # here would invite a tap the gate is about to refuse.
    assert reply.greeting_buttons == []
    assert reply.send_consent_notice is True
    assert reply.send_consent_reminder is False


async def test_first_contact_does_not_record_acceptance(db) -> None:
    """Sending the notice is not consent. Only the tap is."""
    tenant = await _seed_tenant(db)

    await _inbound(tenant, "oi", "wamid.first")

    patient = await _patient(db)
    assert patient.lgpd_accepted_at is None
    assert await _consent_events(db) == 0


# --------------------------------------------------------------------------
# Anything else while pending: re-prompt, and NOTHING else
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["quero marcar uma consulta", "Agendar", "/menu", "voltar"])
async def test_pending_consent_re_prompts_and_serves_nothing(db, body: str) -> None:
    """Not even `/menu` gets through — the gate sits above it in the ladder.

    `Agendar` and `voltar` are in here deliberately: they are the two things a
    patient is most likely to try after reading the frame, and both must be
    answered with the terms rather than with service.
    """
    tenant = await _seed_tenant(db)
    await _inbound(tenant, "oi", "wamid.first")

    reply = await _inbound(tenant, body, "wamid.second")

    assert reply is not None
    assert reply.send_consent_reminder is True
    assert reply.greeting_override is None
    assert reply.menu_requested is False
    patient = await _patient(db)
    assert patient.lgpd_accepted_at is None


# --------------------------------------------------------------------------
# The acceptance
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["✅ Concordo", "Concordo", "concordo"])
async def test_acceptance_stamps_the_patient_and_audits_it(db, body: str) -> None:
    tenant = await _seed_tenant(db)
    await _inbound(tenant, "oi", "wamid.first")

    reply = await _inbound(tenant, body, "wamid.accept")

    patient = await _patient(db)
    assert patient.lgpd_accepted_at is not None
    # Both records, not one: the column is the operational flag the gate reads,
    # the event is the immutable audit row that outlives a context wipe.
    assert await _consent_events(db) == 1
    assert reply.greeting_override == CONSENT_ACCEPTED_MESSAGE


async def test_acceptance_opens_the_service_menu(db) -> None:
    """Step 4: the first message of the conversation to carry action buttons."""
    tenant = await _seed_tenant(db)
    await _inbound(tenant, "oi", "wamid.first")

    reply = await _inbound(tenant, "✅ Concordo", "wamid.accept")

    assert "O que você precisa?" in reply.greeting_override
    assert reply.greeting_buttons == [decorate(EMOJI_SCHEDULE, LABEL_BOOK), LABEL_OTHER]
    assert reply.send_consent_notice is False
    assert reply.send_consent_reminder is False


async def test_acceptance_is_idempotent(db) -> None:
    """WhatsApp redelivers, and patients re-tap buttons further up the thread.

    A second acceptance must produce the same menu and write NOTHING new — the
    same guarantee `/menu` gives. Without it, every re-tap would forge a fresh
    consent moment and move the recorded timestamp.
    """
    tenant = await _seed_tenant(db)
    await _inbound(tenant, "oi", "wamid.first")
    first = await _inbound(tenant, "✅ Concordo", "wamid.accept.1")
    stamped = (await _patient(db)).lgpd_accepted_at

    second = await _inbound(tenant, "✅ Concordo", "wamid.accept.2")

    assert await _consent_events(db) == 1
    assert (await _patient(db)).lgpd_accepted_at == stamped
    assert second.greeting_override == first.greeting_override
    assert second.greeting_buttons == first.greeting_buttons


# --------------------------------------------------------------------------
# After consent the gate is invisible
# --------------------------------------------------------------------------


async def test_a_consented_patient_is_routed_normally(db) -> None:
    tenant = await _seed_tenant(db)
    await _inbound(tenant, "oi", "wamid.first")
    await _inbound(tenant, "✅ Concordo", "wamid.accept")

    reply = await _inbound(tenant, "quero marcar uma consulta", "wamid.after")

    assert reply.send_consent_reminder is False
    assert reply.send_consent_notice is False
    # No greeting either: this is an ordinary mid-conversation turn now.
    assert reply.greeting_override is None


# --------------------------------------------------------------------------
# The safety property: a human on the line is never gated
# --------------------------------------------------------------------------


async def test_human_handover_outranks_the_consent_gate(db) -> None:
    """A clinic employee answering by hand must never be blocked by the bot.

    The gate sits BELOW handover in `_persist_inbound_message`'s ladder for
    exactly this: consent governs what the BOT may do on its own, and a human
    who has taken the conversation is not the bot. Getting this backwards would
    have the product refuse to let a real secretary talk to a patient.
    """
    tenant = await _seed_tenant(db)
    await _inbound(tenant, "oi", "wamid.first")
    async with db() as session:
        conversation = await session.scalar(select(Conversation))
        conversation.handover_state = HandoverState.HUMAN_ACTIVE
        await session.commit()

    reply = await _inbound(tenant, "quero marcar", "wamid.during-handover")

    # The bot stays silent (that is what handover means); crucially it does NOT
    # come back with a consent re-prompt over the human's shoulder.
    assert reply is None or reply.send_consent_reminder is False
