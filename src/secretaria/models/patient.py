"""Patient model - a person messaging a clinic via WhatsApp."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class Patient(Base):
    """A patient, identified by their WhatsApp id (wa_id) within a tenant."""

    __tablename__ = "patients"
    __table_args__ = (UniqueConstraint("tenant_id", "wa_id", name="uq_patients_tenant_wa_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # WhatsApp id == the patient's phone number in E.164-ish digits.
    wa_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
