"""The clinic's standing authorisation for paid notices on the hub config (TASK-032 R7)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402, F401

CONFIG = "/tenants/me/config"
CONFIGURATION = "/tenants/me/configuration"


@pytest_asyncio.fixture
async def clinic(db) -> Tenant:  # noqa: F811
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clínica", phone_number_id=None)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


@pytest.fixture(autouse=True)
def _override(db, clinic):  # noqa: F811
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, clinic.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _stored(db, tenant_id) -> bool:  # noqa: F811
    async with db() as session:
        return (await session.get(Tenant, tenant_id)).paid_notices_auto_approved


async def test_get_config_exposes_the_flag_off_by_default(client: AsyncClient):
    body = (await client.get(CONFIG)).json()
    assert body["paid_notices_auto_approved"] is False


async def test_the_configuration_save_r5_uses_turns_it_on_and_echoes_it(
    client: AsyncClient,
    db,  # noqa: F811
    clinic,  # noqa: F811
):
    response = await client.put(
        CONFIGURATION, json={"tenant": {"paid_notices_auto_approved": True}}
    )

    assert response.status_code == 200, response.text
    assert response.json()["tenant"]["paid_notices_auto_approved"] is True
    assert await _stored(db, clinic.id) is True
    assert (await client.get(CONFIG)).json()["paid_notices_auto_approved"] is True


async def test_the_legacy_config_save_turns_it_back_off(client: AsyncClient, db, clinic):  # noqa: F811
    await client.put(CONFIGURATION, json={"tenant": {"paid_notices_auto_approved": True}})

    response = await client.put(CONFIG, json={"paid_notices_auto_approved": False})

    assert response.status_code == 200
    assert response.json()["paid_notices_auto_approved"] is False
    assert await _stored(db, clinic.id) is False


async def test_a_save_that_omits_it_leaves_it_untouched(client: AsyncClient, db, clinic):  # noqa: F811
    await client.put(CONFIGURATION, json={"tenant": {"paid_notices_auto_approved": True}})

    response = await client.put(CONFIGURATION, json={"tenant": {"persona_notes": "Gentil"}})

    assert response.status_code == 200
    assert await _stored(db, clinic.id) is True


async def test_null_is_refused_and_nothing_changes(client: AsyncClient, db, clinic):  # noqa: F811
    response = await client.put(
        CONFIGURATION, json={"tenant": {"paid_notices_auto_approved": None}}
    )

    assert response.status_code == 422
    assert await _stored(db, clinic.id) is False
