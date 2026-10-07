"""The legacy `cancel_event` cancels only THIS patient's own event (TASK-030 P4, Task 1).

Before this, the tool deleted ANY Google event id the model passed - a third party's
consultation included - with no ownership check. Now the id must belong to an `Appointment`
of the conversation's patient in the turn's tenant (`ai/tools.py::_own_google_event_ids`,
the same proof `check_availability` uses for `do_paciente`), or the tool refuses BEFORE any
Google call or DB write. Applies to every clinic, switch or no switch.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from contextlib import contextmanager  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    Tenant,
)


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


@pytest.fixture(autouse=True)
def _session_factory(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(core_database, "async_session_factory", db)


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


@pytest.fixture
def log(monkeypatch):
    recorder = _Log()
    monkeypatch.setattr(ai_tools, "logger", recorder)
    return recorder


class _Calendar:
    """Records every cancel; a refused call must leave `cancelled` empty."""

    def __init__(self):
        self.tzinfo = UTC
        self.cancelled: list[str] = []

    async def cancel_event(self, event_id: str) -> None:
        self.cancelled.append(event_id)


async def _patient_with_event(db, tenant_id, event_id: str, *, wa_id: str):
    """A patient of `tenant_id`, their conversation, and one upcoming appointment."""
    start = datetime.now(UTC) + timedelta(days=3)
    async with db() as session:
        patient = Patient(tenant_id=tenant_id, wa_id=wa_id, name="Paciente")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant_id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        appointment = Appointment(
            tenant_id=tenant_id,
            patient_id=patient.id,
            conversation_id=conversation.id,
            google_event_id=event_id,
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appointment)
        await session.commit()
        return SimpleNamespace(
            patient_id=patient.id, conversation_id=conversation.id, appointment_id=appointment.id
        )


async def _tenant(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.commit()
        return tenant


@pytest_asyncio.fixture
async def world(db):
    """Tenant A: our patient (evt-own) and another patient (evt-other-patient).
    Tenant B: a third patient (evt-other-tenant)."""
    tenant_a, tenant_b = await _tenant(db), await _tenant(db)
    own = await _patient_with_event(db, tenant_a.id, "evt-own", wa_id="5511900000001")
    other = await _patient_with_event(db, tenant_a.id, "evt-other-patient", wa_id="5511900000002")
    foreign = await _patient_with_event(db, tenant_b.id, "evt-other-tenant", wa_id="5511900000003")
    return SimpleNamespace(tenant_a=tenant_a, own=own, other=other, foreign=foreign)


@contextmanager
def _turn(tenant_id, conversation_id, calendar):
    pairs = [
        (ai_tools._tenant_id_ctx, tenant_id),
        (ai_tools._conversation_id_ctx, conversation_id),
        (ai_tools._calendar_ctx, calendar),
    ]
    tokens = [(var, var.set(value)) for var, value in pairs]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


async def _status(db, appointment_id):
    async with db() as session:
        return (await session.get(Appointment, appointment_id)).status


async def test_the_patients_own_event_is_cancelled_as_before(db, world):
    calendar = _Calendar()
    with _turn(world.tenant_a.id, world.own.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-own"})
    assert result == {"status": "cancelled"}
    assert calendar.cancelled == ["evt-own"]
    assert await _status(db, world.own.appointment_id) == AppointmentStatus.CANCELLED


@pytest.mark.parametrize(
    "event_id",
    ["evt-other-patient", "evt-other-tenant", "evt-that-does-not-exist", ""],
    ids=["another_patient_same_tenant", "another_tenant", "unknown_id", "empty_id"],
)
async def test_an_event_that_is_not_the_patients_is_refused_before_google(db, world, log, event_id):
    calendar = _Calendar()
    with _turn(world.tenant_a.id, world.own.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": event_id})
    assert set(result) == {"error"}
    assert "manage_existing_appointment" in result["error"]
    assert calendar.cancelled == []  # Google was never called
    # Nobody's row moved.
    for appointment_id in (
        world.own.appointment_id,
        world.other.appointment_id,
        world.foreign.appointment_id,
    ):
        assert await _status(db, appointment_id) == AppointmentStatus.SCHEDULED
    (fields,) = [f for e, f in log.events if e == "agent_tool_blocked"]
    assert (fields["tool"], fields["reason"]) == ("cancel_event", "not_patients_event")
    assert fields["tenant_id"] == str(world.tenant_a.id)
    if event_id:
        # A reason code only: the id the model sent is neither logged nor echoed.
        assert event_id not in repr(log.events)
        assert event_id not in result["error"]


async def test_the_other_patient_can_still_cancel_their_own(db, world):
    """The check is per conversation, not a blanket refusal."""
    calendar = _Calendar()
    with _turn(world.tenant_a.id, world.other.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-other-patient"})
    assert result == {"status": "cancelled"}
    assert calendar.cancelled == ["evt-other-patient"]


async def test_the_events_tenant_with_another_patient_is_still_refused(db, world):
    """Ownership is tenant AND patient: pointing the turn at the event's own tenant does
    not make another tenant's patient's consultation ours."""
    async with db() as session:
        tenant_b = (await session.get(Appointment, world.foreign.appointment_id)).tenant_id
    calendar = _Calendar()
    with _turn(tenant_b, world.own.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-other-tenant"})
    assert set(result) == {"error"}
    assert calendar.cancelled == []
    assert await _status(db, world.foreign.appointment_id) == AppointmentStatus.SCHEDULED


@pytest.mark.parametrize(
    "tenant_known, conversation_known",
    [(False, True), (True, False), (False, False)],
    ids=["no_tenant", "no_conversation", "no_context"],
)
async def test_without_a_patient_context_nothing_is_cancelled(
    db, world, tenant_known, conversation_known
):
    """Fail closed, like `do_paciente`: no proof of ownership, no cancel."""
    calendar = _Calendar()
    with _turn(
        world.tenant_a.id if tenant_known else None,
        world.own.conversation_id if conversation_known else None,
        calendar,
    ):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-own"})
    assert set(result) == {"error"}
    assert calendar.cancelled == []
    assert await _status(db, world.own.appointment_id) == AppointmentStatus.SCHEDULED


async def test_an_event_row_without_a_patient_is_nobodys_to_cancel(db, world):
    """A row the hub created with no patient (patient_id NULL) is never this patient's."""
    start = datetime.now(UTC) + timedelta(days=5)
    async with db() as session:
        session.add(
            Appointment(
                tenant_id=world.tenant_a.id,
                patient_id=None,
                google_event_id="evt-staff",
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()
    calendar = _Calendar()
    with _turn(world.tenant_a.id, world.own.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-staff"})
    assert set(result) == {"error"}
    assert calendar.cancelled == []


async def test_a_database_outage_refuses_instead_of_cancelling(world, monkeypatch):
    """`_own_google_event_ids` fails closed (empty set) when the DB is down."""

    def _broken_factory():
        raise RuntimeError("db down")

    monkeypatch.setattr(core_database, "async_session_factory", _broken_factory)
    calendar = _Calendar()
    with _turn(world.tenant_a.id, world.own.conversation_id, calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-own"})
    assert set(result) == {"error"}
    assert calendar.cancelled == []


def test_the_refusal_reason_is_a_stable_enum():
    assert ai_tools.TOOL_BLOCK_NOT_PATIENTS_EVENT == "not_patients_event"
