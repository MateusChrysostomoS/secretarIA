"""Tests for the LLM handoff round (PROMPT 3, multi-doctor flow).

Covers the two exception->sentinel tools and their worker-side handlers:
  - `show_main_menu` (ai/tools.py, always available): non-destructive menu
    return — flow fields reset, history/patient untouched (the opposite of
    the dev-only /menu wipe);
  - `select_professional_and_continue` (plugins/multi_professional.py,
    addon-gated): the LLM's hand-back into the deterministic flow at the
    confirmed doctor's greeting + services;
  - run_agent's exception->sentinel mapping (mirrors CalendarUnavailableError);
  - the selected professional's context overlaid onto the system prompt
    (graph._config_with_selected_professional).

DB-backed pieces use the in-memory-sqlite pattern from
test_multi_professional_plugin.py / test_bot_reply_gating.py.
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
from dataclasses import replace  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import graph, tools as ai_tools  # noqa: E402
from secretaria.ai.formatter import SlotsBubble, TextBubble  # noqa: E402
from secretaria.ai.graph import (  # noqa: E402
    MANAGE_APPOINTMENT_SENTINEL_PREFIX,
    SELECT_PROFESSIONAL_SENTINEL_PREFIX,
    SHOW_MAIN_MENU_SENTINEL,
    START_GUIDED_BOOKING_SENTINEL_PREFIX,
    _config_with_selected_professional,
    run_agent,
)
from secretaria.ai.prompts import secretary_system_prompt  # noqa: E402
from secretaria.ai.tools import (  # noqa: E402
    GuidedBookingRequested,
    ManageAppointmentRequested,
    SelectProfessionalRequested,
    ShowMainMenuRequested,
    manage_existing_appointment,
    show_main_menu,
    start_guided_booking,
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
from secretaria.plugins import multi_professional as mp, registry as reg  # noqa: E402
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    BTN_CHOOSE_PROFESSIONAL,
    BTN_CHOOSE_SERVICE,
    STEP_AWAITING_DAY,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_SERVICE,
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_PICK_CANCEL,
    MenuBubble,
)
from secretaria.services.tenant_config import (  # noqa: E402
    RuntimeAppointmentType,
    TenantRuntimeConfig,
)
from secretaria.workers import tasks  # noqa: E402


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
    # ai/tools + the plugin import async_session_factory lazily (patch the
    # source); workers/tasks imports it at module level (patch the attribute).
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(tasks, "async_session_factory", db)
    yield


@contextmanager
def _agent_context(tenant_id):
    tok = ai_tools._tenant_id_ctx.set(tenant_id)
    try:
        yield
    finally:
        ai_tools._tenant_id_ctx.reset(tok)


def _tenant_config(tenant_id) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=tenant_id,
        clinic_name="Clinic",
        greeting_message=None,
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="tenant-calendar",
        google_refresh_token=None,
    )


async def _seed(db, *, flow_state=FlowState.LLM, selected=None, insurance=None):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            initial_flows={"enabled": True, "menu_label": "Como posso ajudar?"},
            appointment_types=[
                {"name": "Consulta Geral", "duration_min": 30, "is_active": True}
            ],
            # A clinic that can actually take a booking. Both professionals
            # below inherit these (their own column is NULL), which is what
            # keeps the day picker reachable: it now refuses to open for a
            # professional with no availability window anywhere, clinic or own.
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


def _snapshots(professionals):
    return [
        SimpleNamespace(
            id=p.id,
            name=p.name,
            specialty=p.specialty,
            about=p.about,
            context_doctor_message=p.context_doctor_message,
            appointment_types=p.appointment_types,
            # Mirrors what `workers/tasks.py` really builds. It has to: the day
            # picker refuses to open for a professional with no configured
            # hours, and a snapshot that omits the column could not be judged.
            business_hours=p.business_hours,
        )
        for p in professionals
    ]


# --------------------------------------------------------------------------
# Tools raise; run_agent maps exception -> sentinel
# --------------------------------------------------------------------------


async def test_show_main_menu_tool_raises():
    with pytest.raises(ShowMainMenuRequested):
        await show_main_menu.ainvoke({})


async def test_run_agent_maps_show_main_menu_to_sentinel(monkeypatch: pytest.MonkeyPatch):
    async def _fake_history(conversation_id):
        return [HumanMessage(content="quero voltar ao menu")]

    async def _raise(messages, conversation_id):
        raise ShowMainMenuRequested()

    monkeypatch.setattr(graph, "_load_history", _fake_history)
    monkeypatch.setattr(graph, "_invoke_agent_with_retry", _raise)
    reply = await run_agent("oi", context={"conversation_id": str(uuid4())})
    assert reply == SHOW_MAIN_MENU_SENTINEL


async def test_run_agent_maps_select_professional_to_sentinel(monkeypatch: pytest.MonkeyPatch):
    professional_id = uuid4()

    async def _fake_history(conversation_id):
        return [HumanMessage(content="pode ser com a Dra. Ana")]

    async def _raise(messages, conversation_id):
        raise SelectProfessionalRequested(professional_id, "Dra. Ana")

    monkeypatch.setattr(graph, "_load_history", _fake_history)
    monkeypatch.setattr(graph, "_invoke_agent_with_retry", _raise)
    reply = await run_agent("oi", context={"conversation_id": str(uuid4())})
    assert reply == f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{professional_id}"


async def test_run_agent_maps_manage_appointment_to_sentinel(monkeypatch: pytest.MonkeyPatch):
    async def _fake_history(conversation_id):
        return [HumanMessage(content="quero cancelar minha consulta")]

    async def _raise(messages, conversation_id):
        raise ManageAppointmentRequested("cancel")

    monkeypatch.setattr(graph, "_load_history", _fake_history)
    monkeypatch.setattr(graph, "_invoke_agent_with_retry", _raise)
    reply = await run_agent("oi", context={"conversation_id": str(uuid4())})
    assert reply == f"{MANAGE_APPOINTMENT_SENTINEL_PREFIX}cancel"


# --------------------------------------------------------------------------
# manage_existing_appointment: normalize action, recoverable error
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_action,canonical",
    [
        ("reschedule", "reschedule"),
        ("Remarcar", "reschedule"),
        ("  REMARCAR  ", "reschedule"),
        ("cancel", "cancel"),
        ("Cancelar", "cancel"),
        (" CANCEL ", "cancel"),
    ],
)
async def test_manage_existing_appointment_tool_raises_canonical_action(raw_action, canonical):
    with pytest.raises(ManageAppointmentRequested) as exc_info:
        await manage_existing_appointment.ainvoke({"action": raw_action})
    assert exc_info.value.action == canonical


async def test_manage_existing_appointment_tool_invalid_action_is_recoverable():
    result = await manage_existing_appointment.ainvoke({"action": "excluir"})
    assert "error" in result
    assert "excluir" in result["error"]


# --------------------------------------------------------------------------
# select_professional_and_continue: resolve-by-name, recoverable error
# --------------------------------------------------------------------------


async def test_select_professional_tool_raises_with_resolved_id(db):
    tenant, ana, _bruno, _patient, _conv = await _seed(db)
    with _agent_context(tenant.id):
        with pytest.raises(SelectProfessionalRequested) as exc_info:
            await mp.select_professional_and_continue.ainvoke(
                {"professional_name": "dra. ana"}  # case-insensitive
            )
    assert exc_info.value.professional_id == ana.id
    assert exc_info.value.professional_name == ana.name


async def test_select_professional_tool_unknown_name_is_recoverable(db):
    tenant, ana, bruno, _patient, _conv = await _seed(db)
    with _agent_context(tenant.id):
        result = await mp.select_professional_and_continue.ainvoke(
            {"professional_name": "Dr. Ghost"}
        )
    assert "error" in result
    assert ana.name in result["error"]
    assert bruno.name in result["error"]


async def test_list_professionals_includes_about_when_set(db):
    tenant, ana, bruno, _patient, _conv = await _seed(db)
    with _agent_context(tenant.id):
        result = await mp.list_professionals.ainvoke({})
    by_name = {p["name"]: p for p in result["professionals"]}
    assert by_name[ana.name]["about"] == "Atendo com foco em prevenção."
    assert "about" not in by_name[bruno.name]
    # context_doctor_message stays persona-only — never listed.
    assert all("context_doctor_message" not in p for p in result["professionals"])


def test_select_professional_tool_is_addon_gated():
    addons_off = {
        "reactivation_pack": False,
        "verified_identity": False,
        "multi_professional": False,
        "multi_unit": False,
        "ehr": False,
        "pix_deposit": False,
        "analytics_bi": False,
        "human_backup_24_7": False,
    }

    def _summary(addons):
        return EntitlementSummary(
            tenant_id=str(uuid4()),
            status="active",
            active=True,
            secretaria_enabled=True,
            plan="bronze",
            secretaria_tier="basico",
            addons=addons,
            limits={},
        )

    entitled = {
        t.name
        for t in reg.agent_tools_for(_summary({**addons_off, "multi_professional": True}))
    }
    not_entitled = {t.name for t in reg.agent_tools_for(_summary(dict(addons_off)))}
    assert mp.select_professional_and_continue.name in entitled
    assert mp.select_professional_and_continue.name not in not_entitled


# --------------------------------------------------------------------------
# Selected-professional context in the system prompt
# --------------------------------------------------------------------------


def test_config_overlay_renders_professional_context_in_prompt():
    config = _tenant_config(uuid4())
    assert "SOBRE O PROFISSIONAL" not in secretary_system_prompt(config)

    selected = SimpleNamespace(
        id=uuid4(),
        specialty="Cardiologia",
        about="Atendo com foco em prevenção.",
        context_doctor_message="Prefere retornos pela manhã.",
    )
    amended = _config_with_selected_professional(config, selected)
    prompt = secretary_system_prompt(amended)
    assert "SOBRE O PROFISSIONAL" in prompt
    assert "Cardiologia" in prompt
    assert "Atendo com foco em prevenção." in prompt
    assert "Prefere retornos pela manhã." in prompt


def test_config_overlay_is_identity_without_selection():
    config = _tenant_config(uuid4())
    assert _config_with_selected_professional(config, None) is config
    assert _config_with_selected_professional(None, SimpleNamespace(id=uuid4())) is None


def test_prompt_teaches_menu_and_professional_tools():
    prompt = secretary_system_prompt(_tenant_config(uuid4()))
    assert "show_main_menu" in prompt
    assert "select_professional_and_continue" in prompt
    assert "list_professionals" in prompt


# --------------------------------------------------------------------------
# Worker handlers: non-destructive menu return + deterministic re-entry
# --------------------------------------------------------------------------


def _reply_ctx(conversation) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_wa_id="5511999999999",
        inbound_body="tanto faz",
    )


@pytest.fixture
def _captured_bubbles(monkeypatch: pytest.MonkeyPatch):
    captured: list = []

    async def _fake_dispatch(reply, bubbles, tenant=None, waba_token=None):
        captured.extend(bubbles)
        return len(bubbles)

    monkeypatch.setattr(tasks, "_dispatch_bubbles", _fake_dispatch)
    return captured


async def test_handle_show_main_menu_resets_flow_without_deleting(db, _captured_bubbles):
    tenant, ana, bruno, patient, conversation = await _seed(
        db, flow_state=FlowState.LLM, selected=True, insurance="Unimed"
    )
    professionals = _snapshots([ana, bruno])

    await tasks._handle_show_main_menu(
        _reply_ctx(conversation), tenant, professionals, patient.wa_id
    )

    assert len(_captured_bubbles) == 1
    menu = _captured_bubbles[0]
    assert isinstance(menu, MenuBubble)
    assert menu.labels == [BTN_CHOOSE_PROFESSIONAL, BTN_CHOOSE_SERVICE, "Outro"]

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MENU
        assert conv.flow_selected_professional_id is None
        assert conv.flow_selected_insurance is None
        # NON-destructive: patient row and history untouched.
        assert await session.get(Patient, patient.id) is not None
        messages = (
            await session.scalars(
                select(Message).where(Message.conversation_id == conversation.id)
            )
        ).all()
        assert len(messages) == 1


async def test_handle_select_professional_reenters_flow_at_doctor(db, _captured_bubbles):
    tenant, ana, bruno, patient, conversation = await _seed(db, flow_state=FlowState.LLM)
    professionals = _snapshots([ana, bruno])
    sentinel = f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{ana.id}"

    await tasks._handle_select_professional(
        _reply_ctx(conversation), sentinel, tenant, None, professionals, patient.wa_id
    )

    # ONE card: the doctor's presentation heads their service list instead of
    # spending a separate WhatsApp message on it.
    (services,) = _captured_bubbles
    assert isinstance(services, SlotsBubble)
    assert services.body == (
        "Dra. Ana\n\nCardiologia\n\nAtendo com foco em prevenção."
        "\n\nQual serviço você gostaria de agendar?"
    )
    assert services.rows[0][1] == "Consulta Geral"  # tenant fallback services

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.SERVICE_CATALOG
        assert conv.flow_step == STEP_AWAITING_SERVICE
        assert conv.flow_selected_professional_id == ana.id


async def test_handle_select_professional_unresolved_falls_back_to_menu(
    db, _captured_bubbles
):
    tenant, ana, bruno, patient, conversation = await _seed(db, flow_state=FlowState.LLM)
    professionals = _snapshots([ana, bruno])
    sentinel = f"{SELECT_PROFESSIONAL_SENTINEL_PREFIX}{uuid4()}"  # not in roster

    await tasks._handle_select_professional(
        _reply_ctx(conversation), sentinel, tenant, None, professionals, patient.wa_id
    )

    assert len(_captured_bubbles) == 1
    assert isinstance(_captured_bubbles[0], MenuBubble)
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MENU


# --------------------------------------------------------------------------
# _handle_manage_appointment: LLM hand-back into the deterministic manage flow
# --------------------------------------------------------------------------


async def _seed_future_appointment(
    db, tenant, patient, *, start_at, appointment_type="Consulta Geral"
):
    async with db() as session:
        appt = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type=appointment_type,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appt)
        await session.commit()
        await session.refresh(appt)
        return appt


async def test_handle_manage_appointment_single_cancel_reaches_confirm_step(
    db, _captured_bubbles
):
    tenant, ana, bruno, patient, conversation = await _seed(db)
    professionals = _snapshots([ana, bruno])
    appt = await _seed_future_appointment(
        db, tenant, patient, start_at=datetime.now(UTC) + timedelta(days=2)
    )

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "cancel", tenant, professionals, patient.wa_id
    )

    assert len(_captured_bubbles) == 1
    bubble = _captured_bubbles[0]
    assert isinstance(bubble, MenuBubble)
    assert bubble.labels == ["Sim", "Não"]

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MANAGE_BOOKING
        assert conv.flow_step == STEP_MANAGE_CANCEL_CONFIRM
        assert conv.flow_managing_appointment_id == appt.id


async def test_handle_manage_appointment_multiple_reaches_disambiguation_pick(
    db, _captured_bubbles
):
    tenant, ana, bruno, patient, conversation = await _seed(db)
    professionals = _snapshots([ana, bruno])
    now = datetime.now(UTC)
    await _seed_future_appointment(db, tenant, patient, start_at=now + timedelta(days=1))
    await _seed_future_appointment(db, tenant, patient, start_at=now + timedelta(days=5))

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "cancel", tenant, professionals, patient.wa_id
    )

    assert len(_captured_bubbles) == 1
    bubble = _captured_bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    assert len(bubble.rows) == 2

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MANAGE_BOOKING
        assert conv.flow_step == STEP_MANAGE_PICK_CANCEL


async def test_handle_manage_appointment_no_appointments_sends_none_reply(
    db, _captured_bubbles
):
    tenant, ana, bruno, patient, conversation = await _seed(db)
    professionals = _snapshots([ana, bruno])

    await tasks._handle_manage_appointment(
        _reply_ctx(conversation), "reschedule", tenant, professionals, patient.wa_id
    )

    assert isinstance(_captured_bubbles[0], TextBubble)
    assert "não tem" in _captured_bubbles[0].body.lower()

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MENU


# --------------------------------------------------------------------------
# start_guided_booking: the OPTIONAL hand-back that resumes the booking itself
# --------------------------------------------------------------------------
#
# The other three hand-backs answer "the patient wants something else". This
# one answers "the free-text conversation got as far as naming the service, and
# buttons should take it from here" — an offer the model makes, never a step it
# owes. What it must not do is skip anything the button flow would have asked.


@contextmanager
def _booking_context(
    tenant_id, *, services=("Consulta Geral",), topology=BOOKING_TOPOLOGY_SOLE
):
    """Agent context with a catalog to validate `appointment_type` against."""
    config = replace(
        _tenant_config(tenant_id),
        appointment_types=[
            RuntimeAppointmentType(name=name, description=None, duration_min=30)
            for name in services
        ],
    )
    tok_tid = ai_tools._tenant_id_ctx.set(tenant_id)
    tok_cfg = ai_tools._tenant_config_ctx.set(config)
    tok_top = ai_tools._booking_topology_ctx.set(topology)
    try:
        yield
    finally:
        ai_tools._booking_topology_ctx.reset(tok_top)
        ai_tools._tenant_config_ctx.reset(tok_cfg)
        ai_tools._tenant_id_ctx.reset(tok_tid)


async def test_start_guided_booking_raises_with_the_catalogs_own_spelling():
    """The service is proven against the catalog BEFORE the hand-back: it lands
    on `Appointment.appointment_type`, which is read downstream as a catalog
    key (the Pix deposit prices a booking by exact name), never as free text."""
    with _booking_context(uuid4()):
        with pytest.raises(GuidedBookingRequested) as excinfo:
            await start_guided_booking.ainvoke({"appointment_type": "consulta geral"})
    assert excinfo.value.appointment_type == "Consulta Geral"


async def test_start_guided_booking_refuses_a_service_the_clinic_does_not_have():
    """Recoverable, not raised: the model can correct itself in the same turn
    instead of parking the patient in a flow scoped to a phantom service."""
    with _booking_context(uuid4(), services=("Consulta Geral", "Retorno")):
        result = await start_guided_booking.ainvoke({"appointment_type": "Botox"})
    assert "Botox" in result["error"]
    assert "Consulta Geral" in result["error"]
    assert "Retorno" in result["error"]


async def test_start_guided_booking_refuses_an_ambiguous_omission():
    with _booking_context(uuid4(), services=("Consulta Geral", "Retorno")):
        result = await start_guided_booking.ainvoke({"appointment_type": ""})
    assert "Consulta Geral" in result["error"]


async def test_start_guided_booking_derives_the_only_service():
    with _booking_context(uuid4()):
        with pytest.raises(GuidedBookingRequested) as excinfo:
            await start_guided_booking.ainvoke({"appointment_type": ""})
    assert excinfo.value.appointment_type == "Consulta Geral"


async def test_start_guided_booking_carries_no_type_when_there_is_no_catalog():
    """A clinic with nothing to prove a service against books typeless, the
    same honest NULL the rest of the codebase settles for."""
    with _booking_context(uuid4(), services=()):
        with pytest.raises(GuidedBookingRequested) as excinfo:
            await start_guided_booking.ainvoke({"appointment_type": "qualquer coisa"})
    assert excinfo.value.appointment_type is None


async def test_start_guided_booking_fails_closed_on_a_multi_professional_turn():
    """Second lock. The day picker it opens would read availability off the
    CLINIC-level agenda, not the chosen doctor's — so a multi-doctor tenant
    never gets this tool (`tasks._flow_handback_tools`), and an invocation that
    arrives anyway is refused before any hand-back."""
    with _booking_context(uuid4(), topology=BOOKING_TOPOLOGY_MULTI):
        result = await start_guided_booking.ainvoke(
            {"appointment_type": "Consulta Geral"}
        )
    assert "error" in result
    assert "show_main_menu" in result["error"]


async def test_run_agent_maps_start_guided_booking_to_sentinel(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_history(conversation_id):
        return [HumanMessage(content="quero marcar uma consulta geral")]

    async def _raise(messages, conversation_id):
        raise GuidedBookingRequested("Consulta Geral")

    monkeypatch.setattr(graph, "_load_history", _fake_history)
    monkeypatch.setattr(graph, "_invoke_agent_with_retry", _raise)
    reply = await run_agent("oi", context={"conversation_id": str(uuid4())})
    assert reply == f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral"


async def test_run_agent_sentinel_survives_a_clinic_with_no_catalog(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_history(conversation_id):
        return [HumanMessage(content="quero marcar")]

    async def _raise(messages, conversation_id):
        raise GuidedBookingRequested(None)

    monkeypatch.setattr(graph, "_load_history", _fake_history)
    monkeypatch.setattr(graph, "_invoke_agent_with_retry", _raise)
    reply = await run_agent("oi", context={"conversation_id": str(uuid4())})
    assert reply == START_GUIDED_BOOKING_SENTINEL_PREFIX


# --------------------------------------------------------------------------
# Which tenants are even offered the tool
# --------------------------------------------------------------------------


def _handback_names(tenant, topology, plugin_tools=()):
    return [
        getattr(t, "name", str(t))
        for t in tasks._flow_handback_tools(tenant, topology, list(plugin_tools))
    ]


@pytest.mark.parametrize("topology", [BOOKING_TOPOLOGY_SOLE, BOOKING_TOPOLOGY_UNKNOWN])
def test_a_tenant_level_tenant_is_offered_the_guided_booking_tool(topology):
    tenant = SimpleNamespace(initial_flows={"enabled": True})
    assert "start_guided_booking" in _handback_names(tenant, topology)
    assert "manage_existing_appointment" in _handback_names(tenant, topology)


def test_a_multi_professional_tenant_is_not():
    """First lock, and the reason the flow never has to guess an agenda: on
    these tenants the way back into the button flow is
    select_professional_and_continue, which re-enters at a doctor whose
    calendar IS resolved."""
    tenant = SimpleNamespace(initial_flows={"enabled": True})
    names = _handback_names(tenant, BOOKING_TOPOLOGY_MULTI)
    assert "start_guided_booking" not in names
    # ...while the manage hand-back, which opens no picker, still is.
    assert "manage_existing_appointment" in names


def test_plugin_tools_are_preserved_alongside_the_handbacks():
    tenant = SimpleNamespace(initial_flows={"enabled": True})
    names = _handback_names(
        tenant, BOOKING_TOPOLOGY_MULTI, [mp.select_professional_and_continue]
    )
    assert names[0] == mp.select_professional_and_continue.name


# --------------------------------------------------------------------------
# The handler: where the conversation actually lands
# --------------------------------------------------------------------------


class _StubCalendar:
    """Every day of the window is free; records the durations it was asked for."""

    def __init__(self):
        self.tzinfo = ZoneInfo("America/Sao_Paulo")
        self.day_scans: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.day_scans.append(slot_minutes)
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(min(days, 3))]


@pytest.fixture
def _stub_calendar(monkeypatch: pytest.MonkeyPatch) -> _StubCalendar:
    """Replace the worker's calendar-resolution seam; no Google, no credentials."""
    calendar = _StubCalendar()

    async def _fake(session, tenant, target):
        return calendar

    monkeypatch.setattr(tasks, "_appointment_calendar", _fake)
    return calendar


