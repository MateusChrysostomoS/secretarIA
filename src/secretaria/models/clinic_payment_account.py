"""ClinicPaymentAccount - one row per (tenant, payment provider): the clinic's own PSP account.

Replaces the two Asaas-only columns of `tenant_credentials` (which stay until the "narrow"
phase, dual-read/dual-write, see the frozen-contract-migration skill). Every secret column
ends in `_encrypted` (Fernet ciphertext, `core.crypto`) so the structlog redactor and the
`*Out`-schema whitelist convention keep covering it (tenant-secrets-encryption skill).
Decryption happens ONLY in `services/tenant_config.py::get_payment_account`; nothing that
serialises a response ever receives this row.

`status` is a plain VARCHAR (no native PG enum, no CHECK): a new state never needs an
`ALTER TYPE`, which the SQLite test-suite would not catch. `connected` means the key was
validated, its permissions confirmed AND the webhook created and read back; any step
missing is `incomplete` with an `incomplete_reason` (spec section 6.2).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

PROVIDER_ASAAS = "asaas"
PROVIDER_MERCADOPAGO = "mercadopago"
PAYMENT_PROVIDERS = (PROVIDER_ASAAS, PROVIDER_MERCADOPAGO)

ACCOUNT_PENDING = "pending"
ACCOUNT_CONNECTED = "connected"
ACCOUNT_INCOMPLETE = "incomplete"
ACCOUNT_ERROR = "error"
ACCOUNT_REAUTH_REQUIRED = "reauth_required"
ACCOUNT_REVOKED = "revoked"


class ClinicPaymentAccount(Base):
    """The clinic's connection to ONE payment provider."""

    __tablename__ = "clinic_payment_accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(24), server_default=ACCOUNT_PENDING, default=ACCOUNT_PENDING
    )
    # The opaque "clinic id" of the webhook route (`/webhooks/<provider>/<slug>`): a random
    # UUID string, never the tenant id, and rotatable without touching the tenant.
    webhook_slug: Mapped[str] = mapped_column(String(64), unique=True)
    # Non-secret ids of the connection (seller / account id, the webhook registered at Asaas).
    external_account_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    external_webhook_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    # {"customers": bool, "payments": bool, "webhooks": bool} - the outcome of the
    # required probes. Never a value returned by the provider.
    capabilities: Mapped[dict] = mapped_column(
        JSON, server_default=text("'{}'"), default=dict
    )
    # NAMES of the permission groups found in excess by the last verdict; empty when accepted.
    excess_groups: Mapped[list] = mapped_column(
        JSON, server_default=text("'[]'"), default=list
    )
    # Short code: key_invalid | missing_permission:<groups> | excess_permissions |
    # probe_unavailable | webhook_create_failed | webhook_readback_failed |
    # key_expired_or_disabled | legacy_unverified
    incomplete_reason: Mapped[str | None] = mapped_column(String(96), nullable=True)
    # Consecutive daily rechecks that ended INCONCLUSIVE (alert only after 3, spec 12.3A).
    probe_inconclusive_days: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Charge health (TASK-071, Plan 3): the last failed / successful Pix charge made with this
    # account. The reason is a `FailureReason` value and the stage a short step name - never
    # provider text. A failure stays as history after a later success; health is derived by
    # `services/payments/accounts.py::charge_health`.
    last_charge_failure_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_charge_failure_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_charge_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_charge_ok_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Asaas onboarding states (TASK-071, Plan 4: GET /myAccount/status). Fixed words only
    # (PENDING | APPROVED | REJECTED | AWAITING_APPROVAL | UNKNOWN); all None when the key cannot
    # read them. `onboarding_checked_at` is when we last looked (readable or not).
    onboarding_general: Mapped[str | None] = mapped_column(String(24), nullable=True)
    onboarding_docs: Mapped[str | None] = mapped_column(String(24), nullable=True)
    onboarding_commercial: Mapped[str | None] = mapped_column(String(24), nullable=True)
    onboarding_bank: Mapped[str | None] = mapped_column(String(24), nullable=True)
    onboarding_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the CURRENT API key was stored (a new key replaces the old one): a charge that worked
    # with a previous key is not proof for this one (`accounts.charge_health`).
    key_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Setup checklist items only the manager can confirm (TASK-071, Plan 4): whitelisted keys
    # (`checklist.MANUAL_ITEMS`) -> {"done": bool, "at": iso, "by": user id}. A reconnection
    # never clears them.
    setup_checklist: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # --- secrets: Fernet ciphertext, NEVER logged, NEVER returned by a response ---
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_pkce_verifier_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_access_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", name="uq_clinic_payment_accounts_tenant_provider"
        ),
    )
