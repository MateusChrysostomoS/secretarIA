"""Before a "Remarcar" booking reuses a released slot's PAID deposit, read the charge at the
payment provider (TASK-052, owner decision 2026-10-12). READ ONLY - the platform never refunds
(D15).

* The clinic already marked the refund      -> there is no carrying offer: new Pix, no call.
* Provider: paid and no refund listed       -> REUSABLE: the booking carries the deposit.
* Provider: refunded / refund requested / in progress, or any refund listed -> REFUNDED: new Pix.
* Anything else - no account, no charge id, a deposit of another clinic, timeout, network or HTTP
  error, an unexpected status or shape      -> UNKNOWN = FAIL-SAFE: new Pix. The patient may pay
  twice (the first deposit stays paid); the old deposit becomes a refund the clinic owes, with the
  "clinic_carry_refused" notice.

The answer is stored ONCE on the offer (`release_offer.record_carry_check`): two confirm clicks
get the same answer and only one booking carries (`claim_carry_over` is a guarded UPDATE). No
transaction is open while the provider is asked, and a booking never waits more than
CHECK_TIMEOUT_SECONDS for it. Residual risk ACCEPTED by the owner: a refund made in the Asaas
panel in the seconds between this read and the booking. Logs carry ids and the outcome only.
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation, PixDeposit
from secretaria.models.deposit_release_offer import (
    CARRY_CHECK_REFUNDED,
    CARRY_CHECK_REUSABLE,
    CARRY_CHECK_UNKNOWN,
)
from secretaria.services import release_offer, tenant_config as cfg
from secretaria.services.payments import registry
from secretaria.services.payments.provider import ChargeState, PaymentStatus

logger = get_logger(__name__)

CHECK_TIMEOUT_SECONDS = 4.0


def verdict_from(state: ChargeState) -> str:
    """The provider's answer -> REUSABLE / REFUNDED / UNKNOWN (pure)."""
    if state.status is PaymentStatus.REFUNDED or state.refunds == "some":
        return CARRY_CHECK_REFUNDED
    if state.status is PaymentStatus.PAID and state.refunds in ("absent", "none"):
        return CARRY_CHECK_REUSABLE
    return CARRY_CHECK_UNKNOWN


async def _account(session, tenant_id: UUID, provider: str):
    """Seam: THIS clinic's decrypted account (the single decrypt point)."""
    return await cfg.get_payment_account(session, tenant_id, provider)


async def ensure_checked(
    conversation_id: UUID | None, *, now: datetime | None = None
) -> str | None:
    """The stored provider answer for this conversation's "Remarcar", asking the provider the
    first time. None when there is nothing to carry. Never raises: an internal failure is logged
    and answers None - nothing is carried (the booking charges as usual) and B/E still make the
    old deposit due."""
    if conversation_id is None:
        return None
    try:
        return await _ensure_checked(conversation_id, now)
    except Exception as exc:  # noqa: BLE001 - a booking never fails because of this check
        logger.error("release_carry_check_failed", error_type=type(exc).__name__)
        return None


async def _ensure_checked(conversation_id: UUID, now: datetime | None) -> str | None:
    async with async_session_factory() as session:
        tenant_id = await session.scalar(
            select(Conversation.tenant_id).where(Conversation.id == conversation_id)
        )
        if tenant_id is None:
            return None
        offer = await release_offer.carrying_offer(
            session, tenant_id=tenant_id, conversation_id=conversation_id
        )
        if offer is None:
            return None
        if offer.carry_check == CARRY_CHECK_REUSABLE and offer.carry_checked_at is not None:
            moment = now or datetime.now(UTC)
            checked = offer.carry_checked_at
            checked = checked.replace(tzinfo=UTC) if checked.tzinfo is None else checked
            if checked <= moment - release_offer.CARRY_CHECK_MAX_AGE:
                # Review of TASK-052: the booking this answer was read for never completed and
                # the clinic may have refunded since. Ask the provider again.
                await release_offer.reset_stale_carry_check(
                    session, tenant_id=tenant_id, offer_id=offer.id, now=moment
                )
                await session.commit()
                await session.refresh(offer)
        if offer.carry_check is not None:
            return offer.carry_check
        offer_id = offer.id
        deposit = await session.scalar(
            select(PixDeposit).where(
                PixDeposit.id == offer.deposit_id, PixDeposit.tenant_id == tenant_id
            )
        )
        provider_name = deposit.provider if deposit is not None else None
        charge_id = (
            (deposit.provider_charge_id or deposit.asaas_payment_id)
            if deposit is not None
            else None
        )
        account = await _account(session, tenant_id, provider_name) if provider_name else None
    # The session is closed: no transaction (and no row lock) waits on the provider.
    outcome = await _ask_provider(tenant_id, provider_name, account, charge_id)
    async with async_session_factory() as session:
        stored = await release_offer.record_carry_check(
            session, tenant_id=tenant_id, offer_id=offer_id, outcome=outcome, now=now
        )
        await session.commit()
    return stored


async def check_offer_deposit(session, offer) -> str:
    """Read-only provider check of an offer's deposit, for the clinic's acceptance of "Manter"
    after the refund already fell due (review of TASK-052): REUSABLE only when Asaas says the
    deposit is still paid and not refunded; anything else - refunded, unreachable, unexpected - is
    not reusable. Never raises and never refunds."""
    try:
        deposit = await session.scalar(
            select(PixDeposit).where(
                PixDeposit.id == offer.deposit_id, PixDeposit.tenant_id == offer.tenant_id
            )
        )
        provider_name = deposit.provider if deposit is not None else None
        charge_id = (
            (deposit.provider_charge_id or deposit.asaas_payment_id)
            if deposit is not None
            else None
        )
        account = (
            await _account(session, offer.tenant_id, provider_name) if provider_name else None
        )
        return await _ask_provider(offer.tenant_id, provider_name, account, charge_id)
    except Exception as exc:  # noqa: BLE001 - FAIL-SAFE: not reusable
        logger.error("release_offer_deposit_check_failed", error_type=type(exc).__name__)
        return CARRY_CHECK_UNKNOWN


async def _ask_provider(tenant_id: UUID, provider_name, account, charge_id) -> str:
    provider = registry.find_provider(provider_name) if provider_name else None
    if provider is None or account is None or not charge_id or account.tenant_id != tenant_id:
        logger.warning("release_carry_check_unverifiable", tenant_id=str(tenant_id))
        return CARRY_CHECK_UNKNOWN
    try:
        state = await asyncio.wait_for(
            provider.get_charge(account, charge_id), timeout=CHECK_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - timeout, network, HTTP, parsing: FAIL-SAFE
        logger.warning(
            "release_carry_check_provider_failed",
            tenant_id=str(tenant_id),
            error_type=type(exc).__name__,
        )
        return CARRY_CHECK_UNKNOWN
    if not isinstance(state, ChargeState):
        logger.warning("release_carry_check_unexpected_answer", tenant_id=str(tenant_id))
        return CARRY_CHECK_UNKNOWN
    outcome = verdict_from(state)
    logger.info("release_carry_check", tenant_id=str(tenant_id), outcome=outcome)
    return outcome
