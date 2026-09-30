"""Clinic payment email gate: only a patient's cancellation of a paid deposit qualifies.

Operational payment issues are shown in the clinic UI. The one allowed email is keyed by
attention item, so two distinct cancellations never suppress one another.
"""

from uuid import UUID

from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.services import email

logger = get_logger(__name__)

ALERT_DEBOUNCE_SECONDS = 6 * 60 * 60

ALERT_KINDS = frozenset({"patient_cancelled_paid"})


async def send_payment_alert(
    redis, tenant: Tenant, kind: str, *, item_id: UUID | None = None
) -> bool:
    """Attempt the one allowed email at most once per attention item. Never raises."""
    if kind not in ALERT_KINDS or item_id is None:
        return False
    if not tenant.contact_email:
        return False
    if redis is None:
        logger.warning("payment_alert_skipped_no_redis", kind=kind, tenant_id=str(tenant.id))
        return False
    claimed = await redis.set(
        f"payalert:{tenant.id}:{item_id}", "1", ex=ALERT_DEBOUNCE_SECONDS, nx=True
    )
    if not claimed:
        return False
    await email.send_payment_alert(tenant.contact_email, tenant.clinic_name, kind)
    return True
