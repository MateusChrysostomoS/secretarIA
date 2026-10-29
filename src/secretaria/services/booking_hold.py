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
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import delete, or_, select, text, update

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import BookingHold, Conversation, Tenant
from secretaria.services.patient_context import load_upcoming_appointments
from secretaria.services.payments.failure_reason import FailureReason
from secretaria.services.pending_identity import RequestCodeOutcome, request_code

if TYPE_CHECKING:  # pragma: no cover
    from secretaria.services.payments.booking_charge import PaymentStart

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
    attendee_name: str | None = None
    # "code" (the OTP clock, 10 minutes) or "payment" (the clinic's deposit TTL). The
    # "hold never outlives the code" rule holds only in the code phase (TASK-017).
    phase: str = "code"
    # The `pix_deposits` row this hold waits for, in the payment phase.
    charge_id: UUID | None = None

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
            attendee_name=row.attendee_name,
            phase=row.phase or "code",
            charge_id=row.charge_id,
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


def hold_lock_key(tenant_id: UUID, professional_id: UUID | None) -> str:
    """The advisory-lock key of ONE agenda (a clinic's, or one professional's)."""
    return f"booking_hold:{tenant_id}:{professional_id}"


async def _serialize_agenda(session, tenant_id: UUID, professional_id: UUID | None) -> None:
    """Postgres only: serialise concurrent `place_hold`s of the SAME agenda.

    The overlap test in `place_hold` is check-then-insert under READ COMMITTED, and there is no
    UNIQUE constraint to catch a loser (`professional_id` is nullable, so it cannot be one). Two
    patients confirming the same slot in the same instant could both pass the check and both be
    charged for it. A transaction-scoped advisory lock closes that window: the second transaction
    waits for the first to commit and then SEES its row. Released automatically at commit or
    rollback; a different professional's agenda uses a different key and never waits.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": hold_lock_key(tenant_id, professional_id)},
    )


class LiveChargeHoldError(Exception):
    """This conversation's own hold is the reservation behind a Pix that can still be paid.

    `place_hold` normally replaces a conversation's previous hold. Doing that while the charge
    is alive would free the slot the patient may be paying for, so it refuses (spec 4.2 item 1):
    the way out is to void the charge first (`booking_charge.abandon_charge`).
    """


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
    attendee_name: str | None = None,
    phase: str = "code",
    charge_id: UUID | None = None,
) -> HeldSlot | None:
    """Reserve [start_at, end_at) for this conversation. None = somebody else has it.

    `phase="payment"` + `charge_id` reserve the slot for a Pix (TASK-017): the TTL is then the
    clinic's deposit window instead of the 10-minute code clock. Raises `LiveChargeHoldError`
    when this conversation's own previous hold still backs a payable charge.

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
            await _serialize_agenda(session, tenant_id, professional_id)
            live_charge = await session.scalar(
                select(BookingHold.id).where(
                    BookingHold.conversation_id == conversation_id,
                    BookingHold.charge_id.is_not(None),
                    BookingHold.expires_at > now,
                )
            )
            if live_charge is not None:
                raise LiveChargeHoldError
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
                attendee_name=attendee_name,
                start_at=start_at,
                end_at=end_at,
                expires_at=expires_at,
                phase=phase,
                charge_id=charge_id,
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


async def extend_to_payment(
    hold_id: UUID, *, ttl_minutes: int, charge_id: UUID
) -> HeldSlot | None:
    """Turn a LIVE code-phase hold into the payment-phase hold of `charge_id`.

    The same row, so the slot is never free in between: `expires_at` moves to now + the
    clinic's TTL and `phase`/`charge_id` are set in ONE conditional UPDATE. None when the hold
    already expired or vanished (the caller then tries a fresh `place_hold`).
    """
    now = _now()
    async with async_session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(BookingHold)
                .where(BookingHold.id == hold_id, BookingHold.expires_at > now)
                .values(
                    phase="payment",
                    charge_id=charge_id,
                    expires_at=now + timedelta(minutes=ttl_minutes),
                )
                .execution_options(synchronize_session=False)
            )
            if not result.rowcount:
                return None
            row = await session.get(BookingHold, hold_id)
            await session.refresh(row)
            return HeldSlot.of(row)


