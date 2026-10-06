"""manage_existing_appointment v2 through the worker (TASK-030 P3, spec §4.5).

The AI names WHICH of the patient's appointments and, for a reschedule, the new day and
time; the worker lands it on the buttons' own steps - never further than a card - after
the same deposit pre-check the buttons run. In-memory SQLite.
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

import datetime as dt  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    HandoverState,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Professional,
    Tenant,
)
from secretaria.services import (  # noqa: E402
    booking_hold as booking_hold_service,
    flow_router as fr,
    manage_request,
)
from secretaria.services.manage_request import ManageRequest  # noqa: E402
from secretaria.services.patient_context import load_upcoming_appointments  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import handback_log  # noqa: E402
from secretaria.workers.shared.flow_runner import _run_flow  # noqa: E402
from secretaria.workers.shared.greeting import (  # noqa: E402
    _flow_professionals,
    _flow_tenant_snapshot,
)

TZ = ZoneInfo("America/Sao_Paulo")
TODAY = datetime.now(TZ).date()
FIRST_DAY = TODAY + timedelta(days=2)
SECOND_DAY = TODAY + timedelta(days=4)
NEW_DAY = TODAY + timedelta(days=6)
WA_ID = "5511999999999"
OTHER_WA_ID = "5511900000000"
EVERY_DAY = {
    day: [{"start": "08:00", "end": "18:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


def _local(day: dt.date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)


def _ref(day: dt.date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0)


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
    """The owner's agenda: 14:00 and 14:30 free on NEW_DAY. Records writes and reads."""

    def __init__(self):
        self.tzinfo = TZ
        self.day_reads: list = []
        self.slot_reads: list = []
        self.updated: list = []
        self.cancelled: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.day_reads.append(start_day)
        return [datetime(NEW_DAY.year, NEW_DAY.month, NEW_DAY.day, tzinfo=TZ)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        self.slot_reads.append(day.date())
        if day.date() != NEW_DAY:
            return []
        iso = NEW_DAY.isoformat()
        return [{"start": f"{iso}T{t}", "end": "", "label": t} for t in ("14:00", "14:30")][
            :max_slots
        ]

    async def update_event(self, event_id, start, end):
        self.updated.append((event_id, start, end))
        return {"id": event_id}

    async def cancel_event(self, event_id):
        self.cancelled.append(event_id)


class _FakeWhatsAppClient:
    persists_outbound = False
    created: list = []

    def __init__(self):
        self.sent: list = []
        _FakeWhatsAppClient.created.append(self)

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", body, buttons))
        return {"messages": [{"id": "wamid.test"}]}

    async def send_text_message(self, to, body):
        self.sent.append(("text", body))
        return {"messages": [{"id": "wamid.test"}]}


class _LogRecorder:
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
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(booking_hold_service, "async_session_factory", db)

    async def _calendar(session, tenant, target):
        return agenda

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _calendar)

    async def _dispatch(reply, bubbles, tenant=None, waba_token=None):
        sent.extend(bubbles)
        return len(bubbles)

    monkeypatch.setattr(workers_ns, "_dispatch_bubbles", _dispatch)
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(workers_ns, "WhatsAppClient", _FakeWhatsAppClient)
    log = _LogRecorder()
    monkeypatch.setattr(workers_ns, "logger", log)
    return SimpleNamespace(db=db, agenda=agenda, sent=sent, log=log)


