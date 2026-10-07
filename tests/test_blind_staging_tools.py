"""`create_event` / `cancel_event` v2: blind tools that only stage the card (TASK-030 P4).

Owner's decision (2026-10-03): the AI keeps tools with these names, but on the v2 toolset
they never write to the agenda, never read an event, never take a title/description/end/
name/event id, and only hand back to the flow the very request the workflow's own hand-back
tools send - so the patient lands on the details + confirmation card (create) or on the
"Confirmar o cancelamento?" card (cancel), and the patient's tap is what books or cancels.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from contextlib import contextmanager  # noqa: E402
from datetime import UTC, date, datetime, time, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
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

from secretaria.ai import (  # noqa: E402
    graph,
    staging_tools as st,
    tools as ai_tools,
)
from secretaria.ai.staging_tools import cancel_event_v2, create_event_v2  # noqa: E402
from secretaria.ai.tools import (  # noqa: E402
    BookingDraftRequested,
    ManageAppointmentRequested,
    manage_existing_appointment_v2,
    set_booking_draft_v2,
)
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    Tenant,
)
from secretaria.plugins import multi_professional as mp  # noqa: E402
from secretaria.services import pii_pseudonymization as store  # noqa: E402
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_SOLE  # noqa: E402
from secretaria.services.manage_request import appointment_ref  # noqa: E402
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
CANARY = "CANARY-9d41"
ANA = SimpleNamespace(id=uuid4(), name="Dra. Ana")


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log

    def blocked(self):
        return [fields for event, fields in self.events if event == "agent_tool_blocked"]


@pytest.fixture
def log(monkeypatch):
    recorder = _Log()
    monkeypatch.setattr(st, "logger", recorder)
    return recorder


class _ExplodingCalendar:
    """Any use is a bug: a staging tool never reaches the agenda."""

    def __getattr__(self, name):
        raise AssertionError(f"calendar must not be touched (called {name})")


def _config(tenant_id, timezone="America/Sao_Paulo"):
    return SimpleNamespace(tenant_id=tenant_id, timezone=timezone)


@contextmanager
def _turn(tenant_id, conversation_id=None, *, timezone="America/Sao_Paulo"):
    pairs = [
        (ai_tools._tenant_id_ctx, tenant_id),
        (ai_tools._conversation_id_ctx, conversation_id),
        (ai_tools._calendar_ctx, _ExplodingCalendar()),
        (ai_tools._tenant_config_ctx, _config(tenant_id, timezone)),
    ]
    tokens = [(var, var.set(value)) for var, value in pairs]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


# --------------------------------------------------------------------------
# The model-facing contract: names, arguments, no free text that reaches the agenda
# --------------------------------------------------------------------------


def test_the_names_are_the_legacy_ones_and_the_arguments_carry_no_free_text():
    assert create_event_v2.name == ai_tools.create_event.name == "create_event"
    assert cancel_event_v2.name == ai_tools.cancel_event.name == "cancel_event"
    assert set(create_event_v2.args) == {"start", "service", "professional"}
    assert set(cancel_event_v2.args) == {"appointment"}
    for tool in (create_event_v2, cancel_event_v2):
        assert tool.metadata == {"cache_variant": ai_tools.BLIND_STAGING_VARIANT}
        assert "NÃO" in tool.description
    assert "Confirmar" in create_event_v2.description
    assert "nunca diga que foi cancelada" in cancel_event_v2.description
    assert "Nunca um id de evento" in cancel_event_v2.description


def test_the_compiled_agent_cache_tells_them_apart_from_the_legacy_tools():
    assert graph._tool_cache_key(create_event_v2) == "create_event#blind_v2"
    assert graph._tool_cache_key(cancel_event_v2) == "cancel_event#blind_v2"
    assert graph._tool_cache_key(ai_tools.create_event) == "create_event"
    assert graph._tool_cache_key(ai_tools.cancel_event) == "cancel_event"


# --------------------------------------------------------------------------
# create_event v2: the draft, never the agenda
# --------------------------------------------------------------------------


@pytest.fixture
def roster(monkeypatch):
    async def _active(_tenant_id):
        return [ANA]

    monkeypatch.setattr(mp, "_active_professionals", _active)


async def test_create_hands_back_the_draft_and_never_touches_the_agenda(roster):
    with _turn(uuid4()), pytest.raises(BookingDraftRequested) as caught:
        await create_event_v2.ainvoke(
            {"start": "2026-10-08T10:00", "service": " Consulta ", "professional": "dra. ana"}
        )
    exc = caught.value
    assert (exc.appointment_type, exc.professional_id, exc.insurance) == ("Consulta", ANA.id, None)
    assert (exc.day, exc.time) == (date(2026, 10, 8), time(10, 0))
    # "Pra quem" stays unknown: the resolver uses the recorded answer or asks.
    assert exc.attendee is None


@pytest.mark.parametrize(
    "start",
    ["2026-10-08T10:00", "2026-10-08 10:00", "2026-10-08T10:00:00", " 2026-10-08T10:00 "],
)
async def test_the_start_is_read_as_the_clinics_wall_clock(roster, start):
    with _turn(uuid4()), pytest.raises(BookingDraftRequested) as caught:
        await create_event_v2.ainvoke({"start": start})
    assert (caught.value.day, caught.value.time) == (date(2026, 10, 8), time(10, 0))
    assert (caught.value.appointment_type, caught.value.professional_id) == (None, None)


@pytest.mark.parametrize(
    "start",
    [
        "",
        "2026-10-08",
        "10:00",
        "2026-10-08T10:00:30",
        "2026-10-08T10:00-03:00",
        "2026-10-08T10:00Z",
        "2026-02-30T10:00",
        "2026-10-08T25:00",
        "08/10/2026 10:00",
        "quinta às 10h",
        "٢٠٢٦-١٠-٠٨T10:00",
        f"Consulta - {CANARY} Silva",
        "x" * 5000,
    ],
)
async def test_an_unusable_start_is_a_short_error_that_echoes_nothing(roster, log, start):
    with _turn(uuid4()):
        result = await create_event_v2.ainvoke({"start": start})
    assert set(result) == {"error"}
    assert "AAAA-MM-DDTHH:MM" in result["error"]
    assert CANARY not in result["error"] and CANARY not in repr(log.events)
    (fields,) = log.blocked()
    assert fields == {"tool": "create_event", "reason": "bad_start"}


async def test_a_title_or_a_name_the_model_adds_never_reaches_the_draft(roster):
    """Extra arguments are not part of the schema: no title, description or end exists."""
    with _turn(uuid4()), pytest.raises(BookingDraftRequested) as caught:
        await create_event_v2.ainvoke(
            {
                "start": "2026-10-08T10:00",
                "summary": f"Consulta - {CANARY} Silva",
                "description": CANARY,
                "end": "2026-10-08T11:00",
            }
        )
    assert CANARY not in repr(vars(caught.value))


async def test_an_unknown_professional_is_left_for_the_flow_to_ask(roster):
    with _turn(uuid4()), pytest.raises(BookingDraftRequested) as caught:
        await create_event_v2.ainvoke({"start": "2026-10-08T10:00", "professional": "Dr. X"})
    assert caught.value.professional_id is None
    assert caught.value.professional_unresolved is True


async def test_create_without_a_clinic_is_refused(log):
    token = ai_tools._tenant_id_ctx.set(None)
    try:
        result = await create_event_v2.ainvoke({"start": "2026-10-08T10:00"})
    finally:
        ai_tools._tenant_id_ctx.reset(token)
    assert set(result) == {"error"}
    assert log.blocked() == [{"tool": "create_event", "reason": "no_clinic"}]


# --------------------------------------------------------------------------
# cancel_event v2: only this patient's own appointment, only to the cancel card
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_database, "async_session_factory", maker)
    yield maker
    await engine.dispose()


def _upcoming(days: int, hour: int) -> datetime:
    """A clinic-local start `days` from now at `hour`:00, as UTC (what the DB stores)."""
    local_day = datetime.now(TZ).date() + timedelta(days=days)
    return datetime(local_day.year, local_day.month, local_day.day, hour, tzinfo=TZ).astimezone(UTC)


async def _patient(db, tenant_id, wa_id, *starts, event_prefix="evt"):
    async with db() as session:
        patient = Patient(tenant_id=tenant_id, wa_id=wa_id, name="Paciente")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant_id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        for i, start in enumerate(starts):
            session.add(
                Appointment(
                    tenant_id=tenant_id,
                    patient_id=patient.id,
                    conversation_id=conversation.id,
                    google_event_id=f"{event_prefix}-{wa_id}-{i}",
                    appointment_type="Consulta",
                    start_at=start,
                    end_at=start + timedelta(minutes=30),
                    status=AppointmentStatus.SCHEDULED,
                )
            )
        await session.commit()
        return SimpleNamespace(id=patient.id, conversation_id=conversation.id)


async def _tenant(db):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.commit()
        return tenant.id


@pytest_asyncio.fixture
async def world(db):
    """Tenant A: our patient (D+3 10:00, D+5 14:00) and another patient (D+4 09:00).
    Tenant B: a patient with D+3 10:00 too (same minute as ours)."""
    tenant_a, tenant_b = await _tenant(db), await _tenant(db)
    mine = await _patient(db, tenant_a, "5511900000001", _upcoming(3, 10), _upcoming(5, 14))
    other = await _patient(db, tenant_a, "5511900000002", _upcoming(4, 9))
    foreign = await _patient(db, tenant_b, "5511900000003", _upcoming(3, 10))
    return SimpleNamespace(
        tenant_a=tenant_a, tenant_b=tenant_b, mine=mine, other=other, foreign=foreign
    )


def _ref(days: int, hour: int) -> str:
    return appointment_ref(_upcoming(days, hour), TZ)


async def test_cancel_of_an_own_appointment_hands_back_the_cancel_request(db, world):
    with _turn(world.tenant_a, world.mine.conversation_id):
        with pytest.raises(ManageAppointmentRequested) as caught:
            await cancel_event_v2.ainvoke({"appointment": _ref(5, 14)})
    exc = caught.value
    assert exc.action == "cancel"
    assert exc.appointment.strftime("%Y-%m-%d %H:%M") == _ref(5, 14)
    assert (exc.day, exc.time) == (None, None)
    async with db() as session:  # nothing was cancelled by the tool itself
        statuses = set(await session.scalars(select(Appointment.status)))
    assert statuses == {AppointmentStatus.SCHEDULED}


@pytest.mark.parametrize(
    "who, ref_of, reason",
    [
        ("mine", (4, 9), "unknown_appointment"),  # the OTHER patient's slot, same tenant
        ("mine", (6, 10), "unknown_appointment"),  # nobody's
        ("other", (3, 10), "unknown_appointment"),  # ours, asked from their conversation
    ],
    ids=["another_patients_time", "nobodys_time", "from_the_other_conversation"],
)
async def test_an_appointment_that_is_not_the_patients_is_never_targeted(
    db, world, log, who, ref_of, reason
):
    with _turn(world.tenant_a, getattr(world, who).conversation_id):
        result = await cancel_event_v2.ainvoke({"appointment": _ref(*ref_of)})
    assert set(result) == {"error"}
    assert "manage_existing_appointment" in result["error"]
    assert log.blocked() == [{"tool": "cancel_event", "reason": reason}]
    assert _ref(*ref_of) not in repr(log.events) + result["error"]


async def test_another_tenants_appointment_at_the_same_minute_is_not_ours(db, world, log):
    """Tenant B's patient has D+3 10:00 too; from tenant B's id, our conversation finds no patient
    of that tenant - and from tenant A only OUR appointment matches."""
    with _turn(world.tenant_b, world.mine.conversation_id):
        result = await cancel_event_v2.ainvoke({"appointment": _ref(3, 10)})
    assert set(result) == {"error"}
    assert log.blocked() == [{"tool": "cancel_event", "reason": "no_patient"}]


async def test_two_of_the_patients_appointments_at_the_same_minute_are_never_guessed(db, log):
    tenant = await _tenant(db)
    twin = await _patient(db, tenant, "5511900000009", _upcoming(3, 10), _upcoming(3, 10))
    with _turn(tenant, twin.conversation_id):
        result = await cancel_event_v2.ainvoke({"appointment": _ref(3, 10)})
    assert set(result) == {"error"}
    assert log.blocked() == [{"tool": "cancel_event", "reason": "ambiguous_appointment"}]


@pytest.mark.parametrize(
    "appointment",
    ["evt-5511900000001-0", "", "terça às 10h", "2026-10-13", f"{CANARY} Silva", "x" * 5000],
)
async def test_an_event_id_or_free_text_is_not_a_reference(db, world, log, appointment):
    with _turn(world.tenant_a, world.mine.conversation_id):
        result = await cancel_event_v2.ainvoke({"appointment": appointment})
    assert set(result) == {"error"}
    assert "(ref AAAA-MM-DD HH:MM)" in result["error"]
    assert log.blocked() == [{"tool": "cancel_event", "reason": "bad_appointment"}]
    assert CANARY not in repr(log.events) + result["error"]


async def test_the_reference_is_read_in_the_clinics_zone(db):
    """The "(ref ...)" lines are written in the clinic's zone (P3); so is the match."""
    tenant = await _tenant(db)
    start = _upcoming(3, 10)
    patient = await _patient(db, tenant, "5511900000008", start)
    new_york = ZoneInfo("America/New_York")
    with _turn(tenant, patient.conversation_id, timezone="America/New_York"):
        with pytest.raises(ManageAppointmentRequested):
            await cancel_event_v2.ainvoke({"appointment": appointment_ref(start, new_york)})
        result = await cancel_event_v2.ainvoke({"appointment": appointment_ref(start, TZ)})
    assert set(result) == {"error"}  # São Paulo's wall clock is not New York's


