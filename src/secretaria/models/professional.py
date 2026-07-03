"""Professional model - one bookable staff member within a tenant (multi_professional addon).

Entitlement-gated by the `multi_professional` plugin (see
plugins/multi_professional.py). A professional optionally carries its own
Google Calendar id; when unset, booking tools fall back to the tenant's own
`google_calendar_id` (services/calendar.py:CalendarService.from_tenant_config).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class Professional(Base):
    """A bookable professional (doctor, therapist, ...) belonging to a tenant."""

    __tablename__ = "professionals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    # None -> falls back to the tenant's own calendar (google_calendar_id).
    google_calendar_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
