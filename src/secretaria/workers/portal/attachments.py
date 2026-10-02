"""attachments - split out of workers/tasks.py (TASK-023)."""

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Tenant,
)
from secretaria.services.entitlements_client import get_entitlements
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.sender import (
    _reply_sender,
    _send_simple_text,
)

logger = get_logger(__name__)


#: The bot's whole answer to a file: transport, not analysis. No OCR, no vision, no
#: summary - reading the file is the clinic's job (that is PreCheck's differentiator
#: for the exams it receives, not a capability of this channel).
ATTACHMENT_RECEIVED_MESSAGE = "Recebi seu arquivo, a equipe da clínica vai conferir."

async def _handle_attachment_received(reply: _ReplyContext, redis=None) -> None:
    """Acknowledge a file with the fixed receipt - entitlement-gated, on the channel's
    own sender. The shape of `_handle_service_unavailable`: resolve the tenant the
    context carries, fail closed on entitlement, send through `_reply_sender`. On
    Brain-Message (the only channel a file arrives on) the sender records the row.
    """
    if reply.tenant_id is None:
        logger.error("attachment_receipt_no_tenant")
        return
    async with async_session_factory() as session:
        tenant = await session.get(Tenant, reply.tenant_id)
    if tenant is None:
        logger.error("attachment_receipt_no_tenant", tenant_id=str(reply.tenant_id))
        return
    summary = await get_entitlements(tenant.id, redis)
    if summary is None or not (summary.active and summary.secretaria_enabled):
        logger.warning(
            "bot_reply_suppressed_unentitled",
            tenant_id=str(tenant.id),
            status=summary.status if summary is not None else None,
        )
        return
    sender = _reply_sender(reply, tenant, None)
    if sender is None:
        return
    await _send_simple_text(reply.patient_ref, ATTACHMENT_RECEIVED_MESSAGE, client=sender)
