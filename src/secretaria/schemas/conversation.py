"""Request/response schemas for the doctor-hub conversations endpoints."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from secretaria.core.logging import get_logger
from secretaria.models.conversation import HandoverState

logger = get_logger(__name__)


class ConversationRead(BaseModel):
    """Response — whitelisted fields only (no message bodies ride along)."""

    id: str
    patient_wa_id: str | None
    patient_name: str | None
    handover_state: str
    last_message_at: datetime | None


class HandoverUpdate(BaseModel):
    """POST /tenants/me/conversations/{id}/handover."""

    state: HandoverState


class InteractiveOption(BaseModel):
    """One reply button or list row, as the patient's screen showed it."""

    id: str
    title: str
    description: str | None = None


class InteractiveRead(BaseModel):
    """What an interactive outbound message offered — `Message.interactive`.

    Recorded from the same arguments the channel payload was built from
    (services/whatsapp.py::interactive_buttons_record / interactive_list_record),
    so a console can draw the real controls instead of the flattened `body`.
    """

    kind: Literal["buttons", "list"]
    # The card's own text; never the "(opções: ...)" line history appends.
    body: str
    options: list[InteractiveOption]
    # List only: the button that opens the list, and the heading over its rows.
    button_label: str | None = None
    section_title: str | None = None


def interactive_read_or_none(blob: object, *, message_id: object) -> InteractiveRead | None:
    """A row's `Message.interactive` blob in its wire shape, or None.

    Tolerant on purpose, for every reader of the column (the staff hub and the
    Brain-Message portal route): a blob that no longer validates costs THAT
    message its controls (the caller still serves its text `body`), never the
    whole thread. One bad row failing the entire response is how the console
    once lost every thread of a tenant (`ConversationRead.patient_wa_id` on a
    NULL wa_id).
    """
    if not blob:
        return None
    try:
        return InteractiveRead.model_validate(blob)
    except ValidationError:
        logger.warning("message_interactive_invalid", message_id=str(message_id))
        return None


class MessageRead(BaseModel):
    """One message in a conversation thread — GET .../conversations/{id}/messages."""

    id: str
    direction: str
    sender: str
    # The history text the agent reads. For a list or a menu card it is
    # flattened ("... (opções: A, B)"): draw `interactive` instead when present.
    body: str | None
    created_at: datetime
    # Present on an outbound message that went out as reply buttons or a list;
    # None for text, for inbound rows and for rows older than the column. Both
    # channels record it: WhatsApp (workers/tasks.py::_record_outbound) and
    # Brain-Message (services/channel_sender.py::BrainMessageSender), whose
    # portal draws the same controls.
    interactive: InteractiveRead | None = None
    # On an inbound TAP: the id of the option tapped - one of an earlier
    # message's `interactive.options[].id`. Never shown; it is how a console
    # knows which option a reply chose. None for anything typed.
    interactive_reply_id: str | None = None


class MessageSend(BaseModel):
    """POST /tenants/me/conversations/{id}/messages — staff sends a message."""

    body: str = Field(min_length=1)