async def live_hold_overlaps(
    session,
    tenant_id: UUID,
    start_at: datetime,
    end_at: datetime,
    *,
    professional_id: UUID | None | Literal["any"] = "any",
    exclude_charge_id: UUID | None = None,
) -> bool:
    """Is any live hold of this clinic on [start_at, end_at)?

    `professional_id="any"` (the default) ignores whose agenda a hold belongs to: the hub's
    manual booking has no professional to compare with, so any live hold blocks it. A concrete
    id (or None for the tenant-level agenda) compares like `place_hold` does.
    `exclude_charge_id` is the promotion's own hold: it must not block its own payment.
    """
    stmt = select(BookingHold.start_at, BookingHold.end_at).where(
        BookingHold.tenant_id == tenant_id, BookingHold.expires_at > _now()
    )
    if professional_id != "any":
        stmt = stmt.where(
            BookingHold.professional_id == professional_id
            if professional_id is not None
            else BookingHold.professional_id.is_(None)
        )
    if exclude_charge_id is not None:
        stmt = stmt.where(
            or_(BookingHold.charge_id.is_(None), BookingHold.charge_id != exclude_charge_id)
        )
    rows = (await session.execute(stmt)).all()
    return any(
        overlaps(start_at, end_at, _aware(other_start), _aware(other_end))
        for other_start, other_end in rows
    )


