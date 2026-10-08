"""bubbles - split out of workers/tasks.py (TASK-023)."""

from secretaria.ai.formatter import (
    BUTTON_ID_CANCEL,
    BUTTON_ID_CONFIRM,
    ButtonBubble,
    SlotsBubble,
    TextBubble,
)
from secretaria.core.whatsapp_limits import MAX_BUTTONS_PER_MESSAGE
from secretaria.services.flow_router import (
    MenuBubble,
)
from secretaria.services.whatsapp import (
    WhatsAppClient,
    interactive_buttons_record,
    interactive_list_record,
)


def _bubble_buttons(
    bubble: TextBubble | ButtonBubble | SlotsBubble | MenuBubble,
) -> list[tuple[str, str]] | None:
    """The (id, label) pairs a button card goes out with; None for any other bubble.

    Shared by the send (`_send_bubble`) and its record (`_bubble_interactive`),
    so the ids the console links a later tap to are the ids WhatsApp sent.
    """
    if isinstance(bubble, MenuBubble):
        # Generic N-button reply card; the tapped label becomes the next body.
        return [(f"menu|{i}", label) for i, label in enumerate(bubble.labels)]
    if isinstance(bubble, ButtonBubble):
        return [
            (BUTTON_ID_CONFIRM, bubble.confirm_label),
            (BUTTON_ID_CANCEL, bubble.cancel_label),
        ]
    return None

def _slots_rows(bubble: SlotsBubble) -> list[tuple[str, str, str | None]]:
    # Rows are (id, title) or (id, title, description) — see SlotsBubble.
    return [(row[0], row[1], row[2] if len(row) > 2 else None) for row in bubble.rows]

WIDE_MENU_BUTTON_LABEL = "Ver opções"
WIDE_MENU_SECTION_TITLE = "Opções"


def _client_max_buttons(client) -> int:
    """How many reply buttons `client`'s channel draws (WhatsApp 3, the Portal more)."""
    return int(getattr(client, "MAX_BUTTONS", MAX_BUTTONS_PER_MESSAGE))


def _for_client(
    bubble: TextBubble | ButtonBubble | SlotsBubble | MenuBubble, client
) -> TextBubble | ButtonBubble | SlotsBubble | MenuBubble:
    """A menu with more labels than `client` has buttons becomes a tappable list.

    The router stays channel-neutral: it asks for N options as a `MenuBubble`. WhatsApp
    would silently cut anything past three (`interactive_buttons_record`), so there the
    same options go out as a list - same ids ("menu|<i>"), same titles, so the tap comes
    back as the label exactly like a button's. Idempotent: a list is returned as is.
    """
    if isinstance(bubble, MenuBubble) and len(bubble.labels) > _client_max_buttons(client):
        return SlotsBubble(
            body=bubble.body,
            rows=[(f"menu|{index}", label, None) for index, label in enumerate(bubble.labels)],
            button_label=WIDE_MENU_BUTTON_LABEL,
            section_title=WIDE_MENU_SECTION_TITLE,
        )
    return bubble


async def _send_bubble(
    client: WhatsAppClient,
    to: str,
    bubble: TextBubble | ButtonBubble | SlotsBubble | MenuBubble,
) -> dict:
    """Dispatch a single bubble to the right WhatsAppClient method."""
    bubble = _for_client(bubble, client)
    buttons = _bubble_buttons(bubble)
    if buttons is not None:
        return await client.send_buttons(to=to, body=bubble.body, buttons=buttons)
    if isinstance(bubble, SlotsBubble):
        return await client.send_list(
            to=to,
            body=bubble.body,
            button_label=bubble.button_label,
            rows=_slots_rows(bubble),
            section_title=bubble.section_title,
        )
    return await client.send_text_message(to=to, body=bubble.body)

def _bubble_history_body(bubble: TextBubble | ButtonBubble | SlotsBubble | MenuBubble) -> str:
    """Render an outbound bubble as the text the LLM should see in history.

    Interactive cards collapse to a clean string (no markup tags) so the
    next agent turn rebuilt from the DB does not see leftover `[CONFIRM]`
    syntax and try to repeat it.
    """
    if isinstance(bubble, ButtonBubble):
        return bubble.body
    if isinstance(bubble, MenuBubble):
        labels = ", ".join(bubble.labels)
        return f"{bubble.body}\n(opções: {labels})" if labels else bubble.body
    if isinstance(bubble, SlotsBubble):
        labels = ", ".join(row[1] for row in bubble.rows)
        return f"{bubble.body}\n(opções: {labels})" if labels else bubble.body
    return bubble.body

def _bubble_interactive(
    bubble: TextBubble | ButtonBubble | SlotsBubble | MenuBubble,
    *,
    max_buttons: int = MAX_BUTTONS_PER_MESSAGE,
) -> dict | None:
    """What an outbound bubble put on the patient's screen, for `Message.interactive`.

    The human-facing twin of `_bubble_history_body` above, which keeps
    flattening the card for the agent and does not change: this one keeps the
    options, so the staff console can draw the reply buttons / the list the
    patient got - and link a later tap back to them by id. Built from the same
    arguments `_send_bubble` sends (`_bubble_buttons` / `_slots_rows`), through
    the record WhatsAppClient builds its payload from. None for a text bubble
    and for a card with nothing to offer.
    """
    buttons = _bubble_buttons(bubble)
    if buttons:
        return interactive_buttons_record(bubble.body, buttons, max_buttons=max_buttons)
    if isinstance(bubble, SlotsBubble) and bubble.rows:
        return interactive_list_record(
            bubble.body, bubble.button_label, _slots_rows(bubble), bubble.section_title
        )
    return None
