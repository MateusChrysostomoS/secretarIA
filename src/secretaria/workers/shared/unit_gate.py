"""The secretária only answers a clinic with a usable unit (TASK-090 Adendo 1 §A).

A clinic is served only when it has at least one USABLE unit - active AND with a complete
address (services/professional_units.py::unit_is_usable). Without one the reply is suppressed
exactly like an unentitled clinic's (`bot_reply_suppressed_unentitled`): the inbound message
stays persisted, the handover state is untouched (a human can still read and answer it), and
nothing is sent. The log event is `bot_reply_suppressed_no_complete_unit`, with ids only.

FAILS OPEN, on purpose (unlike the entitlement gate, which fails closed): the entitlement
gate is the security/billing gate; this one is a product-readiness gate. A database error
while checking must not silence a healthy clinic, so it is logged
(`unit_gate_check_failed`, warning) and the reply goes ahead.

Called by every path that starts the bot speaking on a conversation: the main turn
(workers/orchestrator.py, after the entitlement gate), the turn safety net, the attachment
receipt, the inactive-tenant degrade and the Portal entry/merge openings. The reminder
action buttons and the openings tied to an existing appointment (a reminder link, a price
notice) are NOT gated: existing appointments are untouched by this rule.
"""

from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.services import professional_units

logger = get_logger(__name__)


async def reply_blocked_no_usable_unit(
    tenant_id: UUID | None, *, conversation_id: UUID | None = None
) -> bool:
    """True when the clinic has NO usable unit (and the suppression was logged); False when it
    has one, when there is no tenant to check (the callers' own gates handle that), or when the
    check itself failed (fail open, logged). Never raises."""
    if tenant_id is None:
        return False
    try:
        async with async_session_factory() as session:
            usable = await professional_units.has_usable_units(session, tenant_id)
    except Exception as exc:
        logger.warning(
            "unit_gate_check_failed",
            tenant_id=str(tenant_id),
            conversation_id=str(conversation_id) if conversation_id is not None else None,
            error_type=type(exc).__name__,
        )
        return False
    if usable:
        return False
    logger.warning(
        "bot_reply_suppressed_no_complete_unit",
        tenant_id=str(tenant_id),
        conversation_id=str(conversation_id) if conversation_id is not None else None,
    )
    return True


__all__ = ["reply_blocked_no_usable_unit"]
