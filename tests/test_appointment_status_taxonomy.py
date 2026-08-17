"""RESCHEDULED is a LIVE status — one taxonomy for writers and readers.

PROMPT_FIX_16. Both carriers (the deterministic flow and the doctor hub) MOVE
the same appointment row when a booking is rescheduled: same id, same
`google_event_id`, same PixDeposit, new `start_at`/`end_at`, status
`RESCHEDULED`. The readers disagreed — `UPCOMING_STATUSES` and the reminders
sweep listed only SCHEDULED/CONFIRMED — so after the very first reschedule the
booking fell out of the manage flow, the greeting, the agent's appointment
tools and the reminder windows, while the metering path (which has no status
filter at all) went on billing it.

This module pins the unified definition end to end:

  * the taxonomy itself (LIVE vs TERMINAL, and that they partition the enum);
  * reschedule -> still upcoming, via `load_upcoming_appointments` (the ONE
    query behind manage, the greeting opening state and the LLM tool);
  * the flow carrier's write, including its Pix reschedule counter;
  * tenant isolation;
  * the regression half: CANCELLED / ATTENDED / NO_SHOW stay out.
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

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    LIVE_APPOINTMENT_STATUSES,
    TERMINAL_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Tenant,
    is_live_status,
)
from secretaria.services import appointment_status as status_service  # noqa: E402
from secretaria.services.flow_router import FlowRouterResult  # noqa: E402
from secretaria.services.patient_context import (  # noqa: E402
    UPCOMING_STATUSES,
    PatientOpeningState,
    load_upcoming_appointments,
    resolve_patient_opening_state,
)
from secretaria.workers import tasks  # noqa: E402

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# The taxonomy itself (pure)
# --------------------------------------------------------------------------


def test_rescheduled_is_live() -> None:
    assert AppointmentStatus.RESCHEDULED in LIVE_APPOINTMENT_STATUSES
    assert is_live_status(AppointmentStatus.RESCHEDULED) is True


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED, AppointmentStatus.RESCHEDULED],
)
def test_live_statuses(status: AppointmentStatus) -> None:
    assert is_live_status(status) is True


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW],
)
def test_terminal_statuses(status: AppointmentStatus) -> None:
    assert is_live_status(status) is False
    assert status in TERMINAL_APPOINTMENT_STATUSES


def test_live_and_terminal_partition_the_enum() -> None:
    """Every member is classified exactly once.

    The guard that matters when a status is added later: a new member nobody
    classifies would silently read as terminal, i.e. bookings quietly stop
    being upcoming — the very failure this round fixes.
    """
    live = set(LIVE_APPOINTMENT_STATUSES)
    terminal = set(TERMINAL_APPOINTMENT_STATUSES)
    assert live | terminal == set(AppointmentStatus)
    assert live & terminal == set()


def test_upcoming_statuses_is_the_shared_constant() -> None:
    """The reader must not keep a private copy that can drift again."""
    assert UPCOMING_STATUSES is LIVE_APPOINTMENT_STATUSES


def test_none_status_is_not_live() -> None:
    assert is_live_status(None) is False


# --------------------------------------------------------------------------
# Transition logging
# --------------------------------------------------------------------------


def test_transition_log_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[tuple[str, dict]] = []

    class _Recorder:
        def _record(self, event, **kwargs):
            records.append((event, kwargs))

        info = warning = error = debug = _record

    monkeypatch.setattr(status_service, "logger", _Recorder())
    appointment_id, tenant_id = uuid4(), uuid4()

    status_service.log_status_transition(
        appointment_id=appointment_id,
        tenant_id=tenant_id,
        old_status=AppointmentStatus.SCHEDULED,
        new_status=AppointmentStatus.RESCHEDULED,
        source=status_service.SOURCE_FLOW,
        idempotency_key="resched:evt-1:2026-07-22T10:00:00+00:00",
    )

    assert len(records) == 1
    event, fields = records[0]
    assert event == "appointment_status_transition"
    assert fields["appointment_id"] == str(appointment_id)
    assert fields["tenant_id"] == str(tenant_id)
    assert fields["old_status"] == "scheduled"
    assert fields["new_status"] == "rescheduled"
    assert fields["source"] == "flow"
    assert fields["still_live"] is True
    # Internal ids and status names only - no phone, no name, no clinical text.
    assert set(fields) == {
        "appointment_id",
        "tenant_id",
        "old_status",
        "new_status",
        "source",
        "idempotency_key",
        "still_live",
    }


def test_transition_log_marks_a_move_out_of_the_live_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`still_live=False` is the countable signal for "left the live set"."""
    records: list[tuple[str, dict]] = []

    class _Recorder:
        def _record(self, event, **kwargs):
            records.append((event, kwargs))

        info = warning = error = debug = _record

    monkeypatch.setattr(status_service, "logger", _Recorder())
    status_service.log_status_transition(
        appointment_id=uuid4(),
        tenant_id=uuid4(),
        old_status=AppointmentStatus.RESCHEDULED,
        new_status=AppointmentStatus.CANCELLED,
        source=status_service.SOURCE_HUB,
    )

    assert records[0][1]["still_live"] is False
    assert records[0][1]["idempotency_key"] is None


