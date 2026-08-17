"""Tests for PUT /tenants/me/configuration — the transactional tenant +
professional config save (api/hub/config.py::update_configuration).

The property that matters, and that the two-PUT design could not offer: either
BOTH scopes move or NEITHER does. Most of the tests below therefore assert on a
FRESH session after the request, not on the response body — a response can
describe an intention, only a re-read describes what was committed.

Same db/tenant/_override pattern as test_hub_config.py.
"""

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
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from secretaria.models.professional import Professional  # noqa: E402

CONFIGURATION = "/tenants/me/configuration"

HOURS_A = {"monday": [{"start": "08:00", "end": "12:00"}]}
HOURS_B = {"tuesday": [{"start": "14:00", "end": "18:00"}]}


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
    """Not connected and no Calendar — the state that proves a plain config
    save is never blocked by the activation gate."""
    async with db() as session:
        t = Tenant(
            id=uuid4(),
            clinic_name="Clinic A",
            phone_number_id=None,
            greeting_message="antes",
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest_asyncio.fixture
async def professional(db, tenant) -> Professional:
    async with db() as session:
        p = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dr. A",
            is_active=True,
            specialty="antes",
            business_hours=HOURS_A,
        )
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return p


@pytest_asyncio.fixture
async def other_tenant_professional(db) -> Professional:
    """A professional belonging to a DIFFERENT clinic — the cross-tenant probe."""
    async with db() as session:
        other = Tenant(id=uuid4(), clinic_name="Clinic B", phone_number_id=None)
        session.add(other)
        await session.flush()
        p = Professional(id=uuid4(), tenant_id=other.id, name="Dr. B", is_active=True)
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return p


@pytest.fixture(autouse=True)
def _override(db, tenant):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    # The route mutates the `tenant` ORM instance and then refreshes it, so it
    # must be the same instance/session the route's own Depends(get_session)
    # yields — exactly as in production, where FastAPI caches the dependency
    # within a request. Re-fetching by id through the overridden dependency
    # reproduces that.
    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _reload(db, tenant_id, professional_id=None):
    """Re-read from a FRESH session — the only honest way to ask what committed."""
    async with db() as session:
        t = await session.get(Tenant, tenant_id)
        p = await session.get(Professional, professional_id) if professional_id else None
        return t, p


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


async def test_saves_tenant_and_professional_in_one_call(
    client: AsyncClient, db, tenant, professional
) -> None:
    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {"greeting_message": "depois", "collect_insurance": True},
            "professional_id": str(professional.id),
            "professional": {"specialty": "depois", "business_hours": HOURS_B},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant"]["greeting_message"] == "depois"
    assert body["professional"]["specialty"] == "depois"

    t, p = await _reload(db, tenant.id, professional.id)
    assert t.greeting_message == "depois"
    assert t.collect_insurance is True
    assert p.specialty == "depois"
    assert p.business_hours == HOURS_B


async def test_response_matches_a_later_get(client: AsyncClient, db, tenant, professional) -> None:
    """The PUT response is built by the same readers the GETs use, so a client
    can hydrate straight from it without a follow-up round trip."""
    payload = {
        "tenant": {"greeting_message": "depois"},
        "professional_id": str(professional.id),
        "professional": {"specialty": "depois"},
    }
    await client.put(CONFIGURATION, json=payload)
    put_again = await client.put(CONFIGURATION, json=payload)
    get_tenant = await client.get("/tenants/me/config")
    get_professionals = await client.get("/tenants/me/professionals")

    assert put_again.json()["tenant"] == get_tenant.json()
    listed = [row for row in get_professionals.json() if row["id"] == str(professional.id)]
    assert put_again.json()["professional"] == listed[0]


async def test_tenant_only_patch_returns_null_professional(client: AsyncClient, db, tenant) -> None:
    response = await client.put(CONFIGURATION, json={"tenant": {"greeting_message": "só tenant"}})
    assert response.status_code == 200
    assert response.json()["professional"] is None

    t, _ = await _reload(db, tenant.id)
    assert t.greeting_message == "só tenant"


async def test_professional_only_patch_leaves_tenant_alone(
    client: AsyncClient, db, tenant, professional
) -> None:
    response = await client.put(
        CONFIGURATION,
        json={
            "professional_id": str(professional.id),
            "professional": {"specialty": "só profissional"},
        },
    )
    assert response.status_code == 200

    t, p = await _reload(db, tenant.id, professional.id)
    assert p.specialty == "só profissional"
    assert t.greeting_message == "antes"  # untouched


