"""Express confirmation end to end in the worker (TASK-030 P3, spec §4.4).

The AI's full draft reaches `_handle_set_booking_draft` (P2b) on a clinic with the AI
draft v2 switch: the patient receives the booking details and the confirmation card, and
NOTHING is booked until "Confirmar" - which runs EXACTLY the button flow's
`flow_router._handle_confirmation` (the Portal code gate and holds included).
In-memory SQLite, same pattern as tests/test_handback_events.py.
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

import json  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, TextBubble  # noqa: E402
from secretaria.ai.graph import BOOKING_DRAFT_SENTINEL_PREFIX  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    BookingHold,
    Conversation,
    FlowState,
    Patient,
    Professional,
    Tenant,
    Unit,
)
from secretaria.services import (  # noqa: E402
    booking_hold as booking_hold_service,
    flow_router as fr,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, CHANNEL_WHATSAPP  # noqa: E402
from secretaria.services.pending_identity import (  # noqa: E402
    BOOKING_SLOT_TAKEN_MESSAGE,
    RequestCodeOutcome,
    RequestCodeResult,
)
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import handback_log  # noqa: E402
from secretaria.workers.shared.flow_runner import _run_flow  # noqa: E402
from secretaria.workers.shared.greeting import (  # noqa: E402
    _flow_professionals,
    _flow_tenant_snapshot,
)

TZ = ZoneInfo("America/Sao_Paulo")
DAY = (datetime.now(TZ) + timedelta(days=3)).date()
WHEN = f"{DAY.strftime('%d/%m/%Y')} às 10:00"
WA_ID = "5511999999999"
EXTERNAL_ID = "00000000-0000-4000-8000-0000000000aa"
OTHER_EXTERNAL_ID = "00000000-0000-4000-8000-0000000000bb"
ADDRESS = {
    "line": "Rua A, 1",
    "neighborhood": "Centro",
    "city": "São Paulo",
    "state": "SP",
    "postal_code": "01000-000",
}
ADDRESS_LINE = "Endereço: Rua A, 1, Centro, São Paulo/SP, CEP 01000-000"
SERVICE = {
    "name": "Consulta",
    "duration_min": 30,
    "is_active": True,
    "sort_order": 0,
    "price": "250",
    "long_description": "Avaliação completa.",
    "requirements": ["Jejum de 8 horas"],
}
EVERY_DAY = {
    day: [{"start": "08:00", "end": "18:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}
DRAFT = {"t": "Consulta", "w": "self", "d": DAY.isoformat(), "h": "10:00"}


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


class _Agenda:
    """The doctor's agenda: 10:00 and 10:30 free on DAY. Records every event created."""

    def __init__(self):
        self.tzinfo = TZ
        self.created: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        return [datetime(DAY.year, DAY.month, DAY.day, tzinfo=TZ)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if day.date() != DAY:
            return []
        iso = DAY.isoformat()
        return [{"start": f"{iso}T{t}", "end": "", "label": t} for t in ("10:00", "10:30")][
            :max_slots
        ]

    async def create_event(self, start, end, summary, description=""):
        self.created.append((start, end, summary))
        return {"id": f"evt-{len(self.created)}", "htmlLink": "https://calendar.google.com/x"}


class _LogRecorder:
    """Records every structlog call - `caplog` is empty for structlog in this suite."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **fields) -> None:
            self.records.append((level, event, fields))

        return _log


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, db):
    agenda = _Agenda()
    sent: list = []
    notices: list = []
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(booking_hold_service, "async_session_factory", db)

    async def _calendar(session, tenant, target):
        return agenda

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _calendar)
    monkeypatch.setattr(workers_ns, "_flow_turn_calendar", lambda conv, config, cal: agenda)

    async def _dispatch(reply, bubbles, tenant=None, waba_token=None):
        sent.extend(bubbles)
        return len(bubbles)

    monkeypatch.setattr(workers_ns, "_dispatch_bubbles", _dispatch)

    async def _notice(reply, hold, *, tenant=None, waba_token=None):
        notices.append(hold)

    monkeypatch.setattr(workers_ns, "_send_booking_gate_notice", _notice)

    async def _no_hooks(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workers_ns, "enqueue_post_booking_hooks", _no_hooks)

    async def _code(tenant_id, external_id):
        return RequestCodeResult(RequestCodeOutcome.SENT, email_masked="m***a@gmail.com")

    monkeypatch.setattr(booking_hold_service, "request_code", _code)
    log = _LogRecorder()
    monkeypatch.setattr(workers_ns, "logger", log)
    return SimpleNamespace(db=db, agenda=agenda, sent=sent, notices=notices, log=log)


def _ref(channel: str) -> str:
    return WA_ID if channel == CHANNEL_WHATSAPP else EXTERNAL_ID


def _reply(conversation, channel: str, body: str) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        channel=channel,
        conversation_id=conversation.id,
        patient_ref=_ref(channel),
        inbound_body=body,
    )


async def _seed(db, *, channel=CHANNEL_WHATSAPP, address=ADDRESS, units=0):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
            timezone="America/Sao_Paulo",
            initial_flows={"ai_draft_v2": True},
            appointment_types=[dict(SERVICE)],
            business_hours=EVERY_DAY,
            address=address,
        )
        session.add(tenant)
        await session.flush()
        doctor = Professional(
            tenant_id=tenant.id, name="Dra. Única", specialty="Clínica Geral", is_active=True
        )
        session.add(doctor)
        for index in range(units):
            session.add(
                Unit(
                    tenant_id=tenant.id, name=f"Unidade {index}", address="Rua B, 2", is_active=True
                )
            )
        if channel == CHANNEL_WHATSAPP:
            patient = Patient(tenant_id=tenant.id, wa_id=WA_ID, name="Maria")
        else:
            patient = Patient(
                tenant_id=tenant.id,
                channel=CHANNEL_BRAIN_MESSAGE,
                external_id=EXTERNAL_ID,
                name="Maria",
                lgpd_accepted_at=datetime.now(UTC),
            )
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, flow_state=FlowState.LLM
        )
        session.add(conversation)
        await session.commit()
        for obj in (tenant, doctor, patient, conversation):
            await session.refresh(obj)
        return tenant, doctor, patient, conversation


async def _other_portal_conversation(db, tenant) -> Conversation:
    async with db() as session:
        patient = Patient(
            tenant_id=tenant.id,
            channel=CHANNEL_BRAIN_MESSAGE,
            external_id=OTHER_EXTERNAL_ID,
            name="Outra",
            lgpd_accepted_at=datetime.now(UTC),
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        return conversation


async def _land(tenant, conversation, channel=CHANNEL_WHATSAPP) -> None:
    await tasks._handle_set_booking_draft(
        _reply(conversation, channel, "quinta às 10h, pra mim"),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps(DRAFT),
        tenant,
        None,
        [],
        _ref(channel),
    )


async def _row(db, conversation) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, conversation.id)


async def _confirm(wired, tenant, conversation, channel=CHANNEL_WHATSAPP) -> bool:
    """Tap "Confirmar" through the live flow runner, exactly like a real turn."""
    async with wired.db() as session:
        row = await session.get(Conversation, conversation.id)
        rows = list(
            (
                await session.scalars(
                    select(Professional).where(Professional.tenant_id == tenant.id)
                )
            ).all()
        )
    snapshot = SimpleNamespace(
        id=row.id,
        tenant_id=row.tenant_id,
        patient_id=row.patient_id,
        flow_state=row.flow_state,
        flow_step=row.flow_step,
        flow_selected_type=row.flow_selected_type,
        flow_selected_day=row.flow_selected_day,
        flow_selected_slot=row.flow_selected_slot,
        flow_selected_professional_id=row.flow_selected_professional_id,
        flow_selected_insurance=row.flow_selected_insurance,
        flow_managing_appointment_id=row.flow_managing_appointment_id,
        flow_attendee_name=row.flow_attendee_name,
        flow_draft=row.flow_draft,
    )
    return await _run_flow(
        _reply(conversation, channel, fr.LABEL_CONFIRM),
        snapshot,
        _flow_tenant_snapshot(tenant, rows, [], None),
        None,
        "Maria",
        _ref(channel),
        tenant=tenant,
        waba_token="t",
        professionals=_flow_professionals(rows, []),
    )


async def _count(db, model, tenant) -> int:
    async with db() as session:
        return await session.scalar(
            select(func.count()).select_from(model).where(model.tenant_id == tenant.id)
        )


def _events(log: _LogRecorder, name: str = handback_log.EVENT_NAME) -> list[dict]:
    return [fields for _level, event, fields in log.records if event == name]


# --------------------------------------------------------------------------
# The landing: details + card, nothing booked
# --------------------------------------------------------------------------


async def test_a_full_draft_lands_on_the_details_and_the_card_and_books_nothing(wired):
    tenant, _doctor, _patient, conversation = await _seed(wired.db)

    await _land(tenant, conversation)

    details, card = wired.sent
    assert isinstance(details, TextBubble) and isinstance(card, ButtonBubble)
    assert details.body == (
        "Confira os detalhes da sua consulta:\n\n"
        "🥼 Profissional: Dra. Única — Clínica Geral\n"
        "🏥 Serviço: Consulta — R$250\n"
        "👤 Para: você\n"
        f"{ADDRESS_LINE}\n\n"
        "Avaliação completa.\n\n"
        "O que levar / preparo:\n"
        "• Jejum de 8 horas"
    )
    assert card.body == f"Consulta\nProfissional: Dra. Única\n{WHEN}"
    row = await _row(wired.db, conversation)
    assert row.flow_step == fr.STEP_AWAITING_CONFIRMATION
    assert (row.flow_selected_day, row.flow_selected_slot) == (
        DAY.isoformat(),
        f"{DAY.isoformat()}T10:00",
    )
    assert row.flow_draft is None
    # Nothing the AI sends creates a booking by itself.
    assert wired.agenda.created == []
    assert await _count(wired.db, Appointment, tenant) == 0
    assert await _count(wired.db, BookingHold, tenant) == 0


async def test_the_hand_back_is_logged_on_the_confirmation_card(wired):
    tenant, _doctor, _patient, conversation = await _seed(wired.db)

    await _land(tenant, conversation)

    (event,) = _events(wired.log)
    assert event["source_tool"] == "set_booking_draft"
    assert event["landing_step"] == "awaiting_confirmation"
    assert event["accepted"] == ["service", "for_whom", "day", "time"]
    assert event["dropped"] == {}
    assert event["fallback"] is None
    assert event["topology"] == "sole"


@pytest.mark.parametrize(
    "address, shown",
    [(ADDRESS, True), (None, False), ({"city": "Recife"}, False)],
)
async def test_the_clinic_address_is_shown_only_when_stored(wired, address, shown):
    tenant, _doctor, _patient, conversation = await _seed(wired.db, address=address)

    await _land(tenant, conversation)

    assert ("Endereço:" in wired.sent[0].body) is shown


async def test_a_clinic_with_units_never_shows_the_clinic_address(wired):
    tenant, _doctor, _patient, conversation = await _seed(wired.db, units=1)

    await _land(tenant, conversation)

    assert "Endereço:" not in wired.sent[0].body


# --------------------------------------------------------------------------
# "Confirmar" is today's path
# --------------------------------------------------------------------------


async def test_confirmar_on_whatsapp_books_through_todays_path(wired):
    tenant, doctor, _patient, conversation = await _seed(wired.db)
    await _land(tenant, conversation)

    await _confirm(wired, tenant, conversation)

    assert len(wired.agenda.created) == 1
    async with wired.db() as session:
        (appointment,) = (
            await session.scalars(select(Appointment).where(Appointment.tenant_id == tenant.id))
        ).all()
    # The sole doctor owns the booking (resolve_booking_owner_id), as on the button path.
    assert appointment.professional_id == doctor.id
    assert appointment.appointment_type == "Consulta"
    assert "Pronto! Seu agendamento está confirmado." in wired.sent[-1].body
    assert (await _row(wired.db, conversation)).flow_state == FlowState.IDLE


async def test_tapping_confirmar_twice_books_once(wired):
    tenant, _doctor, _patient, conversation = await _seed(wired.db)
    await _land(tenant, conversation)

    await _confirm(wired, tenant, conversation)
    await _confirm(wired, tenant, conversation)

    assert len(wired.agenda.created) == 1
    assert await _count(wired.db, Appointment, tenant) == 1
    # The second tap reaches an IDLE conversation, which re-presents the menu.
    assert (await _row(wired.db, conversation)).flow_state == FlowState.MENU


async def test_confirmar_on_the_portal_holds_the_slot_and_asks_for_the_code(wired):
    tenant, _doctor, _patient, conversation = await _seed(wired.db, channel=CHANNEL_BRAIN_MESSAGE)
    await _land(tenant, conversation, CHANNEL_BRAIN_MESSAGE)

    await _confirm(wired, tenant, conversation, CHANNEL_BRAIN_MESSAGE)

    assert wired.agenda.created == []
    assert await _count(wired.db, Appointment, tenant) == 0
    assert await _count(wired.db, BookingHold, tenant) == 1
    assert len(wired.notices) == 1
    assert (await _row(wired.db, conversation)).flow_state == FlowState.AWAITING_EMAIL_CODE


async def test_a_slot_taken_before_confirmar_on_the_portal_is_offered_again(wired):
    tenant, doctor, _patient, conversation = await _seed(wired.db, channel=CHANNEL_BRAIN_MESSAGE)
    await _land(tenant, conversation, CHANNEL_BRAIN_MESSAGE)
    other = await _other_portal_conversation(wired.db, tenant)
    # SQLite drops offsets instead of normalizing TIMESTAMPTZ like PostgreSQL.
    start = datetime(DAY.year, DAY.month, DAY.day, 10, 0, tzinfo=TZ).astimezone(UTC)
    # Somebody else confirms the very same window first - on the sole doctor's agenda,
    # the owner a hold is PLACED with.
    assert (
        await booking_hold_service.place_hold(
            tenant_id=tenant.id,
            conversation_id=other.id,
            patient_id=None,
            professional_id=doctor.id,
            appointment_type="Consulta",
            insurance=None,
            start_at=start,
            end_at=start + timedelta(minutes=30),
        )
        is not None
    )

    await _confirm(wired, tenant, conversation, CHANNEL_BRAIN_MESSAGE)

    assert (await _row(wired.db, conversation)).flow_step == fr.STEP_AWAITING_RETRY
    assert wired.sent[-1].body == BOOKING_SLOT_TAKEN_MESSAGE
    assert wired.agenda.created == []
    assert await _count(wired.db, Appointment, tenant) == 0


async def test_whatsapp_confirmar_does_not_reread_the_agenda_exactly_like_the_button_card(wired):
    """PRE-EXISTING, pinned so P3 provably leaves it alone (plan, "Decisões" 6).

    On WhatsApp the gate is unarmed and `_handle_confirmation` never re-reads the agenda
    nor the holds before creating the event - for the card the slot tap draws as for the
    express card. Whether to add a re-check for BOTH is the owner's call; until then the
    two must behave the same.
    """
    tenant, doctor, _patient, conversation = await _seed(wired.db)
    await _land(tenant, conversation)
    other = await _other_portal_conversation(wired.db, tenant)
    start = datetime(DAY.year, DAY.month, DAY.day, 10, 0, tzinfo=TZ)
    await booking_hold_service.place_hold(
        tenant_id=tenant.id,
        conversation_id=other.id,
        patient_id=None,
        professional_id=doctor.id,
        appointment_type="Consulta",
        insurance=None,
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )

    await _confirm(wired, tenant, conversation)

    assert len(wired.agenda.created) == 1
    assert await _count(wired.db, Appointment, tenant) == 1
