"""Request/response schemas for the doctor-hub config endpoints.

All domain *shape* validation lives here (window end > start, no overlaps,
duration > 0, valid timezone, known weekday keys). The cross-cutting activation
rule (cannot go live without a connected Calendar) needs the DB and lives in
services/tenant_config.py instead.
"""

from datetime import time
from typing import Literal
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
    # Display price shown in the service catalog, e.g. "R$ 250,00". Free text so
    # the clinic controls formatting/currency.
    price: str | None = Field(default=None, max_length=40)
    # Longer copy shown on the service detail step of the deterministic flow.
    long_description: str | None = Field(default=None, max_length=2000)
    # Short pre-consult orientations shown to the patient, e.g. "Jejum de 8 horas".
    requirements: list[str] = Field(default_factory=list)

    @field_validator("requirements")
    @classmethod
    def _check_requirements(cls, value: list[str]) -> list[str]:
        """Trim items, drop blanks, and cap count/length (same style as
        TenantConfigUpdate._check_insurances below)."""
        cleaned = [v.strip() for v in value if v and v.strip()]
        for item in cleaned:
            if len(item) > 300:
                raise ValueError(f"requirement {item!r} exceeds 300 characters")
        if len(cleaned) > 20:
            raise ValueError("at most 20 requirements allowed")
        return cleaned


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


class TenantAddress(BaseModel):
    """Clinic's physical address (contract v1 §10). Every field optional —
    a clinic may fill in only what it has (e.g. no `complement`)."""

    line: str | None = Field(default=None, max_length=255)
    complement: str | None = Field(default=None, max_length=255)
    neighborhood: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=60)
    postal_code: str | None = Field(default=None, max_length=20)


class TenantConfigUpdate(BaseModel):
    """PUT body. Every field is optional — only provided fields are updated.

    `business_hours` and `appointment_types` are replaced wholesale (the JSON
    model edits the whole collection at once). CRITICAL invariant (contract
    v1 §10): the can_activate gate below only fires when `is_active` is
    EXPLICITLY `true` in the request body — a plain config save (this whole
    class's purpose) that omits `is_active`, including one that only sets
    `address`/`insurances`/`collect_insurance`, is ALWAYS allowed regardless
    of whether the WhatsApp number is connected yet. See api/hub/config.py's
    `update_config`.
    """

    greeting_message: str | None = Field(default=None, max_length=4000)
    returning_greeting_message: str | None = Field(default=None, max_length=4000)
    greeting_buttons: list[str] | None = None
    persona_notes: str | None = Field(default=None, max_length=4000)
    post_consult_message: str | None = Field(default=None, max_length=4000)
    post_consult_knowledge: str | None = Field(default=None, max_length=4000)
    language: str | None = Field(default=None, max_length=8)
    timezone: str | None = None
    google_calendar_id: str | None = Field(default=None, max_length=255)
    appointment_duration_min: int | None = Field(default=None, gt=0, le=600)
    business_hours: dict[str, list[TimeWindow]] | None = None
    appointment_types: list[AppointmentType] | None = None
    initial_flows: dict | None = None
    # Clinic physical address. NULL/omitted = leave untouched (or "not
    # collected yet" on first save).
    address: TenantAddress | None = None
    # Accepted health-insurance plan names, e.g. ["Unimed", "Amil"].
    insurances: list[str] | None = None
    # Whether the bot should ask patients for their insurance during booking.
    collect_insurance: bool | None = None
    # --- Pix deposit (sinal) policy — services/payments/deposit_lifecycle.py ---
    pix_deposit_enabled: bool | None = None
    pix_deposit_percent: int | None = Field(default=None, ge=1, le=100)
    pix_refund_window_hours: int | None = Field(default=None, ge=0)
    pix_retention_policy: Literal["total", "partial"] | None = None
    pix_partial_refund_percent: int | None = Field(default=None, ge=1, le=100)
    pix_reschedule_limit: int | None = Field(default=None, ge=0)
    is_active: bool | None = None

    @field_validator("insurances")
    @classmethod
    def _check_insurances(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [v.strip() for v in value if v and v.strip()]
        if len(cleaned) > 50:
            raise ValueError("at most 50 insurance names allowed")
        return cleaned

    @field_validator("initial_flows")
    @classmethod
    def _check_initial_flows(cls, value: dict | None) -> dict | None:
        """Light shape check for the deterministic-flow config blob."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("initial_flows must be an object")
        buttons = value.get("buttons")
        if buttons is not None:
            if not isinstance(buttons, list) or len(buttons) > MAX_GREETING_BUTTONS:
                raise ValueError(
                    f"initial_flows.buttons must be a list of at most "
                    f"{MAX_GREETING_BUTTONS} labels"
                )
            for label in buttons:
                if not isinstance(label, str) or not label.strip():
                    raise ValueError("initial_flows.buttons labels must be non-empty strings")
                if len(label.strip()) > MAX_BUTTON_LABEL_CHARS:
                    raise ValueError(
                        f"initial_flows.buttons label {label!r} exceeds "
                        f"{MAX_BUTTON_LABEL_CHARS} characters (WhatsApp button limit)"
                    )
        return value

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
    returning_greeting_message: str | None
    greeting_buttons: list[str]
    persona_notes: str | None
    post_consult_message: str | None
    post_consult_knowledge: str | None
    language: str
    timezone: str
    google_calendar_id: str
    appointment_duration_min: int
    business_hours: dict
    appointment_types: list
    initial_flows: dict
    address: dict | None
    insurances: list[str]
    collect_insurance: bool
    is_active: bool
    # True when a Google Calendar refresh token is stored for this tenant.
    calendar_connected: bool
    # --- Pix deposit (sinal) policy — services/payments/deposit_lifecycle.py ---
    pix_deposit_enabled: bool
    pix_deposit_percent: int
    pix_refund_window_hours: int
    pix_retention_policy: str
    pix_partial_refund_percent: int
    pix_reschedule_limit: int
    # True when an Asaas API key is stored for this tenant. Read-only/derived
    # (from has_asaas_api_key) — NOT declared on TenantConfigUpdate, so a PUT
    # can never flip it; it only ever changes via
    # POST /internal/tenants/{id}/asaas-connection.
    asaas_connected: bool
