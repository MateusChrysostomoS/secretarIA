"""Tests for api/hub/units.py — units CRUD. Units are core (TASK-090): no add-on, no limit, so
every test here runs with get_entitlements wired to blow up if anything consults it.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Tenant, Unit  # noqa: E402
from secretaria.services import entitlements_client  # noqa: E402

ENDPOINT = "/tenants/me/units"


async def _never_called_fake(tenant_id, redis=None):
    raise AssertionError("units never consult entitlements (TASK-090)")


@pytest.fixture(autouse=True)
def _entitlements_never_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any entitlement fetch from these endpoints is a failure, whichever module imported it."""
    monkeypatch.setattr(entitlements_client, "get_entitlements", _never_called_fake)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    async with db() as session:
        t = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest.fixture(autouse=True)
def _override(db, tenant):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant():
        return tenant

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


def _addr(**overrides) -> dict:
    """A valid structured address (contract C1)."""
    parts = dict(
        cep="01310-100",
        street="Av. Paulista",
        number="1000",
        complement="sala 2",
        neighborhood="Bela Vista",
        city="São Paulo",
        state="SP",
    )
    parts.update(overrides)
    return parts


async def _seed_unit(db, tenant: Tenant, **overrides) -> Unit:
    fields = dict(tenant_id=tenant.id, name="Unidade Centro", address="Rua A, 1", is_active=True)
    fields.update(overrides)
    async with db() as session:
        unit = Unit(**fields)
        session.add(unit)
        await session.commit()
        await session.refresh(unit)
        return unit


# --------------------------------------------------------------------------
# GET — always allowed, no entitlement check
# --------------------------------------------------------------------------


async def test_list_is_allowed_without_entitlement_check(
    client: AsyncClient
) -> None:
    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    assert response.json() == []


async def test_list_shape_is_whitelisted(
    client: AsyncClient, db, tenant
) -> None:
    await _seed_unit(db, tenant, name="Unidade Centro", address="Rua A, 1")

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    row = response.json()[0]
    assert set(row.keys()) == {
        "id",
        "name",
        "address",
        "cep",
        "street",
        "number",
        "complement",
        "neighborhood",
        "city",
        "state",
        "address_incomplete",
        "is_active",
        "created_at",
        "professionals_count",
    }
    assert row["professionals_count"] == 0
    assert row["name"] == "Unidade Centro"
    assert row["address"] == "Rua A, 1"


# --------------------------------------------------------------------------
# POST — no add-on, no limit (TASK-090)
# --------------------------------------------------------------------------


async def test_create_unit_without_addon_succeeds(client: AsyncClient) -> None:
    # The clinic holds no `multi_unit` add-on (the autouse fixture makes any entitlement
    # fetch fail the test): creating a unit is simply allowed.
    response = await client.post(ENDPOINT, json={"name": "Unidade Centro", **_addr()})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Unidade Centro"
    assert body["is_active"] is True


async def test_create_unit_ignores_units_limit(
    client: AsyncClient, db, tenant
) -> None:
    # No `limits["units"]` is read at all (the autouse fixture fails on any entitlement fetch):
    # 2 active units already exist and a third and fourth are created.
    await _seed_unit(db, tenant, name="Existing 1", is_active=True)
    await _seed_unit(db, tenant, name="Existing 2", is_active=True)
    for name in ("Unidade Centro", "Unidade Norte"):
        response = await client.post(ENDPOINT, json={"name": name, **_addr()})
        assert response.status_code == 201, response.text
        assert response.json()["is_active"] is True
    async with db() as session:
        active = (await session.scalars(select(Unit).where(Unit.is_active.is_(True)))).all()
    assert len(active) == 4


async def test_create_inactive_unit_succeeds(client: AsyncClient) -> None:
    response = await client.post(
        ENDPOINT, json={"name": "Unidade Centro", **_addr(), "is_active": False}
    )
    assert response.status_code == 201
    assert response.json()["is_active"] is False


# --------------------------------------------------------------------------
# PATCH — nothing is gated
# --------------------------------------------------------------------------


async def test_patch_rename(client: AsyncClient, db, tenant) -> None:
    unit = await _seed_unit(db, tenant, name="Old Name", is_active=True)

    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"name": "New Name"})
    assert response.status_code == 200
    assert response.json()["name"] == "New Name"


async def test_patch_deactivate(client: AsyncClient, db, tenant) -> None:
    unit = await _seed_unit(db, tenant, is_active=True)

    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False


async def test_patch_activate_without_addon_or_limit_succeeds(
    client: AsyncClient, db, tenant
) -> None:
    await _seed_unit(db, tenant, name="Existing active", is_active=True)
    unit = await _seed_unit(db, tenant, name="To activate", is_active=False)

    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"is_active": True})
    assert response.status_code == 200
    assert response.json()["is_active"] is True


async def test_patch_unknown_id_is_404(client: AsyncClient) -> None:
    response = await client.patch(f"{ENDPOINT}/{uuid4()}", json={"name": "Ghost"})
    assert response.status_code == 404




# --------------------------------------------------------------------------
# Structured address (contract C1, TASK-090 B2)
# --------------------------------------------------------------------------

_ALL_REQUIRED = ["cep", "street", "number", "city", "state"]


