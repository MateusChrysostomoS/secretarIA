"""The clinic's operational e-mail (`Tenant.contact_email`) — read and PATCH shapes.

Used by the hub (`PATCH /tenants/me/contact-email`) and by brain-api through the internal
endpoints (`GET/PATCH /internal/tenants/{id}/contact-email`). The address receives payment,
booking and handoff alerts, so the write is strictly validated and normalised here, once.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

CONTACT_EMAIL_MAX_LENGTH = 254  # matches the `tenants.contact_email` String(254) column

_LOCAL_ATOM = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_EMAIL_RE = re.compile(
    rf"(?P<local>{_LOCAL_ATOM}(?:\.{_LOCAL_ATOM})*)"
    rf"@(?P<domain>(?:{_DOMAIN_LABEL}\.)+(?:[a-z]{{2,63}}|xn--[a-z0-9-]{{1,59}}))"
)
_LOCAL_MAX_LENGTH = 64


def normalise_contact_email(value: Any) -> str | None:
    """Strip, lowercase, blank -> None; raise ValueError (no echo of the input) if invalid."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("contact_email must be a string or null")
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if len(cleaned) > CONTACT_EMAIL_MAX_LENGTH:
        raise ValueError(f"contact_email must be at most {CONTACT_EMAIL_MAX_LENGTH} characters")
    match = _EMAIL_RE.fullmatch(cleaned)
    if match is None or len(match.group("local")) > _LOCAL_MAX_LENGTH:
        raise ValueError("contact_email is not a valid e-mail address")
    return cleaned


class ContactEmailUpdate(BaseModel):
    """PATCH body. The key is REQUIRED: `{}` is a 422, so a typo never silently clears the
    address. An explicit `null` (or a blank string) clears it."""

    model_config = ConfigDict(hide_input_in_errors=True)

    contact_email: str | None

    @field_validator("contact_email", mode="before")
    @classmethod
    def _normalise(cls, value: Any) -> str | None:
        return normalise_contact_email(value)


class ContactEmailRead(BaseModel):
    contact_email: str | None
