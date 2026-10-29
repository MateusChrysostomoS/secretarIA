"""Tell the clinic (and the doctor) that a consultation was booked WITHOUT its Pix deposit
because the charge failed for a technical reason (TASK-071, Plan 3, task 2).

`notify_deposit_failure` does two independent things, in this order:

1. records the failure on the clinic's payment account (reason + stage + when), even when no
   e-mail goes out: the badge in the Portal depends on it, not on the e-mail. The one exception
   is a deposit skipped by the breaker (`record=False`, task 5): nothing new failed, and
   refreshing the timestamp would keep the breaker open for as long as patients keep booking;
2. e-mails the clinic (`Tenant.contact_email`) and, when the consultation has a doctor with a
   linked address, the doctor (`fetch_professional_emails`, asked of brain-api, never stored),
   AT MOST `DAILY_EMAIL_CAP` e-mails per recipient per LOCAL day (the clinic's timezone). The
   count is one Redis `INCR` per recipient, so concurrent failures can never send a 4th one.
   The key carries a tenant-salted hash of the address, never the address
   (`deposit_fail_mail:{tenant}:{sha256(f"{tenant}:{email}")[:16]}:{YYYYMMDD}`), and expires
   after 48 h.

It NEVER raises into the booking (ruling R8): every error is swallowed and logged with a fixed
event name and sanitized fields (tenant id, reason, stage, outcome) - never an address, a name, a
document or a phone. If Redis is unreachable it does NOT send (a missing counter must not become
a flood); that recipient counts as `send_failed`.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.services import email
from secretaria.services.brain_professionals import fetch_professional_emails
from secretaria.services.payments import accounts
from secretaria.services.payments.deposit_lifecycle import _tenant_tz
from secretaria.services.payments.failure_reason import FailureReason, action_text, user_text

logger = get_logger(__name__)

DAILY_EMAIL_CAP = 3
_COUNTER_TTL_SECONDS = 48 * 60 * 60

# TASK-071 Plan 3, task 3: the `ChargeStartError.code`s (and the quote-level `blocked` reasons of
# `deposit_gate.quote_deposit`) that are a TECHNICAL failure of the deposit. None of them may
# stop the patient from booking: the consultation is confirmed WITHOUT the deposit and the clinic
# is told (`notify_deposit_failure`). `payer_*_required` only reaches this set once the patient
# already GAVE that field (the provider still asks): before that it is a question, not a failure.
CONTINUE_WITHOUT_DEPOSIT: frozenset[str] = frozenset(
    {
        # provider answers (booking_charge / asaas_provider)
        "charge_failed",
        "customer_failed",
        "payer_document_required",
        "payer_email_required",
        "customer_missing_id",
        "charge_missing_id",
        "no_api_key",
        # booking_charge's own outcomes
        "no_charge_id",
        "no_copy_paste",
        "persist_failed",
        "no_account",
        "no_provider",
        # quote-level `blocked` reasons (deposit_gate / BookingGate._deposit_quote)
        "no_credential",
        "quote_failed",
    }
)
# NOT technical, and they keep blocking: a live charge already exists for this conversation, or
# there is no patient to charge.
KEEPS_BLOCKING: frozenset[str] = frozenset({"charge_pending", "no_patient"})

# Stages where the deposit is dropped although the CHARGE did not fail: our message could not be
# delivered (`delivery`, the charge was created fine) or the payer field could not be kept
# (`payer_data`, Redis). The booking still goes on without the deposit, but the account is NOT
# marked failing and the clinic gets no "Sinal do Pix" e-mail: that is about the provider only.
NOT_A_CHARGE_FAILURE_STAGES: frozenset[str] = frozenset({"delivery", "payer_data"})


def tells_the_clinic(stage: str) -> bool:
    """Whether a dropped deposit at `stage` is a charge failure the clinic must hear about."""
    return stage not in NOT_A_CHARGE_FAILURE_STAGES


# TASK-071 Plan 3, task 5 (the "breaker"): the failures that belong to the clinic's ACCOUNT, so
# the next charge would fail the same way. While one of them is the account's latest failure (and
# younger than `DEPOSIT_BREAKER_COOLDOWN_MINUTES`) the deposit step is skipped: no CPF question,
# no customer, no charge. A patient-level failure (`customer_document_invalid`, ...) or a passing
# one (`provider_unavailable`, `rate_limited`) never opens it.
BREAKER_REASONS: frozenset[FailureReason] = frozenset(
    {
        FailureReason.api_key_invalid,
        FailureReason.permission_denied,
        FailureReason.qr_unavailable,
        FailureReason.account_not_enabled,
        FailureReason.pix_key_missing,
    }
)
# The fixed stage of a deposit skipped by the breaker: the clinic is told (with the cap) but the
# skip is NOT recorded as a new failure, so the cooldown counts from the last REAL one.
BREAKER_STAGE = "breaker"


def _breaker_cooldown() -> timedelta:
    return timedelta(minutes=get_settings().DEPOSIT_BREAKER_COOLDOWN_MINUTES)


def breaker_open(row, now: datetime) -> bool:
    """Whether the deposit step must be skipped for the account `row` at `now`: its health is
    `failing`, the last failure is an account-level one (`BREAKER_REASONS`, compared by value:
    the column holds the string), it happened less than the cooldown ago, and no connection check
    (`last_verified_at`: a reconnection, the "Tentar de novo" step, the daily recheck) came after
    it - so a clinic that reconnected closes the breaker at once and the next charge is tried.
    The badge (`charge_health`) stays `failing` until a real charge works (owner, 2026-10-29)."""
    if row is None or accounts.charge_health(row) != "failing":
        return False
    if row.last_charge_failure_reason not in {reason.value for reason in BREAKER_REASONS}:
        return False
    failed_at = accounts._utc(row.last_charge_failure_at)
    verified_at = getattr(row, "last_verified_at", None)
    if verified_at is not None and failed_at < accounts._utc(verified_at):
        return False
    return accounts._utc(now) - failed_at < _breaker_cooldown()


async def breaker_reason(tenant_id: UUID, provider: str) -> FailureReason | None:
    """The account's last failure reason when the breaker is OPEN for (tenant, provider), else
    None. Reads the row in a short session of its own; any error reads as a CLOSED breaker
    (logged): the breaker may only ever skip a charge, never break a booking."""
    try:
        async with async_session_factory() as session:
            row = await accounts.get_account_row(session, tenant_id=tenant_id, provider=provider)
            if not breaker_open(row, _now()):
                return None
            return FailureReason(row.last_charge_failure_reason)
    except Exception as exc:  # noqa: BLE001 - a closed breaker is the safe answer
        logger.warning(
            "deposit_breaker_read_failed",
            tenant_id=str(tenant_id),
            error_type=type(exc).__name__,
        )
        return None


_REASON_FOR_CODE: dict[str, FailureReason] = {
    "no_account": FailureReason.no_account,
    "no_credential": FailureReason.no_account,
    "persist_failed": FailureReason.persist_failed,
    "no_copy_paste": FailureReason.qr_unavailable,
    "no_api_key": FailureReason.api_key_invalid,
}


def continues_without_deposit(code: str | None, reason: object = None) -> bool:
    """Whether a failure to start the deposit lets the booking go on without it.

    A listed code always does; so does any other code the provider layer CLASSIFIED (it carries
    a `reason`: a provider code nobody listed yet must not reopen the blocked booking). The two
    `KEEPS_BLOCKING` codes never do."""
    if code in KEEPS_BLOCKING:
        return False
    return code in CONTINUE_WITHOUT_DEPOSIT or reason is not None


def failure_reason_for(code: str | None, reason: object = None) -> FailureReason:
    """The closed reason for the clinic's e-mail: the classified one when there is one, else the
    closest one for the code, else `unknown`."""
    if reason is not None:
        try:
            return FailureReason(str(reason))
        except ValueError:
            pass
    return _REASON_FOR_CODE.get(code or "", FailureReason.unknown)


class NoticeOutcome(StrEnum):
    sent = "sent"
    capped = "capped"
    no_recipient = "no_recipient"
    send_failed = "send_failed"
    # `send_email=False`: the failure was recorded, nobody was e-mailed (the booking it would
    # describe was not made - review fix, 2026-10-29).
    skipped = "skipped"


@dataclass
class NoticeResult:
    """Overall outcome plus how many recipients landed in each bucket (counts only, no
    addresses)."""

    outcome: NoticeOutcome
    sent: int = 0
    capped: int = 0
    failed: int = 0


def _now() -> datetime:
    """The clock, isolated so tests can move it across the local midnight."""
    return datetime.now(UTC)


def _counter_key(tenant_id: UUID, address: str, local_day: str) -> str:
    # Salted with the tenant id, so the digest is not the bare hash of an address (which a list
    # of known addresses could reverse) and the same address hashes differently per clinic.
    material = f"{tenant_id}:{address.strip().lower()}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:16]
    return f"deposit_fail_mail:{tenant_id}:{digest}:{local_day}"


def _overall(sent: int, failed: int, capped: int) -> NoticeOutcome:
    if sent:
        return NoticeOutcome.sent
    if failed:
        return NoticeOutcome.send_failed
    if capped:
        return NoticeOutcome.capped
    return NoticeOutcome.no_recipient


def _when_local(start_at: datetime, tz: ZoneInfo) -> str:
    aware = start_at if start_at.tzinfo is not None else start_at.replace(tzinfo=UTC)
    return aware.astimezone(tz).strftime("%d/%m/%Y às %H:%M")


async def _record_and_load(
    tenant_id: UUID, provider: str, reason: FailureReason, stage: str, *, record: bool = True
) -> tuple[str, str | None, ZoneInfo]:
    """Record the failure (committed, unless `record` is False) and return (clinic name, contact
    e-mail, clinic tz)."""
    async with async_session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return "", None, ZoneInfo("UTC")
        if not record:
            return (
                tenant.clinic_name,
                (tenant.contact_email or "").strip() or None,
                _tenant_tz(tenant),
            )
        try:
            await accounts.record_charge_failure(
                session, tenant_id=tenant_id, provider=provider, reason=reason, stage=stage
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - recording must not stop the e-mail
            await session.rollback()
            logger.warning(
                "deposit_failure_record_failed",
                tenant_id=str(tenant_id),
                reason=str(reason),
                stage=stage,
                error_type=type(exc).__name__,
            )
        return tenant.clinic_name, (tenant.contact_email or "").strip() or None, _tenant_tz(tenant)


async def _doctor_address(tenant_id: UUID, professional_id: UUID | None) -> str | None:
    if professional_id is None:
        return None
    try:
        emails = await fetch_professional_emails(tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "deposit_failure_doctor_lookup_failed",
            tenant_id=str(tenant_id),
            error_type=type(exc).__name__,
        )
        return None
    return ((emails or {}).get(str(professional_id)) or "").strip() or None


async def _within_cap(redis, key: str) -> bool:
    """INCR the recipient's counter; True while it is still within the daily cap."""
    count = await redis.incr(key)
    try:
        await redis.expire(key, _COUNTER_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - the count itself is what matters
        logger.warning("deposit_failure_counter_expire_failed")
    return int(count) <= DAILY_EMAIL_CAP


async def _notify(
    *,
    tenant_id: UUID,
    patient_name: str | None,
    service: str | None,
    start_at: datetime,
    reason: FailureReason,
    stage: str,
    professional_id: UUID | None,
    provider: str,
    redis,
    record: bool = True,
    send_email: bool = True,
) -> NoticeResult:
    clinic_name, clinic_email, tz = await _record_and_load(
        tenant_id, provider, reason, stage, record=record
    )
    if not send_email:
        return NoticeResult(NoticeOutcome.skipped)

    # Distinct recipients by normalized address (same address for clinic and doctor = one mail).
    recipients: dict[str, str] = {}
    for address in (clinic_email, await _doctor_address(tenant_id, professional_id)):
        if address and address.strip().lower() not in recipients:
            recipients[address.strip().lower()] = address.strip()
    if not recipients:
        return NoticeResult(NoticeOutcome.no_recipient)

    local_day = _now().astimezone(tz).strftime("%Y%m%d")
    when_local = _when_local(start_at, tz)
    sent = capped = failed = 0
    for normalized, address in recipients.items():
        try:
            if not await _within_cap(redis, _counter_key(tenant_id, normalized, local_day)):
                capped += 1
                continue
        except Exception as exc:  # noqa: BLE001 - no counter, no e-mail
            failed += 1
            logger.warning(
                "deposit_failure_counter_unavailable",
                tenant_id=str(tenant_id),
                error_type=type(exc).__name__,
            )
            continue
        try:
            delivered = await email.send_deposit_unavailable_alert(
                address,
                clinic_name,
                patient_name=patient_name,
                service=service,
                when_local=when_local,
                reason_text=user_text(reason),
                action_text=action_text(reason),
            )
        except Exception as exc:  # noqa: BLE001 - e-mail is best effort
            delivered = False
            logger.warning(
                "deposit_failure_email_error",
                tenant_id=str(tenant_id),
                error_type=type(exc).__name__,
            )
        if delivered:
            sent += 1
        else:
            failed += 1
    return NoticeResult(_overall(sent, failed, capped), sent, capped, failed)


async def notify_deposit_failure(
    *,
    tenant_id: UUID,
    patient_name: str | None,
    service: str | None,
    start_at: datetime,
    reason: FailureReason,
    stage: str,
    professional_id: UUID | None = None,
    provider: str = "asaas",
    redis=None,
    record: bool = True,
    send_email: bool = True,
) -> NoticeResult:
    """Record the failed deposit and e-mail the clinic (and the doctor) within the daily cap.

    `send_email=False` records only (the badge stays truthful): the e-mail says the patient booked
    WITHOUT the deposit, so it is never sent for a booking that was not made.

    `redis` is any client with `incr` / `expire` (the arq pool the callers already hold); when
    omitted a short-lived client is opened from `REDIS_URL`. `record=False` (a deposit skipped by
    the breaker, task 5) e-mails without recording a new failure: refreshing
    `last_charge_failure_at` on every skip would keep the breaker open for as long as patients
    keep booking, and the real charge would never be tried again. Never raises.
    """
    owned = None
    try:
        if redis is None:
            from redis.asyncio import from_url

            owned = redis = from_url(get_settings().REDIS_URL)
        result = await _notify(
            tenant_id=tenant_id,
            patient_name=patient_name,
            service=service,
            start_at=start_at,
            reason=reason,
            stage=stage,
            professional_id=professional_id,
            provider=provider,
            redis=redis,
            record=record,
            send_email=send_email,
        )
    except Exception as exc:  # noqa: BLE001 - ruling R8: never into the booking
        logger.warning(
            "deposit_failure_notice_error",
            tenant_id=str(tenant_id),
            reason=str(reason),
            stage=stage,
            error_type=type(exc).__name__,
        )
        result = NoticeResult(NoticeOutcome.send_failed)
    finally:
        if owned is not None:
            try:
                await owned.aclose()
            except Exception:  # noqa: BLE001
                pass
    logger.info(
        "deposit_failure_notice",
        tenant_id=str(tenant_id),
        reason=str(reason),
        stage=stage,
        outcome=str(result.outcome),
    )
    return result
