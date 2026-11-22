"""Unit model - one physical location a tenant operates from (core since TASK-090, not an add-on).

The `multi_unit` plugin's tools switch on when the clinic has an active unit
(see plugins/multi_unit.py).
Units do not carry their own Calendar credentials - they only record where
the patient should show up; the booking itself still uses the tenant's (or,
when multi_professional is also active, the professional's) calendar.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class Unit(Base):
    """A physical location (address) a tenant operates from."""

    __tablename__ = "units"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    # Derived text line of the parts below (services/unit_address.format_unit_address); legacy
    # units may have only this text, with every part NULL.
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cep: Mapped[str | None] = mapped_column(String(8), nullable=True)  # 8 digits, no mask
    street: Mapped[str | None] = mapped_column(String(255), nullable=True)
    number: Mapped[str | None] = mapped_column(String(30), nullable=True)
    complement: Mapped[str | None] = mapped_column(String(255), nullable=True)
    neighborhood: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)  # UF
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
