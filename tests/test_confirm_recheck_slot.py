"""A confirmed slot is re-read on the agenda before it is written (double booking at confirm).

The slot list a patient taps is a snapshot. Between the list and "✅ Confirmar" the clinic,
another channel or a person in Google Calendar can fill that exact time, and every confirm
path used to go straight to `create_event` / `update_event`: WhatsApp has no hold at all,
and the Portal's ten-minute hold (`services/booking_hold.py`) only blocks other HOLDS, never
the clinic's own Google writes. The patient was booked on top of somebody else.

Now every patient-confirmed write first asks `availability.slot_taken_at_confirm` about
exactly [start, end) on the agenda the write goes to. What each section proves:

  1. the conflict test itself (`CalendarService.has_conflict`): half-open, and the
     patient's own event never takes its own new time
  2. guided "Confirmar": taken -> nothing created or held, back to the DAY picker
     ("Para quando você gostaria? Escolha um dia:", prefixed with the "just taken"
     line, TASK-072) with service/doctor/convênio kept; free -> booked exactly as
     before; read failure -> booked (fail-open)
  3. reschedule "Confirmar": same three outcomes, the event being moved excluded
  4. the Portal code gate, end to end: taken on the DOCTOR's agenda while the code was
     typed -> no event, hold released, account confirmed, back to the day picker with
     the same doctor/service/convênio (TASK-072), and day -> time -> "Confirmar" books on
     that same agenda without a second code; busy only on the CLINIC calendar -> booked
     on the doctor's (the re-check reads the calendar the event is written to)
  5. the v1 AI booking tools: taken -> the booking-draft hand-back (same service, doctor,
     day) with the fixed line, never a write

Google is replaced by an in-memory agenda in every test; the conflict arithmetic and, in
section 4, the whole worker pipeline are the production code.
"""
import os
from contextlib import contextmanager

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from datetime import UTC, date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.ai.formatter import SlotsBubble  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    BookingHold,
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    Patient,
    Professional,
    Tenant,
)
from secretaria.plugins import (
    multi_professional as mp,  # noqa: E402
    multi_unit as mu,  # noqa: E402
)
from secretaria.services import (
    availability,  # noqa: E402
    booking_hold as holds,  # noqa: E402
)
from secretaria.services.calendar import (  # noqa: E402
    CalendarService,
    CalendarUnavailableError,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    DAY_PICKER_BODY,
    SLOT_TAKEN_AT_CONFIRM_MESSAGE,
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_DAY,
    STEP_MANAGE_CONFIRM,
    STEP_MANAGE_SLOT,
    route,
)
from secretaria.services.pending_identity import (  # noqa: E402
    CODE_ACCEPTED_MESSAGE,
    RequestCodeOutcome,
    RequestCodeResult,
    VerifyOutcome,
    VerifyResult,
)
from secretaria.services.tenant_config import set_google_refresh_token  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
SLOT_DAY = "2099-01-05"  # a Monday
SLOT = f"{SLOT_DAY}T08:00"


def _at(hhmm: str, day: str = SLOT_DAY) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=TZ)


def _event(event_id: str, start: str, end: str, day: str = SLOT_DAY) -> dict:
    """An events.list item, as `check_availability` hands it back (RFC3339 strings)."""
    return {
        "id": event_id,
        "summary": "Outro paciente",
        "start": _at(start, day).isoformat(),
        "end": _at(end, day).isoformat(),
    }


