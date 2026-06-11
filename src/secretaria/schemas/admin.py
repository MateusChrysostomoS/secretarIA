"""Response schemas for the admin tenants fleet view (read-only).

Secret-free by construction: the plaintext `Tenant.access_token`, the WABA token
and the Google refresh token are all omitted. `phone_number_id` is an identifier
(not a credential) and is safe to surface to the operator.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class CalendarHealthStatus(StrEnum):
    """Per-tenant Google Calendar reachability, as the admin table should show it."""

    not_connected = "not_connected"  # no refresh token stored (onboarding incomplete)
    connected = "connected"  # token stored (default) or a live probe succeeded
    error = "error"  # a live probe hit CalendarUnavailableError
    unknown = "unknown"  # not evaluated (reserved for a future cached/cron path)


class CalendarHealth(BaseModel):
    """Calendar status block nested in each tenant summary."""

    connected: bool = Field(
        description="A refresh token is stored, or a live probe succeeded."
    )
    calendar_id: str = Field(
        description="The tenant's google_calendar_id (e.g. 'primary')."
    )
    status: CalendarHealthStatus
    # Null on the cheap default list; set to the probe time when ?probe=true or
    # the per-tenant health endpoint actually performs a live check.
    last_checked_at: datetime | None = None
    # Sanitized reason when status == error (from CalendarUnavailableError). Never
    # contains a token or a raw Google payload.
    detail: str | None = None


class TenantSummary(BaseModel):
    """One row of the admin fleet table. Never includes secrets."""

    id: UUID
    clinic_name: str
    phone_number_id: str
    contact_email: str | None
    language: str
    timezone: str
    is_active: bool
    precheck_enabled: bool
    created_at: datetime
    calendar: CalendarHealth


class TenantListResponse(BaseModel):
    """Paginated fleet listing."""

    items: list[TenantSummary]
    total: int = Field(description="Total tenants, ignoring limit/offset.")
    limit: int
    offset: int
