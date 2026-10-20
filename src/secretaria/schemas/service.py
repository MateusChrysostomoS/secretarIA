"""Request/response schemas for the doctor-hub canonical service catalog.

The clinic edits its services in ONE place (`api/hub/services.py`); a
professional then picks from that catalog and only says what is genuinely
theirs — price, duration and whether they offer it — which stays inside
`ProfessionalConfigUpdate.appointment_types` (schemas/professional.py).

`requirements` reuses `schemas/config.py::clean_requirements`, the same rule
the per-professional `AppointmentType` applies, rather than redeclaring it.
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from secretaria.schemas.config import clean_requirements
from secretaria.schemas.professional import ProfessionalListItem


class ServiceOffer(BaseModel):
    """Who offers the service being created, and on what terms.

    Optional on `ServiceCreate`. Sent by the console when the clinic has ONE
    professional, so creating a service is also offering it: a catalog row on
    its own is offered by nobody and stays unbookable. Duration and price are
    the professional's own (they differ between doctors), exactly the two
    numbers `AppointmentType` carries per professional.
    """

    professional_id: str = Field(min_length=1, max_length=64)
    duration_min: int = Field(gt=0, le=600)
    price: str | None = Field(default=None, max_length=40)


class ServiceCreate(BaseModel):
    """POST /tenants/me/services.

    `name` is the clinic's canonical spelling. The server derives the identity
    key from it (services/service_catalog.py::normalize) and rejects a second
    service that normalizes to the same thing — see the router's 409s.
    """

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    long_description: str | None = Field(default=None, max_length=2000)
    requirements: list[str] = Field(default_factory=list)
    is_active: bool = True
    sort_order: int = 0
    # Absent = the historical behaviour: the row enters the catalog, offered by
    # nobody. Present = the same row, offered by that professional in the same
    # transaction (see ServiceOffer).
    offer: ServiceOffer | None = None

    @field_validator("requirements")
    @classmethod
    def _check_requirements(cls, value: list[str]) -> list[str]:
        return clean_requirements(value)


class ServiceUpdate(BaseModel):
    """PATCH /tenants/me/services/{id}. Every field optional (partial update).

    Renaming here renames the service EVERYWHERE, for every professional,
    because professionals reference the id and never the name — that is the
    whole point of the catalog.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    long_description: str | None = Field(default=None, max_length=2000)
    requirements: list[str] | None = None
    is_active: bool | None = None
    sort_order: int | None = None

    @field_validator("requirements")
    @classmethod
    def _check_requirements(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else clean_requirements(value)


class ServiceRead(BaseModel):
    """One catalog row as the hub sees it.

    `normalized_name` is deliberately NOT exposed: it is an internal identity
    key derived from `name`, and surfacing it would invite a client to send it
    back as if it were editable.

    `professional_ids` is who currently offers this service — the answer the
    hub needs to render "também oferecido por" beside each row, and to warn
    before a rename or a retirement that CHANGES WHAT OTHER DOCTORS OFFER
    (every professional references the id, so a rename here renames it for all
    of them at once). Ids only, never names: the roster the hub already holds
    maps them, and a catalog payload is no place to start duplicating people.
    Empty means nobody offers it — a real state for a service that was just
    created, never "not computed".
    """

    id: str
    name: str
    description: str | None
    long_description: str | None
    requirements: list[str]
    is_active: bool
    sort_order: int
    created_at: datetime
    professional_ids: list[str] = Field(default_factory=list)


class ServiceCreated(ServiceRead):
    """POST /tenants/me/services response: the row, plus who now offers it.

    `professional` is the offering professional's row exactly as
    GET /tenants/me/professionals would return it after the commit — null when
    the request carried no `offer`. Returned so the console can take it as its
    new baseline instead of re-reading the roster (or, worse, treating the
    server's own write as an unsaved local edit).
    """

    professional: ProfessionalListItem | None = None
