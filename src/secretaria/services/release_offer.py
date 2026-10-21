"""A slot the CLINIC released with a PAID deposit (TASK-052, owner decisions 2026-10-12, 3rd round).

Instants: T release, S consultation start, B = min(T + 6 h, closing - 30 min), E = T + 24 h,
K = the "Manter" tap + 24 h. The platform never returns money (decision D15).

* `open_offer` (hub release): ONE `DepositReleaseOffer` per released appointment with a deposit;
  closing = the consultation doctor's (no doctor: the clinic's). `has_secretary=True` switches to
  the clinic's hours - a hook for Task 16 (pending); nobody passes it yet.
* "Manter horário" (`request_keep`, T..S, also after B) asks the CLINIC: `keep_requested` for 24 h
  (K). The clinic accepts (services/release_keep.py -> `claim_keep`) or refuses
  (`decline_keep`); no decision by K is a refusal. B and E do not apply while it is open and the
  refund notices that fall due meanwhile are dropped (the clinic itself is deciding).
* "Remarcar" (`choose_reschedule`) at any time while the offer is open: the deposit carries over
  unless the clinic already said it refunded; the confirmed booking (`claim_carry_over`) closes the
  refund (item, notices) and tells the clinic "REMARCADA levando o sinal".
* "Cancelar" (`choose_cancel`) always tells the clinic at once (unless it already refunded).
* No answer: B -> refund due + notice; E -> "Cancelado por falta de resposta" (terminal).
* The clinic marks "devolvido" (`mark_refunded`, or resolving the item -> `on_refund_recorded`):
  the patient answers "Recebido / Não recebido".

Every transition is ONE guarded UPDATE; the item is deduped per deposit; every send is unique per
(offer, kind, seq); every query carries the tenant id the caller resolved. Never logs a name,
phone, e-mail or text. Nothing here commits unless the docstring says so.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS, truncate_plain
from secretaria.models import (
    Appointment,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Professional,
    Tenant,
)
from secretaria.models.deposit_release_offer import (
    CANCEL_FROM,
    CARRY_CHECK_REFUNDED,
    CARRY_CHECK_REUSABLE,
    CARRY_CHECK_UNKNOWN,
    CARRY_CHECKS,
    DECISION_CANCELLED,
    DECISION_KEEP_DECLINED,
    DECISION_KEEP_REQUESTED,
    DECISION_KEPT,
    DECISION_NO_ANSWER,
    DECISION_NONE,
    DECISION_NOT_OFFERED,
    DECISION_REBOOKED,
    DECISION_RESCHEDULING,
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
    KEEP_FROM,
    KEEP_OUTCOME_ACCEPTED,
    KEEP_OUTCOME_REFUSED,
    KEEP_OUTCOME_TIMEOUT,
    OUTCOME_SKIPPED,
    REFUND_ANSWERABLE,
    REFUND_CARRYABLE,
    REFUND_CLINIC_DECIDED,
    REFUND_DUE,
    REFUND_MARKED,
    REFUND_NOT_DUE,
    REFUND_NOT_RECEIVED,
    REFUND_NOTICE_KINDS,
    REFUND_RECEIVED,
    REFUND_SOURCE_CARRY_REFUNDED,
    REFUND_SOURCE_CARRY_UNKNOWN,
    REFUND_SOURCE_KEEP_REFUSED,
    REFUND_SOURCE_KEEP_TIMEOUT,
    REFUND_SOURCE_PATIENT,
    REFUND_SOURCE_TIMEOUT,
    RESCHEDULE_FROM,
    SILENT_DECISIONS,
    DepositReleaseEmail,
    DepositReleaseOffer,
)
from secretaria.models.payment_attention_item import (
    ATTENTION_OPEN,
    ATTENTION_RESOLVED,
    REASON_RELEASED_REFUND_DUE,
    PaymentAttentionItem,
)
from secretaria.models.pix_deposit import FULFILLMENT_NEEDS_ATTENTION, FULFILLMENT_RESOLVED
from secretaria.services import release_deadlines
from secretaria.services.payments import attention
from secretaria.services.payments.db_utils import insert_ignore
from secretaria.services.payments.money import format_brl
from secretaria.services.staff_patient_message import conversation_id_for
from secretaria.services.tenant_config import professional_business_hours

logger = get_logger(__name__)

DEFAULT_TIMEZONE = "America/Sao_Paulo"
REMINDER_INTERVAL = timedelta(hours=24)
KEEP_WINDOW = timedelta(hours=24)
# A provider answer "paid, not refunded" is good for the minutes a booking takes to complete (two
# confirm clicks, the Portal's code window). After that it is asked again: the clinic may have
# refunded in the meantime (review of TASK-052). Until then it also holds the sweeps and the
# "marcar devolvido" off the deposit a booking is about to carry.
CARRY_CHECK_MAX_AGE = timedelta(minutes=15)
REFUNDED_ACTION = "refunded_in_provider_panel"
CARRIED_RESOLUTION_NOTE = "Remarcada pelo paciente levando o sinal (TASK-052)."
KEPT_RESOLUTION_NOTE = "Horário mantido pela clínica com o mesmo sinal (TASK-052)."
SYSTEM_NAME = "SecretarIA"

ACTION_KEEP = "relkeep"
ACTION_RESCHEDULE = "relresched"
ACTION_CANCEL = "relcancel"
ACTION_RECEIVED = "relgot"
ACTION_NOT_RECEIVED = "relmiss"
OFFER_ACTIONS: tuple[str, ...] = (ACTION_KEEP, ACTION_RESCHEDULE, ACTION_CANCEL)
RELEASE_OFFER_ACTIONS: tuple[str, ...] = (*OFFER_ACTIONS, ACTION_RECEIVED, ACTION_NOT_RECEIVED)
LABEL_KEEP = "Manter horário"
LABEL_RESCHEDULE = "Remarcar"
LABEL_CANCEL = "Cancelar"
LABEL_RECEIVED = "Recebido"
LABEL_NOT_RECEIVED = "Não recebido"

DEPOSIT_OUTCOME_OFFERED = "choice_offered"
REASON_CARRIED_OVER = "carried_over"

KEEP_REQUESTED_NOW = "requested"
KEEP_ALREADY_REQUESTED = "already_requested"
KEEP_AFTER_START = "past"
KEEP_AFTER_REFUND = "refunded"
KEEP_DECLINED_BEFORE = "declined"
KEEP_CLOSED = "closed"
CANCEL_NOTIFIED = "notified"
CANCEL_AFTER_REFUND = "after_refund"
CANCEL_CLOSED = "closed"
RESCHEDULE_CARRYING = "carrying"
RESCHEDULE_NEW_CHARGE = "new_charge"
RESCHEDULE_NO_ANSWER = "no_answer"
RESCHEDULE_CLOSED = "closed"

# --- patient copy (pt-BR; the CLINIC returns the money, never "we" - D15; no bank named) -------

OFFER_HEAD = (
    "Devido à sua não confirmação da ida à consulta, a clínica liberou o seu horário para que "
    "você decida o que fazer."
)
OFFER_HEAD_CONFIRMED = "A clínica liberou o seu horário para que você decida o que fazer."
CLINIC_NOTE_PREFIX = "Recado da clínica: "
OFFER_DETAIL = (
    "Você já pagou o sinal desta consulta ({amount}). Toque em *Manter horário* para pedir à "
    "clínica o mesmo horário, em *Remarcar* para escolher outro dia e horário usando o mesmo "
    "sinal, sem nova cobrança, ou em *Cancelar* para cancelar a consulta e receber o sinal de "
    "volta. Se você não responder até {deadline}, a clínica será avisada para devolver o seu sinal."
)
OFFER_DETAIL_NO_KEEP = (
    "Você já pagou o sinal desta consulta ({amount}). Toque em *Remarcar* para escolher outro dia "
    "e horário usando o mesmo sinal, sem nova cobrança, ou em *Cancelar* para cancelar a consulta "
    "e receber o sinal de volta. Se você não responder até {deadline}, a clínica será avisada para "
    "devolver o seu sinal."
)
KEEP_REQUESTED_TEXT = (
    "Avisamos a clínica que você quer manter o horário de {when}. A clínica vai verificar se o "
    "horário ainda está livre e decide em até 24 horas; a resposta chega por aqui."
)
KEEP_ALREADY_REQUESTED_TEXT = (
    "A clínica já foi avisada de que você quer manter o horário e vai responder por aqui."
)
AFTER_START_TEXT = (
    "O horário da consulta já passou. Você pode remarcar para outro dia e horário ou cancelar a "
    "consulta."
)
KEEP_AFTER_REFUND_TEXT = (
    "A clínica já informou que devolveu o seu sinal. Você pode remarcar (com novo pagamento do "
    "sinal) ou cancelar."
)
KEEP_DECLINED_TAP_TEXT = (
    "A clínica não pôde manter este horário. Você pode remarcar para outro dia e horário ou "
    "cancelar a consulta."
)
KEEP_ACCEPTED_TEXT = (
    "A clínica confirmou: seu horário de {when} está mantido. O sinal que você já pagou continua "
    "valendo para esta consulta."
)
KEEP_DECLINED_TEXT = (
    "A clínica não conseguiu manter o horário de {when}. Você pode remarcar para outro dia e "
    "horário, usando o mesmo sinal, ou cancelar e receber o sinal de volta."
)
KEEP_UNAVAILABLE_TEXT = "Não consegui confirmar o horário agora. Tente de novo em instantes."
TAP_ALREADY_KEPT_TEXT = "Sua consulta já está mantida para {when}."
TAP_ALREADY_REBOOKED_TEXT = "O sinal que você já pagou está valendo para a sua consulta."
# The provider check refused to reuse the deposit: the old one goes back, a new one was asked.
TAP_REBOOKED_NEW_CHARGE_TEXT = (
    "O sinal anterior não pôde ser usado nesta remarcação e a clínica foi avisada para devolvê-lo. "
    "Se quiser marcar uma consulta, é só pedir por aqui."
)
TAP_KEEP_AFTER_CANCEL_TEXT = (
    "Você já pediu o cancelamento desta consulta. Para marcar de novo, toque em Remarcar."
)
CANCEL_DONE_TEXT = (
    "Combinado, sua consulta foi cancelada. A clínica foi avisada e fará a devolução do seu sinal "
    "de {amount}."
)
CANCEL_AFTER_MARKED_TEXT = (
    "Sua consulta foi cancelada. A clínica já informou que devolveu o seu sinal."
)
TAP_REFUND_ALREADY_TEXT = "A devolução do seu sinal já foi pedida à clínica."
TAP_CANCEL_AFTER_KEEP_TEXT = (
    "O sinal que você já pagou está valendo para a sua consulta. Para cancelar essa consulta, use "
    "as opções da própria consulta."
)
OFFER_CLOSED_NO_ANSWER_TEXT = (
    "O prazo de 24 horas para responder terminou e a consulta foi cancelada por falta de "
    "resposta. A clínica foi avisada para devolver o seu sinal. Para marcar uma nova consulta, é "
    "só me escrever."
)
RESCHEDULE_PREFIX = (
    "Ótimo! Escolha o novo horário. Ao confirmar, conferimos se o sinal que você já pagou pode "
    "valer para a nova consulta."
)
RESCHEDULE_NEW_CHARGE_PREFIX = (
    "A clínica já informou que devolveu o seu sinal, então a nova consulta segue as regras de "
    "sinal da clínica."
)
CARRIED_OVER_NOTE = "O sinal que você já pagou vale para esta consulta. Não há nova cobrança."
# Owner 2026-10-12: the provider check said "do not reuse" (or could not tell) - a new Pix.
CARRY_REFUSED_NOTE = (
    "Não foi possível reaproveitar o sinal anterior, então esta consulta terá um sinal novo. "
    "Se o sinal anterior ainda não voltou para você, a clínica vai devolvê-lo."
)
# What the TASK-054 list shows next to the offer (`state_note`).
DEPOSIT_REUSED_LABEL = "Sinal reaproveitado — não devolver"
CARRY_REFUSED_LABEL = "Sinal novo cobrado na remarcação — devolva o sinal anterior"
CHECK_TEXT = (
    "A clínica informou que devolveu o seu sinal de {amount}. Confira a sua conta (a devolução "
    "vai para a conta de onde saiu o Pix) e nos conte:"
)
RECEIVED_TEXT = "Que bom! Obrigada por confirmar."
NOT_RECEIVED_TEXT = "Avisamos a clínica novamente. Ela vai conferir a devolução do seu sinal."
NOT_RECEIVED_RECENT_TEXT = "A clínica já foi avisada novamente há pouco."
CHECK_TOO_EARLY_TEXT = "A clínica ainda não informou a devolução do seu sinal."
CHECK_CLOSED_TEXT = "Esse pagamento já está encerrado."
NOT_FOUND_TEXT = "Não encontrei essa consulta."


def _buttons(released_appointment_id, *pairs: tuple[str, str]) -> list[tuple[str, str]]:
    return [(f"{action}|{released_appointment_id}", label) for action, label in pairs]


def offer_buttons(released_appointment_id, *, keep: bool = True) -> list[tuple[str, str]]:
    pairs = [(ACTION_RESCHEDULE, LABEL_RESCHEDULE), (ACTION_CANCEL, LABEL_CANCEL)]
    if keep:
        pairs.insert(0, (ACTION_KEEP, LABEL_KEEP))
    return _buttons(released_appointment_id, *pairs)


def keep_fallback_buttons(released_appointment_id) -> list[tuple[str, str]]:
    """Remarcar / Cancelar: after S, after a refusal, or when the refund was already marked."""
    return offer_buttons(released_appointment_id, keep=False)


def check_buttons(released_appointment_id) -> list[tuple[str, str]]:
    return _buttons(
        released_appointment_id,
        (ACTION_RECEIVED, LABEL_RECEIVED),
        (ACTION_NOT_RECEIVED, LABEL_NOT_RECEIVED),
    )


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _zone(timezone: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(timezone or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001
        return ZoneInfo(DEFAULT_TIMEZONE)


def when_text(value: datetime, timezone: str | None) -> str:
    """ "20:00 de 05/03" in the clinic's zone (a deadline)."""
    return _as_utc(value).astimezone(_zone(timezone)).strftime("%H:%M de %d/%m")


