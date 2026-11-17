"""TASK-086 A17 (SPEC E7): the hub's Editar WARNS when the final convênio is not accepted.

The clinic's staff may book any combination (their call, like an "encaixe"): the edit is always
saved, and the answer carries `insurance_not_accepted: true` when the convênio, the service or
the doctor changed and the FINAL (service, unit, plan) is a refused cell of the acceptance
matrix. A time-only edit, Particular and an untouched matrix answer false (default), and another
clinic's matrix never counts.
"""

# ruff: noqa: F811

from datetime import timedelta
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from secretaria.models import (
    Appointment,
    Professional,
    ProfessionalInsurancePlan,
    Service,
    Tenant,
    TenantInsurancePlan,
)
from secretaria.models.insurance_acceptance_exception import InsuranceAcceptanceException
from secretaria.models.professional_unit import ProfessionalUnit
from secretaria.models.unit import Unit
from secretaria.services import staff_appointment_edit as sae
from secretaria.services.staff_appointment_edit import StaffEditRequest
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeCalendar,
    acting,
    r7,
    set_appointment,
    set_tenant,
    setup_world,
    soon,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment, seed_world


@pytest.fixture(autouse=True)
def _calendars():
    FakeCalendar.reset()


async def calendar_for(professional_id):
    return FakeCalendar.get(str(professional_id) if professional_id else "tenant")


def _req(**values) -> StaffEditRequest:
    return StaffEditRequest(fields=frozenset(values), **values)


async def _matrix_world(db, world, mode: str):
    """Catalog services Consulta/Retorno (linked entries), plans A/B for `mode`, no exception."""
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        consulta = Service(tenant_id=tenant.id, name="Consulta", normalized_name="consulta")
        retorno = Service(tenant_id=tenant.id, name="Retorno", normalized_name="retorno")
        session.add_all([consulta, retorno])
        await session.flush()
        tenant.appointment_types = [
            {
                "name": "Consulta",
                "is_active": True,
                "duration_min": 30,
                "service_id": str(consulta.id),
            },
            {
                "name": "Retorno",
                "is_active": True,
                "duration_min": 40,
                "service_id": str(retorno.id),
            },
        ]
        tenant.insurance_mode = mode
        pro_id = world.appointment.professional_id
        if mode == "shared":
            plan_a = TenantInsurancePlan(tenant_id=tenant.id, custom_name="Plano A")
            plan_b = TenantInsurancePlan(tenant_id=tenant.id, custom_name="Plano B")
        else:
            plan_a = ProfessionalInsurancePlan(
                tenant_id=tenant.id, professional_id=pro_id, custom_name="Plano A"
            )
            plan_b = ProfessionalInsurancePlan(
                tenant_id=tenant.id, professional_id=pro_id, custom_name="Plano B"
            )
        session.add_all([plan_a, plan_b])
        await session.commit()
    await set_appointment(db, world.appointment.id, insurance="Plano A")
    return SimpleNamespace(
        mode=mode, consulta=consulta, retorno=retorno, plan_a=plan_a, plan_b=plan_b, pro=pro_id
    )


async def _refuse(db, tenant_id, m, service, plan, *, unit_id=None):
    async with db() as session:
        if m.mode == "shared":
            row = InsuranceAcceptanceException(
                tenant_id=tenant_id, service_id=service.id, unit_id=unit_id, tenant_plan_id=plan.id
            )
        else:
            row = InsuranceAcceptanceException(
                tenant_id=tenant_id,
                professional_id=m.pro,
                service_id=service.id,
                unit_id=unit_id,
                professional_plan_id=plan.id,
            )
        session.add(row)
        await session.commit()


async def _world(db, mode: str, phone_number_id: str = "pnid-1"):
    world = await seed_world(
        db,
        start_at=soon(),
        google_event_id="evt-1",
        professional_name="Dra. Ana",
        phone_number_id=phone_number_id,
    )
    m = await _matrix_world(db, world, mode)
    world.appointment = await reload_appointment(db, world.appointment.id)
    return world, m


async def _edit(db, world, request):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        return await sae.apply_staff_edit(
            session, tenant, appointment, request, calendar_for=calendar_for
        )


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_a_refused_convenio_is_saved_with_the_warning(db, mode):
    world, m = await _world(db, mode)
    await _refuse(db, world.tenant.id, m, m.consulta, m.plan_b)

    outcome = await _edit(db, world, _req(insurance="Plano B"))

    assert outcome.insurance_not_accepted is True
    row = await reload_appointment(db, world.appointment.id)
    assert row.insurance == "Plano B"  # never blocks (E7)


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_a_new_service_refused_for_the_kept_convenio_warns(db, mode):
    world, m = await _world(db, mode)
    await _refuse(db, world.tenant.id, m, m.retorno, m.plan_a)

    outcome = await _edit(db, world, _req(service="Retorno"))

    assert outcome.insurance_not_accepted is True
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Retorno"


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_an_accepted_combination_does_not_warn(db, mode):
    world, m = await _world(db, mode)
    await _refuse(db, world.tenant.id, m, m.retorno, m.plan_b)  # another cell

    outcome = await _edit(db, world, _req(insurance="Plano B"))

    assert outcome.insurance_not_accepted is False


