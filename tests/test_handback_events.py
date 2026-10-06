"""Every AI hand-back writes exactly ONE `conversation_handback_entered` event.

TASK-030 P1. The agent's hand-back tools end in worker handlers that land the patient on a
flow step - or fall back to the menu, to a human, or to nothing at all. These tests pin that
every handler exit, success or fallback, records where the patient landed, once, with field
names and reason codes only. DB-backed pieces use the in-memory-sqlite pattern of
tests/test_agent_menu_tools.py.
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
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai.graph import (  # noqa: E402
    BOOKING_DRAFT_SENTINEL_PREFIX,
    SELECT_PROFESSIONAL_SENTINEL_PREFIX,
    START_GUIDED_BOOKING_SENTINEL_PREFIX,
)
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Professional,
    Tenant,
)
from secretaria.services import (  # noqa: E402
    booking_draft as bd,
    booking_hold as booking_hold_service,
)
from secretaria.services.attendee import ATTENDEE_QUESTION_BODY, ATTENDEE_SELF  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    ATTENDEE_NEXT_BOOK,
    FlowRouterResult,
    MenuBubble,
    ai_draft_v2_enabled,
)
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import handback_log, sentinels  # noqa: E402


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
def _patch_session_factory(monkeypatch: pytest.MonkeyPatch, db):
    # ai/tools imports async_session_factory lazily (patch the source); the workers import
    # it at module level (patch every split module through workers_ns).
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    yield


async def _seed(db, *, flow_state=FlowState.LLM, selected=None, insurance=None, attendee=None):
    """A two-doctor clinic (Dra. Ana, Dr. Bruno), one patient, one conversation."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            initial_flows={"enabled": True, "menu_label": "Como posso ajudar?"},
            appointment_types=[{"name": "Consulta Geral", "duration_min": 30, "is_active": True}],
            business_hours={
                day: [{"start": "08:00", "end": "18:00"}]
                for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
            },
        )
        session.add(tenant)
        await session.flush()
        ana = Professional(
            tenant_id=tenant.id,
            name="Dra. Ana",
            specialty="Cardiologia",
            about="Atendo com foco em prevenção.",
            context_doctor_message="Prefere retornos pela manhã.",
            is_active=True,
        )
        bruno = Professional(tenant_id=tenant.id, name="Dr. Bruno", is_active=True)
        session.add_all([ana, bruno])
        await session.flush()
        patient = Patient(tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_state=flow_state,
            flow_selected_professional_id=(ana.id if selected else None),
            flow_selected_insurance=insurance,
            flow_attendee_name=attendee,
        )
        session.add(conversation)
        await session.flush()
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                wam_id="wamid.keep",
                body="oi",
            )
        )
        await session.commit()
        for obj in (tenant, ana, bruno, patient, conversation):
            await session.refresh(obj)
        return tenant, ana, bruno, patient, conversation


async def _seed_sole(db, **kw):
    """The `_seed` clinic reduced to ONE active professional (a SOLE tenant)."""
    tenant, ana, bruno, patient, conversation = await _seed(db, **kw)
    async with db() as session:
        row = await session.get(Professional, bruno.id)
        row.is_active = False
        await session.commit()
    return tenant, ana, patient, conversation


async def _seed_future_appointment(db, tenant, patient, *, start_at):
    async with db() as session:
        appt = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta Geral",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appt)
        await session.commit()
        await session.refresh(appt)
        return appt


def _snapshots(professionals):
    return [
        SimpleNamespace(
            id=p.id,
            name=p.name,
            specialty=p.specialty,
            about=p.about,
            context_doctor_message=p.context_doctor_message,
            appointment_types=p.appointment_types,
            business_hours=p.business_hours,
        )
        for p in professionals
    ]


def _reply_ctx(conversation) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_ref="5511999999999",
        inbound_body="tanto faz",
    )


@pytest.fixture
def _captured_bubbles(monkeypatch: pytest.MonkeyPatch):
    captured: list = []

    async def _fake_dispatch(reply, bubbles, tenant=None, waba_token=None):
        captured.extend(bubbles)
        return len(bubbles)

    monkeypatch.setattr(workers_ns, "_dispatch_bubbles", _fake_dispatch)
    return captured