def slot_text(value: datetime, timezone: str | None) -> str:
    """ "08/03 às 10:00" in the clinic's zone (a consultation)."""
    return _as_utc(value).astimezone(_zone(timezone)).strftime("%d/%m às %H:%M")


def offer_body(
    *,
    deposit: PixDeposit,
    decide_by_at: datetime,
    timezone: str | None,
    justification: str | None,
    confirmed: bool,
    keep: bool = True,
    limit: int = MAX_INTERACTIVE_BODY_CHARS,
) -> str:
    """The release notice of a PAID slot. Only the clinic's note is cut to fit."""
    head = OFFER_HEAD_CONFIRMED if confirmed else OFFER_HEAD
    detail = (OFFER_DETAIL if keep else OFFER_DETAIL_NO_KEEP).format(
        amount=format_brl(deposit.amount_cents), deadline=when_text(decide_by_at, timezone)
    )
    fixed = f"{head}\n\n{detail}"
    note = (justification or "").strip()
    room = limit - len(fixed) - len("\n\n") - len(CLINIC_NOTE_PREFIX)
    if not note or room <= 0:
        return truncate_plain(fixed, limit)
    return f"{head}\n\n{CLINIC_NOTE_PREFIX}{truncate_plain(note, room)}\n\n{detail}"


