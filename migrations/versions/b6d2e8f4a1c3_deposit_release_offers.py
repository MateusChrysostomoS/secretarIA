"""deposit release offers and their scheduled sends

TASK-052 (docs/superpowers/specs/2026-10-11-liberar-horario-com-sinal-pago-design.md). Purely
ADDITIVE (skill frozen-contract-migration): two new tables; no existing table is touched. Code
that predates this revision never reads them.

DEPLOY ORDER: this migration -> secretarIA API -> secretarIA worker RIGHT AFTER -> Brain-Message-
Frontend (copy of the release card) last.

Revision ID: b6d2e8f4a1c3
Revises: e2b7c4a9d1f5
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d2e8f4a1c3"
down_revision: str | None = "e2b7c4a9d1f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "deposit_release_offers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "deposit_id",
            sa.Uuid(),
            sa.ForeignKey("pix_deposits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "released_appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "patient_id",
            sa.Uuid(),
            sa.ForeignKey("patients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("deposit_paid", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("decision", sa.String(24), nullable=False, server_default="none"),
        sa.Column("refund_status", sa.String(16), nullable=False, server_default="not_due"),
        sa.Column("refund_source", sa.String(16), nullable=True),
        _ts("released_at", nullable=False),
        _ts("slot_start_at"),
        _ts("decide_by_at", nullable=False),
        _ts("expires_at", nullable=False),
        sa.Column("paid_notice_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("first_contact_at"),
        _ts("refund_due_at"),
        _ts("refund_marked_at"),
        _ts("patient_answered_at"),
        _ts("last_reminder_at"),
        _ts("after_start_card_at"),
        _ts("keep_requested_at"),
        _ts("keep_deadline_at"),
        sa.Column("keep_outcome", sa.String(16), nullable=True),
        _ts("keep_resolved_at"),
        sa.Column("keep_resolved_by", sa.String(120), nullable=True),
        sa.Column(
            "new_appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("closed_at"),
        sa.Column("carry_check", sa.String(16), nullable=True),
        _ts("carry_checked_at"),
        _ts("deposit_reused_at"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "released_appointment_id", name="uq_deposit_release_offers_released_appointment_id"
        ),
    )
    op.create_index("ix_deposit_release_offers_tenant_id", "deposit_release_offers", ["tenant_id"])
    op.create_index(
        "ix_deposit_release_offers_deposit_id", "deposit_release_offers", ["deposit_id"]
    )
    op.create_index(
        "ix_deposit_release_offers_conversation_id", "deposit_release_offers", ["conversation_id"]
    )
    op.create_index(
        "ix_deposit_release_offers_decision_decide_by",
        "deposit_release_offers",
        ["decision", "decide_by_at"],
    )
    op.create_index(
        "ix_deposit_release_offers_decision_expires",
        "deposit_release_offers",
        ["decision", "expires_at"],
    )
    op.create_index(
        "ix_deposit_release_offers_decision_keep_deadline",
        "deposit_release_offers",
        ["decision", "keep_deadline_at"],
    )
    op.create_table(
        "deposit_release_emails",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "offer_id",
            sa.Uuid(),
            sa.ForeignKey("deposit_release_offers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        _ts("due_at", nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("claimed_at"),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "offer_id", "kind", "seq", name="uq_deposit_release_emails_offer_kind_seq"
        ),
    )
    op.create_index("ix_deposit_release_emails_tenant_id", "deposit_release_emails", ["tenant_id"])
    op.create_index(
        "ix_deposit_release_emails_due", "deposit_release_emails", ["claimed_at", "due_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_deposit_release_emails_due", table_name="deposit_release_emails")
    op.drop_index("ix_deposit_release_emails_tenant_id", table_name="deposit_release_emails")
    op.drop_table("deposit_release_emails")
    for name in (
        "ix_deposit_release_offers_decision_keep_deadline",
        "ix_deposit_release_offers_decision_expires",
        "ix_deposit_release_offers_decision_decide_by",
        "ix_deposit_release_offers_conversation_id",
        "ix_deposit_release_offers_deposit_id",
        "ix_deposit_release_offers_tenant_id",
    ):
        op.drop_index(name, table_name="deposit_release_offers")
    op.drop_table("deposit_release_offers")