async def test_particular_and_a_cleared_convenio_never_warn(db):
    world, m = await _world(db, "shared")
    await _refuse(db, world.tenant.id, m, m.consulta, m.plan_a)

    particular = await _edit(db, world, _req(insurance="Particular"))
    assert particular.insurance_not_accepted is False

    world.appointment = await reload_appointment(db, world.appointment.id)
    cleared = await _edit(db, world, _req(insurance=None))
    assert cleared.insurance_not_accepted is False


async def test_a_time_only_edit_never_warns_even_on_a_refused_booking(db):
    """R14: acceptance does not depend on the time; nothing about the convênio changed."""
    world, m = await _world(db, "shared")
    await _refuse(db, world.tenant.id, m, m.consulta, m.plan_a)  # the CURRENT booking

    outcome = await _edit(db, world, _req(start_at=world.start_at + timedelta(days=1)))
    assert outcome.insurance_not_accepted is False

    world.appointment = await reload_appointment(db, world.appointment.id)
    attendee = await _edit(db, world, _req(attendee_name="João"))
    assert attendee.insurance_not_accepted is False


async def test_an_untouched_matrix_never_warns(db):
    world, _m = await _world(db, "shared")

    outcome = await _edit(db, world, _req(insurance="Plano B", service="Retorno"))

    assert outcome.insurance_not_accepted is False


async def test_the_unit_of_the_booking_decides(db):
    world, m = await _world(db, "shared")
    async with db() as session:
        centro = Unit(tenant_id=world.tenant.id, name="Centro")
        session.add(centro)
        await session.flush()
        session.add(ProfessionalUnit(professional_id=m.pro, unit_id=centro.id))
        await session.commit()
    await _refuse(db, world.tenant.id, m, m.consulta, m.plan_b, unit_id=centro.id)

    # no unit on the booking: the Centro cell is not this booking's
    no_unit = await _edit(db, world, _req(insurance="Plano B"))
    assert no_unit.insurance_not_accepted is False

    await set_appointment(db, world.appointment.id, insurance="Plano A", unit_id=centro.id)
    world.appointment = await reload_appointment(db, world.appointment.id)
    at_centro = await _edit(db, world, _req(insurance="Plano B"))
    assert at_centro.insurance_not_accepted is True


async def test_a_new_doctor_is_judged_on_his_own_matrix(db):
    world, m = await _world(db, "independent")
    async with db() as session:
        bruno = Professional(tenant_id=world.tenant.id, name="Dr. Bruno", is_active=True)
        session.add(bruno)
        await session.flush()
        bruno_a = ProfessionalInsurancePlan(
            tenant_id=world.tenant.id, professional_id=bruno.id, custom_name="Plano A"
        )
        session.add(bruno_a)
        await session.flush()
        session.add(
            InsuranceAcceptanceException(
                tenant_id=world.tenant.id,
                professional_id=bruno.id,
                service_id=m.consulta.id,
                unit_id=None,
                professional_plan_id=bruno_a.id,
            )
        )
        await session.commit()

    outcome = await _edit(db, world, _req(professional_id=bruno.id))

    assert outcome.insurance_not_accepted is True
    assert (await reload_appointment(db, world.appointment.id)).professional_id == bruno.id


async def test_another_clinics_matrix_never_counts(db):
    world, m = await _world(db, "shared")
    other, om = await _world(db, "shared", phone_number_id="pnid-2")
    # Clinic 2 refuses ITS Consulta x Plano B (same names as clinic 1).
    await _refuse(db, other.tenant.id, om, om.consulta, om.plan_b)

    outcome = await _edit(db, world, _req(insurance="Plano B"))
    assert outcome.insurance_not_accepted is False

    # Even a row of clinic 2 pointing at clinic 1's own ids is never read for clinic 1.
    async with db() as session:
        session.add(
            InsuranceAcceptanceException(
                tenant_id=other.tenant.id,
                service_id=m.consulta.id,
                unit_id=None,
                tenant_plan_id=m.plan_a.id,
            )
        )
        await session.commit()
    world.appointment = await reload_appointment(db, world.appointment.id)
    back = await _edit(db, world, _req(insurance="Plano A"))
    assert back.insurance_not_accepted is False


async def test_the_hub_route_answers_the_warning_additively(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, google_event_id="evt-1", professional_name="Dra. Ana")
    m = await _matrix_world(db, world, "shared")
    await _refuse(db, world.tenant.id, m, m.consulta, m.plan_b)
    await set_tenant(db, world.tenant.id, insurance_mode="shared")

    refused = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/edit", json={"insurance": "Plano B"}
    )
    assert refused.status_code == 200, refused.text
    assert refused.json()["insurance_not_accepted"] is True
    assert (await reload_appointment(db, world.appointment.id)).insurance == "Plano B"

    moved = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/edit",
        json={"start_at": (world.start_at + timedelta(days=1)).isoformat()},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["insurance_not_accepted"] is False