async def _seed(db, *, switch=True, own=1, limit=2, deposit_count=None):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
            timezone="America/Sao_Paulo",
            initial_flows={"ai_draft_v2": True} if switch else {},
            appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
            business_hours=EVERY_DAY,
            pix_deposit_enabled=True,
            pix_reschedule_limit=limit,
        )
        session.add(tenant)
        await session.flush()
        doctor = Professional(tenant_id=tenant.id, name="Dra. Única", is_active=True)
        patient = Patient(tenant_id=tenant.id, wa_id=WA_ID, name="Maria")
        other = Patient(tenant_id=tenant.id, wa_id=OTHER_WA_ID, name="Outra")
        session.add_all([doctor, patient, other])
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, flow_state=FlowState.LLM
        )
        other_conversation = Conversation(tenant_id=tenant.id, patient_id=other.id)
        session.add_all([conversation, other_conversation])
        appointments = []
        for day in (FIRST_DAY, SECOND_DAY)[:own]:
            start = _local(day, 9).astimezone(UTC)
            appointments.append(
                Appointment(
                    tenant_id=tenant.id,
                    patient_id=patient.id,
                    professional_id=doctor.id,
                    google_event_id=f"evt-{uuid4()}",
                    appointment_type="Consulta",
                    start_at=start,
                    end_at=start + timedelta(minutes=30),
                    status=AppointmentStatus.SCHEDULED,
                )
            )
        other_start = _local(FIRST_DAY, 11).astimezone(UTC)
        others_appointment = Appointment(
            tenant_id=tenant.id,
            patient_id=other.id,
            professional_id=doctor.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=other_start,
            end_at=other_start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add_all([*appointments, others_appointment])
        await session.flush()
        if deposit_count is not None:
            session.add(
                PixDeposit(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    appointment_id=appointments[0].id,
                    patient_id=patient.id,
                    asaas_payment_id=f"pay-{uuid4()}",
                    amount_cents=10000,
                    percent_applied=30,
                    status=PixDepositStatus.PAID,
                    reschedule_count=deposit_count,
                )
            )
        await session.commit()
        for obj in (
            tenant,
            doctor,
            patient,
            conversation,
            other_conversation,
            *appointments,
            others_appointment,
        ):
            await session.refresh(obj)
        return SimpleNamespace(
            tenant=tenant,
            doctor=doctor,
            patient=patient,
            conversation=conversation,
            other_conversation=other_conversation,
            appointments=appointments,
            others_appointment=others_appointment,
        )


def _reply(conversation, body="remarca pra mim") -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=conversation.id, patient_ref=WA_ID, inbound_body=body
    )


async def _manage(bundle, request: ManageRequest) -> None:
    await tasks._handle_manage_appointment(
        _reply(bundle.conversation),
        request.to_payload(),
        bundle.tenant,
        _flow_professionals([bundle.doctor], []),
        WA_ID,
        waba_token="t",
    )


async def _row(db, conversation) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, conversation.id)


def _events(log: _LogRecorder, name: str = handback_log.EVENT_NAME) -> list[dict]:
    return [fields for _level, event, fields in log.records if event == name]


async def test_cancel_with_the_appointment_named_stops_at_the_cancel_card(wired):
    bundle = await _seed(wired.db, own=2)

    await _manage(bundle, ManageRequest("cancel", appointment=_ref(SECOND_DAY, 9)))

    row = await _row(wired.db, bundle.conversation)
    assert row.flow_step == fr.STEP_MANAGE_CANCEL_CONFIRM
    assert row.flow_managing_appointment_id == bundle.appointments[1].id
    assert wired.sent[-1].body.startswith("Confirmar o cancelamento?")
    assert wired.agenda.cancelled == []
    (event,) = _events(wired.log)
    assert event["source_tool"] == "manage_existing_appointment"
    assert event["landing_step"] == "manage_cancel_confirm"
    assert event["supplied"] == ["action", "appointment"]
    assert event["accepted"] == ["action", "appointment"]


async def test_another_patients_appointment_is_never_targeted(wired):
    bundle = await _seed(wired.db)

    await _manage(bundle, ManageRequest("cancel", appointment=_ref(FIRST_DAY, 11)))

    row = await _row(wired.db, bundle.conversation)
    assert row.flow_managing_appointment_id is None
    assert row.flow_step == fr.STEP_MANAGE_PICK_CANCEL
    (event,) = _events(wired.log)
    assert event["dropped"] == {"appointment": "unknown_appointment"}
    async with wired.db() as session:
        untouched = await session.get(Appointment, bundle.others_appointment.id)
    assert untouched.status == AppointmentStatus.SCHEDULED


