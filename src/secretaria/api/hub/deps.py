"""Shared FastAPI dependencies for the doctor hub.

`get_current_tenant` is the single authentication gate for every existing hub endpoint.
It turns an `Authorization: Bearer <token>` header into a Tenant row, delegating token
validation to core.subscription (which calls brain-api). It does NOT look at the caller's
role, and it must keep not doing so: old hub tokens (no role claim) stay valid everywhere
that uses it.

TASK-020 adds the ROLE gates, for the few actions only a clinic manager may take (the Pix
key, payment settings) or only clinical staff may take (resolving attention items):

  * `require_clinic_manager`   — `manager`, or a `doctor` flagged `is_owner`/`is_manager`.
  * `require_clinic_clinician` — `manager` or `doctor`; never a `secretary`.

Both return the `Tenant` exactly like `get_current_tenant`, so a route adopts a gate by
swapping one `Depends`. They FAIL CLOSED with a structured 403 detail:
`{"code": "role_claim_missing"}` when the token carries no well-formed role (an older
brain-api, a malformed value) and `{"code": "forbidden_role"}` when the role is known but
not allowed. Order of refusals: 401 (no/invalid/inactive token) -> 403 (role) -> 404
(tenant). The tenant always comes from the verified claim, never from the request.

For a route whose ONE handler serves callers of several roles and only some FIELDS are
manager-only (payment settings in the config PUT), take `claim: SubscriptionClaim =
Depends(get_hub_claim)` next to `get_current_tenant` and call `ensure_clinic_manager(claim)`
inside the handler when such a field is present (the second verification is a positive-
cache hit).
"""

from collections.abc import Callable
from dataclasses import replace

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.database import get_session
from secretaria.core.hub_roles import is_clinic_clinician, is_clinic_manager
from secretaria.core.logging import get_logger
from secretaria.core.subscription import SubscriptionClaim, verify_subscription_token
from secretaria.models import Professional, Tenant
from secretaria.services.agenda_visibility import AgendaViewer, viewer_from_claim

logger = get_logger(__name__)

# Machine-readable 403 codes; the console (Brain-Message-Frontend, TASK F4) translates them.
ROLE_CLAIM_MISSING = "role_claim_missing"
FORBIDDEN_ROLE = "forbidden_role"

_MISSING_ROLE_MESSAGE = "Sua sessão não informa o seu papel na clínica. Entre novamente."
_MANAGER_ONLY_MESSAGE = "Somente o gestor da clínica pode fazer isto."
_CLINICIAN_ONLY_MESSAGE = "Somente médicos e gestores da clínica podem fazer isto."


def _bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an Authorization header, tolerating a missing
    'Bearer ' prefix (handy for local curl/testing)."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()


async def _resolve_tenant(session: AsyncSession, tenant_id) -> Tenant | None:
    """Load the claimed tenant, or the only tenant when the claim has no id.

    A real brain-api claim always carries a concrete tenant_id, so the
    single-tenant fallback below is dead in practice today; it stays as a
    defensive fallback rather than a relied-upon code path.
    """
    if tenant_id is not None:
        return await session.get(Tenant, tenant_id)
    rows = (await session.scalars(select(Tenant).limit(2))).all()
    return rows[0] if len(rows) == 1 else None


async def get_hub_claim(authorization: str | None = Header(default=None)) -> SubscriptionClaim:
    """Authenticate the request and return the verified claim (no role judgement).

    401 for a missing/invalid/inactive token — the same refusals `get_current_tenant` gives.
    """
    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    claim = await verify_subscription_token(token)
    if claim is None or not claim.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or inactive subscription token")
    return claim


async def _tenant_for_claim(session: AsyncSession, claim: SubscriptionClaim) -> Tenant:
    tenant = await _resolve_tenant(session, claim.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No tenant resolved for this subscription")
    return tenant


async def get_current_tenant(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Tenant:
    """Authenticate the request and return the caller's Tenant row.

    401 for a missing/invalid/inactive token, 404 when the token is valid but
    no tenant can be resolved. Role-blind by design (see the module docstring).
    """
    claim = await get_hub_claim(authorization)
    return await _tenant_for_claim(session, claim)


def _refuse(code: str, message: str, claim: SubscriptionClaim) -> HTTPException:
    # Only the code, the role string and the tenant id: never the token, never PII.
    logger.warning("hub_role_refused", code=code, role=claim.role, tenant_id=str(claim.tenant_id))
    return HTTPException(status.HTTP_403_FORBIDDEN, detail={"code": code, "message": message})


def _enforce(
    claim: SubscriptionClaim, allowed: Callable[[SubscriptionClaim], bool], message: str
) -> None:
    if claim.role is None:
        raise _refuse(ROLE_CLAIM_MISSING, _MISSING_ROLE_MESSAGE, claim)
    if not allowed(claim):
        raise _refuse(FORBIDDEN_ROLE, message, claim)


def _is_manager(claim: SubscriptionClaim) -> bool:
    return is_clinic_manager(claim.role, claim.is_owner, claim.is_manager)


def _is_clinician(claim: SubscriptionClaim) -> bool:
    return is_clinic_clinician(claim.role)


def ensure_clinic_manager(claim: SubscriptionClaim) -> None:
    """Raise the 403 `require_clinic_manager` would raise; no-op for a manager."""
    _enforce(claim, _is_manager, _MANAGER_ONLY_MESSAGE)


def ensure_clinic_clinician(claim: SubscriptionClaim) -> None:
    """Raise the 403 `require_clinic_clinician` would raise; no-op for a manager or doctor."""
    _enforce(claim, _is_clinician, _CLINICIAN_ONLY_MESSAGE)


async def require_clinic_manager(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Tenant:
    """Only a clinic manager: `manager`, or a `doctor` flagged `is_owner`/`is_manager`."""
    claim = await get_hub_claim(authorization)
    ensure_clinic_manager(claim)
    return await _tenant_for_claim(session, claim)


async def require_clinic_clinician(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Tenant:
    """Clinical staff or the manager: `manager` or `doctor`; never a `secretary`."""
    claim = await get_hub_claim(authorization)
    ensure_clinic_clinician(claim)
    return await _tenant_for_claim(session, claim)


async def get_agenda_viewer(
    authorization: str | None = Header(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AgendaViewer:
    """Who is looking at the agenda (TASK-044 R7, spec 2026-10-09 §5.A).

    Re-reads the introspection `get_current_tenant` just did: a positive answer is cached
    in-process (core/subscription.py), so this is no second brain-api call within
    SUBSCRIPTION_CACHE_TTL_SECONDS. Fails closed: no live claim for THIS clinic is 401.
    The linked professional must be a Professional row of this clinic - any other id is
    dropped, and an "own" viewer without a professional then reaches no appointment.
    """
    token = _bearer_token(authorization)
    claim = await verify_subscription_token(token) if token else None
    if claim is None or not claim.active or claim.tenant_id != tenant.id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or inactive subscription token")
    viewer = viewer_from_claim(claim.agenda_scope, claim.professional_id)
    if viewer.professional_id is not None:
        owned = await session.scalar(
            select(Professional.id).where(
                Professional.id == viewer.professional_id,
                Professional.tenant_id == tenant.id,
            )
        )
        if owned is None:
            viewer = replace(viewer, professional_id=None)
    return viewer
