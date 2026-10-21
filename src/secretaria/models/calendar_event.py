"""CalendarEvent - one event on the secretarIA's OWN agenda (no Google account needed).

## Why this table exists

Founder's decision 2026-10-20: "a agenda funciona de forma independente do Google
Calendar. Caso o Google esteja conectado, ele marca todos os compromissos e
sincroniza a agenda; caso não, vai ser no calendário da secretarIA mesmo".

So every agenda the product reads or writes has one of two backends, chosen by
credential alone (services/calendar.py::CalendarService.from_tenant_config):

    a Google refresh token covers it  ->  Google Calendar, exactly as before
    no credential at all              ->  THIS table (services/local_calendar.py)

One row is what one Google event used to be. Its `id` is a string (`loc_<hex>`)
precisely so it can sit in `appointments.google_event_id` next to real Google ids:
every caller that stores, compares, cancels or moves an event id keeps working
unchanged, and `is_local_event_id` tells the two apart without a lookup.

## Whose agenda

`professional_id` NULL is the clinic's own agenda; a professional id is that
doctor's own - the same meaning it has on `booking_holds.professional_id`. Unlike
Google mode, a doctor without a `google_calendar_id` still gets an agenda of his
own here: there is no Google account to share, and two doctors of one clinic must
not block each other's time.

## Handed over to Google

When the clinic connects Google later, FUTURE rows are pushed there once
(services/calendar_sync.py). A pushed row keeps its history but records
`google_event_id`/`synced_at`, and from then on no read of this table treats it as
busy or lists it: the Google event is the one that counts. Past rows stay here.

Carries what the Google event carried (title with the patient's name, the
service/convênio description) and nothing more: no phone, no e-mail, no attendee
list. Like the Google agenda, it is preserved by the patient-trail cleanup.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func, true
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

EVENT_STATUS_CONFIRMED = "confirmed"
EVENT_STATUS_CANCELLED = "cancelled"


class CalendarEvent(Base):
    """One timed event on one secretarIA agenda (the clinic's or one doctor's)."""

    __tablename__ = "calendar_events"

    # `loc_<uuid hex>` - see services/local_calendar.py::new_local_event_id.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # NULL = the clinic's own agenda. CASCADE like `booking_holds`: professionals are
    # only ever deactivated, and a deleted doctor's agenda has nobody left to read it.
    professional_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"), nullable=True
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Always written in UTC (SQLite, the test engine, keeps no offset).
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Google's `transparency`: False is "show me as free" - listed nowhere as busy.
    busy: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    # "confirmed" | "cancelled". A cancel is a status, never a DELETE: idempotent, and
    # the row is still there for the history a Google "deleted" event also keeps.
    status: Mapped[str] = mapped_column(
        String(16), server_default=EVENT_STATUS_CONFIRMED, default=EVENT_STATUS_CONFIRMED
    )
    # Set once the row was pushed to Google (services/calendar_sync.py).
    google_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # The shape of every read: "this agenda, in this window". Not a UNIQUE or an
        # EXCLUDE constraint: staff may overlap on purpose (an encaixe, a block over a
        # booking), so exclusivity is the patient writes' own check, done under a per-
        # agenda lock (services/local_calendar.py::agenda_write_lock).
        Index("ix_calendar_events_agenda_window", "tenant_id", "professional_id", "start_at"),
    )