def template_text(text: str) -> str:
    """A WhatsApp template parameter may not carry line breaks: one line, single spaces."""
    return " ".join(text.split())


def warning_text(deposit: PixDeposit) -> str:
    """What the CLINIC reads before releasing a paid slot (409 `retention_ack_required`)."""
    return (
        f"O paciente já pagou o sinal ({format_brl(deposit.amount_cents)}). Ao liberar o horário, "
        "ele poderá pedir para manter o horário (a clínica decide), remarcar usando o mesmo sinal "
        "ou cancelar. Se ele cancelar ou não responder até o prazo, a clínica receberá avisos "
        "para devolver o sinal por inteiro pelo painel do provedor de pagamento."
    )


def console_conversation_link(conversation_id: UUID | None) -> str | None:
    """The clinic console's thread (Brain-Message-Frontend `conversationHref`), or None."""
    base = (get_settings().BRAIN_MESSAGE_PUBLIC_URL or "").strip().rstrip("/")
    if not base or conversation_id is None:
        return None
    return f"{base}/chat/?conversationId={conversation_id}"


def keep_allowed(offer: DepositReleaseOffer, now: datetime | None = None) -> bool:
    """Whether "Manter horário" may still be offered/asked: before S, refund not marked."""
    now = _now(now)
    return (
        offer.deposit_paid
        and offer.decision in KEEP_FROM
        and offer.refund_status in REFUND_CARRYABLE
        and (offer.slot_start_at is None or _as_utc(offer.slot_start_at) > now)
    )


def patient_decision(offer: DepositReleaseOffer) -> str:
    """The patient's side, as the clinic list (TASK-054) shows it."""
    if offer.decision == DECISION_NOT_OFFERED:
        return "not_offered"
    if offer.refund_status == REFUND_RECEIVED:
        return "received"
    if offer.refund_status == REFUND_NOT_RECEIVED:
        return "not_received"
    return {
        DECISION_KEEP_REQUESTED: "keep_requested",
        DECISION_KEPT: "kept",
        DECISION_KEEP_DECLINED: "keep_declined",
        DECISION_REBOOKED: "rescheduled",
        DECISION_CANCELLED: "cancelled",
        DECISION_NO_ANSWER: "cancelled_no_answer",
    }.get(offer.decision, "no_answer")


# --- opening -----------------------------------------------------------------------------------


async def _closing_hours(
    session: AsyncSession, tenant: Tenant, appointment: Appointment, has_secretary: bool | None
):
    """The consultation doctor's hours (no doctor: the clinic's); `has_secretary` = clinic's."""
    if has_secretary or appointment.professional_id is None:
        return tenant.business_hours
    professional = await session.get(Professional, appointment.professional_id)
    if professional is None or professional.tenant_id != tenant.id:
        return tenant.business_hours
    return professional_business_hours(professional, tenant)


async def open_offer(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    now: datetime | None = None,
    paid_notice_allowed: bool = False,
    has_secretary: bool | None = None,
) -> DepositReleaseOffer | None:
    """Record the release of an appointment with a deposit of THIS clinic; idempotent.

    PAID: an open offer with T, S, B and E. Anything else: `not_offered` (list only). None when the
    deposit or the appointment is not this clinic's. `has_secretary` is a hook for Task 16
    (pending: brain-api roles); today nobody passes it, so B uses the doctor's closing. No commit.
    """
    if (
        deposit.tenant_id != tenant.id
        or appointment.tenant_id != tenant.id
        or deposit.appointment_id != appointment.id
    ):
        return None
    now = _now(now)
    paid = deposit.status is PixDepositStatus.PAID
    patient = await session.get(Patient, appointment.patient_id) if appointment.patient_id else None
    if patient is not None and patient.tenant_id != tenant.id:
        patient = None
    conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
    decide_by = (
        release_deadlines.decide_by(
            released_at=now,
            business_hours=await _closing_hours(session, tenant, appointment, has_secretary),
            timezone=tenant.timezone,
        )
        if paid
        else now
    )
    await insert_ignore(
        session,
        DepositReleaseOffer,
        {
            "tenant_id": tenant.id,
            "deposit_id": deposit.id,
            "released_appointment_id": appointment.id,
            "patient_id": appointment.patient_id,
            "conversation_id": conversation_id,
            "deposit_paid": paid,
            "decision": DECISION_NONE if paid else DECISION_NOT_OFFERED,
            "refund_status": REFUND_NOT_DUE,
            "released_at": now,
            "slot_start_at": appointment.start_at,
            "decide_by_at": decide_by,
            "expires_at": release_deadlines.expires_at(now) if paid else now,
            "paid_notice_allowed": bool(paid_notice_allowed),
        },
        index_elements=["released_appointment_id"],
    )
    offer = await find_offer(session, tenant_id=tenant.id, released_appointment_id=appointment.id)
    if offer is not None:
        logger.info(
            "deposit_release_offer_opened",
            tenant_id=str(tenant.id),
            offer_id=str(offer.id),
            paid=paid,
        )
    return offer


async def find_offer(
    session: AsyncSession, *, tenant_id: UUID, released_appointment_id: UUID
) -> DepositReleaseOffer | None:
    return await session.scalar(
        select(DepositReleaseOffer).where(
            DepositReleaseOffer.tenant_id == tenant_id,
            DepositReleaseOffer.released_appointment_id == released_appointment_id,
        )
    )