async def _seed_sole(db, **kw):
    """The `_seed` clinic reduced to ONE active professional (a SOLE tenant)."""
    tenant, ana, bruno, patient, conversation = await _seed(db, **kw)
    async with db() as session:
        row = await session.get(Professional, bruno.id)
        row.is_active = False
        await session.commit()
    return tenant, ana, patient, conversation


async def test_handle_start_guided_booking_opens_the_day_picker(
    db, _captured_bubbles, _stub_calendar
):
    """The common case: the clinic does not collect convênio, so the hand-back
    lands exactly where the "Sim, agendar" tap would — the tappable day list,
    with the service the LLM resolved carried forward."""
    tenant, ana, patient, conversation = await _seed_sole(db)
    sentinel = f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral"

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation), sentinel, tenant, _snapshots([ana]), patient.wa_id
    )

    (days,) = _captured_bubbles
    assert isinstance(days, SlotsBubble)
    assert len(days.rows) >= 1
    # Slotted on the service's own duration, not a guess.
    assert _stub_calendar.day_scans == [30]

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.SERVICE_CATALOG
        assert conv.flow_step == STEP_AWAITING_DAY
        assert conv.flow_selected_type == "Consulta Geral"


async def test_handle_start_guided_booking_asks_convenio_first_when_configured(
    db, _captured_bubbles, _stub_calendar
):
    """The literal ask was "open the day list". Doing that unconditionally
    would drop the convênio a clinic switched on in the hub, for no reason
    other than this patient arriving through the LLM — so the step the button
    flow would have shown is shown here too, and the calendar is not even read.
    """
    tenant, ana, patient, conversation = await _seed_sole(db)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.collect_insurance = True
        row.insurances = ["Unimed", "Amil"]
        await session.commit()
        await session.refresh(row)
        tenant = row
    sentinel = f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral"

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation), sentinel, tenant, _snapshots([ana]), patient.wa_id
    )

    (insurance,) = _captured_bubbles
    assert isinstance(insurance, SlotsBubble)
    assert [title for _id, title in insurance.rows][:2] == ["Unimed", "Amil"]
    assert _stub_calendar.day_scans == []

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_step == STEP_AWAITING_INSURANCE
        # The service survives the detour — the day picker needs it next.
        assert conv.flow_selected_type == "Consulta Geral"


