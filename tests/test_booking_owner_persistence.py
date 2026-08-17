"""Every booking persists its effective owner and a canonical service.

The tenant reproduced throughout this module is the one that broke: its LEGACY
`tenants.appointment_types` is empty and everything — service, price, hours —
is configured on its single active `Professional`. Such a clinic listed its
services and booked correctly, then lost the owner on the way to the DB, which
sent the Pix price lookup back to the (empty) tenant catalog and skipped the
deposit. The LLM path had a second bug on top: it stored the free Google
Calendar title as `appointment_type`.

In-memory-sqlite pattern from tests/test_multi_professional_plugin.py (the DB
seam is patched at its SOURCE, `secretaria.core.database.async_session_factory`,
because ai/tools.py imports it lazily). Asaas and WhatsApp are fakes — nothing
here ever reaches a real payment provider.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from contextlib import contextmanager  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
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

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
    Professional,
    Tenant,
)
from secretaria.models.pix_deposit import PixDeposit  # noqa: E402
from secretaria.plugins import (  # noqa: E402
    multi_professional as mp,  # noqa: E402
    pix_deposit,
    post_booking,
)
from secretaria.services import tenant_config as cfg  # noqa: E402
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import STEP_AWAITING_CONFIRMATION, route  # noqa: E402
from secretaria.services.payments import deposit_lifecycle  # noqa: E402

_TZ = ZoneInfo("America/Sao_Paulo")
_SERVICE = "Consulta Cardiológica"
_PRICE = "R$ 300,00"
_PROFESSIONAL_CATALOG = [
    {
        "name": _SERVICE,
        "duration_min": 30,
        "price": _PRICE,
        "is_active": True,
        "sort_order": 0,
    }
]

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "analytics_bi_advanced": False,
    "human_backup_24_7": False,
}


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons={**_ALL_ADDONS_OFF, "pix_deposit": True},
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeCalendar:
    """Records created events; never touches Google."""

    def __init__(self, calendar_id="cal"):
        self.calendar_id = calendar_id
        self.tzinfo = _TZ
        self.created: list[dict] = []

    async def create_event(self, start, end, summary, description=""):
        self.created.append({"summary": summary, "start": start, "end": end})
        return {
            "id": f"evt-{len(self.created)}-{self.calendar_id}",
            "status": "confirmed",
            "htmlLink": "https://cal/evt",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        return [{"start": "2026-08-03T08:00", "end": "2026-08-03T08:30", "label": "08:00"}]


class _StubCalendarService:
    """CalendarService stand-in for the professional-aware tool's construction seam."""

    def __init__(self, *args, **kwargs):
        self.tzinfo = _TZ

    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    @classmethod
    def for_professional(cls, tenant_config, **overrides):
        return cls()

    async def create_event(self, start, end, summary, description=""):
        return {
            "id": "evt-prof",
            "status": "confirmed",
            "htmlLink": "https://cal/evt-prof",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }


class _FakeAsaas:
    """Records every call; nothing leaves the process."""

    def __init__(self):
        self.payments: list[tuple] = []

    async def create_customer(self, name, mobile_phone):
        return "cus_fake"

    async def create_pix_payment(
        self, customer_id, value_cents, external_reference, description, due_date
    ):
        self.payments.append((external_reference, value_cents))
        return {"id": f"pay_{len(self.payments)}", "status": "PENDING"}

    async def get_pix_qr(self, payment_id):
        return {"payload": "000201pix", "encodedImage": "b64", "expirationDate": "2026-08-04"}


class _FakeWhatsApp:
    sent: list[tuple] = []

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        _FakeWhatsApp.sent.append((to, body))
        return {"messages": [{"id": "wamid.test"}]}


class _FakeRedis:
    """Records enqueued jobs instead of talking to arq."""

    def __init__(self):
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, args))