async def record_contact(session: AsyncSession, *, offer_id: UUID, now: datetime) -> None:
    """The patient tapped a button of the offer (Manter, Remarcar or Cancelar): "contato"."""
    await session.execute(
        update(DepositReleaseOffer)
        .where(DepositReleaseOffer.id == offer_id, DepositReleaseOffer.first_contact_at.is_(None))
        .values(first_contact_at=now)
        .execution_options(synchronize_session=False)
    )


# --- scheduling and the refund item ------------------------------------------------------------


async def _schedule(
    session: AsyncSession, offer: DepositReleaseOffer, kind: str, due_at: datetime, seq: int = 0
) -> None:
    await insert_ignore(
        session,
        DepositReleaseEmail,
        {
            "tenant_id": offer.tenant_id,
            "offer_id": offer.id,
            "kind": kind,
            "seq": seq,
            "due_at": _as_utc(due_at),
            "attempts": 0,
        },
        index_elements=["offer_id", "kind", "seq"],
    )


async def skip_kinds(
    session: AsyncSession, offer_id: UUID, kinds: tuple[str, ...] | None, now: datetime
) -> None:
    """Every unclaimed send of the offer (of `kinds`, or all when None) will never go out."""
    query = update(DepositReleaseEmail).where(
        DepositReleaseEmail.offer_id == offer_id, DepositReleaseEmail.claimed_at.is_(None)
    )
    if kinds is not None:
        query = query.where(DepositReleaseEmail.kind.in_(kinds))
    await session.execute(
        query.values(claimed_at=now, outcome=OUTCOME_SKIPPED).execution_options(
            synchronize_session=False
        )
    )


async def _open_refund_item(session: AsyncSession, offer: DepositReleaseOffer) -> None:
    deposit = await session.get(PixDeposit, offer.deposit_id)
    if deposit is None or deposit.tenant_id != offer.tenant_id:
        logger.error("deposit_release_offer_deposit_missing", offer_id=str(offer.id))
        return
    released = await session.get(Appointment, offer.released_appointment_id)
    charge_ref = deposit.provider_charge_id or deposit.asaas_payment_id or str(deposit.id)
    await attention.open_attention_item(
        session,
        tenant_id=offer.tenant_id,
        reason=REASON_RELEASED_REFUND_DUE,
        # The offer id leads the reference: a deposit released a second time (after a keep or a
        # carry closed the first item) is a NEW refund and must not hit the first item's key.
        dedupe_key=attention.attention_dedupe_key(
            deposit.provider, f"{offer.id}:{charge_ref}", REASON_RELEASED_REFUND_DUE
        ),
        deposit=deposit,
        suggested_refund_cents=deposit.amount_cents,
        slot_start=released.start_at if released is not None else None,
        slot_end=released.end_at if released is not None else None,
        service_label=released.appointment_type if released is not None else None,
    )


async def _close_refund_item(
    session: AsyncSession, offer: DepositReleaseOffer, now: datetime, note: str
) -> None:
    """The money is in use again (kept / carried over): the open refund item is closed as
    "rebooked" and the deposit is no longer flagged. Never touches the deposit's status."""
    await session.execute(
        update(PaymentAttentionItem)
        .where(
            PaymentAttentionItem.tenant_id == offer.tenant_id,
            PaymentAttentionItem.deposit_id == offer.deposit_id,
            PaymentAttentionItem.reason == REASON_RELEASED_REFUND_DUE,
            PaymentAttentionItem.status == ATTENTION_OPEN,
        )
        .values(
            status=ATTENTION_RESOLVED,
            resolved_at=now,
            resolution_action="rebooked",
            resolution_note=note,
            resolved_by_name=SYSTEM_NAME,
        )
        .execution_options(synchronize_session=False)
    )
    still_open = await session.scalar(
        select(func.count())
        .select_from(PaymentAttentionItem)
        .where(
            PaymentAttentionItem.tenant_id == offer.tenant_id,
            PaymentAttentionItem.deposit_id == offer.deposit_id,
            PaymentAttentionItem.status == ATTENTION_OPEN,
        )
    )
    if not still_open:
        await session.execute(
            update(PixDeposit)
            .where(
                PixDeposit.id == offer.deposit_id,
                PixDeposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION,
            )
            .values(fulfillment_status=FULFILLMENT_RESOLVED)
            .execution_options(synchronize_session=False)
        )


def _reminder_at(offer: DepositReleaseOffer, now: datetime) -> datetime:
    """E, or 24 h from now when E already passed (a cancel after a refused "Manter")."""
    expires = _as_utc(offer.expires_at)
    return expires if expires > now else now + REMINDER_INTERVAL


async def _make_due(
    session: AsyncSession, offer: DepositReleaseOffer, *, now: datetime, source: str, extra=()
) -> bool:
    """`not_due` -> `due` (guarded); opens the item; B's notice for silence; the reminder."""
    moved = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.refund_status == REFUND_NOT_DUE,
            *extra,
        )
        .values(refund_status=REFUND_DUE, refund_source=source, refund_due_at=now)
        .execution_options(synchronize_session=False)
    )
    if moved.rowcount != 1:
        return False
    await _open_refund_item(session, offer)
    if source == REFUND_SOURCE_TIMEOUT:
        await _schedule(session, offer, EMAIL_REFUND_NOANSWER, offer.decide_by_at)
        await _schedule(session, offer, EMAIL_REFUND_DAY, offer.expires_at)
    elif source == REFUND_SOURCE_PATIENT:
        await _schedule(session, offer, EMAIL_REFUND_DAY, _reminder_at(offer, now))
    logger.info(
        "deposit_release_offer_refund_due",
        tenant_id=str(offer.tenant_id),
        offer_id=str(offer.id),
        source=source,
    )
    return True


async def _reload(session: AsyncSession, offer_id: UUID) -> DepositReleaseOffer:
    return await session.get(DepositReleaseOffer, offer_id, populate_existing=True)


# --- Manter: a request to the clinic -----------------------------------------------------------


