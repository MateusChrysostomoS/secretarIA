"""Reading and resolving attention items for the clinic (spec sections 6.5, 10A.2).

`payment_attention_items` are written by `attention.open_attention_item` (F1). This module is the
clinic's side: list them (tenant-scoped, a whitelist of fields, the patient's DISPLAY NAME read
from `patients` at that moment - never copied into the item) and resolve them with a mandatory
note. Resolving records the clinic's decision; it moves no money and calls no provider (D15).

The one state change it does cause on money rows is the honest one the spec asks for
(10A.6): `cancelado_reembolsado` is written ONLY when the clinic itself marks an item
`refunded_in_provider_panel`, and only for the reasons where the whole deposit really was given
back for a consultation that is not booked (late slot taken, paid-then-cancelled, refund due by
policy, a promotion that never completed).
"""

import base64
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Patient, PixDeposit
from secretaria.models.payment_attention_item import (
    ATTENTION_OPEN,
    ATTENTION_RESOLVED,
    REASON_LATE_SLOT_TAKEN,
    REASON_PAID_THEN_CANCELLED,
    REASON_PROMOTION_FAILED,
    REASON_REFUND_DUE_POLICY,
    REASON_RELEASED_REFUND_DUE,
    RESOLUTION_ACTIONS,
    PaymentAttentionItem,
)
from secretaria.models.pix_deposit import (
    FULFILLMENT_NEEDS_ATTENTION,
    FULFILLMENT_RESOLVED,
    PixDepositStatus,
)

RESOLVED_VISIBLE_DAYS = 90
NOTE_MIN_CHARS = 10
NOTE_MAX_CHARS = 500
NAME_MAX_CHARS = 80
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

# The clinic-facing sentence of each reason (spec 6.5, pt-BR). `late_slot_taken` is worded for
# the two ways it is raised: a payment after the Pix window AND a slot that stopped being free
# inside it (a doctor's manual Google event).
REASON_TEXT = {
    "late_slot_taken": "Pagamento recebido, mas o horário escolhido já não está livre.",
    "paid_then_cancelled": "Sinal pago, consulta cancelada.",
    "refund_due_policy": "Cancelamento dentro da política de devolução: valor sugerido {value}.",
    "duplicate_payment": "Pagamento em duplicidade.",
    "amount_under": "Valor pago menor que o sinal.",
    "amount_over": "Valor pago maior que o sinal.",
    "orphan_payment": "Pagamento sem consulta correspondente.",
    "reference_mismatch": "Pagamento com referência diferente da esperada.",
    "promotion_failed": "Pago, mas não conseguimos confirmar a consulta.",
    "refunded_externally": "O provedor informa que este pagamento foi estornado.",
    "released_refund_due": "Horário liberado pela clínica: devolver o sinal ao paciente.",
}

# Resolving with "refunded in the provider's panel" closes the money story of the deposit only
# for these reasons (the consultation is not booked and the deposit went back in full).
_REFUND_RECORDING_REASONS = frozenset(
    {
        REASON_LATE_SLOT_TAKEN,
        REASON_PAID_THEN_CANCELLED,
        REASON_REFUND_DUE_POLICY,
        REASON_PROMOTION_FAILED,
        REASON_RELEASED_REFUND_DUE,
    }
)

_CPF = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


class ResolveRefused(Exception):
    """A resolve request that cannot be accepted. `code` is symbolic; nothing the clinic typed
    is ever put in it (a note may contain exactly the personal data that was refused)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def money_text(cents: int | None) -> str:
    if cents is None:
        return ""
    reais, rest = divmod(cents, 100)
    return f"R$ {reais:,}".replace(",", ".") + f",{rest:02d}"


def reason_text(reason: str, suggested_refund_cents: int | None) -> str:
    template = REASON_TEXT.get(reason, "Pagamento que precisa de decisão.")
    return template.format(value=money_text(suggested_refund_cents))


def check_resolution_input(action: object, note: object, resolved_by_name: object) -> tuple:
    """Validate a resolve body BY HAND (a Pydantic error would echo the note, and a note is the
    one field where the clinic may type a CPF by mistake). Returns the cleaned triple."""
    if action not in RESOLUTION_ACTIONS:
        raise ResolveRefused("invalid_action")
    if not isinstance(note, str):
        raise ResolveRefused("note_required")
    text = note.strip()
    if len(text) < NOTE_MIN_CHARS:
        raise ResolveRefused("note_too_short")
    if len(text) > NOTE_MAX_CHARS:
        raise ResolveRefused("note_too_long")
    if _EMAIL.search(text) or _CPF.search(text):
        raise ResolveRefused("note_has_personal_data")
    if not isinstance(resolved_by_name, str) or not resolved_by_name.strip():
        raise ResolveRefused("resolved_by_required")
    name = resolved_by_name.strip()
    if len(name) > NAME_MAX_CHARS:
        raise ResolveRefused("resolved_by_too_long")
    return str(action), text, name


def _encode_cursor(sort_at: datetime, item_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"{sort_at.isoformat()}|{item_id}".encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if not cursor:
        return None
    try:
        stamp, _, item_id = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
        at = datetime.fromisoformat(stamp)
        return (at if at.tzinfo else at.replace(tzinfo=UTC)), UUID(item_id)
    except (ValueError, UnicodeDecodeError):
        return None


@dataclass(frozen=True)
class ItemRow:
    item: PaymentAttentionItem
    patient_name: str | None


@dataclass(frozen=True)
class ItemPage:
    open_count: int
    rows: list[ItemRow]
    next_cursor: str | None


async def open_count(session: AsyncSession, tenant_id: UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(PaymentAttentionItem)
            .where(
                PaymentAttentionItem.tenant_id == tenant_id,
                PaymentAttentionItem.status == ATTENTION_OPEN,
            )
        )
    ) or 0


async def list_items(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    status: str = ATTENTION_OPEN,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    now: datetime | None = None,
) -> ItemPage:
    """One page of this clinic's items, newest first. `resolved` shows only the last 90 days."""
    now = now or datetime.now(UTC)
    limit = max(1, min(limit, MAX_LIMIT))
    sort_col = (
        PaymentAttentionItem.detected_at
        if status == ATTENTION_OPEN
        else PaymentAttentionItem.resolved_at
    )
    stmt = (
        select(PaymentAttentionItem, Patient.name)
        .outerjoin(
            Patient,
            (Patient.id == PaymentAttentionItem.patient_id) & (Patient.tenant_id == tenant_id),
        )
        .where(PaymentAttentionItem.tenant_id == tenant_id, PaymentAttentionItem.status == status)
    )
    if status == ATTENTION_RESOLVED:
        stmt = stmt.where(sort_col >= now - timedelta(days=RESOLVED_VISIBLE_DAYS))
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        at, item_id = decoded
        stmt = stmt.where(
            (sort_col < at) | ((sort_col == at) & (PaymentAttentionItem.id < item_id))
        )
    stmt = stmt.order_by(sort_col.desc(), PaymentAttentionItem.id.desc()).limit(limit + 1)
    fetched = (await session.execute(stmt)).all()
    page, more = fetched[:limit], len(fetched) > limit
    rows = [ItemRow(item=row[0], patient_name=(row[1] or "").strip() or None) for row in page]
    next_cursor = None
    if more and rows:
        last = rows[-1].item
        sort_at = last.detected_at if status == ATTENTION_OPEN else last.resolved_at
        next_cursor = _encode_cursor(sort_at, last.id)
    total_open = await open_count(session, tenant_id)
    return ItemPage(open_count=total_open, rows=rows, next_cursor=next_cursor)