# --------------------------------------------------------------------------
# DB fixtures
# --------------------------------------------------------------------------


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
def _wire(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    yield


async def _seed(db, *, status: AppointmentStatus = AppointmentStatus.SCHEDULED) -> dict:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            timezone="America/Sao_Paulo",
        )
        session.add(tenant)
        await session.flush()
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999990000", name="Maria")
        session.add(patient)
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            conversation_id=conversation.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=NOW + timedelta(days=3),
            end_at=NOW + timedelta(days=3, minutes=30),
            status=status,
        )
        session.add(appointment)
        await session.commit()
        return {
            "tenant": tenant,
            "patient": patient,
            "conversation": conversation,
            "appointment": appointment,
        }


async def _upcoming(db, tenant_id, patient_id) -> list[dict]:
    async with db() as session:
        return await load_upcoming_appointments(session, tenant_id, patient_id, now=NOW)


# --------------------------------------------------------------------------
# A rescheduled booking stays upcoming (the ONE query behind manage /
# greeting / list_patient_appointments)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED, AppointmentStatus.RESCHEDULED],
)
async def test_live_appointments_are_upcoming(db, status: AppointmentStatus) -> None:
    seeded = await _seed(db, status=status)

    rows = await _upcoming(db, seeded["tenant"].id, seeded["patient"].id)

    assert [r["id"] for r in rows] == [str(seeded["appointment"].id)]


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW],
)
async def test_terminal_appointments_are_not_upcoming(db, status: AppointmentStatus) -> None:
    """Regression: the other half of the taxonomy must stay out."""
    seeded = await _seed(db, status=status)

    assert await _upcoming(db, seeded["tenant"].id, seeded["patient"].id) == []


async def test_rescheduled_booking_drives_the_greeting_opening_state(db) -> None:
    """HAS_UPCOMING(_SOON) is derived from the same query — a moved booking
    must still produce a "you have a consultation" greeting."""
    seeded = await _seed(db, status=AppointmentStatus.RESCHEDULED)

    async with db() as session:
        context = await resolve_patient_opening_state(
            session, seeded["tenant"].id, seeded["patient"].id, now=NOW
        )

    assert context is not None
    assert context.state in (
        PatientOpeningState.HAS_UPCOMING,
        PatientOpeningState.HAS_UPCOMING_SOON,
    )
    assert [a["id"] for a in context.future_appointments] == [str(seeded["appointment"].id)]