async def request_keep(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> str:
    """ "Manter horário" (T..S, also after B): ask the clinic; 24 h for it to decide. Does not
    commit. Outcomes: KEEP_REQUESTED_NOW / KEEP_ALREADY_REQUESTED / KEEP_AFTER_START /
    KEEP_AFTER_REFUND / KEEP_DECLINED_BEFORE / KEEP_CLOSED."""
    now = _now(now)
    asked = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.deposit_paid.is_(True),
            DepositReleaseOffer.decision.in_(KEEP_FROM),
            DepositReleaseOffer.refund_status.in_(REFUND_CARRYABLE),
            or_(
                DepositReleaseOffer.slot_start_at.is_(None),
                DepositReleaseOffer.slot_start_at > now,
            ),
        )
        .values(
            decision=DECISION_KEEP_REQUESTED,
            keep_requested_at=now,
            keep_deadline_at=now + KEEP_WINDOW,
            patient_answered_at=now,
            first_contact_at=func.coalesce(DepositReleaseOffer.first_contact_at, now),
        )
        .execution_options(synchronize_session=False)
    )
    if asked.rowcount == 1:
        await _schedule(session, offer, EMAIL_KEEP_REQUEST, now)
        logger.info("deposit_release_offer_keep_requested", offer_id=str(offer.id))
        return KEEP_REQUESTED_NOW
    current = await _reload(session, offer.id)
    if current.decision == DECISION_KEEP_REQUESTED:
        return KEEP_ALREADY_REQUESTED
    if current.decision == DECISION_KEEP_DECLINED:
        return KEEP_DECLINED_BEFORE
    if current.decision in KEEP_FROM and current.refund_status not in REFUND_CARRYABLE:
        return KEEP_AFTER_REFUND
    if current.decision in KEEP_FROM:
        return KEEP_AFTER_START
    return KEEP_CLOSED


async def _move_deposit(
    session: AsyncSession, offer: DepositReleaseOffer, new_appointment_id: UUID
) -> bool:
    moved = await session.execute(
        update(PixDeposit)
        .where(
            PixDeposit.id == offer.deposit_id,
            PixDeposit.tenant_id == offer.tenant_id,
            PixDeposit.status == PixDepositStatus.PAID,
            PixDeposit.appointment_id == offer.released_appointment_id,
        )
        .values(appointment_id=new_appointment_id)
        .execution_options(synchronize_session=False)
    )
    return moved.rowcount == 1


async def _undo(session: AsyncSession, offer_id: UUID, decision: str, refund_status: str) -> None:
    await session.execute(
        update(DepositReleaseOffer)
        .where(DepositReleaseOffer.id == offer_id)
        .values(
            decision=decision,
            refund_status=refund_status,
            closed_at=None,
            new_appointment_id=None,
            keep_outcome=None,
            keep_resolved_at=None,
            keep_resolved_by=None,
        )
        .execution_options(synchronize_session=False)
    )


