"""service_change_actions - the patient side of a SERVICE change on a paid sinal (2026-10-26).

Spec: docs/superpowers/specs/2026-10-26-troca-servico-diferenca-sinal-design.md. Channel-neutral:
WhatsApp and the Portal both reach this through `flow_runner._apply_flow_result` (the reply to a
confirmed edit) and `actions._handle_action_button` (a tap on the refund question).

* `service_change_bubbles` - after the commit, the edit's reply follows what the money decided
  (`service_change.request_service_change`): `blocked` -> one line, nothing changed;
  `awaiting_payment` -> the value of the difference, "só muda depois do pagamento" and the Pix
  copia-e-cola at the end of the SAME message (fenced, see `payments/pix_message`);
  `asked` -> the "Pronto" recap and the
  refund question with two fixed ACTION buttons; `applied_now` -> the recap as usual.
* `handle_service_diff_tap` - "Sim, quero de volta" / "Não, abater no valor": honoured only for
  the patient of that sinal, in the tapping conversation's clinic; never the LLM, never money
  moved by us (a "sim" opens an attention item - the clinic returns it in the provider's panel).

One message per event: the paid/expired notices of a difference are the default hooks of
`service_change` (after the webhook / the sweep); nothing here repeats them.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from secretaria.ai.formatter import TextBubble
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation, PixDeposit, Tenant
from secretaria.models.deposit_adjustment import DepositAdjustment
from secretaria.services.flow_router import ActionButtonsBubble
from secretaria.services.payments import service_change
from secretaria.services.payments.service_change import ServiceChangeResult
from secretaria.services.tenant_config import get_waba_token
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.sender import _reply_sender

logger = get_logger(__name__)


def service_change_bubbles(
    bubbles: list, outcome: ServiceChangeResult, *, new_service: str, tenant
) -> list:
    """The reply of a confirmed paid service change. `bubbles` is the router's: the "Pronto"
    recap first, then the returning-patient menu."""
    recap, rest = (bubbles[0], list(bubbles[1:])) if bubbles else (None, [])
    if outcome.kind == "blocked":
        return [TextBubble(body=service_change.blocked_text(outcome.reason)), *rest]
    if outcome.kind == "awaiting_payment":
        ttl = (
            getattr(tenant, "deposit_hold_ttl_minutes", None)
            or service_change.DEFAULT_PIX_TTL_MINUTES
        )
        # ONE message: the explanation with the (fenced) copia-e-cola at its end.
        head = service_change.difference_pix_text(
            new_service, outcome.diff_cents, outcome.pix_copy_paste, ttl
        )
        return [TextBubble(body=head), *rest]
    if outcome.kind == "asked" and outcome.adjustment_id is not None:
        question = ActionButtonsBubble(
            body=service_change.refund_question_text(new_service, outcome.diff_cents),
            buttons=service_change.refund_question_buttons(outcome.adjustment_id),
        )
        return [*([recap] if recap is not None else []), question]
    if outcome.kind == "applied_now" and outcome.decision == service_change.DECISION_COLLECT:
        # Follow-up B: no Pix could be created - the change stands; the clinic collects.
        note = TextBubble(body=service_change.collect_due_text(new_service, outcome.diff_cents))
        return [*([recap] if recap is not None else []), note, *rest]
    return list(bubbles)


async def handle_service_diff_tap(reply: _ReplyContext, action: str, adjustment_id: UUID) -> None:
    """A tap on the refund question. Answers with ONE text; idempotent (a second tap says the
    question was already answered). A tap on someone else's question changes nothing."""
    if reply.conversation_id is None:
        return
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return
        tenant_id = tenant.id
        client = _reply_sender(reply, tenant, await get_waba_token(session, tenant_id))
        if client is None:
            logger.error("service_diff_tap_no_sender", tenant_id=str(tenant_id))
            return
        adjustment = await session.scalar(
            select(DepositAdjustment).where(
                DepositAdjustment.id == adjustment_id, DepositAdjustment.tenant_id == tenant_id
            )
        )
        deposit = (
            await session.get(PixDeposit, adjustment.deposit_id) if adjustment is not None else None
        )
        if (
            adjustment is None
            or deposit is None
            or deposit.tenant_id != tenant_id
            or deposit.patient_id is None
            or deposit.patient_id != conversation.patient_id
        ):
            outcome, diff_cents = "not_found", 0
        else:
            diff_cents = adjustment.diff_cents
            outcome = await service_change.answer_refund_question(
                session,
                tenant_id=tenant_id,
                adjustment_id=adjustment.id,
                accept=action == service_change.ACTION_REFUND_YES,
                now=datetime.now(UTC),
            )
            await session.commit()
        await client.send_text_message(
            to=reply.patient_ref, body=service_change.refund_answer_text(outcome, diff_cents)
        )
    logger.info(
        "service_diff_refund_tap",
        tenant_id=str(tenant_id),
        adjustment_id=str(adjustment_id),
        outcome=outcome,
    )
