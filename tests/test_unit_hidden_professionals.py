"""TASK-090 Adendo 1 - owner ruling on review finding I1 (2026-11-23).

A doctor who HAS unit links but none of them usable (every linked unit inactive or with an
incomplete address, or only inactive links) is NOT offered to patients at all: not in the
booking flow's roster (so neither a tap nor a typed name reaches him), not by the AI
professional / availability / draft tools, no booking path. A doctor with NO unit link keeps
the legacy "agenda without a unit". The manager views (`list_active_professionals`, the hub)
and the agendas of EXISTING consultations keep the full active roster.

Single source: `professional_units.patient_hidden_professional_ids` ->
`patient_visible_professionals` -> `booking_scope.PatientRoster`. All tests run the
REAL usable-unit rule (`real_unit_gate`): under the suite default (every active unit usable) a
doctor linked only to an incomplete unit would still be offered - the old behaviour these tests
must fail on.
"""

from uuid import uuid4

import pytest

from secretaria.ai import tools as ai_tools
from secretaria.core import database as core_database
from secretaria.models import Unit
from secretaria.models.professional_unit import ProfessionalUnit
from secretaria.plugins import multi_professional as mp
from secretaria.services import professional_units, unit_booking
from secretaria.services.tenant_config import list_active_professionals
from secretaria.workers.shared import unit_gate
from tests._local_agenda import seed_professional, seed_tenant, wire_local_agenda
from tests._reminder_fixtures import db  # noqa: F401 - fixture

pytestmark = pytest.mark.real_unit_gate

COMPLETE = {
    "cep": "13010100",
    "street": "Rua Barão de Jaguara",
    "number": "100",
    "city": "Campinas",
    "state": "SP",
}
INCOMPLETE = {"street": "Rua B", "city": "Campinas", "state": "SP"}


@pytest.fixture(autouse=True)
def _wired(db, monkeypatch):  # noqa: F811
    wire_local_agenda(monkeypatch, db)
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(unit_gate, "async_session_factory", db)


async def _unit(db, tenant, name, *, active=True, **parts):  # noqa: F811
    async with db() as session:
        unit = Unit(id=uuid4(), tenant_id=tenant.id, name=name, is_active=active, **parts)
        session.add(unit)
        await session.commit()
        return unit


async def _link(db, professional, unit, *, active=True):  # noqa: F811
    async with db() as session:
        session.add(
            ProfessionalUnit(
                professional_id=professional.id,
                unit_id=unit.id,
                business_hours=None,
                is_active=active,
                sort_order=0,
            )
        )
        await session.commit()


async def _world(db):  # noqa: F811
    """Sede (complete) + Nova (incomplete) + Velha (complete but deactivated); five doctors."""
    tenant = await seed_tenant(db)
    sede = await _unit(db, tenant, "Sede", **COMPLETE)
    nova = await _unit(db, tenant, "Nova", **INCOMPLETE)
    velha = await _unit(db, tenant, "Velha", active=False, **COMPLETE)
    ana = await seed_professional(db, tenant, "Dra. Ana")  # Sede: offered
    bia = await seed_professional(db, tenant, "Dra. Bia")  # Nova only: hidden
    caio = await seed_professional(db, tenant, "Dr. Caio")  # no link: legacy, offered
    davi = await seed_professional(db, tenant, "Dr. Davi")  # Velha only: hidden
    eva = await seed_professional(db, tenant, "Dra. Eva")  # Sede via an INACTIVE link: hidden
    await _link(db, ana, sede)
    await _link(db, ana, nova)
    await _link(db, bia, nova)
    await _link(db, davi, velha)
    await _link(db, eva, sede, active=False)
    return tenant, {"ana": ana, "bia": bia, "caio": caio, "davi": davi, "eva": eva}


async def test_only_doctors_with_a_usable_unit_or_no_link_are_offered(db) -> None:  # noqa: F811
    tenant, docs = await _world(db)
    async with db() as session:
        hidden = await professional_units.patient_hidden_professional_ids(
            session, tenant.id, [d.id for d in docs.values()]
        )
        manager_rows = await list_active_professionals(session, tenant.id)
        patient = [
            p.name
            for p in await professional_units.patient_visible_professionals(
                session, tenant.id, manager_rows
            )
        ]
        manager = [p.name for p in manager_rows]
    assert hidden == {docs["bia"].id, docs["davi"].id, docs["eva"].id}
    assert sorted(patient) == ["Dr. Caio", "Dra. Ana"]
    # The manager's roster (and the agendas of existing consultations) keep everybody.
    assert len(manager) == 5


async def test_the_ai_professional_tools_never_reach_a_hidden_doctor(db) -> None:  # noqa: F811
    tenant, _docs = await _world(db)
    token = ai_tools._tenant_id_ctx.set(tenant.id)
    try:
        listed = await mp.list_professionals.ainvoke({})
        picked = await mp.select_professional_and_continue.ainvoke(
            {"professional_name": "Dra. Bia"}
        )
        draft_id = await ai_tools._draft_professional_id(tenant.id, "Dra. Bia")
    finally:
        ai_tools._tenant_id_ctx.reset(token)
    assert sorted(p["name"] for p in listed["professionals"]) == ["Dr. Caio", "Dra. Ana"]
    assert "não encontrado" in picked["error"] and "Dra. Bia" not in picked["error"].split(":")[-1]
    assert draft_id is None


