"""CORE post_booking hook: ask a Brain-Message visitor for the code, after booking.

The second of two hooks that react to the same event, and the reason they are
two files rather than one. `plugins/precheck_handoff.py` offers the
pre-consult; this one asks the visitor to prove the address they typed at the
start of the conversation. They fire off the same committed appointment and
share nothing else: different destinations, different failure meanings,
different idempotency keys, and only one of them is channel-specific. Folding
them together would mean a PreCheck outage could swallow an account, or an
unverified address could hold up a pre-consult link.

Their order relative to each other does not matter, and cannot: the two are
DISJOINT BY CHANNEL. `precheck_handoff` reads `ctx.patient.wa_id`, which is
NULL for every Brain-Message row (`models/patient.py` — that person has no
phone number), so it skips with `no_patient_phone` before it calls anything;
this hook returns on `channel != brain_message` before it calls anything. For
any one appointment at most one of them can ever send. Registration order
(alphabetical, ruff-enforced, so this module comes first) is therefore not
load-bearing — stated here because the natural assumption on reading two
post_booking hooks is that they queue behind each other, and a future change
that gives a Brain-Message patient a `wa_id` would make that assumption start
to matter. `run_post_booking` has no short-circuit — every hook runs, each
wrapped in its own try/except — so neither can suppress the other either way.

## Why the appointment is already committed when this runs

The owner fixed it: "quando acabar - marcar a consulta em si, envia-se uma
mensagem pela secretarIA, avisando sobre o código". The code is what gives the
patient persistent access to the conversation afterwards; it is NOT a condition
of the booking, and nothing in this module can undo, block or delay one. A
patient who never types the code keeps their appointment — that is a product
decision, recorded in `PLANO_LOGIN_SEM_GATE_PACIENTE_NOVO.md`, not an accident
of this implementation.

## Brain-Message only, and the guard is structural

`ctx.patient.channel != CHANNEL_BRAIN_MESSAGE` returns before anything else
happens. A WhatsApp patient is identified by a phone number Meta already
verified; there is no pending visit for them, no address to prove, and
brain-api would answer 404 to every call this module makes. The check is first
so the WhatsApp path costs exactly one comparison and cannot reach a network
call, a ledger row or a send.

## Silence is the failure mode

Same discipline as `precheck_handoff`, for the same reason: nobody asked for
this message. If brain-api cannot mail a code, the patient must not receive a
message promising one. NOT_PENDING (already an account, or the visit expired),
NO_EMAIL (the conversation never captured an address — for example, a legacy
or partially migrated visit) and UNAVAILABLE all end the same
way: no message, one log line, and the conversation stays exactly as it was.

## Idempotency

`ProcessedEvent`, namespaced `pending_identity:<appointment_id>` — a namespace
of its own so it can never collide with `precheck:<appointment_id>`. Without it
an arq retry of `run_post_booking_hooks` would mail a second code and re-send
the prompt. The claim is released on every path that ends without a message, so
a retry gets a real second chance (the reasoning is spelled out in
`precheck_handoff._release`).

Never logs the address or the code — neither ever reaches this module. See
`services/pending_identity.py`.
"""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation, FlowState, ProcessedEvent
from secretaria.plugins.base import PluginSpec, PostBookingContext
from secretaria.plugins.registry import register
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.pending_identity import (
    CODE_NOTICE_MESSAGE,
    RequestCodeOutcome,
    request_code,
)

logger = get_logger(__name__)


def _ledger_key(appointment_id: UUID) -> str:
    return f"pending_identity:{appointment_id}"


async def _claim(appointment_id: UUID) -> bool:
    """Insert the ledger row. True iff THIS call claimed the send.

    Claim-insert + IntegrityError race pattern, identical to
    `plugins/precheck_handoff.py::_claim`.
    """
    key = _ledger_key(appointment_id)
    async with async_session_factory() as session:
        try:
            async with session.begin():
                existing = await session.scalar(
                    select(ProcessedEvent.id).where(ProcessedEvent.event_id == key)
                )
                if existing is not None:
                    return False
                session.add(ProcessedEvent(event_id=key))
        except IntegrityError:
            return False
    return True


async def _release(appointment_id: UUID) -> None:
    """Give the ledger row back: the code notice did NOT go out. Best-effort."""
    key = _ledger_key(appointment_id)
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == key))
    except Exception as exc:
        logger.warning(
            "pending_identity_release_failed",
            appointment_id=str(appointment_id),
            error=str(exc),
        )


def _skip(ctx: PostBookingContext, reason: str) -> None:
    logger.info(
        "pending_identity_post_booking_skipped",
        reason=reason,
        tenant_id=str(ctx.tenant.id),
        appointment_id=str(ctx.appointment.id),
    )