async def test_reschedule_to_a_free_time_lands_on_the_reschedule_card(wired):
    bundle = await _seed(wired.db)

    await _manage(
        bundle,
        ManageRequest(
            "reschedule", appointment=_ref(FIRST_DAY, 9), day=NEW_DAY, time=dt.time(14, 0)
        ),
    )

    row = await _row(wired.db, bundle.conversation)
    assert row.flow_step == fr.STEP_MANAGE_CONFIRM
    assert row.flow_selected_slot == f"{NEW_DAY.isoformat()}T14:00"
    assert wired.sent[-1].body == (
        f"Remarcar para:\nConsulta\n{NEW_DAY.strftime('%d/%m/%Y')} às 14:00"
    )
    assert wired.agenda.updated == []  # nothing moved yet
    (event,) = _events(wired.log)
    assert event["landing_step"] == "manage_confirm"
    assert event["accepted"] == ["action", "appointment", "day", "time"]


async def test_confirmar_on_that_card_moves_it_through_the_button_path(wired):
    bundle = await _seed(wired.db)
    await _manage(
        bundle,
        ManageRequest(
            "reschedule", appointment=_ref(FIRST_DAY, 9), day=NEW_DAY, time=dt.time(14, 0)
        ),
    )
    async with wired.db() as session:
        row = await session.get(Conversation, bundle.conversation.id)
        upcoming = await load_upcoming_appointments(session, bundle.tenant.id, bundle.patient.id)
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

    await _run_flow(
        _reply(bundle.conversation, fr.LABEL_CONFIRM),
        snapshot,
        _flow_tenant_snapshot(bundle.tenant, [bundle.doctor], [], None),
        None,
        "Maria",
        WA_ID,
        upcoming_appointments=upcoming,
        tenant=bundle.tenant,
        waba_token="t",
        professionals=_flow_professionals([bundle.doctor], []),
        manage_calendar=wired.agenda,
        manage_calendar_owned=True,
    )

    assert len(wired.agenda.updated) == 1
    async with wired.db() as session:
        moved = await session.get(Appointment, bundle.appointments[0].id)
    assert moved.status == AppointmentStatus.RESCHEDULED
    assert wired.agenda.updated[0][1].astimezone(UTC) == _local(NEW_DAY, 14).astimezone(UTC)
    # SQLite drops the offset on aware writes; PostgreSQL normalizes this existing path.
    assert moved.start_at == _local(NEW_DAY, 14).replace(tzinfo=None)


async def test_a_reschedule_at_the_deposit_limit_gets_the_keep_or_cancel_answer(wired):
    bundle = await _seed(wired.db, limit=1, deposit_count=1)

    await _manage(
        bundle,
        ManageRequest(
            "reschedule", appointment=_ref(FIRST_DAY, 9), day=NEW_DAY, time=dt.time(14, 0)
        ),
    )

    kind, _body, buttons = _FakeWhatsAppClient.created[-1].sent[0]
    assert kind == "buttons"
    appointment_id = bundle.appointments[0].id
    assert [bid for bid, _label in buttons] == [
        f"apptconfirm|{appointment_id}",
        f"apptcancel|{appointment_id}",
    ]
    row = await _row(wired.db, bundle.conversation)
    assert row.flow_state == FlowState.MENU
    assert row.flow_managing_appointment_id is None
    assert wired.agenda.slot_reads == []  # no new time was ever computed
    assert wired.agenda.day_reads == []
    (event,) = _events(wired.log)
    assert event["landing_step"] == "menu"
    assert event["fallback"] == "reschedule_limit"


async def test_a_held_new_time_lands_on_that_days_slot_list(wired):
    bundle = await _seed(wired.db)
    start = _local(NEW_DAY, 14).astimezone(UTC)
    await booking_hold_service.place_hold(
        tenant_id=bundle.tenant.id,
        conversation_id=bundle.other_conversation.id,
        patient_id=None,
        professional_id=bundle.doctor.id,
        appointment_type="Consulta",
        insurance=None,
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )

    await _manage(bundle, ManageRequest("reschedule", day=NEW_DAY, time=dt.time(14, 0)))

    row = await _row(wired.db, bundle.conversation)
    assert row.flow_step == fr.STEP_MANAGE_SLOT
    rows = [r[0] for r in wired.sent[-1].rows]
    assert f"slot|{NEW_DAY.isoformat()}T14:00" not in rows
    (event,) = _events(wired.log)
    assert event["dropped"] == {"time": "no_free_slot"}


