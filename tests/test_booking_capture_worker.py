"""An enriched request must survive the flag-OFF worker and subsequent answers."""

import pytest
from sqlalchemy import select

from secretaria.models import Appointment, Conversation, Tenant
from secretaria.services import flow_router as fr
from tests.test_booking_draft_resume_questions import (
    DAY,
    _conversation,
    _hand_back,
    _onboard,
    _professional,
    _seed_tenant,
    _wa_turn,
    db as _db_fixture,
    wired as _wired_fixture,
)

db = _db_fixture
wired = _wired_fixture


async def _off(db, tenant):
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.initial_flows = {}
        await session.commit()
    tenant.initial_flows = {}


@pytest.mark.parametrize("known_attendee", [False, True])
async def test_flag_off_worker_preserves_explicit_day_and_self(wired, known_attendee):
    tenant = await _seed_tenant(wired, doctors=[
        ("Dr. Diogo", ["Cirurgia de Catarata"]),
        ("Dr. Rafael", ["Cirurgia de Catarata"]),
    ])
    await _off(wired, tenant)
    await _onboard(tenant)
    conv = await _conversation(wired, tenant)
    async with wired() as session:
        row = await session.get(Conversation, conv.id)
        row.flow_attendee_name = "" if known_attendee else None
        await session.commit()
    doctor = await _professional(wired, tenant, "Dr. Diogo")
    await _hand_back(wired, tenant, {
        "t": "Cirurgia de Catarata", "p": str(doctor.id), "i": "Particular",
        "w": "self", "d": DAY.isoformat(), "h": None,
    })
    current = await _conversation(wired, tenant)
    assert current.flow_step == fr.STEP_AWAITING_SLOT
    assert current.flow_selected_day == DAY.isoformat()
    assert current.flow_attendee_name == ""
    assert current.flow_selected_professional_id == doctor.id
    # Next turn: the model receives this selected day in ESTADO DA CONVERSA
    # and can hand back only the day plus the newly answered time.
    await _hand_back(wired, tenant, {"d": DAY.isoformat(), "h": "10:00"})
    current = await _conversation(wired, tenant)
    assert current.flow_step == fr.STEP_AWAITING_CONFIRMATION
    assert current.flow_selected_day == DAY.isoformat()
    assert current.flow_selected_slot == f"{DAY.isoformat()}T10:00"
    assert current.flow_selected_professional_id == doctor.id
    async with wired() as session:
        assert (await session.scalars(select(Appointment))).all() == []


async def test_flag_off_worker_does_not_lose_the_day_while_asking_the_doctor(wired):
    tenant = await _seed_tenant(wired, doctors=[
        ("Dra. Ana", ["Primeira Consulta"]), ("Dr. Beto", ["Primeira Consulta"]),
    ])
    await _off(wired, tenant)
    await _onboard(tenant)
    await _hand_back(wired, tenant, {
        "t": "Primeira Consulta", "w": "self", "d": DAY.isoformat(), "h": "10:00",
    })
    current = await _conversation(wired, tenant)
    assert current.flow_step == fr.STEP_AWAITING_PROFESSIONAL
    assert current.flow_draft["d"] == DAY.isoformat()
    await _wa_turn(tenant, "Dr. Beto")
    current = await _conversation(wired, tenant)
    assert current.flow_step == fr.STEP_AWAITING_CONFIRMATION
    assert current.flow_selected_slot == f"{DAY.isoformat()}T10:00"
    assert current.flow_draft is None
    async with wired() as session:
        assert (await session.scalars(select(Appointment))).all() == []


async def test_ambiguous_doctor_answer_preserves_preferences_until_an_explicit_choice(wired):
    tenant = await _seed_tenant(wired, doctors=[
        ("Dr. Diogo Raposo", ["Primeira Consulta"]),
        ("Dr. Diogo Silva", ["Primeira Consulta"]),
        ("Dr. Rafael", ["Primeira Consulta"]),
    ])
    await _off(wired, tenant)
    await _onboard(tenant)
    prior = await _professional(wired, tenant, "Dr. Rafael")
    conv = await _conversation(wired, tenant)
    async with wired() as session:
        row = await session.get(Conversation, conv.id)
        row.flow_attendee_name = ""
        row.flow_selected_professional_id = prior.id
        await session.commit()
    await _hand_back(wired, tenant, {
        "t": "Primeira Consulta", "w": "self", "d": DAY.isoformat(), "h": "10:00",
        "p_unresolved": True,
    })
    current = await _conversation(wired, tenant)
    assert current.flow_step == fr.STEP_AWAITING_PROFESSIONAL
    assert current.flow_draft["d"] == DAY.isoformat()
    assert current.flow_draft["h"] == "10:00"
    await _wa_turn(tenant, "Dr. Diogo Raposo")
    current = await _conversation(wired, tenant)
    chosen = await _professional(wired, tenant, "Dr. Diogo Raposo")
    assert current.flow_selected_professional_id == chosen.id
    assert current.flow_step == fr.STEP_AWAITING_CONFIRMATION
    assert current.flow_selected_slot == f"{DAY.isoformat()}T10:00"
    assert current.flow_draft is None
    async with wired() as session:
        assert (await session.scalars(select(Appointment))).all() == []
