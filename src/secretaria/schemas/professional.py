"""Request/response schemas for the doctor-hub professionals endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from secretaria.schemas.config import AppointmentType, TimeWindow, _validate_business_hours


class ProfessionalCreate(BaseModel):
    """POST /tenants/me/professionals."""

    name: str = Field(min_length=1, max_length=255)
    google_calendar_id: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class ProfessionalUpdate(BaseModel):
    """PATCH /tenants/me/professionals/{id}. Every field is optional (partial update)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    google_calendar_id: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


class ProfessionalRead(BaseModel):
    """Response — whitelisted fields only (no secrets ride along).

    Returned by POST/PATCH unchanged (existing hub consumers keep working) —
    see `ProfessionalListItem` below for the richer GET-list row.
    """

    id: str
    name: str
    google_calendar_id: str | None
    is_active: bool
    created_at: datetime


class ProfessionalConfigUpdate(BaseModel):
    """PUT /tenants/me/professionals/{id}/config (contract v1 §10 item E).

    Every field is optional (partial update). `business_hours` /
    `appointment_types` reuse the EXACT SAME validation semantics as the
    tenant-level `TenantConfigUpdate` (schemas/config.py) — imported, not
    reimplemented, so the two can never silently drift apart.
    """

    business_hours: dict[str, list[TimeWindow]] | None = None
    appointment_types: list[AppointmentType] | None = None
    specialty: str | None = Field(default=None, max_length=255)
    about: str | None = Field(default=None, max_length=4000)
    context_doctor_message: str | None = Field(default=None, max_length=4000)
    google_calendar_id: str | None = Field(default=None, max_length=255)

    @field_validator("business_hours")
    @classmethod
    def _check_business_hours(
        cls, value: dict[str, list[TimeWindow]] | None
    ) -> dict[str, list[TimeWindow]] | None:
        return None if value is None else _validate_business_hours(value)


class ProfessionalCalendarConnect(BaseModel):
    """POST /tenants/me/professionals/{id}/calendar response (shared_account
    mode, docs/CHECKPOINT_google_calendar_modes.md item 3).

    `created=False` means the professional already had a `google_calendar_id`
    and this call was an idempotent no-op (nothing was created or changed).
    """

    professional_id: str
    google_calendar_id: str
    created: bool


class ProfessionalListItem(BaseModel):
    """GET /tenants/me/professionals row — full per-professional config + completeness.

    A superset of `ProfessionalRead`: the list view is what the hub config
    screen uses to render each professional's card / edit-form, so it also
    carries the extended config fields (contract v1 §10) and onboarding
    completeness (services/tenant_config.professional_completeness_item) —
    letting the frontend prefill the PUT config form without a separate
    GET-by-id endpoint (not part of this contract).
    """

    id: str
    name: str
    google_calendar_id: str | None
    is_active: bool
    created_at: datetime
    specialty: str | None
    about: str | None
    context_doctor_message: str | None
    business_hours: dict
    appointment_types: list
    has_calendar: bool
    has_hours: bool
    has_services: bool
    complete: bool
