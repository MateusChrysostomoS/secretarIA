# ruff: noqa: F811
"""TASK-089: a professional who was just invited has nothing configured, so no patient is offered
them until the clinic gives them at least one service.

Reported on the test clinic (2026-11-21): a doctor invited by the manager, who had not even opened
the e-mail, already appeared in "escolher profissional". Cause: a new row had NULL
`appointment_types`, and NULL means "inherit the clinic's whole catalog".

Rule under test: a professional created into a clinic that ALREADY has an active professional
starts with an empty own service list (`[]`: no services; the service-first flow then never offers
them). The clinic's first professional - or one added to a clinic with nobody active - still
inherits, so a single-doctor clinic is untouched.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from sqlalchemy import update  # noqa: E402

from secretaria.models import Professional  # noqa: E402
from secretaria.services import provisioning  # noqa: E402
from secretaria.services.flow_router import _professionals_offering  # noqa: E402
from secretaria.services.service_catalog import professionals_offering  # noqa: E402
from tests._local_agenda import seed_professional, seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402, F401


async def _attach(db, tenant, name):
    async with db() as session:
        professional, created = await provisioning.create_or_attach_professional(
            session, tenant_id=tenant.id, name=name, specialty=None, about=None
        )
        await session.commit()
        return professional.id, created


async def _own_types(db, professional_id):
    async with db() as session:
        return (await session.get(Professional, professional_id)).appointment_types


async def test_the_first_professional_of_a_clinic_still_inherits_the_catalog(db):
    tenant = await seed_tenant(db)
    first, created = await _attach(db, tenant, "Dra. Dona")
    assert created
    assert await _own_types(db, first) is None


async def test_a_professional_invited_into_a_clinic_with_professionals_starts_with_no_services(db):
    tenant = await seed_tenant(db)
    await seed_professional(db, tenant, "Dra. Dona")
    invited, created = await _attach(db, tenant, "Dr. Convidado")
    assert created
    assert await _own_types(db, invited) == []


async def test_a_professional_added_when_every_other_one_is_inactive_still_inherits(db):
    tenant = await seed_tenant(db)
    gone = await seed_professional(db, tenant, "Dra. Antiga")
    async with db() as session:
        await session.execute(
            update(Professional).where(Professional.id == gone.id).values(is_active=False)
        )
        await session.commit()
    newcomer, created = await _attach(db, tenant, "Dr. Novo")
    assert created
    assert await _own_types(db, newcomer) is None


async def test_attaching_an_existing_professional_changes_nothing(db):
    tenant = await seed_tenant(db)
    owner = await seed_professional(db, tenant, "Dra. Dona")
    again, created = await _attach(db, tenant, "dra. dona")
    assert not created and again == owner.id
    assert await _own_types(db, owner.id) is None


async def test_the_service_flow_does_not_offer_a_professional_with_no_services(db):
    tenant = await seed_tenant(db)
    tenant.appointment_types = [{"name": "Consulta", "duration_min": 30, "is_active": True}]
    owner = await seed_professional(db, tenant, "Dra. Dona")  # NULL: inherits the clinic's catalog
    invited_id, _ = await _attach(db, tenant, "Dr. Convidado")
    async with db() as session:
        invited = await session.get(Professional, invited_id)
        offering = _professionals_offering(tenant, [owner, invited], "Consulta")
    assert [p.name for p in offering] == ["Dra. Dona"]


async def test_the_catalog_helpers_read_an_empty_own_list_as_no_services(db):
    tenant = await seed_tenant(db)
    tenant.appointment_types = [{"name": "Consulta", "duration_min": 30, "is_active": True}]
    owner = await seed_professional(db, tenant, "Dra. Dona")  # NULL: inherits
    invited_id, _ = await _attach(db, tenant, "Dr. Convidado")
    async with db() as session:
        invited = await session.get(Professional, invited_id)
    assert [p.name for p in professionals_offering("Consulta", [owner, invited], tenant, [])] == [
        "Dra. Dona"
    ]
    # remarcação ("Outro médico"): the newcomer is not a candidate, not even as the fallback
    from secretaria.services.flow_router import rebooking_candidates

    others, prefix = rebooking_candidates(
        tenant,
        [owner, invited],
        cancelled_professional_id=owner.id,
        service_name="Consulta",
        services=[],
    )
    assert others == [] and prefix is None
