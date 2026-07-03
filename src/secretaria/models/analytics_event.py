"""AnalyticsEvent model - minimal, non-personal usage events (analytics_bi addon).

Entitlement-gated by the `analytics_bi` plugin (plugins/analytics_bi.py),
recorded from its `post_booking` hook. `payload` MUST stay minimal and
non-personal: no patient name, no phone, no free text. Today's only event
kind ("appointment_booked") carries `appointment_type`, `professional_id`
(str, or None) and `source` ("agent" | "flow") — enough to build the doctor
hub's summary endpoint (api/hub/analytics.py) without ever storing anything
LGPD-sensitive here.

Referenced by the LGPD privacy contract (api/internal_privacy.py): analytics
events NEVER carry a patient reference, so erasure never touches this table —
the export bundle's `analytics_events` list is always empty, by construction,
not by filtering.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class AnalyticsEvent(Base):
    """One minimal, non-personal business event for a tenant."""

    __tablename__ = "analytics_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(48))
    # Minimal, non-personal fields only — see module docstring.
    payload: Mapped[dict] = mapped_column(JSON, server_default=text("'{}'"), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
