"""Request/response schemas for the doctor-hub units endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field


class UnitCreate(BaseModel):
    """POST /tenants/me/units."""

    name: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=512)
    is_active: bool = True


class UnitUpdate(BaseModel):
    """PATCH /tenants/me/units/{id}. Every field is optional (partial update)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=512)
    is_active: bool | None = None


class UnitRead(BaseModel):
    """Response — whitelisted fields only (no secrets ride along)."""

    id: str
    name: str
    address: str | None
    is_active: bool
    created_at: datetime