async def _turn_with_selected(db, monkeypatch, docs, tenant, **conversation_fields):  # noqa: F811
    """One main turn (LLM leg) of a conversation that had picked Dra. Bia; returns the roster
    the worker built and the doctor the agent was told is selected."""
    from secretaria.models import Conversation, FlowState, Patient
    from secretaria.workers import orchestrator, tasks
    from tests._patching import workers_ns
    from tests.test_bot_reply_gating import _LLM_ANSWER, _reply_context, _summary

    async with db() as session:
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id=f"55119{uuid4().int % 10**8}")
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_state=FlowState.LLM,
            flow_selected_professional_id=docs["bia"].id,  # picked before she was hidden
            **conversation_fields,
        )
        session.add(conversation)
        await session.commit()
    rosters: list = []
    original = orchestrator.build_patient_roster

    async def _spy(*args, **kwargs):
        roster = await original(*args, **kwargs)
        rosters.append(roster)
        return roster

    async def _entitled(_tenant_id, _redis):
        return _summary()

    selected: list = []

    async def _fake_run_agent(message, context, **kwargs):
        selected.append(getattr(kwargs.get("selected_professional"), "name", None))
        return "Oi!"

    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(orchestrator, "build_patient_roster", _spy)
    monkeypatch.setattr(workers_ns, "get_entitlements", _entitled)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)
    monkeypatch.setattr(workers_ns, "get_waba_token", lambda *_a: _none())
    await tasks._send_bot_reply(_reply_context(conversation, patient, _LLM_ANSWER))
    return rosters, selected


async def test_the_flow_roster_of_a_turn_offers_visible_doctors_and_knows_everyone(
    db, monkeypatch  # noqa: F811
) -> None:
    """The router's roster OFFERS only the visible doctors (a tap or a typed name can only
    match them), while `everyone` keeps all five for the topology and existing consultations.
    A hidden doctor picked for a NEW booking is not resumed (review M2)."""
    tenant, docs = await _world(db)
    rosters, selected = await _turn_with_selected(db, monkeypatch, docs, tenant)
    [roster] = rosters
    assert sorted(p.name for p in roster) == ["Dr. Caio", "Dra. Ana"]
    assert len(roster.everyone) == 5
    assert selected == [None]


async def test_a_hidden_doctor_is_resumed_while_his_own_consultation_is_managed(
    db, monkeypatch  # noqa: F811
) -> None:
    """Owner ruling on I1: hidden = not offered for NEW bookings only. Managing his existing
    consultation keeps him (his agenda, services and units)."""
    tenant, docs = await _world(db)
    _rosters, selected = await _turn_with_selected(
        db, monkeypatch, docs, tenant, flow_managing_appointment_id=uuid4()
    )
    assert selected == ["Dra. Bia"]


async def _none():
    return None


async def test_a_clinic_whose_every_doctor_is_hidden_is_not_served(db) -> None:  # noqa: F811
    tenant = await seed_tenant(db)
    await _unit(db, tenant, "Sede", **COMPLETE)  # usable, but nobody attends there
    nova = await _unit(db, tenant, "Nova", **INCOMPLETE)
    bia = await seed_professional(db, tenant, "Dra. Bia")
    await _link(db, bia, nova)
    async with db() as session:
        assert await professional_units.has_usable_units(session, tenant.id)
        assert not await professional_units.clinic_servable(session, tenant.id)
    assert await unit_gate.reply_blocked_no_usable_unit(tenant.id)
    # Completing Nova brings her (and the clinic) back.
    async with db() as session:
        row = await session.get(Unit, nova.id)
        row.cep, row.number = "13010100", "1"
        await session.commit()
        assert await professional_units.clinic_servable(session, tenant.id)


async def test_a_clinic_without_doctors_needs_only_the_usable_unit(db) -> None:  # noqa: F811
    tenant = await seed_tenant(db)
    await _unit(db, tenant, "Sede", **COMPLETE)
    async with db() as session:
        assert await professional_units.clinic_servable(session, tenant.id)


async def test_check_unit_refuses_an_incomplete_unit_only_for_the_patient(db) -> None:  # noqa: F811
    tenant, docs = await _world(db)
    async with db() as session:
        nova = (
            await session.execute(Unit.__table__.select().where(Unit.name == "Nova"))
        ).first()
        staff = await unit_booking.check_unit(session, tenant.id, docs["ana"], nova.id)
        assert staff.id == nova.id
        with pytest.raises(unit_booking.UnitRefused) as refused:
            await unit_booking.check_unit(
                session, tenant.id, docs["ana"], nova.id, for_patient=True
            )
    assert refused.value.code == "unit_address_incomplete"
    assert refused.value.status_code == 422
