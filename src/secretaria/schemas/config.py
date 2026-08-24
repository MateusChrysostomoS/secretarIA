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

from secretaria.core.whatsapp_limits import (
    MAX_BUTTON_LABEL_CHARS,
    MAX_BUTTONS_PER_MESSAGE as MAX_GREETING_BUTTONS,
    MAX_INTERACTIVE_BODY_CHARS,
)

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

# Re-exported under this module's historical names so hub validation and its
# tests keep importing from here, while core/whatsapp_limits.py stays the ONE
# place the numbers are written down (and the one the send path reads).
MAX_GREETING_WITH_BUTTONS_CHARS = MAX_INTERACTIVE_BODY_CHARS

__all__ = [
    "MAX_BUTTON_LABEL_CHARS",
    "MAX_GREETING_BUTTONS",
    "MAX_GREETING_WITH_BUTTONS_CHARS",
]


def _parse_hhmm(value: str) -> time:
    """Parse an "HH:MM" string into a time, raising ValueError if out of range."""
    hour_str, minute_str = value.split(":")
    return time(int(hour_str), int(minute_str))


def clean_requirements(value: list[str]) -> list[str]:
    """Trim items, drop blanks, and cap count/length.

    ONE rule, shared by every surface that accepts pre-consult orientations:
    the per-professional/tenant `AppointmentType` below and the canonical
    catalog's `ServiceCreate`/`ServiceUpdate` (schemas/service.py). They
    describe the same field and must not drift apart — same reasoning as
    `schemas/professional.py` importing `AppointmentType` rather than
    redeclaring it.
    """
    cleaned = [v.strip() for v in value if v and v.strip()]
    for item in cleaned:
        if len(item) > 300:
            raise ValueError(f"requirement {item!r} exceeds 300 characters")
    if len(cleaned) > 20:
        raise ValueError("at most 20 requirements allowed")
    return cleaned


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
    """A bookable reason for a consult, with its own duration.

    `service_id` is what makes two professionals' "Limpeza" provably the SAME
    service (models/service.py, services/service_catalog.py). Optional, because
    every entry written before the catalog existed lacks one and still resolves
    by normalized name — but a client that sends it is the only way the link is
    ever written, so it must survive validation. Without this field Pydantic
    would drop it silently (`extra="ignore"` is the default) and the catalog
    could never be reached from the hub at all.

    Kept as `str`, not `UUID`: the value round-trips through a JSON column, and
    `service_catalog.entry_service_id` already parses it tolerantly so a
    malformed one reads as "not linked" rather than exploding a booking.
    """

    name: str = Field(min_length=1, max_length=120)
    service_id: str | None = Field(default=None, max_length=64)
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
        return clean_requirements(value)


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
    persona_notes: str | None = Field(default=None, max_length=4000)
    post_consult_message: str | None = Field(default=None, max_length=4000)
    post_consult_knowledge: str | None = Field(default=None, max_length=4000)
    language: str | None = Field(default=None, max_length=8)
    timezone: str | None = None
    google_calendar_id: str | None = Field(default=None, max_length=255)
    # "per_professional" (default) | "shared_account" — see
    # services/tenant_config.py's routing rule. Switching modes is
    # non-destructive: never touches tokens or professional.google_calendar_id.
    google_calendar_mode: Literal["per_professional", "shared_account"] | None = None
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

    @model_validator(mode="after")
    def _check_greeting_within_button_cap(self) -> "TenantConfigUpdate":
        """The greeting always ships with fixed action buttons attached.

        Since the fixed-greeting-buttons round (see
        docs/CHECKPOINT_fixed_greeting_buttons.md), `greeting_message` /
        `returning_greeting_message` are ALWAYS sent with the product-defined
        action buttons (workers/tasks.py::_greeting_buttons_for) whenever
        either is configured at all - there is no more "buttons vs plain
        text" choice for the hub to make. So each must unconditionally fit
        WhatsApp's smaller interactive-body cap (1024 chars), not the plain
        4096-char text cap the Field on its own allows.
        """
        for value in (self.greeting_message, self.returning_greeting_message):
            if value is not None and len(value) > MAX_GREETING_WITH_BUTTONS_CHARS:
                raise ValueError(
                    f"greeting must be at most {MAX_GREETING_WITH_BUTTONS_CHARS} "
                    f"characters (it is always sent with action buttons attached)"
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
    """GET/PUT response. Never includes secrets — only a `calendar_connected` flag.

    `greeting_buttons` is INTENTIONALLY absent: the greeting's buttons are a
    fixed, product-defined set (workers/tasks.py::_greeting_buttons_for), not
    hub-editable, so there is nothing left for this response to report - see
    docs/CHECKPOINT_fixed_greeting_buttons.md for the full contract change.
    """

    clinic_name: str
    greeting_message: str | None
    returning_greeting_message: str | None
    persona_notes: str | None
    post_consult_message: str | None
    post_consult_knowledge: str | None
    language: str
    timezone: str
    google_calendar_id: str
    google_calendar_mode: str
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
