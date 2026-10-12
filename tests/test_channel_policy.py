"""The channel policy table is a product decision; pin it (TASK-024).

If one of these assertions has to change, that is a change in how a channel behaves - decide it
on purpose and change it here in the same commit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, CHANNEL_WHATSAPP
from secretaria.workers.shared.channel_policy import PORTAL, WHATSAPP, policy_for


def test_portal_policy_values() -> None:
    assert policy_for(CHANNEL_BRAIN_MESSAGE) is PORTAL
    assert PORTAL.stamps_delivery_on_inbound is True
    assert PORTAL.redacts_email_code is True
    assert PORTAL.has_inline_identity is True
    assert PORTAL.arms_booking_gate is True
    assert PORTAL.greets_by_name is True
    assert PORTAL.reports_name_to_account is True
    assert PORTAL.shows_typing_indicator is True
    assert PORTAL.consent_scope == "no Portal Brain-Message"
    assert PORTAL.requires_whatsapp_activation is False


def test_whatsapp_policy_values() -> None:
    assert policy_for(CHANNEL_WHATSAPP) is WHATSAPP
    assert WHATSAPP.stamps_delivery_on_inbound is False
    assert WHATSAPP.redacts_email_code is False
    assert WHATSAPP.has_inline_identity is False
    assert WHATSAPP.arms_booking_gate is False
    assert WHATSAPP.greets_by_name is False
    assert WHATSAPP.reports_name_to_account is False
    assert WHATSAPP.shows_typing_indicator is False
    assert WHATSAPP.consent_scope == "no WhatsApp"
    assert WHATSAPP.requires_whatsapp_activation is True


@pytest.mark.parametrize("channel", [None, "", "telegram", "WHATSAPP"])
def test_unknown_channel_is_treated_as_whatsapp(channel) -> None:
    """The old `== CHANNEL_BRAIN_MESSAGE` checks treated everything else as WhatsApp."""
    assert policy_for(channel) is WHATSAPP


@pytest.mark.parametrize(
    ("name", "returning", "whatsapp_asks"),
    [(None, False, True), ("Ana", False, True), (None, True, True), ("Ana", True, False)],
)
def test_asking_the_name_matches_the_old_behaviour(name, returning, whatsapp_asks) -> None:
    patient = SimpleNamespace(name=name)
    assert WHATSAPP.asks_name_at_first_contact(patient, returning) is whatsapp_asks
    # The Portal never asks: the account service already owns the name.
    assert PORTAL.asks_name_at_first_contact(patient, returning) is False
