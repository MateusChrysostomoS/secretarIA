"""TASK-035 — the Portal speaks first on ENTRY, with a message chosen by context.

Owner, 2026-10-05: as soon as a patient enters the Portal the first message must
arrive, without them pressing anything, and WHICH message depends on who they are:

  * a live upcoming appointment  -> detected (`OpeningKind.UPCOMING`); the copy is
    today's "Vi aqui que você já tem uma consulta marcada…" card until the reminder
    message (TASK-032) replaces it;
  * the first appearance after a consult -> "Como foi a sua consulta do dia DD/MM/AAAA?";
  * everyone else -> the clinic's menu question with [🗓️ Agendar, Outro].

And the ENTRY rule: speak when the patient enters, never over a conversation that is
still going (human handling it, onboarding, last message moments ago, flow mid-way).
"""

import os

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
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
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Professional,
    Tenant,
)
from secretaria.models.conversation import FlowState, HandoverState  # noqa: E402
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import LABEL_CANCEL_APPT, LABEL_RESCHEDULE  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.portal import open as open_job  # noqa: E402
from secretaria.workers.shared import opening  # noqa: E402

EXTERNAL_ID = "bm-entry-1"
AGENDAR_OUTRO = ["🗓️ Agendar", "Outro"]
NOW = datetime.now(UTC)
SP = ZoneInfo("America/Sao_Paulo")


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


class _Entitled:
    value = True


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))

    async def _fake_entitlements(tenant_id, redis):
        if not _Entitled.value:
            return None
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

    async def _fake_token(session, tenant_id):
        return "decrypted-waba-token"

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_entitlements)
    monkeypatch.setattr(workers_ns, "get_waba_token", _fake_token)
    _Entitled.value = True
    yield


# --- seeding -----------------------------------------------------------------------------


async def _seed(
    db,
    *,
    consented: bool = True,
    name: str | None = "Maria Silva",
    flow_state: FlowState = FlowState.IDLE,
    handover: HandoverState = HandoverState.BOT_ACTIVE,
    last_message_age: timedelta | None = timedelta(days=3),
    menu_label: str | None = None,
    post_consult_message: str | None = None,
):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica Olhar",
            phone_number_id=f"pn-{uuid4().hex[:8]}",
            is_active=True,
            initial_flows={"menu_label": menu_label} if menu_label else {},
            timezone="America/Sao_Paulo",
            post_consult_message=post_consult_message,
        )
        session.add(tenant)
        await session.flush()
        patient = Patient(
            tenant_id=tenant.id,
            channel=CHANNEL_BRAIN_MESSAGE,
            external_id=EXTERNAL_ID,
            name=name,
            lgpd_accepted_at=NOW - timedelta(days=30) if consented else None,
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_state=flow_state,
            handover_state=handover,
        )
        session.add(conversation)
        await session.flush()
        if last_message_age is not None:
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND,
                    sender=MessageSender.PATIENT,
                    body="oi",
                    created_at=NOW - last_message_age,
                )
            )
        await session.commit()
        return tenant, patient, conversation


async def _appointment(
    db,
    tenant,
    patient,
    *,
    start: datetime,
    status=AppointmentStatus.SCHEDULED,
    professional_name: str | None = None,
):
    async with db() as session:
        professional_id = None
        if professional_name:
            professional = Professional(tenant_id=tenant.id, name=professional_name)
            session.add(professional)
            await session.flush()
            professional_id = professional.id
        session.add(
            Appointment(
                tenant_id=tenant.id,
                patient_id=patient.id,
                google_event_id=f"evt-{uuid4().hex[:8]}",
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                status=status,
                professional_id=professional_id,
            )
        )
        await session.commit()


async def _message(db, conversation, *, at: datetime, direction=MessageDirection.OUTBOUND):
    sender = MessageSender.BOT if direction == MessageDirection.OUTBOUND else MessageSender.PATIENT
    async with db() as session:
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=direction,
                sender=sender,
                body="x",
                created_at=at,
            )
        )
        await session.commit()


async def _outbound(db, conversation) -> list[Message]:
    async with db() as session:
        return list(
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.direction == MessageDirection.OUTBOUND,
                )
                .order_by(Message.created_at)
            )
        )


async def _resolve(conversation, tenant):
    return await opening.resolve_opening_message(conversation.id, tenant)


# --- 1. Detection: which first message -----------------------------------------------------


async def test_a_known_patient_with_nothing_booked_gets_the_default_menu(db) -> None:
    tenant, _, conversation = await _seed(db)
    message = await _resolve(conversation, tenant)
    assert message.kind is opening.OpeningKind.MENU
    assert message.body == "Como posso te ajudar?"
    assert message.labels == AGENDAR_OUTRO


