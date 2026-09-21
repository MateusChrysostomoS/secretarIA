"""Slot reservations: the ten minutes between "Confirmar" and a verified code.

`models/booking_hold.py` says WHY the table exists; this module is the only
thing allowed to write it, and the two rules it exists to enforce are:

1. **The database row comes first, the Google event last.** A hold is a
   promise about a window, made in Postgres, that costs nothing to abandon.
   The Google event — the artefact that is expensive to abandon, because a
   failed clean-up leaves an orphan on a real clinic's agenda — is created
   exactly once, at promotion, after the code has already been verified.

2. **Expiry is a read rule.** Every query here carries `expires_at > now`, so
   a hold nobody promoted stops blocking its slot by the clock alone. No job
   has to run, and a worker that is down cannot leave an abandoned slot
   reserved forever.

The gate itself (`BookingGate`) is an injected collaborator rather than a
branch inside `services/flow_router.py`: the router stays free of brain-api,
of the database and of any notion of which channel it is serving, exactly as
it is free of them today. `workers/tasks.py` builds the gate, arms it only for
a Brain-Message visitor, and hands it in next to the calendar.

Note what is NOT here: nothing reads or writes an e-mail address. The gate
asks brain-api to mail a code (`services/pending_identity.py::request_code`
takes no address by design) and receives back at most a MASK.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, or_, select

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import BookingHold
from secretaria.services.pending_identity import RequestCodeOutcome, request_code

logger = get_logger(__name__)

# The same ten minutes brain-api gives the OTP challenge
# (`brain-api/docs/CHECKPOINT_portal_sessao_pendente.md` §5.4). ONE clock: a
# hold that outlived the code would reserve a slot no code could still
# confirm, and a code that outlived the hold would confirm a slot somebody
# else may already have taken.
HOLD_TTL_MINUTES = 10


def _aware(value: datetime) -> datetime:
    """UTC-attach a naive timestamp read back from the DB.

    SQLite (the test engine) does not store offsets, so a column declared
    `DateTime(timezone=True)` comes back naive there while it comes back aware
    on Postgres. Comparing the two shapes raises, and the comparison in
    question is the one that decides whether a slot is still reserved — so it
    is normalized once, here, instead of at each call site.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class HeldSlot:
    """A live hold, flattened. Plain values, safe to use after the session closes."""

    id: UUID
    tenant_id: UUID
    conversation_id: UUID
    patient_id: UUID | None
    professional_id: UUID | None
    appointment_type: str | None
    insurance: str | None
    start_at: datetime
    end_at: datetime
    expires_at: datetime

    @classmethod
    def of(cls, row: BookingHold) -> "HeldSlot":
        return cls(
            id=row.id,
            tenant_id=row.tenant_id,
            conversation_id=row.conversation_id,
            patient_id=row.patient_id,
            professional_id=row.professional_id,
            appointment_type=row.appointment_type,
            insurance=row.insurance,
            start_at=_aware(row.start_at),
            end_at=_aware(row.end_at),
            expires_at=_aware(row.expires_at),
        )


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open [start, end) overlap. Pure.

    Half-open on purpose: a 10:00-10:30 booking and a 10:30-11:00 one are
    adjacent, not conflicting, which is exactly how `CalendarService`'s own
    slot walk treats them.
    """
    return a_start < b_end and b_start < a_end


async def purge_expired(tenant_id: UUID | None = None) -> int:
    """Delete holds that already expired. Housekeeping only — never correctness.

    Correctness comes from the `expires_at > now` filter every read carries;
    this just stops the table growing without bound. Returns the row count so
    a caller can log it.
    """
    async with async_session_factory() as session:
        async with session.begin():
            stmt = delete(BookingHold).where(BookingHold.expires_at <= _now())
            if tenant_id is not None:
                stmt = stmt.where(BookingHold.tenant_id == tenant_id)
            result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def held_windows(
    tenant_id: UUID,
    professional_id: UUID | None,
    *,
    exclude_conversation: UUID | None = None,
) -> list[tuple[datetime, datetime]]:
    """Live holds on ONE agenda, as [start, end) windows.

    `professional_id=None` means the tenant-level agenda, the same meaning it
    carries on `appointments.professional_id` — so a hold on Dr. A never hides
    a slot on Dr. B's calendar, and a hold on the tenant agenda never hides one
    on a professional's.

    `exclude_conversation` is what lets a patient still see (and re-confirm)
    the very slot THEY are holding: their own reservation must not make their
    own choice disappear from the list.
    """
    stmt = select(BookingHold.start_at, BookingHold.end_at).where(
        BookingHold.tenant_id == tenant_id,
        BookingHold.expires_at > _now(),
    )
    stmt = stmt.where(
        BookingHold.professional_id == professional_id
        if professional_id is not None
        else BookingHold.professional_id.is_(None)
    )
    if exclude_conversation is not None:
        stmt = stmt.where(BookingHold.conversation_id != exclude_conversation)
    async with async_session_factory() as session:
        rows = (await session.execute(stmt)).all()
    return [(_aware(start), _aware(end)) for start, end in rows]


async def live_hold_in(session, conversation_id: UUID) -> HeldSlot | None:
    """This conversation's unexpired hold, read on a session the caller owns.

    Separate from `live_hold` because the inbound leg of `workers/tasks.py`
    asks this question from INSIDE its own open transaction. Opening a second
    session there would be a second connection against a row the first one is
    already holding — and on the SQLite engine the tests run, literally the
    same connection.

    None covers both "never held anything" and "held something that expired" —
    the caller has to tell the patient the same thing either way, and the
    distinction is one the transcript already records.
    """
    row = await session.scalar(
        select(BookingHold)
        .where(
            BookingHold.conversation_id == conversation_id,
            BookingHold.expires_at > _now(),
        )
        .order_by(BookingHold.created_at.desc())
    )
    return HeldSlot.of(row) if row is not None else None


async def latest_hold_in(session, conversation_id: UUID) -> HeldSlot | None:
    """`latest_hold` on a session the caller owns.

    Both shapes exist for the same reason `live_hold`/`live_hold_in` do: the
    worker holds its own session factory (and its tests patch THAT symbol),
    while the gate runs from the router with no session in hand.
    """
    row = await session.scalar(
        select(BookingHold)
        .where(BookingHold.conversation_id == conversation_id)
        .order_by(BookingHold.created_at.desc())
    )
    return HeldSlot.of(row) if row is not None else None


async def release_in(session, hold_id: UUID) -> None:
    """Drop one hold on a session the caller owns. Caller commits."""
    await session.execute(delete(BookingHold).where(BookingHold.id == hold_id))


async def latest_hold(conversation_id: UUID) -> HeldSlot | None:
    """This conversation's most recent hold, EXPIRED OR NOT.

    The distinction `live_hold` erases is the one the verify leg needs: "this
    conversation was never gated" and "this conversation was gated and the ten
    minutes ran out" must produce different messages — the first keeps today's
    behaviour untouched, the second has to tell the patient their slot went
    back on sale. The caller compares `expires_at` itself.
    """
    async with async_session_factory() as session:
        row = await session.scalar(
            select(BookingHold)
            .where(BookingHold.conversation_id == conversation_id)
            .order_by(BookingHold.created_at.desc())
        )
        return HeldSlot.of(row) if row is not None else None


async def live_hold(conversation_id: UUID) -> HeldSlot | None:
    """`live_hold_in` on a session of its own, for callers outside a transaction."""
    async with async_session_factory() as session:
        return await live_hold_in(session, conversation_id)


async def release(hold_id: UUID) -> None:
    """Drop one hold. Best-effort: an undeleted row expires on its own anyway."""
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(BookingHold).where(BookingHold.id == hold_id))
    except Exception as exc:  # pragma: no cover - logged, never raised at a caller
        logger.warning("booking_hold_release_failed", hold_id=str(hold_id), error=str(exc))


async def place_hold(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    patient_id: UUID | None,
    professional_id: UUID | None,
    appointment_type: str | None,
    insurance: str | None,
    start_at: datetime,
    end_at: datetime,
    ttl_minutes: int = HOLD_TTL_MINUTES,
) -> HeldSlot | None:
    """Reserve [start_at, end_at) for this conversation. None = somebody else has it.

    One transaction does all three things that have to agree with each other:
    expired rows go, this conversation's own previous hold goes (a patient who
    went back and picked a different time must not hold two slots), and the
    conflict test then runs against what is left. Doing the check in Python
    rather than with a UNIQUE constraint is deliberate and is explained on the
    model: `professional_id` is nullable, and a Postgres UNIQUE would stop
    protecting precisely the single-professional tenants that are the majority.
    """
    now = _now()
    expires_at = now + timedelta(minutes=ttl_minutes)
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(BookingHold).where(
                    BookingHold.tenant_id == tenant_id,
                    or_(
                        BookingHold.expires_at <= now,
                        BookingHold.conversation_id == conversation_id,
                    ),
                )
            )
            await session.flush()
            agenda = (
                BookingHold.professional_id == professional_id
                if professional_id is not None
                else BookingHold.professional_id.is_(None)
            )
            existing = (
                await session.execute(
                    select(BookingHold.start_at, BookingHold.end_at).where(
                        BookingHold.tenant_id == tenant_id,
                        BookingHold.expires_at > now,
                        agenda,
                    )
                )
            ).all()
            for other_start, other_end in existing:
                if overlaps(start_at, end_at, _aware(other_start), _aware(other_end)):
                    logger.info(
                        "booking_hold_conflict",
                        tenant_id=str(tenant_id),
                        conversation_id=str(conversation_id),
                    )
                    return None
            row = BookingHold(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                patient_id=patient_id,
                professional_id=professional_id,
                appointment_type=appointment_type,
                insurance=insurance,
                start_at=start_at,
                end_at=end_at,
                expires_at=expires_at,
            )
            session.add(row)
            await session.flush()
            held = HeldSlot.of(row)
    logger.info(
        "booking_hold_placed",
        tenant_id=str(tenant_id),
        conversation_id=str(conversation_id),
        hold_id=str(held.id),
        ttl_minutes=ttl_minutes,
    )
    return held


# ---------------------------------------------------------------------------
# The gate handed to the flow router
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    """What the router should do with the slot the patient just confirmed.

    "commit"     - behave exactly as before this feature existed: create the
                   Google event now and confirm the appointment. Every path
                   that is not a Brain-Message visitor owing a code ends here,
                   and so does every failure of the gate itself.
    "held"       - the slot is reserved and a code is in the patient's inbox.
                   No event, no appointment row: the router asks for the code.
    "slot_taken" - somebody else is holding this exact window. The router
                   sends the patient back to pick another time.
    """

    outcome: Literal["commit", "held", "slot_taken"]
    email_masked: str | None = None
    hold_id: UUID | None = None


class BookingGate:
    """Turns "Confirmar" into a hold + a mailed code, for a visitor who owes one.

    Built per turn by `workers/tasks.py` and handed to `flow_router.route()`
    beside the calendar. `armed=False` (WhatsApp, and any turn we could not
    scope to a tenant) makes `decide` a pure `commit` — the hold lookups stay
    live even then, because a slot reserved by a Portal visitor has to be
    invisible on WhatsApp too.

    FAIL-OPEN, and that is the most important sentence here. Every way this can
    go wrong — brain-api unreachable, the visit already verified, no address on
    the visit — resolves to "commit": the patient keeps the booking they just
    made. A gate that failed closed would turn a brain-api outage into a clinic
    that cannot take appointments at all, which is a far worse failure than an
    unverified patient holding a real slot.
    """

    def __init__(
        self,
        *,
        tenant_id: UUID | None,
        conversation_id: UUID,
        patient_id: UUID | None,
        external_id: str | None,
        armed: bool,
    ) -> None:
        self._tenant_id = tenant_id
        self._conversation_id = conversation_id
        self._patient_id = patient_id
        self._external_id = (external_id or "").strip()
        self._armed = bool(armed and tenant_id is not None and self._external_id)

    @property
    def armed(self) -> bool:
        return self._armed

    async def busy_windows(self, professional_id: UUID | None) -> list[tuple[datetime, datetime]]:
        """Windows other conversations are holding on this agenda, for slot lists."""
        if self._tenant_id is None:
            return []
        try:
            return await held_windows(
                self._tenant_id,
                professional_id,
                exclude_conversation=self._conversation_id,
            )
        except Exception as exc:
            # A failed read must never hide the whole agenda. Worst case a held
            # slot is offered twice and `decide` answers `slot_taken`.
            logger.warning("booking_hold_windows_failed", error=str(exc))
            return []

    async def decide(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        professional_id: UUID | None,
        appointment_type: str | None,
        insurance: str | None,
    ) -> GateDecision:
        """Hold the slot and mail a code, or tell the router to commit as usual."""
        if not self._armed or self._tenant_id is None:
            return GateDecision("commit")

        try:
            held = await place_hold(
                tenant_id=self._tenant_id,
                conversation_id=self._conversation_id,
                patient_id=self._patient_id,
                professional_id=professional_id,
                appointment_type=appointment_type,
                insurance=insurance,
                start_at=start_at,
                end_at=end_at,
            )
        except Exception as exc:
            # The hold could not be written. Committing now is the fail-open
            # answer: the patient keeps the appointment, ungated, and the log
            # line is what says the gate did not run for this booking.
            logger.warning(
                "booking_gate_hold_failed",
                error=str(exc),
                conversation_id=str(self._conversation_id),
            )
            return GateDecision("commit")
        if held is None:
            return GateDecision("slot_taken")

        try:
            result = await request_code(self._tenant_id, self._external_id)
        except Exception as exc:
            # `request_code` fails closed by contract and should not raise;
            # belt and braces, because escaping here would leave a hold on a
            # slot nobody is ever going to confirm.
            logger.warning("booking_gate_request_raised", error=str(exc))
            await release(held.id)
            return GateDecision("commit")

        if result.outcome is not RequestCodeOutcome.SENT:
            # NOT_PENDING (this visitor already has an account — there is
            # nothing to prove), NO_EMAIL (no address was ever captured) and
            # UNAVAILABLE all mean the same thing to the patient: no code is
            # coming, so no code may be asked for. The hold goes back and the
            # booking is committed the way it always was.
            logger.info(
                "booking_gate_stood_down",
                reason=result.outcome.value,
                tenant_id=str(self._tenant_id),
                conversation_id=str(self._conversation_id),
            )
            await release(held.id)
            return GateDecision("commit")

        logger.info(
            "booking_gate_held",
            tenant_id=str(self._tenant_id),
            conversation_id=str(self._conversation_id),
            hold_id=str(held.id),
        )
        return GateDecision("held", email_masked=result.email_masked, hold_id=held.id)