class _Recorder:
    """Stand-in for the availability logger: structlog caches its loggers, so the
    calls are recorded directly instead of through capture_logs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def info(self, event: str, **kw) -> None:
        self.calls.append(("info", event, kw))

    def warning(self, event: str, **kw) -> None:
        self.calls.append(("warning", event, kw))

    def events(self, name: str) -> list[dict]:
        return [kw for _, event, kw in self.calls if event == name]


@pytest.fixture
def recheck_log(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(availability, "logger", recorder)
    return recorder


class _Agenda(CalendarService):
    """A real CalendarService minus Google: events live in a list, like on the agenda.

    Only the events.list read (`check_availability`) and the writes are replaced, so
    `has_conflict` - the method every confirm path now asks - runs as in production.
    `reads` records every window asked, which is how "exactly [start, end), on THIS
    agenda" is asserted.
    """

    def __init__(self, events=(), *, read_fails: bool = False, slots=None) -> None:
        self._tz = TZ
        self._calendar_id = "agenda"
        self._business_hours = {}
        self._default_slot_minutes = 30
        self.events = [dict(e) for e in events]
        self.read_fails = read_fails
        self.slots = slots if slots is not None else [
            {"start": f"{SLOT_DAY}T09:00", "end": f"{SLOT_DAY}T09:40", "label": "09:00"}
        ]
        self.reads: list[tuple[datetime, datetime]] = []
        self.listed_days: list[date] = []
        self.created: list[tuple[datetime, datetime, str]] = []
        self.updated: list[tuple[str, datetime, datetime]] = []

    async def check_availability(self, start, end, max_results: int = 50) -> list[dict]:
        self.reads.append((start, end))
        if self.read_fails:
            raise CalendarUnavailableError("Google Calendar returned HTTP 503")
        # Google returns exactly the events overlapping the window.
        return [
            e
            for e in self.events
            if datetime.fromisoformat(e["start"]) < end and datetime.fromisoformat(e["end"]) > start
        ]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6, **kw) -> list[dict]:
        self.listed_days.append(day.date())
        return list(self.slots)

    async def list_available_days(self, start_day, days, slot_minutes=None, **kw):
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(3)]

    async def create_event(self, start, end, summary, description="", reminders=None) -> dict:
        self.created.append((start, end, summary))
        return {"id": "evt-new", "htmlLink": "https://calendar.google.com/evt-new"}

    async def update_event(self, event_id, start, end) -> dict:
        self.updated.append((event_id, start, end))
        return {"id": event_id}


# --------------------------------------------------------------------------
# 1. The conflict test itself
# --------------------------------------------------------------------------


async def test_has_conflict_is_half_open_and_ignores_the_event_being_moved():
    agenda = _Agenda([_event("other", "08:30", "09:00"), _event("mine", "10:00", "10:40")])

    # Overlapping the start of someone else's event: taken.
    assert await agenda.has_conflict(_at("08:00"), _at("08:40")) is True
    # Ending exactly where it begins: adjacent, not conflicting (the slot walk's rule).
    assert await agenda.has_conflict(_at("08:00"), _at("08:30")) is False
    # The patient's own event never takes its own new time...
    assert await agenda.has_conflict(_at("10:20"), _at("11:00"), ignore_event_id="mine") is False
    # ...but is still somebody's event when it is not the one being moved.
    assert await agenda.has_conflict(_at("10:20"), _at("11:00")) is True
    # The read is exactly the window asked, on this agenda.
    assert agenda.reads[0] == (_at("08:00"), _at("08:40"))


# --------------------------------------------------------------------------
# 2. Guided "Confirmar"
# --------------------------------------------------------------------------


def _tenant():
    return SimpleNamespace(
        initial_flows={},
        appointment_types=[
            {"name": "Primeira Consulta", "duration_min": 40, "is_active": True, "sort_order": 0}
        ],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
    )


_DOCTOR_ID = UUID("22222222-2222-4222-8222-222222222222")


def _roster_row(professional_id: UUID, name: str) -> SimpleNamespace:
    """A router-shaped roster entry; NULL catalog and hours = the clinic's."""
    return SimpleNamespace(
        id=professional_id, name=name, specialty=None, about=None,
        appointment_types=None, business_hours=None,
    )


