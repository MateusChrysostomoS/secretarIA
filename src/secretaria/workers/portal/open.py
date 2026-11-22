"""open - split out of workers/tasks.py (TASK-023)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentReminder,
    ConsentEvent,
    Conversation,
    FlowState,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
    is_live_status,
)
from secretaria.models.appointment_reminder import REMINDER_KIND_PRICE_CHANGE
from secretaria.schemas.internal import ConversationEntryContext
from secretaria.services import reminder_hooks
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
)
from secretaria.services.clinic_region import load_clinic_region
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.flow_router import reactivation_gap_minutes
from secretaria.services.payments import adjustment_suspension
from secretaria.services.reminder_text import REMINDER_TERMINAL_TEXT
from secretaria.workers.orchestrator import (
    _send_bot_reply,
)
from secretaria.workers.portal.release_card import offer_card_after_start
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.db import (
    _conversation_message_count,
    _get_or_create_conversation,
)
from secretaria.workers.shared.greeting import (
    _first_contact_reply,
    _load_sole_professional,
)
from secretaria.workers.shared.jobs import (
    _claim_event,
    _release_event,
)
from secretaria.workers.shared.opening import (
    _send_context_opening,
)
from secretaria.workers.shared.price_change_opening import send_price_change_opening
from secretaria.workers.shared.text import (
    _as_utc,
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
                    sole_professional=await _load_sole_professional(session, tenant.id),
                    region=await load_clinic_region(session, tenant),
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


def _enter_ledger_key(conversation_id: UUID, last_message_id: UUID) -> str:
    """One entry opening per (conversation, latest message).

    Keyed on the latest message rather than on time: two concurrent entries (a
    double refresh) see the same latest message and only one can insert the
    key, and once ANY message lands after the opening - the patient's answer
    or the opening itself - a later entry is a new key. 20 + 36 + 1 + 36 = 93
    characters, inside `ProcessedEvent.event_id`'s 128.
    """
    return f"brain_message_enter:{conversation_id}:{last_message_id}"


_IDENTITY_WAITS = (FlowState.AWAITING_NAME, FlowState.AWAITING_EMAIL, FlowState.AWAITING_EMAIL_CODE)


@dataclass(frozen=True)
class _EntryDecision:
    reply: _ReplyContext
    tenant: Tenant
    ledger_key: str
    last_message_id: UUID | None
    appointment_id: UUID | None = None
    source: str = "portal_entry"
    # TASK-085 R UB2 (D-R14): the link is a `price_change` notice - the first message is the
    # price action (`send_price_change_opening`), not the consultation's card.
    price_change_reminder_id: UUID | None = None


async def _latest_message(session, conversation_id: UUID):
    return (
        await session.execute(
            select(Message.id, Message.created_at)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
    ).first()


async def _price_notice_already_shown(session, answered_at: datetime | None, latest) -> bool:
    """TASK-085 R UB2 fix round 1: the price notice was answered and the conversation's LAST
    message is a bot message written at or after that answer (stamps compared to the second:
    the database's own clock may drop the fraction)."""
    if answered_at is None or latest is None:
        return False
    message = await session.get(Message, latest.id)
    if (
        message is None
        or message.direction != MessageDirection.OUTBOUND
        or message.sender != MessageSender.BOT
    ):
        return False
    return _as_utc(latest.created_at) >= _as_utc(answered_at).replace(microsecond=0)


async def _pending_price_notices(
    session, *, tenant_id: UUID, patient_id: UUID, now: datetime
) -> list[UUID]:
    """TASK-085 R A1 (D-R14): the patient's `price_change` notices the explicit link would still
    ACT on (at most two are read: the caller only needs "none / exactly one / several").

    Same criterion as `price_change.open_price_notice` for "neither `not_found` nor `pending`":
    this clinic's row, of a consultation of THIS patient in this clinic, unanswered
    (`answered_at` NULL), active (`invalidated_at` NULL) and of the consultation's current start
    (its version), the consultation live, still ahead and not suspended. An answered or closed
    notice never takes over the ordinary entry (that one keeps today's opening)."""
    rows = (
        await session.execute(
            select(AppointmentReminder.id, AppointmentReminder.appointment_start_at, Appointment)
            .join(Appointment, Appointment.id == AppointmentReminder.appointment_id)
            .where(
                AppointmentReminder.tenant_id == tenant_id,
                AppointmentReminder.kind == REMINDER_KIND_PRICE_CHANGE,
                AppointmentReminder.answered_at.is_(None),
                AppointmentReminder.invalidated_at.is_(None),
                Appointment.tenant_id == tenant_id,
                Appointment.patient_id == patient_id,
            )
            .order_by(AppointmentReminder.sent_at.desc(), AppointmentReminder.id.desc())
        )
    ).all()
    found: list[UUID] = []
    for reminder_id, version, appointment in rows:
        if (
            appointment.start_at is None
            or not is_live_status(appointment.status)
            or _as_utc(appointment.start_at) <= now
            or _as_utc(version) != _as_utc(appointment.start_at)
        ):
            continue
        if await adjustment_suspension.is_suspended(
            session, tenant_id=tenant_id, appointment_id=appointment.id
        ):
            continue
        found.append(reminder_id)
        if len(found) > 1:
            break
    return found


async def _brain_message_entry_decision(
    tenant_id: UUID,
    external_id: str,
    *,
    now: datetime | None = None,
    entry_context: ConversationEntryContext | None = None,
) -> _EntryDecision | None:
    """Should a patient who ENTERS an already-started conversation be spoken to?

    The rule, in order (owner, 2026-10-05: speak first on entry, by context -
    without ever talking over a conversation that is still going):

      * nobody to speak to: unknown tenant/patient/conversation, or an empty
        thread (that is `process_brain_message_open`'s first contact);
      * a human from the clinic owns the conversation -> silence;
      * consent not given yet, or an identity step (name, e-mail, code) still
        open -> silence: those steps carry their own messages and resume offer;
      * the last message is younger than `PORTAL_ENTRY_QUIET_MINUTES` ->
        silence: a refresh mid-chat, or the opening the login itself just sent;
      * a flow still in progress (anything but IDLE/MENU) younger than the
        clinic's reactivation gap -> silence: never cut a booking in half.

    Otherwise the context-aware opening goes out (`workers/shared/opening.py`).

    TASK-085 R UB2 (D-R14, mapa-u §2.3, §7.17): a link to a `price_change` notice
    (the clinic changed the price; the patient was e-mailed) is explicit even with
    reminders v2 switched off, and its first message is the price action
    (`price_change_reminder_id`). A link to a row that does not exist at all in
    this clinic (a broken link: the e-mail went out but its row was never written)
    is treated as an ordinary entry - the usual opening, under the usual rules.
    """
    now = now or datetime.now(UTC)
    settings = get_settings()
    # Only a reminder link overrides the silence rules. `clinic_link` is sent
    # by the frontend on every `convite=` URL load (F5, back to the clinic), so
    # it must keep the ordinary navigation guards.
    explicit = entry_context is not None and entry_context.source == "reminder_link"
    async with async_session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return None
        price_link = False
        link_answered_at = None
        if explicit and entry_context.reminder_id is not None:
            link = (
                await session.execute(
                    select(AppointmentReminder.kind, AppointmentReminder.answered_at).where(
                        AppointmentReminder.id == entry_context.reminder_id,
                        AppointmentReminder.tenant_id == tenant_id,
                    )
                )
            ).first()
            link_kind = link.kind if link is not None else None
            link_answered_at = link.answered_at if link is not None else None
            if link_kind is None:
                # A broken link: no such row in this clinic -> an ordinary entry.
                logger.info("brain_message_enter_broken_link", tenant_id=str(tenant_id))
                explicit = False
            price_link = link_kind == REMINDER_KIND_PRICE_CHANGE
        explicit = explicit and (price_link or reminder_hooks.enabled_for(tenant))
        patient = await session.scalar(
            select(Patient).where(
                Patient.tenant_id == tenant_id,
                Patient.channel == CHANNEL_BRAIN_MESSAGE,
                Patient.external_id == external_id,
            )
        )
        if patient is None:
            return None
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.tenant_id == tenant_id, Conversation.patient_id == patient.id
            )
        )
        if conversation is None:
            return None
        reminder_id = entry_context.reminder_id if explicit else None
        source = entry_context.source if explicit else "portal_entry"
        latest = await _latest_message(session, conversation.id)
        if latest is None and not explicit:
            return None
        age = now - _as_utc(latest.created_at) if latest is not None else timedelta(0)
        flow_running = conversation.flow_state not in (FlowState.IDLE, FlowState.MENU)
        reason = None
        if conversation.handover_state == HandoverState.HUMAN_ACTIVE:
            reason = "human_active"
        elif not patient.lgpd_accepted:
            reason = "consent_pending"
        elif conversation.flow_state in _IDENTITY_WAITS:
            # Never skipped by an opening, however old: the name/e-mail/code
            # step is required (the clinic needs the name for the event, the
            # e-mail and the PreCheck hand-off) and has its own resume offer.
            reason = "identity_step_pending"
        elif not explicit and age < timedelta(minutes=settings.PORTAL_ENTRY_QUIET_MINUTES):
            reason = "recent_activity"
        elif (
            not explicit
            and flow_running
            and age < timedelta(minutes=reactivation_gap_minutes(tenant))
        ):
            reason = "flow_in_progress"
        if reason is not None:
            logger.info(
                "brain_message_enter_silent",
                tenant_id=str(tenant_id),
                conversation_id=str(conversation.id),
                reason=reason,
            )
            return None
        # TASK-085 R A1 (D-R14): the e-mail's link fires TWO entries (the account opening,
        # without context, and the Portal's `/enter` with the link) in no guaranteed order.
        # An ORDINARY entry that passed every ordinary guard above (quiet window, flow in
        # progress, human, consent/identity - Orchestrator follow-up: only WHAT it sends changes)
        # of a patient with a PENDING price notice sends that notice's price action instead of
        # the usual card, under the link's own ledger key: whichever entry comes first speaks,
        # the other one is deduplicated. Inside the quiet window it stays silent and the link
        # entry (which ignores the quiet window) acts alone.
        # Round 3 (review A1 M1/L1/L3): with SEVERAL pending notices the ordinary entry cannot
        # know which e-mail was clicked - it stays silent (no card either) and the link entry is
        # the only one that speaks. `source` stays `portal_entry` (the fallback card keeps the
        # ordinary MENU reset and log label). A clinic without the sinal is never looked up.
        ordinary_price = False
        if not explicit and tenant.pix_deposit_enabled:
            pending = await _pending_price_notices(
                session, tenant_id=tenant_id, patient_id=patient.id, now=now
            )
            if len(pending) > 1:
                logger.info(
                    "brain_message_enter_silent",
                    tenant_id=str(tenant_id),
                    conversation_id=str(conversation.id),
                    reason="several_price_notices_pending",
                )
                return None
            if pending:
                logger.info(
                    "brain_message_enter_pending_price_notice",
                    tenant_id=str(tenant_id),
                    conversation_id=str(conversation.id),
                    reminder_id=str(pending[0]),
                )
                ordinary_price = True
                price_link = True
                reminder_id = pending[0]
        appointment_id = None
        if explicit:
            # The opaque row is only context. Ownership is checked using the
            # patient resolved from the authenticated gateway identity.
            appointment = await session.scalar(
                select(Appointment)
                .join(AppointmentReminder, AppointmentReminder.appointment_id == Appointment.id)
                .where(
                    AppointmentReminder.id == reminder_id,
                    AppointmentReminder.tenant_id == tenant_id,
                    Appointment.tenant_id == tenant_id,
                    Appointment.patient_id == patient.id,
                )
            )
            if appointment is None:
                logger.info(
                    "brain_message_enter_silent",
                    reason="invalid_or_terminal_reminder",
                    conversation_id=str(conversation.id),
                )
                return None
            appointment_id = appointment.id
            if await adjustment_suspension.is_suspended(
                session, tenant_id=tenant_id, appointment_id=appointment.id
            ):
                # TASK-085 R (D-R4): the reminder's consultation waits for the Pix of a change's
                # difference and is hidden from the patient - the ordinary opening goes out
                # (it never shows a suspended consultation), never that consultation's card
                # (UB2: nor a price action on it).
                logger.info(
                    "brain_message_enter_suspended_reminder",
                    conversation_id=str(conversation.id),
                )
                appointment_id = None
                price_link = False
            elif price_link:
                # UB2: the price action replaces the card; `open_price_notice` re-checks the
                # row (owner, version, suspension) under the consultation lock.
                appointment_id = None
                if await _price_notice_already_shown(session, link_answered_at, latest):
                    # Fix round 1 (review M1): a reload / tab switch still carries
                    # `lembrete=`; the notice was answered and the bot spoke last, after it -
                    # nothing new to say (a new patient message or a re-armed notice speaks).
                    logger.info(
                        "brain_message_enter_silent",
                        reason="price_notice_already_shown",
                        conversation_id=str(conversation.id),
                    )
                    return None
        if explicit and latest is not None and not price_link:
            message = await session.get(Message, latest.id)
            # Keep the current visible card on reload. If it has fallen out
            # of the latest page/tap window, present a new one once.
            if message is not None and isinstance(message.interactive, dict):
                for option in message.interactive.get("options", []):
                    raw = option.get("id", "")
                    if raw.startswith("remedit|"):
                        try:
                            row_id = UUID(raw.split("|", 1)[1])
                        except ValueError:
                            continue
                        row = await session.get(AppointmentReminder, row_id)
                        current = (
                            await session.get(Appointment, row.appointment_id)
                            if row is not None
                            else None
                        )
                        if (
                            row is not None
                            and current is not None
                            and current.patient_id == patient.id
                            and current.tenant_id == tenant_id
                            and is_live_status(current.status)
                            and _as_utc(current.start_at) > now
                            and _as_utc(current.start_at) == _as_utc(row.appointment_start_at)
                            and row.tenant_id == tenant_id
                            and row.invalidated_at is None
                            and (appointment_id is None or row.appointment_id == appointment_id)
                            # TASK-085 R: a suspended consultation's card is not "current".
                            and not await adjustment_suspension.is_suspended(
                                session, tenant_id=tenant_id, appointment_id=current.id
                            )
                        ):
                            return None
            if (
                appointment_id is not None
                and message is not None
                and message.body == REMINDER_TERMINAL_TEXT
                and (not is_live_status(appointment.status) or _as_utc(appointment.start_at) <= now)
            ):
                return None
        latest_id = latest.id if latest is not None else None
        return _EntryDecision(
            reply=_ReplyContext(
                channel=CHANNEL_BRAIN_MESSAGE,
                conversation_id=conversation.id,
                tenant_id=tenant_id,
                patient_ref=external_id,
                inbound_body="",
            ),
            tenant=tenant,
            ledger_key=(
                f"reminder_entry:{conversation.id}:{reminder_id or 'clinic'}:{latest_id}"
                if explicit or ordinary_price
                else _enter_ledger_key(conversation.id, latest_id)
            ),
            last_message_id=latest_id,
            appointment_id=appointment_id,
            source=source,
            price_change_reminder_id=reminder_id if price_link else None,
        )


