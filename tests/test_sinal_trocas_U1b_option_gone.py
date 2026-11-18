"""TASK-085 R U1b - the per-convênio "não cobra sinal" option is DELETED (owner, 2026-11-18).

Owner: "Não sabia que existia essa configuração de declarar um convênio como 'não cobra sinal'. Se
existir, apague e deixe todos os convênios cobrarem o sinal." U1 (D-R12) already stopped reading the
flag and kept a one-release compat (a constant `charge_deposit: true` on every plan read and a no-op
PATCH toggle). U1b removes every trace of the option from the code:

- no `charge_deposit` in any hub/agenda read, no PATCH toggle route, no toggle body schema;
- the ORM no longer maps the two legacy columns (they stay in the database for one more release -
  old API code running between the migration and the deploy still reads them - with their
  `server_default true`, so an insert by this code, which never names them, cannot fail);
- an older Portal that still SENDS `charge_deposit` is not refused: the key is ignored, while a
  typo'd REQUIRED field is still a 422.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

import pathlib  # noqa: E402
import re  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.models.insurance import (  # noqa: E402
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.schemas import insurance as insurance_schemas  # noqa: E402
from secretaria.schemas.calendar import CalendarInsurancePlanRead  # noqa: E402
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    ensure_catalog_seeded,
)
from tests.payments_support import make_sessionmaker  # noqa: E402

UNIMED = catalog_id_for_slug("unimed")
AMIL = catalog_id_for_slug("amil")
SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "secretaria"


# --------------------------------------------------------------------------
# Static: nothing in the code names the option any more
# --------------------------------------------------------------------------


def test_no_read_schema_carries_the_flag():
    assert "charge_deposit" not in insurance_schemas.TenantInsurancePlanRead.model_fields
    assert set(CalendarInsurancePlanRead.model_fields) == {"id", "name"}
    assert not hasattr(insurance_schemas, "TenantInsurancePlanDepositUpdate")


def test_no_input_schema_declares_the_flag():
    for schema in (
        insurance_schemas.TenantInsurancePlanIn,
        insurance_schemas.TenantInsurancePlanCustomCreate,
        insurance_schemas.ProfessionalInsuranceCustomCreate,
    ):
        assert "charge_deposit" not in schema.model_fields, schema.__name__


def test_the_orm_no_longer_maps_the_legacy_columns():
    assert "charge_deposit" not in TenantInsurancePlan.__table__.columns
    assert "charge_deposit" not in ProfessionalInsurancePlan.__table__.columns
    assert not hasattr(TenantInsurancePlan, "charge_deposit")
    assert not hasattr(ProfessionalInsurancePlan, "charge_deposit")


def test_no_compat_marker_and_no_flag_access_left_in_src():
    """The one-release compat is gone, and no code reads or declares the flag."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "D-R12 compat" in text:
            offenders.append(f"{path.name}: compat marker")
        # Attribute access, a field declaration or a keyword argument - never a mention in prose.
        if re.search(r"\.charge_deposit\b|\bcharge_deposit\s*[:=]", text):
            offenders.append(f"{path.name}: flag in code")
        if "TenantInsurancePlanDepositUpdate" in text:
            offenders.append(f"{path.name}: toggle schema")
    assert offenders == []


# --------------------------------------------------------------------------
# An older Portal still sending the flag is not refused; typos still are
# --------------------------------------------------------------------------


def test_an_old_clients_flag_is_ignored_on_every_input():
    plan = insurance_schemas.TenantInsurancePlanIn.model_validate(
        {"catalog_id": str(UNIMED), "charge_deposit": False}
    )
    assert plan.model_dump() == {"catalog_id": str(UNIMED)}
    clinic_other = insurance_schemas.TenantInsurancePlanCustomCreate.model_validate(
        {"custom_name": "GEAP", "custom_payment_note": "Na recepção.", "charge_deposit": False}
    )
    assert clinic_other.model_dump() == {
        "custom_name": "GEAP",
        "custom_payment_note": "Na recepção.",
    }
    doctor_other = insurance_schemas.ProfessionalInsuranceCustomCreate.model_validate(
        {"custom_name": "Plano", "custom_payment_note": "Nota.", "charge_deposit": True}
    )
    assert doctor_other.model_dump() == {"custom_name": "Plano", "custom_payment_note": "Nota."}


