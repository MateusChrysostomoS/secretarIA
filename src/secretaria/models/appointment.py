"""Appointment model - links a Google Calendar event to a patient + phone.

## Status taxonomy (PROMPT_FIX_16 - the one canonical definition)

A booking has exactly ONE row for its whole life. Rescheduling MOVES that row
(new `start_at`/`end_at`, same id, same `google_event_id`, same PixDeposit) -
it never creates a replacement and never retires the original. Both carriers
do this: the deterministic flow (`workers/tasks.py::_apply_flow_result`) and
the doctor hub (`api/hub/calendar.py::reschedule_appointment`).

So `RESCHEDULED` means **"this booking is still happening, at a time that has
already been changed once or more"** - it is a LIVE status, not a tombstone.
Reading it as terminal is what made a moved booking vanish from the manage
flow, the reminders sweep, the greeting and the LLM's appointment tools.

    LIVE (the appointment is still going to happen)
      SCHEDULED    just booked, nobody confirmed presence yet
      CONFIRMED    the patient confirmed presence (reminder button / flow)
      RESCHEDULED  the same booking, moved to a new window

    TERMINAL (the appointment resolved; it will not happen again)
      CANCELLED    called off (patient, doctor, or deposit expiry)
      ATTENDED     the doctor marked the patient as seen
      NO_SHOW      the doctor marked the patient as absent

Transitions (source: `flow` = deterministic router, `button` = reminder action
button, `hub` = doctor hub, `system` = deposit-expiry sweep):

    SCHEDULED    -> CONFIRMED    (button apptconfirm)
    SCHEDULED    -> RESCHEDULED  (flow / button apptresched / hub)
    SCHEDULED    -> CANCELLED    (flow / button / hub / system)
    CONFIRMED    -> RESCHEDULED  (flow / button apptresched / hub)
    CONFIRMED    -> CANCELLED    (flow / button / hub)
    RESCHEDULED  -> RESCHEDULED  (moved again, up to pix_reschedule_limit)
    RESCHEDULED  -> CONFIRMED    (button apptconfirm on a moved booking)
    RESCHEDULED  -> CANCELLED    (flow / button / hub)
    any LIVE     -> ATTENDED | NO_SHOW  (hub, doctor-set only)

There is no automatic transition out of a LIVE status when the time passes:
ATTENDED/NO_SHOW are set by hand in the hub, which is why the "just had a
consult" lookback (services/patient_context.py) is time-bounded rather than
status-driven.

Every status filter in the codebase MUST use the shared constants below -
`services/appointment_status.py` carries the matching transition logger. Do
not spell a new tuple of statuses inline; that is exactly how the readers and
writers drifted apart in the first place.
"""

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


# The appointment is still going to happen: it stays manageable (cancel /
# reschedule / confirm), remindable, and visible in greetings and the agent's
# appointment tools. See the module docstring for why RESCHEDULED belongs here.
LIVE_APPOINTMENT_STATUSES: tuple[AppointmentStatus, ...] = (
    AppointmentStatus.SCHEDULED,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.RESCHEDULED,
)

# The appointment resolved. Never upcoming, never remindable.
TERMINAL_APPOINTMENT_STATUSES: tuple[AppointmentStatus, ...] = (
    AppointmentStatus.CANCELLED,
    AppointmentStatus.ATTENDED,
    AppointmentStatus.NO_SHOW,
)


def is_live_status(status: AppointmentStatus | None) -> bool:
    """True when the booking is still going to happen (see the taxonomy above)."""
    return status in LIVE_APPOINTMENT_STATUSES


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
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Phone stored at appointment creation time so cancel/reschedule can reach
    # the patient even if the Patient row changes (e.g. number ported).
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Which professional this booking is with (multi_professional addon). NULL
    # for tenants without the addon, or when the agent didn't route through a
    # professional-aware tool. SET NULL so deleting a professional keeps the
    # appointment record intact.
    professional_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("professionals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Which physical location this booking is at (multi_unit addon). NULL for
    # tenants without the addon. SET NULL so deleting a unit keeps the record.
    unit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("units.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Convênio/insurance label the patient selected (or typed) during the
    # deterministic booking flow. Informational only — never filters
    # professionals or slots (clinic-wide fact, see tenants.insurances).
    insurance: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[AppointmentStatus] = mapped_column(
        # The Postgres `appointment_status` type stores the lowercase enum
        # *values* ("scheduled", ...) — see the c4d8e2f1a5b6 migration. Without
        # values_callable, SQLAlchemy persists the member *names* ("SCHEDULED")
        # instead, which the native PG enum rejects with
        # InvalidTextRepresentationError. values_callable makes both writes and
        # reads use the values, matching the deployed type. (Not caught by the
        # SQLite test suite, which renders Enum as a permissive VARCHAR+CHECK.)
        SAEnum(
            AppointmentStatus,
            name="appointment_status",
            create_type=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=AppointmentStatus.SCHEDULED,
        server_default="scheduled",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