async def claim_keep(
    session: AsyncSession,
    *,
    offer: DepositReleaseOffer,
    new_appointment: Appointment,
    resolved_by: str | None,
    now: datetime | None = None,
) -> bool:
    """The clinic ACCEPTED "Manter" (inside the transaction that inserted `new_appointment`):
    `kept`, the deposit moves, a due refund is undone (item closed, notices dropped), the patient
    is told. Does not commit."""
    if new_appointment.tenant_id != offer.tenant_id:
        return False
    now = _now(now)
    previous_refund = offer.refund_status
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.decision == DECISION_KEEP_REQUESTED,
            DepositReleaseOffer.refund_status.in_(REFUND_CARRYABLE),
            or_(
                DepositReleaseOffer.slot_start_at.is_(None),
                DepositReleaseOffer.slot_start_at > now,
            ),
        )
        .values(
            decision=DECISION_KEPT,
            keep_outcome=KEEP_OUTCOME_ACCEPTED,
            keep_resolved_at=now,
            keep_resolved_by=resolved_by,
            new_appointment_id=new_appointment.id,
            closed_at=now,
            refund_status=REFUND_NOT_DUE,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return False
    if not await _move_deposit(session, offer, new_appointment.id):
        await _undo(session, offer.id, DECISION_KEEP_REQUESTED, previous_refund)
        logger.warning("deposit_release_offer_keep_lost_deposit", offer_id=str(offer.id))
        return False
    await _close_refund_item(session, offer, now, KEPT_RESOLUTION_NOTE)
    await skip_kinds(session, offer.id, REFUND_NOTICE_KINDS, now)
    await _schedule(session, offer, EMAIL_PATIENT_KEEP_ACCEPTED, now)
    logger.info("deposit_release_offer_kept", offer_id=str(offer.id))
    return True


async def decline_keep(
    session: AsyncSession,
    *,
    offer: DepositReleaseOffer,
    outcome: str,
    resolved_by: str | None = None,
    now: datetime | None = None,
) -> bool:
    """The clinic REFUSED "Manter" (`outcome="refused"`) or K passed (`"timeout"`): the refund
    falls due, the clinic gets the refund steps now (+ a reminder in 24 h), the patient gets
    Remarcar / Cancelar. True when THIS call did it. Does not commit."""
    now = _now(now)
    timed_out = outcome == KEEP_OUTCOME_TIMEOUT
    guard = [
        DepositReleaseOffer.id == offer.id,
        DepositReleaseOffer.tenant_id == offer.tenant_id,
        DepositReleaseOffer.decision == DECISION_KEEP_REQUESTED,
    ]
    if timed_out:
        guard.append(DepositReleaseOffer.keep_deadline_at <= now)
    declined = await session.execute(
        update(DepositReleaseOffer)
        .where(*guard)
        .values(
            decision=DECISION_KEEP_DECLINED,
            keep_outcome=KEEP_OUTCOME_TIMEOUT if timed_out else KEEP_OUTCOME_REFUSED,
            keep_resolved_at=now,
            keep_resolved_by=resolved_by,
        )
        .execution_options(synchronize_session=False)
    )
    if declined.rowcount != 1:
        return False
    source = REFUND_SOURCE_KEEP_TIMEOUT if timed_out else REFUND_SOURCE_KEEP_REFUSED
    await _make_due(session, offer, now=now, source=source)
    current = await _reload(session, offer.id)
    if current.refund_status == REFUND_DUE:
        await _schedule(session, current, EMAIL_KEEP_DECLINED, now)
        await _schedule(session, current, EMAIL_REFUND_DAY, now + REMINDER_INTERVAL, seq=1)
    await _schedule(session, current, EMAIL_PATIENT_KEEP_DECLINED, now)
    logger.info("deposit_release_offer_keep_declined", offer_id=str(offer.id), outcome=outcome)
    return True


# --- Cancelar / Remarcar -----------------------------------------------------------------------


async def choose_cancel(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> str:
    """ "Cancelar": the refund falls due and the clinic is told RIGHT AWAY - unless it already said
    it refunded. Does not commit."""
    now = _now(now)
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.deposit_paid.is_(True),
            DepositReleaseOffer.decision.in_(CANCEL_FROM),
        )
        .values(
            decision=DECISION_CANCELLED,
            patient_answered_at=now,
            closed_at=now,
            first_contact_at=func.coalesce(DepositReleaseOffer.first_contact_at, now),
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return CANCEL_CLOSED
    await _make_due(session, offer, now=now, source=REFUND_SOURCE_PATIENT)
    current = await _reload(session, offer.id)
    if current.refund_status == REFUND_DUE:
        await _schedule(session, current, EMAIL_REFUND_CANCEL, now)
        await _schedule(session, current, EMAIL_REFUND_DAY, _reminder_at(current, now))
        return CANCEL_NOTIFIED
    return CANCEL_AFTER_REFUND


async def choose_reschedule(
    session: AsyncSession,
    *,
    offer: DepositReleaseOffer,
    conversation_id: UUID,
    now: datetime | None = None,
) -> str:
    """ "Remarcar" at any time while the offer is open. CARRYING (the next booking carries the
    deposit), NEW_CHARGE (the clinic already said it refunded), NO_ANSWER (closed at E, never
    reopened) or CLOSED (kept / rebooked / not offered). Does not commit."""
    now = _now(now)
    current = await _reload(session, offer.id)
    if current is None or current.tenant_id != offer.tenant_id or not current.deposit_paid:
        return RESCHEDULE_CLOSED
    if current.decision == DECISION_NO_ANSWER:
        return RESCHEDULE_NO_ANSWER
    if current.decision not in RESCHEDULE_FROM:
        return RESCHEDULE_CLOSED
    if current.refund_status not in REFUND_CARRYABLE:
        return RESCHEDULE_NEW_CHARGE
    values: dict = {
        "decision": DECISION_RESCHEDULING,
        "conversation_id": conversation_id,
        "first_contact_at": func.coalesce(DepositReleaseOffer.first_contact_at, now),
    }
    if now >= _as_utc(current.expires_at):
        # Tapped AFTER E: it is the patient's answer, so the 24 h sweep must not close it as "no
        # answer" (the guard compares `patient_answered_at` with `expires_at`).
        values["patient_answered_at"] = now
    await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.decision.in_(RESCHEDULE_FROM),
            DepositReleaseOffer.refund_status.in_(REFUND_CARRYABLE),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    after = await _reload(session, offer.id)
    if after.decision == DECISION_RESCHEDULING and after.refund_status in REFUND_CARRYABLE:
        return RESCHEDULE_CARRYING
    return RESCHEDULE_NEW_CHARGE if after.decision in RESCHEDULE_FROM else RESCHEDULE_CLOSED


def _carrying(tenant_id: UUID, conversation_id: UUID) -> tuple:
    return (
        DepositReleaseOffer.tenant_id == tenant_id,
        DepositReleaseOffer.conversation_id == conversation_id,
        DepositReleaseOffer.deposit_paid.is_(True),
        DepositReleaseOffer.decision == DECISION_RESCHEDULING,
        DepositReleaseOffer.refund_status.in_(REFUND_CARRYABLE),
    )


async def carrying_offer(
    session: AsyncSession, *, tenant_id: UUID, conversation_id: UUID
) -> DepositReleaseOffer | None:
    """This conversation's open "Remarcar" (THIS clinic) whose paid deposit may be carried."""
    return await session.scalar(
        select(DepositReleaseOffer)
        .where(*_carrying(tenant_id, conversation_id))
        .order_by(DepositReleaseOffer.released_at.desc())
        .limit(1)
    )


async def carry_over_pending(
    session: AsyncSession, *, tenant_id: UUID, conversation_id: UUID, now: datetime | None = None
) -> bool:
    """Whether this conversation has a "Remarcar" that may carry a paid deposit. Only the
    provider check (`record_carry_check` = REUSABLE, Task 10) lets `claim_carry_over` carry it."""
    offer = await carrying_offer(session, tenant_id=tenant_id, conversation_id=conversation_id)
    return offer is not None and offer.carry_check in (None, CARRY_CHECK_REUSABLE)


async def claim_carry_over(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    new_appointment: Appointment,
    now: datetime | None = None,
) -> PixDeposit | None:
    """Inside the booking's transaction: move the paid deposit to `new_appointment`, close the
    offer as `rebooked`, undo a due refund (item closed, notices dropped), lock "marcar devolvido"
    (`deposit_reused_at`) and tell the clinic "REMARCADA levando o sinal". ONLY when the provider
    check recorded at the confirmation said REUSABLE (owner 2026-10-12). None when nothing is
    carried. Does not commit."""
    if new_appointment.tenant_id != tenant_id:
        return None
    now = _now(now)
    offer = await carrying_offer(session, tenant_id=tenant_id, conversation_id=conversation_id)
    if offer is None or offer.carry_check != CARRY_CHECK_REUSABLE:
        return None
    previous_refund = offer.refund_status
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.carry_check == CARRY_CHECK_REUSABLE,
            *_carrying(tenant_id, conversation_id),
        )
        .values(
            decision=DECISION_REBOOKED,
            closed_at=now,
            patient_answered_at=now,
            new_appointment_id=new_appointment.id,
            refund_status=REFUND_NOT_DUE,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None
    if not await _move_deposit(session, offer, new_appointment.id):
        await _undo(session, offer.id, DECISION_RESCHEDULING, previous_refund)
        logger.warning("deposit_release_offer_carry_lost_deposit", offer_id=str(offer.id))
        return None
    # The money is in use again: from now on "marcar devolvido" answers 409 `deposit_reused`.
    await session.execute(
        update(DepositReleaseOffer)
        .where(DepositReleaseOffer.id == offer.id)
        .values(deposit_reused_at=now)
        .execution_options(synchronize_session=False)
    )
    await _close_refund_item(session, offer, now, CARRIED_RESOLUTION_NOTE)
    await skip_kinds(session, offer.id, REFUND_NOTICE_KINDS, now)
    await _schedule(session, offer, EMAIL_REBOOKED_DEPOSIT, now)
    logger.info("deposit_release_offer_rebooked", tenant_id=str(tenant_id), offer_id=str(offer.id))
    return await session.get(PixDeposit, offer.deposit_id, populate_existing=True)


async def record_carry_check(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    offer_id: UUID,
    outcome: str,
    now: datetime | None = None,
) -> str | None:
    """Store what the payment provider said when the patient confirmed the booking of a
    "Remarcar" on THIS clinic's paid deposit (owner 2026-10-12). Recorded ONCE: the first answer
    stands and is returned again (two confirm clicks never get two different answers).

    REUSABLE: the booking may carry the deposit (`claim_carry_over`). REFUNDED / UNKNOWN
    (fail-safe; anything unexpected counts as UNKNOWN): no carry - the booking charges a NEW Pix;
    the offer closes as rebooked-with-a-new-charge, the old deposit becomes a refund the clinic
    owes (item opened) and the clinic gets `clinic_carry_refused` at once (the patient may have
    paid twice). None when this clinic has no such open "Remarcar". Does not commit.
    """
    if outcome not in CARRY_CHECKS:
        outcome = CARRY_CHECK_UNKNOWN
    now = _now(now)
    offer = await session.scalar(
        select(DepositReleaseOffer).where(
            DepositReleaseOffer.id == offer_id, DepositReleaseOffer.tenant_id == tenant_id
        )
    )
    if offer is None:
        return None
    if offer.carry_check is not None or offer.conversation_id is None:
        return offer.carry_check
    values: dict = {"carry_check": outcome, "carry_checked_at": now}
    if outcome != CARRY_CHECK_REUSABLE:
        values.update(
            decision=DECISION_REBOOKED,
            closed_at=now,
            patient_answered_at=func.coalesce(DepositReleaseOffer.patient_answered_at, now),
        )
    wrote = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.carry_check.is_(None),
            *_carrying(tenant_id, offer.conversation_id),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    current = await _reload(session, offer.id)
    if wrote.rowcount != 1:
        return current.carry_check
    if outcome != CARRY_CHECK_REUSABLE:
        source = (
            REFUND_SOURCE_CARRY_REFUNDED
            if outcome == CARRY_CHECK_REFUNDED
            else REFUND_SOURCE_CARRY_UNKNOWN
        )
        await _make_due(session, current, now=now, source=source)  # no-op when already due
        await _schedule(session, current, EMAIL_CARRY_REFUSED, now)
        logger.warning(
            "deposit_release_offer_carry_refused",
            tenant_id=str(tenant_id),
            offer_id=str(offer.id),
            check=outcome,
        )
    else:
        logger.info(
            "deposit_release_offer_carry_verified", tenant_id=str(tenant_id), offer_id=str(offer.id)
        )
    return outcome


def _carry_not_holding(now: datetime) -> tuple:
    """SQL guard: NO fresh REUSABLE provider answer is pending on the offer (a booking is about to
    carry the deposit, so B and E must not close it under the booking)."""
    cutoff = now - CARRY_CHECK_MAX_AGE
    return (
        or_(
            DepositReleaseOffer.carry_check.is_(None),
            DepositReleaseOffer.carry_check != CARRY_CHECK_REUSABLE,
            DepositReleaseOffer.carry_checked_at <= cutoff,
        ),
    )


def _not_answered_since_e() -> tuple:
    """SQL guard for E: a patient answer given AFTER E (a late "Remarcar") is not silence."""
    return (
        or_(
            DepositReleaseOffer.patient_answered_at.is_(None),
            DepositReleaseOffer.patient_answered_at <= DepositReleaseOffer.expires_at,
        ),
    )


def carry_check_is_fresh(offer: DepositReleaseOffer, now: datetime) -> bool:
    """Whether the offer holds a REUSABLE provider answer read within `CARRY_CHECK_MAX_AGE`."""
    return (
        offer.carry_check == CARRY_CHECK_REUSABLE
        and offer.carry_checked_at is not None
        and _as_utc(offer.carry_checked_at) > now - CARRY_CHECK_MAX_AGE
    )


async def reset_stale_carry_check(
    session: AsyncSession, *, tenant_id: UUID, offer_id: UUID, now: datetime | None = None
) -> bool:
    """Forget a REUSABLE answer older than `CARRY_CHECK_MAX_AGE` (the booking it was read for never
    completed) so the next confirmation asks the provider again. Never touches a deposit already
    reused. Does not commit."""
    now = _now(now)
    cleared = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer_id,
            DepositReleaseOffer.tenant_id == tenant_id,
            DepositReleaseOffer.decision == DECISION_RESCHEDULING,
            DepositReleaseOffer.carry_check == CARRY_CHECK_REUSABLE,
            DepositReleaseOffer.carry_checked_at <= now - CARRY_CHECK_MAX_AGE,
            DepositReleaseOffer.deposit_reused_at.is_(None),
        )
        .values(carry_check=None, carry_checked_at=None)
        .execution_options(synchronize_session=False)
    )
    return cleared.rowcount == 1


