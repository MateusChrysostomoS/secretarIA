"""Request/response schemas for the doctor-hub conversations endpoints."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from secretaria.core.attachments import stored_attachment_or_none
from secretaria.core.logging import get_logger
from secretaria.models.conversation import HandoverState
from secretaria.models.message import MessageStatus

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


class AttachmentRead(BaseModel):
    """What a message's file IS - never where it is stored (`Message.attachment`).

    The same three fields on both wires: the staff hub here, and the Brain-Message
    listing that brain-api rewrites for the patient (schemas/internal.py).
    """

    content_type: str
    size_bytes: int
    filename: str


def attachment_read_or_none(blob: object, *, message_id: object) -> AttachmentRead | None:
    """A row's `Message.attachment` without its storage key, or None (tolerant, like
    `interactive_read_or_none`: a bad blob costs that message its file, not the thread)."""
    record = stored_attachment_or_none(blob, message_id=message_id)
    if record is None:
        return None
    return AttachmentRead(
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        filename=record.filename,
    )


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
    # The file this message carries (Brain-Message only), WITHOUT where it is stored;
    # the bytes come from GET .../conversations/{id}/messages/{message_id}/media.
    attachment: AttachmentRead | None = None
    # Delivery state (docs/CHECKPOINT_brain_message_status_entrega.md). `status` is
    # DERIVED from the timestamps (models/message.py::status_of): "falhou" > "lido" >
    # "entregue" > "enviado". On WhatsApp only Meta's receipts move it; on Brain-Message
    # a row is "entregue" from birth and "lido" once the other side marks it read.
    # "enviando" never comes from here - it is the client's state before the POST
    # returns. All additive with defaults (`frozen-contract-migration`).
    status: MessageStatus = "enviado"
    delivered_at: datetime | None = None
    read_at: datetime | None = None
    # "<Meta error code>: <title>" when `status == "falhou"`; never personal data.
    failure_reason: str | None = None
    # Bumped by every write to the row, status included.
    updated_at: datetime | None = None


class MessagesReadMark(BaseModel):
    """"I have seen this conversation up to here" - exactly ONE of the two cursors.

    `up_to_message_id`: the last message the reader saw (resolved to its `created_at`
    inside the same conversation). `up_to`: an instant, which must carry its offset -
    a naive time is refused, never guessed at (`naive-timestamp-serialization`).
    Everything the OTHER side wrote up to that point becomes read.
    """

    model_config = ConfigDict(extra="forbid")

    up_to_message_id: UUID | None = None
    up_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def _exactly_one_cursor(self):
        if (self.up_to_message_id is None) == (self.up_to is None):
            raise ValueError("send exactly one of up_to_message_id, up_to")
        return self


class MessagesReadResult(BaseModel):
    """What a read mark did. `applied` is false when read marks do not apply to this
    conversation at all - a WhatsApp patient (only Meta reports those reads), or no such
    Brain-Message conversation - and then nothing was changed. `marked` counts rows that
    moved to read now; rows already read are not counted again."""

    marked: int
    applied: bool


class MessageSend(BaseModel):
    """POST /tenants/me/conversations/{id}/messages — staff sends a message."""

    body: str = Field(min_length=1)


class MessageSendForm(BaseModel):
    """The text fields of a staff send that carries a FILE (multipart/form-data).

    The file itself is the part `file`; `body` becomes an optional caption. Strict
    (`extra="forbid"`): a field this service does not know is refused, not ignored.
    """

    model_config = ConfigDict(extra="forbid")

    body: str | None = Field(default=None, min_length=1)