class _StubCalendar:
    """Every day of the window is free; no Google, no credentials."""

    def __init__(self):
        self.tzinfo = ZoneInfo("America/Sao_Paulo")

    async def list_available_days(self, start_day, days, slot_minutes=None):
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(min(days, 3))]


@pytest.fixture
def _stub_calendar(monkeypatch: pytest.MonkeyPatch) -> _StubCalendar:
    calendar = _StubCalendar()

    async def _fake(session, tenant, target):
        return calendar

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _fake)
    return calendar


class _LogRecorder:
    """Records every structlog call - `caplog` is empty for structlog in this suite."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **fields) -> None:
            self.records.append((level, event, fields))

        return _log


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(workers_ns, "logger", recorder)
    return recorder


def _events(log: _LogRecorder, name: str = handback_log.EVENT_NAME) -> list[dict]:
    return [fields for _level, event, fields in log.records if event == name]


# --------------------------------------------------------------------------
# show_main_menu: the agent's own tool is a hand-back; every other menu is not
# --------------------------------------------------------------------------


async def test_the_agents_menu_tool_logs_one_handback_at_the_menu(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    # Called the way `_send_bot_reply` calls it for the show_main_menu sentinel: no `source`.
    await tasks._handle_show_main_menu(
        _reply_ctx(conversation), tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event == {
        "conversation_id": str(conversation.id),
        "tenant_id": str(tenant.id),
        "source_tool": "show_main_menu",
        "landing_step": "menu",
        "supplied": [],
        "accepted": [],
        "dropped": {},
        "fallback": None,
        "topology": "multi",
        "channel": "whatsapp",
    }
    assert isinstance(_captured_bubbles[0], MenuBubble)
    # The pre-existing event keeps being emitted, once.
    assert len(_events(log, "conversation_menu_rendered")) == 1


async def test_the_menu_tool_without_a_tenant_is_counted_not_silent(
    db, _captured_bubbles, log
) -> None:
    _tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_show_main_menu(
        _reply_ctx(conversation), None, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["source_tool"] == "show_main_menu"
    assert event["fallback"] == "no_tenant"
    assert event["landing_step"] is None
    assert event["tenant_id"] is None
    assert _captured_bubbles == []


@pytest.mark.parametrize(
    "source", ["command", "name_captured", "verified_account", "sentinel_fallback"]
)
async def test_a_menu_rendered_for_any_other_reason_is_not_a_hand_back(
    db, _captured_bubbles, log, source
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_show_main_menu(
        _reply_ctx(conversation), tenant, _snapshots([ana, bruno]), patient.wa_id, source=source
    )

    assert _events(log) == []
    (rendered,) = _events(log, "conversation_menu_rendered")
    assert rendered["source"] == source


# --------------------------------------------------------------------------
# select_professional_and_continue
# --------------------------------------------------------------------------


async def test_select_professional_logs_the_step_it_landed_on(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db, attendee=ATTENDEE_SELF)

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}",
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["source_tool"] == "select_professional"
    assert event["landing_step"] == "awaiting_service"
    assert event["supplied"] == ["professional"]
    assert event["accepted"] == ["professional"]
    assert event["dropped"] == {}
    assert event["fallback"] is None
    assert event["topology"] == "multi"
    assert event["tenant_id"] == str(tenant.id)


async def test_an_unknown_professional_falls_back_to_the_menu_and_is_counted_once(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{uuid4()}",  # not on the roster
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "unknown_professional"
    assert event["landing_step"] == "menu"
    assert event["supplied"] == ["professional"]
    assert event["accepted"] == []
    assert event["dropped"] == {"professional": "unknown_professional"}
    # The menu the fallback renders is the pre-existing event - NOT a second hand-back.
    (rendered,) = _events(log, "conversation_menu_rendered")
    assert rendered["source"] == "sentinel_fallback"
    assert len(_captured_bubbles) == 1
    assert isinstance(_captured_bubbles[0], MenuBubble)


async def test_a_malformed_professional_id_is_a_bad_sentinel(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}not-a-uuid",
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "bad_sentinel"
    assert event["landing_step"] == "menu"
    assert event["supplied"] == []
    assert event["dropped"] == {}


async def test_select_professional_without_a_tenant_is_counted_not_silent(
    db, _captured_bubbles, log
) -> None:
    _tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}",
        None,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "no_tenant"
    assert event["landing_step"] is None  # no tenant, so no menu was rendered either
    assert event["accepted"] == ["professional"]
    assert event["tenant_id"] is None
    assert _captured_bubbles == []


async def test_a_doctor_with_no_services_lands_on_the_config_alert(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    alerted: list = []

    async def _fake_alert(reply, result, **_kwargs):
        alerted.append(result.professional_config_gap)

    monkeypatch.setattr(workers_ns, "_handle_professional_config_incomplete", _fake_alert)
    tenant, ana, bruno, patient, conversation = await _seed(db)
    # Her own empty list: nothing to book. Set on the ROW - the handler reads fresh.
    async with db() as session:
        doctor = await session.get(Professional, ana.id)
        doctor.appointment_types = []
        await session.commit()
    snapshots = _snapshots([ana, bruno])

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}",
        tenant,
        None,
        snapshots,
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == "config_incomplete"
    assert event["fallback"] == "professional_config_incomplete"
    assert event["accepted"] == ["professional"]
    assert alerted == ["services"]


async def test_the_event_is_logged_before_the_flow_state_is_written(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    """A failure while persisting/sending must not erase the fact that the hand-back landed."""

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("persist exploded")

    monkeypatch.setattr(workers_ns, "_apply_flow_result", _boom)
    tenant, ana, bruno, patient, conversation = await _seed(db, attendee=ATTENDEE_SELF)

    with pytest.raises(RuntimeError, match="persist exploded"):
        await tasks._handle_select_professional(
            _reply_ctx(conversation),
            f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}",
            tenant,
            None,
            _snapshots([ana, bruno]),
            patient.wa_id,
        )

    (event,) = _events(log)
    assert event["landing_step"] == "awaiting_service"


# --------------------------------------------------------------------------
# manage_existing_appointment
# --------------------------------------------------------------------------


async def test_manage_logs_the_step_it_landed_on(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)
    await _seed_future_appointment(
        db, tenant, patient, start_at=datetime.now(UTC) + timedelta(days=2)
    )

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "cancel", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["source_tool"] == "manage_existing_appointment"
    assert event["landing_step"] == "manage_cancel_confirm"
    assert event["supplied"] == ["action"]
    assert event["accepted"] == ["action"]
    assert event["fallback"] is None
    assert event["topology"] == "multi"
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_step == event["landing_step"]


async def test_manage_with_two_appointments_lands_on_the_pick_list(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)
    now = datetime.now(UTC)
    await _seed_future_appointment(db, tenant, patient, start_at=now + timedelta(days=1))
    await _seed_future_appointment(db, tenant, patient, start_at=now + timedelta(days=5))

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "cancel", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["landing_step"] == "manage_pick_cancel"
    assert event["fallback"] is None


async def test_manage_with_nothing_to_manage_lands_on_the_menu_and_says_why(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "reschedule", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["landing_step"] == "menu"
    assert event["fallback"] == "no_appointments"
    assert event["accepted"] == ["action"]


async def test_manage_with_an_unknown_action_is_a_bad_sentinel(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "excluir", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["fallback"] == "bad_sentinel"
    assert event["landing_step"] == "menu"
    assert event["supplied"] == []
    assert isinstance(_captured_bubbles[0], MenuBubble)


async def test_manage_without_a_tenant_is_counted_not_silent(db, _captured_bubbles, log) -> None:
    _tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "cancel", None, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["fallback"] == "no_tenant"
    assert event["landing_step"] is None
    assert event["tenant_id"] is None
    assert _captured_bubbles == []


async def test_manage_without_flows_is_counted_not_silent(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    # `flows_enabled` is always True today; the guard is defensive and still has to count.
    monkeypatch.setattr(sentinels, "flows_enabled", lambda _tenant: False)
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "cancel", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["fallback"] == "without_flows"
    assert event["landing_step"] is None
    assert _captured_bubbles == []


async def test_manage_for_a_conversation_without_a_patient_is_counted_not_silent(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, _conversation = await _seed(db)
    orphan = tasks._ReplyContext(
        conversation_id=uuid4(), patient_ref=patient.wa_id, inbound_body="tanto faz"
    )

    await tasks._handle_manage_appointment(
        orphan, "cancel", tenant, _snapshots([ana, bruno]), patient.wa_id
    )

    (event,) = _events(log)
    assert event["fallback"] == "no_patient"
    assert event["landing_step"] is None
    assert _captured_bubbles == []


async def test_a_failure_before_the_landing_is_known_propagates_and_logs_no_event(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    """Decision (plan P1): an infrastructure failure before the landing is known is not a
    landing. It propagates exactly as before (the job fails, the turn safety net answers);
    the gap between `ai_run_agent_manage_appointment` and this event is how it is spotted."""

    async def _db_down(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(workers_ns, "load_upcoming_appointments", _db_down)
    tenant, ana, bruno, patient, conversation = await _seed(db)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await tasks._handle_manage_appointment(
            _reply_ctx(conversation), "cancel", tenant, _snapshots([ana, bruno]), patient.wa_id
        )

    assert _events(log) == []


# --------------------------------------------------------------------------
# start_guided_booking
# --------------------------------------------------------------------------


async def test_guided_booking_logs_the_step_it_landed_on(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    tenant, ana, patient, conversation = await _seed_sole(db, attendee=ATTENDEE_SELF)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        tenant,
        _snapshots([ana]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["source_tool"] == "start_guided_booking"
    assert event["landing_step"] == "awaiting_day"
    assert event["supplied"] == ["service"]
    assert event["accepted"] == ["service"]
    assert event["fallback"] is None
    assert event["topology"] == "sole"
    # The pre-existing event keeps being emitted, once.
    assert len(_events(log, "conversation_guided_booking_entered")) == 1


async def test_guided_booking_turns_a_multi_doctor_clinic_away_and_says_so(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        tenant,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "multi_professional"
    assert event["landing_step"] == "menu"
    assert event["topology"] == "multi"
    assert event["supplied"] == ["service"]
    assert isinstance(_captured_bubbles[0], MenuBubble)


async def test_guided_booking_without_a_tenant_is_counted_not_silent(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    _tenant, ana, patient, conversation = await _seed_sole(db)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        None,
        _snapshots([ana]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "no_tenant"
    assert event["landing_step"] is None
    assert event["supplied"] == ["service"]
    assert _captured_bubbles == []


async def test_guided_booking_with_no_agenda_lands_on_a_person(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    handed_off: list = []

    async def _no_calendar(session, tenant, target):
        return None

    async def _fake_unavailable(reply, redis=None, tenant=None, waba_token=None):
        handed_off.append(reply.conversation_id)

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _no_calendar)
    monkeypatch.setattr(workers_ns, "_handle_calendar_unavailable", _fake_unavailable)
    tenant, ana, patient, conversation = await _seed_sole(db, attendee=ATTENDEE_SELF)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        tenant,
        _snapshots([ana]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == "human_handover"
    assert event["fallback"] == "calendar_unavailable"
    assert handed_off == [conversation.id]


# --------------------------------------------------------------------------
# set_booking_draft
# --------------------------------------------------------------------------


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("attendee", [None, "", "Atendido Teste"])
async def test_selection_only_draft_logs_the_administrative_step_it_lands_on(
    db, _captured_bubbles, _stub_calendar, log, multi, attendee
) -> None:
    """The most common hand-back: an empty draft. It used to log nothing about where it landed."""
    if multi:
        tenant, ana, bruno, patient, conversation = await _seed(db)
    else:
        tenant, ana, patient, conversation = await _seed_sole(db)
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        conv.flow_attendee_name = attendee
        await session.commit()

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + "{}",
        tenant,
        None,
        [],
        patient.wa_id,
    )

    expected = (
        "awaiting_attendee_choice"
        if attendee is None
        else "awaiting_professional"
        if multi
        else "awaiting_service"
    )
    (event,) = _events(log)
    assert event["source_tool"] == "set_booking_draft"
    assert event["landing_step"] == expected
    assert event["supplied"] == []
    assert event["accepted"] == []
    assert event["dropped"] == {}
    assert event["fallback"] is None
    assert event["topology"] == ("multi" if multi else "sole")
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_step == event["landing_step"]


async def test_selection_only_draft_on_a_clinic_with_no_bookable_catalog_is_a_counted_fallback(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    tenant, ana, patient, conversation = await _seed_sole(db)
    async with db() as session:
        doctor = await session.get(Professional, ana.id)
        doctor.appointment_types = []
        await session.commit()

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + "{}",
        tenant,
        None,
        [],
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "no_bookable_catalog"
    assert event["landing_step"] == "menu"
    assert isinstance(_captured_bubbles[0], MenuBubble)


@pytest.mark.parametrize(
    "multi, build, fallback, supplied, accepted, dropped",
    [
        pytest.param(
            True,
            lambda ana: {"t": "Botox", "p": str(ana.id)},
            "invalid_selection",
            ["service", "professional"],
            ["professional"],
            {"service": "not_in_catalog"},
            id="service_not_in_catalog",
        ),
        pytest.param(
            True,
            lambda ana: {"t": "Consulta Geral", "p": str(uuid4())},
            "invalid_selection",
            ["service", "professional"],
            ["service"],
            {"professional": "unknown_professional"},
            id="professional_not_on_the_roster",
        ),
        pytest.param(
            True,
            lambda ana: {"t": "Consulta Geral"},
            "missing_professional",
            ["service"],
            ["service"],
            {},
            id="multi_clinic_service_without_doctor",
        ),
        pytest.param(
            False,
            lambda ana: {"p": str(ana.id)},
            "invalid_selection",
            ["professional"],
            ["professional"],
            {},
            id="sole_clinic_doctor_without_service",
        ),
    ],
)
async def test_a_draft_that_cannot_land_bounces_to_the_menu_with_its_reason(
    db, _captured_bubbles, _stub_calendar, log, multi, build, fallback, supplied, accepted, dropped
) -> None:
    if multi:
        tenant, ana, bruno, patient, conversation = await _seed(db, attendee=ATTENDEE_SELF)
    else:
        tenant, ana, patient, conversation = await _seed_sole(db, attendee=ATTENDEE_SELF)

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps(build(ana)),
        tenant,
        None,
        [],
        patient.wa_id,
    )

    (event,) = _events(log)  # exactly one: the menu fallback is not a second hand-back
    assert event["fallback"] == fallback
    assert event["landing_step"] == "menu"
    assert event["supplied"] == supplied
    assert event["accepted"] == accepted
    assert event["dropped"] == dropped
    assert isinstance(_captured_bubbles[0], MenuBubble)


@pytest.mark.parametrize(
    "service, landing, accepted",
    [
        ("Consulta Geral", "awaiting_day", ["service", "professional"]),
        (None, "awaiting_service", ["professional"]),
    ],
)
async def test_a_draft_that_lands_logs_the_step_and_keeps_the_old_event(
    db, _captured_bubbles, _stub_calendar, log, service, landing, accepted
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db, attendee=ATTENDEE_SELF)

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps({"t": service, "p": str(ana.id)}),
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == landing
    assert event["supplied"] == accepted
    assert event["accepted"] == accepted
    assert event["fallback"] is None
    assert event["topology"] == "multi"
    assert len(_events(log, "conversation_booking_draft_entered")) == 1


@pytest.mark.parametrize(
    "typed, accepted, dropped",
    [
        ("unimed", ["insurance"], {}),
        ("Inventado", [], {"insurance": "unmatched_plan"}),
    ],
)
async def test_a_typed_convenio_is_accepted_or_dropped_by_name_only(
    db, _captured_bubbles, _stub_calendar, log, typed, accepted, dropped
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.collect_insurance = True
        row.insurances = ["Unimed"]
        await session.commit()
        await session.refresh(row)
        tenant = row

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps({"i": typed}),
        tenant,
        None,
        [],
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["supplied"] == ["insurance"]
    assert event["accepted"] == accepted
    assert event["dropped"] == dropped
    assert event["fallback"] is None


@pytest.mark.parametrize("suffix", ["oops", "[]", "null", '{"t": 7}', '{"p": "invalid"}'])
async def test_a_corrupt_draft_is_a_bad_sentinel_with_nothing_trusted(
    db, _captured_bubbles, log, suffix
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + suffix,
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "bad_sentinel"
    assert event["landing_step"] == "menu"
    assert event["supplied"] == []
    assert event["accepted"] == []
    assert event["dropped"] == {}


@pytest.mark.parametrize("reason", ["no_tenant", "without_flows"])
async def test_a_draft_that_cannot_run_is_counted_not_silent(
    db, _captured_bubbles, log, monkeypatch, reason
) -> None:
    if reason == "without_flows":
        # `flows_enabled` is always True today; the guard is defensive and still has to count.
        monkeypatch.setattr(sentinels, "flows_enabled", lambda _tenant: False)
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps({"t": "Consulta Geral"}),
        None if reason == "no_tenant" else tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == reason
    assert event["landing_step"] is None
    assert event["supplied"] == ["service"]
    assert _captured_bubbles == []


async def test_no_hand_back_event_carries_what_the_patient_or_the_model_wrote(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db, attendee=ATTENDEE_SELF)
    typed_service = "Limpeza da Maria Silva"
    typed_plan = "Plano da Maria joao@example.com 11999998888"
    payload = {"t": typed_service, "p": str(ana.id), "i": typed_plan}

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps(payload),
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["fallback"] == "invalid_selection"
    assert event["dropped"] == {"service": "not_in_catalog", "insurance": "unmatched_plan"}
    rendered = repr(event)
    for secret in (
        typed_service,
        typed_plan,
        "Silva",
        "joao@example.com",
        "11999998888",
        str(ana.id),
        str(bruno.id),
        str(patient.id),
        patient.wa_id,
        patient.name,
    ):
        assert secret not in rendered, secret


def test_draft_verdicts_names_what_survived_and_why_the_rest_did_not() -> None:
    accepted, dropped = sentinels._draft_verdicts(
        appointment_type="Botox",
        canonical_type=None,
        professional_id=uuid4(),
        professional=SimpleNamespace(),
        insurance_text="Inventado",
        insurance=None,
    )

    assert accepted == ("professional",)
    assert dropped == {"service": "not_in_catalog", "insurance": "unmatched_plan"}


def test_draft_verdicts_ignores_what_the_agent_did_not_supply() -> None:
    # A convênio already stored on the conversation is not something the agent supplied.
    accepted, dropped = sentinels._draft_verdicts(
        appointment_type=None,
        canonical_type=None,
        professional_id=None,
        professional=None,
        insurance_text=None,
        insurance="Unimed",
    )

    assert (accepted, dropped) == ((), {})


# --------------------------------------------------------------------------
# request_human_handoff
# --------------------------------------------------------------------------


async def test_human_handoff_is_a_counted_hand_back_to_a_person(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    async def _notify(**_kwargs):
        return 1

    monkeypatch.setattr(workers_ns, "notify_human_handoff", _notify)
    tenant, ana, _bruno, _patient, conversation = await _seed(db, selected=True)

    await tasks._handle_human_handoff(
        _reply_ctx(conversation), "patient_requested_human", tenant, None
    )

    (event,) = _events(log)
    assert event == {
        "conversation_id": str(conversation.id),
        "tenant_id": str(tenant.id),
        "source_tool": "request_human_handoff",
        "landing_step": "human_handover",
        "supplied": ["reason"],
        "accepted": ["reason"],
        "dropped": {},
        "fallback": None,
        "topology": None,
        "channel": "whatsapp",
    }
    assert len(_captured_bubbles) == 1  # the patient's confirmation still goes out


async def test_a_handoff_that_could_not_commit_logs_no_hand_back(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    async def _not_committed(*_args, **_kwargs):
        raise RuntimeError("handoff_state_not_committed")

    monkeypatch.setattr(workers_ns, "_set_conversation_human_active", _not_committed)
    tenant, _ana, _bruno, _patient, conversation = await _seed(db)

    with pytest.raises(RuntimeError, match="handoff_state_not_committed"):
        await tasks._handle_human_handoff(_reply_ctx(conversation), "could_not_help", tenant, None)

    assert _events(log) == []
    assert _captured_bubbles == []


# --------------------------------------------------------------------------
# TASK-030 P2: set_booking_draft through the resolver (safety fix + switch)
# --------------------------------------------------------------------------


class _SlotAgenda(_StubCalendar):
    """The stub agenda with two free times on every day."""

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        iso = day.date().isoformat()
        return [{"start": f"{iso}T{t}", "end": "", "label": t} for t in ("10:00", "10:40")][
            :max_slots
        ]


@pytest.fixture
def _slot_agenda(monkeypatch: pytest.MonkeyPatch, db) -> _SlotAgenda:
    agenda = _SlotAgenda()

    async def _fake(session, tenant, target):
        return agenda

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _fake)
    # The gate's hold lookups read the same test database.
    monkeypatch.setattr(booking_hold_service, "async_session_factory", db)
    return agenda


async def _switch_on(db, tenant) -> Tenant:
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.initial_flows = {**(row.initial_flows or {}), "ai_draft_v2": True}
        await session.commit()
        await session.refresh(row)
        return row


async def test_unknown_pra_quem_is_asked_first_even_with_the_switch_off(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)  # pra-quem never answered

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps({"t": "Consulta Geral", "p": str(ana.id)}),
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == "awaiting_attendee_choice"
    assert event["supplied"] == ["service", "professional"]
    assert event["accepted"] == ["service", "professional"]
    assert event["fallback"] is None
    assert _captured_bubbles[0].body == ATTENDEE_QUESTION_BODY
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_step == "awaiting_attendee_choice"
    assert conv.flow_selected_type == ATTENDEE_NEXT_BOOK
    assert conv.flow_selected_professional_id is None
    assert (conv.flow_draft["t"], conv.flow_draft["p"]) == ("Consulta Geral", str(ana.id))


async def test_switch_on_lands_a_full_draft_on_the_confirmation_card(
    db, _captured_bubbles, _slot_agenda, log
) -> None:
    """TASK-030 P3: a free time on a switched-on clinic is the express confirmation."""
    tenant, ana, patient, conversation = await _seed_sole(db)
    tenant = await _switch_on(db, tenant)
    day = (datetime.now(ZoneInfo("America/Sao_Paulo")) + timedelta(days=3)).date()
    payload = {"t": "Consulta Geral", "w": "self", "d": day.isoformat(), "h": "10:00"}

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps(payload),
        tenant,
        None,
        _snapshots([ana]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == "awaiting_confirmation"
    assert event["accepted"] == ["service", "for_whom", "day", "time"]
    assert event["topology"] == "sole"
    assert len(_events(log, "conversation_booking_draft_entered")) == 1
    assert [type(bubble).__name__ for bubble in _captured_bubbles] == ["TextBubble", "ButtonBubble"]
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_step == "awaiting_confirmation"
    assert conv.flow_selected_day == day.isoformat()
    assert conv.flow_selected_slot == f"{day.isoformat()}T10:00"
    assert conv.flow_attendee_name == ATTENDEE_SELF


async def test_selection_only_reaches_the_clinic_alert_for_a_doctor_with_no_services(
    db, _captured_bubbles, log, monkeypatch
) -> None:
    alerted: list = []

    async def _fake_alert(reply, result, **_kwargs):
        alerted.append(result.professional_config_gap)

    monkeypatch.setattr(workers_ns, "_handle_professional_config_incomplete", _fake_alert)
    tenant, ana, bruno, patient, conversation = await _seed(
        db, selected=True, attendee=ATTENDEE_SELF
    )
    async with db() as session:
        doctor = await session.get(Professional, ana.id)
        doctor.appointment_types = []
        await session.commit()

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + "{}",
        tenant,
        None,
        [],
        patient.wa_id,
    )

    assert alerted == ["services"]  # it used to be swallowed by the menu fallback
    (event,) = _events(log)
    assert event["landing_step"] == "config_incomplete"
    assert event["fallback"] == "professional_config_incomplete"


async def test_selection_only_keeps_a_typed_convenio(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(
        db, insurance="Amil Dental", attendee=ATTENDEE_SELF
    )
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.collect_insurance = True
        row.insurance_mode = "shared"
        row.insurances = ["Unimed"]
        await session.commit()
        await session.refresh(row)
        tenant = row

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX + "{}",
        tenant,
        None,
        [],
        patient.wa_id,
    )

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_step == "awaiting_professional"
    assert conv.flow_selected_insurance == "Amil Dental"  # the patient's typed answer


async def test_another_tenants_doctor_is_dropped_never_used(db, _captured_bubbles, log) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db, attendee=ATTENDEE_SELF)
    tenant = await _switch_on(db, tenant)
    _other_tenant, foreign_doctor, _b, _p, _c = await _seed(db)

    await tasks._handle_set_booking_draft(
        _reply_ctx(conversation),
        BOOKING_DRAFT_SENTINEL_PREFIX
        + json.dumps({"t": "Consulta Geral", "p": str(foreign_doctor.id)}),
        tenant,
        None,
        [],
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["dropped"] == {"professional": "unknown_professional"}
    assert event["landing_step"] == "awaiting_professional"
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_selected_professional_id is None


def test_the_resolver_speaks_the_hand_back_vocabulary() -> None:
    assert set(bd.DROP_REASONS) <= handback_log.DROP_REASONS
    assert set(bd.FALLBACK_REASONS) <= handback_log.FALLBACK_REASONS
    assert set(bd.FIELD_NAMES) <= handback_log.FIELD_NAMES


@pytest.mark.parametrize(
    "result",
    [
        FlowRouterResult(
            action="reply", flow_state=FlowState.SERVICE_CATALOG, flow_step="awaiting_slot"
        ),
        FlowRouterResult(action="reply", flow_state=FlowState.MENU),
        FlowRouterResult(
            action="calendar_unavailable",
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step="awaiting_day",
        ),
        FlowRouterResult(action="professional_config_incomplete", flow_state=FlowState.IDLE),
    ],
)
def test_the_resolver_names_landings_like_the_hand_back_log(result) -> None:
    assert bd.landing_step(result) == handback_log.landing_of(result)[0]


@pytest.mark.parametrize(
    "flows, expected",
    [
        ({}, False),
        (None, False),
        ({"ai_draft_v2": False}, False),
        ({"ai_draft_v2": "true"}, False),
        ({"ai_draft_v2": True}, True),
    ],
)
def test_the_switch_reads_only_an_explicit_true(flows, expected) -> None:
    assert ai_draft_v2_enabled(SimpleNamespace(initial_flows=flows)) is expected


# --------------------------------------------------------------------------
# TASK-030 P2: the doctor and guided hand-backs read fresh and ask pra-quem
# --------------------------------------------------------------------------


async def test_select_professional_asks_pra_quem_first_when_unknown(
    db, _captured_bubbles, log
) -> None:
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}",
        tenant,
        None,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["source_tool"] == "select_professional"
    assert event["landing_step"] == "awaiting_attendee_choice"
    assert event["accepted"] == ["professional"]
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_draft["p"] == str(ana.id)
    assert conv.flow_selected_professional_id is None


@pytest.mark.parametrize(
    "stored, in_snapshot, landing",
    [
        ("Maria da Silva", None, "awaiting_service"),
        (None, "Maria da Silva", "awaiting_attendee_choice"),
    ],
)
async def test_select_professional_reads_the_attendee_fresh(
    db, _captured_bubbles, log, stored, in_snapshot, landing
) -> None:
    """The turn-start snapshot may be several tool calls old: the row decides."""
    tenant, ana, bruno, patient, conversation = await _seed(db, attendee=stored)
    stale = (
        SimpleNamespace(flow_attendee_name=in_snapshot, flow_selected_insurance=None),
        tenant,
    )

    await tasks._handle_select_professional(
        _reply_ctx(conversation),
        f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}",
        tenant,
        stale,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == landing
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_attendee_name == stored


async def test_guided_booking_asks_pra_quem_first_when_unknown(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    tenant, ana, patient, conversation = await _seed_sole(db)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        tenant,
        _snapshots([ana]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == "awaiting_attendee_choice"
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_draft["t"] == "Consulta Geral"


async def test_guided_booking_recanonicalizes_against_the_fresh_catalog(
    db, _captured_bubbles, _stub_calendar, log
) -> None:
    """The tool proved the name against ITS turn's catalog; the service may be gone now."""
    tenant, ana, patient, conversation = await _seed_sole(db, attendee=ATTENDEE_SELF)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Removida",
        tenant,
        _snapshots([ana]),
        patient.wa_id,
    )

    (event,) = _events(log)
    assert event["landing_step"] == "awaiting_service"  # the service list, never typeless
    assert event["dropped"] == {"service": "not_in_catalog"}


def test_the_guided_sentinel_is_a_protocol_string() -> None:
    assert tasks._is_agent_sentinel(f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral")
