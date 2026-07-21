"""Response schemas for the doctor-hub analytics endpoints.

`AnalyticsSummary` backs the basic `analytics_bi` add-on's summary endpoint;
`AnalyticsAdvanced` backs the `analytics_bi_advanced` add-on's richer dashboard.
"""

from pydantic import BaseModel


class AnalyticsSummary(BaseModel):
    """GET /tenants/me/analytics/summary response (basic `analytics_bi` add-on)."""

    bookings_total: int
    bookings_last_30d: int
    by_type: dict[str, int]


class MonthlyBookings(BaseModel):
    """One point in the advanced dashboard's monthly booking trend."""

    month: str  # "YYYY-MM"
    count: int


class AnalyticsAdvanced(BaseModel):
    """GET /tenants/me/analytics/advanced response (`analytics_bi_advanced` add-on).

    A superset of the basic summary: adds a 90-day window, per-professional and
    per-source breakdowns, and a dense last-12-months trend. Every dimension is
    derived from the same minimal, non-personal `analytics_events` rows the
    analytics_bi plugin records — `by_professional` is keyed by professional_id
    (or "unassigned"), never by name, keeping this table LGPD-free by
    construction (see models/analytics_event.py).
    """

    bookings_total: int
    bookings_last_30d: int
    bookings_last_90d: int
    by_type: dict[str, int]
    by_professional: dict[str, int]
    by_source: dict[str, int]
    monthly: list[MonthlyBookings]