async def test_repeating_the_same_payload_converges(
    client: AsyncClient, db, tenant, professional
) -> None:
    """PUT is idempotent: a double click, or a retry after a timed-out
    response, must not produce a second, different state."""
    payload = {
        "tenant": {"greeting_message": "mesmo", "appointment_duration_min": 45},
        "professional_id": str(professional.id),
        "professional": {"specialty": "mesmo", "business_hours": HOURS_B},
    }
    first = await client.put(CONFIGURATION, json=payload)
    second = await client.put(CONFIGURATION, json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    t, p = await _reload(db, tenant.id, professional.id)
    assert (t.greeting_message, t.appointment_duration_min) == ("mesmo", 45)
    assert (p.specialty, p.business_hours) == ("mesmo", HOURS_B)

    # And exactly one professional row still exists for this tenant.
    async with db() as session:
        rows = list(
            await session.scalars(select(Professional).where(Professional.tenant_id == tenant.id))
        )
    assert len(rows) == 1


# --------------------------------------------------------------------------
# All-or-nothing — the reason this endpoint exists
# --------------------------------------------------------------------------


async def test_unknown_professional_leaves_tenant_unchanged(
    client: AsyncClient, db, tenant
) -> None:
    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {"greeting_message": "NAO DEVE SER GRAVADO"},
            "professional_id": str(uuid4()),
            "professional": {"specialty": "x"},
        },
    )
    assert response.status_code == 404

    t, _ = await _reload(db, tenant.id)
    assert t.greeting_message == "antes"


async def test_foreign_professional_is_404_and_changes_nothing(
    client: AsyncClient, db, tenant, other_tenant_professional
) -> None:
    """Cross-tenant probe: another clinic's professional is "not found", never
    a permission error, and the caller's own tenant row is untouched."""
    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {"greeting_message": "NAO DEVE SER GRAVADO"},
            "professional_id": str(other_tenant_professional.id),
            "professional": {"specialty": "invadido"},
        },
    )
    assert response.status_code == 404

    t, p = await _reload(db, tenant.id, other_tenant_professional.id)
    assert t.greeting_message == "antes"
    assert p.specialty is None  # the other clinic's row is untouched too


async def test_malformed_professional_id_is_404_not_500(client: AsyncClient, db, tenant) -> None:
    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {"greeting_message": "NAO DEVE SER GRAVADO"},
            "professional_id": "not-a-uuid",
            "professional": {"specialty": "x"},
        },
    )
    assert response.status_code == 404

    t, _ = await _reload(db, tenant.id)
    assert t.greeting_message == "antes"


async def test_invalid_professional_patch_rejects_whole_request(
    client: AsyncClient, db, tenant, professional
) -> None:
    """Pydantic rejects the body before the route runs, so the tenant half
    never even reaches the ORM."""
    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {"greeting_message": "NAO DEVE SER GRAVADO"},
            "professional_id": str(professional.id),
            "professional": {"business_hours": {"monday": [{"start": "18:00", "end": "08:00"}]}},
        },
    )
    assert response.status_code == 422

    t, p = await _reload(db, tenant.id, professional.id)
    assert t.greeting_message == "antes"
    assert p.business_hours == HOURS_A


async def test_blocked_activation_leaves_professional_unchanged(
    client: AsyncClient, db, tenant, professional
) -> None:
    """`is_active: true` fails the gate (no Calendar, no active type) — and the
    professional patch that travelled with it must not land either."""
    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {"is_active": True},
            "professional_id": str(professional.id),
            "professional": {"specialty": "NAO DEVE SER GRAVADO"},
        },
    )
    assert response.status_code == 422

    t, p = await _reload(db, tenant.id, professional.id)
    assert t.is_active is False
    assert p.specialty == "antes"


async def test_blocked_activation_also_discards_the_scalars_sent_with_it(
    client: AsyncClient, db, tenant, professional
) -> None:
    """The activation gate runs AFTER the patch is applied to the ORM object
    (it has to — see check_tenant_activation). This is the test that makes that
    safe: when the gate refuses, the scalar fields that were already assigned
    must be rolled back too, not left half-written."""
    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {"greeting_message": "NAO DEVE SER GRAVADO", "is_active": True},
            "professional_id": str(professional.id),
            "professional": {"specialty": "NAO DEVE SER GRAVADO"},
        },
    )
    assert response.status_code == 422

    t, p = await _reload(db, tenant.id, professional.id)
    assert t.greeting_message == "antes"
    assert t.is_active is False
    assert p.specialty == "antes"