async def test_handle_start_guided_booking_keeps_an_answered_convenio(
    db, _captured_bubbles, _stub_calendar
):
    """A convênio already on the conversation rides through the day picker
    instead of being cleared by the unconditional field writes."""
    tenant, ana, patient, conversation = await _seed_sole(db, insurance="Unimed")
    sentinel = f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral"

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation), sentinel, tenant, _snapshots([ana]), patient.wa_id
    )

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_step == STEP_AWAITING_DAY
        assert conv.flow_selected_insurance == "Unimed"


async def test_handle_start_guided_booking_without_a_type_still_reaches_the_picker(
    db, _captured_bubbles, _stub_calendar
):
    """The empty suffix (a clinic with no catalog): the booking proceeds
    typeless on the tenant's default duration rather than being dropped."""
    tenant, ana, patient, conversation = await _seed_sole(db)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        START_GUIDED_BOOKING_SENTINEL_PREFIX,
        tenant,
        _snapshots([ana]),
        patient.wa_id,
    )

    assert isinstance(_captured_bubbles[0], SlotsBubble)
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_step == STEP_AWAITING_DAY
        assert conv.flow_selected_type is None


async def test_handle_start_guided_booking_without_a_tenant_is_a_logged_noop(
    db, _captured_bubbles, _stub_calendar
):
    tenant, ana, patient, conversation = await _seed_sole(db)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        None,
        _snapshots([ana]),
        patient.wa_id,
    )

    assert _captured_bubbles == []
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.LLM


