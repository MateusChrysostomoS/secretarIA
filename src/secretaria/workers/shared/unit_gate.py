"""The secretária only answers a clinic with a usable unit (TASK-090 Adendo 1 §A).

A clinic is served only when it has at least one USABLE unit - active AND with a complete
address (services/professional_units.py::unit_is_usable) - and, when it has active doctors,
at least one a patient may book with (`professional_units.clinic_servable`; a doctor whose unit
links are all unusable is hidden from patients - owner ruling). Without that the reply is
suppressed
exactly like an unentitled clinic's (`bot_reply_suppressed_unentitled`): the inbound message
stays persisted, the handover state is untouched (a human can still read and answer it), and
nothing is sent. The log event is `bot_reply_suppressed_no_complete_unit`, with ids only.

FAILS OPEN, on purpose (unlike the entitlement gate, which fails closed): the entitlement
gate is the security/billing gate; this one is a product-readiness gate. A database error
while checking must not silence a healthy clinic, so it is logged
(`unit_gate_check_failed`, warning) and the reply goes ahead.

Called by every path that starts the bot speaking on a conversation: the main turn
(workers/orchestrator.py, after the entitlement gate), the turn safety net, the attachment
receipt, the "não entendi o áudio" reply (workers/whatsapp/audio.py), the greeting-button
degrade, the inactive-tenant degrade and the Portal entry/merge openings. NOT gated (author's
ruling, for the owner to confirm): the reminder action buttons and the openings tied to an
existing appointment (a reminder link, a price notice) - existing appointments are untouched -
and the operator's context-reset command of a test clinic (workers/whatsapp/remove_context.py),
which is the operator acting, not a patient conversation.
"""

from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.services import professional_units

logger = get_logger(__name__)


async def reply_blocked_no_usable_unit(
    tenant_id: UUID | None,
    *,
    conversation_id: UUID | None = None,
    known_servable: bool | None = None,
) -> bool:
    """True when the clinic cannot be served (and the suppression was logged); False when it
    can, when there is no tenant to check (the callers' own gates handle that), or when the
    check itself failed (fail open, logged). Never raises.

    Servable = `professional_units.clinic_servable`: a USABLE unit and, with active doctors, at
    least one a patient may book with (the owner's ruling hides a doctor whose unit links are
    all unusable). `known_servable`: the answer the caller already read in its own load (the
    main turn), so the gate does not read the same rows twice; None = read it here."""
    if tenant_id is None:
        return False
    try:
        if known_servable is not None:
            usable = known_servable
        else:
            async with async_session_factory() as session:
                usable = await professional_units.clinic_servable(session, tenant_id)
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
