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
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

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
    _config_with_selected_professional,
    run_agent,
)
from secretaria.ai.prompts import secretary_system_prompt  # noqa: E402
from secretaria.ai.tools import (  # noqa: E402
    ManageAppointmentRequested,
    SelectProfessionalRequested,
    ShowMainMenuRequested,
    manage_existing_appointment,
    show_main_menu,
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
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    BTN_CHOOSE_PROFESSIONAL,
    BTN_FIND_PROFESSIONAL,
    STEP_AWAITING_SERVICE,
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_PICK_CANCEL,
    MenuBubble,
)
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402
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
        persona_notes=None,
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
    assert menu.labels == [BTN_CHOOSE_PROFESSIONAL, BTN_FIND_PROFESSIONAL, "Outro"]

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

    greeting, services = _captured_bubbles
    assert isinstance(greeting, TextBubble)
    assert greeting.body == "Cardiologia\n\nAtendo com foco em prevenção."
    assert isinstance(services, SlotsBubble)
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
