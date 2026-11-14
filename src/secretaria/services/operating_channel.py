"""Where the secretarIA serves a clinic's patients: which channels it may choose, and the switch.

TASK-084 (owner, 2026-11-14): "faça todo o código para que seja possível alterar o ambiente
de funcionamento entre Portal Brain e WhatsApp, mas que hoje esse toggle seja apenas visual e
que não seja possível mudar para o WhatsApp".

So both channels are VALUES of `Tenant.operating_channel`, but only the released ones can be
chosen. RELEASED_OPERATING_CHANNELS is the ONE switch: the hub's read
(`available_operating_channels`) and its write (`set_operating_channel`, a 409 at the route)
both derive from it, and the /contexto screen enables whatever the read lists. Releasing
WhatsApp = adding it here (and the checklist in docs/CHECKPOINT_ambiente_secretaria.md).

A code constant, deliberately not an environment variable: the release needs code anyway, and
nobody should be able to open it from a panel by accident.

Nothing on the message path reads the channel yet - a conversation is still answered on the
channel it arrived on (workers/shared/sender.py::_reply_sender).
"""

from __future__ import annotations

from secretaria.models.tenant import OPERATING_CHANNEL_PORTAL, OPERATING_CHANNELS, Tenant

# The channels a clinic may choose today. WhatsApp is built but NOT released.
RELEASED_OPERATING_CHANNELS: tuple[str, ...] = (OPERATING_CHANNEL_PORTAL,)


class OperatingChannelUnavailable(Exception):
    """The clinic asked for a known channel that is not released yet."""

    def __init__(self, channel: str) -> None:
        super().__init__(f"operating channel not available: {channel}")
        self.channel = channel


def available_operating_channels() -> list[str]:
    """The released channels, always in the display order of OPERATING_CHANNELS."""
    return [channel for channel in OPERATING_CHANNELS if channel in RELEASED_OPERATING_CHANNELS]


def set_operating_channel(tenant: Tenant, channel: str) -> bool:
    """Move `tenant` to `channel`; True when it changed, False when it was already there.

    Restating the current channel is always a no-op, released or not, so a clinic that moved
    before a release was rolled back is never locked out of saving. An unknown value is a
    programming error (the schema refuses it first); a known but unreleased one raises
    OperatingChannelUnavailable and leaves the tenant untouched.
    """
    if channel not in OPERATING_CHANNELS:
        raise ValueError(f"unknown operating channel: {channel!r}")
    if tenant.operating_channel == channel:
        return False
    if channel not in RELEASED_OPERATING_CHANNELS:
        raise OperatingChannelUnavailable(channel)
    tenant.operating_channel = channel
    return True
