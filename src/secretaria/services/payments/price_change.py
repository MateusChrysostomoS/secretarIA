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

U0 (contract slice) froze the surface below; UB1 implemented the sweep (`enqueue_sweep`,
`sweep_price_changes`, `price_notice_needed`); UB2 implements the opening (`open_price_notice`,
`price_opening_messages`, still NotImplementedError until then).
LGPD: ids and counts in logs only; the e-mail address is never logged nor used as a key.
"""

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from time import monotonic
from types import SimpleNamespace
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import LIVE_APPOINTMENT_STATUSES, Appointment, Patient, PixDeposit, Tenant
from secretaria.models.appointment_reminder import (
    REMINDER_CHANNEL_EMAIL,
    REMINDER_KIND_PRICE_CHANGE,
    REMINDER_STATUS_SENT,
    AppointmentReminder,
)
from secretaria.models.deposit_adjustment import (
    STATUS_ASKED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED_VOID_PENDING,
    STATUS_COLLECT_DUE,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import REASON_PRICE_CHANGE_NO_EMAIL
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import reminder_delivery
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome, send_transactional_email_result
from secretaria.services.payments import accounts, attention, notify, service_change
from secretaria.services.payments.adjustment_suspension import visible_to_patient_clause
from secretaria.services.payments.deposit_failure_notice import within_daily_cap
from secretaria.services.payments.deposit_lifecycle import (
    _resolve_service_and_price,
    requote_amount_cents,
)
from secretaria.services.payments.money import parse_brl_to_cents
from secretaria.services.payments.service_change_policy import MIN_DIFFERENCE_CENTS
from secretaria.services.reminder_text import portal_conversation_link

logger = get_logger(__name__)

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
    changed_at.isoformat(), _job_id=sweep_job_id(...), _defer_until=<end of the window> +
    SWEEP_DEFER)`. No pool -> a log line. Never raises (a lost sweep is caught by the next edit).

    UB1 decision (contract delta of mapa-u §2.2 `_defer_by=60s`): arq refuses a job id whose job
    OR RESULT still exists, so with `_defer_by` an edit later in the same 5-minute window, after
    the job already ran, would be dropped and never swept. Deferring until the window closed
    (+ SWEEP_DEFER) makes the one job of the window run after every edit of it committed."""
    log = {"tenant_id": str(tenant_id)}
    if arq_pool is None:
        logger.warning("price_change_sweep_not_enqueued", reason="no_pool", **log)
        return
    try:
        job_id = sweep_job_id(tenant_id, changed_at)
        bucket = int(SWEEP_BUCKET.total_seconds())
        window_end = datetime.fromtimestamp(
            (int(changed_at.timestamp()) // bucket + 1) * bucket, tz=UTC
        )
        await arq_pool.enqueue_job(
            SWEEP_JOB,
            str(tenant_id),
            changed_at.isoformat(),
            _job_id=job_id,
            _defer_until=window_end + SWEEP_DEFER,
        )
    except Exception as exc:  # noqa: BLE001 - the save already committed; never fail it
        logger.warning(
            "price_change_sweep_not_enqueued",
            reason="enqueue_failed",
            error_type=type(exc).__name__,
            **log,
        )
        return
    logger.info("price_change_sweep_enqueued", job_id=job_id, **log)


# Consultations per page of the sweep (keyset on start, id).
_PAGE_SIZE = 200
# An adjustment in one of these keeps the consultation out of the sweep: something is already
# waiting for the patient or the clinic (an unpaid difference, a question, a choice), a difference
# the clinic will COLLECT at the visit (a new notice would charge it a second time - anti-goal
# "cobrança em dobro"), or a cancelled change whose Pix void is pending. A SUSPENDED consultation
# (`restoring`, `old_slot_lost`, ...) is left out by `visible_to_patient_clause` itself. The next
# price edit picks the consultation up once that is settled (mapa-u §7 item 21).
_BLOCKING_ADJUSTMENT_STATUSES: tuple[str, ...] = (
    STATUS_AWAITING_PAYMENT,
    STATUS_ASKED,
    STATUS_AWAITING_CHOICE,
    STATUS_COLLECT_DUE,
    STATUS_CANCELLED_VOID_PENDING,
)
# Redis claim of one consultation while its notice is being sent (held until its row committed):
# two sweeps of the same clinic overlapping (two windows) never e-mail the same consultation twice.
_CLAIM_TTL_SECONDS = 600
_NO_LINK_LINE = "Entre no portal da clínica e abra a conversa."
_DEFAULT_TZ = "America/Sao_Paulo"

# Fix round 1 (review I3): a notice that was capped, not sent, or hit an error is tried again by a
# follow-up sweep of the clinic - capped: at `_NEXT_DAY_AT` of the clinic's next local day (the
# cap is per local day); otherwise `_RETRY_AFTER` later - at most `PRICE_NOTICE_MAX_ATTEMPTS`
# times per consultation version (a capped one counts once per local day). Then the clinic gets
# the item `price_change_no_email` ("fale com ele"), so nobody silently misses a price change.
PRICE_NOTICE_MAX_ATTEMPTS = 3
_RETRY_AFTER = timedelta(minutes=30)
_NEXT_DAY_AT = time(0, 5)
_TRIES_TTL_SECONDS = 14 * 24 * 3600
# Review m2: a sweep stops itself after this long and resumes in a follow-up job (the arq timeout
# of the job, `workers/price_change.JOB_TIMEOUT`, sits above it). Measured on the monotonic clock.
_SWEEP_BUDGET = timedelta(minutes=25)
_RETRY_OUTCOMES = frozenset({"capped", "mail_failed", "error"})


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _zone(tenant: Tenant) -> ZoneInfo:
    try:
        return ZoneInfo(tenant.timezone or _DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(_DEFAULT_TZ)


def _version(start_at: datetime) -> int:
    """The consultation version a notice/attempt/item belongs to: its start (epoch seconds)."""
    return int(_as_utc(start_at).timestamp())


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
    created/re-armed). Otherwise -> item `price_change_no_email` (dedupe per consultation
    version).

    The e-mail goes out BEFORE its row is written (the row id is chosen first and travels in the
    link): a crash in between leaves a dead link (the opening falls back to the common one) and
    the next sweep sends a working one - never a pending row whose e-mail was never sent, which
    would silence the patient for good.

    Fix round 1: capped / not sent / error -> a follow-up sweep is enqueued on `redis` (the
    worker's ArqRedis) under its own job id, bounded by `PRICE_NOTICE_MAX_ATTEMPTS` per
    consultation version, then the clinic item; a sweep past `_SWEEP_BUDGET` stops and resumes in
    a follow-up job. Never raises."""
    log = {"tenant_id": str(tenant_id)}
    counts: dict[str, int] = {}
    retry_soon = next_day = resume = False
    zone = ZoneInfo(_DEFAULT_TZ)
    deadline = monotonic() + _SWEEP_BUDGET.total_seconds()
    processed = 0
    try:
        async with async_session_factory() as session:
            tenant = await session.get(Tenant, tenant_id)
            enabled = tenant is not None and bool(tenant.pix_deposit_enabled)
            if tenant is not None:
                zone = _zone(tenant)
        if not enabled:
            logger.info("price_change_sweep_skipped", reason="no_sinal", **log)
            return 0
        local_day = _as_utc(now).astimezone(zone).strftime("%Y%m%d")
        after: tuple[UUID, datetime] | None = None
        while not resume:
            async with async_session_factory() as session:
                page = await _candidate_page(
                    session, tenant_id=tenant_id, changed_at=changed_at, now=now, after=after
                )
            for appointment_id, start in page:
                if processed and monotonic() >= deadline:
                    resume = True  # out of time: a follow-up job goes on from here
                    break
                processed += 1
                try:
                    outcome = await _notify_one(tenant_id, appointment_id, now=now, redis=redis)
                except Exception as exc:  # noqa: BLE001 - one consultation never stops the batch
                    outcome = "error"
                    logger.warning(
                        "price_change_notice_failed",
                        appointment_id=str(appointment_id),
                        error_type=type(exc).__name__,
                        **log,
                    )
                if outcome in _RETRY_OUTCOMES and redis is not None:
                    if await _attempts_exhausted(
                        redis,
                        tenant_id,
                        appointment_id,
                        start,
                        local_day=local_day,
                        capped=outcome == "capped",
                    ):
                        await _open_mail_failed_item(tenant_id, appointment_id)
                        outcome = "item_mail_failed"
                    elif outcome == "capped":
                        next_day = True
                    else:
                        retry_soon = True
                counts[outcome] = counts.get(outcome, 0) + 1
            if len(page) < _PAGE_SIZE:
                break
            after = page[-1]
    except Exception as exc:  # noqa: BLE001 - a job: log and let the next edit sweep again
        logger.warning("price_change_sweep_failed", error_type=type(exc).__name__, **log)
    await _follow_ups(
        redis,
        tenant_id,
        changed_at,
        now=now,
        zone=zone,
        resume=resume,
        retry_soon=retry_soon,
        next_day=next_day,
    )
    logger.info("price_change_sweep_done", **counts, **log)
    return counts.get("sent", 0)


async def _follow_ups(
    redis,
    tenant_id: UUID,
    changed_at: datetime,
    *,
    now: datetime,
    zone: ZoneInfo,
    resume: bool,
    retry_soon: bool,
    next_day: bool,
) -> None:
    """Enqueue the follow-up sweeps this run needs, each under its own job id (never the hub's
    `sweep_job_id`, so a later price edit is not refused as a duplicate): `resume` (out of time)
    right away, `retry` (~30 min) for failures, `nextday` (00:05 local) for capped notices. The
    job keeps the first edit's `changed_at`, so the same consultations qualify."""
    now = _as_utc(now)
    plans: list[tuple[str, datetime]] = []
    if resume:
        plans.append((f"resume:{int(now.timestamp()) // 60}", now + SWEEP_DEFER))
    if retry_soon:
        run_at = now + _RETRY_AFTER
        bucket = int(run_at.timestamp()) // int(_RETRY_AFTER.total_seconds())
        plans.append((f"retry:{bucket}", run_at))
    if next_day:
        tomorrow = now.astimezone(zone).date() + timedelta(days=1)
        run_at = datetime.combine(tomorrow, _NEXT_DAY_AT, tzinfo=zone).astimezone(UTC)
        plans.append((f"nextday:{tomorrow.strftime('%Y%m%d')}", run_at))
    if not plans:
        return
    enqueue = getattr(redis, "enqueue_job", None)
    if enqueue is None:
        logger.warning(
            "price_change_follow_up_not_enqueued", reason="no_pool", tenant_id=str(tenant_id)
        )
        return
    for suffix, run_at in plans:
        job_id = f"pricechg:{tenant_id}:{suffix}"
        try:
            await enqueue(
                SWEEP_JOB,
                str(tenant_id),
                changed_at.isoformat(),
                _job_id=job_id,
                _defer_until=run_at,
            )
        except Exception as exc:  # noqa: BLE001 - logged; the next price edit sweeps again
            logger.warning(
                "price_change_follow_up_not_enqueued",
                reason="enqueue_failed",
                error_type=type(exc).__name__,
                tenant_id=str(tenant_id),
            )
            continue
        logger.info("price_change_follow_up_enqueued", job_id=job_id, tenant_id=str(tenant_id))


def _tries_key(tenant_id: UUID, appointment_id: UUID, start: datetime) -> str:
    return f"price_change_tries:{tenant_id}:{appointment_id}:{_version(start)}"


async def _attempts_exhausted(
    redis,
    tenant_id: UUID,
    appointment_id: UUID,
    start: datetime,
    *,
    local_day: str,
    capped: bool,
) -> bool:
    """Count one failed attempt of this consultation version; True once it reached
    `PRICE_NOTICE_MAX_ATTEMPTS`. A capped notice counts at most once per local day (a 30-minute
    retry the same day must not burn its attempts). Redis down -> False (logged)."""
    key = _tries_key(tenant_id, appointment_id, start)
    try:
        if capped:
            mark = f"price_change_capday:{tenant_id}:{appointment_id}:{_version(start)}:{local_day}"
            if not await redis.set(mark, "1", ex=2 * 24 * 3600, nx=True):
                return False
        count = await redis.incr(key)
        await redis.expire(key, _TRIES_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - no counter: the follow-up still runs
        logger.warning(
            "price_change_attempt_count_failed",
            error_type=type(exc).__name__,
            tenant_id=str(tenant_id),
        )
        return False
    return int(count) >= PRICE_NOTICE_MAX_ATTEMPTS


async def _candidate_page(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    changed_at: datetime,
    now: datetime,
    after: tuple[UUID, datetime] | None,
) -> list[tuple[UUID, datetime]]:
    """(appointment id, start) of the next page of candidates, ordered by (start, id)."""
    blocked = exists(
        select(DepositAdjustment.id).where(
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.deposit_id == PixDeposit.id,
            DepositAdjustment.status.in_(_BLOCKING_ADJUSTMENT_STATUSES),
        )
    )
    query = (
        select(Appointment.id, Appointment.start_at)
        .join(
            PixDeposit,
            and_(PixDeposit.appointment_id == Appointment.id, PixDeposit.tenant_id == tenant_id),
        )
        .where(
            Appointment.tenant_id == tenant_id,
            Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
            Appointment.patient_id.is_not(None),
            Appointment.start_at > now + PRICE_NOTICE_MIN_LEAD,
            visible_to_patient_clause(),
            PixDeposit.status.in_((PixDepositStatus.PAID, PixDepositStatus.AWAITING)),
            PixDeposit.created_at <= changed_at,
            ~blocked,
        )
        .order_by(Appointment.start_at, Appointment.id)
        .limit(_PAGE_SIZE)
    )
    if after is not None:
        last_id, start = after
        query = query.where(
            or_(
                Appointment.start_at > start,
                and_(Appointment.start_at == start, Appointment.id > last_id),
            )
        )
    return [(row.id, row.start_at) for row in await session.execute(query)]


@dataclass(frozen=True)
class _Notice:
    """What one notice needs once the read transaction is over (plain values, no ORM rows)."""

    tenant_id: UUID
    appointment_id: UUID
    patient_id: UUID
    start_at: datetime
    end_at: datetime | None
    service: str
    clinic_name: str
    zone: ZoneInfo
    provider: str
    portal: bool
    external_id: str | None
    stored_email: str | None
    rearm_id: UUID | None


async def _notify_one(tenant_id: UUID, appointment_id: UUID, *, now: datetime, redis) -> str:
    async with async_session_factory() as session:
        notice = await _read_notice(session, tenant_id, appointment_id, now=now)
        if isinstance(notice, str):
            return notice
        if not notice.portal:
            await _open_no_email_item(session, notice)
            return "item"
    # The read transaction is over before any network call (brain-api, Redis, SMTP).
    email = await _patient_email(notice)
    if not email:
        async with async_session_factory() as session:
            await _open_no_email_item(session, notice)
        return "item"
    if redis is None:
        logger.warning(
            "price_change_mail_skipped",
            reason="no_redis",
            tenant_id=str(tenant_id),
            appointment_id=str(appointment_id),
        )
        return "mail_failed"
    claim = f"price_change_claim:{tenant_id}:{appointment_id}"
    if not await redis.set(claim, "1", ex=_CLAIM_TTL_SECONDS, nx=True):
        return "busy"
    try:
        # Review I1: what was read before the claim may be stale (another sweep may have mailed
        # and committed meanwhile) - decide again under the claim, which is held until the row
        # is committed.
        current = await _recheck_under_claim(notice)
        if isinstance(current, str):
            return current
        return await _mail_and_record(current, email, now=now, redis=redis)
    finally:
        try:
            await redis.delete(claim)
        except Exception:  # noqa: BLE001 - the claim expires on its own
            logger.warning("price_change_claim_release_failed", tenant_id=str(tenant_id))


async def _recheck_under_claim(notice: _Notice) -> _Notice | str:
    """A short read under the claim: the consultation still at the same version, and its notice
    row as it is NOW (pending -> "pending"; answered -> re-arm that row; none -> a new one)."""
    async with async_session_factory() as session:
        appointment = await session.scalar(
            select(Appointment).where(
                Appointment.id == notice.appointment_id,
                Appointment.tenant_id == notice.tenant_id,
            )
        )
        if (
            appointment is None
            or appointment.status not in LIVE_APPOINTMENT_STATUSES
            or appointment.start_at is None
            or _as_utc(appointment.start_at) != notice.start_at
        ):
            return "skipped"
        current = await _current_notice(session, appointment)
    if current is not None and current.answered_at is None:
        return "pending"
    return replace(notice, rearm_id=current.id if current is not None else None)


async def _read_notice(
    session: AsyncSession, tenant_id: UUID, appointment_id: UUID, *, now: datetime
) -> _Notice | str:
    tenant = await session.get(Tenant, tenant_id)
    appointment = await session.scalar(
        select(Appointment).where(
            Appointment.id == appointment_id, Appointment.tenant_id == tenant_id
        )
    )
    if tenant is None or appointment is None or appointment.patient_id is None:
        return "skipped"
    deposit = await session.scalar(
        select(PixDeposit).where(
            PixDeposit.tenant_id == tenant_id, PixDeposit.appointment_id == appointment.id
        )
    )
    patient = await session.get(Patient, appointment.patient_id)
    if deposit is None or patient is None or patient.tenant_id != tenant_id:
        return "skipped"
    if not await price_notice_needed(
        session, tenant=tenant, appointment=appointment, deposit=deposit, now=now
    ):
        return "unneeded"
    current = await _current_notice(session, appointment)
    if current is not None and current.answered_at is None:
        return "pending"  # one pending notice is enough: the amount is computed when opened
    return _Notice(
        tenant_id=tenant_id,
        appointment_id=appointment.id,
        patient_id=patient.id,
        start_at=_as_utc(appointment.start_at),
        end_at=_as_utc(appointment.end_at) if appointment.end_at else None,
        service=appointment.appointment_type or "",
        clinic_name=tenant.clinic_name,
        zone=_zone(tenant),
        provider=deposit.provider or tenant.payment_provider or "asaas",
        portal=patient.channel == CHANNEL_BRAIN_MESSAGE,
        external_id=patient.external_id,
        stored_email=patient.email,
        rearm_id=current.id if current is not None else None,
    )


async def _current_notice(
    session: AsyncSession, appointment: Appointment
) -> AppointmentReminder | None:
    """The active `price_change` row of the consultation's CURRENT start (its version), or None.
    A row of an older start was invalidated by the reschedule (`cancel_reminders`)."""
    start = _as_utc(appointment.start_at)
    rows = await session.scalars(
        select(AppointmentReminder).where(
            AppointmentReminder.tenant_id == appointment.tenant_id,
            AppointmentReminder.appointment_id == appointment.id,
            AppointmentReminder.kind == REMINDER_KIND_PRICE_CHANGE,
            AppointmentReminder.invalidated_at.is_(None),
        )
    )
    return next((row for row in rows if _as_utc(row.appointment_start_at) == start), None)


async def _patient_email(notice: _Notice) -> str | None:
    """The Portal patient's address, by the reminder e-mail's own rule
    (`reminder_delivery._portal_email`: brain-api first, the stored copy only when it is down).
    It only reads `job.tenant.id` and `job.patient.{email,external_id}`, so a namespace stands in
    for the `ReminderJob` it is typed for."""
    lookup = SimpleNamespace(
        tenant=SimpleNamespace(id=notice.tenant_id),
        patient=SimpleNamespace(email=notice.stored_email, external_id=notice.external_id),
    )
    email = await reminder_delivery._portal_email(lookup)  # noqa: SLF001 - one rule, one place
    return (email or "").strip() or None


async def _mail_and_record(notice: _Notice, email: str, *, now: datetime, redis) -> str:
    log = {"tenant_id": str(notice.tenant_id), "appointment_id": str(notice.appointment_id)}
    local_day = _as_utc(now).astimezone(notice.zone).strftime("%Y%m%d")
    try:
        within = await within_daily_cap(
            redis, mail_cap_key(notice.tenant_id, email, local_day), PRICE_MAIL_DAILY_CAP
        )
    except Exception as exc:  # noqa: BLE001 - no counter, no e-mail (the follow-up tries)
        logger.warning(
            "price_change_mail_skipped",
            reason="no_counter",
            error_type=type(exc).__name__,
            **log,
        )
        return "mail_failed"
    if not within:
        logger.info("price_change_mail_capped", **log)
        return "capped"

    row_id = notice.rearm_id or uuid4()
    link = portal_conversation_link(notice.tenant_id, row_id)
    variables = {
        "clinic_name": notice.clinic_name,
        "service": notice.service,
        "when": notice.start_at.astimezone(notice.zone).strftime("%d/%m/%Y às %H:%M"),
        "link_line": link or _NO_LINK_LINE,
    }
    outcome = await send_transactional_email_result(
        to=email, template=EMAIL_TEMPLATE, variables=variables
    )
    if outcome is not EmailOutcome.SENT:
        logger.info("price_change_mail_not_sent", outcome=outcome.value, **log)
        return "mail_failed"

    async with async_session_factory() as session:
        try:
            if notice.rearm_id is not None:
                await session.execute(
                    update(AppointmentReminder)
                    .where(
                        AppointmentReminder.id == notice.rearm_id,
                        AppointmentReminder.tenant_id == notice.tenant_id,
                    )
                    .values(
                        status=REMINDER_STATUS_SENT,
                        answered_at=None,
                        answer=None,
                        sent_at=now,
                        due_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
            else:
                session.add(
                    AppointmentReminder(
                        id=row_id,
                        tenant_id=notice.tenant_id,
                        appointment_id=notice.appointment_id,
                        patient_id=notice.patient_id,
                        kind=REMINDER_KIND_PRICE_CHANGE,
                        appointment_start_at=notice.start_at,
                        due_at=now,
                        # Born sent: the reminder engine only claims `pending` rows (U0 warning).
                        status=REMINDER_STATUS_SENT,
                        channel=REMINDER_CHANNEL_EMAIL,
                        with_prompt=False,
                        sent_at=now,
                        warn_due_at=None,
                    )
                )
            await session.commit()
        except IntegrityError:
            # Another row of this consultation-version won meanwhile (only possible outside the
            # claim, e.g. Redis was flushed): its own e-mail stands; this one is not "sent".
            await session.rollback()
            logger.warning("price_change_notice_row_lost", **log)
            return "row_lost"
    try:
        await redis.delete(_tries_key(notice.tenant_id, notice.appointment_id, notice.start_at))
    except Exception:  # noqa: BLE001 - the counter expires on its own
        logger.warning("price_change_attempt_reset_failed", **log)
    logger.info(
        "price_change_notice_sent",
        rearmed=notice.rearm_id is not None,
        reminder_id=str(row_id),
        **log,
    )
    return "sent"


async def _open_item(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    appointment_id: UUID,
    patient_id: UUID | None,
    provider: str,
    start_at: datetime,
    end_at: datetime | None,
    service: str | None,
    reference: str,
) -> None:
    """The clinic item `price_change_no_email` for one consultation VERSION (`reference` carries
    the consultation id and its start; a replayed sweep never duplicates it, a rescheduled
    consultation gets its own). Not linked to the deposit (nothing is wrong with the money
    itself). Commits."""
    await attention.open_attention_item(
        session,
        tenant_id=tenant_id,
        reason=REASON_PRICE_CHANGE_NO_EMAIL,
        dedupe_key=attention.attention_dedupe_key(
            provider,
            f"{reference}:{appointment_id}:{_version(start_at)}",
            REASON_PRICE_CHANGE_NO_EMAIL,
        ),
        provider=provider,
        patient_id=patient_id,
        slot_start=start_at,
        slot_end=end_at,
        service_label=service or None,
    )
    await session.commit()


async def _open_no_email_item(session: AsyncSession, notice: _Notice) -> None:
    """WhatsApp patient or no e-mail on file: the clinic tells the patient itself."""
    await _open_item(
        session,
        tenant_id=notice.tenant_id,
        appointment_id=notice.appointment_id,
        patient_id=notice.patient_id,
        provider=notice.provider,
        start_at=notice.start_at,
        end_at=notice.end_at,
        service=notice.service,
        reference="price_change",
    )


async def _open_mail_failed_item(tenant_id: UUID, appointment_id: UUID) -> None:
    """`PRICE_NOTICE_MAX_ATTEMPTS` attempts failed (capped / not sent / error): the clinic gets
    the same reason `price_change_no_email` - the patient could not be told by e-mail - under its
    own reference `price_change_mail` (the item has no free-text field; the key tells the two
    cases apart). Reads only the consultation, its deposit's provider and the patient id - never
    `price_notice_needed`, which may be what failed. Never raises."""
    try:
        async with async_session_factory() as session:
            appointment = await session.scalar(
                select(Appointment).where(
                    Appointment.id == appointment_id, Appointment.tenant_id == tenant_id
                )
            )
            if appointment is None or appointment.start_at is None:
                return
            provider = await session.scalar(
                select(PixDeposit.provider).where(
                    PixDeposit.tenant_id == tenant_id, PixDeposit.appointment_id == appointment_id
                )
            )
            await _open_item(
                session,
                tenant_id=tenant_id,
                appointment_id=appointment_id,
                patient_id=appointment.patient_id,
                provider=provider or "asaas",
                start_at=_as_utc(appointment.start_at),
                end_at=_as_utc(appointment.end_at) if appointment.end_at else None,
                service=appointment.appointment_type,
                reference="price_change_mail",
            )
        logger.info(
            "price_change_mail_handed_to_clinic",
            tenant_id=str(tenant_id),
            appointment_id=str(appointment_id),
        )
    except Exception as exc:  # noqa: BLE001 - logged; the clinic item is best effort
        logger.warning(
            "price_change_mail_item_failed",
            error_type=type(exc).__name__,
            tenant_id=str(tenant_id),
            appointment_id=str(appointment_id),
        )


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
    `blocked`. AWAITING -> `deposit_lifecycle.requote_amount_cents(...) != deposit.amount_cents`,
    but only with a readable price (no price / service off = no notice, like `blocked`).
    A clinic without sinal, or rows that do not belong together, -> False."""
    if (
        not tenant.pix_deposit_enabled
        or appointment.tenant_id != tenant.id
        or deposit.tenant_id != tenant.id
        or deposit.appointment_id != appointment.id
        or not appointment.appointment_type
    ):
        return False
    if deposit.status == PixDepositStatus.PAID:
        decision = await service_change.preview_service_change(
            session,
            tenant=tenant,
            appointment=appointment,
            deposit=deposit,
            new_service=appointment.appointment_type,
            professional_id=appointment.professional_id,
            start_at=appointment.start_at,
            now=now,
        )
        if decision.action == "increase_charge":
            account = await accounts.load_charge_account(
                session,
                tenant_id=tenant.id,
                provider=deposit.provider or tenant.payment_provider or "asaas",
                timing="after",
            )
            return account is not None
        if decision.action == "decrease_ask":
            return True
        if decision.action == "decrease_keep":
            return decision.diff_cents >= MIN_DIFFERENCE_CENTS
        return False
    if deposit.status == PixDepositStatus.AWAITING:
        # Fix round 1 (review I2): a service switched off, gone, or without a readable price is
        # NOT a price change - `requote_amount_cents` would read it as 0 and the notice would void
        # the sinal of a booking the clinic only stopped OFFERING. Same silence as PAID's
        # `blocked`. A real price whose new sinal is under the provider minimum still counts.
        _name, price_text = await _resolve_service_and_price(session, tenant, appointment)
        price_cents = parse_brl_to_cents(price_text)
        if price_cents is None or price_cents <= 0:
            return False
        amount = await requote_amount_cents(session, tenant=tenant, appointment=appointment)
        return amount != int(deposit.amount_cents or 0)
    return False


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
    "PRICE_NOTICE_MAX_ATTEMPTS",
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