def _confirming(**kw) -> SimpleNamespace:
    base = dict(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Primeira Consulta",
        flow_selected_day=SLOT_DAY,
        flow_selected_slot=SLOT,
        flow_selected_professional_id=None,
        flow_selected_insurance="Particular",
        flow_managing_appointment_id=None,
        flow_attendee_name="",
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


async def test_confirm_on_a_slot_taken_since_the_list_creates_nothing_and_relists_the_day(
    recheck_log,
):
    agenda = _Agenda([_event("someone-else", "08:00", "08:40")])

    res = await route(_confirming(), _tenant(), agenda, "Confirmar", patient_name="João")

    assert agenda.created == []
    assert res.appointment is None
    # Exactly the confirmed window (the 40-minute service), on the agenda it would go to.
    assert agenda.reads == [(_at("08:00"), _at("08:40"))]
    # TASK-072: the "just taken" line, then the DAY picker - not a dead end, not the LLM.
    assert res.action == "reply"
    [picker] = res.bubbles
    assert isinstance(picker, SlotsBubble)
    assert picker.body == f"{SLOT_TAKEN_AT_CONFIRM_MESSAGE} {DAY_PICKER_BODY}"
    # The booking in progress is kept: the patient only picks a new day and time.
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_AWAITING_DAY
    assert res.flow_selected_type == "Primeira Consulta"
    assert res.flow_selected_day is None
    assert res.flow_selected_slot is None
    assert res.flow_selected_insurance == "Particular"
    assert res.flow_attendee_name == ""
    assert [e["source"] for e in recheck_log.events("booking_slot_taken_at_confirm")] == [
        "flow_confirm"
    ]


async def test_confirm_keeps_the_chosen_doctor_when_the_slot_was_taken():
    agenda = _Agenda([_event("someone-else", "08:20", "08:50")])
    doctor = _roster_row(_DOCTOR_ID, "Dra. Ana")
    other = _roster_row(uuid4(), "Dr. Bruno")

    res = await route(
        _confirming(flow_selected_professional_id=_DOCTOR_ID),
        _tenant(),
        agenda,
        "Confirmar",
        patient_name="João",
        professionals=[doctor, other],
    )

    assert agenda.created == []
    assert res.flow_step == STEP_AWAITING_DAY
    assert res.flow_selected_professional_id == _DOCTOR_ID


async def test_confirm_on_a_free_slot_books_exactly_as_before():
    # Somebody else's event right AFTER the slot does not block it.
    agenda = _Agenda([_event("next-patient", "08:40", "09:20")])

    res = await route(_confirming(), _tenant(), agenda, "Confirmar", patient_name="João")

    assert agenda.created == [(_at("08:00"), _at("08:40"), "Primeira Consulta - João")]
    assert res.appointment["google_event_id"] == "evt-new"
    assert res.flow_state == FlowState.IDLE
    assert res.bubbles[0].body.startswith("Pronto! Seu agendamento está confirmado.")


async def test_confirm_books_as_before_when_the_recheck_read_fails(recheck_log):
    """Fail-open: a flaky events.list must never turn into a clinic that cannot book."""
    agenda = _Agenda(read_fails=True)

    res = await route(_confirming(), _tenant(), agenda, "Confirmar", patient_name="João")

    assert len(agenda.created) == 1
    assert res.appointment is not None
    failed = recheck_log.events("booking_slot_recheck_failed")
    assert len(failed) == 1
    assert failed[0]["source"] == "flow_confirm"
    assert failed[0]["error_type"] == "CalendarUnavailableError"
    assert recheck_log.events("booking_slot_taken_at_confirm") == []


class _ArmedGate:
    """A Portal gate that records whether it was ever asked to hold."""

    armed = True

    def __init__(self) -> None:
        self.decided: list[dict] = []

    async def busy_windows(self, professional_id):
        return []

    async def decide(self, **kw):
        self.decided.append(kw)
        raise AssertionError("a taken slot must never be held")


async def test_a_gated_portal_confirm_on_a_taken_slot_places_no_hold():
    """The check runs BEFORE the gate: no reservation, no code mailed, for a time
    that already belongs to somebody else's event."""
    agenda = _Agenda([_event("someone-else", "08:00", "08:40")])
    gate = _ArmedGate()

    res = await route(
        _confirming(), _tenant(), agenda, "Confirmar", patient_name="João", gate=gate
    )

    assert gate.decided == []
    assert agenda.created == []
    assert res.booking_hold is None
    assert res.bubbles[0].body.startswith(SLOT_TAKEN_AT_CONFIRM_MESSAGE)
    assert res.flow_step == STEP_AWAITING_DAY


# --------------------------------------------------------------------------
# 3. Reschedule "Confirmar"
# --------------------------------------------------------------------------

_APPT_ID = UUID("11111111-1111-4111-8111-111111111111")


def _appointment(start: datetime) -> dict:
    return {
        "id": str(_APPT_ID),
        "google_event_id": "evt-mine",
        "appointment_type": "Primeira Consulta",
        "start_at": start,
        "end_at": start + timedelta(minutes=40),
    }


def _confirming_move(slot: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CONFIRM,
        flow_selected_type=None,
        flow_selected_day=slot[:10],
        flow_selected_slot=slot,
        flow_managing_appointment_id=_APPT_ID,
        patient_id=None,
    )


async def test_reschedule_onto_a_taken_slot_moves_nothing_and_relists_the_day(recheck_log):
    original = _at("14:00", "2099-01-02")
    agenda = _Agenda(
        [
            _event("evt-mine", "14:00", "14:40", "2099-01-02"),
            _event("someone-else", "08:10", "08:50"),
        ]
    )

    res = await route(
        _confirming_move(SLOT),
        _tenant(),
        agenda,
        "Confirmar",
        upcoming_appointments=[_appointment(original)],
    )

    assert agenda.updated == []
    assert res.appointment_reschedule is None
    assert res.bubbles[0].body == SLOT_TAKEN_AT_CONFIRM_MESSAGE
    assert isinstance(res.bubbles[1], SlotsBubble)
    # Still moving THE SAME appointment, back on its slot list.
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_SLOT
    assert res.flow_managing_appointment_id == _APPT_ID
    assert [e["source"] for e in recheck_log.events("booking_slot_taken_at_confirm")] == [
        "flow_reschedule"
    ]


async def test_reschedule_overlapping_its_own_current_time_still_moves():
    """08:00 -> 08:20 on a 40-minute appointment: the only "busy" event in the new
    window is the appointment being moved, which never takes its own new time."""
    original = _at("08:00")
    agenda = _Agenda([_event("evt-mine", "08:00", "08:40")])

    res = await route(
        _confirming_move(f"{SLOT_DAY}T08:20"),
        _tenant(),
        agenda,
        "Confirmar",
        upcoming_appointments=[_appointment(original)],
    )

    assert agenda.updated == [("evt-mine", _at("08:20"), _at("09:00"))]
    assert res.appointment_reschedule["google_event_id"] == "evt-mine"
    assert res.flow_state == FlowState.IDLE


async def test_reschedule_moves_as_before_when_the_recheck_read_fails(recheck_log):
    agenda = _Agenda(read_fails=True)

    res = await route(
        _confirming_move(SLOT),
        _tenant(),
        agenda,
        "Confirmar",
        upcoming_appointments=[_appointment(_at("14:00", "2099-01-02"))],
    )

    assert len(agenda.updated) == 1
    assert res.bubbles[0].body.startswith("Pronto! Sua consulta foi remarcada.")
    assert [e["source"] for e in recheck_log.events("booking_slot_recheck_failed")] == [
        "flow_reschedule"
    ]


# --------------------------------------------------------------------------
# 4. The Portal code gate, end to end
# --------------------------------------------------------------------------

EXTERNAL_ID = "00000000-0000-4000-8000-0000000000bb"


class _FakeGoogle:
    """Events live INSIDE a calendarId, like on Google (cf. the owner-calendar suite).

    `check_availability` is patched too, so the production `has_conflict` reads the
    agenda of whichever CalendarService the worker resolved - which is the point.
    """

    def __init__(self) -> None:
        self.calendars: dict[str | None, dict[str, tuple[datetime, datetime]]] = {}
        self.ops: list[tuple] = []
        self.unreadable: set[str] = set()
        self._n = 0

    def busy(self, calendar_id: str, start: datetime, end: datetime) -> None:
        self.calendars.setdefault(calendar_id, {})[f"foreign-{len(self.ops)}"] = (start, end)

    def install(self, monkeypatch) -> None:
        google = self

        async def check_availability(self, start, end, max_results=50):
            google.ops.append(("read", self._calendar_id))
            if self._calendar_id in google.unreadable:
                raise CalendarUnavailableError("Google Calendar returned HTTP 503")
            return [
                {"id": eid, "summary": "x", "start": s.isoformat(), "end": e.isoformat()}
                for eid, (s, e) in google.calendars.get(self._calendar_id, {}).items()
                if s < end and e > start
            ]

        async def create_event(self, start, end, summary, description="", reminders=None):
            google._n += 1
            eid = f"evt-{google._n}"
            google.calendars.setdefault(self._calendar_id, {})[eid] = (start, end)
            google.ops.append(("create", self._calendar_id, eid))
            return {"id": eid, "htmlLink": f"https://calendar.google.com/{eid}"}

        async def list_available_days(self, start_day, days, slot_minutes=None, **kw):
            base = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            return [base + timedelta(days=3), base + timedelta(days=4)]

        async def list_free_slots(self, day, slot_minutes=None, max_slots=6, **kw):
            start = day.replace(hour=14, minute=40, tzinfo=None)
            end = start + timedelta(minutes=slot_minutes or 30)
            return [
                {
                    "start": start.isoformat(timespec="minutes"),
                    "end": end.isoformat(timespec="minutes"),
                    "label": "14:40",
                }
            ]

        for name, fn in {
            "check_availability": check_availability,
            "create_event": create_event,
            "list_available_days": list_available_days,
            "list_free_slots": list_free_slots,
        }.items():
            monkeypatch.setattr(CalendarService, name, fn)

    def creates(self) -> list[tuple]:
        return [op for op in self.ops if op[0] == "create"]


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
def google(monkeypatch) -> _FakeGoogle:
    g = _FakeGoogle()
    g.install(monkeypatch)
    return g


@pytest.fixture
def portal(monkeypatch, db):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(holds, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))

    async def _none(*a, **k):
        return None

    async def _token(session, tenant_id):
        return "decrypted-waba-token"

    async def _ent(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id), status="active", active=True, secretaria_enabled=True,
            plan="bronze", secretaria_tier="basico", addons={}, limits={},
        )

    async def _verify(tenant_id, external_id, code):
        return VerifyResult(VerifyOutcome.VERIFIED)

    async def _request(tenant_id, external_id):
        # After the code the visitor has an account: nothing left to prove, so the
        # next "Confirmar" commits (the gate stands down) instead of mailing a code.
        return RequestCodeResult(RequestCodeOutcome.NOT_PENDING)

    async def _no_llm(*a, **k):
        raise AssertionError("no turn of this journey may reach the LLM")

    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _none)
    monkeypatch.setattr(workers_ns, "get_waba_token", _token)
    monkeypatch.setattr(workers_ns, "get_entitlements", _ent)
    monkeypatch.setattr(workers_ns, "verify_code", _verify)
    monkeypatch.setattr(workers_ns, "enqueue_post_booking_hooks", _none)
    monkeypatch.setattr(workers_ns, "run_agent", _no_llm)
    monkeypatch.setattr(holds, "request_code", _request)
    monkeypatch.setattr(workers_ns, "request_code", _request)


