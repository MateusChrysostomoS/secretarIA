"""Sending what a released PAID slot scheduled (TASK-052, owner decisions 2026-10-12).

Every `deposit_release_emails` row is ONE send (spec §5.13): CLAIMED (`claimed_at IS NULL`) and
committed BEFORE the send; RE-CHECKED at send time by `still_relevant` (spec §5.8/§5.9 - refund
notices are paused while a "Manter" request is open and dropped once the money is in use again);
SENT (clinic notices to `Tenant.contact_email`; patient messages on the conversation's channel -
Portal card, plus an e-mail with the link only for the refund check; WhatsApp card inside the
24 h window, the PENDING-META template outside it with `paid_notice_allowed`, NEVER an e-mail);
"nothing left" gives the claim back, the 5th failure is `given_up`. Logs ids/kind/outcome only.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, PixDeposit, Tenant
from secretaria.models.deposit_release_offer import (
    DECISION_CANCELLED,
    DECISION_KEEP_DECLINED,
    DECISION_KEEP_REQUESTED,
    DECISION_KEPT,
    DECISION_NO_ANSWER,
    DECISION_REBOOKED,
    EMAIL_CARRY_REFUSED,
    EMAIL_KEEP_DECLINED,
    EMAIL_KEEP_REQUEST,
    EMAIL_PATIENT_CHECK,
    EMAIL_PATIENT_KEEP_ACCEPTED,
    EMAIL_PATIENT_KEEP_DECLINED,
    EMAIL_REBOOKED_DEPOSIT,
    EMAIL_REFUND_CANCEL,
    EMAIL_REFUND_DAY,
    EMAIL_REFUND_NOANSWER,
    EMAIL_REFUND_REMINDER,
    OUTCOME_GIVEN_UP,
    OUTCOME_NO_CONTACT,
    OUTCOME_SENDING,
    OUTCOME_SENT,
    OUTCOME_SKIPPED,
    REFUND_DUE,
    REFUND_MARKED,
    REFUND_NOT_RECEIVED,
    SILENT_DECISIONS,
    DepositReleaseEmail,
    DepositReleaseOffer,
)
from secretaria.services import cancellation_notice, email, release_offer
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.payments import adjustment_queries
from secretaria.services.payments.money import format_brl
from secretaria.services.reminder_text import portal_conversation_link
from secretaria.services.tenant_config import get_waba_token
from secretaria.services.usage_events import emit_usage_event
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)

MAX_ATTEMPTS = 5
BATCH_SIZE = 50
PATIENT_KINDS = (EMAIL_PATIENT_CHECK, EMAIL_PATIENT_KEEP_ACCEPTED, EMAIL_PATIENT_KEEP_DECLINED)


@dataclass
class DispatchReport:
    sent: int = 0
    skipped: int = 0
    no_contact: int = 0
    retried: int = 0
    given_up: int = 0
    lost: int = 0


def still_relevant(kind: str, offer: DepositReleaseOffer) -> bool:
    """The send-time rule of spec §5.8/§5.9 (pure)."""
    if not offer.deposit_paid:
        return False
    decision, refund = offer.decision, offer.refund_status
    rules = {
        EMAIL_KEEP_REQUEST: decision == DECISION_KEEP_REQUESTED,
        EMAIL_PATIENT_KEEP_ACCEPTED: decision == DECISION_KEPT,
        EMAIL_PATIENT_KEEP_DECLINED: decision == DECISION_KEEP_DECLINED,
        EMAIL_KEEP_DECLINED: decision == DECISION_KEEP_DECLINED and refund == REFUND_DUE,
        EMAIL_REBOOKED_DEPOSIT: decision == DECISION_REBOOKED,
        # Owner 2026-10-12: a new Pix was charged at the Remarcar; relevant until marked.
        EMAIL_CARRY_REFUSED: refund == REFUND_DUE,
        EMAIL_PATIENT_CHECK: refund == REFUND_MARKED,
        EMAIL_REFUND_CANCEL: decision == DECISION_CANCELLED and refund == REFUND_DUE,
        EMAIL_REFUND_NOANSWER: refund == REFUND_DUE
        and decision in (*SILENT_DECISIONS, DECISION_NO_ANSWER),
        EMAIL_REFUND_DAY: refund in (REFUND_DUE, REFUND_MARKED, REFUND_NOT_RECEIVED)
        and decision not in (DECISION_KEEP_REQUESTED, DECISION_KEPT, DECISION_REBOOKED),
        EMAIL_REFUND_REMINDER: refund == REFUND_NOT_RECEIVED,
    }
    return rules.get(kind, False)


async def dispatch_due(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    offer_id: UUID | None = None,
    limit: int = BATCH_SIZE,
) -> DispatchReport:
    """Send every unclaimed row due by `now` (only `offer_id`'s when given). Commits per row."""
    now = now or datetime.now(UTC)
    report = DispatchReport()
    query = select(DepositReleaseEmail.id).where(
        DepositReleaseEmail.claimed_at.is_(None), DepositReleaseEmail.due_at <= now
    )
    if offer_id is not None:
        query = query.where(DepositReleaseEmail.offer_id == offer_id)
    due = (
        await session.scalars(
            query.order_by(DepositReleaseEmail.due_at, DepositReleaseEmail.kind).limit(limit)
        )
    ).all()
    for email_id in due:
        try:
            await _dispatch_one(session, email_id, now, report)
        except Exception as exc:  # noqa: BLE001 - one row never stops the batch
            await session.rollback()
            logger.warning(
                "release_offer_send_failed", email_id=str(email_id), error_type=type(exc).__name__
            )
    return report


async def _dispatch_one(
    session: AsyncSession, email_id: UUID, now: datetime, report: DispatchReport
) -> None:
    claimed = await session.execute(
        update(DepositReleaseEmail)
        .where(DepositReleaseEmail.id == email_id, DepositReleaseEmail.claimed_at.is_(None))
        .values(claimed_at=now, outcome=OUTCOME_SENDING)
        .execution_options(synchronize_session=False)
    )
    await session.commit()  # durable BEFORE the send: nobody else can take it now
    if claimed.rowcount != 1:
        report.lost += 1
        return
    row = await session.get(DepositReleaseEmail, email_id, populate_existing=True)
    kind, attempts, offer_id = row.kind, row.attempts, row.offer_id
    offer = await session.get(DepositReleaseOffer, offer_id, populate_existing=True)
    tenant = await session.get(Tenant, row.tenant_id)
    if (
        offer is None
        or tenant is None
        or offer.tenant_id != tenant.id
        or not still_relevant(kind, offer)
    ):
        await _finish(session, email_id, OUTCOME_SKIPPED)
        report.skipped += 1
        _log(email_id, offer_id, kind, OUTCOME_SKIPPED)
        return

    if kind in PATIENT_KINDS:
        delivered = await _send_patient(session, tenant, offer, kind)
    else:
        delivered = await _send_clinic(session, tenant, offer, kind)

    if delivered is None:
        outcome = OUTCOME_NO_CONTACT
        await _finish(session, email_id, outcome)
        report.no_contact += 1
    elif delivered:
        outcome = OUTCOME_SENT
        await _finish(session, email_id, outcome)
        report.sent += 1
    else:
        outcome = await _give_back(session, email_id, attempts)
        if outcome == OUTCOME_GIVEN_UP:
            report.given_up += 1
        else:
            report.retried += 1
    _log(email_id, offer_id, kind, outcome)


def _log(email_id: UUID, offer_id: UUID, kind: str, outcome: str) -> None:
    logger.info(
        "release_offer_send",
        email_id=str(email_id),
        offer_id=str(offer_id),
        kind=kind,
        outcome=outcome,
    )


async def _finish(session: AsyncSession, email_id: UUID, outcome: str) -> None:
    await session.execute(
        update(DepositReleaseEmail)
        .where(DepositReleaseEmail.id == email_id)
        .values(outcome=outcome)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _give_back(session: AsyncSession, email_id: UUID, attempts: int) -> str:
    """Nothing left the building: retry later, or give up on the 5th failure."""
    tries = attempts + 1
    if tries >= MAX_ATTEMPTS:
        values = {"outcome": OUTCOME_GIVEN_UP, "attempts": tries}
        logger.error(
            "release_offer_send_given_up",
            alarm="release_offer_send_undelivered",
            email_id=str(email_id),
        )
    else:
        values = {"claimed_at": None, "outcome": None, "attempts": tries}
    await session.execute(
        update(DepositReleaseEmail)
        .where(DepositReleaseEmail.id == email_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return OUTCOME_GIVEN_UP if tries >= MAX_ATTEMPTS else "retry"


async def _held_text(session: AsyncSession, deposit: PixDeposit | None) -> str:
    """The amount every release message quotes: what the clinic holds for the sinal (sinal +
    paid service differences - differences already asked back; plan 2026-10-26 Task 8)."""
    if deposit is None:
        return ""
    return format_brl(await adjustment_queries.held_total_cents(session, deposit))


async def _send_clinic(
    session: AsyncSession, tenant: Tenant, offer: DepositReleaseOffer, kind: str
) -> bool | None:
    to = (tenant.contact_email or "").strip()
    if not to:
        return None
    deposit = await session.get(PixDeposit, offer.deposit_id)
    released = await session.get(Appointment, offer.released_appointment_id)
    tz = tenant.timezone
    amount = await _held_text(session, deposit)
    slot = (
        release_offer.slot_text(released.start_at, tz)
        if released is not None and released.start_at is not None
        else ""
    )
    link = release_offer.console_conversation_link(offer.conversation_id)
    if kind == EMAIL_KEEP_REQUEST:
        return await email.send_release_keep_request_clinic_email(
            to,
            clinic_name=tenant.clinic_name,
            slot_text=slot,
            decide_by_text=release_offer.when_text(offer.keep_deadline_at, tz),
            conversation_link=link,
        )
    if kind == EMAIL_CARRY_REFUSED:
        return await email.send_release_carry_refused_clinic_email(
            to,
            clinic_name=tenant.clinic_name,
            check=offer.carry_check or "unknown",
            amount_text=amount,
            slot_text=slot,
            conversation_link=link,
        )
    if kind == EMAIL_REBOOKED_DEPOSIT:
        fresh = (
            await session.get(Appointment, offer.new_appointment_id)
            if offer.new_appointment_id
            else None
        )
        return await email.send_release_rebooked_clinic_email(
            to,
            clinic_name=tenant.clinic_name,
            amount_text=amount,
            old_slot_text=slot,
            new_slot_text=release_offer.slot_text(fresh.start_at, tz)
            if fresh is not None and fresh.start_at is not None
            else "",
            conversation_link=link,
        )
    paid = (
        deposit.paid_at.strftime("%d/%m")
        if deposit is not None and deposit.paid_at is not None
        else ""
    )
    return await email.send_release_refund_clinic_email(
        to,
        clinic_name=tenant.clinic_name,
        kind=kind,
        no_answer_closed=offer.decision == DECISION_NO_ANSWER,
        amount_text=amount,
        paid_text=paid,
        slot_text=slot,
        deadline_text=release_offer.when_text(offer.decide_by_at, tz),
        conversation_link=link,
    )


async def _patient_message(
    session: AsyncSession, tenant: Tenant, offer: DepositReleaseOffer, kind: str
) -> tuple[str, list[tuple[str, str]] | None, str, str]:
    """(text, buttons or None, out-of-window template name, amount) of a patient message."""
    released_id = offer.released_appointment_id
    deposit = await session.get(PixDeposit, offer.deposit_id)
    amount = await _held_text(session, deposit)
    if kind == EMAIL_PATIENT_CHECK:
        return (
            release_offer.CHECK_TEXT.format(amount=amount),
            release_offer.check_buttons(released_id),
            get_settings().REFUND_CHECK_TEMPLATE_NAME,
            amount,
        )
    released = await session.get(Appointment, released_id)
    when = (
        release_offer.slot_text(released.start_at, tenant.timezone)
        if released is not None and released.start_at is not None
        else ""
    )
    template = get_settings().RELEASE_OFFER_TEMPLATE_NAME
    if kind == EMAIL_PATIENT_KEEP_ACCEPTED:
        return release_offer.KEEP_ACCEPTED_TEXT.format(when=when), None, template, amount
    return (
        release_offer.KEEP_DECLINED_TEXT.format(when=when),
        release_offer.keep_fallback_buttons(released_id),
        template,
        amount,
    )


async def _send_patient(
    session: AsyncSession, tenant: Tenant, offer: DepositReleaseOffer, kind: str
) -> bool | None:
    patient = await session.get(Patient, offer.patient_id) if offer.patient_id else None
    if patient is None or patient.tenant_id != tenant.id:
        return None
    text, buttons, template, amount = await _patient_message(session, tenant, offer, kind)
    # Everything read up front: the Portal branch commits, and nothing below may lazy-load.
    clinic_name, tenant_id, conversation_id = tenant.clinic_name, tenant.id, offer.conversation_id
    if patient.channel == CHANNEL_BRAIN_MESSAGE:
        patient_email = (patient.email or "").strip()
        external_id = patient.external_id or ""
        delivered = False
        if conversation_id is not None:
            delivered = await _on_portal(session, conversation_id, external_id, text, buttons)
        if kind == EMAIL_PATIENT_CHECK and patient_email:
            if await email.send_refund_check_patient_email(
                patient_email,
                clinic_name=clinic_name,
                amount_text=amount,
                conversation_link=portal_conversation_link(tenant_id),
            ):
                delivered = True
        if conversation_id is None and not (kind == EMAIL_PATIENT_CHECK and patient_email):
            return None
        return delivered
    if not patient.wa_id:
        return None
    # WhatsApp: a message only - never an e-mail (owner decision 2026-10-12).
    return await _on_whatsapp(session, tenant, offer, patient, text, buttons, template, kind)


async def _on_portal(
    session: AsyncSession, conversation_id: UUID, to: str, text: str, buttons
) -> bool:
    try:
        sender = BrainMessageSender(conversation_id=conversation_id, session=session)
        if buttons:
            await sender.send_buttons(to=to, body=text, buttons=buttons)
        else:
            await sender.send_text_message(to=to, body=text)
        await session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        logger.warning("release_offer_portal_send_failed", error_type=type(exc).__name__)
        return False


async def _on_whatsapp(
    session: AsyncSession,
    tenant: Tenant,
    offer: DepositReleaseOffer,
    patient: Patient,
    text: str,
    buttons,
    template: str,
    kind: str,
) -> bool | None:
    """True sent, False failed (retry), None not allowed (outside the window, no permission)."""
    last = await cancellation_notice.last_inbound_at(session, tenant.id, patient.id)
    inside = cancellation_notice.is_inside_window(last)
    if not inside and not offer.paid_notice_allowed:
        return None
    token = await get_waba_token(session, tenant.id)
    offer_id = offer.id
    try:
        client = WhatsAppClient.for_tenant(tenant, token)
        if inside and buttons:
            await client.send_buttons(to=patient.wa_id, body=text, buttons=buttons)
        elif inside:
            await client.send_text_message(to=patient.wa_id, body=text)
        else:
            payload_buttons = (
                release_offer.check_buttons(offer.released_appointment_id)
                if kind == EMAIL_PATIENT_CHECK
                else release_offer.offer_buttons(offer.released_appointment_id)
            )
            await client.send_template(
                to=patient.wa_id,
                template=template,
                lang=cancellation_notice.meta_language_code(tenant.language),
                variables=[release_offer.template_text(text)],
                button_payloads=[button_id for button_id, _label in payload_buttons],
            )
    except Exception as exc:  # noqa: BLE001 - e.g. the template is not approved yet
        logger.warning(
            "release_offer_whatsapp_send_failed",
            offer_id=str(offer_id),
            error_type=type(exc).__name__,
        )
        return False
    if not inside:
        try:
            await emit_usage_event(
                tenant_id=str(tenant.id),
                feature="reminders",
                amount=1,
                event_id=f"release:{kind}:{offer_id}",
            )
        except Exception as exc:  # noqa: BLE001 - fail-open, never retried
            logger.warning("usage_emit_failed", error_type=type(exc).__name__)
    return True
