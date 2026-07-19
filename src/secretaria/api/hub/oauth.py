"""Doctor hub — Google Calendar OAuth (start / callback / disconnect).

The refresh token only ever enters the system through the server-to-server code
exchange in `callback`, encrypted before it touches the database. The doctor
never sees or types a token.

Per-professional variant (contract v1 §10 item C): the SAME callback serves
both flows. The signed `state` carries `tenant_id` ALWAYS and `professional_id`
OPTIONALLY — when present, the callback routes the refresh token to
`set_professional_google_refresh_token` instead of the tenant-level column.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.core.signing import sign, verify
from secretaria.models import Tenant
from secretaria.models.professional import Professional
from secretaria.services import google_oauth, tenant_config as cfg

logger = get_logger(__name__)
router = APIRouter(tags=["hub-calendar"])


async def _owned_professional(
    session: AsyncSession, tenant: Tenant, professional_id: UUID
) -> Professional:
    """Load a professional scoped to `tenant`, or 404 (ownership validation)."""
    professional = await session.scalar(
        select(Professional).where(
            Professional.id == professional_id, Professional.tenant_id == tenant.id
        )
    )
    if professional is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional not found")
    return professional


def _portal_redirect(base: str, status_value: str) -> JSONResponse | RedirectResponse:
    """Send the doctor's browser back to the portal with a status flag.

    Falls back to a plain JSON body when no portal URL is configured (useful
    while the frontend does not exist yet)."""
    if not base:
        return JSONResponse({"status": status_value})
    separator = "&" if "?" in base else "?"
    return RedirectResponse(url=f"{base}{separator}status={status_value}", status_code=302)


@router.get("/tenants/me/calendar/oauth/start")
async def oauth_start(tenant: Tenant = Depends(get_current_tenant)) -> dict:
    """Return the Google consent URL (the frontend redirects the browser to it)."""
    settings = get_settings()
    if not (
        settings.GOOGLE_CLIENT_ID
        and settings.GOOGLE_CLIENT_SECRET
        and settings.GOOGLE_OAUTH_REDIRECT_URI
        and settings.OAUTH_STATE_SECRET
    ):
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Google OAuth is not configured on the platform.",
        )
    # Sign the tenant into the state so the callback can trust who started this.
    state = sign({"tenant_id": str(tenant.id)}, settings.OAUTH_STATE_SECRET)
    return {"authorization_url": google_oauth.build_authorization_url(state)}


@router.get("/tenants/me/professionals/{professional_id}/calendar/oauth/start")
async def professional_oauth_start(
    professional_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return the Google consent URL for ONE professional's own Calendar.

    Validates the professional belongs to the authenticated tenant (404
    otherwise), same as every other professional-scoped hub endpoint.
    """
    settings = get_settings()
    professional = await _owned_professional(session, tenant, professional_id)
    if not (
        settings.GOOGLE_CLIENT_ID
        and settings.GOOGLE_CLIENT_SECRET
        and settings.GOOGLE_OAUTH_REDIRECT_URI
        and settings.OAUTH_STATE_SECRET
    ):
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Google OAuth is not configured on the platform.",
        )
    # The state carries BOTH ids: tenant_id (who started this, as always) AND
    # professional_id (which credential row the callback should write to).
    state = sign(
        {"tenant_id": str(tenant.id), "professional_id": str(professional.id)},
        settings.OAUTH_STATE_SECRET,
    )
    return {"authorization_url": google_oauth.build_authorization_url(state)}


