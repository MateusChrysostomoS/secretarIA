"""A patient books at a unit, end to end, on WhatsApp and on the Portal (TASK-083 spec §8).

Everything is real - the inbound wrappers, the orchestrator (which loads each doctor's units),
the router's unit step, `_apply_flow_result`, the booking gate, the code promotion and the
secretarIA's own agenda - except the WhatsApp client, brain-api's code endpoints and the
entitlement read. The doctor attends at two units with different hours:

    Centro   - Mondays 08:00-12:00
    Zona Sul - Tuesdays 14:00-18:00

so the days offered after picking a unit prove that unit's hours drive the lists, and the
appointment row, the agenda's own event and the confirmation carry the unit.
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

from secretaria.ai import graph  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    BookingHold,
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Professional,
    Tenant,
    Unit,
)
from secretaria.models.calendar_event import CalendarEvent  # noqa: E402
from secretaria.models.professional_unit import ProfessionalUnit  # noqa: E402
from secretaria.services import (
    booking_hold as holds,  # noqa: E402
    flow_router as fr,  # noqa: E402
    pii_pseudonymization as pii_store,  # noqa: E402
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_units import STEP_AWAITING_UNIT  # noqa: E402
from secretaria.services.pending_identity import (  # noqa: E402
    RequestCodeOutcome,
    RequestCodeResult,
    VerifyOutcome,
    VerifyResult,
)
from secretaria.workers import tasks  # noqa: E402
from tests._local_agenda import wire_local_agenda  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
EXTERNAL_ID = "00000000-0000-4000-8000-0000000000c1"
WEEK = {
    d: [{"start": "08:00", "end": "18:00"}]
    for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}
CENTRO_HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}
SUL_HOURS = {"tuesday": [{"start": "14:00", "end": "18:00"}]}


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
    """The WhatsApp client: records what was sent, rows included."""

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


@pytest.fixture(autouse=True)
def wired(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(graph, "async_session_factory", db)
    monkeypatch.setattr(pii_store, "async_session_factory", db)
    monkeypatch.setattr(holds, "async_session_factory", db)
    wire_local_agenda(monkeypatch, db)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    _WireClient.sends = []
    monkeypatch.setattr(workers_ns, "WhatsAppClient", _WireClient)

    async def _none(*_a, **_k):
        return None

    async def _token(session, tenant_id):
        return "decrypted-waba-token"

    async def _entitlements(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id), status="active", active=True, secretaria_enabled=True,
            plan="bronze", secretaria_tier="basico", addons={}, limits={},
        )

    async def _request(tenant_id, external_id):
        return RequestCodeResult(RequestCodeOutcome.SENT, email_masked="m***@x.com")

    async def _verify(tenant_id, external_id, code):
        return VerifyResult(VerifyOutcome.VERIFIED)

    async def _hooks(*_a, **_k):
        return None

    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _none)
    monkeypatch.setattr(workers_ns, "get_waba_token", _token)
    monkeypatch.setattr(workers_ns, "get_entitlements", _entitlements)
    monkeypatch.setattr(holds, "request_code", _request)
    monkeypatch.setattr(workers_ns, "request_code", _request)
    monkeypatch.setattr(workers_ns, "verify_code", _verify)
    monkeypatch.setattr(workers_ns, "enqueue_post_booking_hooks", _hooks)
    return db


async def _seed(db, *, channel: str, units: int):
    """A one-doctor clinic (no Google: the secretarIA agenda) and a consented patient whose
    conversation sits on the service card ("Deseja agendar esse serviço?")."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            timezone="America/Sao_Paulo",
            business_hours=WEEK,
            initial_flows={},
            appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
        )
        session.add(tenant)
        await session.flush()
        doctor = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        session.add(doctor)
        centro = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro", address="Rua A, 10")
        sul = Unit(id=uuid4(), tenant_id=tenant.id, name="Zona Sul", address="Av. B, 20")
        session.add_all([centro, sul])
        await session.flush()
        for position, (unit, hours) in enumerate(((centro, CENTRO_HOURS), (sul, SUL_HOURS))):
            if position < units:
                session.add(
                    ProfessionalUnit(
                        professional_id=doctor.id, unit_id=unit.id, business_hours=hours,
                        sort_order=position,
                    )
                )
        portal = channel == CHANNEL_BRAIN_MESSAGE
        patient = Patient(
            id=uuid4(),
            tenant_id=tenant.id,
            channel=channel,
            external_id=EXTERNAL_ID if portal else None,
            wa_id=None if portal else WA_ID,
            name="Maria",
            lgpd_accepted_at=datetime.now(UTC),
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=fr.STEP_AWAITING_SERVICE_CONFIRM,
            flow_selected_type="Consulta",
            flow_attendee_name="",
        )
        session.add(conversation)
        await session.flush()
        # The service card the patient is answering: not a first contact (no greeting).
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.BOT,
                body="Consulta\n\nDeseja agendar esse serviço?",
            )
        )
        await session.commit()
        return tenant, doctor, centro, sul, conversation