# --------------------------------------------------------------------------
# Fixtures
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
def _fakes(monkeypatch: pytest.MonkeyPatch, db):
    # ai/tools.py + the plugin helpers import async_session_factory lazily, so
    # the SOURCE module attribute is the seam that covers every caller.
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(post_booking, "async_session_factory", db)
    monkeypatch.setattr(pix_deposit, "async_session_factory", db)
    monkeypatch.setattr(deposit_lifecycle, "WhatsAppClient", _FakeWhatsApp)
    _FakeWhatsApp.sent = []
    yield


@contextmanager
def _agent_context(
    tenant_id, tenant_config=None, conversation_id=None, topology=None, redis=None, calendar=None
):
    """Exactly what ai/graph.py::run_agent sets before invoking the agent."""
    tokens = [
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(tenant_id)),
        (ai_tools._tenant_config_ctx, ai_tools._tenant_config_ctx.set(tenant_config)),
        (ai_tools._conversation_id_ctx, ai_tools._conversation_id_ctx.set(conversation_id)),
        (ai_tools._redis_ctx, ai_tools._redis_ctx.set(redis)),
        (ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(calendar)),
    ]
    if topology is not None:
        tokens.append(
            (ai_tools._booking_topology_ctx, ai_tools._booking_topology_ctx.set(topology))
        )
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


