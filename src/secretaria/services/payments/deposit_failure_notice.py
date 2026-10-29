"""Tell the clinic (and the doctor) that a consultation was booked WITHOUT its Pix deposit
because the charge failed for a technical reason (TASK-071, Plan 3, task 2).

`notify_deposit_failure` does two independent things, in this order:

1. ALWAYS records the failure on the clinic's payment account (reason + stage + when), even when
   no e-mail goes out: the badge in the Portal depends on it, not on the e-mail;
2. e-mails the clinic (`Tenant.contact_email`) and, when the consultation has a doctor with a
   linked address, the doctor (`fetch_professional_emails`, asked of brain-api, never stored),
   AT MOST `DAILY_EMAIL_CAP` e-mails per recipient per LOCAL day (the clinic's timezone). The
   count is one Redis `INCR` per recipient, so concurrent failures can never send a 4th one.
   The key carries a hash of the address, never the address (`deposit_fail_mail:{tenant}:
   {sha256(email)[:16]}:{YYYYMMDD}`), and expires after 48 h.

It NEVER raises into the booking (ruling R8): every error is swallowed and logged with a fixed
event name and sanitized fields (tenant id, reason, stage, outcome) - never an address, a name, a
document or a phone. If Redis is unreachable it does NOT send (a missing counter must not become
a flood); that recipient counts as `send_failed`.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
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


class NoticeOutcome(StrEnum):
    sent = "sent"
    capped = "capped"
    no_recipient = "no_recipient"
    send_failed = "send_failed"


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
    digest = hashlib.sha256(address.strip().lower().encode()).hexdigest()[:16]
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
    tenant_id: UUID, provider: str, reason: FailureReason, stage: str
) -> tuple[str, str | None, ZoneInfo]:
    """Record the failure (committed) and return (clinic name, contact e-mail, clinic tz)."""
    async with async_session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return "", None, ZoneInfo("UTC")
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
) -> NoticeResult:
    clinic_name, clinic_email, tz = await _record_and_load(tenant_id, provider, reason, stage)

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
) -> NoticeResult:
    """Record the failed deposit and e-mail the clinic (and the doctor) within the daily cap.

    `redis` is any client with `incr` / `expire` (the arq pool the callers already hold); when
    omitted a short-lived client is opened from `REDIS_URL`. Never raises.
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
