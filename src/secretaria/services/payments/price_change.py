"""price_change - the clinic changes a price while consultations are already booked (TASK-085 R).

3rd round, D-R14 (tasks/TASK-085/sinal-trocas/SPEC.md §5, mapa-u.md §2, §4.5). After the clinic
edits a price (`api/hub/config.py`, `api/hub/professionals.py`, `api/hub/professional_units.py`),
`enqueue_sweep` schedules ONE job per clinic per 5-minute window; the job
(`workers/price_change.py`) calls `sweep_price_changes`, which finds the clinic's future live
consultations whose sinal the new price changes and e-mails each patient a link straight into the
Portal. The link is an `appointment_reminders` row of kind `price_change`
(`reminder_text.portal_conversation_link(tenant, row.id)`); opening it makes the FIRST message the
action (`open_price_notice` + `price_opening_messages`, sent by
`workers/shared/price_change_opening.py`):

* raise + sinal paid -> Pix of only the difference, `mode="applied"` in either timing (unpaid in
  time -> the consultation stands and the clinic gets "cobrar a diferença");
* drop + sinal paid -> asks whether they want the difference back (D-R2 limits);
* sinal not paid yet -> the open Pix is voided and a new one with the new amount goes out (D-R6).

The amount is computed when the link is OPENED (the price in force then), never stored at send
time. A WhatsApp patient or one without an e-mail gets no message: the clinic gets an item
`price_change_no_email`. Only price edits trigger it (the sinal percentage does not).

U0 (contract slice) froze the surface below; UB1 implements the sweep, UB2 the opening. Until then
the bodies raise NotImplementedError, except the pure helpers (the job id and the mail-cap key).
LGPD: ids and counts in logs only; the e-mail address is never logged nor used as a key.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Appointment, PixDeposit, Tenant
from secretaria.services.payments import notify

# Consultations starting sooner than this are left out (the difference is settled on the day).
PRICE_NOTICE_MIN_LEAD = timedelta(hours=3)
# At most this many price e-mails per patient (address) per local day - the Plano 3 mechanism
# (`deposit_failure_notice.within_daily_cap`). Over the cap: nothing is created; the next sweep
# tries.
PRICE_MAIL_DAILY_CAP = 3
# The transactional template (`services/email.py::_TEMPLATES`, added by UB1).
EMAIL_TEMPLATE = "price_change_patient"

# The arq job (`workers/price_change.py::sweep_price_changes`, registered by UB1 in
# `workers/arq_worker.py`), its delay and the window that folds several edits into one job.
SWEEP_JOB = "sweep_price_changes"
SWEEP_DEFER = timedelta(seconds=60)
SWEEP_BUCKET = timedelta(minutes=5)

_UB1 = "TASK-085 R UB1"
_UB2 = "TASK-085 R UB2"


def sweep_job_id(tenant_id: UUID, changed_at: datetime) -> str:
    """The arq `_job_id` of a clinic's sweep: `pricechg:{tenant}:{bucket}`, bucket = the 5-minute
    window (UTC epoch // 300) of `changed_at`. Several edits in one window enqueue ONE job (arq
    refuses a duplicate id); the job reads the prices in force when it runs. Pure; a naive
    `changed_at` is a ValueError (the window must not depend on the server's timezone)."""
    if changed_at.tzinfo is None:
        raise ValueError("changed_at must be timezone-aware")
    bucket = int(changed_at.timestamp()) // int(SWEEP_BUCKET.total_seconds())
    return f"pricechg:{tenant_id}:{bucket}"


def mail_cap_key(tenant_id: UUID, address: str, local_day: str) -> str:
    """The Redis counter of `PRICE_MAIL_DAILY_CAP`:
    `price_change_mail:{tenant}:{sha256(f"{tenant}:{address}")[:16]}:{YYYYMMDD}`. The address is
    normalized (trimmed, lower-case) and only its tenant-salted digest is kept - never the address
    (same rule as `deposit_failure_notice._counter_key`). `local_day` = the clinic's local date."""
    material = f"{tenant_id}:{address.strip().lower()}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:16]
    return f"price_change_mail:{tenant_id}:{digest}:{local_day}"


async def enqueue_sweep(arq_pool, tenant_id: UUID, *, changed_at: datetime) -> None:
    """After the commit of a price edit: `arq_pool.enqueue_job(SWEEP_JOB, str(tenant_id),
    changed_at.isoformat(), _job_id=sweep_job_id(...), _defer_by=SWEEP_DEFER)`. No pool -> a log
    line. Never raises (a lost sweep is caught by the next edit)."""
    raise NotImplementedError(_UB1)


async def sweep_price_changes(
    tenant_id: UUID, *, changed_at: datetime, now: datetime, redis
) -> int:
    """One clinic's sweep (mapa-u §2.2); returns how many notices were sent. Always scoped to the
    clinic, paginated; one consultation's error does not stop the batch.

    Candidates: LIVE `appointments` of the clinic with a patient, `start_at > now +
    PRICE_NOTICE_MIN_LEAD`, `visible_to_patient_clause()` (a suspended one is out), a PAID or
    AWAITING `pix_deposits` row created at or before `changed_at`, no open adjustment/charge, the
    clinic's sinal switched on; `price_notice_needed` decides. Idempotence: an active unanswered
    `price_change` row -> nothing; answered -> re-armed (`answered_at=NULL`, `sent_at=now`) and
    sent again; none -> created. Portal patient with an e-mail -> `EMAIL_TEMPLATE` with the link,
    within `mail_cap_key` / `PRICE_MAIL_DAILY_CAP` (over the cap or not sent -> the row is NOT
    created/re-armed). Otherwise -> item `price_change_no_email` (dedupe per consultation)."""
    raise NotImplementedError(_UB1)


async def price_notice_needed(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    now: datetime,
) -> bool:
    """Whether the price in force changes this consultation's sinal enough to tell the patient.
    Read-only. PAID -> `service_change.preview_service_change` on the row itself: True on
    `increase_charge` (>= R$ 10, with a connected payment account), `decrease_ask`, and
    `decrease_keep` with a difference >= R$ 10; False on `increase_free` / `no_change` /
    `blocked`. AWAITING -> `deposit_lifecycle.requote_amount_cents(...) != deposit.amount_cents`."""
    raise NotImplementedError(_UB1)


PriceOpeningKind = Literal[
    "pix",  # raise + paid: the Pix of the difference went out (mode="applied")
    "refund_question",  # drop + paid: "quer a diferença de volta?" (svcdiffyes|/svcdiffno|)
    "abated",  # drop + paid, under the limits: discounted on the day
    "requoted",  # not paid yet: the old Pix voided, a new one with the new amount
    "unchanged",  # the price is back to what was paid: nothing changes
    "pending",  # opened twice: an adjustment is already open - never a second charge
    "too_close",  # starts within CHOICE_MIN_LEAD: settled on the day
    "retry",  # the provider could not void/create right now: try again
    "not_found",  # not this patient's / clinic's row, or the consultation is gone/suspended
]


@dataclass(frozen=True)
class PriceOpening:
    """What `open_price_notice` decided; `price_opening_messages` turns it into text/buttons."""

    kind: PriceOpeningKind
    appointment_id: UUID | None = None
    adjustment_id: UUID | None = None
    diff_cents: int | None = None
    pix_copy_paste: str | None = None
    expires_at: datetime | None = None
    old_amount_cents: int | None = None


async def open_price_notice(
    session: AsyncSession,
    *,
    tenant: Tenant,
    reminder_id: UUID,
    patient_id: UUID,
    now: datetime,
) -> PriceOpening:
    """The patient opened the e-mail's link (mapa-u §2.3). COMMITS. Checks the row belongs to this
    clinic and patient, marks it answered (`answered_at`, `answer="opened"`), locks the deposit and
    decides WITH THE PRICE OF NOW: PAID -> `service_change.price_requote`; AWAITING ->
    `deposit_lifecycle.requote_awaiting` (voided -> after commit `recharge_after_edit(...,
    reason="price_change")`); an adjustment already open -> "pending"; start within
    `CHOICE_MIN_LEAD` -> "too_close"."""
    raise NotImplementedError(_UB2)


def price_opening_messages(opening: PriceOpening, *, tenant: Tenant) -> list[notify.Outgoing]:
    """The first message(s) of the conversation for `opening` (texts in mapa-u §4.6), delivered
    after the commit by `notify.deliver` on the patient's channel. Pure."""
    raise NotImplementedError(_UB2)


__all__ = [
    "EMAIL_TEMPLATE",
    "PRICE_MAIL_DAILY_CAP",
    "PRICE_NOTICE_MIN_LEAD",
    "SWEEP_BUCKET",
    "SWEEP_DEFER",
    "SWEEP_JOB",
    "PriceOpening",
    "PriceOpeningKind",
    "enqueue_sweep",
    "mail_cap_key",
    "open_price_notice",
    "price_notice_needed",
    "price_opening_messages",
    "sweep_job_id",
    "sweep_price_changes",
]
