"""A piece of the clinic's own material submitted for context extraction (TASK-026)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

KIND_URL, KIND_TEXT, KIND_FILE = "url", "text", "file"
STATUS_PENDING, STATUS_PROCESSING, STATUS_DONE, STATUS_FAILED = (
    "pending",
    "processing",
    "done",
    "failed",
)
IN_FLIGHT = (STATUS_PENDING, STATUS_PROCESSING)


class ContextSource(Base):
    __tablename__ = "context_sources"
    __table_args__ = (Index("ix_context_sources_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(10))
    # What the manager sees in the list: the host of the URL, the file name, or "Texto colado".
    label: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(12), default=STATUS_PENDING, server_default=STATUS_PENDING
    )
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Transient inputs: cleared when the job ends, success or not (SPEC G3).
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What survives: the proposal with its quotes, for a human to review.
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Soft delete: a deleted source keeps counting against the quota (SPEC G5 is a cost cap), but
    # holds nothing and is hidden from every read.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
