"""Request/response schemas for the doctor-hub units endpoints (wire contract C1, TASK-090).

The address is structured (CEP, street, number, complement, neighborhood, city, UF). An
`address` key sent by a client is ignored on input; the text line returned as `address` is
derived from the parts. Part lengths are capped so the derived line always fits the 512-char
`units.address` column. `cep` and `state` are only loosely bounded here: their real validation
(8 digits / one of the 27 UFs) happens in the route, which answers the structured 422
`unit_address_invalid`.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class _AddressPartsIn(BaseModel):
    cep: str | None = Field(default=None, max_length=32)  # mask accepted; stored as 8 digits
    street: str | None = Field(default=None, max_length=150)
    number: str | None = Field(default=None, max_length=30)
    complement: str | None = Field(default=None, max_length=80)
    neighborhood: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=32)  # UF


class UnitCreate(_AddressPartsIn):
    """POST /tenants/me/units."""

    name: str = Field(min_length=1, max_length=255)
    is_active: bool = True


class UnitUpdate(_AddressPartsIn):
    """PATCH /tenants/me/units/{id}. Every field is optional (partial update)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None


class UnitRead(BaseModel):
    """Response — whitelisted fields only (no secrets ride along)."""

    id: str
    name: str
    # Derived from the parts (legacy units: their original free text).
    address: str | None
    cep: str | None = None
    street: str | None = None
    number: str | None = None
    complement: str | None = None
    neighborhood: str | None = None
    city: str | None = None
    state: str | None = None
    # True when any of cep, street, number, city, state is missing/invalid (legacy units).
    address_incomplete: bool = True
    is_active: bool
    created_at: datetime
    # ACTIVE professionals linked to this unit through an active link (TASK-083). 0 = nobody.
    professionals_count: int = 0
