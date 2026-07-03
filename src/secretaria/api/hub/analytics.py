"""Doctor hub — analytics summary (analytics_bi addon).

GET /tenants/me/analytics/summary - booking counts computed from
`analytics_events` (written by plugins/analytics_bi.py's `post_booking`
hook).

Entitlement-gated: addon disabled -> 403 {"detail": "analytics_not_entitled"}
(fetched fresh, redis=None - this path is not hot, mirrors
api/hub/professionals.py / api/hub/units.py). A failed entitlement fetch
(None) fails CLOSED: 503, never silently allowed.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Tenant
from secretaria.models.analytics_event import AnalyticsEvent
from secretaria.schemas.analytics import AnalyticsSummary
from secretaria.services.entitlements_client import get_entitlements, is_entitled

router = APIRouter(prefix="/tenants/me/analytics", tags=["hub-analytics"])

ADDON_KEY = "analytics_bi"
_BOOKED_EVENT = "appointment_booked"


@router.get("/summary", response_model=AnalyticsSummary)
async def analytics_summary(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AnalyticsSummary:
    summary = await get_entitlements(tenant.id, redis=None)
    if summary is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not verify entitlements right now"
        )
    if not is_entitled(summary, ADDON_KEY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "analytics_not_entitled")

    base_filter = (
        AnalyticsEvent.tenant_id == tenant.id,
        AnalyticsEvent.event_type == _BOOKED_EVENT,
    )

    total = await session.scalar(
        select(func.count()).select_from(AnalyticsEvent).where(*base_filter)
    )
    cutoff = datetime.now(UTC) - timedelta(days=30)
    last_30d = await session.scalar(
        select(func.count())
        .select_from(AnalyticsEvent)
        .where(*base_filter, AnalyticsEvent.created_at >= cutoff)
    )

    payloads = await session.scalars(select(AnalyticsEvent.payload).where(*base_filter))
    by_type: dict[str, int] = {}
    for payload in payloads:
        key = (payload or {}).get("appointment_type") or "unknown"
        by_type[key] = by_type.get(key, 0) + 1

    return AnalyticsSummary(
        bookings_total=int(total or 0),
        bookings_last_30d=int(last_30d or 0),
        by_type=by_type,
    )
