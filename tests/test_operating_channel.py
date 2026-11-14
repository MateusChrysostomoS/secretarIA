"""services/operating_channel.py (TASK-084): which channels a clinic may choose, and the switch.

WhatsApp is a known value but NOT released (owner, 2026-11-14): it is listed nowhere a
clinic can pick it and `set_operating_channel` refuses it. Releasing it is one edit to
RELEASED_OPERATING_CHANNELS - these tests flip that constant to prove the rest follows.
"""

from uuid import uuid4

import pytest

from secretaria.models import Tenant
from secretaria.services import operating_channel as service
from secretaria.services.operating_channel import (
    OperatingChannelUnavailable,
    available_operating_channels,
    set_operating_channel,
)


def _tenant(channel: str = "brain_message") -> Tenant:
    return Tenant(id=uuid4(), clinic_name="Clínica", operating_channel=channel)


@pytest.fixture
def released_whatsapp(monkeypatch):
    monkeypatch.setattr(service, "RELEASED_OPERATING_CHANNELS", ("brain_message", "whatsapp"))


def test_only_the_portal_is_available_today():
    assert service.RELEASED_OPERATING_CHANNELS == ("brain_message",)
    assert available_operating_channels() == ["brain_message"]


def test_released_whatsapp_lists_both_in_fixed_order(released_whatsapp):
    assert available_operating_channels() == ["brain_message", "whatsapp"]


def test_release_order_does_not_change_the_display_order(monkeypatch):
    monkeypatch.setattr(service, "RELEASED_OPERATING_CHANNELS", ("whatsapp", "brain_message"))
    assert available_operating_channels() == ["brain_message", "whatsapp"]


def test_same_channel_is_a_no_op():
    tenant = _tenant("brain_message")
    assert set_operating_channel(tenant, "brain_message") is False
    assert tenant.operating_channel == "brain_message"


def test_unreleased_channel_raises_and_changes_nothing():
    tenant = _tenant("brain_message")
    with pytest.raises(OperatingChannelUnavailable) as caught:
        set_operating_channel(tenant, "whatsapp")
    assert caught.value.channel == "whatsapp"
    assert tenant.operating_channel == "brain_message"


def test_released_channel_is_set(released_whatsapp):
    tenant = _tenant("brain_message")
    assert set_operating_channel(tenant, "whatsapp") is True
    assert tenant.operating_channel == "whatsapp"


def test_a_clinic_already_on_an_unreleased_channel_may_restate_it():
    # A rollback of the release must not lock a clinic that already moved: restating its
    # own channel is a no-op, never a 409.
    tenant = _tenant("whatsapp")
    assert set_operating_channel(tenant, "whatsapp") is False
    assert tenant.operating_channel == "whatsapp"


def test_unknown_channel_is_a_value_error():
    tenant = _tenant("brain_message")
    with pytest.raises(ValueError):
        set_operating_channel(tenant, "sms")
    assert tenant.operating_channel == "brain_message"