async def test_the_clinics_own_menu_question_wins_over_the_default(db) -> None:
    tenant, _, conversation = await _seed(db, menu_label="Em que podemos ajudar hoje?")
    message = await _resolve(conversation, tenant)
    assert message.body == "Em que podemos ajudar hoje?"
    assert message.labels == AGENDAR_OUTRO


async def test_an_upcoming_appointment_is_detected(db) -> None:
    tenant, patient, conversation = await _seed(db)
    await _appointment(
        db, tenant, patient, start=NOW + timedelta(days=5), professional_name="Dra. Ana"
    )
    message = await _resolve(conversation, tenant)
    assert message.kind is opening.OpeningKind.UPCOMING
    assert "Dra. Ana" in message.body
    assert message.labels[0] == LABEL_RESCHEDULE
    assert LABEL_CANCEL_APPT in message.labels[1]
    assert message.labels[-1] == "Outro"


@pytest.mark.parametrize("status", [AppointmentStatus.CONFIRMED, AppointmentStatus.RESCHEDULED])
async def test_every_live_status_counts_as_upcoming(db, status) -> None:
    tenant, patient, conversation = await _seed(db)
    await _appointment(db, tenant, patient, start=NOW + timedelta(days=2), status=status)
    assert (await _resolve(conversation, tenant)).kind is opening.OpeningKind.UPCOMING


async def test_a_cancelled_future_appointment_is_not_upcoming(db) -> None:
    tenant, patient, conversation = await _seed(db)
    await _appointment(
        db, tenant, patient, start=NOW + timedelta(days=2), status=AppointmentStatus.CANCELLED
    )
    assert (await _resolve(conversation, tenant)).kind is opening.OpeningKind.MENU


async def test_first_appearance_after_a_consult_asks_how_it_went(db) -> None:
    """Last message 3 days ago, consult 1 day ago: nothing was said since -> post-consult."""
    tenant, patient, conversation = await _seed(db)
    start = NOW - timedelta(days=1)
    await _appointment(
        db,
        tenant,
        patient,
        start=start,
        professional_name="Dr. Bruno",
        status=AppointmentStatus.ATTENDED,
    )
    message = await _resolve(conversation, tenant)
    assert message.kind is opening.OpeningKind.POST_CONSULT
    day = start.astimezone(SP).strftime("%d/%m/%Y")
    assert message.body.startswith(
        f"Olá, Maria! 😊 Como foi a sua consulta do dia {day} com Dr. Bruno?"
    )
    assert opening.POST_CONSULT_DEFAULT_FOLLOWUP in message.body
    assert message.labels == AGENDAR_OUTRO


def test_post_consult_copy_is_the_owner_format_and_uses_the_clinic_text() -> None:
    tenant = Tenant(
        clinic_name="X",
        timezone="America/Sao_Paulo",
        post_consult_message="Sentiu alguma ardência ou visão embaçada?",
    )
    body = opening.compose_post_consult_message(
        tenant,
        "João Pedro",
        {"start_at": datetime(2026, 9, 1, 14, 0, tzinfo=UTC), "attendee_name": None},
        None,
    )
    assert body == (
        "Olá, João! 😊 Como foi a sua consulta do dia 01/09/2026?\n\n"
        "Sentiu alguma ardência ou visão embaçada?"
    )


def test_post_consult_for_someone_else_names_the_attendee() -> None:
    tenant = Tenant(clinic_name="X", timezone="America/Sao_Paulo")
    body = opening.compose_post_consult_message(
        tenant,
        None,
        {"start_at": datetime(2026, 9, 1, 14, 0, tzinfo=UTC), "attendee_name": "Lucas"},
        "Dra. Ana",
    )
    assert body.startswith("Olá! 😊 Como foi a consulta de Lucas do dia 01/09/2026 com Dra. Ana?")


async def test_post_consult_is_asked_only_once(db) -> None:
    """Any message after the consult ended (the question itself, or the patient) ends it."""
    tenant, patient, conversation = await _seed(db)
    start = NOW - timedelta(days=1)
    await _appointment(db, tenant, patient, start=start)
    await _message(db, conversation, at=start + timedelta(hours=2))
    assert (await _resolve(conversation, tenant)).kind is opening.OpeningKind.MENU


@pytest.mark.parametrize("status", [AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW])
async def test_a_consult_that_did_not_happen_is_not_followed_up(db, status) -> None:
    tenant, patient, conversation = await _seed(db)
    await _appointment(db, tenant, patient, start=NOW - timedelta(days=1), status=status)
    assert (await _resolve(conversation, tenant)).kind is opening.OpeningKind.MENU


