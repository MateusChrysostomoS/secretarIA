"""Request/response schemas for the doctor-hub conversations endpoints."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from secretaria.models.conversation import HandoverState


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
    # None for text, for inbound rows, for rows older than the column, and on
    # Brain-Message (whose patient receives the options as text).
    interactive: InteractiveRead | None = None
    # On an inbound TAP: the id of the option tapped - one of an earlier
    # message's `interactive.options[].id`. Never shown; it is how a console
    # knows which option a reply chose. None for anything typed.
    interactive_reply_id: str | None = None


class MessageSend(BaseModel):
    """POST /tenants/me/conversations/{id}/messages — staff sends a message."""

    body: str = Field(min_length=1)
