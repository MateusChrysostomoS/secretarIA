"""ProfessionalServiceUnit - at which units a professional does a given service (TASK-083).

One row = "this professional does this service at this unit". The rule that gives the table its
meaning (spec §4/§6.1):

* NO row for (professional, service)  -> the service is done at EVERY unit the professional
  attends (today's behaviour; every existing clinic starts here, nothing to backfill);
* one or more rows                    -> the service is done ONLY at those units.

The empty set is never persisted as "none": a restricted service always has at least one row,
and dropping the last row is how a service goes back to "every unit" - which the domain layer
(services/professional_service_units.py) only allows when the caller says so explicitly.

All three keys belong to the SAME tenant; the service/API that writes the rows validates it
(`apply_marks`). Deleting the professional, the service or the unit removes the row (CASCADE).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class ProfessionalServiceUnit(Base):
    """One (professional, service, unit) mark."""

    __tablename__ = "professional_service_units"

    professional_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"), primary_key=True
    )
    # Indexed on its own: the PK leads with professional_id, and the cascade from `services`
    # (and any "who does this service here?" lookup) walks by service.
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
