"""TASK-086 T4 review fixes.

1. HIGH - the multi-doctor and multi-unit AI booking tools (`create_event_for_professional`,
   `create_event_at_unit`) now ask the convênio matrix before any calendar call, like
   `create_event` (A14).
2. MEDIUM - `independent` mode, two doctors each with an "Outro" of the SAME name: the patient's
   answer resolves to one of the two ids, and the refused cell of the other doctor is still found.

In-memory SQLite, fake agenda; no Google, no LLM. Reuses the A14 fixtures.
"""

# ruff: noqa: F811 - pytest fixtures (`db`) imported from the A14 test module

from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.models import Professional, ProfessionalInsurancePlan, Service, Tenant
from secretaria.models.insurance_acceptance_exception import InsuranceAcceptanceException
from secretaria.plugins import multi_professional as mp, multi_unit as mu
from secretaria.services import flow_router as fr
from secretaria.services.insurance_acceptance import is_accepted, snapshot_exceptions
from secretaria.services.insurance_catalog import load_tenant_insurance
from secretaria.workers.shared.greeting import _flow_tenant_snapshot
from tests.test_t086_ai_create_event import (  # noqa: F401 - fixtures
    WINDOW,
    _Agenda,
    _appointments,
    _refuse,
    _turn,
    _wire,
    _world,
    db,
)

# --------------------------------------------------------------------------
# 1. Plugin tools
# --------------------------------------------------------------------------


async def _link_doctor_service(db, world):
    """The doctor's stored entry points at the canonical service (TASK-083 shape): the
    multi-doctor tool proves the service against THIS doctor's own stored catalog."""
    async with db() as session:
        doctor = await session.get(Professional, world["doctor"].id)
        doctor.appointment_types = [
            {"name": "Consulta", "duration_min": 30, "service_id": str(world["service"].id)}
        ]
        await session.commit()


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_create_event_for_professional_refuses_a_refused_cell(db, mode, monkeypatch):
    world = await _world(db, mode=mode)
    await _link_doctor_service(db, world)
    await _refuse(db, world)
    agenda = _Agenda()

    async def _calendar(tenant_id, professional):
        return agenda

    monkeypatch.setattr(mp, "_professional_calendar", _calendar)
    with _turn(world, agenda):
        refused = await mp.create_event_for_professional.ainvoke(
            {"professional_name": "Dra. Ana", "unit_name": "Centro", **WINDOW}
        )
    assert "convênio" in refused.get("error", ""), refused
    assert agenda.created == []
    assert await _appointments(db) == []


async def test_create_event_for_professional_books_without_exceptions(db, monkeypatch):
    world = await _world(db)
    await _link_doctor_service(db, world)
    agenda = _Agenda()

    async def _calendar(tenant_id, professional):
        return agenda

    monkeypatch.setattr(mp, "_professional_calendar", _calendar)
    with _turn(world, agenda):
        booked = await mp.create_event_for_professional.ainvoke(
            {"professional_name": "Dra. Ana", "unit_name": "Centro", **WINDOW}
        )
    assert "id" in booked, booked
    assert len(agenda.created) == 1


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_create_event_at_unit_refuses_a_refused_cell(db, mode):
    world = await _world(db, mode=mode)
    await _refuse(db, world)
    agenda = _Agenda()
    with _turn(world, agenda):
        refused = await mu.create_event_at_unit.ainvoke({"unit_name": "Centro", **WINDOW})
    assert "convênio" in refused.get("error", ""), refused
    assert agenda.created == []
    assert await _appointments(db) == []


async def test_create_event_at_unit_books_without_exceptions(db):
    world = await _world(db)
    agenda = _Agenda()
    with _turn(world, agenda):
        booked = await mu.create_event_at_unit.ainvoke({"unit_name": "Centro", **WINDOW})
    assert "id" in booked, booked
    assert len(agenda.created) == 1


# --------------------------------------------------------------------------
# 2. Same-named "Outro" plans of two doctors (independent mode)
# --------------------------------------------------------------------------


def _bare_tenant():
    return SimpleNamespace(
        initial_flows=None,
        appointment_types=[],
        appointment_duration_min=30,
        business_hours={},
        collect_insurance=True,
        insurance_mode="independent",
        insurances=[],
    )


async def test_same_named_outro_of_two_doctors_still_finds_the_refused_cell(db):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            insurance_mode="independent",
        )
        session.add(tenant)
        await session.flush()
        doc_a = Professional(id=uuid4(), tenant_id=tenant.id, name="Dr. A", is_active=True)
        doc_b = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. B", is_active=True)
        service = Service(tenant_id=tenant.id, name="Consulta", normalized_name="consulta")
        session.add_all([doc_a, doc_b, service])
        await session.flush()
        row_a = ProfessionalInsurancePlan(
            professional_id=doc_a.id, tenant_id=tenant.id, custom_name="GEAP"
        )
        row_b = ProfessionalInsurancePlan(
            professional_id=doc_b.id, tenant_id=tenant.id, custom_name="GEAP"
        )
        session.add_all([row_a, row_b])
        await session.flush()
        session.add(
            InsuranceAcceptanceException(
                tenant_id=tenant.id,
                professional_id=doc_b.id,
                service_id=service.id,
                unit_id=None,
                professional_plan_id=row_b.id,
            )
        )
        await session.commit()
        insurance = await load_tenant_insurance(session, tenant.id)

    snapshot = _flow_tenant_snapshot(_bare_tenant(), [], insurance=insurance)
    exc = snapshot_exceptions(snapshot)
    plan_id = fr._matrix_plan_id(snapshot, "GEAP")
    assert plan_id in {str(row_a.id), str(row_b.id)}
    # Whichever same-named id the answer resolved to, Dra. B's refused cell is found ...
    assert is_accepted(exc, doc_b.id, service.id, None, plan_id) is False
    assert is_accepted(exc, doc_b.id, service.id, None, str(row_a.id)) is False
    # ... and it never leaks to Dr. A.
    assert is_accepted(exc, doc_a.id, service.id, None, str(row_a.id)) is True
    assert is_accepted(exc, doc_a.id, service.id, None, str(row_b.id)) is True
