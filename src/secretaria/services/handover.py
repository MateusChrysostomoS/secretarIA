"""Handover logic - switching a conversation between the bot and a human.

Coexistence: the AI (via Cloud API) and a human secretary (via the WhatsApp
mobile app) share the same number. When the secretary replies from the app,
Meta sends a `smb_message_echoes` event; we move that conversation to
HUMAN_ACTIVE so the bot stays quiet and never talks over the human.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models.conversation import Conversation, HandoverState

logger = get_logger(__name__)


class HandoverManager:
    """Reads and mutates the handover state of a conversation.

    All mutations `flush()` but do not `commit()` - the caller owns the
    transaction boundary.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def is_bot_active(self, conversation: Conversation) -> bool:
        """True when the bot is allowed to answer automatically."""
        return conversation.handover_state == HandoverState.BOT_ACTIVE

    async def set_human_active(self, conversation: Conversation) -> None:
        """Pause the bot - a human secretary has taken over."""
        conversation.handover_state = HandoverState.HUMAN_ACTIVE
        conversation.last_human_message_at = datetime.now(UTC)
        await self._session.flush()
        logger.info("handover_set_human_active", conversation_id=str(conversation.id))

    async def set_bot_active(self, conversation: Conversation) -> None:
        """Resume the bot."""
        conversation.handover_state = HandoverState.BOT_ACTIVE
        await self._session.flush()
        logger.info("handover_set_bot_active", conversation_id=str(conversation.id))
