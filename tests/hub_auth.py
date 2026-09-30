"""Test helpers that stand in for brain-api's hub-token introspection (TASK-020).

secretarIA never decodes a hub token; it asks brain-api (`POST
/internal/secretaria/hub-token/verify`). So a "hub token" here is an opaque string plus the
introspection body brain-api would answer for it. `make_hub_token` registers that body;
`install_fake_brain_api` patches only the httpx client, so the REAL
`verify_subscription_token` (settings, parsing, positive cache) runs on the fake answer.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest

from secretaria.config import get_settings
from secretaria.core import subscription

_BODIES: dict[str, dict[str, Any]] = {}


def make_hub_token(
    *,
    tenant_id: UUID,
    role: str | None = "doctor",
    is_owner: bool = False,
    is_manager: bool = False,
    active: bool = True,
    body_overrides: dict[str, Any] | None = None,
) -> str:
    """An opaque hub token whose introspection body carries the given role claims.

    `role=None` reproduces a brain-api that predates TASK-020 (no role keys at all).
    `body_overrides` is applied last, to inject malformed values (`{"role": 123}`).
    """
    body: dict[str, Any] = {
        "active": active,
        "tenant_id": str(tenant_id),
        "professional_id": None,
    }
    if role is not None:
        body.update({"role": role, "is_owner": is_owner, "is_manager": is_manager})
    body.update(body_overrides or {})
    token = f"hub-{uuid4().hex}"
    _BODIES[token] = body
    return token


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self.status_code = 200
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeBrainApi:
    """Stands in for `httpx.AsyncClient`: answers by looking the token up in `_BODIES`."""

    def __init__(self, asked: list[str]) -> None:
        self._asked = asked

    def __call__(self, **kwargs: object) -> "_FakeBrainApi":
        return self

    async def __aenter__(self) -> "_FakeBrainApi":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def post(self, path: str, **kwargs: Any) -> _Response:
        assert path == "/internal/secretaria/hub-token/verify"
        token = kwargs["json"]["token"]
        self._asked.append(token)
        return _Response(_BODIES.get(token, {"active": False}))


def install_fake_brain_api(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Wire the fake brain-api in; returns the list of tokens brain-api was asked about."""
    monkeypatch.setenv("BRAIN_API_BASE_URL", "http://brain-api.internal")
    monkeypatch.setenv("INTERNAL_API_KEY", "shared-secret")
    monkeypatch.setenv("SUBSCRIPTION_CACHE_TTL_SECONDS", "60")
    get_settings.cache_clear()
    subscription._cache.clear()
    asked: list[str] = []
    monkeypatch.setattr(subscription.httpx, "AsyncClient", _FakeBrainApi(asked))
    return asked


def reset_fake_brain_api() -> None:
    _BODIES.clear()
    subscription._cache.clear()
    get_settings.cache_clear()
