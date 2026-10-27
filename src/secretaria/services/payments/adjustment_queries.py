"""What a cancellation needs to know about the service-change adjustments of a sinal (Task 8).

Plan 2026-10-26 (troca de serviço com sinal pago), Task 8. Kept OUT of `service_change.py`
(which imports `deposit_lifecycle`, so `deposit_lifecycle` cannot import it at module level) and
small on purpose. Two concerns:

* **The money the clinic holds** - ONE definition, `service_change.held_deposit_cents`:
  `amount_cents` + every PAID difference (`increase`/`applied`) - every difference the patient
  asked back (`decrease`/`refund_requested`; that money already has its own
  `service_diff_refund_due` attention item). A difference kept as credit (`kept_credit`,
  `clinic_kept`) stays held; an unpaid / expired / cancelled difference was never money.
  Every cancellation figure (suggested refunds, partial retention, notices, warnings, the
  release offer) uses this number, so the items of one deposit never add up to more than the
  patient paid. `held_total_cents` computes it and stamps it on the deposit instance;
  `held_cents_hint` is the synchronous read the pt-BR texts use (the stamp, else
  `amount_cents`). `deposit_lifecycle.get_deposit_for_appointment` stamps every PAID deposit
  it returns, so a text built from that lookup always quotes the total.
* **Settling the open adjustments when the consultation is cancelled** -
  `settle_open_adjustments`: an unpaid difference Pix is voided (best-effort, the
  `_void_awaiting` rule: a charge the provider says is PAID is never deleted) and the
  adjustment marked `cancelled` - a payment that still lands is an ORPHAN item through the
  webhook (Task 5), never a service change; an unanswered "quer a diferença de volta?" is
  closed (`cancelled`) so a late "Sim" cannot open a second item for money the cancellation
  already counts.

Nothing here refunds money (decision D15) and nothing commits. LGPD: logs carry ids, a status
and an error type only.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import object_session

from secretaria.core.logging import get_logger
from secretaria.models import PixDeposit, Tenant
from secretaria.models.deposit_adjustment import (
    KIND_DECREASE,
    KIND_INCREASE,
    STATUS_APPLIED,
    STATUS_ASKED,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_CANCELLED_VOID_PENDING,
    STATUS_COLLECT_DUE,
    STATUS_REFUND_REQUESTED,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import (
    ATTENTION_OPEN,
    ATTENTION_RESOLVED,
    REASON_SERVICE_DIFF_COLLECT_DUE,
    PaymentAttentionItem,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import attention, registry
from secretaria.services.payments.provider import PaymentStatus

logger = get_logger(__name__)

# A plain (unmapped) attribute on the PixDeposit instance: never persisted, survives an
# expire/refresh of the mapped columns, refreshed by every `held_total_cents` call.
_HELD_ATTR = "_held_cents_snapshot"
# `session.info` key (with the deposit id) of the unpaid differences the last settle cancelled.
_VOIDED_KEY = "voided_difference_cents"

# `settle_open_adjustments` outcomes, per adjustment.
SETTLED_VOIDED = "voided"  # unpaid difference: provider charge cancelled, adjustment cancelled
# provider unknown/void failed: cancelled anyway (`cancelled_void_pending`: the sweep retries)
SETTLED_CANCELLED_UNVOIDED = "cancelled_unvoided"
SETTLED_PAYMENT_IN_FLIGHT = "payment_in_flight"  # the provider says PAID: left to the webhook
SETTLED_QUESTION_CLOSED = "question_closed"  # an unanswered refund question: cancelled
SETTLED_COLLECT_CLOSED = "collect_closed"  # a difference to collect: cancelled, item closed

# Follow-up review I3: how a cancellation closes a "cobrar a diferença" item (the clinic's
# existing resolution fields; no refund is promised - that money was never received).
COLLECT_CLOSED_NOTE = "Consulta cancelada: não cobrar a diferença do sinal desta troca."
COLLECT_CLOSED_BY = "Sistema"


def collect_item_key(provider: str, adjustment_id: UUID) -> str:
    """The dedupe key of the ONE `service_diff_collect_due` item of a `collect_due`
    adjustment (the item has no `deposit_id`: this key is how it is found again)."""
    return attention.attention_dedupe_key(
        provider, f"adjustment:{adjustment_id}", REASON_SERVICE_DIFF_COLLECT_DUE
    )


async def open_collect_adjustments(
    session: AsyncSession, *, tenant_id: UUID, deposit: PixDeposit
) -> list[UUID]:
    """The `collect_due` adjustments of this sinal whose collect item is still OPEN (the clinic
    has not settled the difference yet). Tenant-scoped."""
    ids = list(
        (
            await session.scalars(
                select(DepositAdjustment.id).where(
                    DepositAdjustment.tenant_id == tenant_id,
                    DepositAdjustment.deposit_id == deposit.id,
                    DepositAdjustment.status == STATUS_COLLECT_DUE,
                )
            )
        ).all()
    )
    if not ids:
        return []
    keys = {collect_item_key(deposit.provider, i): i for i in ids}
    open_keys = (
        await session.scalars(
            select(PaymentAttentionItem.dedupe_key).where(
                PaymentAttentionItem.tenant_id == tenant_id,
                PaymentAttentionItem.dedupe_key.in_(list(keys)),
                PaymentAttentionItem.status == ATTENTION_OPEN,
            )
        )
    ).all()
    return [keys[key] for key in open_keys]


async def held_total_cents(session: AsyncSession, deposit: PixDeposit) -> int:
    """What the clinic holds for this sinal (see the module docstring) - stamped on `deposit`
    for the synchronous texts. The formula is `service_change.held_deposit_cents` (one
    definition); imported lazily because `service_change` imports `deposit_lifecycle`."""
    from secretaria.services.payments.service_change import held_deposit_cents

    held = int(await held_deposit_cents(session, deposit))
    setattr(deposit, _HELD_ATTR, held)
    return held


def held_cents_hint(deposit: PixDeposit) -> int:
    """The total stamped by the last `held_total_cents` on this instance, else `amount_cents`
    (a deposit nobody looked up through the session - e.g. built in a pure test)."""
    value = getattr(deposit, _HELD_ATTR, None)
    if isinstance(value, int):
        return value
    return int(deposit.amount_cents or 0)


@dataclass(frozen=True)
class AdjustmentTotals:
    """What the agenda shows of a PAID sinal's adjustments: `held_delta_cents` is what
    `held_deposit_cents` adds to `amount_cents`; `pending_difference_cents` the difference Pix
    still waiting for payment (None when there is none)."""

    held_delta_cents: int = 0
    pending_difference_cents: int | None = None


async def load_adjustment_totals(
    session: AsyncSession, tenant_id: UUID, deposit_ids: Iterable[UUID]
) -> dict[UUID, AdjustmentTotals]:
    """`AdjustmentTotals` of a whole page of deposits in ONE tenant-scoped query (the agenda
    list must not query per row). The held delta follows `service_change.held_deposit_cents`
    exactly (paid increases with a charge, minus decreases asked back); deposits with nothing to
    report are absent."""
    ids = {i for i in deposit_ids if i is not None}
    if not ids:
        return {}
    rows = await session.execute(
        select(
            DepositAdjustment.deposit_id,
            DepositAdjustment.kind,
            DepositAdjustment.status,
            DepositAdjustment.provider_charge_id,
            DepositAdjustment.diff_cents,
        ).where(
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.deposit_id.in_(ids),
            DepositAdjustment.status.in_(
                (STATUS_APPLIED, STATUS_REFUND_REQUESTED, STATUS_AWAITING_PAYMENT)
            ),
        )
    )
    delta: dict[UUID, int] = {}
    pending: dict[UUID, int] = {}
    for deposit_id, kind, status, charge_id, diff in rows.all():
        if kind == KIND_INCREASE and status == STATUS_APPLIED and charge_id is not None:
            delta[deposit_id] = delta.get(deposit_id, 0) + int(diff)
        elif kind == KIND_DECREASE and status == STATUS_REFUND_REQUESTED:
            delta[deposit_id] = delta.get(deposit_id, 0) - int(diff)
        elif status == STATUS_AWAITING_PAYMENT:
            pending[deposit_id] = pending.get(deposit_id, 0) + int(diff)
    return {
        deposit_id: AdjustmentTotals(
            held_delta_cents=delta.get(deposit_id, 0),
            pending_difference_cents=pending.get(deposit_id),
        )
        for deposit_id in set(delta) | set(pending)
    }


async def settle_open_adjustments(
    session: AsyncSession,
    *,
    tenant: Tenant,
    deposit: PixDeposit,
    now: datetime,
    void_payments: bool = True,
    close_questions: bool = True,
) -> dict[str, str]:
    """The consultation of this sinal is cancelled (or released): settle what is still open.

    `void_payments` - each `awaiting_payment` difference: `get_charge` first; PAID -> left
    `awaiting_payment` (the webhook / the expiry sweep settle it; the consultation is no longer
    live, so `apply_paid_adjustment` refuses it and opens an ORPHAN item); otherwise the charge
    is voided when its state is KNOWN unpaid (best-effort) and the adjustment moves to
    `cancelled` - or `cancelled_void_pending` when the provider could not be reached or the
    void failed (the payments sweep retries the void until it succeeds) - a payment that still
    lands then hits a non-awaiting adjustment, which is an ORPHAN item too.
    `close_questions` - each `asked` decrease moves to `cancelled`, and each `collect_due`
    increase (the provider was down: the clinic was to collect the difference) moves to
    `cancelled` with its still-open item closed (`COLLECT_CLOSED_NOTE`) - the consultation is
    over, so the clinic is never told to collect for it. A release that may still be kept
    (`close_questions=False`) leaves both open.

    Returns `{adjustment_id: outcome}`. Idempotent (a second call finds nothing open). Never
    commits, never refunds.
    """
    from secretaria.services.payments.service_change import move_adjustment

    statuses: list[str] = []
    if void_payments:
        statuses.append(STATUS_AWAITING_PAYMENT)
    if close_questions:
        statuses.extend((STATUS_ASKED, STATUS_COLLECT_DUE))
    if not statuses:
        return {}
    rows = (
        await session.scalars(
            select(DepositAdjustment)
            .where(
                DepositAdjustment.tenant_id == tenant.id,
                DepositAdjustment.deposit_id == deposit.id,
                DepositAdjustment.status.in_(statuses),
            )
            .order_by(DepositAdjustment.created_at)
        )
    ).all()
    settled: dict[str, str] = {}
    voided_cents = 0
    for adjustment in rows:
        log = {"tenant_id": str(tenant.id), "adjustment_id": str(adjustment.id)}
        if adjustment.status == STATUS_COLLECT_DUE:
            if await move_adjustment(
                session, adjustment, from_status=STATUS_COLLECT_DUE, to_status=STATUS_CANCELLED,
                now=now,
            ):
                await session.execute(
                    update(PaymentAttentionItem)
                    .where(
                        PaymentAttentionItem.tenant_id == tenant.id,
                        PaymentAttentionItem.dedupe_key
                        == collect_item_key(deposit.provider, adjustment.id),
                        PaymentAttentionItem.status == ATTENTION_OPEN,
                    )
                    .values(
                        status=ATTENTION_RESOLVED,
                        resolved_at=now,
                        resolution_action="other",
                        resolution_note=COLLECT_CLOSED_NOTE,
                        resolved_by_name=COLLECT_CLOSED_BY,
                    )
                    .execution_options(synchronize_session=False)
                )
                settled[str(adjustment.id)] = SETTLED_COLLECT_CLOSED
                logger.info("service_change_collect_closed_by_cancel", **log)
            continue
        if adjustment.status == STATUS_ASKED:
            if await move_adjustment(
                session, adjustment, from_status=STATUS_ASKED, to_status=STATUS_CANCELLED,
                now=now,
            ):
                settled[str(adjustment.id)] = SETTLED_QUESTION_CLOSED
                logger.info("service_change_question_closed_by_cancel", **log)
            continue

        state = await _void_difference(session, tenant, deposit, adjustment, log)
        if state == SETTLED_PAYMENT_IN_FLIGHT:
            settled[str(adjustment.id)] = state
            continue
        # Follow-up B (2026-10-27): a charge that could not be voided (provider down, void
        # refused, account unavailable) is cancelled for us all the same, but marked so the
        # payments sweep retries the void (`service_change._retry_pending_voids`).
        target = (
            STATUS_CANCELLED_VOID_PENDING
            if state == SETTLED_CANCELLED_UNVOIDED and adjustment.provider_charge_id
            else STATUS_CANCELLED
        )
        if await move_adjustment(
            session, adjustment, from_status=STATUS_AWAITING_PAYMENT,
            to_status=target, now=now,
        ):
            settled[str(adjustment.id)] = state
            voided_cents += int(adjustment.diff_cents or 0)
            logger.info("service_change_cancelled_by_cancel", outcome=state, **log)
    if void_payments:
        session.info[(_VOIDED_KEY, deposit.id)] = voided_cents
    return settled


def voided_difference_hint(deposit: PixDeposit) -> int:
    """The unpaid difference(s), in cents, that the last `settle_open_adjustments` of this
    deposit cancelled in the deposit's session (0 when none / never settled / detached): the
    cancel texts tell the patient that Pix no longer counts and the clinic's cancel result says
    it was cancelled. Kept in `session.info`, not on the instance: the session's identity map
    is weak, so a later lookup of the same row may hand back a new instance."""
    session = object_session(deposit)
    value = session.info.get((_VOIDED_KEY, deposit.id)) if session is not None else None
    return value if isinstance(value, int) else 0


async def _void_difference(
    session: AsyncSession,
    tenant: Tenant,
    deposit: PixDeposit,
    adjustment: DepositAdjustment,
    log: dict,
) -> str:
    """Best-effort void of one unpaid difference charge (the `_void_awaiting` rule)."""
    charge_id = adjustment.provider_charge_id
    provider = registry.find_provider(deposit.provider)
    account = await cfg.get_payment_account(session, tenant.id, deposit.provider)
    if not charge_id or provider is None or account is None:
        logger.warning("service_change_void_no_provider", **log)
        return SETTLED_CANCELLED_UNVOIDED
    try:
        state = await provider.get_charge(account, charge_id)
    except Exception as exc:  # noqa: BLE001 - unknown state: never delete, cancel the row
        logger.warning(
            "service_change_void_state_unknown", error_type=type(exc).__name__, **log
        )
        return SETTLED_CANCELLED_UNVOIDED
    if state is not None and state.status is PaymentStatus.PAID:
        logger.warning("service_change_void_skipped_paid", **log)
        return SETTLED_PAYMENT_IN_FLIGHT
    try:
        await provider.cancel_charge(account, charge_id)
    except Exception as exc:  # noqa: BLE001 - the row is cancelled anyway (late pay = ORPHAN)
        logger.warning("service_change_void_failed", error_type=type(exc).__name__, **log)
        return SETTLED_CANCELLED_UNVOIDED
    return SETTLED_VOIDED


__all__ = [
    "COLLECT_CLOSED_NOTE",
    "SETTLED_COLLECT_CLOSED",
    "AdjustmentTotals",
    "SETTLED_CANCELLED_UNVOIDED",
    "SETTLED_PAYMENT_IN_FLIGHT",
    "SETTLED_QUESTION_CLOSED",
    "SETTLED_VOIDED",
    "held_cents_hint",
    "held_total_cents",
    "collect_item_key",
    "load_adjustment_totals",
    "open_collect_adjustments",
    "settle_open_adjustments",
    "voided_difference_hint",
]