@router.get("/oauth/google/callback", response_model=None)
async def oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse | RedirectResponse:
    """Google redirects here. Validate state, exchange the code, store the token.

    Serves BOTH the tenant-level and per-professional flows: `state` always
    carries `tenant_id`; when it ALSO carries `professional_id` (per-
    professional start), the refresh token is routed to
    `set_professional_google_refresh_token` instead of the tenant-level column.
    """
    settings = get_settings()
    portal = settings.PORTAL_POST_OAUTH_REDIRECT

    if error or not code or not state:
        logger.warning("hub_oauth_callback_bad_request", error=error)
        return _portal_redirect(portal, "error")

    payload = verify(state, settings.OAUTH_STATE_SECRET, settings.OAUTH_STATE_MAX_AGE)
    if not payload or "tenant_id" not in payload:
        logger.warning("hub_oauth_callback_invalid_state")
        return _portal_redirect(portal, "invalid_state")
    try:
        tenant_id = UUID(payload["tenant_id"])
    except ValueError:
        return _portal_redirect(portal, "invalid_state")

    professional_id: UUID | None = None
    raw_professional_id = payload.get("professional_id")
    if raw_professional_id:
        try:
            professional_id = UUID(raw_professional_id)
        except ValueError:
            logger.warning("hub_oauth_callback_invalid_professional_id")
            return _portal_redirect(portal, "invalid_state")
        # Defense in depth: the professional must still belong to this
        # tenant at callback time (it could have been deleted meanwhile).
        owned = await session.scalar(
            select(Professional).where(
                Professional.id == professional_id, Professional.tenant_id == tenant_id
            )
        )
        if owned is None:
            logger.warning(
                "hub_oauth_callback_professional_not_found", tenant_id=str(tenant_id)
            )
            return _portal_redirect(portal, "invalid_state")

    try:
        refresh_token = await google_oauth.exchange_code_for_refresh_token(code)
    except Exception as exc:  # noqa: BLE001 - surface a clean status to the portal
        logger.error("hub_oauth_exchange_failed", error=str(exc), tenant_id=str(tenant_id))
        return _portal_redirect(portal, "exchange_failed")

    if not refresh_token:
        # No refresh_token (usually a prior consent). Ask the doctor to retry;
        # prompt=consent on /start normally prevents this.
        logger.warning("hub_oauth_no_refresh_token", tenant_id=str(tenant_id))
        return _portal_redirect(portal, "no_refresh_token")

    if professional_id is not None:
        await cfg.set_professional_google_refresh_token(session, professional_id, refresh_token)
        logger.info(
            "hub_oauth_calendar_connected_professional",
            tenant_id=str(tenant_id),
            professional_id=str(professional_id),
        )
    else:
        await cfg.set_google_refresh_token(session, tenant_id, refresh_token)
        logger.info("hub_oauth_calendar_connected", tenant_id=str(tenant_id))
    await session.commit()
    return _portal_redirect(portal, "success")


@router.post("/tenants/me/professionals/{professional_id}/calendar/disconnect")
async def professional_calendar_disconnect(
    professional_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Forget ONE professional's own Calendar refresh token.

    Unlike the tenant-level disconnect, this does NOT force `is_active=False`
    on anything: a professional's own Calendar being disconnected still
    leaves the tenant-level Calendar (or another professional's own) able to
    cover it via the partial-activation rule
    (services/tenant_config.professional_completeness).
    """
    professional = await _owned_professional(session, tenant, professional_id)
    await cfg.clear_professional_google_refresh_token(session, professional.id)
    await session.commit()
    logger.info(
        "hub_oauth_calendar_disconnected_professional",
        tenant_id=str(tenant.id),
        professional_id=str(professional.id),
    )
    return {"status": "disconnected", "professional_id": str(professional.id)}


@router.post("/tenants/me/calendar/disconnect")
async def calendar_disconnect(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Forget the Calendar refresh token and force the tenant offline.

    Without a Calendar the bot cannot schedule, so disconnecting also sets
    `is_active=False`."""
    await cfg.clear_google_refresh_token(session, tenant.id)
    tenant.is_active = False  # `tenant` is attached to this session
    await session.commit()
    logger.info("hub_oauth_calendar_disconnected", tenant_id=str(tenant.id))
    return {"status": "disconnected", "is_active": False}
