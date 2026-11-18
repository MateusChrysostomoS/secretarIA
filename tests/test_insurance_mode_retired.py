# ruff: noqa: F811
"""TASK-086 T5: the 3rd insurance mode can no longer be chosen; tenants in it become `shared`."""

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.operations import Operations  # noqa: E402

from secretaria.api.hub import deps as hub_deps  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from secretaria.models.insurance import INSURANCE_MODES_SELECTABLE  # noqa: E402
from tests._local_agenda import seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

HEADERS = {"authorization": "Bearer retired-mode-token"}
PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "e5a9c3b7d1f2_retire_clinic_with_exceptions_mode.py"
)


@pytest_asyncio.fixture
async def world(db, monkeypatch):
    from secretaria.main import app

    tenant = await seed_tenant(db, name="Clinica R")

    async def _session():
        async with db() as session:
            yield session

    async def _verify(token, *, fresh=False):
        return SubscriptionClaim(
            tenant_id=tenant.id,
            active=True,
            role="manager",
            is_owner=False,
            is_manager=True,
            professional_id=None,
        )

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    yield {"db": db, "tenant": tenant}
    app.dependency_overrides.pop(get_session, None)


def test_selectable_modes_are_two():
    assert INSURANCE_MODES_SELECTABLE == ("shared", "independent")


@pytest.mark.asyncio
async def test_api_refuses_the_third_mode_and_keeps_the_current_one(client, world):
    ok = await client.put("/tenants/me/insurance-mode", headers=HEADERS, json={"mode": "shared"})
    assert ok.status_code == 200
    refused = await client.put(
        "/tenants/me/insurance-mode", headers=HEADERS, json={"mode": "clinic_with_exceptions"}
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "invalid_insurance_mode"
    still = await client.get("/tenants/me/insurance-mode", headers=HEADERS)
    assert still.json()["mode"] == "shared"
    other = await client.put(
        "/tenants/me/insurance-mode", headers=HEADERS, json={"mode": "independent"}
    )
    assert other.status_code == 200


@pytest.mark.asyncio
async def test_migration_converts_only_the_third_mode(db):
    async with db() as session:
        tenants = {}
        for key, mode in {
            "a": "clinic_with_exceptions",
            "b": "shared",
            "c": "independent",
            "d": None,
        }.items():
            t = await seed_tenant(db, name=f"T-{key}")
            row = await session.get(Tenant, t.id)
            row.insurance_mode = mode
            tenants[key] = t.id
        await session.commit()

    spec = importlib.util.spec_from_file_location("retire_mode_migration", PATH)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    def run(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            mig.upgrade()
            mig.downgrade()  # documented no-op: must not raise or change anything
            mig.upgrade()

    async with db() as session:
        conn = await session.connection()
        await conn.run_sync(run)
        await session.commit()
    async with db() as session:
        got = {k: (await session.get(Tenant, i)).insurance_mode for k, i in tenants.items()}
    assert got == {"a": "shared", "b": "shared", "c": "independent", "d": None}
