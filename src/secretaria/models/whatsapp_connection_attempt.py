"""WhatsAppConnectionAttempt - one clinic's attempt to connect its WhatsApp (TASK-093 Plano 3).

The secretarIA backend exchanges the Meta Embedded Signup `code` for an access token,
subscribes the app to the clinic's WABA and wires the number itself
(`services/whatsapp_connect.py`). Each try is one row keyed by the client-supplied
`attempt_id`, unique PER CLINIC (`uq_whatsapp_connection_attempt`): a replay of the same
id finds the row and never exchanges the code twice, and one clinic can never collide with
another's id.

NO secret lives here - never the Meta `code`, never the token (that stays Fernet
ciphertext in `tenant_credentials.waba_token_encrypted`, written only through
`services/tenant_config.py::set_waba_token`).

`status` is a plain VARCHAR with no CHECK (repo convention, see clinic_payment_account.py):
`pending` (created, Meta not finished), `connected`, `failed` (+ `failure_code`).
`brain_notified_at` is NULL until brain-api acknowledged the connection (it still owns the
onboarding state and the charge tied to it); the worker retries every connected row that
is still NULL.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

ATTEMPT_PENDING = "pending"
ATTEMPT_CONNECTED = "connected"
ATTEMPT_FAILED = "failed"
ATTEMPT_STATUSES: tuple[str, ...] = (ATTEMPT_PENDING, ATTEMPT_CONNECTED, ATTEMPT_FAILED)


class WhatsAppConnectionAttempt(Base):
    """One WhatsApp connection attempt of one clinic."""

    __tablename__ = "whatsapp_connection_attempts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "attempt_id", name="uq_whatsapp_connection_attempt"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    attempt_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), server_default=ATTEMPT_PENDING, default=ATTEMPT_PENDING
    )
    phone_number_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    waba_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    brain_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
