"""Schemas for GET/PUT /tenants/me/professionals/{id}/units (TASK-083, spec §9).

`business_hours` reuses the professional's validator (schemas/config.py::_validate_business_hours),
imported and not reimplemented, so a unit's schedule can never drift from the professional's own.
Three states, as everywhere: absent/`null` inherit the professional's hours, `{}` is closed all
week in that unit, a non-empty value is the unit's own schedule.

Both request models forbid extra fields: the client never supplies `agenda_id`/`person_key` or a
link's `is_active`/`sort_order` (the server decides them).
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from secretaria.schemas.config import TimeWindow, _validate_business_hours


def _canonical(unit_id: str) -> str:
    """The UUID's canonical text, so spelling variants of one id count as duplicates."""
    try:
        return str(UUID(unit_id))
    except ValueError:
        return unit_id.strip().lower()


class ProfessionalUnitLink(BaseModel):
    """One wanted link in the PUT body."""

    model_config = ConfigDict(extra="forbid")

    unit_id: str = Field(min_length=1, max_length=64)
    business_hours: dict[str, list[TimeWindow]] | None = None

    @field_validator("business_hours")
    @classmethod
    def _check_business_hours(
        cls, value: dict[str, list[TimeWindow]] | None
    ) -> dict[str, list[TimeWindow]] | None:
        return None if value is None else _validate_business_hours(value)

    def hours_as_json(self) -> dict | None:
        """The column value: plain dicts, `None` kept as `None` (inherit), `{}` kept as `{}`."""
        if self.business_hours is None:
            return None
        return {
            day: [window.model_dump() for window in windows]
            for day, windows in self.business_hours.items()
        }


class ServiceUnitMark(BaseModel):
    """A service restricted to these units (TASK-083 follow-up): done ONLY there.

    A service without a mark is done at every unit of the professional. A mark always lists at
    least one unit: "none" is never stored. Used both in the PUT body and in the GET answer.
    """

    model_config = ConfigDict(extra="forbid")

    service_id: str = Field(min_length=1, max_length=64)
    unit_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("service_id")
    @classmethod
    def _canonical_service_id(cls, value: str) -> str:
        try:
            return str(UUID(value))
        except ValueError:
            raise ValueError("service_id must be a UUID") from None


class ProfessionalUnitsUpdate(BaseModel):
    """PUT body: the whole wanted set (empty list = no links).

    `service_marks` is the COMPLETE wanted restriction state: a service absent from it is done at
    every unit; `null`/omitted leaves the stored marks alone (links-only save).
    """

    model_config = ConfigDict(extra="forbid")

    links: list[ProfessionalUnitLink] = Field(max_length=100)
    service_marks: list[ServiceUnitMark] | None = Field(default=None, max_length=500)

    @field_validator("service_marks")
    @classmethod
    def _no_duplicate_services(
        cls, value: list[ServiceUnitMark] | None
    ) -> list[ServiceUnitMark] | None:
        if value is not None:
            ids = [mark.service_id for mark in value]
            if len(ids) != len(set(ids)):
                raise ValueError("a service can appear only once in service_marks")
        return value

    @field_validator("links")
    @classmethod
    def _no_duplicate_units(cls, value: list[ProfessionalUnitLink]) -> list[ProfessionalUnitLink]:
        ids = [_canonical(link.unit_id) for link in value]
        if len(ids) != len(set(ids)):
            raise ValueError("a unit can appear only once in links")
        return value


class ProfessionalUnitRead(BaseModel):
    """One ACTIVE unit of the tenant, with this professional's link state."""

    unit_id: str
    name: str
    address: str | None
    is_active: bool
    linked: bool
    # The link's own hours: null = inherits the professional's (or not linked), {} = closed.
    business_hours: dict | None


class ProfessionalServiceRead(BaseModel):
    """A service of the professional's active catalog (the ones a mark can be put on)."""

    service_id: str
    name: str


class ProfessionalUnitsRead(BaseModel):
    units: list[ProfessionalUnitRead]
    # The professional's active catalog services that have a `service_id` ...
    services: list[ProfessionalServiceRead] = []
    # ... and, of those, the RESTRICTED ones with their units (the rest mean "every unit").
    service_marks: list[ServiceUnitMark] = []
