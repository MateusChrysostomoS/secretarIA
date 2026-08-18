"""Why a patient walked away after their doctor cancelled.

This is BUSINESS data — churn signal — not a log line. When a clinic cancels on
somebody and that somebody declines to rebook, the reason is the most valuable
thing the conversation produces: "vou procurar outra clínica" and "não preciso
mais" are the same tap on the same button and mean opposite things commercially.

Modelled as an EVENT rather than a column on `appointments` (FEAT_34 §8.3):

* An appointment can be cancelled, rebooked and cancelled again; a mutable
  column would keep only the last answer and silently destroy the earlier ones.
* The fact recorded is "at this moment the patient said this" — it has its own
  timestamp and is never edited afterwards.
* It keeps `appointments` free of a field only one conversational branch writes.

NOT `AnalyticsEvent`: that table's contract is explicitly "minimal and
non-personal — no patient name, no phone, no free text", and it carries no
patient reference so LGPD erasure never touches it. This row is the opposite on
every count, so folding it in there would quietly break both guarantees.

`reason_code` is the tapped option (a stable key, safe to group by in a metrics
query); `reason_text` is whatever the patient typed, which is PATIENT CONTENT —
it lives here and nowhere else, is never logged, and carries the same LGPD
retention obligations as any other patient message.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class RebookingDecline(Base):
    """One patient's stated reason for not rebooking after a doctor cancelled."""

    __tablename__ = "rebooking_declines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Denormalised on purpose: every metrics query for this is per clinic, and
    # it keeps tenant scoping a single predicate instead of a join through
    # `appointments` — the same reason the other per-tenant tables carry it.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), index=True
    )
    # Stable key for the tapped option ("other_clinic", "no_longer_needed",
    # "reschedule_later", "other"), or "free_text" when they typed instead of
    # tapping. Grouping is done on THIS, never on the free text.
    reason_code: Mapped[str] = mapped_column(String(64))
    # Patient content. Never logged, never leaves the tenant.
    reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
