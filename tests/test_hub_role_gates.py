"""TASK-020: the hub role gates, proved on a TEST-ONLY FastAPI app.

No production route uses `require_clinic_manager` / `require_clinic_clinician` yet (F1-F4
apply them), so the gates are exercised through three throwaway routes defined here.
`verify_subscription_token` is REAL: only the httpx client is faked (tests/hub_auth.py),
so parsing, the positive cache and the 401/403/404 ordering are the production paths.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from secretaria.api.hub import deps as hub_deps
from secretaria.api.hub.deps import (
    FORBIDDEN_ROLE,
    ROLE_CLAIM_MISSING,
    ensure_clinic_clinician,
    ensure_clinic_manager,
    get_current_tenant,
    get_hub_claim,
    require_clinic_clinician,
    require_clinic_manager,
)
from secretaria.core.database import get_session
from secretaria.core.subscription import SubscriptionClaim
from secretaria.models import Tenant
from tests.hub_auth import install_fake_brain_api, make_hub_token, reset_fake_brain_api


def _tenant(name: str) -> Tenant:
    return Tenant(
        id=uuid4(),
        clinic_name=name,
        phone_number_id=str(uuid4().int)[:12],
        contact_email=f"{name}@example.com",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        is_active=True,
        precheck_enabled=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        google_calendar_id="primary",
    )


class _FakeSession:
    """`session.get(Tenant, id)` -> canned tenant or None (the same stub test_hub_deps uses)."""

    def __init__(self, tenants: list[Tenant]) -> None:
        self._by_id = {t.id: t for t in tenants}

    async def get(self, model: type, id_: object) -> object | None:
        assert model is Tenant
        return self._by_id.get(id_)


class Rig:
    def __init__(self, client: AsyncClient, tenant_a: Tenant, tenant_b: Tenant) -> None:
        self.client = client
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.asked: list[str] = []


@pytest.fixture
async def rig(monkeypatch: pytest.MonkeyPatch):
    tenant_a, tenant_b = _tenant("clinic-a"), _tenant("clinic-b")
    session = _FakeSession([tenant_a, tenant_b])

    app = FastAPI()

    @app.get("/gate/manager")
    async def manager_only(tenant: Tenant = Depends(require_clinic_manager)) -> dict:
        return {"tenant_id": str(tenant.id)}

    @app.get("/gate/clinician")
    async def clinician_or_manager(tenant: Tenant = Depends(require_clinic_clinician)) -> dict:
        return {"tenant_id": str(tenant.id)}

    @app.get("/gate/open")
    async def any_authenticated(tenant: Tenant = Depends(get_current_tenant)) -> dict:
        return {"tenant_id": str(tenant.id)}

    @app.get("/gate/claim-only")
    async def claim_only(claim: SubscriptionClaim = Depends(get_hub_claim)) -> dict:
        return {"role": claim.role}

    app.dependency_overrides[get_session] = lambda: session
    asked = install_fake_brain_api(monkeypatch)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        the_rig = Rig(client, tenant_a, tenant_b)
        the_rig.asked = asked
        yield the_rig
    app.dependency_overrides.clear()
    reset_fake_brain_api()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# id, role, is_owner, is_manager, expected on /gate/manager, expected on /gate/clinician
MATRIX = [
    ("manager", "manager", False, False, "ok", "ok"),
    ("manager-with-flags", "manager", True, True, "ok", "ok"),
    ("doctor-plain", "doctor", False, False, FORBIDDEN_ROLE, "ok"),
    ("doctor-owner", "doctor", True, False, "ok", "ok"),
    ("doctor-manager-flag", "doctor", False, True, "ok", "ok"),
    ("doctor-owner-and-manager", "doctor", True, True, "ok", "ok"),
    ("secretary", "secretary", False, False, FORBIDDEN_ROLE, FORBIDDEN_ROLE),
    ("secretary-with-flags", "secretary", True, True, FORBIDDEN_ROLE, FORBIDDEN_ROLE),
    ("admin", "admin", False, False, FORBIDDEN_ROLE, FORBIDDEN_ROLE),
    ("admin-with-flags", "admin", True, True, FORBIDDEN_ROLE, FORBIDDEN_ROLE),
    ("legacy-tenant-owner", "tenant_owner", True, True, FORBIDDEN_ROLE, FORBIDDEN_ROLE),
    ("legacy-tenant-staff", "tenant_staff", False, False, FORBIDDEN_ROLE, FORBIDDEN_ROLE),
    ("unknown-role", "superadmin", True, True, FORBIDDEN_ROLE, FORBIDDEN_ROLE),
    ("wrong-case", "Manager", False, True, FORBIDDEN_ROLE, FORBIDDEN_ROLE),
]


def _assert_outcome(response, expected: str, tenant: Tenant) -> None:
    if expected == "ok":
        assert response.status_code == 200, response.text
        assert response.json() == {"tenant_id": str(tenant.id)}
        return
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["code"] == expected
    assert isinstance(detail["message"], str) and detail["message"]


@pytest.mark.parametrize(
    ("case_id", "role", "is_owner", "is_manager", "manager_expected", "clinician_expected"),
    MATRIX,
    ids=[row[0] for row in MATRIX],
)
async def test_role_matrix(
    rig: Rig, case_id, role, is_owner, is_manager, manager_expected, clinician_expected
) -> None:
    token = make_hub_token(
        tenant_id=rig.tenant_a.id, role=role, is_owner=is_owner, is_manager=is_manager
    )

    manager = await rig.client.get("/gate/manager", headers=_bearer(token))
    clinician = await rig.client.get("/gate/clinician", headers=_bearer(token))

    _assert_outcome(manager, manager_expected, rig.tenant_a)
    _assert_outcome(clinician, clinician_expected, rig.tenant_a)


@pytest.mark.parametrize("path", ["/gate/manager", "/gate/clinician"])
@pytest.mark.parametrize(
    "overrides",
    [
        {},  # role=None below: a brain-api that predates TASK-020
        {"role": ""},
        {"role": 123},
        {"role": ["manager"]},
        {"role": True},
        {"role": "m" * 33},
    ],
    ids=["no-role-keys", "empty", "int", "list", "bool", "too-long"],
)
async def test_missing_or_malformed_role_fails_closed(rig: Rig, path, overrides) -> None:
    """Flags never rescue a missing role: `is_owner`/`is_manager` are true on every case."""
    body = {"is_owner": True, "is_manager": True, **overrides}
    token = make_hub_token(tenant_id=rig.tenant_a.id, role=None, body_overrides=body)

    response = await rig.client.get(path, headers=_bearer(token))

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == ROLE_CLAIM_MISSING


@pytest.mark.parametrize("value", ["true", 1, "yes", [True]])
async def test_non_boolean_flags_do_not_make_a_doctor_a_manager(rig: Rig, value) -> None:
    token = make_hub_token(
        tenant_id=rig.tenant_a.id,
        role="doctor",
        body_overrides={"is_owner": value, "is_manager": value},
    )

    manager = await rig.client.get("/gate/manager", headers=_bearer(token))
    clinician = await rig.client.get("/gate/clinician", headers=_bearer(token))

    assert manager.status_code == 403
    assert manager.json()["detail"]["code"] == FORBIDDEN_ROLE
    assert clinician.status_code == 200


@pytest.mark.parametrize("path", ["/gate/manager", "/gate/clinician"])
async def test_no_token_and_bad_token_are_401_never_403(rig: Rig, path) -> None:
    assert (await rig.client.get(path)).status_code == 401
    assert (await rig.client.get(path, headers=_bearer("never-registered"))).status_code == 401


@pytest.mark.parametrize("path", ["/gate/manager", "/gate/clinician"])
async def test_inactive_subscription_is_401_even_for_a_manager(rig: Rig, path) -> None:
    token = make_hub_token(tenant_id=rig.tenant_a.id, role="manager", is_manager=True, active=False)

    response = await rig.client.get(path, headers=_bearer(token))

    assert response.status_code == 401


async def test_role_is_checked_before_the_tenant_lookup(rig: Rig) -> None:
    """A forbidden role on an unknown tenant is 403, not 404 (no tenant-existence leak)."""
    ghost = uuid4()
    forbidden = make_hub_token(tenant_id=ghost, role="secretary")
    allowed = make_hub_token(tenant_id=ghost, role="manager", is_manager=True)

    assert (await rig.client.get("/gate/manager", headers=_bearer(forbidden))).status_code == 403
    assert (await rig.client.get("/gate/manager", headers=_bearer(allowed))).status_code == 404


async def test_tenant_comes_only_from_the_verified_claim(rig: Rig) -> None:
    token = make_hub_token(tenant_id=rig.tenant_b.id, role="manager", is_manager=True)
    hostile = {**_bearer(token), "X-Tenant-Id": str(rig.tenant_a.id)}

    response = await rig.client.get(f"/gate/manager?tenant_id={rig.tenant_a.id}", headers=hostile)

    assert response.status_code == 200
    assert response.json() == {"tenant_id": str(rig.tenant_b.id)}


@pytest.mark.parametrize("role", [None, "secretary", "doctor", "manager", "admin"])
async def test_existing_gate_ignores_the_role(rig: Rig, role) -> None:
    """`get_current_tenant` (every existing hub route) accepts any authenticated caller,
    including an old-shape token with no role — the deploy-window guarantee."""
    token = make_hub_token(tenant_id=rig.tenant_a.id, role=role)

    response = await rig.client.get("/gate/open", headers=_bearer(token))

    assert response.status_code == 200
    assert response.json() == {"tenant_id": str(rig.tenant_a.id)}


async def test_get_hub_claim_authenticates_without_judging_the_role(rig: Rig) -> None:
    secretary = make_hub_token(tenant_id=rig.tenant_a.id, role="secretary")

    ok = await rig.client.get("/gate/claim-only", headers=_bearer(secretary))
    anonymous = await rig.client.get("/gate/claim-only")

    assert ok.status_code == 200 and ok.json() == {"role": "secretary"}
    assert anonymous.status_code == 401


def test_ensure_helpers_match_the_dependencies() -> None:
    tenant_id = uuid4()

    def claim(role, is_owner=False, is_manager=False) -> SubscriptionClaim:
        return SubscriptionClaim(
            tenant_id=tenant_id,
            active=True,
            role=role,
            is_owner=is_owner,
            is_manager=is_manager,
        )

    ensure_clinic_manager(claim("manager"))
    ensure_clinic_manager(claim("doctor", is_owner=True))
    ensure_clinic_clinician(claim("doctor"))
    for bad in (claim("doctor"), claim("secretary", True, True), claim("admin", True, True)):
        with pytest.raises(hub_deps.HTTPException) as denied:
            ensure_clinic_manager(bad)
        assert denied.value.status_code == 403
        assert denied.value.detail["code"] == FORBIDDEN_ROLE
    with pytest.raises(hub_deps.HTTPException) as missing:
        ensure_clinic_clinician(claim(None, True, True))
    assert missing.value.detail["code"] == ROLE_CLAIM_MISSING
