"""degrade - split out of workers/tasks.py (TASK-023)."""

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Tenant,
)
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.tenant_config import (
    get_waba_token,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.sender import (
    _reply_sender,
    _send_simple_text,
)
from secretaria.workers.shared.text import (
    SERVICE_UNAVAILABLE_MESSAGE,
)

logger = get_logger(__name__)


async def _handle_service_unavailable(reply: _ReplyContext, redis=None) -> None:
    """The "bot not activated yet" degrade, on the RIGHT sender (PROMPT_FIX_21).

    This path has no conversation (none is created for an inactive tenant), so
    it used to build a bare `WhatsAppClient()` and answer from the global env
    scaffold - i.e. potentially from another clinic's WhatsApp number, and
    without the entitlement gate every other outbound passes. It now resolves
    the tenant carried on the `_ReplyContext`, applies the same fail-closed
    entitlement check as `_send_bot_reply`, and sends on that tenant's own
    credentials or not at all.

    The allowlist was already applied upstream, before this context was ever
    built (see `_persist_inbound_message`).
    """
    if reply.tenant_id is None:
        logger.error("whatsapp_credential_missing", tenant_id=None, missing="tenant")
        return

    async with async_session_factory() as session:
        tenant = await session.get(Tenant, reply.tenant_id)
        if tenant is None:
            logger.error("whatsapp_credential_missing", tenant_id=None, missing="tenant")
            return
        waba_token = await get_waba_token(session, tenant.id)

    summary = await get_entitlements(tenant.id, redis)
    if summary is None or not (summary.active and summary.secretaria_enabled):
        # Fails closed exactly like the main reply path: an unentitled tenant
        # gets no outbound at all, not even this fallback.
        logger.warning(
            "bot_reply_suppressed_unentitled",
            tenant_id=str(tenant.id),
            status=summary.status if summary is not None else None,
        )
        return

    client = _reply_sender(reply, tenant, waba_token)
    if client is None:
        return
    await _send_simple_text(reply.patient_ref, SERVICE_UNAVAILABLE_MESSAGE, client=client)
