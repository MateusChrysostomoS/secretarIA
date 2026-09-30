"""PaymentWebhookEvent - the durable, per-clinic ledger of provider webhook events.

Absorbs `processed_asaas_events` (which stays until the "narrow" phase, because the old
`/webhooks/asaas` route and its `process_asaas_event` job keep running until each clinic is
moved to the per-clinic URL).

Written by the webhook handler BEFORE it answers 200 (persist-first; see
`api/webhook_payments.py` for why that is a deliberate, payments-only exception to the
Meta fast-ACK rule). The unique key includes the tenant: an `event_id` forged for clinic A
can never block the same id arriving for clinic B (the old ledger had a GLOBAL unique).

`payload` is MINIMISED before it is stored (payer, customer, document, e-mail, phone and
receipt URLs are removed, `services/payments/webhook_payload.py`) and set NULL after 30 days
by the daily job; the row itself stays.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

EVENT_RECEIVED = "received"
EVENT_PROCESSED = "processed"
EVENT_IGNORED = "ignored"
EVENT_FAILED = "failed"


class PaymentWebhookEvent(Base):
    __tablename__ = "payment_webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(16))
    event_id: Mapped[str] = mapped_column(String(160))
    provider_charge_id: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), server_default="", default="")
    status: Mapped[str] = mapped_column(
        String(16), server_default=EVENT_RECEIVED, default=EVENT_RECEIVED
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    # A short symbolic code (an exception class name or a ProviderError.code), never a message.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "event_id", name="uq_payment_webhook_events_event"
        ),
        Index("ix_payment_webhook_events_status_received", "status", "received_at"),
    )