async def _conversation_id(tenant_id: UUID, patient_id: UUID) -> UUID | None:
    """This patient's conversation row, which IS the Brain-Message address.

    `BrainMessageSender` delivers by writing a `Message` on a conversation, so
    without this id there is nowhere for the notice to land. One row per
    (tenant, patient) by unique constraint, so this can only ever match one.
    """
    async with async_session_factory() as session:
        return await session.scalar(
            select(Conversation.id).where(
                Conversation.tenant_id == tenant_id,
                Conversation.patient_id == patient_id,
            )
        )


async def _post_booking(ctx: PostBookingContext) -> None:
    """Ask the visitor to prove their address, once, after their booking.

    Order of operations mirrors `precheck_handoff._post_booking`: every free
    local check happens BEFORE the ledger claim, the claim happens before the
    brain-api call, and the claim is released on every path that ends without a
    message.

    The state is written BEFORE the send, not after. If the write succeeded and
    the send then failed, the patient is in a state expecting a code they were
    never told about — and the very next thing they type leaves it (any
    non-6-digit message exits, see `workers/tasks.py`'s gate), so the cost is
    one turn. The other order costs more: a patient who receives the prompt,
    types the code and finds it read as a menu tap.
    """
    if ctx.patient is None:
        # A block slot created from the doctor hub: nobody to ask.
        _skip(ctx, "no_patient")
        return

    if ctx.patient.channel != CHANNEL_BRAIN_MESSAGE:
        # The WhatsApp path stops here, before any I/O. See the docstring.
        _skip(ctx, "not_brain_message")
        return

    external_id = (ctx.patient.external_id or "").strip()
    if not external_id:
        # A Brain-Message row with no handle should not exist, but the column
        # is nullable (models/patient.py) and a network call keyed on an empty
        # string would be a 404 at best.
        _skip(ctx, "no_external_id")
        return

    conversation_id = await _conversation_id(ctx.tenant.id, ctx.patient.id)
    if conversation_id is None:
        _skip(ctx, "no_conversation")
        return

    if not await _claim(ctx.appointment.id):
        _skip(ctx, "already_sent")
        return

    try:
        outcome = await request_code(ctx.tenant.id, external_id)
    except Exception as exc:
        # `request_code` fails closed by contract and should never raise; belt
        # and braces, because escaping here would leave the claim held with no
        # message behind it.
        logger.warning(
            "pending_identity_post_booking_failed",
            reason="request_raised",
            error=str(exc),
            tenant_id=str(ctx.tenant.id),
            appointment_id=str(ctx.appointment.id),
        )
        await _release(ctx.appointment.id)
        return

    if outcome is not RequestCodeOutcome.SENT:
        # NOT_PENDING (already an account, or the visit expired), NO_EMAIL (no
        # address was ever captured), UNAVAILABLE. The patient hears nothing:
        # this hook was not asked for anything, so it has nothing to apologise
        # for, and a prompt for a code that was never mailed is worse than
        # silence.
        await _release(ctx.appointment.id)
        _skip(ctx, f"request_{outcome.value}")
        return

    try:
        async with async_session_factory() as session:
            async with session.begin():
                conversation = await session.get(Conversation, conversation_id)
                if conversation is not None:
                    conversation.flow_state = FlowState.AWAITING_EMAIL_CODE
        sender = BrainMessageSender(
            conversation_id=conversation_id,
            session_factory=async_session_factory,
        )
        await sender.send_text_message(to=external_id, body=CODE_NOTICE_MESSAGE)
    except Exception as exc:
        # The code IS in the patient's inbox; only the prompt is missing. The
        # claim goes back so an arq retry of the surrounding job can try again
        # — brain-api treats a second request as a new challenge on the same
        # visit, and the first one simply expires unused.
        logger.warning(
            "pending_identity_post_booking_failed",
            reason="send_failed",
            error_type=type(exc).__name__,
            tenant_id=str(ctx.tenant.id),
            appointment_id=str(ctx.appointment.id),
        )
        await _release(ctx.appointment.id)
        return

    logger.info(
        "pending_identity_post_booking_sent",
        source=ctx.source,
        tenant_id=str(ctx.tenant.id),
        appointment_id=str(ctx.appointment.id),
    )


PENDING_IDENTITY_SPEC = PluginSpec(
    id="pending_identity",
    # No entitlement gate, for the same reason `precheck_handoff` has none: the
    # real gate lives one hop away in brain-api, which owns whether this visit
    # exists at all and answers NOT_PENDING when it does not. `()` still means
    # "while the subscription is active" (`registry._spec_enabled`).
    entitlement_keys=(),
    post_booking=_post_booking,
)
register(PENDING_IDENTITY_SPEC)
