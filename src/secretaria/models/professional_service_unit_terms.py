"""ProfessionalServiceUnitTerms - price/duration exceptions per professional x service x unit.

TASK-085.

One row = "at this unit, this professional charges `price` and/or takes `duration_min` for this
service". The table only stores exceptions to the professional's base entry
(`Professional.appointment_types[]`, or the clinic's when inherited):

* NO row for (professional, service, unit) -> the base price/duration apply (today's behaviour;
  every existing clinic starts here, nothing to backfill);
* a row with only `price` (or only `duration_min`) -> the other half is inherited from the base.

At least one of the two is always set (CHECK) and a duration, when set, is 1..600 like the base.
`price` is free text (<= 40) exactly like `appointment_types[].price`. This is NOT
`professional_service_units` (there, "has a row" restricts WHERE the service is done).

`tenant_id` repeats the tenant of the three keys; the writer validates they are all the same
clinic and every read filters on it (services/service_terms.py). Deleting the professional, the
service, the unit or the tenant removes the row (CASCADE).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class ProfessionalServiceUnitTerms(Base):
    """One (professional, service, unit) price/duration exception."""

    __tablename__ = "professional_service_unit_terms"
    __table_args__ = (
        CheckConstraint(
            "price IS NOT NULL OR duration_min IS NOT NULL",
            name="ck_psut_price_or_duration",
        ),
        CheckConstraint(
            "duration_min IS NULL OR (duration_min > 0 AND duration_min <= 600)",
            name="ck_psut_duration_range",
        ),
    )

    professional_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"), primary_key=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    price: Mapped[str | None] = mapped_column(String(40), nullable=True)
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
