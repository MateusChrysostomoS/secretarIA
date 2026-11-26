# ruff: noqa: F811
"""TASK-091 T1: a professional added to a clinic that already has professionals is born EMPTY and
MARKED (`requires_self_setup`), so the invited doctor configures themselves and nothing is
inherited from the clinic or from another doctor.

Born empty means, for the newcomer: no own services (`[]`), no own hours (`{}`: closed, NOT the
NULL that inherits the clinic's), no unit links, no own convênio list, and `requires_self_setup`.
The clinic's FIRST professional (and anyone created into a clinic with nobody active) keeps the
legacy behaviour untouched, and professionals that already exist are never flagged.

The hub's `POST /tenants/me/professionals` is the second door a newcomer can come through and must
obey the same rule as the invite (`provisioning.create_or_attach_professional`).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import UUID  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.models import Professional, ProfessionalUnit  # noqa: E402
from secretaria.services import provisioning  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    professional_appointment_types,
    professional_business_hours,
)
from tests._local_agenda import seed_professional, seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402, F401


async def _attach(db, tenant, name, **kwargs):
    async with db() as session:
        professional, created = await provisioning.create_or_attach_professional(
            session, tenant_id=tenant.id, name=name, specialty=None, about=None, **kwargs
        )
        await session.commit()
        return professional.id, created


async def _load(db, professional_id) -> Professional:
    async with db() as session:
        return await session.get(Professional, professional_id)


async def _unit_links(db, professional_id) -> int:
    async with db() as session:
        rows = await session.scalars(
            select(ProfessionalUnit).where(ProfessionalUnit.professional_id == professional_id)
        )
        return len(list(rows))


# --- the invite door (provisioning) ---------------------------------------------------------------


async def test_invited_professional_is_born_empty(db):
    tenant = await seed_tenant(db)
    await seed_professional(db, tenant, "Dra. Dona")
    invited_id, created = await _attach(db, tenant, "Dr. Convidado")
    assert created

    invited = await _load(db, invited_id)
    assert invited.appointment_types == []
    assert invited.business_hours == {}
    assert invited.insurance_plans_customized is False
    assert invited.requires_self_setup is True
    assert await _unit_links(db, invited_id) == 0
    # the resolvers read the empty own values verbatim: nothing is inherited from the clinic
    assert professional_appointment_types(invited, tenant) == []
    assert professional_business_hours(invited, tenant) == {}


async def test_the_first_professional_keeps_inheriting_and_is_not_flagged(db):
    tenant = await seed_tenant(db)
    first_id, created = await _attach(db, tenant, "Dra. Dona")
    assert created

    first = await _load(db, first_id)
    assert first.appointment_types is None
    assert first.business_hours is None
    assert first.requires_self_setup is False


async def test_a_professional_added_when_every_other_one_is_inactive_is_not_flagged(db):
    tenant = await seed_tenant(db)
    gone = await seed_professional(db, tenant, "Dra. Antiga")
    async with db() as session:
        await session.execute(
            update(Professional).where(Professional.id == gone.id).values(is_active=False)
        )
        await session.commit()
    newcomer_id, _ = await _attach(db, tenant, "Dr. Novo")

    newcomer = await _load(db, newcomer_id)
    assert newcomer.appointment_types is None
    assert newcomer.business_hours is None
    assert newcomer.requires_self_setup is False


async def test_explicit_invited_flag_overrides_the_clinic_state(db):
    tenant = await seed_tenant(db)
    invited_id, _ = await _attach(db, tenant, "Dr. Convidado", invited=True)
    assert (await _load(db, invited_id)).requires_self_setup is True

    other = await seed_tenant(db, name="Outra Clínica")
    await seed_professional(db, other, "Dra. Dona")
    not_invited_id, _ = await _attach(db, other, "Dr. Sócio", invited=False)
    not_invited = await _load(db, not_invited_id)
    assert not_invited.requires_self_setup is False
    assert not_invited.appointment_types is None


async def test_existing_professionals_unflagged(db):
    tenant = await seed_tenant(db)
    owner = await seed_professional(db, tenant, "Dra. Dona")
    assert (await _load(db, owner.id)).requires_self_setup is False
    # attaching by name to an existing professional changes nothing about them
    again_id, created = await _attach(db, tenant, "dra. dona")
    assert not created and again_id == owner.id
    assert (await _load(db, owner.id)).requires_self_setup is False


# --- the hub door (POST /tenants/me/professionals) ------------------------------------------------


@pytest.fixture
def hub_as(db):
    """Override the hub's session + tenant for ONE tenant (a real in-memory DB, no auth)."""
    from secretaria.main import app

    def _install(tenant):
        async def _session():
            async with db() as session:
                yield session

        async def _tenant():
            return tenant

        app.dependency_overrides[get_session] = _session
        app.dependency_overrides[get_current_tenant] = _tenant

    yield _install
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def test_hub_post_professional_with_invite_does_not_inherit(client, db, hub_as):
    tenant = await seed_tenant(db)
    await seed_professional(db, tenant, "Dra. Dona")
    hub_as(tenant)

    response = await client.post("/tenants/me/professionals", json={"name": "Dr. Convidado"})
    assert response.status_code == 201, response.text

    created = await _load(db, UUID(response.json()["id"]))
    assert created.appointment_types == []
    assert created.business_hours == {}
    assert created.insurance_plans_customized is False
    assert created.requires_self_setup is True
    assert await _unit_links(db, created.id) == 0


async def test_hub_post_into_a_clinic_without_professionals_keeps_legacy_inheritance(
    client, db, hub_as
):
    tenant = await seed_tenant(db)
    hub_as(tenant)

    response = await client.post("/tenants/me/professionals", json={"name": "Dra. Dona"})
    assert response.status_code == 201, response.text

    first = await _load(db, UUID(response.json()["id"]))
    assert first.appointment_types is None
    assert first.business_hours is None
    assert first.requires_self_setup is False