async def process_brain_message_enter(
    ctx: dict,
    tenant_id: str,
    external_id: str,
    entry_context: dict | None = None,
) -> None:
    """arq job: a known patient entered a conversation that already has history.

    The sibling of `process_brain_message_open` for the case that one refuses
    (`exists`): the decision is `_brain_message_entry_decision`, the message is
    `_send_context_opening`. Same discipline as the open job: claim, re-read
    right before speaking (an inbound that landed meanwhile wins), and hand
    the claim back when nothing reached the patient.
    """
    tenant_uuid = UUID(tenant_id)
    # TASK-052: after the released consultation's start, opening the conversation shows only
    # "Remarcar" / "Cancelar" (once per offer); nothing else is said on this entry.
    if await offer_card_after_start(tenant_uuid, external_id):
        return
    context = (
        ConversationEntryContext.model_validate(entry_context)
        if entry_context is not None
        else None
    )
    decision = await _brain_message_entry_decision(tenant_uuid, external_id, entry_context=context)
    if decision is None:
        if (
            context is not None
            and context.source == "clinic_link"
            and not await _portal_conversation_has(tenant_uuid, external_id)
        ):
            await process_brain_message_open(ctx, tenant_id, external_id)
        return
    summary = await get_entitlements(tenant_uuid, ctx.get("redis"))
    if summary is None or not (summary.active and summary.secretaria_enabled):
        logger.warning(
            "bot_reply_suppressed_unentitled",
            tenant_id=tenant_id,
            entitlement_unknown=summary is None,
            conversation_id=str(decision.reply.conversation_id),
        )
        return
    if not await _claim_event(decision.ledger_key):
        logger.info("brain_message_enter_already_claimed", tenant_id=tenant_id)
        return

    async def _still_current() -> bool:
        # Asked immediately before the write: a turn that landed meanwhile (a
        # tap on an older card as the page loaded) wins, never the opening.
        async with async_session_factory() as session:
            latest = await _latest_message(session, decision.reply.conversation_id)
        return (latest.id if latest is not None else None) == decision.last_message_id

    try:
        rendered = None
        if decision.price_change_reminder_id is not None:
            # TASK-085 R UB2: the first message is the price action. Asked right before: once
            # the notice is opened the money is decided and its message must go out.
            if await _still_current():
                rendered = await send_price_change_opening(
                    decision.reply,
                    decision.tenant,
                    reminder_id=decision.price_change_reminder_id,
                    redis=ctx.get("redis"),
                )
                if rendered is False:
                    # Not (or no longer) a price action for this patient: the usual opening.
                    rendered = await _send_context_opening(
                        decision.reply,
                        decision.tenant,
                        external_id,
                        redis=ctx.get("redis"),
                        source=decision.source,
                        still_current=_still_current,
                    )
        else:
            rendered = await _send_context_opening(
                decision.reply,
                decision.tenant,
                external_id,
                redis=ctx.get("redis"),
                source=decision.source,
                still_current=_still_current,
                **({"appointment_id": decision.appointment_id} if decision.appointment_id else {}),
            )
    except Exception:
        # Same key on the next entry (nothing new was written): leaving it
        # claimed would silence this conversation until the patient types.
        await _release_event(decision.ledger_key, event="brain_message_enter_release_failed")
        raise
    if rendered is None:
        logger.info("brain_message_enter_superseded", tenant_id=tenant_id)
    elif not rendered:
        await _release_event(decision.ledger_key, event="brain_message_enter_release_failed")
        logger.warning("brain_message_enter_nothing_sent", tenant_id=tenant_id)
