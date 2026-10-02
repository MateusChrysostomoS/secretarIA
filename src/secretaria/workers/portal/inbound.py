"""inbound - split out of workers/tasks.py (TASK-023)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.attachments import attachment_body
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    ConsentEvent,
    Conversation,
    Message,
    MessageDirection,
    Patient,
    ProcessedEvent,
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
    _event_already_processed,
)
from secretaria.workers.turn_router import (
    _route_inbound_turn,
)

logger = get_logger(__name__)


# How far back a Brain-Message tap may reach: the option must be on one of
# this many most recent cards (reply buttons or lists) of the conversation.
# Generous against a patient scrolling up, tight against a stale card driving
# a flow that has long moved on; WhatsApp itself lets a patient tap any old
# card, and the router copes with that today, so the window is a bound on
# what an unsigned client can inject, not on what the flow accepts.
BRAIN_MESSAGE_TAP_WINDOW = 10

def offered_reply_ids(cards: list[dict | None]) -> set[str]:
    """Every option id the given `Message.interactive` blobs put on screen.

    Pure, and tolerant of a blob that lost its shape (a card with no options,
    an option with no id) - a malformed card simply offers nothing.
    """
    ids: set[str] = set()
    for card in cards:
        if not isinstance(card, dict):
            continue
        for option in card.get("options") or []:
            if isinstance(option, dict) and isinstance(option.get("id"), str):
                ids.add(option["id"])
    return ids

async def _validated_brain_message_reply_id(
    session: AsyncSession, *, tenant: Tenant, patient: Patient, reply_id: str
) -> str | None:
    """`reply_id` if a recent card of this patient's conversation offered it, else None.

    A WhatsApp tap id arrives inside a webhook Meta signed; a Brain-Message
    tap id arrives from the patient's own browser, relayed by the switchboard.
    The switchboard vouches for WHO is speaking (their session) but not for
    the id itself, so before `_route_inbound_turn` may treat it as a tap -
    and re-attach its payload to the routing text - it has to be one of the
    ids THIS conversation actually put on screen, recently. Anything else is
    dropped: the turn is then routed on `text` alone, exactly as a typed
    message, and the event is logged. The set to check against is the column
    `Message.interactive` exists for; a patient's forged "prof|<other uuid>"
    never reaches `inbound_routing_text`.

    Scope is the patient's own conversation only: the id is looked up under
    (tenant, patient), so a patient cannot replay an id another conversation
    offered, even one of the same clinic.
    """
    conversation_id = await session.scalar(
        select(Conversation.id).where(
            Conversation.tenant_id == tenant.id,
            Conversation.patient_id == patient.id,
        )
    )
    offered: set[str] = set()
    if conversation_id is not None:
        cards = (
            await session.scalars(
                select(Message.interactive)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.direction == MessageDirection.OUTBOUND,
                    Message.interactive.is_not(None),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(BRAIN_MESSAGE_TAP_WINDOW)
            )
        ).all()
        offered = offered_reply_ids(list(cards))
    if reply_id in offered:
        return reply_id
    logger.warning(
        "brain_message_reply_id_rejected",
        tenant_id=str(tenant.id),
        conversation_id=str(conversation_id) if conversation_id is not None else None,
        # Untrusted input: bounded by the schema, cut again for the log line.
        reply_id=reply_id[:80],
        offered_count=len(offered),
    )
    return None

def _log_discarded_attachment(attachment: dict | None, reason: str) -> None:
    """Report a file whose turn this job dropped before writing its row.

    The API stored the bytes before enqueueing; a job that returns without the
    `messages` row leaves them in the bucket with nothing pointing at them. The worker
    holds no storage credentials (services/media_storage.py), so it cannot delete them -
    it says so, loudly and with the key (ids only, no name), for an operator or a sweep.
    """
    if attachment is not None:
        logger.warning(
            "brain_message_attachment_discarded",
            reason=reason,
            r2_object_key=attachment.get("r2_object_key"),
        )

async def _persist_brain_message_inbound(
    *,
    tenant_id: UUID,
    external_id: str,
    text: str | None,
    patient_name: str | None = None,
    dedupe_id: str | None = None,
    interactive_reply_id: str | None = None,
    attachment: dict | None = None,
) -> _ReplyContext | None:
    """Resolve a Brain-Message turn and hand it to the channel-neutral core.

    The Brain-Message twin of `_persist_inbound_message`: same shape, same
    transaction discipline, different resolution. Where the WhatsApp wrapper
    turns a `phone_number_id` into a tenant and a `wa_id` into a patient, this
    one is handed the tenant id outright (the brain-api switchboard derived it
    from its own validated session) and keys the patient on
    `(tenant, channel="brain_message", external_id)`.

    Two gates from the WhatsApp wrapper are deliberately NOT here:

      * the `wa_id` allowlist. It is the hard boundary of the WhatsApp
        Coexistence test window and is expressed as a set of phone-number
        digits (`config.py::bot_allowlist_wa_ids`); a Brain-Message patient has
        no phone number, so there is nothing to compare. Reaching this endpoint
        at all already requires the internal API key AND a brain-api session.
      * `ProcessedEvent` dedupe, unless the caller supplies `dedupe_id`. On
        WhatsApp it is mandatory because Meta redelivers; here the switchboard
        makes one call per patient action over an authenticated request. The
        hook is kept so it can be turned on from the caller's side without
        touching this function.
    """
    async with async_session_factory() as session:
        try:
            async with session.begin():
                if dedupe_id is not None:
                    if await _event_already_processed(session, dedupe_id):
                        logger.info("brain_message_duplicate", dedupe_id=dedupe_id)
                        _log_discarded_attachment(attachment, "duplicate")
                        return None
                    session.add(ProcessedEvent(event_id=dedupe_id))

                tenant = await session.get(Tenant, tenant_id)
                if tenant is None:
                    logger.error("brain_message_tenant_unresolved", tenant_id=str(tenant_id))
                    _log_discarded_attachment(attachment, "tenant_unresolved")
                    return None

                # `Tenant.is_active` is the WhatsApp go-live flag: the internal
                # activation endpoint deliberately requires a connected WhatsApp
                # number in addition to calendar/services/hours. Brain-Message is
                # a separate entitled channel and must not inherit that transport
                # gate, otherwise a portal-only clinic can never create a
                # conversation. `_send_bot_reply` still fails closed on the
                # authoritative subscription/secretaria entitlement, while the
                # ordinary booking flow continues to validate its own runtime
                # configuration and calendar.

                patient = await session.scalar(
                    select(Patient).where(
                        Patient.tenant_id == tenant.id,
                        Patient.channel == CHANNEL_BRAIN_MESSAGE,
                        Patient.external_id == external_id,
                    )
                )
                is_returning_patient = patient is not None
                if patient is None:
                    # `wa_id` stays NULL: this person has no WhatsApp number.
                    # That is what the nullable column shipped for - see
                    # models/patient.py.
                    patient = Patient(
                        tenant_id=tenant.id,
                        channel=CHANNEL_BRAIN_MESSAGE,
                        external_id=external_id,
                        name=patient_name,
                    )
                    session.add(patient)
                    await session.flush()
                    # One consent event per new patient row, exactly as on
                    # WhatsApp. `ConsentEvent.wa_id` is NOT NULL and has no
                    # channel column of its own (models/consent_event.py), so
                    # the subject is identified by the only handle this channel
                    # has: their external_id. Widening that model is a separate
                    # migration, not a side effect of this pipeline.
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

                # A tap id from this channel is the patient's own browser's
                # word for it, not Meta's: only honoured when it names an
                # option a recent card of THIS conversation offered.
                if interactive_reply_id is not None:
                    interactive_reply_id = await _validated_brain_message_reply_id(
                        session, tenant=tenant, patient=patient, reply_id=interactive_reply_id
                    )

                return await _route_inbound_turn(
                    session,
                    tenant=tenant,
                    patient=patient,
                    patient_ref=external_id,
                    channel=CHANNEL_BRAIN_MESSAGE,
                    is_returning_patient=is_returning_patient,
                    # A file's row is never empty: its caption, or the placeholder.
                    body=(
                        text
                        if attachment is None
                        else attachment_body(text, attachment.get("filename") or "anexo")
                    ),
                    # No Meta id exists for a message Meta never carried.
                    inbound_wam_id=None,
                    interactive_reply_id=interactive_reply_id,
                    attachment=attachment,
                )
        except IntegrityError:
            # A concurrent call already claimed this dedupe id, or raced us to
            # the (tenant, channel, external_id) constraint.
            logger.info("brain_message_duplicate_race", dedupe_id=dedupe_id)
            _log_discarded_attachment(attachment, "integrity_race")
            return None

async def process_brain_message_inbound(
    ctx: dict,
    tenant_id: str,
    external_id: str,
    text: str | None = None,
    patient_name: str | None = None,
    dedupe_id: str | None = None,
    interactive_reply_id: str | None = None,
    attachment: dict | None = None,
) -> None:
    """arq job: run one Brain-Message turn end to end.

    `attachment` (optional, so jobs enqueued by an older API still run) is the
    stored file record the API wrote to the bucket before enqueueing - the job
    carries a reference, never the bytes.

    The same ack-fast/work-async split the WhatsApp webhook uses (see the
    `whatsapp-webhook-arq` skill's golden rule): the HTTP handler enqueues this
    and returns 202 immediately, so an LLM turn never runs inside a request.
    Nothing about that rule was specific to Meta - it is about not holding a
    connection open for seconds of model latency - so the new channel follows it
    rather than taking an exception for itself.
    """
    reply = await _persist_brain_message_inbound(
        tenant_id=UUID(tenant_id),
        external_id=external_id,
        text=text,
        patient_name=patient_name,
        dedupe_id=dedupe_id,
        interactive_reply_id=interactive_reply_id,
        attachment=attachment,
    )
    if reply is not None:
        await _send_bot_reply(reply, redis=ctx.get("redis"))
