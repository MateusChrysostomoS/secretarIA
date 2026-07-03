"""Tests for api/hub/deps.py:get_current_tenant — the doctor-hub auth gate.

Token verification itself is covered in test_subscription.py; here we only
check that get_current_tenant wires `core.subscription.verify_subscription_token`
correctly: 401 on a missing/failed/inactive claim, and resolving the claimed
tenant row when the claim is active. `verify_subscription_token` is
monkeypatched at the point deps.py imported it, so no HTTP call is ever made.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from secretaria.api.hub import deps as hub_deps  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import Tenant  # noqa: E402


def _make_tenant(**overrides: object) -> Tenant:
    fields: dict[str, object] = {
        "id": uuid4(),
        "clinic_name": "Clinic A",
        "phone_number_id": "111",
        "contact_email": "a@example.com",
        "language": "pt-BR",
        "timezone": "America/Sao_Paulo",
        "is_active": True,
        "precheck_enabled": False,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "google_calendar_id": "primary",
    }
    fields.update(overrides)
    return Tenant(**fields)


class _FakeSession:
    """A minimal session stub: session.get(Tenant, id) -> canned tenant or None."""

    def __init__(self, tenants_by_id: dict) -> None:
        self._tenants_by_id = tenants_by_id

    async def get(self, model: type, id_: object) -> object | None:
        assert model is Tenant
        return self._tenants_by_id.get(id_)


async def test_missing_bearer_token_is_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await hub_deps.get_current_tenant(authorization=None, session=_FakeSession({}))
    assert exc_info.value.status_code == 401


async def test_verification_failure_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_verify(token: str) -> SubscriptionClaim | None:
        return None

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _fake_verify)

    with pytest.raises(HTTPException) as exc_info:
        await hub_deps.get_current_tenant(authorization="Bearer whatever", session=_FakeSession({}))
    assert exc_info.value.status_code == 401


async def test_inactive_claim_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_verify(token: str) -> SubscriptionClaim | None:
        return SubscriptionClaim(tenant_id=uuid4(), active=False)

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _fake_verify)

    with pytest.raises(HTTPException) as exc_info:
        await hub_deps.get_current_tenant(authorization="Bearer whatever", session=_FakeSession({}))
    assert exc_info.value.status_code == 401


async def test_active_claim_resolves_the_claimed_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = _make_tenant()

    async def _fake_verify(token: str) -> SubscriptionClaim | None:
        assert token == "the-token"
        return SubscriptionClaim(tenant_id=tenant.id, active=True)

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _fake_verify)

    result = await hub_deps.get_current_tenant(
        authorization="Bearer the-token",
        session=_FakeSession({tenant.id: tenant}),
    )
    assert result is tenant


async def test_active_claim_with_unknown_tenant_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_verify(token: str) -> SubscriptionClaim | None:
        return SubscriptionClaim(tenant_id=uuid4(), active=True)

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _fake_verify)

    with pytest.raises(HTTPException) as exc_info:
        await hub_deps.get_current_tenant(authorization="Bearer whatever", session=_FakeSession({}))
    assert exc_info.value.status_code == 404
