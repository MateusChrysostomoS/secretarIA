"""Passwords and recipient data persist only as an encrypted, expiring job envelope."""

import json
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, SecretStr

from secretaria.core.crypto import decrypt, encrypt


class StaffWelcomeEnvelope(BaseModel):
    # Identity authority validates email; keep the receiver dependency-free.
    to: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=255)
    clinic_name: str = Field(min_length=1, max_length=255)
    temporary_password: SecretStr = Field(min_length=8, max_length=72)
    login_url: HttpUrl
    tenant_id: UUID
    user_id: UUID
    credential_version: int = Field(ge=0)
    expires_at: datetime


def seal_staff_welcome(payload: dict) -> tuple[str, int]:
    envelope = StaffWelcomeEnvelope.model_validate(payload)
    if (
        envelope.expires_at.tzinfo is None
        or envelope.login_url.username
        or envelope.login_url.password
    ):
        raise ValueError("Invalid staff welcome envelope")
    remaining = int((envelope.expires_at - datetime.now(UTC)).total_seconds())
    if not 0 < remaining <= 72 * 3600:
        raise ValueError("Invalid staff welcome expiry")
    data = envelope.model_dump(mode="json", exclude={"temporary_password"})
    data["temporary_password"] = envelope.temporary_password.get_secret_value()
    return encrypt(json.dumps(data)), remaining


def open_staff_welcome(payload_encrypted: str) -> StaffWelcomeEnvelope:
    envelope = StaffWelcomeEnvelope.model_validate_json(decrypt(payload_encrypted))
    if envelope.expires_at.tzinfo is None or envelope.expires_at <= datetime.now(UTC):
        raise ValueError("Expired staff welcome envelope")
    return envelope