_seq = iter(range(1, 100_000))


async def _turn(tenant, channel: str, body: str, reply_id: str | None = None):
    if channel == CHANNEL_BRAIN_MESSAGE:
        reply = await tasks._persist_brain_message_inbound(
            tenant_id=tenant.id,
            external_id=EXTERNAL_ID,
            text=body,
            patient_name="Maria",
            interactive_reply_id=reply_id,
        )
    else:
        reply = await tasks._persist_inbound_message(
            phone_number_id=PHONE_NUMBER_ID,
            wa_id=WA_ID,
            patient_name="Maria",
            wam_id=f"wamid.in.{next(_seq)}",
            body=body,
            interactive_reply_id=reply_id,
        )
    assert reply is not None
    await tasks._send_bot_reply(reply, redis=None)
    return reply


async def _conversation(db, conversation_id) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, conversation_id)


def _next(weekday: int) -> datetime:
    """The next `weekday` (Mon=0) at least two days ahead, clinic-local midnight."""
    day = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day


def _offered_days(rows) -> set[int]:
    return {
        datetime.fromisoformat(row_id.split("|")[1]).weekday()
        for row_id, *_ in rows
        if row_id.startswith("day|")
    }


async def _pick_sul_and_a_time(db, tenant, channel, sul, conversation):
    """"Sim" on the service card -> the unit list -> Zona Sul -> a Tuesday -> 14:00."""
    await _turn(tenant, channel, fr.LABEL_BOOK_SERVICE)
    row = await _conversation(db, conversation.id)
    assert row.flow_step == STEP_AWAITING_UNIT
    assert row.flow_selected_unit_id is None

    await _turn(tenant, channel, "Zona Sul", f"unit|{sul.id}")
    row = await _conversation(db, conversation.id)
    assert row.flow_step == fr.STEP_AWAITING_DAY
    assert row.flow_selected_unit_id == sul.id

    tuesday = _next(1)
    day_id = f"day|{tuesday.date().isoformat()}|0"
    await _turn(tenant, channel, "Ter", day_id)
    row = await _conversation(db, conversation.id)
    assert row.flow_step == fr.STEP_AWAITING_SLOT
    assert row.flow_selected_unit_id == sul.id

    slot = tuesday.replace(hour=14).replace(tzinfo=None).isoformat(timespec="minutes")
    await _turn(tenant, channel, "14:00", f"slot|{slot}")
    row = await _conversation(db, conversation.id)
    assert row.flow_step == fr.STEP_AWAITING_CONFIRMATION
    assert row.flow_selected_unit_id == sul.id
    return tuesday.replace(hour=14)


