"""Channel policy: what differs between the channels, as data (TASK-024).

Before this module, neutral worker code asked "is this the Portal?" in about a dozen places
(`channel == CHANNEL_BRAIN_MESSAGE`). Each of those is a question about a CAPABILITY of the
channel, so each now reads a field of the channel's policy instead. A third channel is one
more row here; enabling a Portal-only behaviour on WhatsApp is flipping one field.

Rules for this file:
  * data only - no I/O, no imports from `whatsapp/` or `portal/`;
  * an unknown or missing channel gets the WHATSAPP policy, which is exactly what the old
    `== CHANNEL_BRAIN_MESSAGE` comparisons did (anything else was treated as WhatsApp);
  * `tests/test_channel_policy.py` pins every value below. Changing one is a product
    decision and must change that test in the same commit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, CHANNEL_WHATSAPP


@dataclass(frozen=True)
class ChannelPolicy:
    channel: str
    # Brain-Message: reaching the clinic IS delivery, so the inbound row is stamped delivered.
    stamps_delivery_on_inbound: bool
    # A six-digit login code typed in chat is stored as "[código oculto]", not as typed.
    redacts_email_code: bool
    # The visitor proves an e-mail inside the chat (e-mail -> code -> "quer continuar?").
    has_inline_identity: bool
    # A booking waits for the e-mail code before the appointment is really created.
    arms_booking_gate: bool
    # The greeting uses the account's known name.
    greets_by_name: bool
    # The name given in chat is also reported to the account service (brain-api).
    reports_name_to_account: bool
    # The Portal shows "digitando" while the automation prepares a reply (TASK-033).
    shows_typing_indicator: bool
    # Where the LGPD consent was given, as written in the audit record.
    consent_scope: str
    # (patient, is_returning_patient) -> ask "qual o seu nome?" at first contact.
    asks_name_at_first_contact: Callable[[Any, bool], bool]


def _whatsapp_asks_name(patient: Any, is_returning_patient: bool) -> bool:
    return not is_returning_patient or not patient.name


def _portal_asks_name(patient: Any, is_returning_patient: bool) -> bool:
    return False


WHATSAPP = ChannelPolicy(
    channel=CHANNEL_WHATSAPP,
    stamps_delivery_on_inbound=False,
    redacts_email_code=False,
    has_inline_identity=False,
    arms_booking_gate=False,
    greets_by_name=False,
    reports_name_to_account=False,
    shows_typing_indicator=False,
    consent_scope="no WhatsApp",
    asks_name_at_first_contact=_whatsapp_asks_name,
)

PORTAL = ChannelPolicy(
    channel=CHANNEL_BRAIN_MESSAGE,
    stamps_delivery_on_inbound=True,
    redacts_email_code=True,
    has_inline_identity=True,
    arms_booking_gate=True,
    greets_by_name=True,
    reports_name_to_account=True,
    shows_typing_indicator=True,
    consent_scope="no Portal Brain-Message",
    asks_name_at_first_contact=_portal_asks_name,
)

_BY_CHANNEL = {WHATSAPP.channel: WHATSAPP, PORTAL.channel: PORTAL}


def policy_for(channel: str | None) -> ChannelPolicy:
    """The policy of `channel`; WhatsApp's for anything unknown or None."""
    return _BY_CHANNEL.get(channel, WHATSAPP)