# --- B, E, K -----------------------------------------------------------------------------------


async def settle_if_expired(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> bool:
    """B passed with no answer (or an unfinished "Remarcar"): refund due (`timeout`)."""
    now = _now(now)
    return await _make_due(
        session,
        offer,
        now=now,
        source=REFUND_SOURCE_TIMEOUT,
        extra=(
            DepositReleaseOffer.decision.in_(SILENT_DECISIONS),
            DepositReleaseOffer.deposit_paid.is_(True),
            DepositReleaseOffer.decide_by_at <= now,
            *_carry_not_holding(now),
        ),
    )


async def close_if_no_answer(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> bool:
    """E passed without a final choice: "Cancelado por falta de resposta" (terminal); the refund
    is due as on "Cancelar" (settled here too if B was never swept). Does not commit."""
    now = _now(now)
    closed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.decision.in_(SILENT_DECISIONS),
            DepositReleaseOffer.deposit_paid.is_(True),
            DepositReleaseOffer.expires_at <= now,
            *_carry_not_holding(now),
            *_not_answered_since_e(),
        )
        .values(decision=DECISION_NO_ANSWER, closed_at=now)
        .execution_options(synchronize_session=False)
    )
    if closed.rowcount != 1:
        return False
    await _make_due(session, offer, now=now, source=REFUND_SOURCE_TIMEOUT)
    logger.info("deposit_release_offer_no_answer", offer_id=str(offer.id))
    return True


async def expire_offers(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 50
) -> int:
    """The sweep: B, E and K. One commit per offer; one failure never stops the batch. Returns
    how many transitions THIS tick made. Commits."""
    now = _now(now)
    paid = DepositReleaseOffer.deposit_paid.is_(True)
    silent = DepositReleaseOffer.decision.in_(SILENT_DECISIONS)
    queries = (
        select(DepositReleaseOffer.id).where(
            paid,
            silent,
            DepositReleaseOffer.refund_status == REFUND_NOT_DUE,
            DepositReleaseOffer.decide_by_at <= now,
        ),
        select(DepositReleaseOffer.id).where(
            paid,
            silent,
            DepositReleaseOffer.expires_at <= now,
            *_carry_not_holding(now),
            *_not_answered_since_e(),
        ),
        select(DepositReleaseOffer.id).where(
            paid,
            DepositReleaseOffer.decision == DECISION_KEEP_REQUESTED,
            DepositReleaseOffer.keep_deadline_at <= now,
        ),
    )
    ids: list[UUID] = []
    for query in queries:
        ids.extend((await session.scalars(query.limit(limit))).all())
    changed = 0
    for offer_id in dict.fromkeys(ids):
        try:
            offer = await session.get(DepositReleaseOffer, offer_id)
            if offer is not None:
                changed += int(await settle_if_expired(session, offer=offer, now=now))
                changed += int(await close_if_no_answer(session, offer=offer, now=now))
                changed += int(
                    await decline_keep(session, offer=offer, outcome=KEEP_OUTCOME_TIMEOUT, now=now)
                )
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - one offer never stops the batch
            await session.rollback()
            logger.warning(
                "deposit_release_offer_expire_failed",
                offer_id=str(offer_id),
                error_type=type(exc).__name__,
            )
    return changed


# --- devolvido / Recebido / Não recebido -------------------------------------------------------


async def record_received(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> str:
    """ "Recebido": `closed`, `already_closed`, `too_early` (not marked yet) or `not_refund`."""
    now = _now(now)
    closed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.refund_status.in_(REFUND_ANSWERABLE),
        )
        .values(refund_status=REFUND_RECEIVED, patient_answered_at=now)
        .execution_options(synchronize_session=False)
    )
    if closed.rowcount == 1:
        await skip_kinds(session, offer.id, None, now)
        return "closed"
    current = await _reload(session, offer.id)
    if current is not None and current.refund_status in (REFUND_RECEIVED, REFUND_CLINIC_DECIDED):
        return "already_closed"
    if current is not None and current.refund_status == REFUND_DUE:
        return "too_early"
    return "not_refund"