@pytest.mark.parametrize("missing", ["tenant", "conversation"])
async def test_cancel_without_a_patient_context_is_refused(db, world, log, missing):
    tenant = None if missing == "tenant" else world.tenant_a
    conversation = None if missing == "conversation" else world.mine.conversation_id
    with _turn(tenant, conversation):
        result = await cancel_event_v2.ainvoke({"appointment": _ref(3, 10)})
    assert set(result) == {"error"}
    assert log.blocked() == [{"tool": "cancel_event", "reason": "no_clinic"}]


async def test_a_database_failure_is_an_error_and_never_a_cancel(world, log, monkeypatch):
    def _broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(core_database, "async_session_factory", _broken)
    with _turn(world.tenant_a, world.mine.conversation_id):
        result = await cancel_event_v2.ainvoke({"appointment": _ref(3, 10)})
    assert set(result) == {"error"}
    assert "NUNCA diga que algo foi cancelado" in result["error"]
    assert [e for e, _ in log.events] == ["agent_cancel_staging_failed"]


# --------------------------------------------------------------------------
# The same hand-back as the workflow's own tools: same sentinel, so the same landing
# --------------------------------------------------------------------------


def _runtime_config(tenant_id) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=tenant_id,
        clinic_name="Clinica",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="cal",
        google_refresh_token=None,
    )


