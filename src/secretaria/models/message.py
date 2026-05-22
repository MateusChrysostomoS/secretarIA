"""Message model - a single message inside a conversation."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, Text, func
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