async def test_create_unit_requires_structured_address_422(client: AsyncClient, db) -> None:
    # The old free-text `address` is ignored on input: it does not satisfy the requirement.
    response = await client.post(ENDPOINT, json={"name": "Unidade Centro", "address": "Rua A, 1"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "unit_address_invalid"
    assert isinstance(detail["message"], str) and detail["message"]
    assert detail["fields"] == _ALL_REQUIRED
    async with db() as session:
        assert (await session.scalars(select(Unit))).all() == []


async def test_create_unit_lists_only_the_bad_fields(client: AsyncClient) -> None:
    response = await client.post(
        ENDPOINT, json={"name": "U", **_addr(cep="123", state="XX", street="  ")}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["fields"] == ["cep", "street", "state"]


async def test_create_unit_derives_address(client: AsyncClient, db) -> None:
    response = await client.post(
        ENDPOINT,
        json={"name": "Unidade Centro", "address": "ignored text", **_addr(state="sp")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["address"] == "Av. Paulista, 1000 - sala 2, Bela Vista, São Paulo - SP, 01310-100"
    assert body["cep"] == "01310100"
    assert body["state"] == "SP"
    assert body["street"] == "Av. Paulista"
    assert body["address_incomplete"] is False
    async with db() as session:
        saved = await session.scalar(select(Unit))
        assert saved.address == body["address"]
        assert saved.cep == "01310100"


async def test_create_unit_without_optional_parts(client: AsyncClient) -> None:
    response = await client.post(
        ENDPOINT, json={"name": "U", **_addr(complement=None, neighborhood="")}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["address"] == "Av. Paulista, 1000, São Paulo - SP, 01310-100"
    assert body["complement"] is None
    assert body["neighborhood"] is None


async def test_patch_name_only_keeps_legacy_unit_valid(client: AsyncClient, db, tenant) -> None:
    unit = await _seed_unit(db, tenant, name="Legacy", address="Rua A, 1")
    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"name": "Legacy 2"})
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Legacy 2"
    assert body["address"] == "Rua A, 1"  # untouched legacy text
    assert body["address_incomplete"] is True
    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"is_active": False})
    assert response.status_code == 200
    assert response.json()["address"] == "Rua A, 1"


async def test_patch_address_key_alone_is_ignored(client: AsyncClient, db, tenant) -> None:
    unit = await _seed_unit(db, tenant, name="Legacy", address="Rua A, 1")
    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"address": "Other"})
    assert response.status_code == 200
    assert response.json()["address"] == "Rua A, 1"


async def test_patch_parts_revalidates_merged_result(client: AsyncClient, db, tenant) -> None:
    created = await client.post(ENDPOINT, json={"name": "U", **_addr()})
    unit_id = created.json()["id"]

    # A part that breaks the merged result is refused and nothing is saved.
    bad = await client.patch(f"{ENDPOINT}/{unit_id}", json={"name": "New", "state": "XX"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["fields"] == ["state"]
    bad = await client.patch(f"{ENDPOINT}/{unit_id}", json={"street": None})
    assert bad.status_code == 422
    assert bad.json()["detail"]["fields"] == ["street"]
    async with db() as session:
        saved = await session.scalar(select(Unit).where(Unit.id == UUID(unit_id)))
        assert saved.name == "U" and saved.state == "SP" and saved.street == "Av. Paulista"

    # A single valid part is merged with the existing ones and the text line is re-derived.
    ok = await client.patch(f"{ENDPOINT}/{unit_id}", json={"number": "2000", "cep": "20040-020"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["address"] == "Av. Paulista, 2000 - sala 2, Bela Vista, São Paulo - SP, 20040-020"
    assert body["number"] == "2000"
    assert body["city"] == "São Paulo"


async def test_patch_parts_on_legacy_unit_needs_all_required(
    client: AsyncClient, db, tenant
) -> None:
    unit = await _seed_unit(db, tenant, name="Legacy", address="Rua A, 1")
    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"cep": "01310-100"})
    assert response.status_code == 422
    assert response.json()["detail"]["fields"] == ["street", "number", "city", "state"]
    async with db() as session:
        assert (await session.get(Unit, unit.id)).address == "Rua A, 1"

    response = await client.patch(f"{ENDPOINT}/{unit.id}", json=_addr())
    assert response.status_code == 200
    assert response.json()["address_incomplete"] is False


async def test_unit_read_address_incomplete_flag(client: AsyncClient, db, tenant) -> None:
    await _seed_unit(db, tenant, name="A legacy", address="Rua A, 1")
    await _seed_unit(db, tenant, name="B complete", address="x", **{
        "cep": "01310100", "street": "Av. P", "number": "1", "city": "SP", "state": "SP",
    })
    await _seed_unit(db, tenant, name="C bad uf", address="x", **{
        "cep": "01310100", "street": "Av. P", "number": "1", "city": "SP", "state": "ZZ",
    })
    rows = {r["name"]: r for r in (await client.get(ENDPOINT)).json()}
    assert rows["A legacy"]["address_incomplete"] is True
    assert rows["B complete"]["address_incomplete"] is False
    assert rows["C bad uf"]["address_incomplete"] is True


async def test_patch_other_tenants_unit_is_404(client: AsyncClient, db) -> None:
    async with db() as session:
        other = Tenant(id=uuid4(), clinic_name="Other", phone_number_id=str(uuid4())[:12])
        session.add(other)
        await session.commit()
    foreign = await _seed_unit(db, other, name="Foreign", address="Rua Z, 9")
    response = await client.patch(f"{ENDPOINT}/{foreign.id}", json=_addr())
    assert response.status_code == 404
    async with db() as session:
        assert (await session.get(Unit, foreign.id)).address == "Rua Z, 9"
