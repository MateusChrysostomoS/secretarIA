"""The Portal's one-time card after a released PAID slot's start (TASK-052).

Portal-only on purpose: it lives under `workers/portal/` because it writes a Portal card and
looks the patient up by the Portal channel, and neutral code in `workers/shared/` must not branch
on the channel (tests/test_channel_branches.py). The taps themselves are channel-neutral and stay
in `workers/shared/release_offer_actions.py`.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Patient
from secretaria.models.deposit_release_offer import KEEP_FROM, DepositReleaseOffer
from secretaria.services import release_offer as ro
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender

logger = get_logger(__name__)


async def offer_card_after_start(
    tenant_id: UUID, external_id: str, *, now: datetime | None = None
) -> bool:
    """Portal entry after S with the offer still unanswered: ONE card with Remarcar / Cancelar."""
    now = now or datetime.now(UTC)
    async with async_session_factory() as session:
        patient = await session.scalar(
            select(Patient).where(
                Patient.tenant_id == tenant_id,
                Patient.channel == CHANNEL_BRAIN_MESSAGE,
                Patient.external_id == external_id,
            )
        )
        if patient is None:
            return False
        offer = await session.scalar(
            select(DepositReleaseOffer)
            .where(
                DepositReleaseOffer.tenant_id == tenant_id,
                DepositReleaseOffer.patient_id == patient.id,
                DepositReleaseOffer.deposit_paid.is_(True),
                DepositReleaseOffer.decision.in_(KEEP_FROM),
                DepositReleaseOffer.slot_start_at <= now,
                DepositReleaseOffer.expires_at > now,
                DepositReleaseOffer.after_start_card_at.is_(None),
            )
            .order_by(DepositReleaseOffer.released_at.desc())
            .limit(1)
        )
        if offer is None or offer.conversation_id is None:
            return False
        conversation_id, released_id = offer.conversation_id, offer.released_appointment_id
        claimed = await session.execute(
            update(DepositReleaseOffer)
            .where(
                DepositReleaseOffer.id == offer.id,
                DepositReleaseOffer.after_start_card_at.is_(None),
            )
            .values(after_start_card_at=now)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        if claimed.rowcount != 1:
            return False
        sender = BrainMessageSender(conversation_id=conversation_id, session=session)
        await sender.send_buttons(
            to=external_id,
            body=ro.AFTER_START_TEXT,
            buttons=ro.keep_fallback_buttons(released_id),
        )
        await session.commit()
    logger.info("deposit_release_offer_after_start_card", tenant_id=str(tenant_id))
    return True
