# ruff: noqa: F811
"""TASK-091 T3: the patient only sees an invited professional once they are ready to book.

`booking_blocked` is the flag the roster snapshots carry: set (by `list_active_professionals`) on a
professional marked `requires_self_setup` whose readiness (T2) is not complete. The pure router
filters on it at every point where the PATIENT CHOOSES - the generic doctor list, the doctors of a
service, "Outro médico", the "several doctors" decision, the clinic's service union - and nowhere
else: looking a doctor up by id (an existing appointment, a rebooking that keeps the doctor) still
finds a blocked one, so nothing already booked breaks.
"""

import os
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from sqlalchemy import update  # noqa: E402

from secretaria.models import FlowState, Professional  # noqa: E402
from secretaria.services import flow_router  # noqa: E402
from secretaria.services.booking_scope import (  # noqa: E402
    resolve_booking_owner_id,
    sole_active_professional,
)
from secretaria.services.flow_router import (  # noqa: E402
    _enter_professional_list,
    _enter_service_professional_list,
    _find_professional_by_id,
    _is_multi_professional,
    rebooking_candidates,
    route,
)
from secretaria.services.professional_readiness import list_bookable_professionals  # noqa: E402
from secretaria.services.tenant_config import list_active_professionals  # noqa: E402
from tests._local_agenda import seed_professional, seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402, F401

HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}
SERVICES = [{"name": "Consulta", "duration_min": 30, "is_active": True, "sort_order": 0}]


def _tenant():
    return SimpleNamespace(
        initial_flows={
            "enabled": True,
            "buttons": ["Serviços e Custo", "Horários", "Outro"],
            "menu_label": "Como posso ajudar?",
        },
        appointment_types=SERVICES,
        appointment_duration_min=30,
        business_hours=HOURS,
        collect_insurance=False,
        insurance_mode="shared",
        insurances=None,
    )


def _doctor(name, *, blocked=False, services=SERVICES):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty="Clínica",
        about=None,
        appointment_types=list(services),
        business_hours=HOURS,
        booking_blocked=blocked,
    )


