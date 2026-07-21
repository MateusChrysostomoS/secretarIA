"""Doctor hub — analytics endpoints.

- GET /tenants/me/analytics/summary  (basic `analytics_bi` add-on): booking
  counts computed from `analytics_events` (written by plugins/analytics_bi.py's
  `post_booking` hook).
- GET /tenants/me/analytics/advanced (`analytics_bi_advanced` add-on): a
  superset of the summary — a 90-day window, per-professional / per-source
  breakdowns, and a dense last-12-months trend, from the same rows.

Both are entitlement-gated identically (fetched fresh, redis=None - this path
is not hot, mirrors api/hub/professionals.py / api/hub/units.py): add-on
disabled -> 403, and a failed entitlement fetch (None) fails CLOSED with 503,
never silently allowed. The two keys are DISTINCT: `analytics_bi_advanced` does
not grant the basic summary and vice-versa.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Tenant
from secretaria.models.analytics_event import AnalyticsEvent
from secretaria.schemas.analytics import AnalyticsAdvanced, AnalyticsSummary, MonthlyBookings
from secretaria.services.entitlements_client import get_entitlements, is_entitled

router = APIRouter(prefix="/tenants/me/analytics", tags=["hub-analytics"])

ADDON_KEY = "analytics_bi"
ADVANCED_ADDON_KEY = "analytics_bi_advanced"
_BOOKED_EVENT = "appointment_booked"
_ADVANCED_TREND_MONTHS = 12


def _as_aware_utc(dt: datetime) -> datetime:
    """Normalize a stored `created_at` to an aware UTC datetime.

    `AnalyticsEvent.created_at` is `DateTime(timezone=True)`: Postgres returns an
    aware value, but SQLite (tests) drops the tzinfo. Coerce the naive case to
    UTC so the advanced endpoint's window comparisons never raise on mixing
    naive and aware datetimes.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _recent_month_keys(now: datetime, months: int) -> list[str]:
    """The last `months` "YYYY-MM" keys ending at `now`, oldest first (dense)."""
    keys: list[str] = []
    year, month = now.year, now.month
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(keys))


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


@router.get("/advanced", response_model=AnalyticsAdvanced)
async def analytics_advanced(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AnalyticsAdvanced:
    summary = await get_entitlements(tenant.id, redis=None)
    if summary is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not verify entitlements right now"
        )
    if not is_entitled(summary, ADVANCED_ADDON_KEY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "analytics_advanced_not_entitled")

    # One scan of (created_at, payload); every aggregate is computed in Python so the
    # month-bucketing stays portable across Postgres (prod) and SQLite (tests), the same
    # way the summary endpoint already pulls payloads client-side for `by_type`.
    rows = (
        await session.execute(
            select(AnalyticsEvent.created_at, AnalyticsEvent.payload).where(
                AnalyticsEvent.tenant_id == tenant.id,
                AnalyticsEvent.event_type == _BOOKED_EVENT,
            )
        )
    ).all()

    now = datetime.now(UTC)
    cutoff_30d = now - timedelta(days=30)
    cutoff_90d = now - timedelta(days=90)

    last_30d = 0
    last_90d = 0
    by_type: dict[str, int] = {}
    by_professional: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_month: dict[str, int] = {}

    for created_at, payload in rows:
        created_at = _as_aware_utc(created_at)
        if created_at >= cutoff_30d:
            last_30d += 1
        if created_at >= cutoff_90d:
            last_90d += 1
        data = payload or {}
        type_key = data.get("appointment_type") or "unknown"
        by_type[type_key] = by_type.get(type_key, 0) + 1
        professional_key = data.get("professional_id") or "unassigned"
        by_professional[professional_key] = by_professional.get(professional_key, 0) + 1
        source_key = data.get("source") or "unknown"
        by_source[source_key] = by_source.get(source_key, 0) + 1
        month_key = created_at.strftime("%Y-%m")
        by_month[month_key] = by_month.get(month_key, 0) + 1

    monthly = [
        MonthlyBookings(month=key, count=by_month.get(key, 0))
        for key in _recent_month_keys(now, _ADVANCED_TREND_MONTHS)
    ]

    return AnalyticsAdvanced(
        bookings_total=len(rows),
        bookings_last_30d=last_30d,
        bookings_last_90d=last_90d,
        by_type=by_type,
        by_professional=by_professional,
        by_source=by_source,
        monthly=monthly,
    )
