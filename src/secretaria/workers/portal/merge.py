"""Discard a Portal visit superseded by the clinic's existing account."""

from uuid import UUID

from arq import Retry
from sqlalchemy import select

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation, Message, MessageDirection, Patient, Tenant
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.tenant_config import list_active_professionals
from secretaria.services.visit_merge import discard_visit
from secretaria.workers.portal.open import (
    _open_ledger_key,
    _portal_conversation_has,
    process_brain_message_open,
)
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.jobs import _claim_event, _release_event
from secretaria.workers.shared.sentinels import _handle_show_main_menu

logger = get_logger(__name__)


async def merge_brain_message_visit(
    ctx: dict,
    tenant_id: str,
    visit_external_id: str,
    into_external_id: str,
) -> None:
    """arq job: a Portal visit was merged into an existing account - throw the visit away.

    The visit's conversation (greeting, e-mail, code) is deleted; it is NOT carried over, by
    the owner's decision: the opening shows once, the first time ever. The account's own
    conversation then gets the main menu so a patient who returns mid-flow is never left
    staring at a stale step. An account that never had a conversation here gets the normal
    opening instead (that IS its first time).

    Idempotent through a ledger key, like the open job. The discard is committed BEFORE
    anything is sent, so a send failure can never resurrect the visit.
    """
    if visit_external_id == into_external_id:
        logger.warning("brain_message_merge_self_refused", tenant_id=tenant_id)
        return
    tenant_uuid = UUID(tenant_id)
    key = f"brain_message_merge:{tenant_id}:{visit_external_id}"
    try:
        claimed = await _claim_event(key)
    except Exception as exc:
        logger.warning(
            "brain_message_merge_claim_retry", tenant_id=tenant_id, error=type(exc).__name__
        )
        raise Retry(defer=5) from exc
    if not claimed:
        logger.info("brain_message_merge_already_claimed", tenant_id=tenant_id)
        return

    try:
        await _finish_merge(ctx, tenant_uuid, tenant_id, visit_external_id, into_external_id)
    except Exception as exc:
        await _release_event(key, event="brain_message_merge_release_failed")
        logger.warning("brain_message_merge_retry", tenant_id=tenant_id, error=type(exc).__name__)
        raise Retry(defer=5) from exc


async def _finish_merge(
    ctx: dict, tenant_uuid: UUID, tenant_id: str, visit_external_id: str, into_external_id: str
) -> None:
    async with async_session_factory() as session:
        discarded = await discard_visit(session, tenant_uuid, visit_external_id)
        await session.commit()
    logger.info("brain_message_merge_discarded", tenant_id=tenant_id, status=discarded.status)

    async with async_session_factory() as session:
        account = await session.scalar(
            select(Patient).where(
                Patient.tenant_id == tenant_uuid,
                Patient.channel == CHANNEL_BRAIN_MESSAGE,
                Patient.external_id == into_external_id,
            )
        )
        conversation = (
            await session.scalar(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_uuid, Conversation.patient_id == account.id
                )
            )
            if account is not None
            else None
        )
        has_history = (
            conversation is not None
            and await session.scalar(
                select(Message.id).where(Message.conversation_id == conversation.id).limit(1)
            )
            is not None
        )
        tenant = await session.get(Tenant, tenant_uuid)
        professionals = (
            await list_active_professionals(session, tenant_uuid) if tenant is not None else []
        )

    if tenant is None:
        return
    summary = await get_entitlements(tenant_uuid, ctx.get("redis"))
    if summary is None or not (summary.active and summary.secretaria_enabled):
        logger.warning(
            "bot_reply_suppressed_unentitled",
            tenant_id=tenant_id,
            entitlement_unknown=summary is None,
            conversation_id=str(conversation.id) if conversation is not None else None,
        )
        return
    if not has_history:
        # The account's identity never had a conversation here: this IS the first time.
        try:
            await process_brain_message_open(ctx, tenant_id, into_external_id)
        except Exception:
            await _release_event(
                _open_ledger_key(tenant_uuid, into_external_id),
                event="brain_message_merge_open_release_failed",
            )
            raise
        if not await _portal_conversation_has(
            tenant_uuid, into_external_id, direction=MessageDirection.OUTBOUND
        ):
            raise RuntimeError("merge_open_not_sent")
        return

    reply = _ReplyContext(
        channel=CHANNEL_BRAIN_MESSAGE,
        conversation_id=conversation.id,
        patient_ref=into_external_id,
        inbound_body="",
        tenant_id=tenant_uuid,
    )
    rendered = await _handle_show_main_menu(
        reply,
        tenant,
        professionals,
        into_external_id,
        redis=ctx.get("redis"),
        source="merged_visit",
    )
    if not rendered:
        raise RuntimeError("merge_menu_not_sent")