async def _seed(db):
    """A two-doctor clinic: Dr. A and the clinic each have their OWN calendar."""
    async with db() as s:
        tenant = Tenant(
            id=uuid4(), clinic_name="Clínica QA", phone_number_id="1234567890",
            is_active=True, timezone="America/Sao_Paulo", initial_flows={},
            google_calendar_id="clinic-cal", google_calendar_mode="shared_account",
        )
        s.add(tenant)
        await s.flush()
        dr_a = Professional(tenant_id=tenant.id, name="Dr. A", google_calendar_id="cal-dr-a")
        dr_b = Professional(tenant_id=tenant.id, name="Dr. B", google_calendar_id="cal-dr-b")
        s.add_all([dr_a, dr_b])
        patient = Patient(
            id=uuid4(), tenant_id=tenant.id, channel=CHANNEL_BRAIN_MESSAGE,
            external_id=EXTERNAL_ID, name="Maria", lgpd_accepted_at=datetime.now(UTC),
        )
        s.add(patient)
        await s.flush()
        conv = Conversation(
            id=uuid4(), tenant_id=tenant.id, patient_id=patient.id,
            flow_state=FlowState.AWAITING_EMAIL_CODE,
        )
        s.add(conv)
        # A CONNECTED clinic (the env-var token no longer stands in for a tenant's).
        await set_google_refresh_token(s, tenant.id, "clinic-token")
        await s.commit()
        for row in (tenant, dr_a, patient, conv):
            await s.refresh(row)
        return tenant, dr_a, patient, conv


