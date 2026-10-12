"""Durable references-only intent for one committed patient edit."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class ProfessionalEditNotice(Base):
    __tablename__ = "professional_edit_notices"
    __table_args__ = (
        UniqueConstraint("appointment_id", "revision", name="uq_profedit_appointment_revision"),
        Index("ix_profedit_pending_due", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"),
    )
    revision: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(64))
    changed_fields: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    dispatch_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
