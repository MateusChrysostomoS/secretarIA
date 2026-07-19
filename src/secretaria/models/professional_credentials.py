"""Encrypted-at-rest credentials for a professional (own Google Calendar).

Mirrors `models/tenant_credentials.py`'s split: secrets never ride along with
`professionals` config reads/responses. Unlike `tenant_credentials` (its own
`id` PK + a separately-unique `tenant_id`), the PK here IS `professional_id`
itself - a strict 1:1 with `professionals`, at most one credential row per
professional. Holds ONLY the professional's own Google Calendar refresh
token; a professional without its own connection falls back to the
tenant-level credential (`services/tenant_config.professional_completeness`'s
`has_calendar` ORs the two - a tenant-wide connection covers every
professional that hasn't connected their own). The `_encrypted` suffix is
load-bearing: the structlog redactor and the *Out-schema whitelist
convention key off it.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class ProfessionalCredentials(Base):
    """At most one row per professional, holding its own encrypted Calendar refresh token."""

    __tablename__ = "professional_credentials"

    professional_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"), primary_key=True
    )
    # Fernet ciphertext of the Google Calendar refresh_token. NULL = this
    # professional has not connected its own Calendar (falls back to the
    # tenant-level credential). NEVER logged, NEVER returned by an API response.
    google_refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
