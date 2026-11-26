# ruff: noqa: F811
"""TASK-091 T2: when is a professional "ready to book", and what is still missing.

The rule (one place: services/professional_readiness.py):
  servicos   - at least one active service of their own;
  horarios   - own weekly hours (the professional's, or hours of a unit they attend);
  unidades   - only when the clinic has ACTIVE units: at least one active link to an active unit;
  convenios  - only when `Tenant.insurance_mode == "independent"`: at least one plan of their own.
               In `shared` and `clinic_with_exceptions` the doctor inherits the clinic's list, so a
               convênio is never what is missing.

`booking_ready` is computed for EVERY professional, flagged or not; only `requires_self_setup`
decides whether it is used as a filter (T3).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import (  # noqa: E402
    Professional,
    ProfessionalInsurancePlan,
    ProfessionalUnit,
    Tenant,
    Unit,
)
from secretaria.services import hub_configuration as hubcfg  # noqa: E402
from secretaria.services.professional_readiness import (  # noqa: E402
    professional_readiness,
    professionals_readiness,
)
from tests._local_agenda import WEEK_HOURS, seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402, F401

SERVICES = [{"name": "Consulta", "duration_min": 30, "is_active": True}]
HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}


async def _tenant(db, *, mode: str | None = "shared") -> Tenant:
    tenant = await seed_tenant(db)
    async with db() as session:
        merged = await session.get(Tenant, tenant.id)
        merged.insurance_mode = mode
        await session.commit()
        await session.refresh(merged)
        return merged


async def _unit(db, tenant: Tenant, *, active: bool = True) -> Unit:
    async with db() as session:
        unit = Unit(id=uuid4(), tenant_id=tenant.id, name=f"Unidade {uuid4().hex[:4]}")
        unit.is_active = active
        session.add(unit)
        await session.commit()
        return unit


async def _professional(db, tenant: Tenant, **fields) -> Professional:
    values = dict(
        id=uuid4(),
        tenant_id=tenant.id,
        name="Dr. Convidado",
        is_active=True,
        requires_self_setup=True,
        appointment_types=SERVICES,
        business_hours=HOURS,
    )
    values.update(fields)
    async with db() as session:
        professional = Professional(**values)
        session.add(professional)
        await session.commit()
        return professional


async def _link(db, professional: Professional, unit: Unit, **fields) -> None:
    async with db() as session:
        session.add(ProfessionalUnit(professional_id=professional.id, unit_id=unit.id, **fields))
        await session.commit()


async def _own_plan(db, professional: Professional, tenant: Tenant) -> None:
    async with db() as session:
        session.add(
            ProfessionalInsurancePlan(
                professional_id=professional.id, tenant_id=tenant.id, custom_name="Unimed X"
            )
        )
        await session.commit()


async def _readiness(db, tenant, professional):
    async with db() as session:
        return await professional_readiness(session, tenant, professional)


# --- the four items ------------------------------------------------------------------------------


async def test_a_fully_configured_professional_is_ready(db):
    tenant = await _tenant(db)
    professional = await _professional(db, tenant)
    readiness = await _readiness(db, tenant, professional)
    assert readiness.ready is True
    assert readiness.missing == ()


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"appointment_types": []}, ["servicos"]),
        ({"business_hours": {}}, ["horarios"]),
        ({"appointment_types": [], "business_hours": {}}, ["servicos", "horarios"]),
        # a service that is switched off offers nothing
        (
            {"appointment_types": [{"name": "Consulta", "duration_min": 30, "is_active": False}]},
            ["servicos"],
        ),
        # every weekday closed is no hours at all
        ({"business_hours": {"monday": []}}, ["horarios"]),
    ],
)
async def test_each_missing_item_shows_up_in_missing(db, override, expected):
    tenant = await _tenant(db)
    professional = await _professional(db, tenant, **override)
    readiness = await _readiness(db, tenant, professional)
    assert readiness.ready is False
    assert list(readiness.missing) == expected


async def test_a_clinic_with_units_requires_an_active_link_to_an_active_unit(db):
    tenant = await _tenant(db)
    unit = await _unit(db, tenant)
    professional = await _professional(db, tenant)
    assert list((await _readiness(db, tenant, professional)).missing) == ["unidades"]

    await _link(db, professional, unit)
    assert (await _readiness(db, tenant, professional)).ready is True


async def test_a_link_that_is_switched_off_does_not_count(db):
    tenant = await _tenant(db)
    unit = await _unit(db, tenant)
    professional = await _professional(db, tenant)
    await _link(db, professional, unit, is_active=False)
    assert list((await _readiness(db, tenant, professional)).missing) == ["unidades"]


async def test_a_link_to_an_inactive_unit_does_not_count(db):
    tenant = await _tenant(db)
    await _unit(db, tenant)  # keeps the clinic "with units"
    gone = await _unit(db, tenant, active=False)
    professional = await _professional(db, tenant)
    await _link(db, professional, gone)
    assert list((await _readiness(db, tenant, professional)).missing) == ["unidades"]


async def test_a_clinic_without_units_never_asks_for_a_unit(db):
    tenant = await _tenant(db)
    await _unit(db, tenant, active=False)  # only an inactive unit: the clinic has no units
    professional = await _professional(db, tenant)
    assert (await _readiness(db, tenant, professional)).ready is True


async def test_hours_of_a_unit_they_attend_count_as_hours(db):
    # The professional's own hours are closed ({}), but the link carries its own schedule: patients
    # are offered those slots (services/professional_units.unit_hours), so nothing is missing.
    tenant = await _tenant(db)
    unit = await _unit(db, tenant)
    professional = await _professional(db, tenant, business_hours={})
    await _link(db, professional, unit, business_hours=HOURS)
    assert (await _readiness(db, tenant, professional)).ready is True


async def test_a_unit_link_with_empty_hours_is_not_hours(db):
    tenant = await _tenant(db)
    unit = await _unit(db, tenant)
    professional = await _professional(db, tenant, business_hours={})
    await _link(db, professional, unit, business_hours={})
    assert list((await _readiness(db, tenant, professional)).missing) == ["horarios"]


# --- convênios depend on the clinic's insurance mode -------------------------------------------


@pytest.mark.parametrize("mode", ["shared", "clinic_with_exceptions", None])
async def test_the_convenio_is_never_required_when_the_clinic_list_is_inherited(db, mode):
    tenant = await _tenant(db, mode=mode)
    professional = await _professional(db, tenant)
    assert (await _readiness(db, tenant, professional)).ready is True


async def test_independent_mode_requires_the_doctors_own_plan(db):
    tenant = await _tenant(db, mode="independent")
    professional = await _professional(db, tenant)
    assert list((await _readiness(db, tenant, professional)).missing) == ["convenios"]

    await _own_plan(db, professional, tenant)
    assert (await _readiness(db, tenant, professional)).ready is True


async def test_missing_keeps_a_stable_order(db):
    tenant = await _tenant(db, mode="independent")
    await _unit(db, tenant)
    professional = await _professional(db, tenant, appointment_types=[], business_hours={})
    readiness = await _readiness(db, tenant, professional)
    assert list(readiness.missing) == ["servicos", "horarios", "unidades", "convenios"]


# --- flagged or not -------------------------------------------------------------------------------


async def test_the_readiness_is_computed_for_an_unflagged_professional_too(db):
    tenant = await _tenant(db)
    inheriting = await _professional(
        db, tenant, requires_self_setup=False, appointment_types=None, business_hours=None
    )
    ready = await _readiness(db, tenant, inheriting)
    assert ready.ready is True  # the clinic's own catalog and hours are inherited

    empty_clinic = await seed_tenant(db, hours={}, name="Sem horário")
    own = await _professional(
        db, empty_clinic, requires_self_setup=False, appointment_types=None, business_hours=None
    )
    assert list((await _readiness(db, empty_clinic, own)).missing) == ["horarios"]


async def test_the_batch_and_the_single_readiness_agree(db):
    tenant = await _tenant(db, mode="independent")
    unit = await _unit(db, tenant)
    ready = await _professional(db, tenant, name="Pronto")
    await _link(db, ready, unit)
    await _own_plan(db, ready, tenant)
    bare = await _professional(db, tenant, name="Vazio", appointment_types=[], business_hours={})

    async with db() as session:
        batch = await professionals_readiness(session, tenant, [ready, bare])
        single_ready = await professional_readiness(session, tenant, ready)
        single_bare = await professional_readiness(session, tenant, bare)
    assert batch[ready.id] == single_ready and single_ready.ready is True
    assert batch[bare.id] == single_bare
    assert list(single_bare.missing) == ["servicos", "horarios", "unidades", "convenios"]


# --- the hub contract (ProfessionalListItem) ------------------------------------------------------


async def test_the_list_item_exposes_requires_self_setup_booking_ready_and_missing(db):
    tenant = await _tenant(db)
    bare = await _professional(db, tenant, appointment_types=[], business_hours={})
    async with db() as session:
        item = await hubcfg.professional_list_item(session, bare, tenant)
    assert item.requires_self_setup is True
    assert item.booking_ready is False
    assert item.missing == ["servicos", "horarios"]

    done = await _professional(db, tenant, name="Dra. Pronta")
    async with db() as session:
        item = await hubcfg.professional_list_item(session, done, tenant)
    assert item.requires_self_setup is True
    assert item.booking_ready is True
    assert item.missing == []


async def test_an_unflagged_professional_is_reported_unflagged(db):
    tenant = await _tenant(db)
    owner = await _professional(db, tenant, requires_self_setup=False, name="Dra. Dona")
    async with db() as session:
        item = await hubcfg.professional_list_item(session, owner, tenant)
    assert item.requires_self_setup is False
    assert item.booking_ready is True
    assert item.missing == []


def test_week_hours_fixture_is_a_non_empty_schedule():
    # guards the assumption the "inherits the clinic" cases above lean on
    assert WEEK_HOURS["monday"]