def _conversation(**kw):
    base = dict(
        flow_state=FlowState.IDLE,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _row_titles(result) -> list[str]:
    return [row[1] for bubble in result.bubbles for row in getattr(bubble, "rows", [])]


# --- the choice points ---------------------------------------------------------------------


def test_the_generic_doctor_list_hides_a_blocked_invitee():
    owner, invitee = _doctor("Dra. Dona"), _doctor("Dr. Convidado", blocked=True)
    shown = _row_titles(_enter_professional_list(_tenant(), [owner, invitee]))
    assert any("Dona" in t for t in shown)
    assert not any("Convidado" in t for t in shown)


def test_the_doctors_of_a_service_hide_a_blocked_invitee_even_as_the_fallback():
    owner, invitee = _doctor("Dra. Dona"), _doctor("Dr. Convidado", blocked=True)
    shown = _row_titles(_enter_service_professional_list(_tenant(), [owner, invitee], "Consulta"))
    assert any("Dona" in t for t in shown) and not any("Convidado" in t for t in shown)

    # nobody visible does the service: the "everyone" fallback still never includes the invitee
    other = _doctor(
        "Dra. Outra", services=[{"name": "Exame", "duration_min": 20, "is_active": True}]
    )
    fallback = _row_titles(
        _enter_service_professional_list(_tenant(), [other, invitee], "Consulta")
    )
    assert not any("Convidado" in t for t in fallback)


def test_other_doctor_on_rebooking_never_offers_a_blocked_invitee():
    owner, second = _doctor("Dra. Dona"), _doctor("Dra. Segunda")
    invitee = _doctor("Dr. Convidado", blocked=True)
    others, prefix = rebooking_candidates(
        _tenant(),
        [owner, second, invitee],
        cancelled_professional_id=owner.id,
        service_name="Consulta",
        services=[],
    )
    assert [p.name for p in others] == ["Dra. Segunda"] and prefix is None

    # nobody else offers it: the full-roster fallback excludes the blocked one as well
    only_invitee, _ = rebooking_candidates(
        _tenant(),
        [owner, invitee],
        cancelled_professional_id=owner.id,
        service_name="Consulta",
        services=[],
    )
    assert only_invitee == []


def test_one_doctor_plus_a_blocked_invitee_is_still_a_single_doctor_clinic():
    owner, invitee = _doctor("Dra. Dona"), _doctor("Dr. Convidado", blocked=True)
    assert _is_multi_professional([owner, invitee]) is False
    assert _is_multi_professional([owner, _doctor("Dra. Segunda")]) is True
    assert sole_active_professional([owner, invitee]) is owner
    assert resolve_booking_owner_id([owner, invitee], None) == owner.id


async def test_the_booking_entry_with_one_doctor_and_a_blocked_invitee_is_byte_identical():
    owner, invitee = _doctor("Dra. Dona"), _doctor("Dr. Convidado", blocked=True)
    for body in ("Serviços e Custo", "Horários", "Agendar", "oi"):
        with_invitee = await route(
            _conversation(), _tenant(), None, body, professionals=[owner, invitee]
        )
        alone = await route(_conversation(), _tenant(), None, body, professionals=[owner])
        assert with_invitee == alone, body


def test_the_clinic_service_catalog_hides_a_blocked_invitees_services():
    owner = _doctor("Dra. Dona")
    invitee = _doctor(
        "Dr. Convidado",
        blocked=True,
        services=[{"name": "Só do convidado", "duration_min": 20, "is_active": True}],
    )
    names = [s["name"] for s in flow_router._clinic_service_catalog(_tenant(), [owner, invitee])]
    assert names == ["Consulta"]


def test_an_existing_appointment_with_a_blocked_doctor_still_resolves_that_doctor():
    # The lookups are NOT choice points: a patient already booked with the doctor keeps them when
    # they reschedule ("Outro horário"), whatever the doctor's readiness is now.
    owner, invitee = _doctor("Dra. Dona"), _doctor("Dr. Convidado", blocked=True)
    roster = [owner, invitee]
    assert _find_professional_by_id(roster, invitee.id) is invitee
    assert resolve_booking_owner_id(roster, invitee.id) == invitee.id


# --- the roster from the database ----------------------------------------------------------


async def _invite(db, tenant, **fields):
    values = dict(
        name="Dr. Convidado",
        appointment_types=[],
        business_hours={},
        requires_self_setup=True,
    )
    values.update(fields)
    async with db() as session:
        professional = Professional(tenant_id=tenant.id, is_active=True, **values)
        session.add(professional)
        await session.commit()
        return professional


async def test_an_incomplete_invitee_is_blocked_and_a_complete_one_is_not(db):
    tenant = await seed_tenant(db)
    owner = await seed_professional(db, tenant, "Dra. Dona")
    invitee = await _invite(db, tenant)

    async with db() as session:
        roster = await list_active_professionals(session, tenant.id)
        blocked = {p.name: p.booking_blocked for p in roster}
        assert blocked == {"Dr. Convidado": True, "Dra. Dona": False}
        assert [p.id for p in await list_bookable_professionals(session, tenant.id)] == [owner.id]
        # one ready doctor + one incomplete invitee: the clinic is still single-doctor
        assert sole_active_professional(roster).id == owner.id

    async with db() as session:
        await session.execute(
            update(Professional)
            .where(Professional.id == invitee.id)
            .values(appointment_types=SERVICES, business_hours=HOURS)
        )
        await session.commit()
    async with db() as session:
        bookable = await list_bookable_professionals(session, tenant.id)
        assert {p.name for p in bookable} == {"Dra. Dona", "Dr. Convidado"}
        assert all(p.booking_blocked is False for p in bookable)


async def test_only_flagged_professionals_are_ever_blocked(db):
    # An unflagged professional with nothing configured keeps behaving exactly as before: the
    # filter is not applied to them, whatever their readiness says.
    tenant = await seed_tenant(db, hours={})
    bare = await _invite(
        db,
        tenant,
        name="Dra. Antiga",
        requires_self_setup=False,
        appointment_types=None,
        business_hours=None,
    )
    async with db() as session:
        roster = await list_active_professionals(session, tenant.id)
        assert [p.booking_blocked for p in roster] == [False]
        assert [p.id for p in await list_bookable_professionals(session, tenant.id)] == [bare.id]


async def test_the_flow_snapshot_carries_the_flag(db):
    from secretaria.workers.shared.greeting import _flow_professionals

    tenant = await seed_tenant(db)
    await seed_professional(db, tenant, "Dra. Dona")
    await _invite(db, tenant)
    async with db() as session:
        roster = await list_active_professionals(session, tenant.id)
        snapshots = _flow_professionals(roster, None)
    assert {s.name: s.booking_blocked for s in snapshots} == {
        "Dr. Convidado": True,
        "Dra. Dona": False,
    }
