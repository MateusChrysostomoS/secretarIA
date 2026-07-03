"""Unit model - one physical location a tenant operates from (multi_unit addon).

Entitlement-gated by the `multi_unit` plugin (see plugins/multi_unit.py).
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
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