def test_a_typo_is_still_refused():
    with pytest.raises(ValidationError):
        insurance_schemas.TenantInsurancePlanIn.model_validate({"catalogId": str(UNIMED)})
    with pytest.raises(ValidationError):
        insurance_schemas.TenantInsurancePlanCustomCreate.model_validate(
            {"customName": "GEAP", "custom_payment_note": "x"}
        )
    with pytest.raises(ValidationError):
        # The wrapper stays strict: a misspelled `plans` would otherwise empty the clinic's list.
        insurance_schemas.TenantInsurancePlansUpdate.model_validate({"plan": []})


# --------------------------------------------------------------------------
# The hub over HTTP
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def hub(db):
    from fastapi import Depends
    from sqlalchemy.ext.asyncio import AsyncSession

    from secretaria.main import app

    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            pix_deposit_enabled=True,
            pix_deposit_percent=30,
            insurance_mode="shared",
        )
        session.add(tenant)
        await session.commit()
        tenant_id = tenant.id

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    yield tenant_id
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def test_hub_plans_no_longer_mention_the_flag_and_an_old_flag_is_ignored(
    client: AsyncClient, hub, db
):
    saved = await client.put(
        "/tenants/me/insurance-plans",
        json={"plans": [{"catalog_id": str(UNIMED), "charge_deposit": False}]},
    )
    assert saved.status_code == 200, saved.text
    custom = await client.post(
        "/tenants/me/insurance-plans/custom",
        json={
            "custom_name": "GEAP",
            "custom_payment_note": "Na recepção.",
            "charge_deposit": False,
        },
    )
    assert custom.status_code == 201, custom.text
    listed = await client.get("/tenants/me/insurance-plans")
    assert listed.status_code == 200
    for body in (saved.json(), [custom.json()], listed.json()):
        for plan in body:
            assert "charge_deposit" not in plan
    async with db() as session:
        names = (
            await session.scalars(
                select(TenantInsurancePlan.custom_name).where(TenantInsurancePlan.tenant_id == hub)
            )
        ).all()
    assert sorted(name or "" for name in names) == ["", "GEAP"]


async def test_hub_doctor_plans_no_longer_mention_the_flag(client: AsyncClient, hub, db):
    async with db() as session:
        tenant = await session.get(Tenant, hub)
        tenant.insurance_mode = "independent"
        doctor = Professional(id=uuid4(), tenant_id=hub, name="Dra. Ana", is_active=True)
        session.add(doctor)
        await session.commit()
    created = await client.post(
        f"/tenants/me/professionals/{doctor.id}/insurance-plans/custom",
        json={
            "custom_name": "Plano da Ana",
            "custom_payment_note": "Nota.",
            "charge_deposit": False,
        },
    )
    assert created.status_code in (200, 201), created.text
    got = await client.get(f"/tenants/me/professionals/{doctor.id}/insurance-plans")
    assert got.status_code == 200, got.text
    selectable = got.json()["selectable"]
    assert any(plan["name"] == "Plano da Ana" for plan in selectable)
    assert all("charge_deposit" not in plan for plan in selectable)


async def test_the_old_toggle_route_is_gone(client: AsyncClient, hub, db):
    async with db() as session:
        mine = TenantInsurancePlan(tenant_id=hub, catalog_id=UNIMED)
        session.add(mine)
        await session.commit()
    response = await client.patch(
        f"/tenants/me/insurance-plans/{mine.id}", json={"charge_deposit": False}
    )
    assert response.status_code in (404, 405), response.text
