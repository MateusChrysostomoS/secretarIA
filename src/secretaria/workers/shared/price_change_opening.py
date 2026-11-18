"""The first message of a conversation opened from a "the price changed" e-mail (TASK-085 R, D-R14).

3rd round (tasks/TASK-085/sinal-trocas/mapa-u.md §2.3, §4.5). The e-mail links to the Portal with
`lembrete=<appointment_reminders.id>` of a `price_change` row; `workers/portal/open.py`
(`_brain_message_entry_decision` -> `process_brain_message_enter`) recognizes the row's kind and
calls `send_price_change_opening` instead of the usual opening, even with reminders v2 switched
off. This module opens a session, calls `price_change.open_price_notice` (which checks the row is
this patient's and this clinic's, marks it answered, and decides with the price in force NOW -
it commits), then delivers `price_change.price_opening_messages(...)` after the commit on the
patient's channel. Opening the link twice never charges twice (kind "pending").

An unpaid sinal re-quoted ("requoted": the old Pix was voided) gets its new Pix from
`deposit_lifecycle.recharge_after_edit(reason="price_change")`, which sends ONE message opening
with "O valor da sua consulta mudou e o sinal também..." - that is the first message then.

Channel-neutral (`workers/shared/`): never imports `whatsapp/` or `portal/`. U0 (contract slice)
froze the signature; UB2 implemented it. Logs: ids and outcome kinds only.
"""

from datetime import UTC, datetime
from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation, Patient, Tenant
from secretaria.services.payments import deposit_lifecycle, notify, price_change

logger = get_logger(__name__)


def _now() -> datetime:
    """Seam for tests."""
    return datetime.now(UTC)


async def send_price_change_opening(
    reply, tenant: Tenant, *, reminder_id: UUID, redis=None
) -> bool | None:
    """Send the price-change opening for the reply context's patient. True when it was sent,
    False when it no longer applies (row not this patient's/clinic's, a broken link, an older
    version of the consultation, or the consultation is suspended: the caller falls back to the
    usual opening), None when superseded (the conversation moved on before the write: nothing
    sent - the caller checks that right before calling, because once the notice is opened the
    money decision is made and its message must go out).

    The patient is the conversation's own (the authenticated identity the entry resolved), never
    one taken from the link. A delivery failure raises `notify.DeliveryError` (the caller hands
    its claim back); the decision itself stays committed, and opening the link again answers
    "pending"."""
    log = {"tenant_id": str(tenant.id), "reminder_id": str(reminder_id)}
    async with async_session_factory() as session:
        current = await session.get(Tenant, tenant.id)
        conversation = await session.get(Conversation, reply.conversation_id)
        if (
            current is None
            or conversation is None
            or conversation.tenant_id != tenant.id
            or conversation.patient_id is None
        ):
            logger.info("price_change_opening_skipped", why="no_conversation", **log)
            return False
        patient = await session.get(Patient, conversation.patient_id)
        if patient is None or patient.tenant_id != tenant.id:
            logger.info("price_change_opening_skipped", why="no_patient", **log)
            return False
        opening = await price_change.open_price_notice(
            session,
            tenant=current,
            reminder_id=reminder_id,
            patient_id=patient.id,
            now=_now(),
        )
        if opening.kind == "not_found":
            return False
        # The opening committed or rolled back: the rows this session holds may be expired -
        # load them again (an async session never lazy-loads).
        await session.refresh(current)
        await session.refresh(patient)
        messages = price_change.price_opening_messages(opening, tenant=current)
        if messages:
            await notify.deliver(
                session, current, patient, messages, conversation_id=reply.conversation_id
            )
    if opening.kind == "requoted" and opening.appointment_id is not None:
        # After the commit, own session; never raises (it tells the patient itself, also when
        # the new Pix could not be created).
        await deposit_lifecycle.recharge_after_edit(
            tenant_id=tenant.id,
            appointment_id=opening.appointment_id,
            old_amount_cents=opening.old_amount_cents,
            redis=redis,
            reason="price_change",
        )
    logger.info("price_change_opening_sent", kind=opening.kind, **log)
    return True
