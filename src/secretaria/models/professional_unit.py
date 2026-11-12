"""ProfessionalUnit - which of the tenant's units a professional attends (TASK-083).

`Unit` is per tenant (models/unit.py) and a professional is per tenant, so the link is
tenant-internal: a professional is only ever linked to units of their own tenant (enforced by the
service/API that writes it, services/professional_units.py).

`business_hours` has the format of `professionals.business_hours` and the same NULL rule:
NULL = inherit the professional's own hours; a value (even `{}`) = this unit's own schedule.
A professional with NO active link is an agenda without a unit, exactly as today; with one or
more active links the unit's availability is that link's hours. The BUSY time is never per unit:
the person is in one place at a time, so the whole agenda is busy (services/agenda.py).

Deactivating a `Unit` keeps these rows; the unit simply stops being offered.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, func, text
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class ProfessionalUnit(Base):
    """One professional <-> unit link, with an optional own schedule."""

    __tablename__ = "professional_units"

    professional_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"), primary_key=True
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"), primary_key=True
    )
    # Same shape as professionals.business_hours; NULL = inherit the professional's own.
    business_hours: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
