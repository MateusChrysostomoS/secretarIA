"""Encrypted-at-rest credentials for a tenant.

Kept in a SEPARATE table from `tenants` (config) so secrets never ride along in
config reads/responses and are easy to audit. Today it holds the Google Calendar
refresh token (introduced by the OAuth callback, encrypted with core.crypto).
The WhatsApp/WABA token stays on `tenants` for now (provisioned manually).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class TenantCredentials(Base):
    """One row per tenant holding its encrypted Google Calendar refresh token."""

    __tablename__ = "tenant_credentials"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, index=True
    )
    # Fernet ciphertext of the Google Calendar refresh_token. NULL = not connected.
    # NEVER logged, NEVER returned by an API response.
    google_refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