async def test_a_consult_older_than_the_followup_window_is_not_followed_up(db) -> None:
    tenant, patient, conversation = await _seed(db, last_message_age=timedelta(days=90))
    await _appointment(db, tenant, patient, start=NOW - timedelta(days=45))
    assert (await _resolve(conversation, tenant)).kind is opening.OpeningKind.MENU


async def test_a_consult_still_in_progress_is_not_followed_up(db) -> None:
    tenant, patient, conversation = await _seed(db)
    await _appointment(db, tenant, patient, start=NOW - timedelta(minutes=10))
    assert (await _resolve(conversation, tenant)).kind is opening.OpeningKind.MENU


async def test_an_upcoming_appointment_wins_over_a_past_one(db) -> None:
    tenant, patient, conversation = await _seed(db)
    await _appointment(db, tenant, patient, start=NOW - timedelta(days=1))
    await _appointment(db, tenant, patient, start=NOW + timedelta(days=10))
    assert (await _resolve(conversation, tenant)).kind is opening.OpeningKind.UPCOMING


async def test_another_clinics_appointment_is_never_read(db) -> None:
    tenant, _, conversation = await _seed(db)
    other, other_patient, _ = await _seed(db)
    await _appointment(db, other, other_patient, start=NOW + timedelta(days=3))
    assert (await _resolve(conversation, tenant)).kind is opening.OpeningKind.MENU


# --- 2. The entry rule ------------------------------------------------------------------------


async def _enter(tenant) -> None:
    await tasks.process_brain_message_enter({"redis": None}, str(tenant.id), EXTERNAL_ID)


async def test_entering_an_old_conversation_sends_the_opening_without_any_tap(db) -> None:
    tenant, _, conversation = await _seed(db)
    await _enter(tenant)
    sent = await _outbound(db, conversation)
    assert len(sent) == 1
    assert sent[0].body.startswith("Como posso te ajudar?")
    assert sent[0].interactive is not None
    async with db() as session:
        assert (await session.get(Conversation, conversation.id)).flow_state == FlowState.MENU


async def test_entering_with_an_upcoming_appointment_sends_that_card(db) -> None:
    tenant, patient, conversation = await _seed(db)
    await _appointment(db, tenant, patient, start=NOW + timedelta(days=4))
    await _enter(tenant)
    (sent,) = await _outbound(db, conversation)
    assert "Vi aqui que você já tem uma consulta marcada" in sent.body


async def test_entering_after_a_consult_sends_the_post_consult_question(db) -> None:
    tenant, patient, conversation = await _seed(db)
    await _appointment(db, tenant, patient, start=NOW - timedelta(days=1))
    await _enter(tenant)
    (sent,) = await _outbound(db, conversation)
    assert "Como foi a sua consulta do dia" in sent.body


async def test_a_refresh_right_after_does_not_repeat_it(db) -> None:
    tenant, _, conversation = await _seed(db)
    for _ in range(3):
        await _enter(tenant)
    assert len(await _outbound(db, conversation)) == 1


async def test_the_post_consult_question_is_not_repeated_on_a_later_entry(db) -> None:
    """A day later: the question already went out, so the next entry gets the menu."""
    tenant, patient, conversation = await _seed(db)
    await _appointment(db, tenant, patient, start=NOW - timedelta(days=1))
    await _enter(tenant)
    async with db() as session:
        for row in await session.scalars(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.direction == MessageDirection.OUTBOUND,
            )
        ):
            row.created_at = NOW - timedelta(hours=20)
        await session.commit()
    await _enter(tenant)
    bodies = [m.body for m in await _outbound(db, conversation)]
    assert len(bodies) == 2
    assert "Como foi" in bodies[0]
    assert bodies[1].startswith("Como posso te ajudar?")


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"handover": HandoverState.HUMAN_ACTIVE}, "a person from the clinic is handling it"),
        ({"consented": False}, "onboarding (e-mail/name/LGPD) carries its own messages"),
        ({"last_message_age": timedelta(minutes=5)}, "the conversation is live"),
        (
            {"flow_state": FlowState.SERVICE_CATALOG, "last_message_age": timedelta(hours=1)},
            "a booking is mid-way",
        ),
        ({"last_message_age": None}, "an empty thread is the first-contact door's job"),
    ],
)
async def test_entry_stays_silent_when_it_would_talk_over_something(db, kwargs, why) -> None:
    tenant, _, conversation = await _seed(db, **kwargs)
    await _enter(tenant)
    assert await _outbound(db, conversation) == [], why