async def request_reminder(
    session: AsyncSession, *, offer: DepositReleaseOffer, now: datetime | None = None
) -> str:
    """ "Não recebido": `sent`, `recent` (< 24 h), `too_early`, `closed` or `not_refund`."""
    now = _now(now)
    claimed = await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.tenant_id == offer.tenant_id,
            DepositReleaseOffer.refund_status.in_(REFUND_ANSWERABLE),
            or_(
                DepositReleaseOffer.last_reminder_at.is_(None),
                DepositReleaseOffer.last_reminder_at <= now - REMINDER_INTERVAL,
            ),
        )
        .values(refund_status=REFUND_NOT_RECEIVED, last_reminder_at=now, patient_answered_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 1:
        last_seq = await session.scalar(
            select(func.coalesce(func.max(DepositReleaseEmail.seq), 0)).where(
                DepositReleaseEmail.offer_id == offer.id,
                DepositReleaseEmail.kind == EMAIL_REFUND_REMINDER,
            )
        )
        await _schedule(session, offer, EMAIL_REFUND_REMINDER, now, seq=int(last_seq or 0) + 1)
        return "sent"
    current = await _reload(session, offer.id)
    if current is None:
        return "not_refund"
    if current.refund_status in (REFUND_RECEIVED, REFUND_CLINIC_DECIDED):
        return "closed"
    if current.refund_status == REFUND_DUE:
        return "too_early"
    if current.refund_status in REFUND_ANSWERABLE:
        return "recent"
    return "not_refund"


async def on_refund_recorded(
    session: AsyncSession, *, tenant_id: UUID, item: PaymentAttentionItem, now: datetime | None
) -> UUID | None:
    """Called by `attention_view.resolve_item` for every resolution. For THIS clinic's
    `released_refund_due` item: "refunded in the panel" -> `marked` + the patient's check now (an
    open "Manter" request ends as refused); any other decision -> `clinic_decided`, nothing pending
    goes out. Does not commit."""
    if item.reason != REASON_RELEASED_REFUND_DUE or item.deposit_id is None:
        return None
    now = _now(now)
    offer = await session.scalar(
        select(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.tenant_id == tenant_id,
            DepositReleaseOffer.deposit_id == item.deposit_id,
            DepositReleaseOffer.refund_status == REFUND_DUE,
        )
        .order_by(DepositReleaseOffer.released_at.desc())
        .limit(1)
    )
    if offer is None:
        return None
    refunded = item.resolution_action == REFUNDED_ACTION
    moved = await session.execute(
        update(DepositReleaseOffer)
        .where(DepositReleaseOffer.id == offer.id, DepositReleaseOffer.refund_status == REFUND_DUE)
        .values(
            refund_status=REFUND_MARKED if refunded else REFUND_CLINIC_DECIDED,
            refund_marked_at=now if refunded else None,
        )
        .execution_options(synchronize_session=False)
    )
    if moved.rowcount != 1:
        return None
    await session.execute(
        update(DepositReleaseOffer)
        .where(
            DepositReleaseOffer.id == offer.id,
            DepositReleaseOffer.decision == DECISION_KEEP_REQUESTED,
        )
        .values(
            decision=DECISION_KEEP_DECLINED,
            keep_outcome=KEEP_OUTCOME_REFUSED,
            keep_resolved_at=now,
            keep_resolved_by=item.resolved_by_name,
        )
        .execution_options(synchronize_session=False)
    )
    if refunded:
        await _schedule(session, offer, EMAIL_PATIENT_CHECK, now)
    else:
        await skip_kinds(session, offer.id, None, now)
    logger.info("deposit_release_offer_refund_recorded", offer_id=str(offer.id), refunded=refunded)
    return offer.id


async def mark_refunded(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    offer_id: UUID,
    note: str,
    resolved_by_name: str,
    now: datetime | None = None,
) -> str:
    """The clinic says it refunded in the Asaas panel (TASK-054 list). Idempotent.

    `marked` / `already` (marked or answered before: nothing re-sent) / `not_due` / `keep_pending`
    (decide "Manter" first) / `deposit_reused` (a "Remarcar" reused the deposit: never refund it)
    / `not_found` (no such offer in THIS clinic). Goes through
    `attention_view.resolve_item`, so the deposit is recorded as refunded exactly as in the
    payments screen. Commits.
    """
    from secretaria.services.payments import attention_view  # lazy: it calls back into here

    now = _now(now)
    offer = await session.scalar(
        select(DepositReleaseOffer).where(
            DepositReleaseOffer.id == offer_id, DepositReleaseOffer.tenant_id == tenant_id
        )
    )
    if offer is None:
        return "not_found"
    if offer.deposit_reused_at is not None:
        return "deposit_reused"  # owner 2026-10-12: the money is in use again
    if offer.decision == DECISION_RESCHEDULING and carry_check_is_fresh(offer, now):
        return "deposit_reused"  # a booking is about to carry it (the provider just said "paid")
    if offer.refund_status in (REFUND_MARKED, REFUND_RECEIVED, REFUND_NOT_RECEIVED):
        return "already"
    if offer.decision == DECISION_KEEP_REQUESTED:
        return "keep_pending"
    if offer.refund_status != REFUND_DUE:
        return "not_due"
    item_id = await session.scalar(
        select(PaymentAttentionItem.id)
        .where(
            PaymentAttentionItem.tenant_id == tenant_id,
            PaymentAttentionItem.deposit_id == offer.deposit_id,
            PaymentAttentionItem.reason == REASON_RELEASED_REFUND_DUE,
            PaymentAttentionItem.status == ATTENTION_OPEN,
        )
        .limit(1)
    )
    if item_id is not None:
        await attention_view.resolve_item(
            session,
            tenant_id,
            item_id,
            action=REFUNDED_ACTION,
            note=note,
            resolved_by_name=resolved_by_name,
            now=now,
        )
    current = await _reload(session, offer_id)
    if current.refund_status == REFUND_DUE:
        # No open item (resolved elsewhere a moment ago, or never opened): record it here.
        moved = await session.execute(
            update(DepositReleaseOffer)
            .where(
                DepositReleaseOffer.id == offer_id, DepositReleaseOffer.refund_status == REFUND_DUE
            )
            .values(refund_status=REFUND_MARKED, refund_marked_at=now)
            .execution_options(synchronize_session=False)
        )
        if moved.rowcount == 1:
            await _schedule(session, current, EMAIL_PATIENT_CHECK, now)
        await session.commit()
        current = await _reload(session, offer_id)
    if current.refund_status == REFUND_MARKED:
        return "marked"
    if current.refund_status in (REFUND_RECEIVED, REFUND_NOT_RECEIVED):
        return "already"
    return "not_due"
