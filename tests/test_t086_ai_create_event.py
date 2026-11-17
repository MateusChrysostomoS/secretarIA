"""TASK-086 A14 (W5): the legacy `create_event` refuses a convênio the matrix does not accept.

One-doctor clinic (the legacy toolset's topology). The guard reads the conversation's convênio,
the sole doctor, the canonical service id and the unit, and answers a tool error BEFORE any
calendar call. A read failure refuses (fail-closed, like the unit check). "Particular" and a
clinic that never touched the matrix book exactly as before.

In-memory SQLite, fake agenda; no Google, no LLM.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from contextlib import contextmanager  # noqa: E402
from datetime import UTC  # noqa: E402
from uuid import uuid4  # noqa: E402

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
    Conversation,
    Patient,
    Professional,
    ProfessionalInsurancePlan,
    Service,
    Tenant,
    TenantInsurancePlan,
    Unit,
)
from secretaria.models.insurance_acceptance_exception import (  # noqa: E402
    InsuranceAcceptanceException,
)
from secretaria.models.professional_unit import ProfessionalUnit  # noqa: E402
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_SOLE  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    RuntimeAppointmentType,
    TenantRuntimeConfig,
)

CENTRO_HOURS = {"friday": [{"start": "08:00", "end": "12:00"}]}
WINDOW = {
    "start": "2026-07-10T09:00:00",
    "end": "2026-07-10T09:30:00",
    "summary": "Consulta",
    "appointment_type": "Consulta",
}


class _Agenda:
    """Fake agenda: records every write; a write is what the guard must prevent."""

    def __init__(self):
        self.tzinfo = UTC
        self.created: list = []
        self.unit = None

    def with_unit(self, unit_id, location, *, business_hours=None):
        self.unit = unit_id
        return self

    async def list_free_slots(self, day, max_slots=6, **_kw):
        return []

    async def create_event(self, start, end, summary, description=""):
        self.created.append((start, end))
        return {"id": f"evt-{len(self.created)}", "status": "confirmed", "htmlLink": None}


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
def _wire(monkeypatch, db):
    monkeypatch.setattr(core_database, "async_session_factory", db)


async def _world(db, *, mode="shared", with_unit=True, insurance="Unimed"):
    """One clinic, one doctor (optionally at one unit), one service, one plan, one chat."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            insurance_mode=mode,
        )
        session.add(tenant)
        await session.flush()
        doctor = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        service = Service(tenant_id=tenant.id, name="Consulta", normalized_name="consulta")
        clinic_plan = TenantInsurancePlan(tenant_id=tenant.id, custom_name="Unimed")
        session.add_all([doctor, service, clinic_plan])
        await session.flush()
        doctor_plan = ProfessionalInsurancePlan(
            professional_id=doctor.id, tenant_id=tenant.id, custom_name="Unimed"
        )
        session.add(doctor_plan)
        unit = None
        if with_unit:
            unit = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro", address="Rua A, 10")
            session.add(unit)
            await session.flush()
            session.add(
                ProfessionalUnit(
                    professional_id=doctor.id, unit_id=unit.id, business_hours=CENTRO_HOURS
                )
            )
        patient = Patient(tenant_id=tenant.id, wa_id=str(uuid4().int)[:13], name="Paciente")
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, flow_selected_insurance=insurance
        )
        session.add(conversation)
        await session.commit()
        return {
            "tenant": tenant,
            "doctor": doctor,
            "service": service,
            "clinic_plan": clinic_plan,
            "doctor_plan": doctor_plan,
            "unit": unit,
            "conversation": conversation,
        }


async def _refuse(db, world, *, unit_id="same"):
    """Store one refused cell for the world's service/plan (at its unit, or `unit_id`)."""
    unit_id = (
        (world["unit"].id if world["unit"] is not None else None) if unit_id == "same" else unit_id
    )
    async with db() as session:
        if world["tenant"].insurance_mode == "independent":
            row = InsuranceAcceptanceException(
                tenant_id=world["tenant"].id,
                professional_id=world["doctor"].id,
                service_id=world["service"].id,
                unit_id=unit_id,
                professional_plan_id=world["doctor_plan"].id,
            )
        else:
            row = InsuranceAcceptanceException(
                tenant_id=world["tenant"].id,
                service_id=world["service"].id,
                unit_id=unit_id,
                tenant_plan_id=world["clinic_plan"].id,
            )
        session.add(row)
        await session.commit()


def _config(world, *, runtime=False) -> TenantRuntimeConfig:
    """`runtime=True`: the production shape - `RuntimeAppointmentType` objects, no service id."""
    if runtime:
        types = [RuntimeAppointmentType(name="Consulta", description=None, duration_min=30)]
    else:
        types = [{"name": "Consulta", "duration_min": 30, "service_id": str(world["service"].id)}]
    return TenantRuntimeConfig(
        tenant_id=world["tenant"].id,
        clinic_name="Clinic",
        language="pt-BR",
        timezone="UTC",
        appointment_duration_min=30,
        appointment_types=types,
        business_hours={},
        google_calendar_id="primary",
        google_refresh_token=None,
        professional_id=world["doctor"].id,
    )