async def test_a_flow_abandoned_long_ago_gets_the_opening(db) -> None:
    tenant, _, conversation = await _seed(
        db, flow_state=FlowState.SERVICE_CATALOG, last_message_age=timedelta(days=2)
    )
    await _enter(tenant)
    assert len(await _outbound(db, conversation)) == 1


async def test_an_unentitled_clinic_stays_silent_and_a_later_entry_can_still_speak(db) -> None:
    tenant, _, conversation = await _seed(db)
    _Entitled.value = False
    await _enter(tenant)
    assert await _outbound(db, conversation) == []
    _Entitled.value = True
    await _enter(tenant)
    assert len(await _outbound(db, conversation)) == 1


async def test_a_message_that_lands_mid_entry_wins(db, monkeypatch) -> None:
    """The patient typed between the decision and the send: their turn answers, not this."""
    tenant, _, conversation = await _seed(db)
    real = open_job._brain_message_entry_decision

    async def _decide_then_patient_types(*args, **kwargs):
        decision = await real(*args, **kwargs)
        await _message(db, conversation, at=NOW, direction=MessageDirection.INBOUND)
        return decision

    monkeypatch.setattr(open_job, "_brain_message_entry_decision", _decide_then_patient_types)
    await _enter(tenant)
    assert await _outbound(db, conversation) == []


async def test_a_send_that_explodes_releases_the_claim(db, monkeypatch) -> None:
    tenant, _, conversation = await _seed(db)
    real = open_job._send_context_opening
    calls = 0

    async def _boom_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic")
        return await real(*args, **kwargs)

    monkeypatch.setattr(open_job, "_send_context_opening", _boom_once)
    with pytest.raises(RuntimeError):
        await _enter(tenant)
    await _enter(tenant)
    assert len(await _outbound(db, conversation)) == 1


async def test_another_clinics_patient_is_never_reached(db) -> None:
    tenant, _, conversation = await _seed(db)
    _, _, other_conversation = await _seed(db)
    await _enter(tenant)
    assert await _outbound(db, other_conversation) == []
    assert len(await _outbound(db, conversation)) == 1


@pytest.mark.parametrize(
    "wait", [FlowState.AWAITING_NAME, FlowState.AWAITING_EMAIL, FlowState.AWAITING_EMAIL_CODE]
)
async def test_an_open_identity_step_is_never_skipped_even_days_later(db, wait) -> None:
    """Review finding: the name/e-mail/code step must not be erased by an opening."""
    tenant, _, conversation = await _seed(db, flow_state=wait, last_message_age=timedelta(days=2))
    await _enter(tenant)
    assert await _outbound(db, conversation) == []
    async with db() as session:
        assert (await session.get(Conversation, conversation.id)).flow_state == wait


async def test_a_turn_that_lands_while_the_opening_is_composed_wins(db, monkeypatch) -> None:
    """Review finding: the re-read happens right before the write, after composing."""
    tenant, _, conversation = await _seed(db)
    real = opening.resolve_opening_message

    async def _compose_then_patient_taps(*args, **kwargs):
        result = await real(*args, **kwargs)
        await _message(db, conversation, at=NOW, direction=MessageDirection.INBOUND)
        return result

    monkeypatch.setattr(opening, "resolve_opening_message", _compose_then_patient_taps)
    await _enter(tenant)
    assert await _outbound(db, conversation) == []


# --- 3. Route -> queue -> job, end to end ----------------------------------------------------


class _FakeArqPool:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))


async def test_the_open_route_on_a_started_thread_ends_in_the_opening(db, monkeypatch) -> None:
    """What brain-api's `/threads/secretaria/enter` reaches: `/open` answers `exists`, queues
    the entry job, and that job - run as the worker would - leaves the opening in the thread."""
    from httpx import ASGITransport, AsyncClient

    from secretaria.config import get_settings
    from secretaria.core.database import get_session

    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    get_settings.cache_clear()
    from secretaria.main import app

    async def _override_session():
        async with db() as session:
            yield session

    tenant, _, conversation = await _seed(db)
    app.dependency_overrides[get_session] = _override_session
    pool = _FakeArqPool()
    app.state.arq_pool = pool
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/internal/brain-message/open",
                headers={"X-Internal-Api-Key": "test-internal-key"},
                json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID},
            )
    finally:
        app.dependency_overrides.clear()
        app.state.arq_pool = None
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json() == {"status": "exists"}
    ((name, args, kwargs),) = pool.jobs
    assert name == "process_brain_message_enter"
    await getattr(tasks, name)({"redis": None}, *args, **kwargs)
    (sent,) = await _outbound(db, conversation)
    assert sent.body.startswith("Como posso te ajudar?")
