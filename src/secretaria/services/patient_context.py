"""Patient appointment context — shared queries + the opening-state resolver.

One source of truth for "does this patient have appointments?": used by the
manage (cancel/reschedule) flow trigger and the conversation-opening greeting
router (workers/tasks.py), and by the read-only `list_patient_appointments`
agent tool (ai/tools.py).

HARD RULE: appointment state is DERIVED here at message time, never stored as
a flag on Patient — a stored flag desyncs on book/cancel/complete.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.models import LIVE_APPOINTMENT_STATUSES, Appointment, AppointmentStatus, Message

logger = get_logger(__name__)

# A live upcoming booking — the shared taxonomy, never a local tuple
# (models/appointment.py). CONFIRMED counts alongside SCHEDULED: a confirmed
# appointment is still upcoming and still manageable. So does RESCHEDULED
# (PROMPT_FIX_16): a reschedule MOVES this very row to a new window, so a
# booking that was moved once is every bit as upcoming as one that never was.
# Leaving it out is what made a rescheduled consult vanish from the manage
# flow, the greeting and `list_patient_appointments`.
UPCOMING_STATUSES = LIVE_APPOINTMENT_STATUSES

# Excluded from "recent past": a cancelled row never means the patient just had
# (or missed) a consult. RESCHEDULED is NOT excluded any more - the row's
# `start_at` was moved to the new window, so a past `start_at` on a rescheduled
# booking means that booking really did just happen.
_RECENT_PAST_EXCLUDED = (AppointmentStatus.CANCELLED,)


def as_utc(dt: datetime) -> datetime:
    """Treat a naive timestamp (e.g. from SQLite) as UTC; pass tz-aware through."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class PatientOpeningState(str, Enum):  # noqa: UP042 - StrEnum would change str(member)
    """The five opening states, in first-match order (see the resolver)."""

    HAS_UPCOMING_SOON = "has_upcoming_soon"
    HAS_UPCOMING = "has_upcoming"
    JUST_HAD_CONSULT = "just_had_consult"
    RETURNING_NO_APPOINTMENT = "returning_no_appointment"
    NEW = "new"


@dataclass(frozen=True)
class PatientOpeningContext:
    """Resolved opening state + the data that produced it.

    Buckets are evaluated lazily in first-match order, so only the fields the
    matched state needed are populated (e.g. `recent_past_appointments` stays
    empty when a future appointment already decided the state, and
    `has_history` is only meaningful for the last two states).
    """

    state: PatientOpeningState
    future_appointments: list[dict] = field(default_factory=list)
    recent_past_appointments: list[dict] = field(default_factory=list)
    has_history: bool = False


async def load_upcoming_appointments(
    session: AsyncSession, tenant_id, patient_id, *, now: datetime | None = None
) -> list[dict]:
    """Future LIVE appointments for a patient, nearest first.

    "Live" is SCHEDULED / CONFIRMED / RESCHEDULED — see UPCOMING_STATUSES above
    and the taxonomy on models/appointment.py.

    Extracted from workers/tasks.py so the manage flow, the opening-state
    resolver and the `list_patient_appointments` agent tool share ONE query.
    Returned as plain dicts (detached from the ORM) so callers can read them
    after the session closes.
    """
    now = now or datetime.now(UTC)
    rows = await session.scalars(
        select(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.tenant_id == tenant_id,
            Appointment.status.in_(UPCOMING_STATUSES),
            Appointment.start_at >= now,
        )
        .order_by(Appointment.start_at)
    )
    return [
        {
            "id": str(appt.id),
            "google_event_id": appt.google_event_id,
            "appointment_type": appt.appointment_type,
            "insurance": appt.insurance,
            "attendee_name": appt.attendee_name,
            "start_at": appt.start_at,
            "end_at": appt.end_at,
            # Which professional owns this booking (None = tenant-level) - lets
            # the manage flow act on the owning calendar instead of a stale
            # booking-flow selection (workers/tasks.py::_manage_owner_calendar_target).
            "professional_id": str(appt.professional_id) if appt.professional_id else None,
            **({"google_calendar_source": appt.google_calendar_source}
               if appt.google_calendar_source is not None else {}),
        }
        for appt in rows
    ]


async def _load_recent_past_appointments(
    session: AsyncSession, tenant_id, patient_id, *, now: datetime, window: timedelta
) -> list[dict]:
    """Non-cancelled appointments that STARTED within the last `window`, newest first.

    Only this bounded lookback is ever considered: there is no auto-transition
    to attended/no_show (the doctor sets status manually via the hub), so an
    old appointment stuck in SCHEDULED must never read as "just had a consult".
    `status` carries the AppointmentStatus member itself (not .value).
    """
    rows = await session.scalars(
        select(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.tenant_id == tenant_id,
            Appointment.status.not_in(_RECENT_PAST_EXCLUDED),
            Appointment.start_at < now,
            Appointment.start_at >= now - window,
        )
        .order_by(Appointment.start_at.desc())
    )
    return [
        {
            "id": str(appt.id),
            "appointment_type": appt.appointment_type,
            "start_at": appt.start_at,
            "end_at": appt.end_at,
            "status": appt.status,
        }
        for appt in rows
    ]


