"""Message model - a single message inside a conversation."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class MessageDirection(enum.StrEnum):
    """Relative to the clinic's WhatsApp number."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageSender(enum.StrEnum):
    """Who authored the message."""

    PATIENT = "patient"
    BOT = "bot"
    HUMAN = "human"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class Message(Base):
    """One WhatsApp message, inbound or outbound."""

    __tablename__ = "messages"
    __table_args__ = (
        # Serves the persisted daily byte quota (api/internal.py::
        # _enforce_attachment_quota): only rows WITH a file enter it, so it stays tiny.
        # Migration 5e1f9a3c7d20.
        Index(
            "ix_messages_attachment_created_at",
            "created_at",
            postgresql_where=text("attachment IS NOT NULL"),
            sqlite_where=text("attachment IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(
        SAEnum(
            MessageDirection,
            native_enum=False,
            length=16,
            values_callable=_enum_values,
        )
    )
    sender: Mapped[MessageSender] = mapped_column(
        SAEnum(
            MessageSender,
            native_enum=False,
            length=16,
            values_callable=_enum_values,
        )
    )
    # Meta message id (wamid.*). Nullable - not every event carries one.
    wam_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The raw id of the interactive control an INBOUND message tapped
    # (`interactive.button_reply.id` / `list_reply.id`, e.g. "prof|<uuid>") -
    # the machine half of a tap, NULL for anything typed. `body` keeps only the
    # human half, the title the patient saw, because `body` is what the staff
    # console and the patient portal render. Readers that need the payload back
    # (the flow router, the agent's history) recompose it with
    # schemas/webhook.py::inbound_routing_text; nothing displays this column.
    interactive_reply_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What an interactive OUTBOUND message offered - the reply buttons or the
    # list rows exactly as the channel drew them:
    #   {"kind": "buttons" | "list", "body", "options": [{"id", "title",
    #    "description"}], "button_label", "section_title"}
    # built by services/whatsapp.py::interactive_buttons_record /
    # interactive_list_record from the same arguments as the Graph API payload.
    # `body` above stays the flattened text the agent's history reads (a list
    # gains "(opções: A, B)"), untouched; this is the half a HUMAN screen needs -
    # the staff console draws real controls from it, and links a later tap back
    # to it through `interactive_reply_id`. NULL for text, for inbound rows, for
    # rows written before the column, and on Brain-Message, whose patient
    # receives the options as plain text (services/channel_sender.py).
    # Plain JSON like every JSON column here: the test suite runs on SQLite and
    # nothing queries inside the blob.
    interactive: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The ONE file a Brain-Message message carries (patient -> clinic or clinic ->
    # patient), or NULL: {"r2_object_key", "content_type", "size_bytes", "filename"}
    # (core/attachments.py::StoredAttachment). The bytes live in secretarIA's own R2
    # bucket (services/media_storage.py); `r2_object_key` never leaves this service -
    # every projection drops it and both media routes stream the bytes themselves.
    # `body` is never empty on such a row: the caption, or "[anexo: <filename>]"
    # (core/attachments.py::attachment_body). WhatsApp media is not stored here.
    # `none_as_null=True`, unlike `interactive` above: a message without a file must be
    # SQL NULL, never JSON 'null', because the quota index and sum select on
    # `attachment IS NOT NULL`.
    attachment: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
