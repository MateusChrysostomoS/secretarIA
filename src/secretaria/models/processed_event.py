"""ProcessedEvent model - idempotency ledger for incoming webhook events."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class ProcessedEvent(Base):
    """One row per Meta event id already handled.

    The unique constraint on `event_id` is the backstop that makes webhook
    processing idempotent even under concurrent delivery / Meta retries.
    """

    __tablename__ = "processed_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(128), unique=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