async def test_activation_succeeds_when_the_same_request_supplies_prerequisites(
    client: AsyncClient, db, tenant, professional
) -> None:
    """The flip side: hours + services + `is_active: true` in ONE request is a
    legitimate go-live, and the gate must judge the tenant as patched rather
    than as it was before. Mirrors the legacy endpoint's own guarantee in
    tests/test_hub_config.py::test_put_activate_succeeds_once_prerequisites_met.

    The Calendar token is the one prerequisite a request cannot supply for
    itself (can_activate reads it from tenant_credentials), so it is seeded
    here exactly as the legacy test seeds it.
    """
    from secretaria.services.tenant_config import set_google_refresh_token

    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "refresh-token")
        await session.commit()

    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {
                "business_hours": HOURS_A,
                "appointment_types": [
                    {"name": "Consulta", "duration_min": 30, "is_active": True}
                ],
                "is_active": True,
            },
            "professional_id": str(professional.id),
            "professional": {"specialty": "junto"},
        },
    )
    assert response.status_code == 200

    t, p = await _reload(db, tenant.id, professional.id)
    assert t.is_active is True
    assert p.specialty == "junto"


async def test_failure_between_validation_and_commit_rolls_both_back(
    client: AsyncClient, db, tenant, professional, monkeypatch
) -> None:
    """The injected-failure smoke test: blow up AFTER the tenant has been
    mutated in the session but BEFORE the commit. Both scopes must survive
    untouched — this is the exact scenario the old two-PUT flow could not
    survive, because by this point it had already committed the tenant."""
    from secretaria.api.hub import config as config_module

    def _boom(*_args, **_kwargs):
        raise RuntimeError("injected failure before commit")

    monkeypatch.setattr(config_module.hubcfg, "apply_professional_config", _boom)

    with pytest.raises(RuntimeError):
        await client.put(
            CONFIGURATION,
            json={
                "tenant": {"greeting_message": "NAO DEVE SER GRAVADO"},
                "professional_id": str(professional.id),
                "professional": {"specialty": "NAO DEVE SER GRAVADO"},
            },
        )

    t, p = await _reload(db, tenant.id, professional.id)
    assert t.greeting_message == "antes"
    assert p.specialty == "antes"


# --------------------------------------------------------------------------
# Envelope validation
# --------------------------------------------------------------------------


async def test_unknown_top_level_key_is_rejected(client: AsyncClient, tenant) -> None:
    response = await client.put(
        CONFIGURATION,
        json={"tenant": {"greeting_message": "x"}, "professionalId": "camelCase"},
    )
    assert response.status_code == 422


async def test_professional_id_without_patch_is_rejected(
    client: AsyncClient, db, tenant, professional
) -> None:
    response = await client.put(
        CONFIGURATION,
        json={"tenant": {"greeting_message": "x"}, "professional_id": str(professional.id)},
    )
    assert response.status_code == 422

    t, _ = await _reload(db, tenant.id)
    assert t.greeting_message == "antes"


async def test_professional_patch_without_id_is_rejected(client: AsyncClient, tenant) -> None:
    response = await client.put(CONFIGURATION, json={"professional": {"specialty": "x"}})
    assert response.status_code == 422


async def test_empty_body_is_rejected_rather_than_a_fake_success(
    client: AsyncClient, tenant
) -> None:
    """A 200 on an empty body would read as "saved" to the caller."""
    response = await client.put(CONFIGURATION, json={})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# The legacy endpoints keep their exact behaviour during the rollout
# --------------------------------------------------------------------------


async def test_legacy_tenant_put_still_saves(client: AsyncClient, db, tenant) -> None:
    response = await client.put("/tenants/me/config", json={"greeting_message": "legado"})
    assert response.status_code == 200

    t, _ = await _reload(db, tenant.id)
    assert t.greeting_message == "legado"


async def test_legacy_and_aggregate_share_the_activation_gate(client: AsyncClient, tenant) -> None:
    """Same request, same refusal, same message — the two paths must not
    diverge on the one rule that can take a clinic live."""
    legacy = await client.put("/tenants/me/config", json={"is_active": True})
    aggregate = await client.put(CONFIGURATION, json={"tenant": {"is_active": True}})

    assert legacy.status_code == aggregate.status_code == 422
    assert legacy.json()["detail"] == aggregate.json()["detail"]


async def test_legacy_professional_put_still_saves(
    client: AsyncClient, db, tenant, professional
) -> None:
    response = await client.put(
        f"/tenants/me/professionals/{professional.id}/config",
        json={"specialty": "legado", "business_hours": HOURS_B},
    )
    assert response.status_code == 200

    _, p = await _reload(db, tenant.id, professional.id)
    assert p.specialty == "legado"
    assert p.business_hours == HOURS_B


async def test_legacy_professional_put_still_404s_across_tenants(
    client: AsyncClient, other_tenant_professional
) -> None:
    response = await client.put(
        f"/tenants/me/professionals/{other_tenant_professional.id}/config",
        json={"specialty": "invadido"},
    )
    assert response.status_code == 404