def _held_window() -> tuple[datetime, datetime]:
    start = (datetime.now(TZ) + timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    return start, start + timedelta(minutes=30)


async def _hold(tenant, dr_a, patient, conv) -> tuple[datetime, datetime]:
    start, end = _held_window()
    held = await holds.place_hold(
        tenant_id=tenant.id, conversation_id=conv.id, patient_id=patient.id,
        professional_id=dr_a.id, appointment_type="Cirurgia de Catarata",
        insurance="Particular", start_at=start.astimezone(UTC), end_at=end.astimezone(UTC),
    )
    assert held is not None
    return start, end


async def _portal(tenant, text_, reply_id=None):
    await tasks.process_brain_message_inbound(
        {}, str(tenant.id), EXTERNAL_ID, text=text_, interactive_reply_id=reply_id
    )


async def _outbound(db, conv) -> list[str]:
    async with db() as s:
        return list(
            (
                await s.scalars(
                    select(Message.body)
                    .where(
                        Message.conversation_id == conv.id,
                        Message.direction == MessageDirection.OUTBOUND,
                    )
                    .order_by(Message.created_at, text("messages.rowid"))
                )
            ).all()
        )


async def _rows(db, model) -> list:
    async with db() as s:
        return list((await s.scalars(select(model))).all())


async def test_code_turn_on_a_slot_taken_on_the_doctors_agenda_books_nothing_and_reoffers(
    db, google, portal, recheck_log
):
    tenant, dr_a, patient, conv = await _seed(db)
    start, end = await _hold(tenant, dr_a, patient, conv)
    # While the code was being typed, the clinic filled the slot on Dr. A's own agenda.
    google.busy("cal-dr-a", start - timedelta(minutes=15), start + timedelta(minutes=15))

    await _portal(tenant, "123456")

    assert google.creates() == []
    assert await _rows(db, Appointment) == []
    assert await _rows(db, BookingHold) == [], "the hold must go back on sale"
    # The re-check read the agenda the event would have been written to.
    assert ("read", "cal-dr-a") in google.ops
    # The account is confirmed first, on its own; then the "just taken" line on the
    # DAY picker (TASK-072, owner 2026-10-29).
    sent = await _outbound(db, conv)
    assert sent[-2] == CODE_ACCEPTED_MESSAGE
    assert sent[-1].startswith(f"{SLOT_TAKEN_AT_CONFIRM_MESSAGE} {DAY_PICKER_BODY}")
    assert not any("confirmado" in body for body in sent)
    # Same doctor, same service, same convênio, on the DAY step - never IDLE, never menu.
    async with db() as s:
        row = await s.get(Conversation, conv.id)
    assert row.flow_state == FlowState.SERVICE_CATALOG
    assert row.flow_step == STEP_AWAITING_DAY
    assert row.flow_selected_professional_id == dr_a.id
    assert row.flow_selected_type == "Cirurgia de Catarata"
    assert row.flow_selected_insurance == "Particular"
    assert row.flow_selected_day is None
    assert [e["source"] for e in recheck_log.events("booking_slot_taken_at_confirm")] == [
        "booking_hold_promotion"
    ]

    # The day picker is a real step: day -> time -> "Confirmar" books on Dr. A's agenda,
    # with no second code (the account was verified a turn ago).
    new_day = (datetime.now(TZ) + timedelta(days=3)).date().isoformat()
    await _portal(tenant, "dia", f"day|{new_day}|0")
    slot = f"{new_day}T14:40"
    await _portal(tenant, "🗓️ 14:40", f"slot|{slot}")
    await _portal(tenant, "✅ Confirmar", "confirm_yes")

    created = google.creates()
    assert [(op[0], op[1]) for op in created] == [("create", "cal-dr-a")]
    [appointment] = await _rows(db, Appointment)
    assert appointment.professional_id == dr_a.id
    assert appointment.start_at.astimezone(TZ).strftime("%Y-%m-%dT%H:%M") == slot


async def test_code_turn_ignores_a_busy_clinic_calendar_and_books_on_the_doctors(
    db, google, portal
):
    """The re-check must read the calendar the event is WRITTEN to. Busy on the clinic
    calendar (the agenda gated bookings used to land on before 75b4a63) is not busy on
    Dr. A's: the booking goes through, on Dr. A's."""
    tenant, dr_a, patient, conv = await _seed(db)
    start, end = await _hold(tenant, dr_a, patient, conv)
    google.busy("clinic-cal", start, end)

    await _portal(tenant, "123456")

    [appointment] = await _rows(db, Appointment)
    assert appointment.professional_id == dr_a.id
    assert [(op[0], op[1]) for op in google.creates()] == [("create", "cal-dr-a")]
    assert ("read", "clinic-cal") not in google.ops
    assert (await _outbound(db, conv))[-1].startswith(CODE_ACCEPTED_MESSAGE)


async def test_code_turn_books_as_before_when_the_recheck_read_fails(
    db, google, portal, recheck_log
):
    tenant, dr_a, patient, conv = await _seed(db)
    await _hold(tenant, dr_a, patient, conv)
    google.unreadable.add("cal-dr-a")

    await _portal(tenant, "123456")

    assert len(await _rows(db, Appointment)) == 1
    assert [(op[0], op[1]) for op in google.creates()] == [("create", "cal-dr-a")]
    assert "Pronto! Seu agendamento está confirmado." in (await _outbound(db, conv))[-1]
    assert [e["source"] for e in recheck_log.events("booking_slot_recheck_failed")] == [
        "booking_hold_promotion"
    ]


# --------------------------------------------------------------------------
# 5. The v1 AI booking tools
# --------------------------------------------------------------------------


@contextmanager
def _agent_turn(calendar, *, tenant_id=None):
    """The ContextVars run_agent sets, minus a tenant config (no catalog to prove)."""
    cal_token = ai_tools._calendar_ctx.set(calendar)
    tenant_token = ai_tools._tenant_id_ctx.set(tenant_id)
    try:
        yield
    finally:
        ai_tools._tenant_id_ctx.reset(tenant_token)
        ai_tools._calendar_ctx.reset(cal_token)


_WINDOW = {"start": f"{SLOT_DAY}T08:00:00", "end": f"{SLOT_DAY}T08:30:00"}


async def test_ai_create_event_on_a_taken_slot_hands_back_to_that_days_times():
    agenda = _Agenda([_event("someone-else", "08:00", "08:30")])

    with _agent_turn(agenda), pytest.raises(ai_tools.BookingDraftRequested) as raised:
        await ai_tools.create_event.ainvoke({**_WINDOW, "summary": "Consulta - Maria"})

    assert agenda.created == []
    assert raised.value.day == date(2099, 1, 5)
    assert raised.value.time is None
    assert raised.value.intro == SLOT_TAKEN_AT_CONFIRM_MESSAGE
    assert agenda.reads == [(_at("08:00"), _at("08:30"))]


async def test_ai_create_event_on_a_free_slot_books_as_before():
    agenda = _Agenda([_event("next-patient", "08:30", "09:00")])

    with _agent_turn(agenda):
        result = await ai_tools.create_event.ainvoke({**_WINDOW, "summary": "Consulta - Maria"})

    assert result["id"] == "evt-new"
    assert agenda.created == [(_at("08:00"), _at("08:30"), "Consulta - Maria")]


async def test_ai_create_event_books_as_before_when_the_recheck_read_fails():
    agenda = _Agenda(read_fails=True)

    with _agent_turn(agenda):
        result = await ai_tools.create_event.ainvoke({**_WINDOW, "summary": "Consulta - Maria"})

    assert result["id"] == "evt-new"
    assert len(agenda.created) == 1


@pytest_asyncio.fixture
async def non_payment_tenant(db, monkeypatch):
    from secretaria.core import database

    # The merged Pix guard also reads the tenant. Keep this test on its own database,
    # with a real clinic that does not require a deposit, before testing the slot check.
    monkeypatch.setattr(database, "async_session_factory", db)
    async with db() as session:
        tenant = Tenant(clinic_name="Clínica de teste", pix_deposit_enabled=False)
        session.add(tenant)
        await session.commit()
        return tenant.id


async def test_ai_professional_tool_on_a_taken_slot_hands_back_with_that_doctor(
    monkeypatch, non_payment_tenant,
):
    agenda = _Agenda([_event("someone-else", "08:15", "08:45")])
    doctor = SimpleNamespace(id=_DOCTOR_ID, name="Dra. Ana", context_doctor_message=None)

    async def _roster(tenant_id):
        return [doctor]

    async def _services(tenant_id, professional):
        return []

    async def _calendar(tenant_id, professional):
        assert professional is doctor
        return agenda

    monkeypatch.setattr(mp, "_active_professionals", _roster)
    monkeypatch.setattr(mp, "_professional_services", _services)
    monkeypatch.setattr(mp, "_professional_calendar", _calendar)

    with (
        _agent_turn(None, tenant_id=non_payment_tenant),
        pytest.raises(ai_tools.BookingDraftRequested) as raised,
    ):
        await mp.create_event_for_professional.ainvoke(
            {**_WINDOW, "professional_name": "Dra. Ana", "summary": "Consulta - Maria"}
        )

    assert agenda.created == []
    assert raised.value.professional_id == _DOCTOR_ID
    assert raised.value.day == date(2099, 1, 5)
    assert raised.value.intro == SLOT_TAKEN_AT_CONFIRM_MESSAGE


async def test_ai_unit_tool_on_a_taken_slot_hands_back(monkeypatch, non_payment_tenant):
    agenda = _Agenda([_event("someone-else", "08:00", "08:30")])

    async def _unit(tenant_id, unit_name):
        return SimpleNamespace(id=uuid4(), name=unit_name), None

    monkeypatch.setattr(mu, "_resolve_unit_or_error", _unit)

    with (
        _agent_turn(agenda, tenant_id=non_payment_tenant),
        pytest.raises(ai_tools.BookingDraftRequested),
    ):
        await mu.create_event_at_unit.ainvoke(
            {**_WINDOW, "unit_name": "Centro", "summary": "Consulta - Maria"}
        )

    assert agenda.created == []
