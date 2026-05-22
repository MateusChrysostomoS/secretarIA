"""Conversation model - one ongoing thread between a patient and a clinic."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class HandoverState(enum.StrEnum):
    """Who currently owns the conversation.

    BOT_ACTIVE   - the AI may answer automatically.
    HUMAN_ACTIVE - a human secretary is handling it; the bot stays silent.
    """

    BOT_ACTIVE = "BOT_ACTIVE"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"


def _handover_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class Conversation(Base):
    """A patient <-> clinic conversation and its handover state."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "patient_id", name="uq_conversations_tenant_patient"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), index=True
    )
    handover_state: Mapped[HandoverState] = mapped_column(
        SAEnum(
            HandoverState,
            native_enum=False,
            length=32,
            values_callable=_handover_values,
        ),
        default=HandoverState.BOT_ACTIVE,
    )
    last_human_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_bot_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
