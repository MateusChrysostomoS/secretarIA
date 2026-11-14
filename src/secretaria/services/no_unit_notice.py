"""Tell the clinic that a doctor + service has NO unit where it can be booked (TASK-083, T6;
spec §6.3).

The router (`flow_router._enter_no_unit_for_service`) already answered the patient in plain words
and returned `notify_no_unit`; the worker (`flow_runner._notify_no_unit_for_service`) calls
`send_no_unit_notice` for it. One plain e-mail to `Tenant.contact_email` with a link to the doctor's
page in /contexto (`{BRAIN_MESSAGE_PUBLIC_URL}/contexto/?prof=<professional_id>`), AT MOST ONE per
(clinic, doctor, service) per LOCAL day (the clinic's timezone).

The limit is one atomic Redis `INCR` (`within_daily_cap`, shared with `deposit_failure_notice`),
keyed `no_unit_mail:{tenant}:{professional}:{service}:{YYYYMMDD}`: ids only, never an address. If
the send fails the day's slot is released so the next patient who hits the same dead end can retry.

Nothing about the patient reaches the e-mail or the logs (no name, phone, channel); logs carry ids
and an error type only. It NEVER raises into the patient's flow: no `contact_email`, no Redis, a
failing SMTP all read as `False`. If Redis is unreachable it does NOT send (a missing counter must
not become a flood).
"""

from datetime import UTC, datetime
from uuid import UUID

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.services import email
from secretaria.services.payments.deposit_failure_notice import within_daily_cap
from secretaria.services.payments.deposit_lifecycle import _tenant_tz

logger = get_logger(__name__)

DAILY_CAP = 1


def _now() -> datetime:
    """The clock, isolated so tests can move it across the local midnight."""
    return datetime.now(UTC)


def no_unit_link(tenant: Tenant, professional_id: UUID) -> str:
    """Where the clinic fixes it: the doctor's page in the Portal's /contexto."""
    base = (get_settings().BRAIN_MESSAGE_PUBLIC_URL or "").strip().rstrip("/")
    return f"{base}/contexto/?prof={professional_id}"


def _counter_key(tenant_id: UUID, professional_id: UUID, service_id: UUID, local_day: str) -> str:
    return f"no_unit_mail:{tenant_id}:{professional_id}:{service_id}:{local_day}"


async def _release(redis, key: str) -> None:
    """Give the day's slot back after a failed send (best effort)."""
    try:
        await redis.delete(key)
    except Exception:  # noqa: BLE001 - at worst the clinic hears about it tomorrow
        logger.warning("no_unit_notice_release_failed")


async def _send(
    tenant: Tenant,
    professional_id: UUID,
    professional_name: str,
    service_id: UUID,
    service_name: str,
    redis,
) -> bool:
    address = (tenant.contact_email or "").strip()
    if not address:
        logger.info("no_unit_notice_no_recipient", tenant_id=str(tenant.id))
        return False
    local_day = _now().astimezone(_tenant_tz(tenant)).strftime("%Y%m%d")
    key = _counter_key(tenant.id, professional_id, service_id, local_day)
    try:
        if not await within_daily_cap(redis, key, DAILY_CAP):
            return False
    except Exception as exc:  # noqa: BLE001 - no counter, no e-mail
        logger.warning(
            "no_unit_notice_counter_unavailable",
            tenant_id=str(tenant.id),
            error_type=type(exc).__name__,
        )
        return False
    try:
        delivered = await email.send_no_unit_alert(
            address,
            tenant.clinic_name,
            professional_name=professional_name,
            service_name=service_name,
            link=no_unit_link(tenant, professional_id),
        )
    except Exception as exc:  # noqa: BLE001 - e-mail is best effort
        delivered = False
        logger.warning(
            "no_unit_notice_email_error",
            tenant_id=str(tenant.id),
            error_type=type(exc).__name__,
        )
    if not delivered:
        await _release(redis, key)
    return bool(delivered)


async def send_no_unit_notice(
    tenant: Tenant,
    professional_id: UUID,
    professional_name: str,
    service_id: UUID,
    service_name: str,
    *,
    redis=None,
) -> bool:
    """E-mail the clinic about (doctor, service) unless it already heard today. `True` = sent.

    `redis` is any client with `incr` / `expire` / `delete` (the arq pool the worker holds); when
    omitted a short-lived client is opened from `REDIS_URL`. Never raises.
    """
    owned = None
    try:
        if redis is None:
            from redis.asyncio import from_url

            owned = redis = from_url(get_settings().REDIS_URL)
        sent = await _send(
            tenant, professional_id, professional_name, service_id, service_name, redis
        )
    except Exception as exc:  # noqa: BLE001 - never into the patient's flow
        logger.warning(
            "no_unit_notice_error",
            tenant_id=str(getattr(tenant, "id", None)),
            error_type=type(exc).__name__,
        )
        sent = False
    finally:
        if owned is not None:
            try:
                await owned.aclose()
            except Exception:  # noqa: BLE001
                pass
    logger.info(
        "no_unit_notice",
        tenant_id=str(getattr(tenant, "id", None)),
        professional_id=str(professional_id),
        service_id=str(service_id),
        sent=sent,
    )
    return sent