async def resolve_patient_opening_state(
    session: AsyncSession,
    tenant_id: UUID,
    patient_id: UUID | None,
    *,
    now: datetime | None = None,
) -> PatientOpeningContext | None:
    """Resolve the rich opening state for a patient, or None when unresolved.

    `patient_id=None` (not resolved at the gate) returns None: the caller MUST
    degrade to the existing first-contact greeting — never to a "you have no
    appointment" message. Erring toward first-contact is safe; erring toward
    "no appointment" when they have one is not.
    """
    if patient_id is None:
        return None
    now = now or datetime.now(UTC)
    settings = get_settings()

    future = await load_upcoming_appointments(session, tenant_id, patient_id, now=now)
    if future:
        soon_cutoff = now + timedelta(hours=settings.UPCOMING_SOON_HOURS)
        state = (
            PatientOpeningState.HAS_UPCOMING_SOON
            if as_utc(future[0]["start_at"]) <= soon_cutoff
            else PatientOpeningState.HAS_UPCOMING
        )
        context = PatientOpeningContext(state=state, future_appointments=future)
    else:
        recent_past = await _load_recent_past_appointments(
            session,
            tenant_id,
            patient_id,
            now=now,
            window=timedelta(hours=settings.POST_CONSULT_WINDOW_HOURS),
        )
        if recent_past:
            context = PatientOpeningContext(
                state=PatientOpeningState.JUST_HAD_CONSULT,
                recent_past_appointments=recent_past,
            )
        else:
            has_history = (
                await session.scalar(
                    select(Appointment.id)
                    .where(
                        Appointment.patient_id == patient_id,
                        Appointment.tenant_id == tenant_id,
                        Appointment.start_at.is_not(None),
                        Appointment.start_at < now,
                    )
                    .limit(1)
                )
            ) is not None
            context = PatientOpeningContext(
                state=(
                    PatientOpeningState.RETURNING_NO_APPOINTMENT
                    if has_history
                    else PatientOpeningState.NEW
                ),
                has_history=has_history,
            )

    logger.info(
        "patient_opening_state_resolved",
        tenant_id=str(tenant_id),
        patient_id=str(patient_id),
        state=context.state.value,
        future_count=len(context.future_appointments),
        recent_past_count=len(context.recent_past_appointments),
    )
    return context


# A consult that did not happen earns no "como foi?" question.
_FOLLOWUP_EXCLUDED = (AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW)


async def find_post_consult_followup(
    session: AsyncSession,
    tenant_id: UUID,
    patient_id: UUID,
    conversation_id: UUID,
    *,
    now: datetime | None = None,
) -> dict | None:
    """The consult to ask "como foi?" about, on the patient's first appearance after it.

    The most recent appointment that already ENDED (end, or start when the end
    is unknown) within `POST_CONSULT_FOLLOWUP_DAYS`, cancelled and no-show
    excluded — and only when this conversation has carried NO message since it
    ended. That last condition is what makes it "the first appearance after
    the consult" without storing a flag (the module's hard rule): the question
    itself is a message, so once it went out the next entry reads as a normal
    one. A past row still SCHEDULED/CONFIRMED counts: nothing auto-marks a
    consult as attended, and asking how it went is safe either way.
    """
    now = now or datetime.now(UTC)
    window = timedelta(days=get_settings().POST_CONSULT_FOLLOWUP_DAYS)
    appointment = await session.scalar(
        select(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.tenant_id == tenant_id,
            Appointment.status.not_in(_FOLLOWUP_EXCLUDED),
            Appointment.start_at < now,
            Appointment.start_at >= now - window,
        )
        .order_by(Appointment.start_at.desc())
        .limit(1)
    )
    if appointment is None:
        return None
    if appointment.post_consult_notified_at is not None:
        # TASK-032 R7: the clinic's "Compareceu" already delivered the post-consult
        # message for this appointment - asking again on the next open would repeat it.
        return None
    ended_at = as_utc(appointment.end_at or appointment.start_at)
    if ended_at > now:
        return None  # still in progress
    spoken_since = await session.scalar(
        select(Message.id)
        .where(Message.conversation_id == conversation_id, Message.created_at >= ended_at)
        .limit(1)
    )
    if spoken_since is not None:
        return None
    return {
        "id": str(appointment.id),
        "appointment_type": appointment.appointment_type,
        "start_at": appointment.start_at,
        "end_at": appointment.end_at,
        "status": appointment.status,
        "attendee_name": appointment.attendee_name,
        "professional_id": (
            str(appointment.professional_id) if appointment.professional_id else None
        ),
    }