async def live_payment_holds(
    session, tenant_id: UUID, start: datetime, end: datetime
) -> list[BookingHold]:
    """The live payment-phase holds of a clinic that overlap [start, end): the agenda's
    "awaiting payment" list. Rows, not `HeldSlot`s, so the caller reads `charge_id` too."""
    rows = await session.scalars(
        select(BookingHold)
        .where(
            BookingHold.tenant_id == tenant_id,
            BookingHold.phase == "payment",
            BookingHold.expires_at > _now(),
            BookingHold.start_at < end,
            BookingHold.end_at > start,
        )
        .order_by(BookingHold.start_at)
    )
    return list(rows)


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
    "awaiting_payment"
                 - (`before` timing) the slot is held for the deposit window and a Pix
                   exists: `payment` carries what the caller sends. No event, no
                   appointment: they are created by the payment (`fulfillment.py`).
    "blocked"    - (`before` timing) a deposit is due but cannot be asked for. The booking is
                   NOT made and nothing is held; `blocked_reason` is symbolic.
    "needs_payer_data"
                 - (`before` timing) the provider will not create the charge without a payer
                   field it has not been given (decision D7): the CPF/CNPJ (Asaas) or the e-mail
                   (Mercado Pago). Nothing is held or charged; the router parks the conversation
                   in `AWAITING_PAYER_DATA`. `payer_field` is "document" or "email" and
                   `payer_provider` names the provider for the copy.
    """

    outcome: Literal[
        "commit", "held", "slot_taken", "awaiting_payment", "blocked", "needs_payer_data"
    ]
    email_masked: str | None = None
    hold_id: UUID | None = None
    payment: "PaymentStart | None" = None
    blocked_reason: str | None = None
    payer_field: str | None = None
    payer_provider: str | None = None
    # TASK-071 Plan 3 (ruling R5): set on a `commit` whose deposit was DROPPED because it failed
    # for a technical reason (a `FailureReason` value). The clinic was already told.
    deposit_waived_reason: str | None = None


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
        redis=None,
        waive_deposit: str | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._conversation_id = conversation_id
        self._patient_id = patient_id
        self._external_id = (external_id or "").strip()
        self._armed = bool(armed and tenant_id is not None and self._external_id)
        # Cache for the add-on entitlement lookup of `quote_deposit` (`before` timing); optional.
        self._redis = redis
        self._tenant: Tenant | None = None
        # TASK-071 Plan 3: a `FailureReason` value when the worker already knows the deposit
        # cannot be taken on this turn (the payer field could not be kept): the deposit step is
        # dropped and the booking goes on as if none were due (the clinic is told).
        self._waive_deposit = waive_deposit

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def deposit_capable(self) -> bool:
        """Whether `decide` may have something to say on this channel: the deposit gate
        (`before` timing) applies on WhatsApp too, so it does not depend on `armed`."""
        return self._tenant_id is not None and self._conversation_id is not None

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
        attendee_name: str | None = None,
    ) -> GateDecision:
        """Hold the slot and mail a code / ask for the Pix, or tell the router to commit."""
        if self._tenant_id is None:
            return GateDecision("commit")

        # `before` timing: a deposit gates the booking on EVERY channel. The quote is the first
        # thing decided (nothing is reserved yet); every clinic on `after` (all of them today)
        # leaves here with `quote is None` and takes the code unchanged from before.
        from secretaria.services.payments.deposit_failure_notice import (
            continues_without_deposit,
            failure_reason_for,
        )

        quote = await self._deposit_quote(
            appointment_type=appointment_type,
            professional_id=professional_id,
            insurance=insurance,
        )
        # (code, reason, stage) of a deposit that is dropped for a technical reason (ruling R6):
        # the booking then takes the NORMAL no-deposit path below and the clinic is told.
        waiver: tuple[str, FailureReason, str] | None = None
        if quote is not None and quote.blocked:
            if not continues_without_deposit(quote.reason):
                return await self._blocked(quote.reason)
            waiver = (quote.reason, failure_reason_for(quote.reason), "quote")
        elif quote is not None and quote.required:
            if self._waive_deposit is None:
                return await self._decide_required(
                    quote,
                    start_at=start_at,
                    end_at=end_at,
                    professional_id=professional_id,
                    appointment_type=appointment_type,
                    insurance=insurance,
                    attendee_name=attendee_name,
                )
            waiver = (
                "payer_data_unavailable",
                failure_reason_for(None, self._waive_deposit),
                "payer_data",
            )

        decision = await self._decide_without_deposit(
            start_at=start_at,
            end_at=end_at,
            professional_id=professional_id,
            appointment_type=appointment_type,
            insurance=insurance,
            attendee_name=attendee_name,
        )
        if waiver is None or decision.outcome != "commit":
            # A "held" slot is decided again once the code is verified (the deposit question is
            # asked there, `workers/shared/deposit_turn.py::_payment_after_code`); a slot taken
            # by somebody else books nothing. Only a real booking tells the clinic.
            return decision
        code, reason, stage = waiver
        return await self.continue_without_deposit(
            code=code,
            reason=reason,
            stage=stage,
            start_at=start_at,
            professional_id=professional_id,
            appointment_type=appointment_type,
            attendee_name=attendee_name,
            provider=(quote.provider if quote is not None else None),
        )

    async def _decide_without_deposit(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        professional_id: UUID | None,
        appointment_type: str | None,
        insurance: str | None,
        attendee_name: str | None,
    ) -> GateDecision:
        """The gate as it was before any deposit existed: Portal code hold, or commit."""
        if not self._armed:
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
                attendee_name=attendee_name,
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

    # ------------------------------------------------------------------
    # `before` timing (TASK-017)
    # ------------------------------------------------------------------

    async def _deposit_quote(
        self,
        *,
        appointment_type: str | None,
        professional_id: UUID | None,
        insurance: str | None,
    ):
        """The deposit decision for this booking, or None when the clinic is not on `before`.

        FAIL-OPEN until the clinic is known to be on `before` (the gate has always failed open:
        an outage must not stop an `after` clinic's agenda), FAIL-CLOSED afterwards: once a
        deposit may be due, a failure to decide is a `blocked`, never a free booking.
        """
        # TASK-052 (owner 2026-10-12): the confirmation of a "Remarcar" asks the payment provider
        # whether the released slot's PAID deposit may be reused - before any session is open.
        from secretaria.models.deposit_release_offer import CARRY_CHECK_REUSABLE
        from secretaria.services import release_carry_check, release_offer
        from secretaria.services.payments import deposit_gate

        carry_check = await release_carry_check.ensure_checked(self._conversation_id)

        on_before = False
        try:
            async with async_session_factory() as session:
                tenant = await session.get(Tenant, self._tenant_id)
                if tenant is None or not deposit_gate.timing_is_before(tenant):
                    return None
                on_before = True
                self._tenant = tenant
                patient_id = self._patient_id
                if patient_id is None and self._conversation_id is not None:
                    patient_id = await session.scalar(
                        select(Conversation.patient_id).where(
                            Conversation.id == self._conversation_id
                        )
                    )
                self._patient_id = patient_id
                # Reusable: the next booking of this conversation carries that deposit (no Pix).
                # Refunded / unknown: the normal quote below asks for a NEW Pix.
                if carry_check == CARRY_CHECK_REUSABLE:
                    return deposit_gate.DepositQuote(
                        "no_deposit", release_offer.REASON_CARRIED_OVER
                    )
                return await deposit_gate.quote_deposit(
                    session,
                    tenant,
                    patient_id=patient_id,
                    appointment_type=appointment_type,
                    professional_id=professional_id,
                    insurance=insurance,
                    redis=self._redis,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("booking_gate_quote_failed", error_type=type(exc).__name__)
            if not on_before:
                return None
            return deposit_gate.DepositQuote("blocked", "quote_failed")

    async def _blocked(
        self,
        code: str,
        *,
        reason: str | None = None,
        stage: str | None = None,
        status: int | None = None,
    ) -> GateDecision:
        """A deposit is due but cannot be asked for: the booking is not made and a human takes over.

        No clinic e-mail (owner answer 13: the only clinic e-mail is a paid deposit the patient
        cancelled); the log line above is the operational record. `code` is the symbolic outcome
        (it stays `GateDecision.blocked_reason`); `reason`/`stage`/`status` are the closed-
        vocabulary record of a failed charge, None when no charge was attempted."""
        logger.warning(
            "booking_gate_deposit_blocked",
            code=code,
            reason=reason,
            stage=stage,
            status=status,
            tenant_id=str(self._tenant_id),
            conversation_id=str(self._conversation_id),
        )
        return GateDecision("blocked", blocked_reason=code)

    async def continue_without_deposit(
        self,
        *,
        code: str,
        reason: FailureReason,
        stage: str,
        start_at: datetime,
        professional_id: UUID | None,
        appointment_type: str | None,
        attendee_name: str | None,
        provider: str | None = None,
        status: int | None = None,
    ) -> GateDecision:
        """Drop the deposit step and book (TASK-071 Plan 3): the clinic and the doctor are told
        (`notify_deposit_failure`, which also marks the payment account as failing and never
        raises) and the router commits the booking WITHOUT a deposit.

        The one seam every "the deposit cannot be taken, book anyway" decision of the gate goes
        through. `stage` is a fixed literal, `reason` a closed `FailureReason`: no provider text,
        no personal data in the log line."""
        from secretaria.services.payments import deposit_failure_notice

        logger.warning(
            "booking_gate_deposit_waived",
            code=code,
            reason=str(reason),
            stage=stage,
            status=status,
            tenant_id=str(self._tenant_id),
            conversation_id=str(self._conversation_id),
        )
        if self._tenant_id is not None and deposit_failure_notice.tells_the_clinic(stage):
            await deposit_failure_notice.notify_deposit_failure(
                tenant_id=self._tenant_id,
                patient_name=attendee_name or await self._patient_name(),
                service=appointment_type,
                start_at=start_at,
                reason=reason,
                stage=stage,
                professional_id=professional_id,
                provider=provider or self._provider_name(),
                redis=self._redis,
            )
        return GateDecision("commit", deposit_waived_reason=str(reason))

    def _provider_name(self) -> str:
        from secretaria.models.clinic_payment_account import PROVIDER_ASAAS

        return (getattr(self._tenant, "payment_provider", None) or PROVIDER_ASAAS)

    async def _patient_name(self) -> str | None:
        """The patient's name for the clinic's e-mail (never logged). Best effort."""
        from secretaria.models import Patient

        if self._patient_id is None:
            return None
        try:
            async with async_session_factory() as session:
                return await session.scalar(
                    select(Patient.name).where(Patient.id == self._patient_id)
                )
        except Exception as exc:  # noqa: BLE001 - a name is never worth a booking
            logger.warning("booking_gate_patient_name_failed", error_type=type(exc).__name__)
            return None

    async def _forget_payer_data(self) -> None:
        """The provider call that needed the data has been made: forget all of it (D7)."""
        from secretaria.services.payments import payer_data

        await payer_data.discard(
            self._redis, tenant_id=self._tenant_id, conversation_id=self._conversation_id
        )

    async def _decide_required(
        self,
        quote,
        *,
        start_at: datetime,
        end_at: datetime,
        professional_id: UUID | None,
        appointment_type: str | None,
        insurance: str | None,
        attendee_name: str | None,
    ) -> GateDecision:
        """A deposit is due. Portal first proves the address (decision D12: never charge an
        identity nobody proved), WhatsApp goes straight to the Pix."""
        held: HeldSlot | None = None
        if self._armed:
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
                    attendee_name=attendee_name,
                )
            except LiveChargeHoldError:
                return await self._blocked("charge_pending")
            if held is None:
                return GateDecision("slot_taken")
            try:
                result = await request_code(self._tenant_id, self._external_id)
            except Exception as exc:  # noqa: BLE001 - `request_code` fails closed by contract
                logger.warning("booking_gate_request_raised", error=str(exc))
                result = None
            if result is not None and result.outcome is RequestCodeOutcome.SENT:
                # The code comes first; the payment starts when it is verified
                # (`workers/tasks.py`), on this same hold, extended to the payment phase.
                return GateDecision("held", email_masked=result.email_masked, hold_id=held.id)
            # No code is coming (already verified, no address, brain-api down): the deposit is
            # itself the barrier, so the SAME hold moves straight to the payment phase.

        from secretaria.services.payments import booking_charge, payer_data

        # What the patient already gave this turn or the last one (encrypted, 15 minutes, this
        # conversation only). Nothing is the normal case.
        payer = await payer_data.recall_all(
            self._redis, tenant_id=self._tenant_id, conversation_id=self._conversation_id
        )
        try:
            payment = await booking_charge.begin_payment(
                tenant_id=self._tenant_id,
                conversation_id=self._conversation_id,
                patient_id=self._patient_id,
                quote=quote,
                professional_id=professional_id,
                appointment_type=(appointment_type or "Consulta"),
                insurance=insurance,
                attendee_name=attendee_name,
                start_at=start_at,
                end_at=end_at,
                existing_hold=held,
                payer_document=payer.document,
                payer_email=payer.email,
            )
        except booking_charge.ChargeStartError as exc:
            needed = payer_data.kind_for_code(exc.code)
            if needed is not None and not payer.has(needed):
                # The provider is the authority on what it needs (decision D7): nothing is held
                # (`begin_payment` released the hold) and nothing was charged, so whatever the
                # patient already gave stays (its TTL) for the answer that completes the request.
                return GateDecision(
                    "needs_payer_data", payer_field=needed, payer_provider=quote.provider
                )
            # The provider saw the data (or failed for another reason): forget it. A provider
            # that STILL asks for what it was given is never a second question.
            await self._forget_payer_data()
            from secretaria.services.payments.deposit_failure_notice import (
                continues_without_deposit,
                failure_reason_for,
            )

            if continues_without_deposit(exc.code, exc.reason):
                # TASK-071 Plan 3: a TECHNICAL failure never stops the booking. `begin_payment`
                # released the hold; the router's `commit` re-checks the slot before the event.
                return await self.continue_without_deposit(
                    code=exc.code,
                    reason=failure_reason_for(exc.code, exc.reason),
                    stage=exc.stage or "charge",
                    status=exc.status_code,
                    start_at=start_at,
                    professional_id=professional_id,
                    appointment_type=appointment_type,
                    attendee_name=attendee_name,
                    provider=quote.provider,
                )
            return await self._blocked(
                exc.code,
                reason=str(exc.reason) if exc.reason else None,
                stage=exc.stage,
                status=exc.status_code,
            )
        if payment is None:
            # No provider call was made: the data (if any) stays for the next try (its TTL).
            return GateDecision("slot_taken")
        await self._forget_payer_data()
        return GateDecision("awaiting_payment", hold_id=payment.hold_id, payment=payment)

async def appointment_stands_in(
    session, *, tenant_id: UUID, patient_id: UUID, conversation_id: UUID
) -> bool:
    """Whether a real appointment stands behind this conversation's code wait.

    The question every "sua consulta continua marcada" has to answer first
    (live test, 2026-10-05). Since the gate, a Portal code wait normally has
    NOTHING behind it but a ten-minute hold; an appointment exists only on the
    paths that still book before the code - a booking the gate stood down on
    (fail-open, see `BookingGate`) followed by the post-booking code notice,
    or a booking committed before the gate shipped.

    True when the patient has a live upcoming appointment AND, if this
    conversation still carries a hold row, one of those appointments sits on
    the held window: a hold row means the LATEST booking attempt was gated and
    never promoted (promotion drops the row), so an older appointment must not
    make the new, unconfirmed slot read as booked. Read on the caller's
    session, like `live_hold_in`.
    """
    upcoming = await load_upcoming_appointments(session, tenant_id, patient_id)
    if not upcoming:
        return False
    held = await latest_hold_in(session, conversation_id)
    if held is None:
        return True
    return any(
        isinstance(appt["start_at"], datetime) and _aware(appt["start_at"]) == held.start_at
        for appt in upcoming
    )
