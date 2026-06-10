"""Appointment model - links a Google Calendar event to a patient + phone."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class AppointmentStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"
    CONFIRMED = "confirmed"
    ATTENDED = "attended"
    NO_SHOW = "no_show"


class Appointment(Base):
    """Platform-side record of a clinic appointment.

    Created whenever the platform (agent or doctor hub) creates an event on
    Google Calendar. Stores the google_event_id so actions like cancel/reschedule
    can look up the event, and the patient phone so the platform knows who to
    notify without re-fetching from WhatsApp.

    Appointments created by the bot (via create_event tool) have patient_id set.
    Block slots created via the doctor hub have patient_id = NULL.
    """

    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The conversation the booking came from (bot path). NULL for doctor-hub
    # creations and block slots. SET NULL so deleting a conversation keeps the
    # appointment record intact.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    google_event_id: Mapped[str] = mapped_column(String(255), index=True)
    # htmlLink returned by Google Calendar on insert — handy for the doctor hub
    # and for confirmation messages without re-fetching the event.
    google_event_link: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Name of the appointment type/reason chosen (e.g. "Primeira consulta").
    appointment_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # The booked window, mirrored from Google Calendar so appointments can be
    # queried (reminders, analytics) without round-tripping to Google.
    start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phone stored at appointment creation time so cancel/reschedule can reach
    # the patient even if the Patient row changes (e.g. number ported).
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[AppointmentStatus] = mapped_column(
        SAEnum(AppointmentStatus, name="appointment_status", create_type=True),
        default=AppointmentStatus.SCHEDULED,
        server_default="scheduled",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