async def test_switch_off_ignores_the_v2_fields(wired):
    bundle = await _seed(wired.db, switch=False)

    await _manage(bundle, ManageRequest("reschedule", day=NEW_DAY, time=dt.time(14, 0)))

    row = await _row(wired.db, bundle.conversation)
    assert row.flow_step == fr.STEP_MANAGE_DAY  # v1: the single appointment's day picker
    (event,) = _events(wired.log)
    assert event["supplied"] == ["action"]


def test_the_manage_resolver_speaks_the_hand_back_vocabulary():
    assert set(manage_request.DROP_REASONS) <= handback_log.DROP_REASONS
    assert set(manage_request.FALLBACK_REASONS) <= handback_log.FALLBACK_REASONS
    assert set(manage_request.FIELD_NAMES) <= handback_log.FIELD_NAMES
    assert handback_log.FALLBACK_RESCHEDULE_LIMIT in handback_log.FALLBACK_REASONS


async def test_a_blocked_reschedule_does_not_even_resolve_the_calendar(wired, monkeypatch):
    bundle = await _seed(wired.db, limit=1, deposit_count=1)

    async def forbidden(*args, **kwargs):
        pytest.fail("blocked reschedule must not resolve an agenda")

    monkeypatch.setattr(workers_ns, "_appointment_calendar", forbidden)
    await _manage(bundle, ManageRequest("reschedule", day=NEW_DAY, time=dt.time(14, 0)))
    assert (await _row(wired.db, bundle.conversation)).flow_state == FlowState.MENU
    assert _events(wired.log)[0]["fallback"] == "reschedule_limit"


async def test_the_current_clinic_switch_wins_over_the_turn_start_snapshot(wired):
    bundle = await _seed(wired.db)
    async with wired.db() as session:
        fresh = await session.get(Tenant, bundle.tenant.id)
        fresh.initial_flows = {}
        await session.commit()
    await _manage(bundle, ManageRequest("reschedule", day=NEW_DAY, time=dt.time(14, 0)))
    row = await _row(wired.db, bundle.conversation)
    assert row.flow_step == fr.STEP_MANAGE_DAY
    assert _events(wired.log)[0]["supplied"] == ["action"]


async def test_the_current_deposit_limit_wins_over_the_turn_start_snapshot(wired):
    bundle = await _seed(wired.db, limit=2, deposit_count=1)
    async with wired.db() as session:
        fresh = await session.get(Tenant, bundle.tenant.id)
        fresh.pix_reschedule_limit = 1
        await session.commit()
    await _manage(bundle, ManageRequest("reschedule", day=NEW_DAY, time=dt.time(14, 0)))
    assert (await _row(wired.db, bundle.conversation)).flow_state == FlowState.MENU
    assert wired.agenda.day_reads == wired.agenda.slot_reads == []


async def test_a_conversation_from_another_clinic_is_refused(wired):
    bundle = await _seed(wired.db)
    other = await _seed(wired.db)
    await tasks._handle_manage_appointment(
        _reply(bundle.conversation), "cancel", other.tenant, [], WA_ID, waba_token="t"
    )
    row = await _row(wired.db, bundle.conversation)
    assert row.flow_state == FlowState.LLM
    assert wired.sent == []
    assert _events(wired.log)[0]["landing_step"] is None


async def test_indistinguishable_appointments_activate_human_help_without_an_agenda(
    wired, monkeypatch
):
    bundle = await _seed(wired.db, own=2)
    async with wired.db() as session:
        twin = await session.get(Appointment, bundle.appointments[1].id)
        twin.start_at = bundle.appointments[0].start_at
        twin.end_at = bundle.appointments[0].end_at
        await session.commit()

    async def no_external_notification(**kwargs):
        return None

    monkeypatch.setattr(workers_ns, "notify_human_handoff", no_external_notification)
    await _manage(bundle, ManageRequest("cancel", appointment=_ref(FIRST_DAY, 9)))
    row = await _row(wired.db, bundle.conversation)
    assert row.handover_state == HandoverState.HUMAN_ACTIVE
    assert row.flow_managing_appointment_id is None
    assert wired.agenda.cancelled == wired.agenda.updated == []
    assert wired.agenda.day_reads == wired.agenda.slot_reads == []
    assert "equipe" in wired.sent[-1].body
    (event,) = _events(wired.log)
    assert event["landing_step"] == "human_handover"
    assert event["fallback"] == "ambiguous_appointment"
