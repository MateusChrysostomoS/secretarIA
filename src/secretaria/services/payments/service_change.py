"""A service change on an appointment whose sinal is PAID: the money side (Task 5).

Spec: docs/superpowers/specs/2026-10-26-troca-servico-diferenca-sinal-design.md. The pure rule is
`service_change_policy.decide_service_change`; this module orchestrates it. Three entry points
(none commits - the caller owns the transaction, like every `services/` module):

* `request_service_change` - called by the patient's edit flow and the clinic's agenda edit
  INSTEAD of writing a service change themselves when the sinal is paid:
    - dearer by >= R$ 10: creates a `DepositAdjustment` (`awaiting_payment`) and a Pix of ONLY
      the difference (`external_reference` = the adjustment id). The edit is NOT written: it is
      kept in `pending_edit` and applied by the webhook when the Pix is paid
      (`event_processor._on_adjustment_paid` -> `apply_paid_adjustment`);
    - dearer by < R$ 10, same price, cheaper: the edit is written NOW (the same writer as every
      edit, `write_appointment_edit`, plus the doctor's "Consulta alterada" outbox row);
      cheaper by >= R$ 10 far enough ahead also records an `asked` adjustment (the caller asks
      the patient "quer a diferença de volta?"); cheaper otherwise records `clinic_kept`;
    - a second change while an adjustment is open (unpaid Pix or unanswered question) is
      `blocked` - "aguarde o pagamento anterior".
* `answer_refund_question` - "Sim, quero de volta" opens ONE attention item
  (`service_diff_refund_due`, suggested refund = the difference: the clinic returns it in the
  provider's panel, never us); "Não, abater" records `kept_credit`.
* `expire_adjustments` - the sweep (`jobs.expire_due_adjustments`, every payment-expiry tick):
  a question unanswered for 24 h is a "no" (`kept_credit`, no item); an unpaid difference past
  `expires_at` is checked at the provider FIRST (a paid one is reconciled through the webhook
  path, never expired), voided, marked `expired`; the service never changed. It also retries
  the void of a difference cancelled while the provider was down (`cancelled_void_pending`).

Provider down (follow-up B, 2026-10-27): when the Pix of the difference cannot be created
because the provider is UNAVAILABLE (a retryable `ProviderError` - network, 429, 5xx - or a
network timeout), the change stands NOW like `increase_free` (decision `increase_collect`,
adjustment `collect_due`) and the clinic gets ONE `service_diff_collect_due` item - the patient
never redoes anything and that uncollected difference is never money held. A permanent refusal
(401/403 key, 400 validation such as a missing CPF, a charge without its code, anything
unknown) still blocks (`charge_failed`): a difference is never waived silently. While that
item is open, every further service change of the consultation is blocked (`collect_pending`);
cancelling the consultation closes it (`adjustment_queries.settle_open_adjustments`).

`PixDeposit.amount_cents` never changes: what the clinic holds is `held_deposit_cents`.
Patient notices after a commit run through `ADJUSTMENT_APPLIED_HOOKS` / `ADJUSTMENT_EXPIRED_HOOKS`
(the `jobs.EXPIRY_HOOKS` pattern; the default ones below send one text through `notify`).
The agenda (Task 6): `sync_agenda_event` is the ONE helper that puts the event in line with the
row after a service change (the patient's edit at Confirmar, the paid difference through the
`update_agenda_after_payment` hook, the clinic's agenda edit); a window that no longer fits when
the Pix is paid opens a `service_diff_agenda_pending` item instead of overlapping events.
LGPD: logs carry ids, a status and an error type only - never a name, phone, document or code.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import (
    LIVE_APPOINTMENT_STATUSES,
    Appointment,
    Patient,
    PixDeposit,
    Tenant,
    is_live_status,
)
from secretaria.models.deposit_adjustment import (
    KIND_DECREASE,
    KIND_INCREASE,
    STATUS_APPLIED,
    STATUS_ASKED,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_CANCELLED_VOID_PENDING,
    STATUS_CLINIC_KEPT,
    STATUS_COLLECT_DUE,
    STATUS_EXPIRED,
    STATUS_KEPT_CREDIT,
    STATUS_REFUND_REQUESTED,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import (
    REASON_SERVICE_DIFF_AGENDA_PENDING,
    REASON_SERVICE_DIFF_COLLECT_DUE,
    REASON_SERVICE_DIFF_REFUND_DUE,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import booking_hold, tenant_config as cfg
from secretaria.services.appointment_calendar_origin import (
    CalendarOriginUnresolved,
    calendar_professional_id,
)
from secretaria.services.appointment_edit_write import write_appointment_edit
from secretaria.services.appointment_status import SOURCE_HUB, SOURCE_SYSTEM
from secretaria.services.calendar import CalendarSlotTakenError, build_event_description
from secretaria.services.local_calendar import update_event_details_exclusive
from secretaria.services.patient_context import as_utc
from secretaria.services.payments import (
    accounts,
    adjustment_queries,
    attention,
    notify,
    registry,
)
from secretaria.services.payments.deposit_lifecycle import (
    _resolve_service_and_price,
    _tenant_tz,
    register_reschedule,
)
from secretaria.services.payments.money import format_brl, parse_brl_to_cents
from secretaria.services.payments.pix_message import pix_code_tail
from secretaria.services.payments.provider import ChargeRequest, PaymentStatus, ProviderError
from secretaria.services.payments.service_change_policy import ChangeDecision, decide_service_change
from secretaria.services.professional_edit_outbox import record_professional_edit

logger = get_logger(__name__)

# 24 h without an answer to "quer a diferença de volta?" = "não" (owner's decision).
QUESTION_TTL = timedelta(hours=24)
DEFAULT_MIN_DAYS = 2
DEFAULT_PIX_TTL_MINUTES = 30
# `ServiceChangeResult.decision` when the Pix of the difference could not be created (follow-up
# B): applied now, the clinic collects the difference.
DECISION_COLLECT = "increase_collect"

# The fixed buttons of the refund question. Each tap carries its own id,
# `<action>|<adjustment id>` (schemas/webhook.py `_ACTION_BUTTON_PREFIXES`), so it is routed as an
# ACTION on both channels whatever the conversation is doing - never through the LLM
# (workers/shared/service_change_actions.py -> `answer_refund_question`). Labels <= 20 chars
# (WhatsApp reply-button title).
ACTION_REFUND_YES = "svcdiffyes"
ACTION_REFUND_NO = "svcdiffno"
REFUND_ACTIONS = frozenset({ACTION_REFUND_YES, ACTION_REFUND_NO})
BUTTON_REFUND_YES = ACTION_REFUND_YES
BUTTON_REFUND_NO = ACTION_REFUND_NO
LABEL_REFUND_YES = "Sim, quero de volta"
LABEL_REFUND_NO = "Não, abater no valor"
REFUND_QUESTION_BUTTONS: tuple[tuple[str, str], ...] = (
    (BUTTON_REFUND_YES, LABEL_REFUND_YES),
    (BUTTON_REFUND_NO, LABEL_REFUND_NO),
)


def refund_question_buttons(adjustment_id: UUID) -> list[tuple[str, str]]:
    """The (id, label) pairs of the refund question for ONE adjustment."""
    return [
        (f"{ACTION_REFUND_YES}|{adjustment_id}", LABEL_REFUND_YES),
        (f"{ACTION_REFUND_NO}|{adjustment_id}", LABEL_REFUND_NO),
    ]

ResultKind = Literal["applied_now", "awaiting_payment", "asked", "blocked"]


@dataclass(frozen=True)
class ServiceChangeResult:
    """What a service change did. `kind`:

    * `applied_now`      - the edit was written in the caller's transaction (no Pix, no question;
                           with `decision` `increase_collect` the Pix could not be created and
                           the clinic collects `diff_cents` - tell the patient);
    * `awaiting_payment` - a difference Pix exists (`pix_copy_paste`, `expires_at`); NOTHING was
                           written on the appointment;
    * `asked`            - the edit was written and the patient must be asked about the refund
                           (`adjustment_id` goes to `answer_refund_question`);
    * `blocked`          - nothing was written; `reason` is machine-readable:
                           `price_unavailable`, `deposit_not_paid`, `adjustment_pending`,
                           `refund_question_pending`, `collect_pending` (the clinic still has
                           to collect the previous difference), `no_payment_account`,
                           `charge_failed` (a permanent refusal at the provider).
    """

    kind: ResultKind
    diff_cents: int = 0
    pix_copy_paste: str | None = None
    adjustment_id: UUID | None = None
    expires_at: datetime | None = None
    reason: str | None = None
    # The policy action behind it (`service_change_policy.Action`), for messages and tests.
    decision: str | None = None
    # When the edit was written: what changed (R6 names, e.g. "serviço") and the doctor notice.
    changed_fields: tuple[str, ...] = ()
    notice_id: UUID | None = None


@dataclass(frozen=True)
class AdjustmentNotice:
    """What a post-commit hook may know about an adjustment: ids, labels, an amount."""

    adjustment_id: UUID
    tenant_id: UUID
    appointment_id: UUID | None
    patient_id: UUID | None
    kind: str
    old_service: str
    new_service: str
    diff_cents: int
    # Only on a not-applied notice of an UNDERPAID difference: what actually arrived.
    paid_cents: int | None = None


AdjustmentHook = Callable[[AsyncSession, Tenant, AdjustmentNotice], Awaitable[None]]

# Run AFTER the commit that applied a paid difference (event_processor.run_post_commit) and after
# the sweep expired an unpaid one. Best-effort and isolated: a failing hook is logged.
ADJUSTMENT_APPLIED_HOOKS: list[AdjustmentHook] = []
ADJUSTMENT_EXPIRED_HOOKS: list[AdjustmentHook] = []
# Run AFTER the commit of a paid difference that could NOT be applied (the consultation was
# cancelled, or moved and the new service no longer fits; or it was UNDERPAID - `paid_cents`
# set, follow-up C): the clinic has an item; the patient gets one neutral notice (final-review I2).
ADJUSTMENT_NOT_APPLIED_HOOKS: list[AdjustmentHook] = []


def register_applied_hook(hook: AdjustmentHook) -> None:
    ADJUSTMENT_APPLIED_HOOKS.append(hook)


def register_expired_hook(hook: AdjustmentHook) -> None:
    ADJUSTMENT_EXPIRED_HOOKS.append(hook)


def register_not_applied_hook(hook: AdjustmentHook) -> None:
    ADJUSTMENT_NOT_APPLIED_HOOKS.append(hook)


# --------------------------------------------------------------------------
# pt-BR copy (the callers of Tasks 6/7 send it; never promises an automatic refund)
# --------------------------------------------------------------------------


def difference_pix_text(
    new_service: str,
    diff_cents: int,
    copy_paste: str | None,
    ttl_minutes: int,
    *,
    source: str | None = None,
) -> str:
    """`source` = who asked for the change: `SOURCE_HUB` (the clinic's agenda edit) says so;
    anything else keeps the patient's own wording."""
    value = format_brl(diff_cents)
    if source == SOURCE_HUB:
        head = (
            f"A clínica quer trocar o serviço da sua consulta para {new_service}. "
            f"Para isso, falta pagar a diferença do sinal: {value} via Pix."
        )
    else:
        head = (
            f"Para trocar sua consulta para {new_service}, falta pagar a diferença do sinal: "
            f"{value} via Pix."
        )
    lines = [head]
    lines.append("Sua consulta só muda depois que o pagamento for confirmado.")
    lines.append(
        f"O código vale por {ttl_minutes} minutos. Se não for pago, a consulta continua como está."
    )
    # ONE message: the code is its last lines (fenced - see `pix_message`), never a bubble apart.
    lines.extend(pix_code_tail(copy_paste))
    return "\n".join(lines)


def refund_question_text(new_service: str, diff_cents: int) -> str:
    return (
        f"Pronto, sua consulta agora é {new_service}. Com a troca, o sinal pago ficou "
        f"{format_brl(diff_cents)} acima do necessário.\n"
        "Quer essa diferença de volta? Se sim, a clínica entra em contato para combinar a "
        "devolução. Se preferir, o valor fica abatido no restante da consulta."
    )


def adjustment_applied_text(new_service: str, diff_cents: int) -> str:
    return (
        f"Recebemos o Pix da diferença ({format_brl(diff_cents)}). "
        f"Sua consulta agora é {new_service}."
    )


_KEPT_AS_IT_WAS = "Sua consulta continua como estava."
_BLOCKED_TEXT = {
    "charge_failed": (
        "Não foi possível gerar o Pix da diferença agora. "
        f"{_KEPT_AS_IT_WAS} Tente de novo em instantes ou fale com a clínica."
    ),
    "adjustment_pending": (
        "Já existe um Pix da diferença aguardando pagamento para essa consulta. "
        f"{_KEPT_AS_IT_WAS} Aguarde esse pagamento (ou o prazo dele acabar) para pedir outra troca."
    ),
    "refund_question_pending": (
        "Antes de outra troca, responda a mensagem anterior sobre a diferença do sinal. "
        f"{_KEPT_AS_IT_WAS}"
    ),
    "collect_pending": (
        "A diferença do sinal da troca anterior ainda vai ser acertada com a clínica. "
        f"{_KEPT_AS_IT_WAS} Para trocar de novo, fale com a clínica."
    ),
}
_BLOCKED_DEFAULT = (
    f"Não foi possível calcular o sinal do novo serviço. {_KEPT_AS_IT_WAS} "
    "Para trocar, fale com a clínica."
)


def blocked_text(reason: str | None) -> str:
    """What the patient reads when `request_service_change` was `blocked` (nothing changed)."""
    return _BLOCKED_TEXT.get(reason or "", _BLOCKED_DEFAULT)


def refund_answer_text(outcome: str, diff_cents: int) -> str:
    """The reply to a tap on the refund question (`answer_refund_question`'s outcome). The
    clinic returns the money in the provider's panel - never promised as automatic."""
    value = format_brl(diff_cents)
    if outcome == STATUS_REFUND_REQUESTED:
        return (
            f"Certo! Já avisamos: a clínica vai entrar em contato para combinar a "
            f"devolução da diferença de {value}."
        )
    if outcome == STATUS_KEPT_CREDIT:
        return f"Combinado! A diferença de {value} fica abatida no valor da consulta."
    if outcome == "already_answered":
        return "Essa pergunta já foi respondida (ou o prazo para responder acabou)."
    return "Não encontrei essa pergunta sobre a diferença do sinal."


def adjustment_not_applied_text(new_service: str, diff_cents: int) -> str:
    """Neutral: the money arrived, the change did not happen, the clinic gets in touch. Never
    promises a refund (the clinic decides, through its attention item)."""
    return (
        f"Recebemos o Pix da diferença ({format_brl(diff_cents)}), mas não foi possível "
        f"concluir a troca da sua consulta para {new_service}, porque ela foi alterada antes "
        "da confirmação do pagamento. A clínica vai entrar em contato com você."
    )


def adjustment_underpaid_text(new_service: str, diff_cents: int, paid_cents: int) -> str:
    """Follow-up C: the Pix of the difference arrived SHORT. The exact numbers, the change not
    made, the clinic gets in touch. No new Pix for the rest and no refund promised."""
    missing = max(diff_cents - paid_cents, 0)
    return (
        f"Recebemos {format_brl(paid_cents)} no Pix da diferença, mas o valor necessário é "
        f"{format_brl(diff_cents)}. Faltam {format_brl(missing)}. Por isso, a troca da sua "
        f"consulta para {new_service} não foi feita. A clínica vai entrar em contato com você."
    )


def collect_due_text(new_service: str, diff_cents: int, *, source: str | None = None) -> str:
    """Follow-up B: the change stood without a Pix (the provider did not answer). Plain and
    neutral: the difference is settled with the clinic; nothing to pay here, nothing refunded."""
    value = format_brl(diff_cents)
    if source == SOURCE_HUB:
        return (
            f"A troca do serviço da sua consulta para {new_service} já está feita. "
            f"A diferença do sinal ({value}) será acertada diretamente com a clínica."
        )
    return (
        f"Não conseguimos gerar agora o Pix da diferença do sinal ({value}), mas sua troca para "
        f"{new_service} já está feita. Essa diferença será acertada diretamente com a clínica."
    )


def adjustment_expired_text(old_service: str, new_service: str) -> str:
    return (
        f"O Pix da diferença para trocar sua consulta para {new_service} venceu sem pagamento. "
        f"Sua consulta continua como {old_service}, no mesmo horário. "
        "Se ainda quiser trocar, é só pedir de novo."
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return as_utc(value).astimezone(UTC).isoformat()


def _uuid_or_none(value) -> UUID | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def _str_or_none(value) -> str | None:
    return None if value is None else str(value)


_PLAIN_KEYS = (
    "appointment_type",
    "insurance",
    "attendee_name",
    "google_event_id",
    "google_event_link",
    "old_google_event_id",
    "old_google_calendar_source",
)
_FLAG_KEYS = ("time_changed", "doctor_changed", "calendar_changed")


def serialize_edit(edit: dict, appointment: Appointment, *, source: str) -> dict:
    """The R6 edit dict as JSON (`pending_edit`), plus the row it was made against
    (`expected`): a paid difference is applied only onto that same row. Drops what is not
    data (the calendar client) and the patient's name (never stored here)."""
    out: dict = {key: edit.get(key) for key in _PLAIN_KEYS}
    for key in _FLAG_KEYS:
        out[key] = bool(edit.get(key, False))
    if "calendar_changed" not in edit:
        out["calendar_changed"] = out["doctor_changed"]
    out["professional_id"] = _str_or_none(edit.get("professional_id"))
    out["old_professional_id"] = _str_or_none(edit.get("old_professional_id"))
    out["start_at"] = _iso(edit.get("start_at") or appointment.start_at)
    out["end_at"] = _iso(edit.get("end_at") or appointment.end_at)
    out["source"] = source
    out["expected"] = {
        "start_at": _iso(appointment.start_at),
        # The event's window before the change: the agenda hook re-checks a window that grew.
        "end_at": _iso(appointment.end_at),
        "appointment_type": appointment.appointment_type,
        "google_event_id": appointment.google_event_id,
        "professional_id": _str_or_none(appointment.professional_id),
    }
    return out


def deserialize_edit(data: dict) -> dict:
    """`pending_edit` back to the dict `write_appointment_edit` takes."""
    edit = {key: data.get(key) for key in _PLAIN_KEYS}
    for key in _FLAG_KEYS:
        edit[key] = bool(data.get(key, False))
    edit["professional_id"] = _uuid_or_none(data.get("professional_id"))
    edit["old_professional_id"] = _uuid_or_none(data.get("old_professional_id"))
    edit["start_at"] = datetime.fromisoformat(data["start_at"])
    edit["end_at"] = datetime.fromisoformat(data["end_at"])
    return edit


def _same_service_and_doctor(appointment: Appointment, expected: dict) -> bool:
    """The difference was priced for THIS service with THIS doctor: another service or doctor
    on the row since the request makes the paid change meaningless (never applied)."""
    return appointment.appointment_type == expected.get("appointment_type") and _str_or_none(
        appointment.professional_id
    ) == expected.get("professional_id")


def _paid_window(
    appointment: Appointment, data: dict, expected: dict
) -> tuple[datetime, datetime, bool] | None:
    """(start, end, time_changed) the paid change lands on (final-review I2).

    The row did not move since the request: the change's own window, including its own move
    (`time_changed`). The row was moved meanwhile (a reschedule is never blocked by a pending
    Pix): that later move wins, and the new service lands on the CURRENT start with the new
    service's length - `time_changed` False, the start is not touched."""
    try:
        own_start = datetime.fromisoformat(data["start_at"])
        own_end = datetime.fromisoformat(data["end_at"])
    except (KeyError, TypeError, ValueError):
        return None
    length = own_end - own_start
    if length <= timedelta(0):
        return None
    if _iso(appointment.start_at) == expected.get("start_at"):
        return as_utc(own_start), as_utc(own_end), bool(data.get("time_changed"))
    start = as_utc(appointment.start_at)
    return start, start + length, False


async def _window_fits(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    start: datetime,
    end: datetime,
) -> bool:
    """Does [start, end) still fit for this row? A window inside the row's own current one
    always fits (max(old, new) length is what is checked). Otherwise no other live booking of
    the same agenda (doctor, or the clinic's when there is none) and no live hold may overlap.
    Database only: the Google agenda is re-checked by `update_agenda_after_payment` after the
    commit (no network call while the webhook holds the row lock)."""
    current_start = as_utc(appointment.start_at)
    current_end = as_utc(appointment.end_at or appointment.start_at)
    if start >= current_start and end <= current_end:
        return True
    same_agenda = (
        Appointment.professional_id == appointment.professional_id
        if appointment.professional_id is not None
        else Appointment.professional_id.is_(None)
    )
    clash = await session.scalar(
        select(Appointment.id)
        .where(
            Appointment.tenant_id == tenant.id,
            Appointment.id != appointment.id,
            Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
            same_agenda,
            Appointment.start_at < end,
            Appointment.end_at > start,
        )
        .limit(1)
    )
    if clash is not None:
        return False
    return not await booking_hold.live_hold_overlaps(
        session, tenant.id, start, end, professional_id=appointment.professional_id
    )


def _notice(adjustment: DepositAdjustment, patient_id: UUID | None) -> AdjustmentNotice:
    return AdjustmentNotice(
        adjustment_id=adjustment.id,
        tenant_id=adjustment.tenant_id,
        appointment_id=adjustment.appointment_id,
        patient_id=patient_id,
        kind=adjustment.kind,
        old_service=adjustment.old_service,
        new_service=adjustment.new_service,
        diff_cents=adjustment.diff_cents,
    )


async def held_deposit_cents(session: AsyncSession, deposit: PixDeposit) -> int:
    """What the clinic holds for this sinal: the original amount, plus every PAID difference,
    minus every difference the patient asked back. (A difference kept as credit stays held.)
    Task 8 uses it for the refund of a cancellation."""
    paid_increases = await session.scalar(
        select(func.coalesce(func.sum(DepositAdjustment.diff_cents), 0)).where(
            DepositAdjustment.tenant_id == deposit.tenant_id,
            DepositAdjustment.deposit_id == deposit.id,
            DepositAdjustment.kind == KIND_INCREASE,
            DepositAdjustment.status == STATUS_APPLIED,
            DepositAdjustment.provider_charge_id.is_not(None),
        )
    )
    refunds = await session.scalar(
        select(func.coalesce(func.sum(DepositAdjustment.diff_cents), 0)).where(
            DepositAdjustment.tenant_id == deposit.tenant_id,
            DepositAdjustment.deposit_id == deposit.id,
            DepositAdjustment.kind == KIND_DECREASE,
            DepositAdjustment.status == STATUS_REFUND_REQUESTED,
        )
    )
    return int(deposit.amount_cents) + int(paid_increases or 0) - int(refunds or 0)


async def open_adjustment(
    session: AsyncSession, *, tenant_id: UUID, deposit_id: UUID
) -> DepositAdjustment | None:
    """The adjustment still waiting for something (an unpaid difference or an unanswered
    question), or None. Tasks 6/7/8 read it (menu state, cancellation)."""
    return await session.scalar(
        select(DepositAdjustment)
        .where(
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.deposit_id == deposit_id,
            DepositAdjustment.status.in_((STATUS_AWAITING_PAYMENT, STATUS_ASKED)),
        )
        .order_by(DepositAdjustment.created_at.desc())
        .limit(1)
    )


async def find_adjustment(
    session: AsyncSession, *, tenant_id: UUID, charge_id: str, provider: str | None = None
) -> DepositAdjustment | None:
    """The tenant's adjustment for a provider charge id (the webhook's lookup). Scoped by the
    tenant on the adjustment AND on its deposit, and by the deposit's provider when given."""
    query = (
        select(DepositAdjustment)
        .join(PixDeposit, PixDeposit.id == DepositAdjustment.deposit_id)
        .where(
            DepositAdjustment.tenant_id == tenant_id,
            PixDeposit.tenant_id == tenant_id,
            DepositAdjustment.provider_charge_id == charge_id,
        )
    )
    if provider is not None:
        query = query.where(PixDeposit.provider == provider)
    return await session.scalar(query)


async def move_adjustment(
    session: AsyncSession,
    adjustment: DepositAdjustment,
    *,
    from_status: str,
    to_status: str,
    now: datetime,
) -> bool:
    """Atomic `from_status -> to_status` (`UPDATE ... WHERE status = ... RETURNING`): exactly
    one caller wins, so a redelivered webhook or two workers never apply a change twice."""
    claimed = await session.execute(
        update(DepositAdjustment)
        .where(
            DepositAdjustment.id == adjustment.id,
            DepositAdjustment.tenant_id == adjustment.tenant_id,
            DepositAdjustment.status == from_status,
        )
        .values(status=to_status, resolved_at=now)
        .returning(DepositAdjustment.id)
        .execution_options(synchronize_session=False)
    )
    won = claimed.scalar_one_or_none() is not None
    await session.refresh(adjustment)
    return won


async def _price_cents(
    session: AsyncSession, tenant: Tenant, appointment: Appointment, service: str, professional_id
) -> int | None:
    """The new service's price, resolved exactly like the sinal's (owner's own catalog first)."""
    probe = SimpleNamespace(
        appointment_type=service,
        professional_id=_uuid_or_none(professional_id) if professional_id is not None
        else appointment.professional_id,
    )
    name, price = await _resolve_service_and_price(session, tenant, probe)
    if name is None:
        return None
    return parse_brl_to_cents(price)


@dataclass(frozen=True)
class _Written:
    fields: tuple[str, ...]
    notice_id: UUID | None


async def _write(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    edit: dict,
    *,
    source: str,
    key: str,
) -> _Written:
    fields = await write_appointment_edit(
        session,
        appointment,
        tenant_id=tenant.id,
        timezone=tenant.timezone,
        edit=edit,
        source=source,
        idempotency_key=key,
    )
    notice = await record_professional_edit(session, appointment, fields)
    return _Written(tuple(fields), notice.id if notice is not None else None)


# --------------------------------------------------------------------------
# request_service_change
# --------------------------------------------------------------------------


async def preview_service_change(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    new_service: str,
    professional_id,
    start_at: datetime | None,
    now: datetime,
) -> ChangeDecision:
    """The policy decision for a new service on this PAID sinal - what
    `request_service_change` will do, computed the same way (price resolved like the sinal's,
    what the clinic holds, the tenant's minimum days). Read-only: writes, charges and locks
    nothing, so the confirmation card can show the difference before Confirmar."""
    price = await _price_cents(session, tenant, appointment, new_service, professional_id)
    held = await held_deposit_cents(session, deposit)
    days_until = (
        (as_utc(start_at) - as_utc(now)).total_seconds() / 86400 if start_at is not None else 0.0
    )
    min_days = tenant.pix_difference_refund_min_days
    return decide_service_change(
        paid_deposit_cents=held,
        percent_applied=deposit.percent_applied or 0,
        new_price_cents=price,
        days_until=days_until,
        min_days=DEFAULT_MIN_DAYS if min_days is None else min_days,
    )


def confirm_card_line_text(action: str, diff_cents: int) -> str | None:
    """The line the confirmation card adds for a service change on a paid sinal (None when
    there is nothing to say: no difference, or blocked - Confirmar then explains). Never
    promises money back."""
    value = format_brl(diff_cents)
    if action == "increase_charge":
        return (
            f"Diferença do sinal: {value}. Ao confirmar, enviamos o Pix dessa diferença; "
            "o serviço só muda depois do pagamento."
        )
    if action == "increase_free":
        return f"Diferença do sinal: {value}, acertada na consulta."
    if action == "decrease_ask":
        return (
            f"O sinal pago fica {value} acima do novo valor. Ao confirmar, perguntamos se você "
            "quer essa diferença de volta."
        )
    if action == "decrease_keep":
        return (
            f"O sinal pago fica {value} acima do novo valor; essa diferença fica abatida "
            "na consulta."
        )
    return None


async def request_service_change(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    new_service: str,
    pending_edit: dict,
    now: datetime,
    source: str = SOURCE_SYSTEM,
) -> ServiceChangeResult:
    """Decide and do the money side of a service change on a PAID sinal. Never commits.

    `pending_edit` is the R6 edit dict the caller would have written (`appointment_type`,
    `professional_id`, `insurance`, `attendee_name`, `google_event_id`, `start_at`, `end_at`,
    `time_changed`, `doctor_changed`, ...). The caller keeps its own guards (live row, the
    reschedule limit when the time moves, the calendar) and must NOT write the edit itself:
    this function writes it when the policy says the change stands now. `source` is the
    status-log source of that write (`SOURCE_FLOW` / `SOURCE_HUB`).
    """
    if (
        deposit.tenant_id != tenant.id
        or appointment.tenant_id != tenant.id
        or deposit.appointment_id != appointment.id
    ):
        raise ValueError("deposit, appointment and tenant do not match")

    # Serialize every change of this sinal: two concurrent requests cannot both see "nothing
    # pending" and create two charges.
    locked = await session.scalar(
        select(PixDeposit)
        .where(PixDeposit.id == deposit.id, PixDeposit.tenant_id == tenant.id)
        .with_for_update()
    )
    if locked is None or locked.status != PixDepositStatus.PAID:
        return ServiceChangeResult(kind="blocked", reason="deposit_not_paid")
    pending = await open_adjustment(session, tenant_id=tenant.id, deposit_id=locked.id)
    if pending is not None:
        reason = (
            "adjustment_pending"
            if pending.status == STATUS_AWAITING_PAYMENT
            else "refund_question_pending"
        )
        return ServiceChangeResult(kind="blocked", reason=reason)
    # Follow-up review I3: a previous difference the clinic is still collecting (provider was
    # down) blocks ANY further change - also back to the old service - until the clinic
    # resolves that item: never a second charge, never a stale "cobrar" for a moot difference.
    if await adjustment_queries.open_collect_adjustments(
        session, tenant_id=tenant.id, deposit=locked
    ):
        return ServiceChangeResult(kind="blocked", reason="collect_pending")

    decision = await preview_service_change(
        session,
        tenant=tenant,
        appointment=appointment,
        deposit=locked,
        new_service=new_service,
        professional_id=pending_edit.get("professional_id", appointment.professional_id),
        start_at=pending_edit.get("start_at") or appointment.start_at,
        now=now,
    )
    log = {"tenant_id": str(tenant.id), "appointment_id": str(appointment.id)}
    if decision.action == "blocked":
        logger.info("service_change_blocked", reason=decision.reason, **log)
        return ServiceChangeResult(kind="blocked", reason=decision.reason, decision="blocked")

    old_service = appointment.appointment_type or ""
    if decision.action == "increase_charge":
        return await _charge_difference(
            session, tenant=tenant, appointment=appointment, deposit=locked,
            old_service=old_service, new_service=new_service, diff_cents=decision.diff_cents,
            pending_edit=pending_edit, now=now, source=source,
        )

    # Every other action: the change stands now, written by the one edit writer.
    adjustment = None
    if decision.action in ("decrease_ask", "decrease_keep"):
        asks = decision.action == "decrease_ask"
        adjustment = DepositAdjustment(
            id=uuid4(),
            tenant_id=tenant.id,
            deposit_id=locked.id,
            appointment_id=appointment.id,
            kind=KIND_DECREASE,
            status=STATUS_ASKED if asks else STATUS_CLINIC_KEPT,
            old_service=old_service[:120],
            new_service=new_service[:120],
            diff_cents=decision.diff_cents,
            asked_at=now if asks else None,
            resolved_at=None if asks else now,
        )
        session.add(adjustment)
        await session.flush()
    edit = deserialize_edit(serialize_edit(pending_edit, appointment, source=source))
    edit["appointment_type"] = new_service
    written = await _write(
        session, tenant, appointment, edit, source=source,
        key=f"service_change:{appointment.id}:{adjustment.id if adjustment else 'now'}",
    )
    kind: ResultKind = "asked" if decision.action == "decrease_ask" else "applied_now"
    logger.info("service_change_applied", decision=decision.action, **log)
    return ServiceChangeResult(
        kind=kind,
        diff_cents=decision.diff_cents,
        adjustment_id=adjustment.id if adjustment is not None else None,
        decision=decision.action,
        changed_fields=written.fields,
        notice_id=written.notice_id,
    )


async def _charge_difference(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    old_service: str,
    new_service: str,
    diff_cents: int,
    pending_edit: dict,
    now: datetime,
    source: str,
) -> ServiceChangeResult:
    log = {"tenant_id": str(tenant.id), "appointment_id": str(appointment.id)}
    provider_name = deposit.provider
    provider = registry.find_provider(provider_name)
    account = await accounts.load_charge_account(
        session, tenant_id=tenant.id, provider=provider_name, timing="after"
    )
    patient = await session.get(Patient, deposit.patient_id) if deposit.patient_id else None
    if provider is None or account is None or patient is None or patient.tenant_id != tenant.id:
        logger.warning("service_change_no_payment_account", **log)
        return ServiceChangeResult(
            kind="blocked", reason="no_payment_account", decision="increase_charge"
        )

    adjustment_id = uuid4()
    ttl = tenant.deposit_hold_ttl_minutes or DEFAULT_PIX_TTL_MINUTES
    expires_at = as_utc(now) + timedelta(minutes=ttl)
    customer_id = await accounts.get_customer_id(
        session, tenant_id=tenant.id, patient_id=patient.id, provider=provider_name
    )
    description = f"Diferença do sinal - {new_service} - {tenant.clinic_name}"
    try:
        created = await provider.create_pix_charge(
            account,
            ChargeRequest(
                amount_cents=diff_cents,
                external_reference=str(adjustment_id),
                description=description[:255],
                due_date=as_utc(now).astimezone(_tenant_tz(tenant)).date(),
                payer_name=patient.name or "Paciente",
                payer_phone=patient.wa_id,
                provider_customer_id=customer_id,
                expires_at=expires_at,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - nothing charged
        outage = provider_unavailable(exc)
        logger.warning(
            "service_change_charge_failed",
            code=exc.code if isinstance(exc, ProviderError) else type(exc).__name__,
            outage=outage,
            **log,
        )
        if not outage:
            # A refusal that will happen again (key, validation, unknown): never waived.
            return ServiceChangeResult(
                kind="blocked", reason="charge_failed", decision="increase_charge"
            )
        # The provider is down: the clinic collects (follow-up B). The same id is kept - a
        # timeout may come after the charge was created with it as the reference.
        return await _apply_and_collect(
            session, tenant=tenant, appointment=appointment, deposit=deposit,
            old_service=old_service, new_service=new_service, diff_cents=diff_cents,
            pending_edit=pending_edit, now=now, source=source, adjustment_id=adjustment_id,
        )
    if not created.copy_paste:
        # The patient must SEE what they pay: a charge without its code is voided, not kept.
        try:
            await provider.cancel_charge(account, created.provider_charge_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("service_change_void_failed", error_type=type(exc).__name__, **log)
        logger.warning("service_change_charge_without_code", **log)
        return ServiceChangeResult(
            kind="blocked", reason="charge_failed", decision="increase_charge"
        )

    if created.provider_customer_id and created.provider_customer_id != customer_id:
        await accounts.remember_customer_id(
            session,
            tenant_id=tenant.id,
            patient_id=patient.id,
            provider=provider_name,
            provider_customer_id=created.provider_customer_id,
        )
    adjustment = DepositAdjustment(
        id=adjustment_id,
        tenant_id=tenant.id,
        deposit_id=deposit.id,
        appointment_id=appointment.id,
        kind=KIND_INCREASE,
        status=STATUS_AWAITING_PAYMENT,
        old_service=old_service[:120],
        new_service=new_service[:120],
        pending_edit={
            **serialize_edit(pending_edit, appointment, source=source),
            "appointment_type": new_service,
        },
        diff_cents=diff_cents,
        provider_charge_id=created.provider_charge_id,
        expires_at=expires_at,
    )
    session.add(adjustment)
    await session.flush()
    logger.info("service_change_awaiting_payment", adjustment_id=str(adjustment_id), **log)
    return ServiceChangeResult(
        kind="awaiting_payment",
        diff_cents=diff_cents,
        pix_copy_paste=created.copy_paste,
        adjustment_id=adjustment_id,
        expires_at=expires_at,
        decision="increase_charge",
    )


def provider_unavailable(exc: BaseException) -> bool:
    """An OUTAGE (a retry may work): a retryable `ProviderError` (network, 429, 5xx) or a
    transport error / timeout that escaped the provider. Anything else is a refusal."""
    if isinstance(exc, ProviderError):
        return bool(exc.retryable)
    return isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError))


async def _apply_and_collect(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    old_service: str,
    new_service: str,
    diff_cents: int,
    pending_edit: dict,
    now: datetime,
    source: str,
    adjustment_id: UUID | None = None,
) -> ServiceChangeResult:
    """Follow-up B (2026-10-27): the Pix of the difference could not be created because the
    provider was unavailable (`provider_unavailable`). The change stands NOW - the
    `increase_free` way - so the patient never has to ask (or pay) again here; a
    `collect_due` adjustment records it (never money held: `held_deposit_cents` counts only
    PAID differences) and ONE `service_diff_collect_due` item tells the clinic to collect
    `diff_cents` itself.

    The item is not linked to the sinal (`deposit_id` NULL): it is money still to RECEIVE, so it
    must not stamp/flag the sinal or show the sinal's payment date. Never commits."""
    adjustment = DepositAdjustment(
        id=adjustment_id or uuid4(),
        tenant_id=tenant.id,
        deposit_id=deposit.id,
        appointment_id=appointment.id,
        kind=KIND_INCREASE,
        status=STATUS_COLLECT_DUE,
        old_service=old_service[:120],
        new_service=new_service[:120],
        diff_cents=diff_cents,
        resolved_at=now,
    )
    session.add(adjustment)
    await session.flush()
    edit = deserialize_edit(serialize_edit(pending_edit, appointment, source=source))
    edit["appointment_type"] = new_service
    written = await _write(
        session, tenant, appointment, edit, source=source,
        key=f"service_change:{appointment.id}:{adjustment.id}",
    )
    await attention.open_attention_item(
        session,
        tenant_id=tenant.id,
        reason=REASON_SERVICE_DIFF_COLLECT_DUE,
        dedupe_key=adjustment_queries.collect_item_key(deposit.provider, adjustment.id),
        provider=deposit.provider,
        patient_id=deposit.patient_id,
        amount_cents=diff_cents,
        slot_start=appointment.start_at,
        slot_end=appointment.end_at,
        service_label=new_service,
    )
    logger.warning(
        "service_change_applied_collect_due",
        tenant_id=str(tenant.id),
        appointment_id=str(appointment.id),
        adjustment_id=str(adjustment.id),
    )
    return ServiceChangeResult(
        kind="applied_now",
        diff_cents=diff_cents,
        adjustment_id=adjustment.id,
        decision=DECISION_COLLECT,
        changed_fields=written.fields,
        notice_id=written.notice_id,
    )


# --------------------------------------------------------------------------
# the webhook side: applying a PAID difference (called by event_processor)
# --------------------------------------------------------------------------


async def apply_paid_adjustment(
    session: AsyncSession,
    tenant: Tenant,
    adjustment: DepositAdjustment,
    *,
    now: datetime,
) -> tuple[str, ...] | None:
    """Write the `pending_edit` of a difference whose payment was just claimed (the caller
    already moved it `awaiting_payment -> applied`, so this runs once). None when the change
    can no longer be applied - the consultation is gone, not live, past, or the row moved on
    since the request (another edit): then NOTHING is written and the caller opens an item."""
    if adjustment.appointment_id is None or not adjustment.pending_edit:
        return None
    appointment = await session.scalar(
        select(Appointment)
        .where(Appointment.id == adjustment.appointment_id, Appointment.tenant_id == tenant.id)
        .with_for_update()
    )
    data = adjustment.pending_edit
    expected = data.get("expected") or {}
    log = {"tenant_id": str(tenant.id), "adjustment_id": str(adjustment.id)}
    if (
        appointment is None
        or not is_live_status(appointment.status)
        or appointment.start_at is None
        or as_utc(appointment.start_at) <= as_utc(now)
        or not _same_service_and_doctor(appointment, expected)
    ):
        logger.warning("service_change_not_applicable", cause="row", **log)
        return None
    window = _paid_window(appointment, data, expected)
    if window is None or not await _window_fits(session, tenant, appointment, *window[:2]):
        logger.warning("service_change_not_applicable", cause="window", **log)
        return None
    start, end, time_changed = window
    # Final-review I1: only what the service change owns is written (service, end, and the
    # start when THIS change moved it); convênio, plan ids, for-whom, doctor and the event come
    # from the CURRENT row, so an edit made while the Pix was pending is never reverted.
    plan_ids = (appointment.insurance_plan_id, appointment.insurance_professional_plan_id)
    edit = {
        "appointment_type": data.get("appointment_type") or appointment.appointment_type,
        "professional_id": appointment.professional_id,
        "insurance": appointment.insurance,
        "attendee_name": appointment.attendee_name,
        "google_event_id": appointment.google_event_id,
        "google_event_link": appointment.google_event_link,
        "start_at": start,
        "end_at": end,
        "time_changed": time_changed,
        "doctor_changed": False,
        "calendar_changed": False,
    }
    written = await _write(
        session, tenant, appointment, edit,
        source=data.get("source") or SOURCE_SYSTEM,
        key=f"service_change:{appointment.id}:{adjustment.id}",
    )
    # The writer re-resolves the plan ids from the convênio text: keep the row's own ones.
    appointment.insurance_plan_id, appointment.insurance_professional_plan_id = plan_ids
    if time_changed:
        # The time moves only now (the change waited for the Pix): it counts now. A limit
        # reached meanwhile never undoes a paid change - logged, like flow_runner's race.
        allowed, _count = await register_reschedule(
            session, tenant=tenant, appointment=appointment
        )
        if not allowed:
            logger.warning(
                "service_change_reschedule_limit_race",
                tenant_id=str(tenant.id),
                adjustment_id=str(adjustment.id),
            )
    logger.info(
        "service_change_paid_applied", tenant_id=str(tenant.id), adjustment_id=str(adjustment.id)
    )
    return written.fields


async def _run_hooks(
    hooks: list[AdjustmentHook], session: AsyncSession, tenant: Tenant, notice: AdjustmentNotice
) -> None:
    for hook in list(hooks):
        try:
            await hook(session, tenant, notice)
        except Exception as exc:  # noqa: BLE001 - a notice never undoes the money state
            await session.rollback()
            logger.warning(
                "service_change_hook_failed",
                hook=getattr(hook, "__name__", "hook"),
                error_type=type(exc).__name__,
            )


async def _notice_for(session: AsyncSession, tenant: Tenant, adjustment_id: UUID):
    adjustment = await session.get(DepositAdjustment, adjustment_id)
    if adjustment is None or adjustment.tenant_id != tenant.id:
        return None
    deposit = await session.get(PixDeposit, adjustment.deposit_id)
    return _notice(adjustment, deposit.patient_id if deposit is not None else None)


async def run_applied_hooks(
    session: AsyncSession, tenant: Tenant, adjustment_id: UUID, *, redis=None
) -> None:
    """After the commit that applied a paid difference (`event_processor.run_post_commit`).
    Runs only for the event that won the `awaiting -> applied` claim, so a redelivered webhook
    never repeats any of it."""
    notice = await _notice_for(session, tenant, adjustment_id)
    if notice is None:
        return
    await _run_hooks(ADJUSTMENT_APPLIED_HOOKS, session, tenant, notice)
    try:
        await after_paid_move(session, tenant, adjustment_id, redis=redis)
    except Exception as exc:  # noqa: BLE001 - never undoes the paid change
        await session.rollback()
        logger.warning("service_change_move_followup_failed", error_type=type(exc).__name__)


async def after_paid_move(
    session: AsyncSession, tenant: Tenant, adjustment_id: UUID, *, redis=None
) -> None:
    """A paid difference whose change ALSO moved the time (`pending_edit["time_changed"]`):
    the move happened only now, so what an immediate move does after its commit happens here
    (the same two steps as `workers/shared/flow_runner._apply_flow_result`):

    * the reminders are re-planned for the new time (`reminder_hooks.after_appointment_rescheduled`,
      own transaction, best-effort) - the old rows no longer match the start;
    * a PATIENT-initiated change (source `flow`) queues the A7 "Consulta remarcada" notice once
      (`enqueue_appointment_change_notification`; its arq job id names the change). A clinic
      change (hub) never announces itself, exactly as on the immediate path.
    """
    from secretaria.plugins.appointment_change_notifications import (
        CHANGE_RESCHEDULED,
        enqueue_appointment_change_notification,
    )
    from secretaria.services import reminder_hooks
    from secretaria.services.appointment_status import SOURCE_FLOW

    adjustment = await session.get(DepositAdjustment, adjustment_id)
    if (
        adjustment is None
        or adjustment.tenant_id != tenant.id
        or adjustment.kind != KIND_INCREASE
        or adjustment.status != STATUS_APPLIED
        or adjustment.appointment_id is None
    ):
        return
    data = adjustment.pending_edit or {}
    if not data.get("time_changed"):
        return
    appointment = await session.scalar(
        select(Appointment).where(
            Appointment.id == adjustment.appointment_id, Appointment.tenant_id == tenant.id
        )
    )
    if appointment is None or not is_live_status(appointment.status):
        return
    if _iso(appointment.start_at) != _iso(data.get("start_at")):
        # A later reschedule won (`_paid_window`): THIS change did not move the time, so it
        # neither re-plans nor announces a move (the reschedule did its own).
        return
    tenant_id, appointment_id, new_start = tenant.id, appointment.id, appointment.start_at
    replan = reminder_hooks.enabled_for(tenant) or (appointment.confirmation_count or 0) > 0
    previous_raw = (data.get("expected") or {}).get("start_at")
    previous_start = datetime.fromisoformat(previous_raw) if previous_raw else None
    # End this session's read transaction before the hook opens its own.
    await session.commit()
    if replan:
        await reminder_hooks.after_appointment_rescheduled(appointment_id)
    if data.get("source") == SOURCE_FLOW:
        await enqueue_appointment_change_notification(
            redis,
            tenant_id,
            appointment_id,
            CHANGE_RESCHEDULED,
            new_start_at=new_start,
            previous_start_at=previous_start,
        )
    logger.info(
        "service_change_paid_move_followed_up",
        tenant_id=str(tenant_id),
        adjustment_id=str(adjustment_id),
        replanned=replan,
    )


async def run_not_applied_hooks(
    session: AsyncSession, tenant: Tenant, adjustment_id: UUID, *, paid_cents: int | None = None
) -> None:
    """After the commit that turned a paid difference into an attention item because the change
    could no longer be applied, or because it was UNDERPAID (`paid_cents`, follow-up C)
    (`event_processor._on_adjustment_paid`). Runs only for the event that decided it."""
    notice = await _notice_for(session, tenant, adjustment_id)
    if notice is not None:
        if paid_cents is not None:
            notice = replace(notice, paid_cents=paid_cents)
        await _run_hooks(ADJUSTMENT_NOT_APPLIED_HOOKS, session, tenant, notice)


async def run_expired_hooks(session: AsyncSession, tenant: Tenant, adjustment_id: UUID) -> None:
    """After the commit that expired an unpaid difference (webhook EXPIRED or the sweep)."""
    notice = await _notice_for(session, tenant, adjustment_id)
    if notice is not None:
        await _run_hooks(ADJUSTMENT_EXPIRED_HOOKS, session, tenant, notice)


# --------------------------------------------------------------------------
# answer_refund_question
# --------------------------------------------------------------------------


async def answer_refund_question(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    adjustment_id: UUID,
    accept: bool,
    now: datetime,
) -> str:
    """The patient's answer to "quer a diferença de volta?". Never commits; idempotent.

    Returns `refund_requested` (an attention item was opened for the clinic - the system
    never returns money itself), `kept_credit`, `already_answered` (answered before, or the
    24 h passed: nothing changes) or `not_found` (no such question for this tenant).
    """
    adjustment = await session.scalar(
        select(DepositAdjustment)
        .where(DepositAdjustment.id == adjustment_id, DepositAdjustment.tenant_id == tenant_id)
        .with_for_update()
    )
    if adjustment is None or adjustment.kind != KIND_DECREASE:
        return "not_found"
    target = STATUS_REFUND_REQUESTED if accept else STATUS_KEPT_CREDIT
    if not await move_adjustment(
        session, adjustment, from_status=STATUS_ASKED, to_status=target, now=now
    ):
        return "already_answered"
    if accept:
        deposit = await session.get(PixDeposit, adjustment.deposit_id)
        appointment = (
            await session.get(Appointment, adjustment.appointment_id)
            if adjustment.appointment_id
            else None
        )
        await attention.open_attention_item(
            session,
            tenant_id=tenant_id,
            reason=REASON_SERVICE_DIFF_REFUND_DUE,
            dedupe_key=attention.attention_dedupe_key(
                deposit.provider, f"adjustment:{adjustment.id}", REASON_SERVICE_DIFF_REFUND_DUE
            ),
            deposit=deposit,
            slot_start=appointment.start_at if appointment is not None else None,
            slot_end=appointment.end_at if appointment is not None else None,
            service_label=adjustment.new_service,
            suggested_refund_cents=adjustment.diff_cents,
        )
    logger.info(
        "service_change_refund_answered",
        tenant_id=str(tenant_id),
        adjustment_id=str(adjustment.id),
        outcome=target,
    )
    return target


# --------------------------------------------------------------------------
# expire_adjustments - the sweep
# --------------------------------------------------------------------------

EnqueueFn = Callable[[str, str], Awaitable[None]]

# Where the next expiry sweep starts in the ordered list of overdue unpaid differences (per
# process, no column: a restart simply begins at the head again). A row the sweep cannot settle
# - provider down, account disconnected - stays `awaiting_payment` and, ordered first by
# `expires_at`, would otherwise fill every batch and starve the rows behind it.
_SWEEP_OFFSET = 0


async def expire_adjustments(
    session: AsyncSession,
    *,
    now: datetime,
    enqueue: EnqueueFn | None = None,
    limit: int = 50,
) -> int:
    """One sweep tick; commits per adjustment. Returns how many adjustments it resolved.

    1. `asked` for 24 h -> `kept_credit` (no item, no notice to the clinic).
    2. `awaiting_payment` past `expires_at` -> `get_charge` FIRST: paid -> a synthetic PAID
       event through the webhook path (`enqueue` given; never expired); provider down or the
       void failed -> untouched, the next tick retries; otherwise void, `expired`, and the
       `ADJUSTMENT_EXPIRED_HOOKS` (the patient's notice) after the commit. Untouched rows move
       `_SWEEP_OFFSET` on, so the next tick looks past them (no starvation of healthy rows).
    3. (follow-up B) `cancelled_void_pending` -> the void is retried (`_retry_pending_voids`).
    """
    resolved = 0
    questions = (
        await session.scalars(
            select(DepositAdjustment)
            .where(
                DepositAdjustment.status == STATUS_ASKED,
                DepositAdjustment.asked_at.is_not(None),
                DepositAdjustment.asked_at <= now - QUESTION_TTL,
            )
            .order_by(DepositAdjustment.asked_at)
            .limit(limit)
        )
    ).all()
    for adjustment in questions:
        if await move_adjustment(
            session, adjustment, from_status=STATUS_ASKED, to_status=STATUS_KEPT_CREDIT, now=now
        ):
            resolved += 1
    await session.commit()

    global _SWEEP_OFFSET

    async def due_ids(offset: int) -> list[UUID]:
        return list(
            (
                await session.scalars(
                    select(DepositAdjustment.id)
                    .where(
                        DepositAdjustment.status == STATUS_AWAITING_PAYMENT,
                        DepositAdjustment.expires_at.is_not(None),
                        DepositAdjustment.expires_at <= now,
                    )
                    .order_by(DepositAdjustment.expires_at, DepositAdjustment.id)
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        )

    offset = _SWEEP_OFFSET
    due = await due_ids(offset)
    if not due and offset:
        offset = 0
        due = await due_ids(0)
    untouched = 0
    for adjustment_id in due:
        try:
            tenant = await _expire_one(session, adjustment_id, now, enqueue)
        except Exception as exc:  # noqa: BLE001 - one adjustment never stops the batch
            await session.rollback()
            logger.warning("service_change_expiry_failed", error_type=type(exc).__name__)
            untouched += 1
            continue
        if tenant is None:
            untouched += 1
            continue
        resolved += 1
        await run_expired_hooks(session, tenant, adjustment_id)
        await session.commit()
    # Rows left as they were (provider down, account disconnected, a paid one handed to the
    # webhook path) move the window on, so they never keep a healthy row out of the batch.
    _SWEEP_OFFSET = offset + untouched if len(due) == limit else 0
    resolved += await _retry_pending_voids(session, now=now, enqueue=enqueue, limit=limit)
    return resolved


# Same rotating start as `_SWEEP_OFFSET`, for the differences whose void is still pending.
_VOID_OFFSET = 0
# Follow-up review minor 1: a void still impossible this long after the cancellation is given
# up (logged once; a payment that still lands is an ORPHAN item). The Pix itself expired long
# before (`deposit_hold_ttl_minutes`).
VOID_RETRY_WINDOW = timedelta(days=7)


async def _retry_pending_voids(
    session: AsyncSession, *, now: datetime, enqueue: EnqueueFn | None, limit: int
) -> int:
    """Follow-up B (2026-10-27): a difference cancelled while the provider could not void its
    Pix (`cancelled_void_pending`, `adjustment_queries.settle_open_adjustments`) is retried on
    every tick until the provider answers - so the patient is never left with a live charge.
    `get_charge` first: a PAID charge is never deleted (it is reconciled through the webhook
    path, where a non-awaiting difference is an ORPHAN item); otherwise the charge is voided and
    the row becomes `cancelled`. No patient notice (the cancellation already told them)."""
    global _VOID_OFFSET

    async def pending_ids(offset: int) -> list[UUID]:
        return list(
            (
                await session.scalars(
                    select(DepositAdjustment.id)
                    .where(DepositAdjustment.status == STATUS_CANCELLED_VOID_PENDING)
                    .order_by(DepositAdjustment.resolved_at, DepositAdjustment.id)
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        )

    offset = _VOID_OFFSET
    pending = await pending_ids(offset)
    if not pending and offset:
        offset = 0
        pending = await pending_ids(0)
    done = untouched = 0
    for adjustment_id in pending:
        try:
            settled = await _retry_void_one(session, adjustment_id, now, enqueue)
        except Exception as exc:  # noqa: BLE001 - one row never stops the batch
            await session.rollback()
            logger.warning("service_change_void_retry_failed", error_type=type(exc).__name__)
            settled = False
        if settled:
            done += 1
        else:
            untouched += 1
    _VOID_OFFSET = offset + untouched if len(pending) == limit else 0
    return done


async def _retry_void_one(
    session: AsyncSession, adjustment_id: UUID, now: datetime, enqueue: EnqueueFn | None
) -> bool:
    from secretaria.services.payments import jobs

    adjustment = await session.get(DepositAdjustment, adjustment_id)
    if adjustment is None or adjustment.status != STATUS_CANCELLED_VOID_PENDING:
        return False
    deposit = await session.get(PixDeposit, adjustment.deposit_id)
    if deposit is None or deposit.tenant_id != adjustment.tenant_id:
        return False
    log = {"tenant_id": str(adjustment.tenant_id), "adjustment_id": str(adjustment.id)}
    charge_id = adjustment.provider_charge_id
    since = adjustment.resolved_at or adjustment.created_at
    if charge_id and since is not None and as_utc(now) - as_utc(since) > VOID_RETRY_WINDOW:
        logger.warning("service_change_void_retry_gave_up", cause="window", **log)
        charge_id = None  # final: stop asking
    if charge_id:
        provider = registry.find_provider(deposit.provider)
        account = await cfg.get_payment_account(session, adjustment.tenant_id, deposit.provider)
        if provider is None or account is None:
            return False
        try:
            state = await provider.get_charge(account, charge_id)
        except Exception as exc:  # noqa: BLE001 - cannot tell: never delete blind
            if isinstance(exc, ProviderError) and exc.status_code == 404:
                # The provider no longer knows the charge: nothing left to void.
                logger.warning("service_change_void_retry_gave_up", cause="not_found", **log)
                state = None
                charge_id = None
            else:
                logger.warning(
                    "service_change_void_retry_unavailable",
                    error_type=type(exc).__name__,
                    **log,
                )
                return False
        if charge_id is None:
            pass
        elif state is not None and state.status is PaymentStatus.REFUNDED:
            # Already paid and refunded at the provider: never deleted; nothing to void.
            logger.warning("service_change_void_retry_gave_up", cause="refunded", **log)
        elif state is not None and state.status is PaymentStatus.PAID:
            if enqueue is None:
                return False  # the webhook (or a sweep with `enqueue`) reconciles it
            await jobs.inject_paid_event_for_charge(
                session,
                tenant_id=adjustment.tenant_id,
                provider=deposit.provider,
                charge_id=charge_id,
                state=state,
                enqueue=enqueue,
            )
        else:
            try:
                await provider.cancel_charge(account, charge_id)
            except Exception as exc:  # noqa: BLE001 - not voided
                if not (isinstance(exc, ProviderError) and not exc.retryable
                        and exc.status_code is not None and 400 <= exc.status_code < 500):
                    logger.warning(
                        "service_change_void_retry_void_failed",
                        error_type=type(exc).__name__,
                        **log,
                    )
                    return False  # an outage: retry next tick
                # Refused for good (4xx): asking again every tick changes nothing.
                logger.warning(
                    "service_change_void_retry_gave_up", cause="refused",
                    status_code=exc.status_code, **log,
                )
    won = await move_adjustment(
        session, adjustment, from_status=STATUS_CANCELLED_VOID_PENDING,
        to_status=STATUS_CANCELLED, now=now,
    )
    await session.commit()
    if won:
        logger.info("service_change_void_retried", **log)
    return won


async def _expire_one(
    session: AsyncSession, adjustment_id: UUID, now: datetime, enqueue: EnqueueFn | None
) -> Tenant | None:
    from secretaria.services.payments import jobs

    adjustment = await session.get(DepositAdjustment, adjustment_id)
    if adjustment is None or adjustment.status != STATUS_AWAITING_PAYMENT:
        return None
    deposit = await session.get(PixDeposit, adjustment.deposit_id)
    tenant = await session.get(Tenant, adjustment.tenant_id)
    if deposit is None or tenant is None or not adjustment.provider_charge_id:
        return None
    provider = registry.find_provider(deposit.provider)
    account = await cfg.get_payment_account(session, adjustment.tenant_id, deposit.provider)
    if provider is None or account is None:
        return None
    log = {"tenant_id": str(adjustment.tenant_id), "adjustment_id": str(adjustment.id)}
    try:
        state = await provider.get_charge(account, adjustment.provider_charge_id)
    except Exception as exc:  # noqa: BLE001 - cannot tell: never expire blind
        logger.warning(
            "service_change_expiry_provider_unavailable", error_type=type(exc).__name__, **log
        )
        return None
    if state.status is PaymentStatus.PAID:
        if enqueue is not None:
            await jobs.inject_paid_event_for_charge(
                session,
                tenant_id=adjustment.tenant_id,
                provider=deposit.provider,
                charge_id=adjustment.provider_charge_id,
                state=state,
                enqueue=enqueue,
            )
        return None
    try:
        await provider.cancel_charge(account, adjustment.provider_charge_id)
    except Exception as exc:  # noqa: BLE001 - not voided: retry next tick
        logger.warning("service_change_expiry_void_failed", error_type=type(exc).__name__, **log)
        return None
    won = await move_adjustment(
        session, adjustment, from_status=STATUS_AWAITING_PAYMENT, to_status=STATUS_EXPIRED,
        now=now,
    )
    await session.commit()
    if not won:
        return None
    logger.info("service_change_expired", **log)
    return tenant


# --------------------------------------------------------------------------
# the agenda (Task 6): ONE helper for "the event follows the row after a service change"
# --------------------------------------------------------------------------

CalendarResolver = Callable[[AsyncSession, Tenant, Appointment], Awaitable[Any]]


async def _default_calendar(
    session: AsyncSession, tenant: Tenant, appointment: Appointment
) -> Any | None:
    """The agenda that owns the appointment's event (the doctor's own, else the clinic's), or
    None when it cannot be built or the event's origin is unknown (never guess an agenda)."""
    from secretaria.services.booking_promotion import calendar_for_booking

    try:
        owner = calendar_professional_id(appointment)
    except CalendarOriginUnresolved:
        return None
    return await calendar_for_booking(session, tenant, owner)


# Replaceable seam (tests; a worker that already holds the agenda passes it directly).
calendar_for_appointment: CalendarResolver = _default_calendar


def agenda_summary(appointment: Appointment, patient_name: str | None) -> str:
    who = appointment.attendee_name or patient_name or "Paciente"
    return f"{appointment.appointment_type or 'Consulta'} - {who}"


async def sync_agenda_event(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    calendar: Any,
    *,
    patient_name: str | None,
    window_changed: bool,
) -> None:
    """Put the appointment's event in line with its ROW: service in the title and the
    description, start and end. Used by the patient's edit when a service change stands at
    Confirmar (`workers/shared/appointment_edit_apply.py`) and by `update_agenda_after_payment`
    - the clinic's agenda edit (Task 7) reuses it too.

    A window that changed is checked first (free on the agenda, no live booking hold of another
    patient) and written exclusively on the secretarIA agenda; raises `CalendarSlotTakenError`
    when it does not fit and lets `CalendarUnavailableError` through. Never commits, never
    touches the row or the money.
    """
    event_id = (appointment.google_event_id or "").strip()
    if not event_id:
        return
    start = as_utc(appointment.start_at)
    end = as_utc(appointment.end_at or appointment.start_at)
    summary = agenda_summary(appointment, patient_name)
    description = build_event_description(
        service=appointment.appointment_type,
        insurance=appointment.insurance,
        attendee_name=appointment.attendee_name,
    )
    if not window_changed:
        await calendar.update_event_details(event_id, start, end, summary, description)
        return
    if not await calendar.is_slot_free(start, end, ignore_event_id=event_id):
        raise CalendarSlotTakenError("service_change_window_taken")
    if await booking_hold.live_hold_overlaps(
        session, tenant.id, start, end, professional_id=appointment.professional_id
    ):
        raise CalendarSlotTakenError("service_change_window_held")
    await update_event_details_exclusive(calendar, event_id, start, end, summary, description)


async def update_agenda_after_payment(
    session: AsyncSession, tenant: Tenant, notice: AdjustmentNotice
) -> None:
    """`ADJUSTMENT_APPLIED_HOOKS`: the Pix of the difference was paid and the webhook wrote the
    row (money first, committed). Now the agenda follows. When the new window no longer fits,
    or the agenda cannot be reached, ONE attention item tells the clinic to adjust the agenda
    (`service_diff_agenda_pending`): never two overlapping events, never the payment undone.
    Sends nothing to the patient (the default notice does, once)."""
    if notice.kind != KIND_INCREASE or notice.appointment_id is None:
        return
    adjustment = await session.get(DepositAdjustment, notice.adjustment_id)
    appointment = await session.scalar(
        select(Appointment).where(
            Appointment.id == notice.appointment_id, Appointment.tenant_id == tenant.id
        )
    )
    if (
        adjustment is None
        or adjustment.tenant_id != tenant.id
        or appointment is None
        or not is_live_status(appointment.status)
        or not (appointment.google_event_id or "").strip()
    ):
        return
    expected = (adjustment.pending_edit or {}).get("expected") or {}
    window_changed = (
        _iso(appointment.start_at) != expected.get("start_at")
        or _iso(appointment.end_at) != expected.get("end_at")
    )
    patient = (
        await session.get(Patient, appointment.patient_id) if appointment.patient_id else None
    )
    log = {"tenant_id": str(tenant.id), "adjustment_id": str(adjustment.id)}
    try:
        calendar = await calendar_for_appointment(session, tenant, appointment)
        if calendar is None:
            raise LookupError("no_calendar")
        await sync_agenda_event(
            session,
            tenant,
            appointment,
            calendar,
            patient_name=patient.name if patient is not None else None,
            window_changed=window_changed,
        )
    except Exception as exc:  # noqa: BLE001 - the payment stands; the clinic fixes the agenda
        logger.warning(
            "service_change_agenda_not_updated", error_type=type(exc).__name__, **log
        )
        deposit = await session.get(PixDeposit, adjustment.deposit_id)
        await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason=REASON_SERVICE_DIFF_AGENDA_PENDING,
            dedupe_key=attention.attention_dedupe_key(
                deposit.provider,
                f"adjustment:{adjustment.id}",
                REASON_SERVICE_DIFF_AGENDA_PENDING,
            ),
            deposit=deposit,
            provider_charge_id=adjustment.provider_charge_id,
            slot_start=appointment.start_at,
            slot_end=appointment.end_at,
            service_label=adjustment.new_service,
        )
        await session.commit()
        return
    logger.info("service_change_agenda_updated", window_changed=window_changed, **log)


# --------------------------------------------------------------------------
# default patient notices (registered below; Tasks 6/7 may add their own hooks)
# --------------------------------------------------------------------------


async def _patient_of(session: AsyncSession, tenant: Tenant, notice: AdjustmentNotice):
    if notice.patient_id is None:
        return None
    patient = await session.get(Patient, notice.patient_id)
    if patient is None or patient.tenant_id != tenant.id:
        return None
    return patient


async def notify_patient_adjustment_expired(
    session: AsyncSession, tenant: Tenant, notice: AdjustmentNotice
) -> None:
    patient = await _patient_of(session, tenant, notice)
    if patient is None:
        return
    await notify.deliver(
        session, tenant, patient,
        [notify.Text(adjustment_expired_text(notice.old_service, notice.new_service))],
    )


async def notify_patient_adjustment_applied(
    session: AsyncSession, tenant: Tenant, notice: AdjustmentNotice
) -> None:
    patient = await _patient_of(session, tenant, notice)
    if patient is None:
        return
    await notify.deliver(
        session, tenant, patient,
        [notify.Text(adjustment_applied_text(notice.new_service, notice.diff_cents))],
    )


async def notify_patient_adjustment_not_applied(
    session: AsyncSession, tenant: Tenant, notice: AdjustmentNotice
) -> None:
    patient = await _patient_of(session, tenant, notice)
    if patient is None:
        return
    if notice.paid_cents is not None and notice.paid_cents < notice.diff_cents:
        body = adjustment_underpaid_text(notice.new_service, notice.diff_cents, notice.paid_cents)
    else:
        body = adjustment_not_applied_text(notice.new_service, notice.diff_cents)
    await notify.deliver(session, tenant, patient, [notify.Text(body)])


# The agenda first, then the patient's ONE notice of the paid difference (Task 6).
_DEFAULT_APPLIED_HOOKS: tuple[AdjustmentHook, ...] = (
    update_agenda_after_payment,
    notify_patient_adjustment_applied,
)
register_expired_hook(notify_patient_adjustment_expired)
register_not_applied_hook(notify_patient_adjustment_not_applied)
for _hook in _DEFAULT_APPLIED_HOOKS:
    register_applied_hook(_hook)

__all__ = [
    "ACTION_REFUND_NO",
    "ACTION_REFUND_YES",
    "ADJUSTMENT_APPLIED_HOOKS",
    "ADJUSTMENT_EXPIRED_HOOKS",
    "ADJUSTMENT_NOT_APPLIED_HOOKS",
    "BUTTON_REFUND_NO",
    "BUTTON_REFUND_YES",
    "DECISION_COLLECT",
    "REFUND_ACTIONS",
    "REFUND_QUESTION_BUTTONS",
    "AdjustmentNotice",
    "ServiceChangeResult",
    "adjustment_applied_text",
    "adjustment_expired_text",
    "adjustment_not_applied_text",
    "adjustment_underpaid_text",
    "agenda_summary",
    "answer_refund_question",
    "apply_paid_adjustment",
    "blocked_text",
    "collect_due_text",
    "provider_unavailable",
    "difference_pix_text",
    "expire_adjustments",
    "find_adjustment",
    "held_deposit_cents",
    "move_adjustment",
    "open_adjustment",
    "refund_answer_text",
    "refund_question_buttons",
    "refund_question_text",
    "request_service_change",
    "sync_agenda_event",
    "update_agenda_after_payment",
]
