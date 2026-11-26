"""Shapes of the hub's /tenants/me/whatsapp/* routes (TASK-093 Plano 3, stage a).

Nothing here ever carries a secret: no token, no Meta code in a response, and the connected
number only as a masked tail (`phone_number_id_masked`).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

_META_ID = r"^[0-9]{1,64}$"


class EmbeddedSignupConfigOut(BaseModel):
    """The public ids the Meta browser SDK needs (null when not configured)."""

    app_id: str | None
    config_id: str | None


class WhatsAppAttemptIn(BaseModel):
    """POST /tenants/me/whatsapp/attempts - what the Embedded Signup popup handed back."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    code: str = Field(min_length=1, max_length=2048)
    phone_number_id: str = Field(pattern=_META_ID)
    waba_id: str = Field(pattern=_META_ID)


class WhatsAppAttemptOut(BaseModel):
    """The attempt's state - never the token."""

    attempt_id: str
    status: str
    failure_code: str | None


class WhatsAppConnectionOut(BaseModel):
    """The clinic's WhatsApp connection, safe for any hub user to read."""

    connected: bool
    phone_number_id_masked: str | None
    connected_at: datetime | None
    channel_released: bool
