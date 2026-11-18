"""old_slot - the OLD consultation's event while a change waits for its Pix (TASK-085 R).

3rd round, D-R10/D-R11 (tasks/TASK-085/sinal-trocas/SPEC.md §5, mapa-u.md §1.2-§1.5, §4.3). In a
clinic that books only AFTER the sinal, a change to a dearer service/unit/doctor creates a
`mode="suspend"` DepositAdjustment. The old consultation's ROW stays intact (status, slot, the paid
sinal still pointing at it), but its EVENT leaves the doctor's agenda (Google or the secretarIA's
own), so the old slot is free for other patients; only the NEW slot is held (a `booking_holds`
row of the adjustment). `deposit_adjustments.old_slot` says where that event is:

    NULL -> releasing (claimed by `vacate_old_slot`; the event may still exist)
         -> released  (deleted; the slot is free)
         -> restored  (re-created on the old slot: `kept_old`)
          | rebooked  (the row moved to a new slot: "Outro horário" or the hub)
          | replaced  (paid: an event was created on the new slot/agenda)

Event ids: Google never lets a deleted event id be used again, so every re-creation gets a NEW
deterministic id with a prefix of its own path (`difkeep` / `difpaid` / `difmove` / `difhub` + the
adjustment's uuid hex). Google's alphabet is base32hex (a-v, 0-9): every prefix stays inside it
(never w/x/y/z). The secretarIA's own agenda prefixes `loc_` (`LocalCalendarService` refuses any
other id) - the `release_keep.keep_event_id` / `booking_promotion.event_id_for` rule. A re-sent
id is a 409 that `create_event(event_id=)` reads as success, which is why no two paths may
share a prefix.

Rules every function here keeps (the `release_keep.accept_keep` pattern):

* no database transaction is open while an agenda answers (`_end_read` before every agenda call:
  Google answers in seconds or times out, and an open transaction there is an "idle in
  transaction" connection);
* lock order consultation -> adjustment (contrato.md §4), and every state change is a GUARDED
  UPDATE (`... WHERE status/old_slot = what was read RETURNING id`): exactly one caller wins;
* an event created and then not kept (the claim was lost) is deleted again (`discard_event`) -
  unless a twin call with the SAME deterministic id won, whose event it is;
* on the secretarIA's own agenda (and a person's agenda shared by two clinics) the create is
  exclusive (`create_event_exclusive`); on a plain Google agenda it is check-then-create, the
  same guarantee as every booking there.

U0 froze the surface; UA1 implemented it. LGPD: ids, instants and outcomes in logs only.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, PixDeposit, Tenant
from secretaria.models.appointment import LIVE_APPOINTMENT_STATUSES, is_live_status
from secretaria.models.deposit_adjustment import (
    MODE_SUSPEND,
    OLD_SLOT_REBOOKED,
    OLD_SLOT_RELEASED,
    OLD_SLOT_RELEASING,
    OLD_SLOT_REPLACED,
    OLD_SLOT_RESTORED,
    REBOOKABLE_STATUSES,
    RESTORABLE_STATUSES,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_KEPT_OLD,
    STATUS_OLD_SLOT_LOST,
    STATUS_REBOOKED,
    STATUS_RESTORING,
    SUSPENDED_STATUSES,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import (
    ATTENTION_OPEN,
    ATTENTION_RESOLVED,
    REASON_SERVICE_DIFF_REBOOK_DUE,
    PaymentAttentionItem,
)
from secretaria.models.pix_deposit import FULFILLMENT_NEEDS_ATTENTION, FULFILLMENT_RESOLVED
from secretaria.services import agenda, booking_hold, unit_booking
from secretaria.services.calendar import (
    CalendarSlotTakenError,
    CalendarUnavailableError,
    build_event_description,
)
from secretaria.services.local_calendar import LocalCalendarService, create_event_exclusive
from secretaria.services.payments import attention
from secretaria.services.payments.adjustment_suspension import occupies_slot_clause

logger = get_logger(__name__)

# A `releasing` claim older than this is retried by `retry_vacates` (the payments sweep, ~2 min).
RETRY_AFTER = timedelta(minutes=2)

RestoreOutcome = Literal[
    "restored", "slot_taken", "calendar_unavailable", "already_decided", "not_applicable"
]
# Who asks to bring the old consultation back: the patient's "Manter a antiga" tap, the silence
# sweep (D-R8), a change that expired without room for a choice, or a payment that could not
# apply the change (paid late / short / wrong reference / not applicable).
RestoreTrigger = Literal["tap", "silence", "no_room", "not_applied"]

# The change is still open and its old event may be CLAIMED (`old_slot` NULL -> releasing). A
# claim already made is finished in any suspended status (SUSPENDED_STATUSES): a change that went
# on to `restoring` / `old_slot_lost` still must not leave the old event on the agenda.
VACATE_STATUSES: tuple[str, ...] = (STATUS_AWAITING_PAYMENT, STATUS_AWAITING_CHOICE)
# `finish_rebook` closes the clinic's item with this note (the hub shows it in the history).
REBOOK_CLOSED_BY = "Sistema"
REBOOK_CLOSED_NOTE = {
    "patient": "O paciente escolheu outro horário para a consulta.",
    "hub": "A clínica remarcou a consulta.",
}

# One prefix per path (mapa-u §6 risk 4). All inside base32hex (a-v).
_PREFIX_RESTORE = "difkeep"
_PREFIX_PAID = "difpaid"
_PREFIX_REBOOK = {"patient": "difmove", "hub": "difhub"}


def _event_id(prefix: str, adjustment_id: UUID, calendar) -> str:
    local = "loc_" if isinstance(calendar, LocalCalendarService) else ""
    return f"{local}{prefix}{adjustment_id.hex}"


def restore_event_id(adjustment_id: UUID, *, calendar) -> str:
    """The event that puts the old consultation BACK on its slot: `difkeep<hex>`
    (`loc_difkeep<hex>` on the secretarIA's own agenda). Pure, deterministic."""
    return _event_id(_PREFIX_RESTORE, adjustment_id, calendar)


def paid_event_id(adjustment_id: UUID, *, calendar) -> str:
    """The event created on the NEW slot when the difference is paid after the old event was
    deleted: `difpaid<hex>` (`loc_` on the own agenda). Pure, deterministic."""
    return _event_id(_PREFIX_PAID, adjustment_id, calendar)


def rebook_event_id(
    adjustment_id: UUID, *, calendar, source: Literal["patient", "hub"]
) -> str:
    """The event of the old consultation moved to a NEW slot: `difmove<hex>` when the patient chose
    "Outro horário", `difhub<hex>` when the clinic rescheduled it (`loc_` on the own agenda).
    Distinct prefixes: the patient and the hub may both try. Pure; ValueError for another source."""
    prefix = _PREFIX_REBOOK.get(source)
    if prefix is None:
        raise ValueError(f"unknown rebook source: {source!r}")
    return _event_id(prefix, adjustment_id, calendar)


def rebook_item_key(provider: str, adjustment_id: UUID) -> str:
    """The dedupe key of the ONE `service_diff_rebook_due` item of a change (how it is found
    again: one sinal may have more than one change)."""
    return attention.attention_dedupe_key(
        provider, f"adjustment:{adjustment_id}", REASON_SERVICE_DIFF_REBOOK_DUE
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _now() -> datetime:
    """The wall clock (a seam: the tests pin it)."""
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _log(adjustment_id: UUID, tenant_id: UUID | None = None) -> dict:
    out = {"adjustment_id": str(adjustment_id)}
    if tenant_id is not None:
        out["tenant_id"] = str(tenant_id)
    return out


async def _end_read(session: AsyncSession) -> None:
    """End a READ-ONLY transaction before an agenda call (`release_keep._end_read`). With
    pending writes the transaction is left as it is (the caller owns it)."""
    if session.in_transaction() and not (session.new or session.dirty or session.deleted):
        await session.commit()


async def _calendar_of(
    session: AsyncSession, tenant: Tenant, owner: Any, unit_id: UUID | None
) -> Any | None:
    """The agenda that owns `owner`'s event (`service_change.calendar_for_appointment`: the
    doctor's own, else the clinic's), scoped to the unit (id and location of a new event).
    None when it cannot be built. Lazy import: `service_change` imports this module."""
    from secretaria.services.payments import service_change

    try:
        calendar = await service_change.calendar_for_appointment(session, tenant, owner)
        if calendar is None:
            return None
        return await unit_booking.calendar_at_unit(session, calendar, tenant.id, unit_id)
    except Exception as exc:  # noqa: BLE001 - no agenda: the caller retries or tells the clinic
        logger.warning("old_slot_calendar_failed", error_type=type(exc).__name__)
        return None


def _window(appointment: Appointment) -> tuple[datetime, datetime]:
    start = _utc(appointment.start_at)
    return start, _utc(appointment.end_at or appointment.start_at)


def _upcoming(appointment: Appointment | None, now: datetime) -> bool:
    return (
        appointment is not None
        and is_live_status(appointment.status)
        and appointment.start_at is not None
        and _utc(appointment.start_at) > _utc(now)
    )


async def _event_texts(
    session: AsyncSession, tenant: Tenant, appointment: Appointment, patient_name: str | None
) -> tuple[str, str]:
    """The event's title and description, as every change of a consultation writes them
    (`service_change.agenda_summary` / `build_event_description` + the unit's lines)."""
    from secretaria.services.payments import service_change

    description = build_event_description(
        service=appointment.appointment_type,
        insurance=appointment.insurance,
        attendee_name=appointment.attendee_name,
        **await unit_booking.booking_unit_fields(
            session, tenant.id, appointment.unit_id, appointment.professional_id
        ),
    )
    return service_change.agenda_summary(appointment, patient_name), description


async def _patient_name(session: AsyncSession, appointment: Appointment) -> str | None:
    if appointment.patient_id is None:
        return None
    patient = await session.get(Patient, appointment.patient_id)
    if patient is None or patient.tenant_id != appointment.tenant_id:
        return None
    return patient.name


async def _locked(
    session: AsyncSession, tenant_id: UUID, adjustment_id: UUID
) -> tuple[Appointment | None, DepositAdjustment | None]:
    """Lock order consultation -> adjustment (FOR UPDATE; a no-op on SQLite), both re-read."""
    adjustment = await session.scalar(
        select(DepositAdjustment)
        .where(DepositAdjustment.id == adjustment_id, DepositAdjustment.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if adjustment is None:
        return None, None
    appointment = None
    if adjustment.appointment_id is not None:
        appointment = await session.scalar(
            select(Appointment)
            .where(
                Appointment.id == adjustment.appointment_id,
                Appointment.tenant_id == tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    adjustment = await session.scalar(
        select(DepositAdjustment)
        .where(DepositAdjustment.id == adjustment_id, DepositAdjustment.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return appointment, adjustment


# --------------------------------------------------------------------------
# leaving the agenda
# --------------------------------------------------------------------------


async def _claim(
    session: AsyncSession, tenant_id: UUID, adjustment_id: UUID, *, now: datetime
) -> bool:
    """tx1: `old_slot` NULL -> `releasing` for an open suspended change of a live consultation.
    Commits (from here the old event belongs to this mechanism); rolls back when not won."""
    appointment, adjustment = await _locked(session, tenant_id, adjustment_id)
    if (
        adjustment is None
        or adjustment.mode != MODE_SUSPEND
        or adjustment.status not in VACATE_STATUSES
        or adjustment.old_slot is not None
        or appointment is None
        or not is_live_status(appointment.status)
    ):
        await session.rollback()
        return False
    claimed = await session.execute(
        update(DepositAdjustment)
        .where(
            DepositAdjustment.id == adjustment_id,
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.mode == MODE_SUSPEND,
            DepositAdjustment.status.in_(VACATE_STATUSES),
            DepositAdjustment.old_slot.is_(None),
        )
        .values(old_slot=OLD_SLOT_RELEASING, old_slot_at=now)
        .returning(DepositAdjustment.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.scalar_one_or_none() is None:
        await session.rollback()
        return False
    await session.commit()
    logger.info("old_slot_claimed", **_log(adjustment_id, tenant_id))
    return True


async def _release_claimed(
    session: AsyncSession, adjustment_id: UUID, *, now: datetime
) -> Literal["released", "releasing", "not_applicable"]:
    """Delete the old event of a `releasing` change (no transaction open meanwhile), then the
    guarded `releasing -> released`. Commits. "releasing" = try again later."""
    adjustment = await session.scalar(
        select(DepositAdjustment)
        .where(DepositAdjustment.id == adjustment_id)
        .execution_options(populate_existing=True)
    )
    if (
        adjustment is None
        or adjustment.mode != MODE_SUSPEND
        or adjustment.old_slot != OLD_SLOT_RELEASING
        or adjustment.status not in SUSPENDED_STATUSES
        or adjustment.appointment_id is None
    ):
        await session.rollback()
        return "not_applicable"
    tenant_id = adjustment.tenant_id
    log = _log(adjustment_id, tenant_id)
    tenant = await session.get(Tenant, tenant_id)
    appointment = await session.scalar(
        select(Appointment)
        .where(Appointment.id == adjustment.appointment_id, Appointment.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if tenant is None or appointment is None:
        await session.rollback()
        return "releasing"
    event_id = (appointment.google_event_id or "").strip()
    if event_id:
        calendar = await _calendar_of(session, tenant, appointment, appointment.unit_id)
        if calendar is None:
            await session.rollback()
            logger.warning("old_slot_vacate_no_calendar", **log)
            return "releasing"
        await _end_read(session)
        try:
            await calendar.cancel_event(event_id)  # False / 404 = already gone: fine
        except Exception as exc:  # noqa: BLE001 - the slot stays taken (safe); retried
            logger.warning("old_slot_vacate_failed", error_type=type(exc).__name__, **log)
            return "releasing"
    done = await session.execute(
        update(DepositAdjustment)
        .where(
            DepositAdjustment.id == adjustment_id,
            DepositAdjustment.old_slot == OLD_SLOT_RELEASING,
        )
        .values(old_slot=OLD_SLOT_RELEASED, old_slot_at=now)
        .returning(DepositAdjustment.id)
        .execution_options(synchronize_session=False)
    )
    released = done.scalar_one_or_none() is not None
    await session.commit()
    logger.info("old_slot_released", moved=released, **log)
    return "released" if released else "not_applicable"


async def vacate_old_slot(
    tenant_id: UUID, adjustment_id: UUID
) -> Literal["released", "releasing", "not_applicable"]:
    """Take the old consultation's event OFF the agenda, after the change's commit (mapa-u §1.4).

    Own session; never raises. tx1 locks consultation -> adjustment; only `mode="suspend"`,
    status in (`awaiting_payment`, `awaiting_choice`), a LIVE consultation and `old_slot IS NULL`
    -> `old_slot = 'releasing'`, `old_slot_at = now`, commit (from here the old event belongs to
    this mechanism). Network: `cancel_event(row.google_event_id)` on the consultation's calendar
    (False/404 = already gone). tx2: guarded `releasing -> released`. A failure leaves
    `releasing` (the slot stays taken - safe) for `retry_vacates`. `appointments.google_event_id`
    is NOT changed. "not_applicable" = nothing to do (another mode/state, already claimed, or the
    database failed before the claim - `retry_vacates` claims those later)."""
    claimed = False
    try:
        async with async_session_factory() as session:
            now = _now()
            claimed = await _claim(session, tenant_id, adjustment_id, now=now)
            if not claimed:
                return "not_applicable"
            # "not_applicable" here: the change moved on meanwhile (paid / restored / cancelled)
            # and its own path owns the old event now.
            return await _release_claimed(session, adjustment_id, now=now)
    except Exception as exc:  # noqa: BLE001 - best-effort after the commit; the sweep retries
        logger.warning(
            "old_slot_vacate_error",
            error_type=type(exc).__name__,
            **_log(adjustment_id, tenant_id),
        )
        return "releasing" if claimed else "not_applicable"


async def retry_vacates(session: AsyncSession, *, now: datetime, limit: int = 50) -> int:
    """Retry the deletions left `releasing`: `old_slot='releasing'`, status in
    SUSPENDED_STATUSES (a change that went on to `restoring`/`old_slot_lost` included),
    `old_slot_at <= now - RETRY_AFTER`. Also claims an
    open suspended change still at `old_slot` NULL older than RETRY_AFTER (the post-commit call
    is best-effort: a worker may die right after the commit). Commits per row; never raises;
    returns how many reached `released`. Run by `service_change.expire_adjustments`."""
    now = _utc(now)
    cutoff = now - RETRY_AFTER
    try:
        rows = (
            await session.execute(
                select(DepositAdjustment.id, DepositAdjustment.tenant_id)
                .where(
                    DepositAdjustment.mode == MODE_SUSPEND,
                    or_(
                        and_(
                            DepositAdjustment.status.in_(SUSPENDED_STATUSES),
                            DepositAdjustment.old_slot == OLD_SLOT_RELEASING,
                            or_(
                                DepositAdjustment.old_slot_at.is_(None),
                                DepositAdjustment.old_slot_at <= cutoff,
                            ),
                        ),
                        and_(
                            DepositAdjustment.status.in_(VACATE_STATUSES),
                            DepositAdjustment.old_slot.is_(None),
                            DepositAdjustment.created_at <= cutoff,
                        ),
                    ),
                )
                .order_by(DepositAdjustment.created_at)
                .limit(limit)
            )
        ).all()
        await session.rollback()
    except Exception as exc:  # noqa: BLE001 - the next tick tries again
        await session.rollback()
        logger.warning("old_slot_retry_query_failed", error_type=type(exc).__name__)
        return 0
    released = 0
    for adjustment_id, tenant_id in rows:
        try:
            await _claim(session, tenant_id, adjustment_id, now=now)
            if await _release_claimed(session, adjustment_id, now=now) == "released":
                released += 1
        except Exception as exc:  # noqa: BLE001 - one row never stops the batch
            await session.rollback()
            logger.warning(
                "old_slot_retry_failed",
                error_type=type(exc).__name__,
                **_log(adjustment_id, tenant_id),
            )
    if released:
        logger.info("old_slot_retry_released", count=released)
    return released


# --------------------------------------------------------------------------
# is the old slot still free?
# --------------------------------------------------------------------------


async def _other_consultation_occupies(
    session: AsyncSession, appointment: Appointment, start: datetime, end: datetime
) -> bool:
    """Another LIVE consultation of the same agenda (the doctor's, or the clinic's without one)
    that occupies its slot (`occupies_slot_clause`) overlaps [start, end)."""
    same_agenda = (
        Appointment.professional_id == appointment.professional_id
        if appointment.professional_id is not None
        else Appointment.professional_id.is_(None)
    )
    found = await session.scalar(
        select(Appointment.id)
        .where(
            Appointment.tenant_id == appointment.tenant_id,
            Appointment.id != appointment.id,
            Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
            Appointment.start_at < end,
            Appointment.end_at > start,
            same_agenda,
            occupies_slot_clause(),
        )
        .limit(1)
    )
    return found is not None


async def old_slot_free(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    adjustment: DepositAdjustment,
) -> bool | None:
    """Whether the old consultation's slot is still free to put it back. Read-only; None = the
    agenda could not be read. Checks: the agenda (ignoring `restore_event_id` and the old event
    itself - still there while `releasing`), payment holds (`exclude_adjustment_id=adjustment.id`),
    other consultations that occupy (`occupies_slot_clause`, same agenda, without this row) and
    `agenda.shared_busy`. Never holds a transaction open while reading the agenda."""
    if appointment.start_at is None:
        return False
    start, end = _window(appointment)
    tenant_id, professional_id = tenant.id, appointment.professional_id
    adjustment_id = adjustment.id
    calendar = await _calendar_of(session, tenant, appointment, appointment.unit_id)
    if calendar is None:
        return None
    ignored = {restore_event_id(adjustment_id, calendar=calendar)}
    old_event = (appointment.google_event_id or "").strip()
    if old_event:
        ignored.add(old_event)
    await _end_read(session)
    try:
        events = await calendar.check_availability(start, end)
    except Exception as exc:  # noqa: BLE001 - unreadable agenda: the caller decides
        logger.warning(
            "old_slot_free_unreadable",
            error_type=type(exc).__name__,
            **_log(adjustment_id, tenant_id),
        )
        return None
    # A sibling clinic's bare window has no id: it is somebody else's (the release_keep rule).
    if any(str(event.get("id")) not in ignored for event in events):
        return False
    if await booking_hold.live_hold_overlaps(
        session,
        tenant_id,
        start,
        end,
        professional_id=professional_id,
        exclude_adjustment_id=adjustment_id,
    ):
        return False
    if await _other_consultation_occupies(session, appointment, start, end):
        return False
    # TASK-083: the same doctor busy then at another clinic sharing his agenda (windows only).
    return not await agenda.shared_busy(session, professional_id, start, end)


# --------------------------------------------------------------------------
# coming back
# --------------------------------------------------------------------------


def _restorable(adjustment: DepositAdjustment, trigger: str, now: datetime) -> bool:
    if adjustment.mode != MODE_SUSPEND:
        return False
    if trigger == "tap":
        return (
            adjustment.status == STATUS_AWAITING_CHOICE
            and adjustment.expires_at is not None
            and _utc(now) < _utc(adjustment.expires_at)
        )
    return adjustment.status in RESTORABLE_STATUSES


# Internal: `old_slot` changed under us while the change is still restorable (start over once).
_MOVED = "moved"


async def _transition(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    adjustment_id: UUID,
    trigger: str,
    now: datetime,
    expect_old_slot: str | None,
    event: tuple[str, str | None] | None,
) -> str:
    """Under the locks: still restorable and `old_slot` as expected -> `kept_old` (+ `restored`
    and the row re-pointed when an event was created), the new hold released, commit."""
    appointment, adjustment = await _locked(session, tenant_id, adjustment_id)
    if adjustment is None or not _restorable(adjustment, trigger, now):
        await session.rollback()
        return "already_decided"
    if not _upcoming(appointment, now):
        await session.rollback()
        return "not_applicable"
    if adjustment.old_slot != expect_old_slot:
        await session.rollback()
        return _MOVED
    values: dict = {"status": STATUS_KEPT_OLD, "resolved_at": now}
    if event is not None:
        values.update(old_slot=OLD_SLOT_RESTORED, old_slot_at=now)
    same_old_slot = (
        DepositAdjustment.old_slot.is_(None)
        if expect_old_slot is None
        else DepositAdjustment.old_slot == expect_old_slot
    )
    claimed = await session.execute(
        update(DepositAdjustment)
        .where(
            DepositAdjustment.id == adjustment_id,
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.status == adjustment.status,
            same_old_slot,
        )
        .values(**values)
        .returning(DepositAdjustment.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.scalar_one_or_none() is None:
        await session.rollback()
        return "already_decided"
    if event is not None:
        appointment.google_event_id, appointment.google_event_link = event
    await booking_hold.release_difference_hold_in(session, adjustment_id)
    await session.commit()
    return "restored"


async def _discard_unless_kept(
    session: AsyncSession, calendar: Any, tenant_id: UUID, adjustment_id: UUID, event_id: str
) -> None:
    """Lost the claim: delete the event created - unless a twin call with the same
    deterministic id won and the consultation now points at it."""
    await session.rollback()
    adjustment = await session.scalar(
        select(DepositAdjustment)
        .where(DepositAdjustment.id == adjustment_id, DepositAdjustment.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    kept = False
    if adjustment is not None and adjustment.appointment_id is not None:
        current = await session.scalar(
            select(Appointment.google_event_id).where(
                Appointment.id == adjustment.appointment_id, Appointment.tenant_id == tenant_id
            )
        )
        kept = current == event_id
    await session.rollback()
    if kept:
        return
    logger.info("old_slot_restore_lost_race", **_log(adjustment_id, tenant_id))
    await discard_event(calendar, event_id)


async def _restore_once(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    adjustment_id: UUID,
    now: datetime,
    trigger: str,
) -> str:
    log = _log(adjustment_id, tenant_id)
    adjustment = await session.scalar(
        select(DepositAdjustment)
        .where(DepositAdjustment.id == adjustment_id, DepositAdjustment.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    if adjustment is None or adjustment.mode != MODE_SUSPEND:
        return "not_applicable"
    if not _restorable(adjustment, trigger, now):
        return "already_decided"
    appointment = None
    if adjustment.appointment_id is not None:
        appointment = await session.scalar(
            select(Appointment)
            .where(
                Appointment.id == adjustment.appointment_id,
                Appointment.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
        )
    if not _upcoming(appointment, now):
        return "not_applicable"
    state = adjustment.old_slot
    if state is None:
        # The event never left the agenda: only the transition.
        return await _transition(
            session, tenant_id=tenant_id, adjustment_id=adjustment_id, trigger=trigger, now=now,
            expect_old_slot=None, event=None,
        )
    if state not in (OLD_SLOT_RELEASING, OLD_SLOT_RELEASED):
        return "already_decided"

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return "not_applicable"
    calendar = await _calendar_of(session, tenant, appointment, appointment.unit_id)
    if calendar is None:
        return "calendar_unavailable"
    own = restore_event_id(adjustment_id, calendar=calendar)
    start, end = _window(appointment)
    summary, description = await _event_texts(
        session, tenant, appointment, await _patient_name(session, appointment)
    )
    old_event = (appointment.google_event_id or "").strip()

    if state == OLD_SLOT_RELEASING:
        # Never the "still there" shortcut: a vacate may be deleting the old event right now.
        # Delete it here (idempotent) and put a NEW event back, as for a released one.
        if old_event and old_event != own:
            await _end_read(session)
            try:
                await calendar.cancel_event(old_event)
            except Exception as exc:  # noqa: BLE001 - nothing changed; the caller retries
                logger.warning(
                    "old_slot_restore_delete_failed", error_type=type(exc).__name__, **log
                )
                return "calendar_unavailable"
        await session.execute(
            update(DepositAdjustment)
            .where(
                DepositAdjustment.id == adjustment_id,
                DepositAdjustment.old_slot == OLD_SLOT_RELEASING,
            )
            .values(old_slot=OLD_SLOT_RELEASED, old_slot_at=now)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        await session.refresh(adjustment)
        if adjustment.old_slot != OLD_SLOT_RELEASED:
            return _MOVED

    free = await old_slot_free(
        session, tenant=tenant, appointment=appointment, adjustment=adjustment
    )
    if free is None:
        return "calendar_unavailable"
    if not free:
        logger.info("old_slot_restore_taken", **log)
        return "slot_taken"
    await _end_read(session)
    try:
        created = await create_event_exclusive(
            calendar, start=start, end=end, summary=summary, description=description, event_id=own
        )
    except CalendarSlotTakenError:
        logger.info("old_slot_restore_taken_at_write", **log)
        return "slot_taken"
    except Exception as exc:  # noqa: BLE001 - nothing changed; the caller retries
        logger.warning("old_slot_restore_create_failed", error_type=type(exc).__name__, **log)
        return "calendar_unavailable"
    if str(created.get("status") or "") == "cancelled":
        # A dead event under our id: never point a consultation at it.
        logger.error("old_slot_restore_event_dead", **log)
        return "calendar_unavailable"
    event_id = str(created.get("id") or own)
    try:
        outcome = await _transition(
            session, tenant_id=tenant_id, adjustment_id=adjustment_id, trigger=trigger, now=now,
            expect_old_slot=OLD_SLOT_RELEASED, event=(event_id, created.get("htmlLink")),
        )
    except Exception:
        await _discard_unless_kept(session, calendar, tenant_id, adjustment_id, event_id)
        raise
    if outcome == "restored":
        logger.info("old_slot_restored", trigger=trigger, **log)
        return outcome
    await _discard_unless_kept(session, calendar, tenant_id, adjustment_id, event_id)
    return "already_decided" if outcome == _MOVED else outcome


async def restore_old_slot(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    adjustment_id: UUID,
    now: datetime,
    trigger: RestoreTrigger,
) -> RestoreOutcome:
    """Put the old consultation back as it was (D-R8/D-R11). COMMITS; never raises.

    "tap" requires `awaiting_choice` and `now < expires_at`; the other triggers require a status
    in RESTORABLE_STATUSES. Free -> create `restore_event_id` (`create_event_exclusive`, outside a
    transaction), then lock consultation -> adjustment, still restorable -> `kept_old`,
    `old_slot='restored'`, the row re-pointed, the new hold released, commit; lost the race ->
    delete the created event (unless a twin call with the same id won). `old_slot` NULL -> only
    the transition (the event never left). `releasing` -> the old event is deleted first
    (idempotent; a vacate may be deleting it right now) and `released`, then as released.
    "slot_taken" / "calendar_unavailable": nothing about the change moves (the caller decides
    `old_slot_lost` / `restoring`). A consultation no longer live or already started ->
    "not_applicable"; decided by someone else -> "already_decided". Any unexpected error ->
    "calendar_unavailable" (rolled back)."""
    now = _utc(now)
    try:
        for _attempt in range(2):
            outcome = await _restore_once(
                session, tenant_id=tenant_id, adjustment_id=adjustment_id, now=now,
                trigger=trigger,
            )
            if outcome != _MOVED:
                return outcome  # type: ignore[return-value]
        return "already_decided"
    except Exception as exc:  # noqa: BLE001 - never raises: the caller retries next tick
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001, S110 - the session is unusable anyway
            pass
        logger.warning(
            "old_slot_restore_failed",
            error_type=type(exc).__name__,
            trigger=trigger,
            **_log(adjustment_id, tenant_id),
        )
        return "calendar_unavailable"


# --------------------------------------------------------------------------
# the old slot was lost / the change ended without applying
# --------------------------------------------------------------------------


async def mark_old_slot_lost(
    session: AsyncSession, *, tenant: Tenant, adjustment: DepositAdjustment, now: datetime
) -> bool:
    """RESTORABLE -> `old_slot_lost` (guarded; still open: `resolved_at` stays NULL); releases
    the new hold; opens the attention item `service_diff_rebook_due` (dedupe
    `attention_dedupe_key(provider, f"adjustment:{id}", reason)`, value = the paid sinal, slot =
    the old one). Never commits. False = already decided."""
    from secretaria.services.payments.service_change import held_deposit_cents

    if adjustment.tenant_id != tenant.id:
        return False
    claimed = await session.execute(
        update(DepositAdjustment)
        .where(
            DepositAdjustment.id == adjustment.id,
            DepositAdjustment.tenant_id == tenant.id,
            DepositAdjustment.mode == MODE_SUSPEND,
            DepositAdjustment.status.in_(RESTORABLE_STATUSES),
        )
        .values(status=STATUS_OLD_SLOT_LOST)
        .returning(DepositAdjustment.id)
        .execution_options(synchronize_session=False)
    )
    won = claimed.scalar_one_or_none() is not None
    await session.refresh(adjustment)
    if not won:
        return False
    await booking_hold.release_difference_hold_in(session, adjustment.id)
    deposit = await session.get(PixDeposit, adjustment.deposit_id)
    appointment = (
        await session.get(Appointment, adjustment.appointment_id)
        if adjustment.appointment_id is not None
        else None
    )
    if deposit is not None and deposit.tenant_id == tenant.id:
        await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason=REASON_SERVICE_DIFF_REBOOK_DUE,
            dedupe_key=rebook_item_key(deposit.provider, adjustment.id),
            deposit=deposit,
            amount_cents=int(await held_deposit_cents(session, deposit)),
            slot_start=appointment.start_at if appointment is not None else None,
            slot_end=appointment.end_at if appointment is not None else None,
            service_label=adjustment.old_service,
        )
    await session.flush()
    logger.info("old_slot_lost", **_log(adjustment.id, tenant.id))
    return True


async def to_restoring(
    session: AsyncSession, *, adjustment: DepositAdjustment, from_status: str, now: datetime
) -> bool:
    """Guarded `from_status -> restoring` (the agenda was unreachable / the change ended without
    applying); the new hold released (no "Reenviar" from here). `from_status` must be
    `awaiting_payment` or `awaiting_choice`; still open (`resolved_at` stays NULL). Never
    commits. False = the row was no longer in `from_status`."""
    if from_status not in VACATE_STATUSES:
        return False
    claimed = await session.execute(
        update(DepositAdjustment)
        .where(
            DepositAdjustment.id == adjustment.id,
            DepositAdjustment.tenant_id == adjustment.tenant_id,
            DepositAdjustment.mode == MODE_SUSPEND,
            DepositAdjustment.status == from_status,
        )
        .values(status=STATUS_RESTORING)
        .returning(DepositAdjustment.id)
        .execution_options(synchronize_session=False)
    )
    won = claimed.scalar_one_or_none() is not None
    await session.refresh(adjustment)
    if won:
        await booking_hold.release_difference_hold_in(session, adjustment.id)
        logger.info("old_slot_restoring", from_status=from_status, **_log(adjustment.id))
    return won


# --------------------------------------------------------------------------
# "Outro horário" / the hub's reschedule
# --------------------------------------------------------------------------


async def rebookable(
    session: AsyncSession, *, tenant_id: UUID, appointment_id: UUID
) -> DepositAdjustment | None:
    """The change whose old consultation may be moved to a new slot now (status in
    REBOOKABLE_STATUSES, `mode="suspend"`, this clinic), newest first; None otherwise."""
    return await session.scalar(
        select(DepositAdjustment)
        .where(
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.appointment_id == appointment_id,
            DepositAdjustment.mode == MODE_SUSPEND,
            DepositAdjustment.status.in_(REBOOKABLE_STATUSES),
        )
        .order_by(DepositAdjustment.created_at.desc())
        .limit(1)
    )


async def finish_rebook(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    adjustment_id: UUID,
    appointment: Appointment,
    event_id: str,
    event_link: str | None,
    now: datetime,
    source: Literal["patient", "hub"],
) -> bool:
    """Close a rebook in the caller's transaction: guarded UPDATE REBOOKABLE -> `rebooked`
    (`resolved_at`), `old_slot='rebooked'`; writes `google_event_id`/link on the row (the caller
    writes start/end/status as in any reschedule); releases the hold; closes the rebook item
    (`resolution_action="rebooked"`; the sinal leaves "needs attention" when nothing else is
    open). Never commits. False = already decided (the caller rolls back and discards the
    event)."""
    if appointment.tenant_id != tenant_id or source not in REBOOK_CLOSED_NOTE:
        return False
    claimed = await session.execute(
        update(DepositAdjustment)
        .where(
            DepositAdjustment.id == adjustment_id,
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.appointment_id == appointment.id,
            DepositAdjustment.mode == MODE_SUSPEND,
            DepositAdjustment.status.in_(REBOOKABLE_STATUSES),
        )
        .values(
            status=STATUS_REBOOKED,
            resolved_at=now,
            old_slot=OLD_SLOT_REBOOKED,
            old_slot_at=now,
        )
        .returning(DepositAdjustment.deposit_id)
        .execution_options(synchronize_session=False)
    )
    deposit_id = claimed.scalar_one_or_none()
    if deposit_id is None:
        return False
    appointment.google_event_id = event_id
    appointment.google_event_link = event_link
    await booking_hold.release_difference_hold_in(session, adjustment_id)
    deposit = await session.get(PixDeposit, deposit_id)
    if deposit is not None and deposit.tenant_id == tenant_id:
        await session.execute(
            update(PaymentAttentionItem)
            .where(
                PaymentAttentionItem.tenant_id == tenant_id,
                PaymentAttentionItem.dedupe_key
                == rebook_item_key(deposit.provider, adjustment_id),
                PaymentAttentionItem.status == ATTENTION_OPEN,
            )
            .values(
                status=ATTENTION_RESOLVED,
                resolved_at=now,
                resolution_action="rebooked",
                resolution_note=REBOOK_CLOSED_NOTE[source],
                resolved_by_name=REBOOK_CLOSED_BY,
            )
            .execution_options(synchronize_session=False)
        )
        still_open = await session.scalar(
            select(func.count())
            .select_from(PaymentAttentionItem)
            .where(
                PaymentAttentionItem.tenant_id == tenant_id,
                PaymentAttentionItem.deposit_id == deposit.id,
                PaymentAttentionItem.status == ATTENTION_OPEN,
            )
        )
        if not still_open and deposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION:
            deposit.fulfillment_status = FULFILLMENT_RESOLVED
    await session.flush()
    logger.info("old_slot_rebooked", source=source, **_log(adjustment_id, tenant_id))
    return True


# --------------------------------------------------------------------------
# paid after the old event left
# --------------------------------------------------------------------------


async def place_new_event_after_payment(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    adjustment: DepositAdjustment,
    patient_name: str | None,
) -> None:
    """The difference was paid after the old event left the agenda (`old_slot` releasing or
    released): delete the old event if still `releasing` (idempotent), create `paid_event_id` on
    the new doctor's/unit's agenda (free there first; exclusive on the secretarIA's agenda),
    re-point `google_event_id`/link/`calendar_source`, `old_slot='replaced'`, commit. COMMITS;
    raises on failure (the caller opens `service_diff_agenda_pending`). Already `replaced` ->
    nothing (a repeated hook)."""
    state = adjustment.old_slot
    if state == OLD_SLOT_REPLACED:
        return
    if state not in (OLD_SLOT_RELEASING, OLD_SLOT_RELEASED):
        raise ValueError("old_slot_not_vacated")
    tenant_id, adjustment_id, appointment_id = tenant.id, adjustment.id, appointment.id
    expected = (adjustment.pending_edit or {}).get("expected") or {}
    old_professional = (
        _uuid_or_none(expected.get("professional_id"))
        if "professional_id" in expected
        else appointment.professional_id
    )
    old_owner = SimpleNamespace(
        professional_id=old_professional,
        google_calendar_source=appointment.google_calendar_source,
    )
    new_source = "professional" if appointment.professional_id is not None else "clinic"
    new_owner = SimpleNamespace(
        professional_id=appointment.professional_id, google_calendar_source=new_source
    )
    new_calendar = await _calendar_of(session, tenant, new_owner, appointment.unit_id)
    if new_calendar is None:
        raise LookupError("no_new_calendar")
    own = paid_event_id(adjustment_id, calendar=new_calendar)
    start, end = _window(appointment)
    summary, description = await _event_texts(session, tenant, appointment, patient_name)
    old_event = (appointment.google_event_id or "").strip()

    if state == OLD_SLOT_RELEASING and old_event and old_event != own:
        old_calendar = await _calendar_of(session, tenant, old_owner, None)
        if old_calendar is None:
            raise LookupError("no_old_calendar")
        await _end_read(session)
        await old_calendar.cancel_event(old_event)  # raises -> the clinic fixes the agenda
        await session.execute(
            update(DepositAdjustment)
            .where(
                DepositAdjustment.id == adjustment_id,
                DepositAdjustment.old_slot == OLD_SLOT_RELEASING,
            )
            .values(old_slot=OLD_SLOT_RELEASED, old_slot_at=_now())
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    await _end_read(session)
    events = await new_calendar.check_availability(start, end)
    if any(str(event.get("id")) != own for event in events):
        raise CalendarSlotTakenError("old_slot_paid_window_taken")
    created = await create_event_exclusive(
        new_calendar, start=start, end=end, summary=summary, description=description, event_id=own
    )
    if str(created.get("status") or "") == "cancelled":
        raise CalendarUnavailableError("event id is taken")
    new_event = str(created.get("id") or own)

    row = await session.scalar(
        select(Appointment)
        .where(Appointment.id == appointment_id, Appointment.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    claimed = await session.execute(
        update(DepositAdjustment)
        .where(
            DepositAdjustment.id == adjustment_id,
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.old_slot.in_((OLD_SLOT_RELEASING, OLD_SLOT_RELEASED)),
        )
        .values(old_slot=OLD_SLOT_REPLACED, old_slot_at=_now())
        .returning(DepositAdjustment.id)
        .execution_options(synchronize_session=False)
    )
    if row is None or claimed.scalar_one_or_none() is None:
        await session.rollback()
        await session.refresh(adjustment)
        if adjustment.old_slot == OLD_SLOT_REPLACED:
            return  # a repeated hook already placed it (same deterministic id)
        raise RuntimeError("old_slot_moved")
    row.google_event_id = new_event
    row.google_event_link = created.get("htmlLink")
    row.google_calendar_source = new_source
    await session.commit()
    await session.refresh(adjustment)
    logger.info("old_slot_replaced", **_log(adjustment_id, tenant_id))


async def discard_event(calendar, event_id: str) -> None:
    """Compensation: delete an event this module created and could not keep. Never raises."""
    if calendar is None or not (event_id or "").strip():
        return
    try:
        await calendar.cancel_event(event_id)
    except Exception as exc:  # noqa: BLE001 - logged; the event is ours and empty
        logger.error("old_slot_orphan_event", error_type=type(exc).__name__)


__all__ = [
    "REBOOK_CLOSED_BY",
    "REBOOK_CLOSED_NOTE",
    "RETRY_AFTER",
    "VACATE_STATUSES",
    "RestoreOutcome",
    "RestoreTrigger",
    "discard_event",
    "finish_rebook",
    "mark_old_slot_lost",
    "old_slot_free",
    "paid_event_id",
    "place_new_event_after_payment",
    "rebook_event_id",
    "rebook_item_key",
    "rebookable",
    "restore_event_id",
    "restore_old_slot",
    "retry_vacates",
    "to_restoring",
    "vacate_old_slot",
]
