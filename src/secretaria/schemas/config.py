"""Request/response schemas for the doctor-hub config endpoints.

All domain *shape* validation lives here (window end > start, no overlaps,
duration > 0, valid timezone, known weekday keys). The cross-cutting activation
rule (cannot go live without a connected Calendar) needs the DB and lives in
services/tenant_config.py instead.
"""

from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

# Convention: lowercase English weekday names, Monday-first.
WEEKDAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_HHMM = r"^\d{2}:\d{2}$"

# WhatsApp reply-button limits: at most 3 buttons, 20-char titles. An interactive
# message body caps at 1024 chars (a plain text message allows 4096), so a
# greeting that carries buttons must stay within the smaller limit.
MAX_GREETING_BUTTONS = 3
MAX_BUTTON_LABEL_CHARS = 20
MAX_GREETING_WITH_BUTTONS_CHARS = 1024


def _parse_hhmm(value: str) -> time:
    """Parse an "HH:MM" string into a time, raising ValueError if out of range."""
    hour_str, minute_str = value.split(":")
    return time(int(hour_str), int(minute_str))


class TimeWindow(BaseModel):
    """A single availability window within a day (local clinic time)."""

    start: str = Field(pattern=_HHMM, description='"HH:MM", e.g. "08:00"')
    end: str = Field(pattern=_HHMM, description='"HH:MM", e.g. "12:00"')

    @model_validator(mode="after")
    def _end_after_start(self) -> "TimeWindow":
        if _parse_hhmm(self.end) <= _parse_hhmm(self.start):
            raise ValueError(f"window end {self.end!r} must be after start {self.start!r}")
        return self


class AppointmentType(BaseModel):
    """A bookable reason for a consult, with its own duration."""

    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    duration_min: int = Field(gt=0, le=600)
    is_active: bool = True
    sort_order: int = 0


def _validate_business_hours(value: dict[str, list[TimeWindow]]) -> dict[str, list[TimeWindow]]:
    """Reject unknown weekday keys and overlapping windows within a day."""
    for day, windows in value.items():
        if day not in WEEKDAYS:
            raise ValueError(f"unknown weekday {day!r}; expected one of {WEEKDAYS}")
        ranges = sorted((_parse_hhmm(w.start), _parse_hhmm(w.end)) for w in windows)
        for (_, prev_end), (next_start, _) in zip(ranges, ranges[1:], strict=False):
            if next_start < prev_end:
                raise ValueError(f"overlapping availability windows on {day!r}")
    return value


class TenantConfigUpdate(BaseModel):
    """PUT body. Every field is optional — only provided fields are updated.

    `business_hours` and `appointment_types` are replaced wholesale (the JSON
    model edits the whole collection at once).
    """

    greeting_message: str | None = Field(default=None, max_length=4000)
    greeting_buttons: list[str] | None = None
    persona_notes: str | None = Field(default=None, max_length=4000)
    language: str | None = Field(default=None, max_length=8)
    timezone: str | None = None
    google_calendar_id: str | None = Field(default=None, max_length=255)
    appointment_duration_min: int | None = Field(default=None, gt=0, le=600)
    business_hours: dict[str, list[TimeWindow]] | None = None
    appointment_types: list[AppointmentType] | None = None
    is_active: bool | None = None

    @field_validator("business_hours")
    @classmethod
    def _check_business_hours(
        cls, value: dict[str, list[TimeWindow]] | None
    ) -> dict[str, list[TimeWindow]] | None:
        return None if value is None else _validate_business_hours(value)

    @field_validator("greeting_buttons")
    @classmethod
    def _check_greeting_buttons(cls, value: list[str] | None) -> list[str] | None:
        """Trim labels, reject blanks/dupes and enforce WhatsApp's 3x20 limits."""
        if value is None:
            return None
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            label = raw.strip()
            if not label:
                raise ValueError("greeting button labels cannot be blank")
            if len(label) > MAX_BUTTON_LABEL_CHARS:
                raise ValueError(
                    f"greeting button label {label!r} exceeds "
                    f"{MAX_BUTTON_LABEL_CHARS} characters"
                )
            key = label.casefold()
            if key in seen:
                raise ValueError(f"duplicate greeting button label {label!r}")
            seen.add(key)
            cleaned.append(label)
        if len(cleaned) > MAX_GREETING_BUTTONS:
            raise ValueError(f"at most {MAX_GREETING_BUTTONS} greeting buttons allowed")
        return cleaned

    @model_validator(mode="after")
    def _check_greeting_with_buttons(self) -> "TenantConfigUpdate":
        """A greeting that carries buttons must fit WhatsApp's interactive body cap.

        Only enforced when both fields arrive in the same request (the hub form
        sends them together); a buttons-only update can't see the stored body.
        """
        if (
            self.greeting_buttons
            and self.greeting_message is not None
            and len(self.greeting_message) > MAX_GREETING_WITH_BUTTONS_CHARS
        ):
            raise ValueError(
                f"a greeting with buttons must be at most "
                f"{MAX_GREETING_WITH_BUTTONS_CHARS} characters"
            )
        return self

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value


class TenantConfigRead(BaseModel):
    """GET/PUT response. Never includes secrets — only a `calendar_connected` flag."""

    clinic_name: str
    greeting_message: str | None
    greeting_buttons: list[str]
    persona_notes: str | None
    language: str
    timezone: str
    google_calendar_id: str
    appointment_duration_min: int
    business_hours: dict
    appointment_types: list
    is_active: bool
    # True when a Google Calendar refresh token is stored for this tenant.
    calendar_connected: bool
