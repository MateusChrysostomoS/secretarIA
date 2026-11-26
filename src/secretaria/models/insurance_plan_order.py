"""InsurancePlanOrder - the clinic's ONE order of convênios in the per-doctor mode (TASK-090).

In `independent` mode there is no clinic-level plan row (`tenant_insurance_plans.sort_order` only
orders the `shared`/`clinic_with_exceptions` list), so the order the patient sees and the matrix
columns follow lives here, one row per placed plan:

* `plan_key = "catalog:<insurance_catalog.id>"` - a catalog plan, the SAME position for every doctor
  who picked it (two doctors taking "Unimed" share one place in the list);
* `plan_key = "custom:<professional_insurance_plans.id>"` - one doctor's own "Outro", which is never
  deduplicated with another doctor's, so it is keyed by its row.

A plan with no row was never placed: it follows the placed ones alphabetically. Rows of plans that
no longer exist are harmless (only gaps in `position`); a tenant's removal removes its rows.
Written only by `services/insurance_plan_order.py::save_subset_order`.
"""

import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class InsurancePlanOrder(Base):
    """One placed plan of a clinic's per-doctor-mode convênio order."""

    __tablename__ = "insurance_plan_order"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    plan_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