async def test_rescheduled_booking_reaches_the_llm_tool(db, monkeypatch) -> None:
    """`list_patient_appointments` reads the same query — the agent must not
    tell a patient they have no appointment right after moving one."""
    seeded = await _seed(db, status=AppointmentStatus.RESCHEDULED)
    # This tool resolves "future" against the real clock (not the fixed NOW the
    # other tests inject), so the booking is moved to a genuinely future window.
    real_now = datetime.now(UTC)
    async with db() as session:
        appt = await session.get(Appointment, seeded["appointment"].id)
        appt.start_at = real_now + timedelta(days=3)
        appt.end_at = real_now + timedelta(days=3, minutes=30)
        await session.commit()

    # The tool imports async_session_factory lazily FROM core.database, so it
    # is patched there (same seam as tests/test_list_patient_appointments_tool.py).
    monkeypatch.setattr(core_database, "async_session_factory", db)

    tok_tid = ai_tools._tenant_id_ctx.set(seeded["tenant"].id)
    tok_conv = ai_tools._conversation_id_ctx.set(seeded["conversation"].id)
    try:
        result = await ai_tools.list_patient_appointments.ainvoke({})
    finally:
        ai_tools._tenant_id_ctx.reset(tok_tid)
        ai_tools._conversation_id_ctx.reset(tok_conv)

    assert result["count"] == 1
    assert len(result["appointments"]) == 1


async def test_a_rescheduled_booking_can_be_rescheduled_again(db) -> None:
    """RESCHEDULED -> RESCHEDULED: still manageable, no dead end."""
    seeded = await _seed(db, status=AppointmentStatus.RESCHEDULED)

    rows = await _upcoming(db, seeded["tenant"].id, seeded["patient"].id)
    assert len(rows) == 1  # visible to the manage flow, which acts on this list

    async with db() as session:
        appt = await session.get(Appointment, seeded["appointment"].id)
        appt.start_at = NOW + timedelta(days=5)
        appt.end_at = NOW + timedelta(days=5, minutes=30)
        appt.status = AppointmentStatus.RESCHEDULED
        await session.commit()

    rows = await _upcoming(db, seeded["tenant"].id, seeded["patient"].id)
    assert len(rows) == 1
    assert rows[0]["id"] == str(seeded["appointment"].id)  # same booking throughout


async def test_two_tenants_with_identical_bookings_do_not_leak(db) -> None:
    a = await _seed(db, status=AppointmentStatus.RESCHEDULED)
    b = await _seed(db, status=AppointmentStatus.RESCHEDULED)

    rows_a = await _upcoming(db, a["tenant"].id, a["patient"].id)
    rows_b = await _upcoming(db, b["tenant"].id, b["patient"].id)

    assert [r["id"] for r in rows_a] == [str(a["appointment"].id)]
    assert [r["id"] for r in rows_b] == [str(b["appointment"].id)]
    # And a cross-scoped lookup finds nothing.
    assert await _upcoming(db, a["tenant"].id, b["patient"].id) == []


async def test_past_rescheduled_booking_reads_as_a_recent_consult(db) -> None:
    """The row's `start_at` MOVED to the new window, so a past `start_at` on a
    rescheduled booking really did just happen. It used to be excluded from the
    recent-past lookback together with CANCELLED."""
    seeded = await _seed(db, status=AppointmentStatus.RESCHEDULED)
    async with db() as session:
        appt = await session.get(Appointment, seeded["appointment"].id)
        appt.start_at = NOW - timedelta(hours=2)
        appt.end_at = NOW - timedelta(hours=1)
        await session.commit()

    async with db() as session:
        context = await resolve_patient_opening_state(
            session, seeded["tenant"].id, seeded["patient"].id, now=NOW
        )

    assert context is not None
    assert context.state == PatientOpeningState.JUST_HAD_CONSULT


async def test_past_cancelled_booking_is_still_not_a_recent_consult(db) -> None:
    """Regression: CANCELLED stays excluded from the lookback."""
    seeded = await _seed(db, status=AppointmentStatus.CANCELLED)
    async with db() as session:
        appt = await session.get(Appointment, seeded["appointment"].id)
        appt.start_at = NOW - timedelta(hours=2)
        appt.end_at = NOW - timedelta(hours=1)
        await session.commit()

    async with db() as session:
        context = await resolve_patient_opening_state(
            session, seeded["tenant"].id, seeded["patient"].id, now=NOW
        )

    assert context is not None
    assert context.state != PatientOpeningState.JUST_HAD_CONSULT


# --------------------------------------------------------------------------
# The flow carrier: one row moved, deposit intact, counter correct
# --------------------------------------------------------------------------


