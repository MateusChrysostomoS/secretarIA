# ruff: noqa: F811
"""Junção TASK-090 + TASK-091: which doctors a PATIENT is offered, and the clinic's shape.

TASK-090 Adendo 1 hides from NEW bookings a doctor whose unit links are all unusable (incomplete
address), but he still counts for the clinic's shape - one doctor vs several (owner ruling, C1).
TASK-091 hides a flagged invitee who is not ready to book, and he does NOT count: "one ready
doctor + one incomplete invitee" is still a single-doctor clinic. Together, one rule (pairs that
conflicted: booking_scope, flow_router, plugins/multi_professional):

* offered (doctor lists, taps, the AI's roster): every active doctor minus both;
* the shape (`booking_scope.topology_roster`): every active doctor minus the unready invitee;
* an existing consultation still finds its doctor among everyone (invitee and hidden included).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.plugins import multi_professional  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.booking_scope import (  # noqa: E402
    PatientRoster,
    booking_topology,
    everyone_of,
    sole_active_professional,
)
from secretaria.services.tenant_config import list_active_professionals  # noqa: E402
from secretaria.workers.shared.flow_simulation_input import load_simulation_inputs  # noqa: E402
from secretaria.workers.shared.patient_roster import build_patient_roster  # noqa: E402
from tests.test_unit_hidden_doctor_topology import (  # noqa: E402,F401
    _ids,
    _seed,
    _turn,
    _WireClient,
    db,
    wired,
)

# The REAL usable-unit rule (TASK-090 Adendo 1): Dra. Bia is linked only to an incomplete unit.
pytestmark = pytest.mark.real_unit_gate


def _doctor(name: str, *, blocked: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), name=name, booking_blocked=blocked)


def test_the_shape_counts_a_hidden_doctor_and_not_an_unready_invitee():
    ana, bia, ivo = _doctor("Ana"), _doctor("Bia"), _doctor("Ivo", blocked=True)
    roster = PatientRoster([ana], everyone=[ana, bia, ivo])  # Bia hidden by her units
    assert booking_topology(roster) == "multi"
    assert sole_active_professional(roster) is None
    assert fr._is_multi_professional(roster) is True


def test_one_ready_doctor_plus_an_unready_invitee_is_a_single_doctor_clinic():
    ana, ivo = _doctor("Ana"), _doctor("Ivo", blocked=True)
    for roster in (PatientRoster([ana], everyone=[ana, ivo]), [ana, ivo]):
        assert booking_topology(roster) == "sole"
        assert sole_active_professional(roster) is ana
        assert fr._is_multi_professional(roster) is False


def test_the_single_ready_doctor_owns_an_ownerless_consultation_and_the_booking():
    ana, ivo = _doctor("Ana"), _doctor("Ivo", blocked=True)
    roster = PatientRoster([ana], everyone=[ana, ivo])
    ownerless = {"professional_id": None, "unit_id": None}
    assert fr._appointment_owner_and_unit_id(ownerless, roster) == (ana, None)
    nobody_picked = SimpleNamespace(flow_selected_professional_id=None)
    assert fr._booking_professional(nobody_picked, roster) is ana


def test_an_existing_consultation_of_the_unready_invitee_still_finds_him():
    ana, ivo = _doctor("Ana"), _doctor("Ivo", blocked=True)
    assert fr._find_owner_by_id(PatientRoster([ana], everyone=[ana, ivo]), ivo.id) is ivo


async def _add_unready_invitee(db, tenant) -> Professional:
    """Dr. Ivo, a flagged invitee with a service but no hours and no unit: not ready to book."""
    async with db() as session:
        ivo = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dr. Ivo",
            is_active=True,
            requires_self_setup=True,
            appointment_types=[{"name": "Retina", "duration_min": 30, "is_active": True}],
            business_hours={},
        )
        session.add(ivo)
        await session.commit()
        return ivo


async def test_the_flow_roster_offers_neither_the_hidden_doctor_nor_the_unready_invitee(db):
    tenant, ana, bia, *_ = await _seed(db)
    ivo = await _add_unready_invitee(db, tenant)
    async with db() as session:
        rows = await list_active_professionals(session, tenant.id)
        clinic = await session.get(Tenant, tenant.id)
        roster = await build_patient_roster(session, clinic, rows, None)
    assert {p.id for p in roster} == {ana.id}
    assert {p.id for p in everyone_of(roster)} == {ana.id, bia.id, ivo.id}
    assert booking_topology(roster) == "multi"  # Ana + the hidden Bia; Ivo does not count


async def test_the_ai_roster_offers_neither_the_hidden_doctor_nor_the_unready_invitee(db):
    tenant, ana, bia, *_ = await _seed(db)
    ivo = await _add_unready_invitee(db, tenant)
    roster = await multi_professional._active_professionals(tenant.id)
    assert {p.id for p in roster} == {ana.id}
    assert {p.id for p in everyone_of(roster)} == {ana.id, bia.id, ivo.id}
    assert booking_topology(roster) == "multi"


async def test_the_patient_never_sees_the_hidden_doctor_nor_the_unready_invitee(db):
    tenant, ana, bia, *_ = await _seed(db, with_caio=True)
    ivo = await _add_unready_invitee(db, tenant)
    await _turn("Retina")
    kind, _body, rows = _WireClient.sends[-1]
    assert kind == "list"
    offered = _ids(rows)
    assert f"prof|{ana.id}" in offered
    assert f"prof|{bia.id}" not in offered
    assert f"prof|{ivo.id}" not in offered


async def test_the_doctors_testar_offers_neither_the_hidden_doctor_nor_the_unready_invitee(db):
    """The hub's "Testar" (flow simulation) shows the patient's flow: the same offer and shape."""
    tenant, ana, bia, *_ = await _seed(db)
    ivo = await _add_unready_invitee(db, tenant)
    async with db() as session:
        inputs = await load_simulation_inputs(session, await session.get(Tenant, tenant.id))
    assert {p.id for p in inputs.professionals} == {ana.id}
    assert {p.id for p in everyone_of(inputs.professionals)} == {ana.id, bia.id, ivo.id}
    assert booking_topology(inputs.professionals) == "multi"
