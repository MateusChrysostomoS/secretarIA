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

    THREE-STATE SEMANTICS for `business_hours` / `appointment_types`. The router
    applies `model_dump(exclude_unset=True)`, so all three are expressible and
    each means something different:

      key absent        -> leave the stored value exactly as it is
      key present, null -> STOP having an own value: go back to inheriting the
                           clinic's legacy column (a deliberate operation, not
                           an accident)
      key present, {}/[]-> an OWN override that is empty: closed all week /
                           offering nothing. Inherits NOTHING.

    Sending `{}` therefore takes the professional OFF the clinic's hours rather
    than resetting them to it — see the NULL-versus-EMPTY note in
    services/tenant_config.py for why that distinction is load-bearing.
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


class ProfessionalCalendarBulkItem(BaseModel):
    """One professional's outcome inside a bulk secondary-calendar run.

    `error` is a machine-readable code (never a stack, never a Google message)
    for the rows that could not be created while OTHERS succeeded — the whole
    reason this endpoint answers 200 with a per-row report instead of a single
    status: the successes are already committed and the clinic must be able to
    see exactly which doctors still need one.
    """

    professional_id: str
    name: str
    google_calendar_id: str | None
    created: bool
    error: str | None = None


class ProfessionalCalendarBulkResult(BaseModel):
    """POST /tenants/me/professionals/calendars response.

    Counts first, because that is what the UI puts in a toast; `items` carries
    the per-professional detail for the roster it repaints afterwards.
    `already` counts the idempotent no-ops (a professional that already had a
    calendar), which is why re-running this is always safe.
    """

    created: int
    already: int
    failed: int
    items: list[ProfessionalCalendarBulkItem]


class ProfessionalListItem(BaseModel):
    """GET /tenants/me/professionals row — full per-professional config + completeness.

    A superset of `ProfessionalRead`: the list view is what the hub config
    screen uses to render each professional's card / edit-form, so it also
    carries the extended config fields (contract v1 §10) and onboarding
    completeness (services/tenant_config.professional_completeness_item) —
    letting the frontend prefill the PUT config form without a separate
    GET-by-id endpoint (not part of this contract).

    `business_hours` / `appointment_types` are this professional's OWN stored
    value, flattened to `{}` / `[]` when they have none — unchanged, because
    older clients already parse them that way. What used to be unknowable is
    WHICH of the two an empty value is, and that is what
    `business_hours_inherited` / `appointment_types_inherited` answer: they are
    additive, always present from this version on, and derived from the same
    `professional_inherits_*` predicates the runtime resolvers use, so the wire
    and the patient-facing behaviour cannot disagree. A client that does not
    know them is unaffected; a client that does must not present an inherited
    field as an own one (see the NULL-versus-EMPTY note in
    services/tenant_config.py).
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
    business_hours_inherited: bool
    appointment_types: list
    appointment_types_inherited: bool
    has_calendar: bool
    # "professional" | "tenant" | "none" — WHOSE Calendar credential covers this
    # row, additive alongside `has_calendar` (invariant:
    # has_calendar == calendar_source != "none"). A boolean alone could not tell
    # "this doctor connected their agenda" from "the clinic's connection covers
    # them", which is how the profile screen ended up offering "Reconectar
    # agenda" to a doctor who had never connected one. A category, never a
    # credential: no token, calendar id, OAuth scope or Google account rides on
    # this field or on any other in this model.
    calendar_source: str
    has_hours: bool
    has_services: bool
    complete: bool