async def test_handle_start_guided_booking_hands_off_when_the_agenda_is_unknown(
    db, _captured_bubbles, monkeypatch: pytest.MonkeyPatch
):
    """A selection that no longer resolves yields no calendar. The picker then
    answers `calendar_unavailable` — a human, never a guessed day list."""
    tenant, ana, patient, conversation = await _seed_sole(db)

    async def _no_calendar(session, tenant, target):
        return None

    monkeypatch.setattr(tasks, "_appointment_calendar", _no_calendar)
    handed_off: list = []

    async def _fake_unavailable(reply, redis=None, tenant=None, waba_token=None):
        handed_off.append(reply.conversation_id)

    monkeypatch.setattr(tasks, "_handle_calendar_unavailable", _fake_unavailable)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        tenant,
        _snapshots([ana]),
        patient.wa_id,
    )

    assert handed_off == [conversation.id]
    assert _captured_bubbles == []


async def test_the_next_tap_after_the_handback_actually_advances(
    db, _captured_bubbles, _stub_calendar
):
    """The state the hand-back persists must be one `route()` can CONTINUE from.

    Landing on the right `flow_step` is only half the contract: the row also has
    to carry everything the next step reads. `_apply_flow_result` writes every
    flow_* field unconditionally, so a result that forgot one would CLEAR it and
    the following tap would quietly fall through to the LLM. This taps a real day
    row out of the bubble the hand-back just produced and asserts the flow moves
    on to the slot picker with the service intact.
    """
    from secretaria.services import flow_router

    tenant, ana, patient, conversation = await _seed_sole(db)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        tenant,
        _snapshots([ana]),
        patient.wa_id,
    )

    # Tap the first day exactly as WhatsApp delivers it: "<title> (<payload>)".
    (days,) = _captured_bubbles
    row_id, row_title = days.rows[0]
    assert row_id.startswith("day|")
    tap = f"{row_title} ({row_id.split('|', 1)[1]})"

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)

    class _WithSlots(_StubCalendar):
        async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
            iso = day.date().isoformat()
            return [{"start": f"{iso}T08:00", "end": f"{iso}T08:30", "label": "08:00"}]

    res = await flow_router.route(conv, tenant, _WithSlots(), tap)

    assert res.action == "reply", res.action
    assert res.flow_step == flow_router.STEP_AWAITING_SLOT
    # The service the LLM resolved is still riding along, one step later.
    assert res.flow_selected_type == "Consulta Geral"