async def _run_flow_reschedule(db, seeded, *, new_start: datetime) -> None:
    reply = tasks._ReplyContext(
        conversation_id=seeded["conversation"].id,
        tenant_id=seeded["tenant"].id,
        patient_wa_id=seeded["patient"].wa_id,
        inbound_body="",
    )
    result = FlowRouterResult(
        action="reply",
        bubbles=[],
        appointment_reschedule={
            "google_event_id": seeded["appointment"].google_event_id,
            "start_at": new_start,
            "end_at": new_start + timedelta(minutes=30),
        },
    )
    async with db() as session:
        tenant = await session.get(Tenant, seeded["tenant"].id)
    await tasks._apply_flow_result(
        reply, result, seeded["patient"].wa_id, tenant=tenant, waba_token="tok"
    )


async def test_flow_reschedule_moves_the_same_row_and_keeps_it_upcoming(db) -> None:
    seeded = await _seed(db)
    new_start = NOW + timedelta(days=6)

    await _run_flow_reschedule(db, seeded, new_start=new_start)

    async with db() as session:
        appt = await session.get(Appointment, seeded["appointment"].id)
        assert appt is not None  # the SAME row, never a replacement
        assert appt.status == AppointmentStatus.RESCHEDULED
        assert appt.google_event_id == seeded["appointment"].google_event_id
        assert appt.start_at.replace(tzinfo=UTC) == new_start

    rows = await _upcoming(db, seeded["tenant"].id, seeded["patient"].id)
    assert [r["id"] for r in rows] == [str(seeded["appointment"].id)]


async def test_flow_reschedule_logs_the_transition(db, monkeypatch: pytest.MonkeyPatch) -> None:
    seeded = await _seed(db)
    calls: list[dict] = []

    def _capture(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(tasks, "log_status_transition", _capture)

    await _run_flow_reschedule(db, seeded, new_start=NOW + timedelta(days=6))

    assert len(calls) == 1
    assert calls[0]["old_status"] == AppointmentStatus.SCHEDULED
    assert calls[0]["new_status"] == AppointmentStatus.RESCHEDULED
    assert calls[0]["source"] == "flow"
    assert calls[0]["appointment_id"] == seeded["appointment"].id
    assert calls[0]["idempotency_key"].startswith("resched:")


async def test_reprocessing_the_same_reschedule_does_not_move_it_twice(db) -> None:
    """Idempotent replay: the same target window applied twice lands on the
    same row, at the same time, once."""
    seeded = await _seed(db)
    new_start = NOW + timedelta(days=6)

    await _run_flow_reschedule(db, seeded, new_start=new_start)
    await _run_flow_reschedule(db, seeded, new_start=new_start)

    rows = await _upcoming(db, seeded["tenant"].id, seeded["patient"].id)
    assert len(rows) == 1
    async with db() as session:
        appt = await session.get(Appointment, seeded["appointment"].id)
        assert appt.start_at.replace(tzinfo=UTC) == new_start


async def test_deposit_follows_the_rescheduled_booking_and_counter_increments(db) -> None:
    """Pix: the deposit stays bound to the SAME appointment id, and the
    reschedule counter moves by exactly one per move."""
    seeded = await _seed(db)
    async with db() as session:
        tenant = await session.get(Tenant, seeded["tenant"].id)
        tenant.pix_reschedule_limit = 3
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=seeded["tenant"].id,
            appointment_id=seeded["appointment"].id,
            patient_id=seeded["patient"].id,
            asaas_payment_id="pay_1",
            amount_cents=7900,
            percent_applied=0,
            status=PixDepositStatus.PAID,
            reschedule_count=0,
        )
        session.add(deposit)
        await session.commit()
        deposit_id = deposit.id

    await _run_flow_reschedule(db, seeded, new_start=NOW + timedelta(days=6))

    async with db() as session:
        moved = await session.get(PixDeposit, deposit_id)
        assert moved is not None
        assert moved.appointment_id == seeded["appointment"].id  # never re-pointed
        assert moved.status == PixDepositStatus.PAID
        assert moved.reschedule_count == 1
