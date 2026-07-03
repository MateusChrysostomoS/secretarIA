"""Tests for ai/tools.py:iniciar_pre_consulta — the precheck hand-off agent tool.

Mirrors test_multi_professional_plugin.py's pattern: `request_precheck_handoff`
is monkeypatched (no real network call), ContextVars are set/reset manually
exactly like ai/graph.py:run_agent does, and an in-memory sqlite DB backs the
conversation/patient phone resolution.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from contextlib import contextmanager  # noqa: E402
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
from secretaria.models import Conversation, Patient, Tenant  # noqa: E402
from secretaria.services.precheck import HandoffOutcome, HandoffResult  # noqa: E402


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
def _fakes(monkeypatch: pytest.MonkeyPatch, db):
    # ai/tools.py's _resolve_patient_phone imports async_session_factory
    # lazily FROM core.database, so the source module attribute must be
    # patched (see test_multi_professional_plugin.py's identical note).
    monkeypatch.setattr(core_database, "async_session_factory", db)
    yield


@contextmanager
def _agent_context(tenant_id=None, conversation_id=None):
    tok_tid = ai_tools._tenant_id_ctx.set(tenant_id)
    tok_conv = ai_tools._conversation_id_ctx.set(conversation_id)
    try:
        yield
    finally:
        ai_tools._tenant_id_ctx.reset(tok_tid)
        ai_tools._conversation_id_ctx.reset(tok_conv)


async def _seed_tenant_patient_conversation(db, wa_id: str = "5511999999999"):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()
        patient = Patient(tenant_id=tenant.id, wa_id=wa_id, name="Maria")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.commit()
        for obj in (tenant, patient, conversation):
            await session.refresh(obj)
        return tenant, patient, conversation


def _set_precheck_number(monkeypatch: pytest.MonkeyPatch, number: str = "551140028922") -> None:
    monkeypatch.setenv("PRECHECK_WHATSAPP_NUMBER", number)
    monkeypatch.setenv("PRECHECK_HANDOFF_PREFILL", "Olá! Quero fazer minha pré-consulta.")
    from secretaria.config import get_settings

    get_settings.cache_clear()


# --------------------------------------------------------------------------
# Not configured for this clinic
# --------------------------------------------------------------------------


async def test_no_precheck_number_configured_returns_unavailable_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECHECK_WHATSAPP_NUMBER", "")
    from secretaria.config import get_settings

    get_settings.cache_clear()

    result = await ai_tools.iniciar_pre_consulta.ainvoke({})
    assert "não está disponível" in result


async def test_no_tenant_context_returns_unavailable_text_without_calling_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_precheck_number(monkeypatch)
    called = False

    async def _fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        return HandoffResult(HandoffOutcome.SEEDED)

    monkeypatch.setattr(ai_tools, "request_precheck_handoff", _fail_if_called)

    with _agent_context(tenant_id=None):
        result = await ai_tools.iniciar_pre_consulta.ainvoke({})
    assert "não está disponível" in result
    assert called is False


# --------------------------------------------------------------------------
# Success paths: SEEDED / ALREADY_ACTIVE
# --------------------------------------------------------------------------


async def test_seeded_returns_link_with_number_and_urlencoded_prefill(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    _set_precheck_number(monkeypatch, number="551140028922")
    tenant, _patient, conversation = await _seed_tenant_patient_conversation(db)

    async def _fake_handoff(tenant_id, phone_number):
        assert tenant_id == tenant.id
        assert phone_number == "5511999999999"
        return HandoffResult(HandoffOutcome.SEEDED)

    monkeypatch.setattr(ai_tools, "request_precheck_handoff", _fake_handoff)

    with _agent_context(tenant_id=tenant.id, conversation_id=conversation.id):
        result = await ai_tools.iniciar_pre_consulta.ainvoke({})

    assert "https://wa.me/551140028922" in result
    assert "text=Ol%C3%A1%21%20Quero%20fazer%20minha%20pr%C3%A9-consulta." in result


async def test_already_active_also_returns_success_link(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    _set_precheck_number(monkeypatch)
    tenant, _patient, conversation = await _seed_tenant_patient_conversation(db)

    async def _fake_handoff(tenant_id, phone_number):
        return HandoffResult(HandoffOutcome.ALREADY_ACTIVE)

    monkeypatch.setattr(ai_tools, "request_precheck_handoff", _fake_handoff)

    with _agent_context(tenant_id=tenant.id, conversation_id=conversation.id):
        result = await ai_tools.iniciar_pre_consulta.ainvoke({})

    assert "https://wa.me/" in result


# --------------------------------------------------------------------------
# Failure paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", [HandoffOutcome.NOT_ENTITLED, HandoffOutcome.NO_CLINIC])
async def test_not_entitled_and_no_clinic_return_not_available_text(
    monkeypatch: pytest.MonkeyPatch, db, outcome
) -> None:
    _set_precheck_number(monkeypatch)
    tenant, _patient, conversation = await _seed_tenant_patient_conversation(db)

    async def _fake_handoff(tenant_id, phone_number):
        return HandoffResult(outcome)

    monkeypatch.setattr(ai_tools, "request_precheck_handoff", _fake_handoff)

    with _agent_context(tenant_id=tenant.id, conversation_id=conversation.id):
        result = await ai_tools.iniciar_pre_consulta.ainvoke({})

    assert "não está disponível" in result
    assert "wa.me" not in result


async def test_conflict_tells_patient_to_continue_on_precheck_number(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    _set_precheck_number(monkeypatch)
    tenant, _patient, conversation = await _seed_tenant_patient_conversation(db)

    async def _fake_handoff(tenant_id, phone_number):
        return HandoffResult(HandoffOutcome.CONFLICT)

    monkeypatch.setattr(ai_tools, "request_precheck_handoff", _fake_handoff)

    with _agent_context(tenant_id=tenant.id, conversation_id=conversation.id):
        result = await ai_tools.iniciar_pre_consulta.ainvoke({})

    assert "já tem uma pré-consulta em andamento" in result


async def test_unavailable_outcome_returns_temporary_failure_text(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    _set_precheck_number(monkeypatch)
    tenant, _patient, conversation = await _seed_tenant_patient_conversation(db)

    async def _fake_handoff(tenant_id, phone_number):
        return HandoffResult(HandoffOutcome.UNAVAILABLE)

    monkeypatch.setattr(ai_tools, "request_precheck_handoff", _fake_handoff)

    with _agent_context(tenant_id=tenant.id, conversation_id=conversation.id):
        result = await ai_tools.iniciar_pre_consulta.ainvoke({})

    assert "instabilidade temporária" in result


async def test_no_resolvable_patient_phone_returns_temporary_failure_without_calling_service(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    """Tenant context set but no conversation (or no patient on it) -> can't
    resolve a phone number to hand off; fail without ever calling brain-api."""
    _set_precheck_number(monkeypatch)
    tenant = uuid4()
    called = False

    async def _fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        return HandoffResult(HandoffOutcome.SEEDED)

    monkeypatch.setattr(ai_tools, "request_precheck_handoff", _fail_if_called)

    with _agent_context(tenant_id=tenant, conversation_id=None):
        result = await ai_tools.iniciar_pre_consulta.ainvoke({})

    assert "instabilidade temporária" in result
    assert called is False
