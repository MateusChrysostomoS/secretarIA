"""Request/response schemas for the doctor-hub professionals endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field


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
    """Response — whitelisted fields only (no secrets ride along)."""

    id: str
    name: str
    google_calendar_id: str | None
    is_active: bool
    created_at: datetime
