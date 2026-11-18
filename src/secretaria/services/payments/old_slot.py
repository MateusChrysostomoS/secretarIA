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

U0 (contract slice) froze the surface below; UA1 implements it. Until then the bodies raise
NotImplementedError, except the pure id builders. LGPD: ids, instants and outcomes in logs only.
"""

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Appointment, Tenant
from secretaria.models.deposit_adjustment import DepositAdjustment
from secretaria.services.local_calendar import LocalCalendarService

# A `releasing` claim older than this is retried by `retry_vacates` (the payments sweep, ~2 min).
RETRY_AFTER = timedelta(minutes=2)

RestoreOutcome = Literal[
    "restored", "slot_taken", "calendar_unavailable", "already_decided", "not_applicable"
]
# Who asks to bring the old consultation back: the patient's "Manter a antiga" tap, the silence
# sweep (D-R8), a change that expired without room for a choice, or a payment that could not
# apply the change (paid late / short / wrong reference / not applicable).
RestoreTrigger = Literal["tap", "silence", "no_room", "not_applied"]

_UA1 = "TASK-085 R UA1"

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


async def vacate_old_slot(
    tenant_id: UUID, adjustment_id: UUID
) -> Literal["released", "releasing", "not_applicable"]:
    """Take the old consultation's event OFF the agenda, after the change's commit (mapa-u §1.4).

    Own session; never raises. tx1 locks consultation -> adjustment; only `mode="suspend"`,
    status in (`awaiting_payment`, `awaiting_choice`) and `old_slot IS NULL` -> `old_slot =
    'releasing'`, `old_slot_at = now`, commit (from here the old event belongs to this mechanism).
    Network: `cancel_event(row.google_event_id)` on the consultation's calendar (False/404 = already
    gone). tx2: guarded `releasing -> released`. A failure leaves `releasing` (the slot stays taken
    - safe) for `retry_vacates`. `appointments.google_event_id` is NOT changed.
    "not_applicable" = nothing to do (another mode/state, or already claimed)."""
    raise NotImplementedError(_UA1)


async def retry_vacates(session: AsyncSession, *, now: datetime, limit: int = 50) -> int:
    """Retry the deletions left `releasing`: `old_slot='releasing'`, status in
    (`awaiting_payment`, `awaiting_choice`), `old_slot_at <= now - RETRY_AFTER`. Commits per row;
    returns how many reached `released`. Run by `service_change.expire_adjustments`."""
    raise NotImplementedError(_UA1)


async def old_slot_free(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    adjustment: DepositAdjustment,
) -> bool | None:
    """Whether the old consultation's slot is still free to put it back. Read-only; None = the
    agenda could not be read. Checks: the agenda (ignoring `restore_event_id`), payment holds
    (`exclude_adjustment_id=adjustment.id`), other consultations that occupy
    (`occupies_slot_clause`, same agenda, without this row) and `agenda.shared_busy`. Never holds
    a transaction open while reading the agenda."""
    raise NotImplementedError(_UA1)


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
    delete the created event. `old_slot` NULL -> only the transition (the event never left);
    `releasing` with the old event still alive -> only the transition.
    "slot_taken" / "calendar_unavailable": NOTHING changes (the caller decides `old_slot_lost` /
    `restoring`). A consultation no longer live or already started -> "not_applicable"; decided
    by someone else -> "already_decided"."""
    raise NotImplementedError(_UA1)


async def mark_old_slot_lost(
    session: AsyncSession, *, tenant: Tenant, adjustment: DepositAdjustment, now: datetime
) -> bool:
    """RESTORABLE -> `old_slot_lost` (guarded); releases the new hold; opens the attention item
    `service_diff_rebook_due` (dedupe `attention_dedupe_key(provider, f"adjustment:{id}", reason)`,
    value = the paid sinal, slot = the old one). Never commits. False = already decided."""
    raise NotImplementedError(_UA1)


async def to_restoring(
    session: AsyncSession, *, adjustment: DepositAdjustment, from_status: str, now: datetime
) -> bool:
    """Guarded `from_status -> restoring` (the agenda was unreachable / the change ended without
    applying). Never commits. False = the row was no longer in `from_status`."""
    raise NotImplementedError(_UA1)


async def rebookable(
    session: AsyncSession, *, tenant_id: UUID, appointment_id: UUID
) -> DepositAdjustment | None:
    """The change whose old consultation may be moved to a new slot now (status in
    REBOOKABLE_STATUSES, `mode="suspend"`, this clinic), newest first; None otherwise."""
    raise NotImplementedError(_UA1)


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
    writes start/end/status as in any reschedule); releases the hold; closes the rebook item.
    Never commits. False = already decided (the caller rolls back and discards the event)."""
    raise NotImplementedError(_UA1)


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
    the new doctor's/unit's agenda, re-point `google_event_id`/link/`calendar_source`,
    `old_slot='replaced'`, commit. COMMITS; raises on failure (the caller opens
    `service_diff_agenda_pending`)."""
    raise NotImplementedError(_UA1)


async def discard_event(calendar, event_id: str) -> None:
    """Compensation: delete an event this module created and could not keep. Never raises."""
    raise NotImplementedError(_UA1)


__all__ = [
    "RETRY_AFTER",
    "RestoreOutcome",
    "RestoreTrigger",
    "discard_event",
    "finish_rebook",
    "mark_old_slot_lost",
    "old_slot_free",
    "paid_event_id",
    "place_new_event_after_payment",
    "rebook_event_id",
    "rebookable",
    "restore_event_id",
    "restore_old_slot",
    "retry_vacates",
    "to_restoring",
    "vacate_old_slot",
]
