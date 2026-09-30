"""PatientPaymentCustomer - the provider-side customer id of a patient, per provider.

Generalises `Patient.asaas_customer_id` (which stays until the "narrow" phase). It stores
ONLY the provider's opaque id: never a CPF, an e-mail or any document (decision D7).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class PatientPaymentCustomer(Base):
    __tablename__ = "patient_payment_customers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # CASCADE: erasing a patient must erase the mapping (the erasure endpoint reads the
    # provider ids FIRST to ask the provider to delete the remote customer).
    patient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(16))
    provider_customer_id: Mapped[str] = mapped_column(String(96))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "patient_id", "provider", name="uq_patient_payment_customers_key"
        ),
    )