async def _seed_sole_professional_clinic(db, *, price=_PRICE, pix=True):
    """The broken-in-production shape: empty tenant catalog, one professional."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinica Teste",
            phone_number_id=str(uuid4())[:12],
            timezone="America/Sao_Paulo",
            appointment_types=[],  # LEGACY catalog deliberately empty
            business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
            appointment_duration_min=30,
            google_calendar_id="clinic-cal",
            pix_deposit_enabled=pix,
            pix_deposit_percent=30,
        )
        session.add(tenant)
        await session.flush()
        professional = Professional(
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[{**_PROFESSIONAL_CATALOG[0], "price": price}],
            business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        )
        patient = Patient(tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add_all([professional, patient])
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        if pix:
            await cfg.set_asaas_api_key(session, tenant.id, "asaas-test-key")
        await session.commit()
        for row in (tenant, professional, patient, conversation):
            await session.refresh(row)
        return SimpleNamespace(
            tenant=tenant,
            professional=professional,
            patient=patient,
            conversation=conversation,
        )


def _flow_tenant_snapshot(clinic):
    """What workers/tasks.py::_flow_tenant_snapshot builds for this clinic."""
    return SimpleNamespace(
        initial_flows={},
        appointment_types=clinic.professional.appointment_types,
        appointment_duration_min=clinic.tenant.appointment_duration_min,
        business_hours=clinic.professional.business_hours,
        collect_insurance=False,
        insurances=None,
    )


def _flow_professional(professional):
    """The plain snapshot the worker passes to route(professionals=...)."""
    return SimpleNamespace(
        id=professional.id,
        name=professional.name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=professional.appointment_types,
    )


def _conversation_snapshot(**kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type=_SERVICE,
        flow_selected_day="2026-08-03",
        flow_selected_slot="2026-08-03T08:00",
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


async def _persist_flow_appointment(db, clinic, result):
    """Persist a FlowRouterResult exactly like workers/tasks.py::_apply_flow_result."""
    async with db() as session:
        async with session.begin():
            appointment = Appointment(
                tenant_id=clinic.tenant.id,
                patient_id=clinic.patient.id,
                conversation_id=clinic.conversation.id,
                phone=clinic.patient.wa_id,
                status=AppointmentStatus.SCHEDULED,
                **result.appointment,
            )
            session.add(appointment)
        await session.refresh(appointment)
        return appointment


async def _tenant_runtime_config(db, tenant):
    async with db() as session:
        return await cfg.load_tenant_config(session, await session.get(Tenant, tenant.id))


# --------------------------------------------------------------------------
# Deterministic flow: the owner reaches the row
# --------------------------------------------------------------------------


async def test_flow_booking_carries_the_single_professional_as_owner(db):
    clinic = await _seed_sole_professional_clinic(db)
    result = await route(
        _conversation_snapshot(),
        _flow_tenant_snapshot(clinic),
        _FakeCalendar(),
        "Confirmar",
        "Maria",
        professionals=[_flow_professional(clinic.professional)],
    )
    assert result.appointment is not None
    assert result.appointment["professional_id"] == clinic.professional.id
    assert result.appointment["appointment_type"] == _SERVICE


async def test_flow_booking_without_any_professional_has_no_owner(db):
    """Zero active professionals: nothing to attribute the booking to."""
    clinic = await _seed_sole_professional_clinic(db)
    result = await route(
        _conversation_snapshot(),
        _flow_tenant_snapshot(clinic),
        _FakeCalendar(),
        "Confirmar",
        "Maria",
        professionals=[],
    )
    assert "professional_id" not in result.appointment


async def test_flow_booking_stores_the_catalog_spelling_not_the_stored_drift(db):
    """A tapped, truncated or differently-cased type is canonicalised."""
    clinic = await _seed_sole_professional_clinic(db)
    result = await route(
        _conversation_snapshot(flow_selected_type=_SERVICE.lower()),
        _flow_tenant_snapshot(clinic),
        _FakeCalendar(),
        "Confirmar",
        "Maria",
        professionals=[_flow_professional(clinic.professional)],
    )
    assert result.appointment["appointment_type"] == _SERVICE


# --------------------------------------------------------------------------
# LLM base tool: owner + canonical type, summary kept separate
# --------------------------------------------------------------------------


async def test_base_create_event_persists_owner_and_canonical_type(db):
    clinic = await _seed_sole_professional_clinic(db)
    config = await _tenant_runtime_config(db, clinic.tenant)
    # load_tenant_config resolves the single professional's catalog + identity.
    assert config.professional_id == clinic.professional.id
    assert [t.name for t in config.appointment_types] == [_SERVICE]

    with _agent_context(
        clinic.tenant.id,
        tenant_config=config,
        conversation_id=clinic.conversation.id,
        topology=BOOKING_TOPOLOGY_SOLE,
        calendar=_FakeCalendar(),
    ):
        result = await ai_tools.create_event.ainvoke(
            {
                "start": "2026-08-03T08:00:00",
                "end": "2026-08-03T08:30:00",
                "summary": "Consulta - Maria",
                "appointment_type": _SERVICE,
            }
        )
    assert "id" in result
    async with db() as session:
        appointment = (await session.scalars(select(Appointment))).one()
        assert appointment.professional_id == clinic.professional.id
        # The Calendar title and the service are different concepts.
        assert appointment.appointment_type == _SERVICE
        assert appointment.appointment_type != "Consulta - Maria"


async def test_base_create_event_derives_the_type_when_the_catalog_is_unambiguous(db):
    clinic = await _seed_sole_professional_clinic(db)
    config = await _tenant_runtime_config(db, clinic.tenant)
    with _agent_context(
        clinic.tenant.id,
        tenant_config=config,
        topology=BOOKING_TOPOLOGY_SOLE,
        calendar=_FakeCalendar(),
    ):
        await ai_tools.create_event.ainvoke(
            {
                "start": "2026-08-03T08:00:00",
                "end": "2026-08-03T08:30:00",
                "summary": "Consulta - Maria",
            }
        )
    async with db() as session:
        appointment = (await session.scalars(select(Appointment))).one()
        assert appointment.appointment_type == _SERVICE


async def test_base_create_event_refuses_a_service_the_clinic_does_not_have(db):
    """Fail closed: no Google event, no row, and the valid options are listed."""
    clinic = await _seed_sole_professional_clinic(db)
    config = await _tenant_runtime_config(db, clinic.tenant)
    calendar = _FakeCalendar()
    with _agent_context(
        clinic.tenant.id,
        tenant_config=config,
        topology=BOOKING_TOPOLOGY_SOLE,
        calendar=calendar,
    ):
        result = await ai_tools.create_event.ainvoke(
            {
                "start": "2026-08-03T08:00:00",
                "end": "2026-08-03T08:30:00",
                "summary": "Consulta - Maria",
                "appointment_type": "Botox",
            }
        )
    assert "error" in result
    assert _SERVICE in result["error"]
    assert calendar.created == []
    async with db() as session:
        assert (await session.scalars(select(Appointment))).all() == []


async def test_base_create_event_requires_a_type_when_the_catalog_is_ambiguous(db):
    clinic = await _seed_sole_professional_clinic(db)
    async with db() as session:
        professional = await session.get(Professional, clinic.professional.id)
        professional.appointment_types = [
            *_PROFESSIONAL_CATALOG,
            {"name": "Retorno", "duration_min": 20, "is_active": True, "sort_order": 1},
        ]
        await session.commit()
    config = await _tenant_runtime_config(db, clinic.tenant)
    calendar = _FakeCalendar()
    with _agent_context(
        clinic.tenant.id,
        tenant_config=config,
        topology=BOOKING_TOPOLOGY_SOLE,
        calendar=calendar,
    ):
        result = await ai_tools.create_event.ainvoke(
            {
                "start": "2026-08-03T08:00:00",
                "end": "2026-08-03T08:30:00",
                "summary": "Consulta - Maria",
            }
        )
    assert "error" in result
    assert calendar.created == []


async def test_base_create_event_books_without_a_type_when_there_is_no_catalog(db):
    """No services configured at all: NULL is honest, the free title is not."""
    clinic = await _seed_sole_professional_clinic(db)
    async with db() as session:
        professional = await session.get(Professional, clinic.professional.id)
        professional.appointment_types = []
        await session.commit()
    config = await _tenant_runtime_config(db, clinic.tenant)
    with _agent_context(
        clinic.tenant.id,
        tenant_config=config,
        topology=BOOKING_TOPOLOGY_SOLE,
        calendar=_FakeCalendar(),
    ):
        result = await ai_tools.create_event.ainvoke(
            {
                "start": "2026-08-03T08:00:00",
                "end": "2026-08-03T08:30:00",
                "summary": "Consulta - Maria",
            }
        )
    assert "id" in result
    async with db() as session:
        appointment = (await session.scalars(select(Appointment))).one()
        assert appointment.appointment_type is None
        assert appointment.professional_id == clinic.professional.id


async def test_base_create_event_claims_no_owner_on_a_tenant_without_professionals(db):
    clinic = await _seed_sole_professional_clinic(db)
    async with db() as session:
        professional = await session.get(Professional, clinic.professional.id)
        professional.is_active = False
        await session.commit()
    config = await _tenant_runtime_config(db, clinic.tenant)
    with _agent_context(
        clinic.tenant.id,
        tenant_config=config,
        topology=BOOKING_TOPOLOGY_NONE,
        calendar=_FakeCalendar(),
    ):
        await ai_tools.create_event.ainvoke(
            {
                "start": "2026-08-03T08:00:00",
                "end": "2026-08-03T08:30:00",
                "summary": "Consulta - Maria",
            }
        )
    async with db() as session:
        appointment = (await session.scalars(select(Appointment))).one()
        assert appointment.professional_id is None


async def test_base_create_event_never_books_on_a_multi_professional_tenant(db):
    """A `professional_id` overlaid for the PROMPT is not an owner for this tool."""
    clinic = await _seed_sole_professional_clinic(db)
    config = await _tenant_runtime_config(db, clinic.tenant)
    calendar = _FakeCalendar()
    with _agent_context(
        clinic.tenant.id,
        tenant_config=config,
        topology=BOOKING_TOPOLOGY_MULTI,
        calendar=calendar,
    ):
        result = await ai_tools.create_event.ainvoke(
            {
                "start": "2026-08-03T08:00:00",
                "end": "2026-08-03T08:30:00",
                "summary": "Consulta - Maria",
                "appointment_type": _SERVICE,
            }
        )
    # Refused outright rather than booked with the prompt's professional.
    assert "error" in result
    assert calendar.created == []
    async with db() as session:
        assert (await session.scalars(select(Appointment))).all() == []


# --------------------------------------------------------------------------
# Professional-aware tool: same two guarantees, its own catalog
# --------------------------------------------------------------------------


async def test_professional_tool_persists_canonical_type_not_the_summary(db, monkeypatch):
    clinic = await _seed_sole_professional_clinic(db)
    monkeypatch.setattr(ai_tools, "CalendarService", _StubCalendarService)
    config = await _tenant_runtime_config(db, clinic.tenant)
    with _agent_context(clinic.tenant.id, tenant_config=config):
        result = await mp.create_event_for_professional.ainvoke(
            {
                "professional_name": "Dra. Ana",
                "start": "2026-08-03T08:00:00",
                "end": "2026-08-03T08:30:00",
                "summary": "Consulta - Maria",
                "appointment_type": _SERVICE.lower(),
            }
        )
    assert "id" in result
    async with db() as session:
        appointment = (await session.scalars(select(Appointment))).one()
        assert appointment.professional_id == clinic.professional.id
        assert appointment.appointment_type == _SERVICE


async def test_professional_tool_refuses_a_service_outside_that_professionals_catalog(
    db, monkeypatch
):
    clinic = await _seed_sole_professional_clinic(db)
    monkeypatch.setattr(ai_tools, "CalendarService", _StubCalendarService)
    config = await _tenant_runtime_config(db, clinic.tenant)
    with _agent_context(clinic.tenant.id, tenant_config=config):
        result = await mp.create_event_for_professional.ainvoke(
            {
                "professional_name": "Dra. Ana",
                "start": "2026-08-03T08:00:00",
                "end": "2026-08-03T08:30:00",
                "summary": "Consulta - Maria",
                "appointment_type": "Botox",
            }
        )
    assert "error" in result
    async with db() as session:
        assert (await session.scalars(select(Appointment))).all() == []


# --------------------------------------------------------------------------
# Pix: the price is found through the owner
# --------------------------------------------------------------------------


async def _add_appointment(session, clinic, **kw):
    base = dict(
        tenant_id=clinic.tenant.id,
        professional_id=clinic.professional.id,
        appointment_type=_SERVICE,
        google_event_id="evt-price",
        start_at=datetime(2026, 8, 3, 11, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 3, 11, 30, tzinfo=UTC),
    )
    base.update(kw)
    appointment = Appointment(**base)
    session.add(appointment)
    await session.flush()
    return appointment


async def test_price_resolves_through_the_owning_professional(db):
    clinic = await _seed_sole_professional_clinic(db)
    async with db() as session:
        tenant = await session.get(Tenant, clinic.tenant.id)
        appointment = await _add_appointment(session, clinic)
        price = await deposit_lifecycle._price_text_for_appointment(session, tenant, appointment)
    assert price == _PRICE


async def test_price_is_unresolvable_without_the_owner(db):
    """The exact production symptom: the legacy tenant catalog has nothing."""
    clinic = await _seed_sole_professional_clinic(db)
    async with db() as session:
        tenant = await session.get(Tenant, clinic.tenant.id)
        appointment = await _add_appointment(
            session, clinic, professional_id=None, google_event_id="evt-no-owner"
        )
        price = await deposit_lifecycle._price_text_for_appointment(session, tenant, appointment)
    assert price is None


async def test_free_calendar_title_as_type_creates_no_charge(db, monkeypatch):
    """A booking whose type is unprovable must skip the deposit, not guess one."""
    clinic = await _seed_sole_professional_clinic(db)
    asaas = _FakeAsaas()
    monkeypatch.setattr(deposit_lifecycle, "_asaas_client_for", lambda api_key: asaas)
    async with db() as session:
        tenant = await session.get(Tenant, clinic.tenant.id)
        patient = await session.get(Patient, clinic.patient.id)
        appointment = await _add_appointment(
            session,
            clinic,
            appointment_type="Consulta - Maria",  # a Calendar title, not a service
            google_event_id="evt-title",
        )
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token=None
        )
    assert deposit is None
    assert asaas.payments == []


async def test_owned_booking_creates_exactly_one_charge(db, monkeypatch):
    clinic = await _seed_sole_professional_clinic(db)
    asaas = _FakeAsaas()
    monkeypatch.setattr(deposit_lifecycle, "_asaas_client_for", lambda api_key: asaas)
    async with db() as session:
        tenant = await session.get(Tenant, clinic.tenant.id)
        patient = await session.get(Patient, clinic.patient.id)
        appointment = await _add_appointment(session, clinic, google_event_id="evt-ok")
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token=None
        )
        await session.commit()
    assert deposit is not None
    # 30% of R$ 300,00
    assert asaas.payments == [(str(appointment.id), 9000)]


# --------------------------------------------------------------------------
# End to end: both booking surfaces reach post_booking, one Pix intent each
# --------------------------------------------------------------------------


async def test_flow_and_tool_bookings_each_produce_exactly_one_pix_intent(db, monkeypatch):
    clinic = await _seed_sole_professional_clinic(db)
    asaas = _FakeAsaas()
    monkeypatch.setattr(deposit_lifecycle, "_asaas_client_for", lambda api_key: asaas)

    async def _entitlements(tenant_id, redis=None):
        return _summary(tenant_id=str(tenant_id))

    monkeypatch.setattr(post_booking, "get_entitlements", _entitlements)
    redis = _FakeRedis()

    # 1) deterministic flow
    flow_result = await route(
        _conversation_snapshot(),
        _flow_tenant_snapshot(clinic),
        _FakeCalendar(),
        "Confirmar",
        "Maria",
        professionals=[_flow_professional(clinic.professional)],
    )
    flow_appointment = await _persist_flow_appointment(db, clinic, flow_result)
    await post_booking.enqueue_post_booking_hooks(
        redis, clinic.tenant.id, flow_appointment.id, source="flow"
    )

    # 2) LLM tool
    config = await _tenant_runtime_config(db, clinic.tenant)
    with _agent_context(
        clinic.tenant.id,
        tenant_config=config,
        conversation_id=clinic.conversation.id,
        topology=BOOKING_TOPOLOGY_SOLE,
        redis=redis,
        calendar=_FakeCalendar("tool-cal"),
    ):
        await ai_tools.create_event.ainvoke(
            {
                "start": "2026-08-03T09:00:00",
                "end": "2026-08-03T09:30:00",
                "summary": "Consulta - Maria",
                "appointment_type": _SERVICE,
            }
        )

    # Both surfaces enqueued the hook exactly once, with their own source.
    assert [job[0] for job in redis.jobs] == [
        "run_post_booking_hooks",
        "run_post_booking_hooks",
    ]
    assert {job[1][2] for job in redis.jobs} == {"flow", "agent"}

    async with db() as session:
        appointments = (await session.scalars(select(Appointment))).all()
    assert len(appointments) == 2
    # Same owner, same canonical service, whichever surface booked it.
    assert {a.professional_id for a in appointments} == {clinic.professional.id}
    assert {a.appointment_type for a in appointments} == {_SERVICE}

    # Run the enqueued jobs: one Pix charge per booking, no more.
    for _name, args in redis.jobs:
        await post_booking.run_post_booking_hooks({"redis": redis}, *args)

    assert len(asaas.payments) == 2
    assert {reference for reference, _cents in asaas.payments} == {
        str(a.id) for a in appointments
    }
    assert {cents for _reference, cents in asaas.payments} == {9000}
    async with db() as session:
        deposits = (await session.scalars(select(PixDeposit))).all()
    assert len(deposits) == 2
