"""TASK-072 (live battery 2026-10-29): what the agent is told about the patient and the clinic.

Rendering only for the prompt blocks (v1 and v2), the worker-side builders, and the
`save_patient_name` tool over an in-memory database.
"""

import os
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import prompts_v2, tools as ai_tools  # noqa: E402
from secretaria.ai.prompts import secretary_system_prompt  # noqa: E402
from secretaria.ai.prompts_v2 import secretary_system_prompt_v2  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Conversation, Patient, Tenant  # noqa: E402
from secretaria.services.llm_context import (  # noqa: E402
    build_clinic_features,
    build_patient_identity,
)
from secretaria.services.tenant_config import (  # noqa: E402
    ClinicFeatures,
    PatientIdentityStatus,
    TenantRuntimeConfig,
)

V2_NAMES = frozenset(
    {
        "set_booking_draft",
        "show_main_menu",
        "get_service_info",
        "offer_human_handoff",
        "save_patient_name",
    }
)


@pytest.fixture(autouse=True)
def _fixed_clinic_day(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(prompts_v2, "_clinic_today", lambda _tz: date(2026, 10, 8))


def _config(**overrides) -> TenantRuntimeConfig:
    fields = dict(
        tenant_id=uuid4(),
        clinic_name="Clínica Teste",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="primary",
        google_refresh_token=None,
    )
    fields.update(overrides)
    return TenantRuntimeConfig(**fields)


def _both(config: TenantRuntimeConfig, names=V2_NAMES) -> dict[str, str]:
    return {
        "v1": secretary_system_prompt(config),
        "v2": secretary_system_prompt_v2(config, tool_names=names),
    }


# --------------------------------------------------------------------------
# Problem 2: several questions in one message
# --------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_a_multi_question_message_is_answered_in_one_reply(version):
    prompt = _both(_config())[version]
    assert "VÁRIAS perguntas" in prompt
    assert "NA MESMA resposta" in prompt
    assert "Só para a parte que você não sabe" in prompt


# --------------------------------------------------------------------------
# Problem 3: what is ON / OFF, and no "reserved for another patient"
# --------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_a_clinic_without_the_deposit_says_so_and_bans_the_subject(version):
    config = _config(clinic_features=ClinicFeatures(deposit_enabled=False, insurance_plan_count=0))
    prompt = _both(config)[version]
    assert "RECURSOS DESTA CLÍNICA" in prompt
    assert "- Sinal (Pix antecipado): DESLIGADO" in prompt
    assert "Nunca mencione, ofereça nem prometa um recurso DESLIGADO" in prompt
    assert "nunca fale de sinal, Pix, pagamento antecipado" in prompt
    assert "nenhum convênio cadastrado (atendimento particular)" in prompt


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_a_clinic_with_the_deposit_and_plans_says_on(version):
    config = _config(clinic_features=ClinicFeatures(deposit_enabled=True, insurance_plan_count=3))
    prompt = _both(config)[version]
    assert "- Sinal (Pix antecipado): LIGADO" in prompt
    assert "aceita convênios (3 cadastrados" in prompt
    assert "- Marcar, remarcar e cancelar pelo chat: LIGADO" in prompt


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_no_block_without_the_turn_facts(version):
    prompt = _both(_config())[version]
    assert "RECURSOS DESTA CLÍNICA" not in prompt
    assert "- Nome:" not in prompt


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_an_unavailable_time_is_never_reserved_for_someone(version):
    prompt = _both(_config())[version]
    assert "só está indisponível" in prompt
    assert '"reservado", "guardado" ou "aguardando"' in prompt


def test_the_availability_tool_description_does_not_say_other_patients_reserve():
    from secretaria.ai.availability_tool import get_availability

    assert "estão reservando" not in get_availability.description


# --------------------------------------------------------------------------
# Problem 1: name and e-mail
# --------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_a_missing_name_is_stated_and_asked_for(version):
    config = _config(patient_identity=PatientIdentityStatus(name_known=False, email_known=True))
    prompt = _both(config)[version]
    assert "DADOS DO PACIENTE" in prompt
    assert "- Nome: ainda NÃO temos." in prompt
    assert "- E-mail: já temos." in prompt
    assert "peça com naturalidade" in prompt
    assert "save_patient_name" in prompt


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_a_missing_email_is_stated_and_the_agent_does_not_pretend_to_save_it(version):
    config = _config(patient_identity=PatientIdentityStatus(name_known=True, email_known=False))
    prompt = _both(config)[version]
    assert "- E-mail: ainda NÃO temos." in prompt
    assert "Você não grava e-mail por aqui" in prompt


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_everything_known_is_never_asked_again(version):
    config = _config(patient_identity=PatientIdentityStatus(name_known=True, email_known=True))
    prompt = _both(config)[version]
    assert "Não peça esses dados de novo." in prompt
    assert "peça com naturalidade" not in prompt


def test_v2_does_not_name_the_tool_a_turn_does_not_have():
    config = _config(patient_identity=PatientIdentityStatus(name_known=False, email_known=False))
    prompt = secretary_system_prompt_v2(config, tool_names=V2_NAMES - {"save_patient_name"})
    assert "save_patient_name" not in prompt
    assert "ainda NÃO temos" in prompt


def test_v1_in_an_edit_turn_does_not_name_the_tool():
    config = _config(patient_identity=PatientIdentityStatus(name_known=False, email_known=True))
    assert "save_patient_name" not in secretary_system_prompt(config, name_tool=False)


def test_the_identity_block_carries_no_value():
    config = _config(patient_identity=build_patient_identity("Maria Silva", "maria@x.com"))
    prompt = _both(config)["v2"]
    assert "Maria" not in prompt
    assert "maria@x.com" not in prompt


# --------------------------------------------------------------------------
# Worker-side builders
# --------------------------------------------------------------------------


def test_build_patient_identity_only_reports_booleans():
    assert build_patient_identity(None, None) == PatientIdentityStatus(False, False)
    assert build_patient_identity("  ", "") == PatientIdentityStatus(False, False)
    assert build_patient_identity("Ana", None) == PatientIdentityStatus(True, False)
    assert build_patient_identity(None, "a@b.co") == PatientIdentityStatus(False, True)
    # The Portal proves an e-mail inside the chat even before a booking copies it locally.
    assert build_patient_identity("Ana", None, email_on_account=True).email_known is True


def test_build_clinic_features_reads_the_deposit_switch_and_the_plans():
    tenant = SimpleNamespace(
        pix_deposit_enabled=False, insurances=[], initial_flows={}, insurance_plans=None
    )
    assert build_clinic_features(tenant) == ClinicFeatures(
        deposit_enabled=False, insurance_plan_count=0, online_booking=True
    )
    tenant.pix_deposit_enabled = True
    snapshot = SimpleNamespace(insurance_plans=[{"id": None, "name": "Unimed"}], insurances=[])
    assert build_clinic_features(tenant, snapshot) == ClinicFeatures(
        deposit_enabled=True, insurance_plan_count=1, online_booking=True
    )
    assert build_clinic_features(None) is None


async def test_run_agent_threads_the_turn_facts_into_the_prompt(monkeypatch):
    from secretaria.ai import graph

    seen: dict[str, str] = {}

    async def _no_history(_cid):  # noqa: ANN001
        return []

    async def _capture(_messages):  # noqa: ANN001
        content = graph._prompt_with_today({"messages": []})[0].content
        seen["prompt"] = (
            content if isinstance(content, str) else "".join(b["text"] for b in content)
        )
        return "ok"

    monkeypatch.setattr(graph, "_load_history", _no_history)
    monkeypatch.setattr(graph, "invoke_agent", _capture)
    await graph.run_agent(
        "oi",
        context={"conversation_id": "11111111-2222-3333-4444-555555555555"},
        tenant_config=_config(),
        patient_identity=PatientIdentityStatus(name_known=False, email_known=True),
        clinic_features=ClinicFeatures(deposit_enabled=False, insurance_plan_count=0),
    )
    assert "- Sinal (Pix antecipado): DESLIGADO" in seen["prompt"]
    assert "- Nome: ainda NÃO temos." in seen["prompt"]


# --------------------------------------------------------------------------
# save_patient_name
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


@contextmanager
def _turn(tenant_id, conversation_id):
    tok_t = ai_tools._tenant_id_ctx.set(tenant_id)
    tok_c = ai_tools._conversation_id_ctx.set(conversation_id)
    try:
        yield
    finally:
        ai_tools._conversation_id_ctx.reset(tok_c)
        ai_tools._tenant_id_ctx.reset(tok_t)


async def _seed(db, *, name=None, channel="whatsapp"):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinica", phone_number_id=str(uuid4())[:12])
        other = Tenant(id=uuid4(), clinic_name="Outra", phone_number_id=str(uuid4())[:12])
        session.add_all([tenant, other])
        await session.flush()
        patient = Patient(tenant_id=tenant.id, wa_id="5511900000001", name=name, channel=channel)
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.commit()
        return tenant.id, other.id, patient.id, conversation.id


async def _name_of(db, patient_id):
    async with db() as session:
        return (await session.get(Patient, patient_id)).name


async def test_save_patient_name_fills_an_empty_name(db):
    tenant_id, _, patient_id, conversation_id = await _seed(db)
    with _turn(tenant_id, conversation_id):
        result = await ai_tools.save_patient_name.coroutine(name="fabio teste")
    assert result == {"saved": True}
    assert await _name_of(db, patient_id) == "Fabio Teste"


async def test_save_patient_name_never_overwrites_a_stored_name(db):
    tenant_id, _, patient_id, conversation_id = await _seed(db, name="Maria Silva")
    with _turn(tenant_id, conversation_id):
        result = await ai_tools.save_patient_name.coroutine(name="Outro Nome")
    assert "error" in result
    assert await _name_of(db, patient_id) == "Maria Silva"


@pytest.mark.parametrize("text", ["", "oi", "meu e-mail é a@b.co", "quero marcar uma consulta"])
async def test_save_patient_name_refuses_what_is_not_a_name(db, text):
    tenant_id, _, patient_id, conversation_id = await _seed(db)
    with _turn(tenant_id, conversation_id):
        result = await ai_tools.save_patient_name.coroutine(name=text)
    assert "error" in result
    assert await _name_of(db, patient_id) is None


async def test_save_patient_name_is_tenant_isolated(db):
    _, other_tenant_id, patient_id, conversation_id = await _seed(db)
    with _turn(other_tenant_id, conversation_id):
        result = await ai_tools.save_patient_name.coroutine(name="Fabio Teste")
    assert "error" in result
    assert await _name_of(db, patient_id) is None


async def test_save_patient_name_without_a_turn_does_nothing(db):
    _, _, patient_id, _ = await _seed(db)
    result = await ai_tools.save_patient_name.coroutine(name="Fabio Teste")
    assert "error" in result
    assert await _name_of(db, patient_id) is None


async def test_save_patient_name_reports_the_name_on_the_portal(db, monkeypatch):
    tenant_id, _, patient_id, conversation_id = await _seed(db, channel="brain_message")
    async with db() as session:
        patient = await session.get(Patient, patient_id)
        patient.external_id = "session-1"
        await session.commit()
    reported: list[tuple] = []

    async def _report(tenant, external_id, name):
        reported.append((tenant, external_id, name))
        return True

    monkeypatch.setattr("secretaria.services.pending_identity.report_name", _report)
    with _turn(tenant_id, conversation_id):
        result = await ai_tools.save_patient_name.coroutine(name="Fabio Teste")
    assert result == {"saved": True}
    assert reported == [(tenant_id, "session-1", "Fabio Teste")]
