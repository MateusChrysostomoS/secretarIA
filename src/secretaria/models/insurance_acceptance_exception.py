"""InsuranceAcceptanceException - one convênio a clinic/doctor does NOT accept for a service at a
unit (TASK-086, SPEC E1).

Exceptions only: no row = accepted, so every existing clinic keeps today's behaviour with no
backfill. One row = "this plan is refused for this (service, unit)".

Two matrices, told apart by `professional_id` (SPEC E2):

* `professional_id IS NULL`  -> the CLINIC matrix (mode `shared`), plan = `tenant_plan_id`;
* `professional_id` set      -> that DOCTOR's matrix (mode `independent`), plan =
  `professional_plan_id`.

`unit_id IS NULL` is the "no unit" slice (a clinic/doctor with no unit links), never "all units".
Exactly one plan column is set, and it must match the scope (both CHECKs below).

All keys belong to the SAME tenant; the API that writes rows validates it (the FKs cannot).
Removing the plan, service, unit, doctor or tenant removes the row (CASCADE). Uniqueness is by
partial unique indexes, one per NULL shape of (professional_id, unit_id), because a plain UNIQUE
treats NULLs as distinct.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

# CASE/SUM keeps the CHECKs portable between SQLite (tests) and Postgres, like models/insurance.py.
_EXACTLY_ONE_PLAN = (
    "(CASE WHEN tenant_plan_id IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN professional_plan_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)
_SCOPE_MATCHES_PLAN = (
    "(professional_id IS NULL AND tenant_plan_id IS NOT NULL) "
    "OR (professional_id IS NOT NULL AND professional_plan_id IS NOT NULL)"
)


def _partial(name: str, columns: list[str], where: str) -> Index:
    return Index(
        name, *columns, unique=True, postgresql_where=text(where), sqlite_where=text(where)
    )


class InsuranceAcceptanceException(Base):
    """One refused (scope, service, unit, plan) cell."""

    __tablename__ = "insurance_acceptance_exceptions"
    __table_args__ = (
        CheckConstraint(
            _EXACTLY_ONE_PLAN, name="ck_insurance_acceptance_exceptions_exactly_one_plan"
        ),
        CheckConstraint(
            _SCOPE_MATCHES_PLAN, name="ck_insurance_acceptance_exceptions_scope_matches_plan"
        ),
        _partial(
            "uq_iae_clinic_unit",
            ["tenant_id", "service_id", "unit_id", "tenant_plan_id"],
            "professional_id IS NULL AND unit_id IS NOT NULL",
        ),
        _partial(
            "uq_iae_clinic_no_unit",
            ["tenant_id", "service_id", "tenant_plan_id"],
            "professional_id IS NULL AND unit_id IS NULL",
        ),
        _partial(
            "uq_iae_prof_unit",
            ["professional_id", "service_id", "unit_id", "professional_plan_id"],
            "professional_id IS NOT NULL AND unit_id IS NOT NULL",
        ),
        _partial(
            "uq_iae_prof_no_unit",
            ["professional_id", "service_id", "professional_plan_id"],
            "professional_id IS NOT NULL AND unit_id IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # One read per tenant (SPEC E9) walks this index.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # NULL = the clinic matrix (mode `shared`).
    professional_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"), nullable=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), index=True
    )
    # NULL = the "no unit" slice (not "every unit").
    unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"), index=True, nullable=True
    )
    tenant_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenant_insurance_plans.id", ondelete="CASCADE"), index=True, nullable=True
    )
    professional_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("professional_insurance_plans.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
