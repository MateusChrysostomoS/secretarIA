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


class ProfessionalUnitsUpdate(BaseModel):
    """PUT body: the whole wanted set (empty list = no links)."""

    model_config = ConfigDict(extra="forbid")

    links: list[ProfessionalUnitLink] = Field(max_length=100)

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


class ProfessionalUnitsRead(BaseModel):
    units: list[ProfessionalUnitRead]
