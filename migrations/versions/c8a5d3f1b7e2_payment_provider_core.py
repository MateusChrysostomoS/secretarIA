"""payment provider core: accounts, webhook ledger, customers, attention items

TASK-016 (F1 of TASK F, docs/CHECKPOINT_pix_provider_core.md). Purely ADDITIVE, widen-before-
narrow (skill frozen-contract-migration): every new column is nullable or carries a server
default equal to today's behaviour, so code that predates this revision - the API or the
worker, whichever is deployed first - keeps working untouched. Nothing is dropped.

What this adds, and why (rationale: models/clinic_payment_account.py, payment_webhook_event.py,
patient_payment_customer.py, payment_attention_item.py, pix_deposit.py):

- `clinic_payment_accounts` - one row per (tenant, provider); replaces the two Asaas columns of
  `tenant_credentials` (which STAY: dual-read / dual-write until a later "narrow" revision).
  Data migration: every tenant that has an Asaas key gets a `connected` row whose ciphertext is
  COPIED (never re-encrypted) and a fresh opaque `webhook_slug`. `last_verified_at` stays NULL:
  such a row was connected by an operator under the old regime, so it is usable for the
  `after` timing but is NOT "ready for `before`" until the new connection flow verifies it.
- `payment_webhook_events` - persist-first ledger, UNIQUE(tenant_id, provider, event_id).
- `patient_payment_customers` - provider customer ids per patient (backfilled from
  `patients.asaas_customer_id`, which stays).
- `payment_attention_items` - what needs the clinic's decision (no automatic refund, D15).
- `pix_deposits` - `appointment_id` becomes NULLABLE (the charge exists before the appointment
  in `before` timing) and gains provider / provider_charge_id / external_reference /
  expires_at / fulfillment_status / confirming_event_id / paid_amount_cents / booking_snapshot /
  hold_id / calendar_event_id / deposit_timing. The native PG enum `pix_deposit_status` is NOT
  touched. Backfill: `provider_charge_id = asaas_payment_id`, `external_reference` = the
  appointment id (that is what the pre-core code sent as `externalReference`).
- `tenants` - payment_provider ('asaas'), deposit_timing ('after'), deposit_hold_ttl_minutes (30).
- `booking_holds` - phase ('code') and charge_id (NULL).

DEPLOY ORDER (both services map `Tenant`, `PixDeposit`, `BookingHold`): this migration ->
API + worker BOTH on the new code (deploy_parity=match) -> nothing else for F1. Downgrade
refuses to run while any `pix_deposits.appointment_id` is NULL (it would lose money rows).

Revision ID: c8a5d3f1b7e2
Revises: d4f8a2c6e913
Create Date: 2026-09-29
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "c8a5d3f1b7e2"
down_revision: str | None = "d4f8a2c6e913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHUNK = 500


def _backfill(bind) -> None:
    """Copy the Asaas data of the old columns into the new tables. Idempotent.

    Uses typed lightweight tables (not raw text) so `Uuid` binds correctly on Postgres and on
    the SQLite the unit test runs against.
    """
    creds = sa.table(
        "tenant_credentials",
        sa.column("tenant_id", sa.Uuid()),
        sa.column("asaas_api_key_encrypted", sa.Text()),
        sa.column("asaas_webhook_token_encrypted", sa.Text()),
    )
    accounts = sa.table(
        "clinic_payment_accounts",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("status", sa.String()),
        sa.column("webhook_slug", sa.String()),
        sa.column("api_key_encrypted", sa.Text()),
        sa.column("webhook_token_encrypted", sa.Text()),
    )
    have_account = {
        row[0]
        for row in bind.execute(
            sa.select(accounts.c.tenant_id).where(accounts.c.provider == "asaas")
        )
    }
    legacy = bind.execute(
        sa.select(
            creds.c.tenant_id,
            creds.c.asaas_api_key_encrypted,
            creds.c.asaas_webhook_token_encrypted,
        ).where(creds.c.asaas_api_key_encrypted.is_not(None))
    ).all()
    new_accounts = [
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "provider": "asaas",
            "status": "connected",
            "webhook_slug": str(uuid.uuid4()),
            "api_key_encrypted": api_key,
            "webhook_token_encrypted": webhook_token,
        }
        for tenant_id, api_key, webhook_token in legacy
        if tenant_id not in have_account
    ]
    if new_accounts:
        bind.execute(sa.insert(accounts), new_accounts)

    patients = sa.table(
        "patients",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("asaas_customer_id", sa.String()),
    )
    customers = sa.table(
        "patient_payment_customers",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("patient_id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("provider_customer_id", sa.String()),
    )
    have_customer = {
        row[0]
        for row in bind.execute(
            sa.select(customers.c.patient_id).where(customers.c.provider == "asaas")
        )
    }
    with_customer = bind.execute(
        sa.select(patients.c.id, patients.c.tenant_id, patients.c.asaas_customer_id).where(
            patients.c.asaas_customer_id.is_not(None)
        )
    ).all()
    new_customers = [
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "patient_id": patient_id,
            "provider": "asaas",
            "provider_customer_id": customer_id,
        }
        for patient_id, tenant_id, customer_id in with_customer
        if patient_id not in have_customer
    ]
    for start in range(0, len(new_customers), _CHUNK):
        bind.execute(sa.insert(customers), new_customers[start : start + _CHUNK])

    deposits = sa.table(
        "pix_deposits",
        sa.column("id", sa.Uuid()),
        sa.column("appointment_id", sa.Uuid()),
        sa.column("asaas_payment_id", sa.String()),
        sa.column("provider_charge_id", sa.String()),
        sa.column("external_reference", sa.String()),
    )
    todo = bind.execute(
        sa.select(deposits.c.id, deposits.c.appointment_id, deposits.c.asaas_payment_id).where(
            deposits.c.asaas_payment_id.is_not(None), deposits.c.provider_charge_id.is_(None)
        )
    ).all()
    updates = [
        {
            "b_id": deposit_id,
            "b_charge": payment_id,
            "b_ref": str(uuid.UUID(str(appointment_id))) if appointment_id else None,
        }
        for deposit_id, appointment_id, payment_id in todo
    ]
    stmt = (
        sa.update(deposits)
        .where(deposits.c.id == sa.bindparam("b_id"))
        .values(
            provider_charge_id=sa.bindparam("b_charge"),
            external_reference=sa.bindparam("b_ref"),
        )
    )
    for start in range(0, len(updates), _CHUNK):
        bind.execute(stmt, updates[start : start + _CHUNK])


def upgrade() -> None:
    # --- tenants / booking_holds: inert columns the F2 gate will read ---------------------
    op.add_column(
        "tenants",
        sa.Column("payment_provider", sa.String(length=16), nullable=False, server_default="asaas"),
    )
    op.add_column(
        "tenants",
        sa.Column("deposit_timing", sa.String(length=16), nullable=False, server_default="after"),
    )
    op.add_column(
        "tenants",
        sa.Column("deposit_hold_ttl_minutes", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column(
        "booking_holds",
        sa.Column("phase", sa.String(length=16), nullable=False, server_default="code"),
    )
    op.add_column("booking_holds", sa.Column("charge_id", sa.Uuid(), nullable=True))

    # --- pix_deposits: widen -------------------------------------------------------------
    op.alter_column("pix_deposits", "appointment_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column(
        "pix_deposits",
        sa.Column("provider", sa.String(length=16), nullable=False, server_default="asaas"),
    )
    op.add_column("pix_deposits", sa.Column("provider_charge_id", sa.String(length=96)))
    op.add_column("pix_deposits", sa.Column("external_reference", sa.String(length=64)))
    op.add_column("pix_deposits", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column("pix_deposits", sa.Column("last_payment_check_at", sa.DateTime(timezone=True)))
    op.add_column("pix_deposits", sa.Column("paid_notice_sent_at", sa.DateTime(timezone=True)))
    op.add_column(
        "pix_deposits",
        sa.Column(
            "fulfillment_status", sa.String(length=24), nullable=False, server_default="none"
        ),
    )
    op.add_column("pix_deposits", sa.Column("confirming_event_id", sa.String(length=160)))
    op.add_column("pix_deposits", sa.Column("paid_amount_cents", sa.Integer()))
    op.add_column("pix_deposits", sa.Column("booking_snapshot", sa.JSON()))
    op.add_column("pix_deposits", sa.Column("hold_id", sa.Uuid()))
    op.add_column("pix_deposits", sa.Column("calendar_event_id", sa.String(length=255)))
    op.add_column(
        "pix_deposits",
        sa.Column("deposit_timing", sa.String(length=16), nullable=False, server_default="after"),
    )

    # --- new tables ---------------------------------------------------------------------
    op.create_table(
        "clinic_payment_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("webhook_slug", sa.String(length=64), nullable=False),
        sa.Column("external_account_id", sa.String(length=96)),
        sa.Column("external_webhook_id", sa.String(length=96)),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("excess_groups", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("incomplete_reason", sa.String(length=96)),
        sa.Column("probe_inconclusive_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("connected_at", sa.DateTime(timezone=True)),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_probe_at", sa.DateTime(timezone=True)),
        sa.Column("api_key_encrypted", sa.Text()),
        sa.Column("webhook_token_encrypted", sa.Text()),
        sa.Column("access_token_encrypted", sa.Text()),
        sa.Column("refresh_token_encrypted", sa.Text()),
        sa.Column("webhook_secret_encrypted", sa.Text()),
        sa.Column("oauth_pkce_verifier_encrypted", sa.Text()),
        sa.Column("oauth_access_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("webhook_slug"),
        sa.UniqueConstraint(
            "tenant_id", "provider", name="uq_clinic_payment_accounts_tenant_provider"
        ),
    )
    op.create_index(
        "ix_clinic_payment_accounts_tenant_id", "clinic_payment_accounts", ["tenant_id"]
    )

    op.create_table(
        "payment_webhook_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("provider_charge_id", sa.String(length=96)),
        sa.Column("event_type", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="received"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("payload", sa.JSON()),
        sa.UniqueConstraint(
            "tenant_id", "provider", "event_id", name="uq_payment_webhook_events_event"
        ),
    )
    op.create_index("ix_payment_webhook_events_tenant_id", "payment_webhook_events", ["tenant_id"])
    op.create_index(
        "ix_payment_webhook_events_provider_charge_id",
        "payment_webhook_events",
        ["provider_charge_id"],
    )
    op.create_index(
        "ix_payment_webhook_events_status_received",
        "payment_webhook_events",
        ["status", "received_at"],
    )

    op.create_table(
        "patient_payment_customers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "patient_id",
            sa.Uuid(),
            sa.ForeignKey("patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("provider_customer_id", sa.String(length=96), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id", "patient_id", "provider", name="uq_patient_payment_customers_key"
        ),
    )
    op.create_index(
        "ix_patient_payment_customers_tenant_id", "patient_payment_customers", ["tenant_id"]
    )
    op.create_index(
        "ix_patient_payment_customers_patient_id", "patient_payment_customers", ["patient_id"]
    )

    op.create_table(
        "payment_attention_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "deposit_id", sa.Uuid(), sa.ForeignKey("pix_deposits.id", ondelete="SET NULL")
        ),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("provider_charge_id", sa.String(length=96)),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("patients.id", ondelete="SET NULL")),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("amount_cents", sa.Integer()),
        sa.Column("paid_amount_cents", sa.Integer()),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("slot_start", sa.DateTime(timezone=True)),
        sa.Column("slot_end", sa.DateTime(timezone=True)),
        sa.Column("service_label", sa.String(length=120)),
        sa.Column("suggested_refund_cents", sa.Integer()),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("provider_refund_seen_at", sa.DateTime(timezone=True)),
        sa.Column("dedupe_key", sa.String(length=220), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_action", sa.String(length=32)),
        sa.Column("resolution_note", sa.String(length=500)),
        sa.Column("resolved_by_name", sa.String(length=80)),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_payment_attention_items_dedupe"),
    )
    op.create_index(
        "ix_payment_attention_items_tenant_id", "payment_attention_items", ["tenant_id"]
    )
    op.create_index(
        "ix_payment_attention_items_deposit_id", "payment_attention_items", ["deposit_id"]
    )
    op.create_index(
        "ix_payment_attention_items_tenant_status",
        "payment_attention_items",
        ["tenant_id", "status", "detected_at"],
    )

    # --- data: backfill BEFORE the new unique constraint, so a bad row fails loudly first ----
    if not context.is_offline_mode():
        _backfill(op.get_bind())

    op.create_unique_constraint(
        "uq_pix_deposits_provider_charge", "pix_deposits", ["provider", "provider_charge_id"]
    )
    op.create_index(
        "ix_pix_deposits_tenant_status_expires",
        "pix_deposits",
        ["tenant_id", "status", "expires_at"],
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        orphans = op.get_bind().execute(
            sa.text("SELECT count(*) FROM pix_deposits WHERE appointment_id IS NULL")
        ).scalar()
        if orphans:
            raise RuntimeError(
                "cannot downgrade: pix_deposits rows without an appointment exist "
                "(charges created before their appointment); resolve them first"
            )

    op.drop_index("ix_pix_deposits_tenant_status_expires", table_name="pix_deposits")
    op.drop_constraint("uq_pix_deposits_provider_charge", "pix_deposits", type_="unique")

    op.drop_table("payment_attention_items")
    op.drop_table("patient_payment_customers")
    op.drop_table("payment_webhook_events")
    op.drop_table("clinic_payment_accounts")

    for column in (
        "deposit_timing",
        "calendar_event_id",
        "hold_id",
        "booking_snapshot",
        "paid_amount_cents",
        "confirming_event_id",
        "fulfillment_status",
        "expires_at",
        "last_payment_check_at",
        "paid_notice_sent_at",
        "external_reference",
        "provider_charge_id",
        "provider",
    ):
        op.drop_column("pix_deposits", column)
    op.alter_column("pix_deposits", "appointment_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_column("booking_holds", "charge_id")
    op.drop_column("booking_holds", "phase")
    op.drop_column("tenants", "deposit_hold_ttl_minutes")
    op.drop_column("tenants", "deposit_timing")
    op.drop_column("tenants", "payment_provider")
