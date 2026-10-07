"""Decoys: on a v2 turn NO name, contact, event field or other tenant's data reaches the model.

The owner's rule (2026-10-03): the AI never sees a name or any other patient's data - not
pseudonymized, not in any form. This runs one whole v2 turn through `graph.run_agent` with
`invoke_agent` replaced by a script (the seam tests/test_pii_pseudonymization.py uses to see
what the model would receive). The world is full of decoys - unique strings that must never
show up anywhere the model reads:

  * the database: another patient of the same clinic (name, phone, e-mail, appointment), the
    third party our own patient booked for (attendee name, also recorded on the conversation),
    and a second clinic with its own doctor, unit, patient and appointment at the very minute
    of ours;
  * the agenda: a REAL CalendarService over a fake Google client whose events carry the
    decoys in title, description, location, link, attendees, creator and id.

The script renders the system prompt + history the model would get, runs EVERY tool of the
v2 set as the agent holds it (output allowlist inside the pseudonymization guard), renders
the prompt again with the results appended, and also runs each tool RAW (no wrapper) to
check its declared output shape. Nothing here reaches OpenAI or Google.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import json  # noqa: E402
from datetime import UTC, date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402
from pseudonymize_core import has_unresolved_tokens  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import (  # noqa: E402
    graph,
    tools as ai_tools,
)
from secretaria.ai.tool_output import (  # noqa: E402
    undeclared_keys,
    wrap_tools_with_output_allowlist,
)
from secretaria.ai.tools import (  # noqa: E402
    BookingDraftRequested,
    GuidedBookingRequested,
    HumanHandoffRequested,
    ManageAppointmentRequested,
    SelectProfessionalRequested,
    ShowMainMenuRequested,
)
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    Professional,
    Tenant,
    Unit,
)
from secretaria.plugins import multi_professional as mp, registry as reg  # noqa: E402
from secretaria.services import (  # noqa: E402
    booking_hold as booking_hold_service,
    pii_pseudonymization as store,
)
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_SOLE  # noqa: E402
from secretaria.services.calendar import CalendarService  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.llm_context import build_conversation_state  # noqa: E402
from secretaria.services.manage_request import appointment_ref  # noqa: E402
from secretaria.services.patient_context import load_upcoming_appointments  # noqa: E402
from secretaria.services.precheck import HandoffOutcome  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    RuntimeAppointmentType,
    TenantRuntimeConfig,
)
from secretaria.workers.shared.llm_context import (  # noqa: E402
    _appointment_context_text,
    _flow_handback_tools,
)

TZ = ZoneInfo("America/Sao_Paulo")

# Every string below is unique and must never reach the model, in any form.
OTHER_PATIENT_NAME = "Zuleica Decoyana"
OTHER_PATIENT_PHONE = "5511987650001"
OTHER_PATIENT_EMAIL = "zuleica.decoyana@example.com"
OWN_ATTENDEE_NAME = "Hermengarda Decoyosa"
OWN_EVENT_ID = "evt-decoy-own-71c2"
OTHER_EVENT_ID = "evt-decoy-other-55a9"
EVENT_DESCRIPTION = "Notas DECOY-DESC-3381"
EVENT_LOCATION = "Sala DECOY-LOC-2210"
EVENT_LINK = "https://calendar.example/DECOY-LINK-9047"
EVENT_ATTENDEE_EMAIL = "decoy.attendee.6612@example.com"
TENANT_B_CLINIC = "Clinica Decoy-B 4471"
TENANT_B_DOCTOR = "Dr. Decoy-B Teodoro"
TENANT_B_UNIT = "Unidade Decoy-B 8830"
TENANT_B_PATIENT = "Teodora Decoy-B"
TENANT_B_PHONE = "5511987650002"
TENANT_B_EVENT_ID = "evt-decoy-b-1029"
CANARIES = (
    OTHER_PATIENT_NAME,
    OTHER_PATIENT_PHONE,
    OTHER_PATIENT_EMAIL,
    OWN_ATTENDEE_NAME,
    OWN_EVENT_ID,
    OTHER_EVENT_ID,
    EVENT_DESCRIPTION,
    EVENT_LOCATION,
    EVENT_LINK,
    EVENT_ATTENDEE_EMAIL,
    TENANT_B_CLINIC,
    TENANT_B_DOCTOR,
    TENANT_B_UNIT,
    TENANT_B_PATIENT,
    TENANT_B_PHONE,
    TENANT_B_EVENT_ID,
)
_HANDBACKS = (
    BookingDraftRequested,
    GuidedBookingRequested,
    HumanHandoffRequested,
    ManageAppointmentRequested,
    SelectProfessionalRequested,
    ShowMainMenuRequested,
)
ALL_ADDONS = EntitlementSummary(
    tenant_id=str(uuid4()),
    status="active",
    active=True,
    secretaria_enabled=True,
    plan="bronze",
    secretaria_tier="basico",
    addons={"multi_professional": True, "multi_unit": True},
    limits={},
)
_ALL_WEEK = {
    day: [{"start": "08:00", "end": "12:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


def _leaks(text: str) -> list[str]:
    folded = text.casefold()
    return [canary for canary in CANARIES if canary.casefold() in folded]


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _day(offset: int) -> date:
    return datetime.now(TZ).date() + timedelta(days=offset)


def _at(offset: int, hour: int) -> datetime:
    day = _day(offset)
    return datetime(day.year, day.month, day.day, hour, tzinfo=TZ)


# --------------------------------------------------------------------------
# The decoy world
# --------------------------------------------------------------------------


class _GoogleEvents:
    def __init__(self, items):
        self._items = items

    def list(self, **_kwargs):
        items = self._items

        class _Request:
            def execute(self):
                return {"items": items}

        return _Request()


class _GoogleService:
    def __init__(self, items):
        self._events = _GoogleEvents(items)

    def events(self):
        return self._events


def _google_event(event_id: str, title: str, start: datetime) -> dict:
    return {
        "id": event_id,
        "summary": title,
        "description": EVENT_DESCRIPTION,
        "location": EVENT_LOCATION,
        "htmlLink": EVENT_LINK,
        "attendees": [{"email": EVENT_ATTENDEE_EMAIL, "displayName": OTHER_PATIENT_NAME}],
        "creator": {"email": OTHER_PATIENT_EMAIL},
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
    }


def _decoy_calendar(monkeypatch) -> CalendarService:
    """A REAL CalendarService (its own slot walk) over a fake Google client full of decoys."""
    settings = SimpleNamespace(
        CLINIC_TIMEZONE="America/Sao_Paulo",
        GOOGLE_CALENDAR_ID="primary",
        GOOGLE_CLIENT_ID="id",
        GOOGLE_CLIENT_SECRET="secret",
        GOOGLE_REFRESH_TOKEN="token",
    )
    calendar = CalendarService(settings=settings)
    calendar._business_hours = _ALL_WEEK
    calendar._default_slot_minutes = 30
    items = [
        _google_event(OTHER_EVENT_ID, f"Consulta - {OTHER_PATIENT_NAME}", _at(2, 10)),
        _google_event(OWN_EVENT_ID, f"Consulta - {OWN_ATTENDEE_NAME}", _at(3, 10)),
    ]
    monkeypatch.setattr(calendar, "_service", _GoogleService(items))
    return calendar


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_database, "async_session_factory", maker)
    monkeypatch.setattr(store, "async_session_factory", maker)
    monkeypatch.setattr(booking_hold_service, "async_session_factory", maker)
    yield maker
    await engine.dispose()


def _appointment(tenant_id, patient_id, conversation_id, event_id, start, **extra):
    return Appointment(
        tenant_id=tenant_id,
        patient_id=patient_id,
        conversation_id=conversation_id,
        google_event_id=event_id,
        appointment_type="Consulta",
        start_at=start.astimezone(UTC),
        end_at=(start + timedelta(minutes=30)).astimezone(UTC),
        status=AppointmentStatus.SCHEDULED,
        **extra,
    )


async def _seed(db):
    async with db() as session:
        clinic = Tenant(
            id=uuid4(),
            clinic_name="Clinica Aurora",
            phone_number_id=str(uuid4())[:12],
            timezone="America/Sao_Paulo",
            appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
            initial_flows={"ai_draft_v2": True},
        )
        other_clinic = Tenant(
            id=uuid4(), clinic_name=TENANT_B_CLINIC, phone_number_id=str(uuid4())[:12]
        )
        session.add_all([clinic, other_clinic])
        await session.flush()
        doctor = Professional(
            tenant_id=clinic.id, name="Dra. Ana Souza", google_calendar_id=None, is_active=True
        )
        session.add_all(
            [
                doctor,
                Professional(tenant_id=other_clinic.id, name=TENANT_B_DOCTOR, is_active=True),
                Unit(tenant_id=clinic.id, name="Unidade Centro", address="Rua Um, 1"),
                Unit(tenant_id=other_clinic.id, name=TENANT_B_UNIT, address="Rua B, 2"),
            ]
        )
        patient = Patient(tenant_id=clinic.id, wa_id="5511900000001", name="Paciente Proprio")
        other = Patient(
            tenant_id=clinic.id,
            wa_id=OTHER_PATIENT_PHONE,
            name=OTHER_PATIENT_NAME,
            email=OTHER_PATIENT_EMAIL,
        )
        foreign = Patient(tenant_id=other_clinic.id, wa_id=TENANT_B_PHONE, name=TENANT_B_PATIENT)
        session.add_all([patient, other, foreign])
        await session.flush()
        conversation = Conversation(
            tenant_id=clinic.id,
            patient_id=patient.id,
            # Our patient already answered "pra quem": the third party's name is on record.
            flow_attendee_name=OWN_ATTENDEE_NAME,
        )
        other_conversation = Conversation(tenant_id=clinic.id, patient_id=other.id)
        foreign_conversation = Conversation(tenant_id=other_clinic.id, patient_id=foreign.id)
        session.add_all([conversation, other_conversation, foreign_conversation])
        await session.flush()
        session.add_all(
            [
                _appointment(
                    clinic.id,
                    patient.id,
                    conversation.id,
                    OWN_EVENT_ID,
                    _at(3, 10),
                    professional_id=doctor.id,
                    attendee_name=OWN_ATTENDEE_NAME,
                ),
                _appointment(
                    clinic.id, other.id, other_conversation.id, OTHER_EVENT_ID, _at(2, 10)
                ),
                _appointment(
                    other_clinic.id,
                    foreign.id,
                    foreign_conversation.id,
                    TENANT_B_EVENT_ID,
                    _at(3, 10),
                ),
            ]
        )
        await session.commit()
        return SimpleNamespace(
            clinic=clinic,
            doctor=doctor,
            conversation_id=conversation.id,
            patient_id=patient.id,
        )


def _config(clinic) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=clinic.id,
        clinic_name=clinic.clinic_name,
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[
            RuntimeAppointmentType(name="Consulta", description=None, duration_min=30)
        ],
        business_hours=_ALL_WEEK,
        google_calendar_id="primary",
        google_refresh_token=None,
    )


def _scripted_calls(own_ref: str, other_ref: str) -> dict[str, list[dict]]:
    """What the script makes the "model" call. Every v2 tool must have an entry."""
    first, last = _day(2).isoformat(), _day(3).isoformat()
    return {
        "get_availability": [{"day_from": first, "day_to": last}],
        "list_patient_appointments": [{}],
        "get_service_info": [{"service_name": "Consulta"}, {"service_name": OTHER_PATIENT_NAME}],
        "iniciar_pre_consulta": [{}],
        "show_main_menu": [{}],
        "list_professionals": [{}],
        "list_units": [{}],
        "select_professional_and_continue": [{"professional_name": OTHER_PATIENT_NAME}],
        "start_guided_booking": [
            {"appointment_type": "Consulta"},
            {"appointment_type": OTHER_PATIENT_NAME},
        ],
        "set_booking_draft": [{"service": "Consulta", "day": last, "time": "09:00"}],
        "manage_existing_appointment": [{"action": "cancel", "appointment": own_ref}],
        "request_human_handoff": [{"reason": "nenhum"}],
        "create_event": [
            {"start": f"{last}T09:00", "service": "Consulta"},
            {"start": "amanhã às 10"},
        ],
        "cancel_event": [
            {"appointment": own_ref},
            {"appointment": other_ref},
            # A decoy IN the argument: a refusal must not echo it back.
            {"appointment": OTHER_EVENT_ID},
        ],
    }


@pytest_asyncio.fixture(params=["whatsapp", "brain_message"])
async def turn(db, monkeypatch, request):
    """Run one v2 turn over the decoy world; return everything the model would have read."""
    world = await _seed(db)
    calendar = _decoy_calendar(monkeypatch)
    monkeypatch.setattr(
        graph,
        "CalendarService",
        SimpleNamespace(from_tenant_config=lambda _config: calendar),
    )

    async def _no_precheck(_tenant_id, _phone):
        return SimpleNamespace(outcome=HandoffOutcome.NOT_ENTITLED)

    monkeypatch.setattr(ai_tools, "request_precheck_handoff", _no_precheck)

    async def _history(_conversation_id):
        return [HumanMessage(content="Oi, quero ver horários e cancelar minha consulta")]

    monkeypatch.setattr(graph, "_load_history", _history)
    graph._AGENTS.clear()
    monkeypatch.setattr(
        graph,
        "create_react_agent",
        lambda model, tools, prompt: SimpleNamespace(tools=list(tools), prompt=prompt),
    )

    async with db() as session:
        clinic = await session.get(Tenant, world.clinic.id)
        conversation = await session.get(Conversation, world.conversation_id)
        doctor = await session.get(Professional, world.doctor.id)
        upcoming = await load_upcoming_appointments(session, clinic.id, world.patient_id)
        state = build_conversation_state(conversation, clinic, [doctor])
    config = _config(clinic)
    appointment_context = _appointment_context_text(
        upcoming,
        "America/Sao_Paulo",
        {str(doctor.id): doctor.name},
        config.appointment_types,
        with_refs=True,
    )
    extras = _flow_handback_tools(clinic, BOOKING_TOPOLOGY_SOLE, reg.agent_tools_for(ALL_ADDONS))
    own_ref = appointment_ref(_at(3, 10), TZ)
    calls = _scripted_calls(own_ref, appointment_ref(_at(2, 10), TZ))
    seen = SimpleNamespace(
        own_ref=own_ref, prompts=[], outputs=[], raw_outputs=[], tool_names=set(), calls=calls
    )

    async def _run(tool, args):
        try:
            return await tool.ainvoke(args)
        except _HANDBACKS as exc:
            return {"handed_back": type(exc).__name__}

    async def _scripted(messages):
        agent = graph.build_agent(
            graph._extra_tools_ctx.get(),
            ai_tools._booking_topology_ctx.get(),
            toolset_v2=ai_tools._ai_toolset_v2_ctx.get(),
        )
        raw = {
            t.name: t
            for t in graph.effective_tools(
                ai_tools._booking_topology_ctx.get(),
                graph._extra_tools_ctx.get(),
                toolset_v2=True,
            )
        }
        seen.prompts.append(agent.prompt({"messages": list(messages)}))
        replies = []
        for tool in agent.tools:
            seen.tool_names.add(tool.name)
            for i, args in enumerate(calls.get(tool.name, [])):
                out = await _run(tool, args)
                seen.outputs.append((tool.name, out))
                seen.raw_outputs.append((tool.name, await _run(raw[tool.name], args)))
                replies.append(ToolMessage(content=_text(out), tool_call_id=f"{tool.name}-{i}"))
        seen.prompts.append(agent.prompt({"messages": [*messages, *replies]}))
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _scripted)
    reply = await graph.run_agent(
        "Oi, quero ver horários e cancelar minha consulta",
        context={"conversation_id": str(world.conversation_id), "channel": request.param},
        tenant_config=config,
        extra_tools=extras,
        appointment_context=appointment_context,
        booking_topology=BOOKING_TOPOLOGY_SOLE,
        conversation_state=state,
        toolset_v2=True,
    )
    assert reply == "ok"
    graph._AGENTS.clear()
    return seen


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def test_the_canary_check_itself_catches_a_leak():
    """A check that never fires proves nothing: it must catch a decoy in any case and a token."""
    assert _leaks(f"Consulta - {OTHER_PATIENT_NAME.upper()}") == [OTHER_PATIENT_NAME]
    assert _leaks(_text({"id": TENANT_B_EVENT_ID})) == [TENANT_B_EVENT_ID]
    assert _leaks("Dra. Ana Souza, Consulta, 08:00") == []
    assert has_unresolved_tokens("Consulta - [PACIENTE_a1b2]")


async def test_every_v2_tool_was_run_against_the_decoys(turn):
    ran = {name for name, _ in turn.outputs}
    # Every tool the v2 agent holds has a scripted call, and ran.
    assert turn.tool_names <= set(turn.calls), turn.tool_names - set(turn.calls)
    assert ran == turn.tool_names
    assert {"get_availability", "create_event", "cancel_event", "list_patient_appointments"} <= ran
    # The reads really read, and the staging tools really staged or refused.
    (availability,) = [out for name, out in turn.outputs if name == "get_availability"]
    assert availability["windows"]
    (listing,) = [out for name, out in turn.outputs if name == "list_patient_appointments"]
    assert listing["count"] == 1
    own, other, event_id = [out for name, out in turn.outputs if name == "cancel_event"]
    assert own == {"handed_back": "ManageAppointmentRequested"}
    assert set(other) == {"error"} and set(event_id) == {"error"}
    staged, garbled = [out for name, out in turn.outputs if name == "create_event"]
    assert staged == {"handed_back": "BookingDraftRequested"}
    assert set(garbled) == {"error"}
    # The prompt did carry the appointment block (with its reference) and the state.
    system = str(turn.prompts[0][0].content)
    assert turn.own_ref in system


async def test_no_decoy_reaches_the_model_on_a_v2_turn(turn):
    rendered = [str(message.content) for prompt in turn.prompts for message in prompt]
    outputs = [_text(out) for _name, out in turn.outputs]
    for text in [*rendered, *outputs]:
        assert _leaks(text) == [], text[:300]
        # "Not even pseudonymized": nothing was there for the guard to mask.
        assert not has_unresolved_tokens(text), text[:300]


async def test_every_raw_v2_output_stays_inside_its_allowlist(turn):
    """The tools THEMSELVES stay inside their declarations - the wrapper is a backstop, not
    the reason the decoys stay out."""
    for name, out in turn.raw_outputs:
        if isinstance(out, dict) and "handed_back" in out:
            continue  # a hand-back ends the turn: the model never reads anything from it
        assert undeclared_keys(name, out) == [], (name, out)
        assert _leaks(_text(out)) == [], name


@pytest.mark.parametrize("v2", [False, True], ids=["legacy", "v2"])
@pytest.mark.parametrize(
    "tool,args",
    [
        (ai_tools.start_guided_booking, {"appointment_type": OTHER_PATIENT_NAME}),
        (mp.select_professional_and_continue, {"professional_name": OTHER_PATIENT_NAME}),
    ],
    ids=["guided_service", "professional_selection"],
)
async def test_retained_handback_errors_omit_unrecognized_input_only_on_v2(db, tool, args, v2):
    world = await _seed(db)
    tokens = [
        (var, var.set(value))
        for var, value in (
            (ai_tools._tenant_id_ctx, world.clinic.id),
            (ai_tools._tenant_config_ctx, _config(world.clinic)),
            (ai_tools._ai_toolset_v2_ctx, v2),
            (ai_tools._booking_topology_ctx, BOOKING_TOPOLOGY_SOLE),
        )
    ]
    try:
        (wrapped,) = wrap_tools_with_output_allowlist([tool])
        result = await wrapped.ainvoke(args)
        assert set(result) == {"error"}
        assert (OTHER_PATIENT_NAME in result["error"]) is (not v2)
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
