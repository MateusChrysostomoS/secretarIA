"""Tenant model - one clinic, with its own WhatsApp Business credentials."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class Tenant(Base):
    """A clinic using SecretarIA.

    The system is multi-tenant in the data model. For the MVP a single tenant
    is configured via environment variables.
    """

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    clinic_name: Mapped[str] = mapped_column(String(255))
    # The WhatsApp phone_number_id (NOT the phone number).
    phone_number_id: Mapped[str] = mapped_column(String(64), unique=True)
    waba_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # TODO(security): encrypt the access token at rest (Fernet / KMS / Vault).
    #   Stored in plaintext for the MVP only.
    access_token: Mapped[str] = mapped_column(String(512), default="")
    # Feature flag: only call the Precheck service when enabled for this tenant.
    precheck_enabled: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
