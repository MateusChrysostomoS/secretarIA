"""Pix deposit (sinal) lifecycle: pix_deposits + processed_asaas_events tables,
tenant / tenant_credentials / patient additive columns.

FOUNDATION wave for the Pix deposit (sinal) feature — purely additive, no
existing table/column is touched, renamed or dropped. Adds:

  - `pix_deposits` — one row per appointment that got a deposit charge. UNIQUE
    on `appointment_id`: a reschedule updates the Appointment row IN PLACE
    (same id, new start_at/end_at — see services/calendar.py:update_event), so
    the existing deposit naturally "follows" the rescheduled appointment with
    zero migration machinery of its own; `register_reschedule`
    (services/payments/deposit_lifecycle.py) only bumps a counter against
    `tenants.pix_reschedule_limit`, it never re-points this FK. `status` is a
    dedicated native enum (`pix_deposit_status`) — a SEPARATE state machine
    from `appointment_status` (see models/pix_deposit.py's docstring): the
    appointment's own status answers "did the patient show up", this table
    answers "what happened to the money", and the two are orthogonal (e.g. a
    no-show can end up NO_SHOW_RETAINED, or leave nothing to retain at all if
    the deposit was never paid).
  - `processed_asaas_events` — idempotency ledger for the Asaas webhook,
    mirroring `processed_events` (the existing Meta webhook ledger) exactly,
    kept as a separate table since the two providers' event ids are different
    id spaces.
  - `tenants.pix_deposit_enabled` / `pix_deposit_percent` /
    `pix_refund_window_hours` / `pix_retention_policy` /
    `pix_partial_refund_percent` / `pix_reschedule_limit` — per-tenant policy
    knobs surfaced on the doctor hub (api/hub/config.py). `tenants.pix_key`
    (existing) is untouched — it belongs to the old message-only
    services/payments/pix.py stub / pix_whatsapp addon, unused by this new
    lifecycle.
  - `tenant_credentials.asaas_api_key_encrypted` /
    `asaas_webhook_token_encrypted` — Fernet ciphertext, mirroring the
    existing `waba_token_encrypted` column (tenant-secrets-encryption skill).
  - `patients.asaas_customer_id` — the clinic's-own-Asaas-account customer id
    for this patient (NOT a secret — a foreign-system foreign key, reused
    across deposits so we don't create a duplicate Asaas customer per
    booking).

All new columns are nullable or carry a server_default; existing rows need no
backfill.

Revision ID: b06ff85998bf
Revises: e51cd84e1959
Create Date: 2026-07-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b06ff85998bf"
down_revision: str | None = "e51cd84e1959"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False is essential: the type is created explicitly in upgrade()
# with checkfirst=True (idempotent) — see c4d8e2f1a5b6's appointment_status
# for the same pattern (op.create_table would otherwise ALSO auto-emit
# `CREATE TYPE pix_deposit_status` with no checkfirst and collide).
pix_deposit_status = postgresql.ENUM(
    "aguardando_sinal",
    "confirmado_pago",
    "cancelado_reembolsado",
    "cancelado_retido",
    "no_show_retido",
    "expirado",
    name="pix_deposit_status",
    create_type=False,
)


def upgrade() -> None:
    pix_deposit_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "pix_deposits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("appointment_id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=True),
        sa.Column("asaas_payment_id", sa.String(length=64), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("percent_applied", sa.Integer(), nullable=False),
        sa.Column("pix_copy_paste", sa.Text(), nullable=True),
        sa.Column(
            "status",
            pix_deposit_status,
            server_default="aguardando_sinal",
            nullable=False,
        ),
        sa.Column("reschedule_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("refunded_amount_cents", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_pix_deposits_tenant_id"), "pix_deposits", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_pix_deposits_appointment_id"), "pix_deposits", ["appointment_id"], unique=True
    )
    op.create_index(
        op.f("ix_pix_deposits_asaas_payment_id"),
        "pix_deposits",
        ["asaas_payment_id"],
        unique=True,
    )

    op.create_table(
        "processed_asaas_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_processed_asaas_events_event_id"),
        "processed_asaas_events",
        ["event_id"],
        unique=True,
    )

    op.add_column(
        "tenants",
        sa.Column(
            "pix_deposit_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "tenants",
        sa.Column("pix_deposit_percent", sa.Integer(), server_default="30", nullable=False),
    )
    op.add_column(
        "tenants",
        sa.Column("pix_refund_window_hours", sa.Integer(), server_default="24", nullable=False),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "pix_retention_policy", sa.String(length=16), server_default="total", nullable=False
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "pix_partial_refund_percent", sa.Integer(), server_default="50", nullable=False
        ),
    )
    op.add_column(
        "tenants",
        sa.Column("pix_reschedule_limit", sa.Integer(), server_default="2", nullable=False),
    )

    op.add_column(
        "tenant_credentials",
        sa.Column("asaas_api_key_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "tenant_credentials",
        sa.Column("asaas_webhook_token_encrypted", sa.Text(), nullable=True),
    )

    op.add_column(
        "patients",
        sa.Column("asaas_customer_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("patients", "asaas_customer_id")

    op.drop_column("tenant_credentials", "asaas_webhook_token_encrypted")
    op.drop_column("tenant_credentials", "asaas_api_key_encrypted")

    op.drop_column("tenants", "pix_reschedule_limit")
    op.drop_column("tenants", "pix_partial_refund_percent")
    op.drop_column("tenants", "pix_retention_policy")
    op.drop_column("tenants", "pix_refund_window_hours")
    op.drop_column("tenants", "pix_deposit_percent")
    op.drop_column("tenants", "pix_deposit_enabled")

    op.drop_index(
        op.f("ix_processed_asaas_events_event_id"), table_name="processed_asaas_events"
    )
    op.drop_table("processed_asaas_events")

    op.drop_index(op.f("ix_pix_deposits_asaas_payment_id"), table_name="pix_deposits")
    op.drop_index(op.f("ix_pix_deposits_appointment_id"), table_name="pix_deposits")
    op.drop_index(op.f("ix_pix_deposits_tenant_id"), table_name="pix_deposits")
    op.drop_table("pix_deposits")
    pix_deposit_status.drop(op.get_bind(), checkfirst=True)
