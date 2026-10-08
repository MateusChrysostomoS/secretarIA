"""A menu with more labels than the channel has buttons (TASK-032 R6, spec §5.3)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from secretaria.ai.formatter import SlotsBubble  # noqa: E402
from secretaria.services.channel_sender import BrainMessageSender  # noqa: E402
from secretaria.services.flow_router import MenuBubble  # noqa: E402
from secretaria.services.whatsapp import WhatsAppClient, interactive_buttons_record  # noqa: E402
from secretaria.workers.shared.bubbles import (  # noqa: E402
    _bubble_interactive,
    _client_max_buttons,
    _for_client,
    _send_bubble,
)

LABELS = ["Mudar data", "Mudar horário", "Mudar serviço", "Mudar médico", "Outro"]


class _Recorder:
    def __init__(self, max_buttons: int) -> None:
        self.MAX_BUTTONS = max_buttons
        self.calls: list[tuple] = []

    async def send_buttons(self, to, body, buttons):
        self.calls.append(("buttons", body, buttons))
        return {}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        self.calls.append(("list", body, button_label, rows, section_title))
        return {}

    async def send_text_message(self, to, body):
        self.calls.append(("text", body))
        return {}


async def test_five_labels_become_a_list_on_a_three_button_channel():
    client = _Recorder(3)

    await _send_bubble(client, "to", MenuBubble(body="O que mudar?", labels=LABELS))

    [(kind, body, button_label, rows, _section)] = client.calls
    assert (kind, body, button_label) == ("list", "O que mudar?", "Ver opções")
    assert [row[0] for row in rows] == [f"menu|{i}" for i in range(5)]
    assert [row[1] for row in rows] == LABELS


async def test_five_labels_stay_buttons_on_a_wide_channel():
    client = _Recorder(8)

    await _send_bubble(client, "to", MenuBubble(body="O que mudar?", labels=LABELS))

    [(kind, _body, buttons)] = client.calls
    assert kind == "buttons"
    assert [label for _id, label in buttons] == LABELS


async def test_a_menu_that_fits_is_never_touched():
    client = _Recorder(3)
    bubble = MenuBubble(body="b", labels=["Sim", "Não"])
    assert _for_client(bubble, client) is bubble
    await _send_bubble(client, "to", bubble)
    assert client.calls[0][0] == "buttons"


def test_the_record_matches_what_was_sent_on_each_channel():
    menu = MenuBubble(body="O que mudar?", labels=LABELS)
    narrow, wide = _Recorder(3), _Recorder(8)

    as_list = _bubble_interactive(
        _for_client(menu, narrow), max_buttons=_client_max_buttons(narrow)
    )
    as_buttons = _bubble_interactive(_for_client(menu, wide), max_buttons=_client_max_buttons(wide))

    assert as_list["kind"] == "list"
    assert [option["title"] for option in as_list["options"]] == LABELS
    assert as_buttons["kind"] == "buttons"
    assert [option["title"] for option in as_buttons["options"]] == LABELS


def test_the_buttons_record_cap_is_configurable_and_defaults_to_three():
    buttons = [(f"b|{i}", label) for i, label in enumerate(LABELS)]
    assert len(interactive_buttons_record("b", buttons)["options"]) == 3
    assert len(interactive_buttons_record("b", buttons, max_buttons=8)["options"]) == 5


def test_each_channel_declares_how_many_buttons_it_draws():
    assert WhatsAppClient.MAX_BUTTONS == 3
    assert BrainMessageSender.MAX_BUTTONS >= 5
    assert isinstance(_for_client(MenuBubble(body="b", labels=LABELS), _Recorder(3)), SlotsBubble)
