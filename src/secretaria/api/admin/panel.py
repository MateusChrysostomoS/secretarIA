"""Administrative endpoints (currently: data wipe).

Every route here is guarded by `require_admin`, which checks the
`X-Admin-Token` header against `Settings.ADMIN_TOKEN`. If the setting is
unset the endpoint returns 503, so a deployment that forgot to configure
the token cannot have it accidentally exposed.
"""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.services.admin import DEFAULT_WIPE_TABLES, TENANT_TABLE, wipe_data

logger = get_logger(__name__)

router = APIRouter(prefix="/admin")

# Declaring the scheme (instead of a bare Header dependency) is what makes
# Swagger UI render the "Authorize" lock icon on this endpoint. auto_error
# is off so we can return our own 403 with a clearer message.
_admin_token_scheme = APIKeyHeader(
    name="X-Admin-Token",
    auto_error=False,
    description="Admin shared secret. Configure via the ADMIN_TOKEN env var.",
)


def require_admin(
    token: Annotated[str | None, Security(_admin_token_scheme)] = None,
) -> None:
    """FastAPI dependency: 403 unless `X-Admin-Token` matches the env token.

    Uses `secrets.compare_digest` so equal-length tokens are compared in
    constant time, avoiding header-timing oracles.

    Accepts ADMIN_TOKEN_PREVIOUS as well as ADMIN_TOKEN (any-of) so the token can
    be rotated without downtime: deploy the new value as ADMIN_TOKEN, keep the old
    one as ADMIN_TOKEN_PREVIOUS until every caller has switched, then drop it.
    """
    settings = get_settings()
    if not settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin endpoint not configured.",
        )
    is_current = bool(token) and secrets.compare_digest(token, settings.ADMIN_TOKEN)
    is_previous = (
        bool(token)
        and bool(settings.ADMIN_TOKEN_PREVIOUS)
        and secrets.compare_digest(token, settings.ADMIN_TOKEN_PREVIOUS)
    )
    if not (is_current or is_previous):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin token.",
        )


class ResetRequest(BaseModel):
    confirm: bool = Field(
        ...,
        description="Must be true. Guard against accidental wipes via curl.",
    )
    include_tenants: bool = Field(
        False,
        description=(
            "When true, also truncate the tenants table. Next inbound webhook "
            "will auto-provision a new tenant from environment settings."
        ),
    )


class ResetResponse(BaseModel):
    wiped: list[str] = Field(
        ...,
        description="Tables truncated, in execution order.",
    )


@router.post(
    "/reset",
    response_model=ResetResponse,
    dependencies=[Depends(require_admin)],
    summary="Wipe conversation data",
    description=(
        "Truncates messages, conversations, patients and processed_events. "
        f"Default wipe list: {', '.join(DEFAULT_WIPE_TABLES)}. Pass "
        f"include_tenants=true to also wipe `{TENANT_TABLE}`. "
        "Requires the X-Admin-Token header."
    ),
    responses={
        403: {"description": "Missing or invalid admin token."},
        503: {"description": "ADMIN_TOKEN not configured on the server."},
    },
)
async def reset_data(payload: ResetRequest) -> ResetResponse:
    """Reset the database. Requires `confirm: true` to actually run."""
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set confirm: true to proceed.",
        )
    wiped = await wipe_data(include_tenants=payload.include_tenants)
    logger.warning(
        "admin_reset_endpoint_executed",
        wiped=wiped,
        include_tenants=payload.include_tenants,
    )
    return ResetResponse(wiped=wiped)
