"""The clinic's operating channel (`Tenant.operating_channel`, TASK-084) - read and PATCH shapes.

`brain_message` is the Portal Brain chat, `whatsapp` the clinic's WhatsApp number - the
`Patient.channel` vocabulary. Which of them a clinic may CHOOSE is not a schema question:
services/operating_channel.py answers it, and the route turns a refusal into a 409.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

OperatingChannel = Literal["brain_message", "whatsapp"]


class OperatingChannelUpdate(BaseModel):
    """PATCH /tenants/me/operating-channel body. The key is required; nothing else is accepted."""

    model_config = ConfigDict(extra="forbid")

    operating_channel: OperatingChannel


class OperatingChannelRead(BaseModel):
    """What the clinic is on, and what it may choose (only released channels, fixed order)."""

    operating_channel: OperatingChannel
    available_operating_channels: list[OperatingChannel]
