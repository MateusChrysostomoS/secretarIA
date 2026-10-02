"""open - split out of workers/tasks.py (TASK-023)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    ConsentEvent,
    Conversation,
    Message,
    MessageDirection,
    Patient,
    Tenant,
)
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
)
from secretaria.workers.orchestrator import (
    _send_bot_reply,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.db import (
    _conversation_message_count,
    _get_or_create_conversation,
)
from secretaria.workers.shared.greeting import (
    _first_contact_reply,
)
from secretaria.workers.shared.jobs import (
    _claim_event,
    _release_event,
)

logger = get_logger(__name__)


def _open_ledger_key(tenant_id: UUID, external_id: str) -> str:
    """The idempotency key for "this visitor has already been greeted unasked".

    Keyed on (tenant, external_id) rather than on a request id, because the
    thing that must happen at most once is per VISITOR, not per call: brain-api
    fires this on every `POST /patient-access/pending` that creates a visit, and
    a browser refresh that resumes the same pending visit arrives with the same
    `external_id`.

    `ProcessedEvent.event_id` is VARCHAR(128) and this key is at most
    19 + 36 + 1 + 64 = 120 characters — `external_id` is bounded at 64 by both
    the route's schema and `Patient.external_id`'s column, so the key cannot
    outgrow the column.
    """
    return f"brain_message_open:{tenant_id}:{external_id}"

async def _portal_conversation_has(
    tenant_id: UUID, external_id: str, *, direction: MessageDirection | None = None
) -> bool:
    """Does this Portal conversation already carry a message (of `direction`)?

    One indexed read on the same three-link scope every Brain-Message query
    uses. Two questions, one query, because they differ only in the predicate:

      * `direction=OUTBOUND` — did the greeting actually LAND? On this channel
        the row IS the delivery (`services/channel_sender.py`), so an outbound
        row is the only honest evidence: `_send_bot_reply` swallows its own
        send failures by design and cannot report one back.
      * `direction=None` — has this thread begun AT ALL? Which is what decides
        whether an unsolicited greeting is still appropriate.
    """
    stmt = (
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(Patient, Patient.id == Conversation.patient_id)
        .where(
            Conversation.tenant_id == tenant_id,
            Patient.tenant_id == tenant_id,
            Patient.channel == CHANNEL_BRAIN_MESSAGE,
            Patient.external_id == external_id,
        )
    )
    if direction is not None:
        stmt = stmt.where(Message.direction == direction)
    async with async_session_factory() as session:
        return await session.scalar(stmt.limit(1)) is not None

async def _open_brain_message_conversation(
    *,
    tenant_id: UUID,
    external_id: str,
    patient_name: str | None,
) -> _ReplyContext | None:
    """Create the conversation, if it is new, and decide the opening turn.

    The Brain-Message sibling of `_persist_brain_message_inbound`, with the one
    difference that gives this whole route its reason to exist: it writes no
    inbound `Message`. Patient row, consent event and conversation are created
    exactly as that function creates them, so the two doors produce the same
    rows for the same visitor.

    Returns None — and the caller sends nothing — when there is nobody to greet
    (unknown tenant) or nothing to open (the conversation already has history).
    That second case is the contract's `exists`: a conversation that has already
    started must never receive an unsolicited greeting on top of it.
    """
    async with async_session_factory() as session:
        try:
            async with session.begin():
                tenant = await session.get(Tenant, tenant_id)
                if tenant is None:
                    logger.error("brain_message_open_tenant_unresolved", tenant_id=str(tenant_id))
                    return None

                # `Tenant.is_active` is deliberately NOT consulted, for the
                # reason spelled out in `_persist_brain_message_inbound`: it is
                # the WhatsApp go-live flag and this channel must not inherit
                # a transport gate. `_send_bot_reply` still fails closed on the
                # authoritative subscription/secretaria entitlement.
                patient = await session.scalar(
                    select(Patient).where(
                        Patient.tenant_id == tenant.id,
                        Patient.channel == CHANNEL_BRAIN_MESSAGE,
                        Patient.external_id == external_id,
                    )
                )
                if patient is None:
                    patient = Patient(
                        tenant_id=tenant.id,
                        channel=CHANNEL_BRAIN_MESSAGE,
                        external_id=external_id,
                        name=patient_name,
                    )
                    session.add(patient)
                    await session.flush()
                    session.add(
                        ConsentEvent(
                            tenant_id=tenant.id,
                            wa_id=external_id,
                            kind="first_contact_service",
                            legal_basis=(
                                "TODO_LAWYER: execução de contrato vs consentimento — "
                                "pendencias_advogado.md item pendente"
                            ),
                        )
                    )
                elif patient_name and not patient.name:
                    patient.name = patient_name

                conversation = await _get_or_create_conversation(session, tenant, patient)
                # THE RACE THIS READ DOES NOT CLOSE, stated where it lives: the
                # ledger claim serialises two `open` jobs against each other,
                # and the unique constraints on `patients` and `conversations`
                # serialise two jobs that both try to CREATE them. What neither
                # covers is an `open` overlapping a genuinely concurrent FIRST
                # inbound on a conversation row that already exists with no
                # messages: under READ COMMITTED both can read zero before
                # either commits, and the patient sees the greeting twice.
                # `process_brain_message_open` re-reads this immediately before
                # sending, which closes everything except a sub-commit overlap
                # (the inbound path writes its `Message` inside the SAME
                # transaction as its decision, so it becomes visible the moment
                # it commits). Closing the remainder needs a lock both paths
                # take — see the CHECKPOINT; it is not worth putting one in the
                # hot path of every WhatsApp turn for a duplicated greeting.
                prior_messages = await _conversation_message_count(session, conversation.id)
                if prior_messages:
                    logger.info(
                        "brain_message_open_already_started",
                        tenant_id=str(tenant_id),
                        conversation_id=str(conversation.id),
                    )
                    return None

                # `patient.lgpd_accepted_at` is not branched on, unlike in
                # `_route_inbound_turn`: a conversation with zero messages and a
                # consented patient is a state no path produces (consent is only
                # ever written alongside messages), and treating it as a first
                # contact is harmless if it ever appears — the consent notice is
                # idempotent and, on this channel, the brain-api probe decides
                # what actually goes out anyway.
                return _first_contact_reply(
                    tenant=tenant,
                    conversation_id=conversation.id,
                    patient_ref=external_id,
                    channel=CHANNEL_BRAIN_MESSAGE,
                )
        except IntegrityError:
            # A concurrent open (or a first inbound) raced us to the
            # (tenant, channel, external_id) constraint. Whoever won is
            # greeting this visitor; this call says nothing.
            logger.info("brain_message_open_race", tenant_id=str(tenant_id))
            return None

async def process_brain_message_open(
    ctx: dict,
    tenant_id: str,
    external_id: str,
    patient_name: str | None = None,
) -> None:
    """arq job: greet a Portal visitor who has not said anything yet.

    Idempotent in two independent layers, because one is not enough:

      * the ROUTE refuses (200 `exists`) when the conversation already carries
        a message, which is the cheap answer for a returning visitor and costs
        no job at all;
      * this LEDGER claim is what makes two concurrent calls — the same link
        opened twice, a refresh racing the first request — produce one
        greeting. Both would pass the route's read; only one can insert the key.

    The claim is given back when nothing was actually said, so a greeting lost
    to a send failure does not become a permanently empty chat.
    """
    tenant_uuid = UUID(tenant_id)
    key = _open_ledger_key(tenant_uuid, external_id)
    if not await _claim_event(key):
        logger.info(
            "brain_message_open_already_claimed",
            tenant_id=tenant_id,
            external_id=external_id,
        )
        return

    reply = await _open_brain_message_conversation(
        tenant_id=tenant_uuid,
        external_id=external_id,
        patient_name=patient_name,
    )
    if reply is not None:
        # Asked AGAIN, after the decision transaction closed and immediately
        # before speaking. The decision and the send are necessarily in
        # different transactions (an HTTP probe and a model-free greeting have
        # no business inside an open one), and in that gap a genuinely
        # concurrent FIRST inbound may have started the thread. The inbound
        # path commits its `Message` together with its own decision, so this
        # read sees it the instant it lands — which narrows the duplicate
        # greeting to an overlap shorter than one commit. See the comment at
        # `_open_brain_message_conversation`'s count for what it still does
        # not close.
        if await _portal_conversation_has(tenant_uuid, external_id):
            logger.info(
                "brain_message_open_superseded",
                tenant_id=tenant_id,
                external_id=external_id,
            )
            return
        await _send_bot_reply(reply, redis=ctx.get("redis"))

    if await _portal_conversation_has(
        tenant_uuid, external_id, direction=MessageDirection.OUTBOUND
    ):
        logger.info(
            "brain_message_open_greeted",
            tenant_id=tenant_id,
            external_id=external_id,
        )
        return
    # Nothing reached the patient: an unknown tenant, an unentitled clinic, a
    # send that failed. Hand the key back rather than sealing the silence in.
    await _release_event(key, event="brain_message_open_release_failed")
    logger.warning(
        "brain_message_open_nothing_sent",
        tenant_id=tenant_id,
        external_id=external_id,
    )