@contextmanager
def _turn(world, agenda, *, runtime=False):
    tokens = [
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(world["tenant"].id)),
        (
            ai_tools._conversation_id_ctx,
            ai_tools._conversation_id_ctx.set(world["conversation"].id),
        ),
        (ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(agenda)),
        (
            ai_tools._tenant_config_ctx,
            ai_tools._tenant_config_ctx.set(_config(world, runtime=runtime)),
        ),
        (
            ai_tools._booking_topology_ctx,
            ai_tools._booking_topology_ctx.set(BOOKING_TOPOLOGY_SOLE),
        ),
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


async def _appointments(db):
    async with db() as session:
        return list(await session.scalars(select(Appointment)))


# --------------------------------------------------------------------------
# Characterization: no matrix -> the booking goes through exactly as before
# --------------------------------------------------------------------------


async def test_no_exceptions_books_as_before(db):
    world = await _world(db)
    agenda = _Agenda()
    with _turn(world, agenda):
        booked = await ai_tools.create_event.ainvoke(WINDOW)
    assert "id" in booked and "error" not in booked
    assert len(agenda.created) == 1
    assert len(await _appointments(db)) == 1


async def test_refusal_at_another_unit_does_not_block_this_one(db):
    world = await _world(db)
    await _refuse(db, world, unit_id=None)  # the "no unit" slice, not this unit
    agenda = _Agenda()
    with _turn(world, agenda):
        booked = await ai_tools.create_event.ainvoke(WINDOW)
    assert "id" in booked
    assert len(agenda.created) == 1


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_refused_combo_never_reaches_the_calendar(db, mode):
    world = await _world(db, mode=mode)
    await _refuse(db, world)
    agenda = _Agenda()
    with _turn(world, agenda):
        refused = await ai_tools.create_event.ainvoke(WINDOW)
    assert refused == {"error": ai_tools.INSURANCE_NOT_ACCEPTED_ERROR}
    assert agenda.created == []  # the calendar client is never called
    assert await _appointments(db) == []


async def test_refused_for_a_doctor_without_units_uses_the_no_unit_slice(db):
    world = await _world(db, with_unit=False)
    await _refuse(db, world)
    agenda = _Agenda()
    with _turn(world, agenda):
        refused = await ai_tools.create_event.ainvoke(WINDOW)
    assert "error" in refused
    assert agenda.created == []


async def test_particular_is_never_refused(db):
    world = await _world(db, insurance="Particular")
    await _refuse(db, world)
    agenda = _Agenda()
    with _turn(world, agenda):
        booked = await ai_tools.create_event.ainvoke(WINDOW)
    assert "id" in booked
    assert len(agenda.created) == 1


async def test_no_convenio_answered_is_never_refused(db):
    world = await _world(db, insurance=None)
    await _refuse(db, world)
    agenda = _Agenda()
    with _turn(world, agenda):
        booked = await ai_tools.create_event.ainvoke(WINDOW)
    assert "id" in booked


async def test_a_failed_read_refuses_before_the_calendar(db, monkeypatch):
    world = await _world(db)
    agenda = _Agenda()

    async def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "secretaria.services.insurance_acceptance.booking_refused", _boom, raising=True
    )
    with _turn(world, agenda):
        refused = await ai_tools.create_event.ainvoke(WINDOW)
    assert refused == {"error": ai_tools.INSURANCE_LOOKUP_FAILED_ERROR}
    assert agenda.created == []
    assert await _appointments(db) == []


async def test_the_refusal_log_carries_ids_only(db, caplog, monkeypatch):
    world = await _world(db)
    await _refuse(db, world)
    seen: list[tuple[str, dict]] = []

    from secretaria.services import insurance_acceptance

    monkeypatch.setattr(
        insurance_acceptance.logger,
        "info",
        lambda event, **kw: seen.append((event, kw)),
    )
    with _turn(world, _Agenda()):
        await ai_tools.create_event.ainvoke(WINDOW)
    [(event, fields)] = [item for item in seen if item[0] == "agent_tool_insurance_refused"]
    assert "Unimed" not in str(fields)
    assert fields["service_id"] == str(world["service"].id)


async def _store_doctor_services(db, world):
    async with db() as session:
        doctor = await session.get(Professional, world["doctor"].id)
        doctor.appointment_types = [
            {"name": "Consulta", "duration_min": 30, "service_id": str(world["service"].id)}
        ]
        await session.commit()


async def test_production_catalog_shape_resolves_the_service_id_from_the_doctor(db):
    """The turn's catalog is `RuntimeAppointmentType` (no id): the guard reads the doctor's
    stored entries, so the matrix still applies."""
    world = await _world(db, with_unit=False)
    await _store_doctor_services(db, world)
    await _refuse(db, world)
    agenda = _Agenda()
    with _turn(world, agenda, runtime=True):
        refused = await ai_tools.create_event.ainvoke(WINDOW)
    assert refused == {"error": ai_tools.INSURANCE_NOT_ACCEPTED_ERROR}
    assert agenda.created == []


async def test_production_catalog_shape_without_exceptions_books(db):
    world = await _world(db, with_unit=False)
    await _store_doctor_services(db, world)
    agenda = _Agenda()
    with _turn(world, agenda, runtime=True):
        booked = await ai_tools.create_event.ainvoke(WINDOW)
    assert "id" in booked
    assert len(agenda.created) == 1