async def test_handle_start_guided_booking_slots_on_the_sole_doctors_own_duration(
    db, _captured_bubbles, _stub_calendar
):
    """The clinic shape this repo calls "the tenant that broke": the legacy
    `tenants.appointment_types` column is EMPTY and every service lives on the
    single active professional.

    The deterministic router never sees the raw Tenant row — it sees
    `_flow_tenant_snapshot`, which substitutes the sole professional's own
    catalog. A hand-back that read the ORM row instead would find no services,
    fall back to `tenant.appointment_duration_min`, and offer the patient days
    sliced at the wrong length. 50 != 30 is what makes that visible.
    """
    tenant, ana, patient, conversation = await _seed_sole(db)
    async with db() as session:
        t = await session.get(Tenant, tenant.id)
        t.appointment_types = []  # legacy column empty — everything is per-doctor
        t.appointment_duration_min = 30
        prof = await session.get(Professional, ana.id)
        prof.appointment_types = [
            {"name": "Consulta Longa", "duration_min": 50, "is_active": True}
        ]
        await session.commit()
        await session.refresh(t)
        tenant = t

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Longa",
        tenant,
        _snapshots([ana]),
        patient.wa_id,
    )

    assert isinstance(_captured_bubbles[0], SlotsBubble)
    # The doctor's own 50 minutes, not the clinic's 30-minute default.
    assert _stub_calendar.day_scans == [50]

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_step == STEP_AWAITING_DAY
        assert conv.flow_selected_type == "Consulta Longa"


async def test_handle_start_guided_booking_turns_a_multi_doctor_clinic_away(
    db, _captured_bubbles, _stub_calendar
):
    """Second lock on the topology.

    `_flow_handback_tools` withholds the tool from a multi tenant, but it judges
    by the roster THAT TURN loaded — and a failed roster load maps to UNKNOWN,
    which hands the tool over. The handler re-reads the roster, so a clinic that
    really has 2+ active doctors gets the menu instead of a day picker built on
    the clinic-level agenda, which is nobody's real availability.
    """
    # `_seed` (not `_seed_sole`) leaves BOTH professionals active.
    tenant, ana, bruno, patient, conversation = await _seed(db)

    await tasks._handle_start_guided_booking(
        _reply_ctx(conversation),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Consulta Geral",
        tenant,
        _snapshots([ana, bruno]),
        patient.wa_id,
    )

    # The menu, not a day list — and the calendar was never even read.
    (menu,) = _captured_bubbles
    assert isinstance(menu, MenuBubble)
    assert _stub_calendar.day_scans == []

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MENU
        assert conv.flow_step is None
