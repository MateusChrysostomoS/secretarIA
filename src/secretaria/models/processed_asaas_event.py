"""ProcessedAsaasEvent model - idempotency ledger for incoming Asaas webhook events.

Mirrors models/processed_event.py (the Meta webhook ledger) exactly, but kept
as a SEPARATE table rather than reusing `processed_events`: Asaas event ids
and Meta event ids are different id spaces from two unrelated providers, and
keeping the ledgers apart means a collision between the two spaces can never
happen and either table can be pruned/reasoned about independently.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class ProcessedAsaasEvent(Base):
    """One row per Asaas event id already handled.

    The unique constraint on `event_id` is the backstop that makes
    `services/payments/deposit_lifecycle.py::apply_asaas_event` idempotent
    even under concurrent delivery / Asaas retries - see that function's
    docstring for the claim-then-mutate transaction shape.
    """

    __tablename__ = "processed_asaas_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(128), unique=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
