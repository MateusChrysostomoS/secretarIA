"""Admin fleet endpoints — list tenants and check each one's Calendar health.

Guarded by `require_admin` (the same `X-Admin-Token` shared secret as the other
admin routes). Read-only and secret-free: responses never carry the WhatsApp
access token or the Google refresh token.

Calendar health has two cost tiers:
  * default — `connected` is derived in SQL from whether an encrypted refresh
    token exists (no network, no decryption). This is what the table loads on
    first paint.
  * on demand — `?probe=true` on the list, or `GET .../{id}/calendar/health`,
    performs a live reachability check that catches a token revoked/expired
    after onboarding. Probing is slow (one Google call per connected tenant),
    so it is opt-in and concurrency-capped.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.admin import require_admin
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.tenant_credentials import TenantCredentials
from secretaria.schemas.admin import (
    CalendarHealth,
    CalendarHealthStatus,
    TenantListResponse,
    TenantSummary,
)
from secretaria.services.calendar import CalendarService, CalendarUnavailableError
from secretaria.services.tenant_config import TenantRuntimeConfig, load_tenant_config

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/tenants", tags=["admin-tenants"])

_DEFAULT_PAGE = 50
_MAX_PAGE = 200
# Cap simultaneous Google calls so a ?probe over a page can't hammer the OAuth
# token endpoint; bound the whole page so one hung probe can't stall the response.
_PROBE_CONCURRENCY = 5
_PROBE_TOTAL_TIMEOUT_S = 20.0
# The probe only needs the cheapest call that touches the real calendar_id.
_PROBE_WINDOW = timedelta(minutes=1)

_ADMIN_RESPONSES = {
    403: {"description": "Missing or invalid admin token."},
    503: {"description": "ADMIN_TOKEN not configured on the server."},
}


def _summary(tenant: Tenant, calendar: CalendarHealth) -> TenantSummary:
    """Build a secret-free row DTO field-by-field (never `model_validate(tenant)`)."""
    return TenantSummary(
        id=tenant.id,
        clinic_name=tenant.clinic_name,
        phone_number_id=tenant.phone_number_id,
        contact_email=tenant.contact_email,
        language=tenant.language,
        timezone=tenant.timezone,
        is_active=tenant.is_active,
        precheck_enabled=tenant.precheck_enabled,
        created_at=tenant.created_at,
        calendar=calendar,
    )


def _derived_health(tenant: Tenant, connected: bool) -> CalendarHealth:
    """Calendar health from stored state only — no network, no decryption."""
    return CalendarHealth(
        connected=connected,
        calendar_id=tenant.google_calendar_id,
        status=(
            CalendarHealthStatus.connected
            if connected
            else CalendarHealthStatus.not_connected
        ),
        last_checked_at=None,
        detail=None,
    )


async def _list_tenants_page(
    session: AsyncSession, *, limit: int, offset: int
) -> tuple[list[tuple[Tenant, bool]], int]:
    """Return (rows, total). Each row is (tenant, calendar_connected).

    `connected` is derived in SQL from whether an encrypted refresh token exists
    — the ciphertext itself is never selected or decrypted. The 1:1 unique FK on
    `tenant_credentials` guarantees the LEFT JOIN cannot fan out, so there is no
    N+1 and no duplicate tenant rows.
    """
    connected = TenantCredentials.google_refresh_token_encrypted.isnot(None).label(
        "connected"
    )
    stmt = (
        select(Tenant, connected)
        .outerjoin(TenantCredentials, TenantCredentials.tenant_id == Tenant.id)
        .order_by(Tenant.created_at.desc(), Tenant.id)
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    rows = [(tenant, bool(flag)) for tenant, flag in result.all()]
    total = await session.scalar(select(func.count()).select_from(Tenant))
    return rows, int(total or 0)


async def _probe_config(config: TenantRuntimeConfig, calendar_id: str) -> CalendarHealth:
    """Live reachability check for one tenant's calendar. Never raises.

    Builds a per-tenant CalendarService and does the cheapest real call that
    exercises the tenant's actual calendar_id (events.list over a 1-minute
    window). A revoked/expired token or a Google outage surfaces as
    CalendarUnavailableError -> status=error with a sanitized detail.
    """
    checked_at = datetime.now(UTC)
    if config.google_refresh_token is None:
        return CalendarHealth(
            connected=False,
            calendar_id=calendar_id,
            status=CalendarHealthStatus.not_connected,
            last_checked_at=checked_at,
            detail=None,
        )

    service = CalendarService.from_tenant_config(config)
    now = datetime.now(UTC)
    try:
        await service.check_availability(now, now + _PROBE_WINDOW)
    except CalendarUnavailableError as exc:
        # exc messages are already sanitized (e.g. "Google Calendar returned HTTP 401").
        return CalendarHealth(
            connected=False,
            calendar_id=calendar_id,
            status=CalendarHealthStatus.error,
            last_checked_at=checked_at,
            detail=str(exc),
        )
    except Exception as exc:  # defensive: a probe must never 500 the listing
        logger.warning("admin_calendar_probe_unexpected", error=str(exc))
        return CalendarHealth(
            connected=False,
            calendar_id=calendar_id,
            status=CalendarHealthStatus.error,
            last_checked_at=checked_at,
            detail="Calendar probe failed unexpectedly.",
        )

    return CalendarHealth(
        connected=True,
        calendar_id=calendar_id,
        status=CalendarHealthStatus.connected,
        last_checked_at=checked_at,
        detail=None,
    )


async def _probe_page(
    session: AsyncSession, rows: list[tuple[Tenant, bool]]
) -> list[TenantSummary]:
    """Live-probe each tenant in the page, concurrency-capped and time-bounded.

    Two-phase, because a single AsyncSession cannot run concurrent statements:
    (1) serially load+decrypt each connected tenant's config on the request
    session (fast DB reads); (2) run only the slow network probes concurrently
    under a small semaphore. Tenants with no stored token skip the network. On
    the overall timeout, unfinished rows fall back to their derived status
    (left with last_checked_at=None to signal they were not actually probed).
    """
    # Phase 1 — serial DB work; only connected tenants need a decrypted config.
    configs: list[TenantRuntimeConfig | None] = []
    for tenant, connected in rows:
        configs.append(await load_tenant_config(session, tenant) if connected else None)

    # Pre-fill with derived fallbacks; live results overwrite per index below.
    healths: list[CalendarHealth] = [
        _derived_health(tenant, connected) for tenant, connected in rows
    ]

    # Phase 2 — concurrent, capped, time-bounded network probes.
    semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def _probe_one(config: TenantRuntimeConfig, calendar_id: str) -> CalendarHealth:
        async with semaphore:
            return await _probe_config(config, calendar_id)

    index_of: dict[asyncio.Task, int] = {}
    for idx, ((tenant, _connected), config) in enumerate(
        zip(rows, configs, strict=True)
    ):
        if config is not None:
            task = asyncio.create_task(_probe_one(config, tenant.google_calendar_id))
            index_of[task] = idx

    if index_of:
        done, pending = await asyncio.wait(
            index_of.keys(), timeout=_PROBE_TOTAL_TIMEOUT_S
        )
        for task in pending:
            task.cancel()
        if pending:
            logger.warning("admin_calendar_probe_page_timeout", unfinished=len(pending))
        for task in done:
            if not task.cancelled() and task.exception() is None:
                healths[index_of[task]] = task.result()

    return [
        _summary(tenant, health)
        for (tenant, _connected), health in zip(rows, healths, strict=True)
    ]


@router.get(
    "",
    response_model=TenantListResponse,
    dependencies=[Depends(require_admin)],
    summary="List tenants (admin fleet view)",
    description=(
        "Returns every tenant with a secret-free summary and its Google Calendar "
        "status. Pass probe=true to live-check each tenant's calendar (slower). "
        "Requires the X-Admin-Token header."
    ),
    responses=_ADMIN_RESPONSES,
)
async def list_tenants(
    limit: Annotated[int, Query(ge=1, le=_MAX_PAGE)] = _DEFAULT_PAGE,
    offset: Annotated[int, Query(ge=0)] = 0,
    probe: Annotated[
        bool,
        Query(
            description=(
                "Live-check each tenant's Google Calendar (one Google call per "
                "connected tenant; concurrency-capped). Off by default — the list "
                "is then derived from stored credentials with no network."
            ),
        ),
    ] = False,
    session: AsyncSession = Depends(get_session),
) -> TenantListResponse:
    rows, total = await _list_tenants_page(session, limit=limit, offset=offset)
    if probe:
        items = await _probe_page(session, rows)
    else:
        items = [
            _summary(tenant, _derived_health(tenant, connected))
            for tenant, connected in rows
        ]
    return TenantListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/{tenant_id}/calendar/health",
    response_model=CalendarHealth,
    dependencies=[Depends(require_admin)],
    summary="Live Google Calendar health for one tenant",
    description=(
        "Performs a live reachability check against the tenant's Google Calendar "
        "and reports connected / not_connected / error. Requires the "
        "X-Admin-Token header."
    ),
    responses={**_ADMIN_RESPONSES, 404: {"description": "No tenant with that id."}},
)
async def tenant_calendar_health(
    tenant_id: Annotated[UUID, Path(description="Tenant UUID.")],
    session: AsyncSession = Depends(get_session),
) -> CalendarHealth:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tenant.")
    config = await load_tenant_config(session, tenant)
    return await _probe_config(config, tenant.google_calendar_id)