async def get_item(
    session: AsyncSession, tenant_id: UUID, item_id: UUID
) -> ItemRow | None:
    row = (
        await session.execute(
            select(PaymentAttentionItem, Patient.name)
            .outerjoin(
                Patient,
                (Patient.id == PaymentAttentionItem.patient_id)
                & (Patient.tenant_id == tenant_id),
            )
            .where(
                PaymentAttentionItem.id == item_id, PaymentAttentionItem.tenant_id == tenant_id
            )
        )
    ).first()
    if row is None:
        return None
    return ItemRow(item=row[0], patient_name=(row[1] or "").strip() or None)


async def resolve_item(
    session: AsyncSession,
    tenant_id: UUID,
    item_id: UUID,
    *,
    action: str,
    note: str,
    resolved_by_name: str,
    now: datetime | None = None,
) -> str:
    """Resolve one item of THIS clinic. Returns `resolved`, `already_resolved` or `not_found`.

    Atomic: `UPDATE ... WHERE status = 'open' RETURNING`, so a second click (or a second manager)
    never overwrites the first decision. Commits.
    """
    now = now or datetime.now(UTC)
    claimed = await session.execute(
        update(PaymentAttentionItem)
        .where(
            PaymentAttentionItem.id == item_id,
            PaymentAttentionItem.tenant_id == tenant_id,
            PaymentAttentionItem.status == ATTENTION_OPEN,
        )
        .values(
            status=ATTENTION_RESOLVED,
            resolved_at=now,
            resolution_action=action,
            resolution_note=note,
            resolved_by_name=resolved_by_name,
        )
        .returning(PaymentAttentionItem.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.scalar_one_or_none() is None:
        existing = await session.scalar(
            select(PaymentAttentionItem.id).where(
                PaymentAttentionItem.id == item_id, PaymentAttentionItem.tenant_id == tenant_id
            )
        )
        return "already_resolved" if existing is not None else "not_found"

    item = await session.get(PaymentAttentionItem, item_id)
    await session.refresh(item)
    await _close_deposit_story(session, tenant_id, item, action)
    # TASK-052: a released PAID slot's refund item. "Refunded in the provider's panel" asks the
    # patient "Recebido / Não recebido"; any other decision ends the clinic's notices. Lazy
    # import: services/release_offer.py imports this package.
    from secretaria.services import release_offer

    await release_offer.on_refund_recorded(session, tenant_id=tenant_id, item=item, now=now)
    await session.commit()
    return "resolved"


async def _close_deposit_story(
    session: AsyncSession, tenant_id: UUID, item: PaymentAttentionItem, action: str
) -> None:
    """When no other item of the deposit is open the deposit is `resolved`; a refund the clinic
    reports having made in the provider's panel is the ONLY thing that records one."""
    if item.deposit_id is None:
        return
    deposit = await session.get(PixDeposit, item.deposit_id)
    if deposit is None or deposit.tenant_id != tenant_id:
        return
    still_open = await session.scalar(
        select(func.count())
        .select_from(PaymentAttentionItem)
        .where(
            PaymentAttentionItem.tenant_id == tenant_id,
            PaymentAttentionItem.deposit_id == deposit.id,
            PaymentAttentionItem.status == ATTENTION_OPEN,
        )
    )
    if not still_open and deposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION:
        deposit.fulfillment_status = FULFILLMENT_RESOLVED
    if (
        action == "refunded_in_provider_panel"
        and item.reason in _REFUND_RECORDING_REASONS
        and deposit.status == PixDepositStatus.PAID
    ):
        deposit.status = PixDepositStatus.CANCELLED_REFUNDED
        deposit.resolved_at = item.resolved_at
        deposit.refunded_amount_cents = (
            item.suggested_refund_cents
            or item.paid_amount_cents
            or deposit.paid_amount_cents
            or deposit.amount_cents
        )