async def _sentinel(monkeypatch, tool, args, *, tenant_id, conversation_id) -> str:
    """What graph.run_agent hands the worker when the model calls `tool` with `args`."""

    async def _the_model_calls_the_tool(messages):
        return str(await tool.ainvoke(args))

    async def _no_history(_conversation_id):
        return []

    monkeypatch.setattr(graph, "invoke_agent", _the_model_calls_the_tool)
    monkeypatch.setattr(graph, "_load_history", _no_history)
    return await graph.run_agent(
        "oi",
        context={"conversation_id": str(conversation_id)},
        tenant_config=_runtime_config(tenant_id),
        booking_topology=BOOKING_TOPOLOGY_SOLE,
    )


async def test_create_lands_exactly_like_the_draft_with_day_and_time(db, roster, monkeypatch):
    monkeypatch.setattr(store, "async_session_factory", db)
    ids = {"tenant_id": uuid4(), "conversation_id": uuid4()}
    staged = await _sentinel(
        monkeypatch,
        create_event_v2,
        {"start": "2026-10-08T10:00", "service": "Consulta", "professional": "Dra. Ana"},
        **ids,
    )
    drafted = await _sentinel(
        monkeypatch,
        set_booking_draft_v2,
        {"service": "Consulta", "professional": "Dra. Ana", "day": "2026-10-08", "time": "10:00"},
        **ids,
    )
    assert staged.startswith(graph.BOOKING_DRAFT_SENTINEL_PREFIX)
    assert staged == drafted


async def test_cancel_lands_exactly_like_the_manage_request(db, world, monkeypatch):
    monkeypatch.setattr(store, "async_session_factory", db)
    ids = {"tenant_id": world.tenant_a, "conversation_id": world.mine.conversation_id}
    staged = await _sentinel(monkeypatch, cancel_event_v2, {"appointment": _ref(5, 14)}, **ids)
    managed = await _sentinel(
        monkeypatch,
        manage_existing_appointment_v2,
        {"action": "cancel", "appointment": _ref(5, 14)},
        **ids,
    )
    assert staged.startswith(graph.MANAGE_APPOINTMENT_SENTINEL_PREFIX)
    assert staged == managed