@pytest.mark.asyncio
async def test_whatsapp_books_at_the_chosen_unit_within_its_hours(db):
    tenant, _doctor, _centro, sul, conversation = await _seed(db, channel="whatsapp", units=2)

    await _turn(tenant, "whatsapp", fr.LABEL_BOOK_SERVICE)
    kind, body, rows = _WireClient.sends[-1]
    assert kind == "list" and body == "Em qual unidade você prefere ser atendido(a)?"
    assert [r[1] for r in rows[:2]] == ["Centro", "Zona Sul"]

    await _turn(tenant, "whatsapp", "Zona Sul", f"unit|{sul.id}")
    kind, _body, rows = _WireClient.sends[-1]
    assert kind == "list"
    assert _offered_days(rows) == {1}  # Zona Sul: Tuesdays only
    assert rows[-1][1] == "⬅️ Outra unidade"

    # Restart from the service card and book.
    async with db() as session, session.begin():
        row = await session.get(Conversation, conversation.id)
        row.flow_step, row.flow_selected_unit_id = fr.STEP_AWAITING_SERVICE_CONFIRM, None
    start_local = await _pick_sul_and_a_time(db, tenant, "whatsapp", sul, conversation)
    assert "Local: Zona Sul — Av. B, 20" in _WireClient.sends[-1][1]

    await _turn(tenant, "whatsapp", fr.LABEL_CONFIRM)
    async with db() as session:
        [appointment] = list(await session.scalars(select(Appointment)))
        [event] = list(await session.scalars(select(CalendarEvent)))
    assert appointment.unit_id == sul.id
    assert event.unit_id == sul.id and event.id == appointment.google_event_id
    assert event.start_at.replace(tzinfo=UTC) == start_local.astimezone(UTC)
    confirmation = _WireClient.sends[-1][1]
    assert "Pronto! Seu agendamento está confirmado." in confirmation
    assert "Local: Zona Sul — Av. B, 20" in confirmation
    assert (await _conversation(db, conversation.id)).flow_selected_unit_id is None


@pytest.mark.asyncio
async def test_whatsapp_with_one_unit_skips_the_question_and_uses_its_hours(db):
    tenant, _doctor, centro, _sul, conversation = await _seed(db, channel="whatsapp", units=1)
    await _turn(tenant, "whatsapp", fr.LABEL_BOOK_SERVICE)
    kind, _body, rows = _WireClient.sends[-1]
    assert kind == "list"
    assert _offered_days(rows) == {0}  # Centro: Mondays only
    row = await _conversation(db, conversation.id)
    assert row.flow_step == fr.STEP_AWAITING_DAY
    assert row.flow_selected_unit_id == centro.id


@pytest.mark.asyncio
async def test_whatsapp_without_units_is_todays_flow(db):
    tenant, _doctor, _centro, _sul, conversation = await _seed(db, channel="whatsapp", units=0)
    await _turn(tenant, "whatsapp", fr.LABEL_BOOK_SERVICE)
    kind, _body, rows = _WireClient.sends[-1]
    assert kind == "list"
    assert len(_offered_days(rows)) > 2  # the clinic's whole week
    row = await _conversation(db, conversation.id)
    assert row.flow_step == fr.STEP_AWAITING_DAY
    assert row.flow_selected_unit_id is None


@pytest.mark.asyncio
async def test_the_portal_holds_with_the_unit_and_the_code_books_at_it(db):
    tenant, _doctor, _centro, sul, conversation = await _seed(
        db, channel=CHANNEL_BRAIN_MESSAGE, units=2
    )
    await _pick_sul_and_a_time(db, tenant, CHANNEL_BRAIN_MESSAGE, sul, conversation)

    await _turn(tenant, CHANNEL_BRAIN_MESSAGE, fr.LABEL_CONFIRM)
    async with db() as session:
        [hold] = list(await session.scalars(select(BookingHold)))
        assert list(await session.scalars(select(Appointment))) == []
    assert hold.unit_id == sul.id
    assert (await _conversation(db, conversation.id)).flow_state is FlowState.AWAITING_EMAIL_CODE

    extra = await tasks._promote_booking_hold(
        tasks._ReplyContext(
            channel=CHANNEL_BRAIN_MESSAGE,
            conversation_id=conversation.id,
            tenant_id=None,
            patient_ref=EXTERNAL_ID,
            inbound_body="123456",
        ),
        tenant=tenant,
        waba_token=None,
        redis=None,
    )
    async with db() as session:
        [appointment] = list(await session.scalars(select(Appointment)))
        [event] = list(await session.scalars(select(CalendarEvent)))
    assert appointment.unit_id == sul.id
    assert event.unit_id == sul.id
    assert "Local: Zona Sul — Av. B, 20" in extra
