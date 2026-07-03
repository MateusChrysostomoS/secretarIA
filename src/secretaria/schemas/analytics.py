"""Response schema for the doctor-hub analytics summary endpoint (analytics_bi addon)."""

from pydantic import BaseModel


class AnalyticsSummary(BaseModel):
    """GET /tenants/me/analytics/summary response."""

    bookings_total: int
    bookings_last_30d: int
    by_type: dict[str, int]
