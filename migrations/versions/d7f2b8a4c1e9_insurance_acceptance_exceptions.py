"""insurance_acceptance_exceptions: which convênios are NOT accepted per service and unit.

TASK-086 (matrix of convênios by professional x service x unit). Exceptions only: no row = the
convênio is accepted, so every existing clinic keeps today's behaviour with no backfill.

    CREATE TABLE insurance_acceptance_exceptions (
        id                   UUID PRIMARY KEY,
        tenant_id            UUID NOT NULL FK -> tenants.id                       ON DELETE CASCADE,
        professional_id      UUID NULL     FK -> professionals.id                 ON DELETE CASCADE,
        service_id           UUID NOT NULL FK -> services.id                      ON DELETE CASCADE,
        unit_id              UUID NULL     FK -> units.id                         ON DELETE CASCADE,
        tenant_plan_id       UUID NULL     FK -> tenant_insurance_plans.id        ON DELETE CASCADE,
        professional_plan_id UUID NULL     FK -> professional_insurance_plans.id  ON DELETE CASCADE,
        created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK exactly one of (tenant_plan_id, professional_plan_id),
        CHECK scope matches plan (clinic matrix <-> tenant_plan_id, doctor <-> professional_plan_id)
    )
    + four partial UNIQUE indexes (one per NULL shape of professional_id / unit_id) and one plain
      index per FK column.

ADDITIVE: a new table nothing reads until the new code ships, so the database can move first.

Parallel head: TASK-085 also branches from a4e7c2f9d6b1; the Integrator merges the two heads with a
bridge migration (no schema change) when both land.

Rollback: `alembic downgrade` drops the table; the per-service/unit exceptions are lost (every
convênio goes back to "accepted everywhere"). Nothing else references it. The conversion of the
`clinic_with_exceptions` mode (data migration) is NOT here: it is TASK-086 T5.

Revision ID: d7f2b8a4c1e9
Revises: a4e7c2f9d6b1
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7f2b8a4c1e9"
down_revision: str | None = "a4e7c2f9d6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "insurance_acceptance_exceptions"
_UNIQUES = (
    (
        "uq_iae_clinic_unit",
        ["tenant_id", "service_id", "unit_id", "tenant_plan_id"],
        "professional_id IS NULL AND unit_id IS NOT NULL",
    ),
    (
        "uq_iae_clinic_no_unit",
        ["tenant_id", "service_id", "tenant_plan_id"],
        "professional_id IS NULL AND unit_id IS NULL",
    ),
    (
        "uq_iae_prof_unit",
        ["professional_id", "service_id", "unit_id", "professional_plan_id"],
        "professional_id IS NOT NULL AND unit_id IS NOT NULL",
    ),
    (
        "uq_iae_prof_no_unit",
        ["professional_id", "service_id", "professional_plan_id"],
        "professional_id IS NOT NULL AND unit_id IS NULL",
    ),
)
_FK_INDEXES = ("tenant_id", "service_id", "unit_id", "tenant_plan_id", "professional_plan_id")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=True),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=True),
        sa.Column("tenant_plan_id", sa.Uuid(), nullable=True),
        sa.Column("professional_plan_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(CASE WHEN tenant_plan_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN professional_plan_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_insurance_acceptance_exceptions_exactly_one_plan",
        ),
        sa.CheckConstraint(
            "(professional_id IS NULL AND tenant_plan_id IS NOT NULL) "
            "OR (professional_id IS NOT NULL AND professional_plan_id IS NOT NULL)",
            name="ck_insurance_acceptance_exceptions_scope_matches_plan",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["unit_id"], ["units.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_plan_id"], ["tenant_insurance_plans.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["professional_plan_id"], ["professional_insurance_plans.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in _FK_INDEXES:
        op.create_index(f"ix_{_TABLE}_{column}", _TABLE, [column])
    for name, columns, where in _UNIQUES:
        op.create_index(
            name,
            _TABLE,
            columns,
            unique=True,
            postgresql_where=sa.text(where),
            sqlite_where=sa.text(where),
        )


def downgrade() -> None:
    # Dropping the table drops its indexes; the exceptions are lost (documented above).
    op.drop_table(_TABLE)
