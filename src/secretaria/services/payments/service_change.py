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

TASK-085 R 3rd round (tasks/TASK-085/sinal-trocas/mapa-u.md §1.2, §4.4): while the Pix of a
SUSPENDED change waits, the old consultation's event is off the agenda (`old_slot`, owned by
`services/payments/old_slot.py`). Every way the change ends without being applied - silence at the
choice deadline, no room for a choice, a payment that cannot apply it - tries to bring it back
(`restore_and_tell`): back -> `kept_old`; its slot taken -> `old_slot_lost` (the clinic rebooks,
the patient may pick "Outro horário"); the agenda down -> `restoring` (the sweep retries). A paid
change whose old event left gets a NEW event (`update_agenda_after_payment`). D-R14:
`price_requote` decides what a price the CLINIC changed does to a paid sinal.

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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import (
    LIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentStatus,
    Patient,
    PixDeposit,
    Professional,
    Tenant,
    Unit,
    is_live_status,
)
from secretaria.models.deposit_adjustment import (
    KIND_DECREASE,
    KIND_INCREASE,
    MODE_APPLIED,
    MODE_SUSPEND,
    OLD_SLOT_RELEASED,
    OLD_SLOT_RELEASING,
    OLD_SLOT_REPLACED,
    REBOOKABLE_STATUSES,
    RESTORABLE_STATUSES,
    STATUS_APPLIED,
    STATUS_ASKED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_CANCELLED_VOID_PENDING,
    STATUS_CLINIC_KEPT,
    STATUS_COLLECT_DUE,
    STATUS_EXPIRED,
    STATUS_KEPT_CREDIT,
    STATUS_OLD_SLOT_LOST,
    STATUS_REFUND_REQUESTED,
    STATUS_RESTORING,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import (
    REASON_SERVICE_DIFF_AGENDA_PENDING,
    REASON_SERVICE_DIFF_COLLECT_DUE,
    REASON_SERVICE_DIFF_REFUND_DUE,
    REASON_SERVICE_DIFF_UNPAID,
    REASON_UNSEEN_CHARGE_TO_VOID,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import agenda, booking_hold, tenant_config as cfg, unit_booking
from secretaria.services.appointment_calendar_origin import (
    CalendarOriginUnresolved,
    calendar_professional_id,
)
from secretaria.services.appointment_edit_write import write_appointment_edit
from secretaria.services.appointment_status import SOURCE_HUB, SOURCE_SYSTEM
from secretaria.services.calendar import CalendarSlotTakenError, build_event_description
from secretaria.services.local_calendar import (
    create_event_exclusive,
    update_event_details_exclusive,
)
from secretaria.services.patient_context import as_utc
from secretaria.services.payments import (
    accounts,
    adjustment_queries,
    attention,
    notify,
    old_slot,
    registry,
)
from secretaria.services.payments.adjustment_suspension import occupies_slot_clause
from secretaria.services.payments.deposit_gate import timing_is_before
from secretaria.services.payments.deposit_lifecycle import (
    _resolve_service_and_price,
    _tenant_tz,
    register_reschedule,
)
from secretaria.services.payments.money import format_brl, parse_brl_to_cents
from secretaria.services.payments.pix_message import pix_code_tail
from secretaria.services.payments.provider import ChargeRequest, PaymentStatus, ProviderError
from secretaria.services.payments.service_change_policy import (
    MIN_DIFFERENCE_CENTS,
    ChangeDecision,
    decide_service_change,
)
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


# --------------------------------------------------------------------------
# TASK-085 R (D-R4/D-R7/D-R8): the choice after the Pix of a SUSPENDED change expired unpaid.
# Frozen by S0 - tasks/TASK-085/sinal-trocas/contrato.md. Routed like the refund question: each
# tap carries `<action>|<adjustment id>` (schemas/webhook.py `_ACTION_BUTTON_PREFIXES`), on both
# channels, never through the LLM (workers/shared/service_change_actions.py
# `handle_service_diff_choice` -> `choose_after_expiry`). Labels are the owner's (D-R7), <= 20
# chars; the message body spells each option out (`choice_message_text`).
# --------------------------------------------------------------------------

ACTION_CHOICE_KEEP = "svcdiffkeep"
ACTION_CHOICE_RESEND = "svcdiffresend"
ACTION_CHOICE_BOTH = "svcdiffboth"
# TASK-085 R 3rd round (D-R11, mapa-u §1.5): the old slot was taken - choose another one for the
# old consultation (same service, doctor, unit and convênio; the paid sinal still counts).
ACTION_CHOICE_OTHER = "svcdiffother"
CHOICE_ACTIONS = frozenset(
    {ACTION_CHOICE_KEEP, ACTION_CHOICE_RESEND, ACTION_CHOICE_BOTH, ACTION_CHOICE_OTHER}
)
LABEL_CHOICE_KEEP = "Manter a antiga"
LABEL_CHOICE_RESEND = "Reenviar o Pix"
LABEL_CHOICE_BOTH = "Cancelar ambas"
LABEL_CHOICE_OTHER = "Outro horário"
# The "Cancelar ambas" action under another name: once the change is over (`old_slot_lost`) only
# ONE consultation is left to cancel (`lost_slot_buttons`).
LABEL_CANCEL_CONSULTATION = "Cancelar consulta"
# The `choice` argument of `choose_after_expiry`.
CHOICE_KEEP = "keep"
CHOICE_RESEND = "resend"
CHOICE_BOTH = "both"
CHOICE_OTHER = "other"
Choice = Literal["keep", "resend", "both", "other"]
CHOICE_OF_ACTION: dict[str, str] = {
    ACTION_CHOICE_KEEP: CHOICE_KEEP,
    ACTION_CHOICE_RESEND: CHOICE_RESEND,
    ACTION_CHOICE_BOTH: CHOICE_BOTH,
    ACTION_CHOICE_OTHER: CHOICE_OTHER,
}
# D-R8: the patient has up to 24 h to choose; no answer by then = the engine tries to bring the
# old consultation back (D-R11: `old_slot.restore_old_slot(trigger="silence")`).
CHOICE_TTL = timedelta(hours=24)
# Fix round 1 (Orchestrator ruling): nothing of a suspended change - its Pix, the choice - may run
# later than this before the EARLIER of the two consultations (old and new), minus the sweep's
# margin (it runs every ~2 min), so the old visit is never lost close to its time.
CHOICE_MIN_LEAD = timedelta(hours=1)
SWEEP_MARGIN = timedelta(minutes=5)
# The shortest Pix window worth suspending for (capped by the clinic's own TTL when that is
# shorter): with less room the change is NOT suspended - it falls back to the legacy deferred
# mode (mode NULL: the old visit stays valid and visible, the change happens only if paid).
MIN_SUSPEND_PIX = timedelta(minutes=30)
# The hold of the new window outlives the deadline it serves (the Pix's `expires_at`, then the
# choice's) by this much, so the sweep or a late webhook always finds it alive - but it never
# outlives the NEW consultation's start (`difference_hold_expiry`).
DIFFERENCE_HOLD_GRACE = timedelta(minutes=15)


def choice_buttons(
    adjustment_id: UUID, *, resend: bool = True, old_slot_free: bool = True
) -> list[tuple[str, str]]:
    """The (id, label) pairs of the choice for ONE adjustment, in the owner's order. `resend`
    False leaves "Reenviar o Pix" out (the new slot is no longer free - mapa §9 OQ-4).
    `old_slot_free` False (D-R11: somebody took the old slot) puts "Outro horário" in the place
    of "Manter a antiga"."""
    first = (
        (ACTION_CHOICE_KEEP, LABEL_CHOICE_KEEP)
        if old_slot_free
        else (ACTION_CHOICE_OTHER, LABEL_CHOICE_OTHER)
    )
    pairs = [first]
    if resend:
        pairs.append((ACTION_CHOICE_RESEND, LABEL_CHOICE_RESEND))
    pairs.append((ACTION_CHOICE_BOTH, LABEL_CHOICE_BOTH))
    return [(f"{action}|{adjustment_id}", label) for action, label in pairs]


def lost_slot_buttons(adjustment_id: UUID) -> list[tuple[str, str]]:
    """The buttons of the `old_slot_lost` notice (D-R11): "Outro horário" and "Cancelar
    consulta" (the `svcdiffboth` action - the change is over, only the old consultation is
    left)."""
    return [
        (f"{ACTION_CHOICE_OTHER}|{adjustment_id}", LABEL_CHOICE_OTHER),
        (f"{ACTION_CHOICE_BOTH}|{adjustment_id}", LABEL_CANCEL_CONSULTATION),
    ]


def suspend_cutoff(old_start: datetime, new_start: datetime) -> datetime:
    """The latest instant anything of a suspended change may run until: `min(old_start,
    new_start) - CHOICE_MIN_LEAD - SWEEP_MARGIN`."""
    return min(as_utc(old_start), as_utc(new_start)) - CHOICE_MIN_LEAD - SWEEP_MARGIN


def suspend_pix_expiry(
    now: datetime, ttl_minutes: int, old_start: datetime, new_start: datetime
) -> datetime | None:
    """The `expires_at` of the difference Pix of a SUSPENDED change: `now + ttl`, capped at
    `suspend_cutoff`. None = no room for a minimally useful Pix (less than `min(ttl,
    MIN_SUSPEND_PIX)` left): do NOT suspend - the engine falls back to the legacy deferred mode
    (and "Reenviar o Pix" answers `no_window`)."""
    now = as_utc(now)
    ttl = timedelta(minutes=ttl_minutes)
    expiry = min(now + ttl, suspend_cutoff(old_start, new_start))
    return expiry if expiry - now >= min(ttl, MIN_SUSPEND_PIX) else None


def choice_deadline(now: datetime, old_start: datetime, new_start: datetime) -> datetime | None:
    """Until when the patient may choose (D-R8): `now + CHOICE_TTL`, capped at `suspend_cutoff`
    (the earlier of the two consultations - 1 h - the sweep margin). None = no room left: the old
    consultation stands at once (`kept_old`), nobody is asked."""
    now = as_utc(now)
    deadline = min(now + CHOICE_TTL, suspend_cutoff(old_start, new_start))
    return deadline if deadline > now else None


def resend_fits(
    now: datetime, ttl_minutes: int, old_start: datetime, new_start: datetime
) -> bool:
    """Whether "Reenviar o Pix" is still possible: the new consultation is at least
    `CHOICE_MIN_LEAD` away AND a minimally useful Pix fits (`suspend_pix_expiry`). False ->
    `no_window`, and the buttons are offered without "Reenviar o Pix"."""
    if as_utc(new_start) - as_utc(now) < CHOICE_MIN_LEAD:
        return False
    return suspend_pix_expiry(now, ttl_minutes, old_start, new_start) is not None


def difference_hold_expiry(deadline: datetime, new_start: datetime) -> datetime:
    """The `expires_at` of the hold that serves `deadline` (a Pix's, or the choice's):
    `deadline + DIFFERENCE_HOLD_GRACE`, never past the NEW consultation's start."""
    return min(as_utc(deadline) + DIFFERENCE_HOLD_GRACE, as_utc(new_start))


ChoiceKind = Literal[
    "kept_old",  # keep (now `old_slot.restore_old_slot(trigger="tap")`): the old visit is back
    "resent",  # resend: a NEW adjustment (awaiting_payment) with a new Pix took over
    "cancel_old",  # both: -> cancelled; the CALLER now cancels the old visit
    "no_window",  # resend refused: the new slot is gone; nothing changed (offer keep/both only)
    "resend_failed",  # resend refused: the provider did not create the Pix; nothing changed
    "already_decided",  # not awaiting_choice any more (answered, deadline passed, released)
    "not_found",  # no such adjustment in this clinic, or not a suspended change
    # TASK-085 R 3rd round (D-R11, mapa-u §1.5):
    "rebook",  # other: the new hold freed; the CALLER enters the reschedule of the old visit
    "slot_taken",  # keep refused by the restore: the old slot was taken ("Outro horário")
    "restore_failed",  # keep: the agenda did not answer; nothing changed ("tente de novo")
]


@dataclass(frozen=True)
class ChoiceOutcome:
    """What `choose_after_expiry` did (contrato.md). Plain values, safe after the commit."""

    kind: ChoiceKind
    # "resent": the NEW adjustment; otherwise the tapped one (None for "not_found").
    adjustment_id: UUID | None = None
    # The OLD consultation ("cancel_old": the one the caller must cancel after the commit).
    appointment_id: UUID | None = None
    diff_cents: int = 0
    # "resent" only: the new Pix and its deadline.
    pix_copy_paste: str | None = None
    expires_at: datetime | None = None
    # The patient's names of the two consultations (`DepositAdjustment.old/new_service`).
    old_service: str | None = None
    new_service: str | None = None


UnpaidOutcome = Literal[
    "payment_in_flight",  # the provider says PAID: nothing changed - the paid path settles it
    "void_failed",  # the charge could not be voided / its state read: nothing changed, retry
    "choice_pending",  # suspend: -> awaiting_choice; send `choice_message_text` + buttons
    # Not returned by `on_difference_unpaid` since the 3rd round; still a `run_unpaid_followup`
    # outcome (the old consultation came back: the kept notice).
    "kept_old",
    # TASK-085 R 3rd round: suspend, no room for a choice -> `restoring`; after the commit
    # `run_unpaid_followup` brings the old consultation back (`old_slot.restore_old_slot`).
    "restoring",
    "collect_due",  # applied: -> collect_due + `service_diff_unpaid` item; notify the patient
    "cancelled",  # the consultation is gone / not live: -> cancelled, hold released, no notice
    "not_applicable",  # not awaiting_payment, or a legacy row (mode NULL): untouched
]

ResultKind = Literal[
    "applied_now",
    "awaiting_payment",
    "applied_awaiting_payment",
    "suspended",
    "asked",
    "blocked",
]


@dataclass(frozen=True)
class ServiceChangeResult:
    """What a service change did. `kind`:

    * `applied_now`      - the edit was written in the caller's transaction (no Pix, no question;
                           with `decision` `increase_collect` the Pix could not be created and
                           the clinic collects `diff_cents` - tell the patient);
    * `awaiting_payment` - the LEGACY deferred mode (mode NULL): a difference Pix exists, NOTHING
                           was written, the old consultation stays valid AND visible; unpaid =
                           the old one stands (today's texts). TASK-085 R returns it only as the
                           "before" fallback when no minimally useful Pix fits before the visits
                           (`suspend_pix_expiry` None) - callers keep today's handling for it;
    * `applied_awaiting_payment` - TASK-085 R D-R5 (a clinic that does not require the sinal to
                           book): the edit WAS written now (`changed_fields`, `notice_id`) and a
                           difference Pix exists (`pix_copy_paste`, `expires_at`,
                           `adjustment_id`, mode `applied`);
    * `suspended`        - TASK-085 R D-R4 (a clinic that books only after the sinal): a
                           difference Pix exists, NOTHING was written, the old consultation is
                           suspended (hidden from the patient, its slot kept) and the new window
                           is held (mode `suspend`);
    * `asked`            - the edit was written and the patient must be asked about the refund
                           (`adjustment_id` goes to `answer_refund_question`);
    * `blocked`          - nothing was written; `reason` is machine-readable:
                           `price_unavailable`, `deposit_not_paid`, `adjustment_pending`,
                           `refund_question_pending`, `collect_pending` (the clinic still has
                           to collect the previous difference), `no_payment_account`,
                           `charge_failed` (a permanent refusal at the provider); TASK-085 R:
                           `choice_pending` (the patient still has to choose after an expired
                           difference Pix) and `slot_taken` (a suspended change's new window is
                           not free / cannot be held).
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
    # TASK-085: the name the patient reads for the new consultation when it is NOT just the
    # service's (a change of UNIT only: "Consulta na Unidade Centro"); None = the service name.
    label: str | None = None


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
    # TASK-085 R: how the change waited for its Pix (None = legacy; "suspend"; "applied").
    mode: str | None = None


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


def _hhmm(when: datetime | None, tz: Any) -> str | None:
    """"14:30" of `when` in `tz` (a tzinfo or a zone name); None without both."""
    if when is None or tz is None:
        return None
    if isinstance(tz, str):
        try:
            tz = ZoneInfo(tz)
        except (ValueError, ZoneInfoNotFoundError):
            return None
    return as_utc(when).astimezone(tz).strftime("%H:%M")


def difference_pix_text(
    new_service: str,
    diff_cents: int,
    copy_paste: str | None,
    ttl_minutes: int,
    *,
    source: str | None = None,
    held_new_slot: bool = False,
    expires_at: datetime | None = None,
    tz: Any = None,
) -> str:
    """The Pix of the difference of a change that waits for it (`suspended`, and the legacy
    `awaiting_payment`). `source` = who asked for the change: `SOURCE_HUB` (the clinic's agenda
    edit) says so; anything else keeps the patient's own wording.

    TASK-085 R: `held_new_slot=True` ONLY for a `suspended` change. 3rd round (D-R10, mapa-u
    §4.6): the OLD consultation leaves the agenda while the Pix waits and only the new time stays
    reserved - until `expires_at` ("até 14:30", in `tz` = the clinic's zone or its name) when
    given, else "até o fim do prazo do Pix"; unpaid, the patient may go back to the old one (if
    its time is still free), pick another time, resend the Pix or cancel. The default (False:
    the legacy deferred mode `awaiting_payment`, no hold) never promises a reserved slot and keeps
    today's "se não for pago, a consulta continua como está"."""
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
    if held_new_slot:
        until = _hhmm(expires_at, tz)
        lines.append(
            "Enquanto isso, sua consulta antiga sai da agenda e o novo horário fica reservado "
            f"{f'até {until}' if until else 'até o fim do prazo do Pix'}."
        )
        lines.append(
            f"O código vale por {ttl_minutes} minutos. Se o Pix não for pago, você poderá voltar "
            "para a consulta antiga (se o horário ainda estiver livre), escolher outro horário, "
            "reenviar o Pix ou cancelar."
        )
    else:
        lines.append(
            f"O código vale por {ttl_minutes} minutos. Se não for pago, a consulta continua "
            "como está."
        )
    # ONE message: the code is its last lines (fenced - see `pix_message`), never a bubble apart.
    lines.extend(pix_code_tail(copy_paste))
    return "\n".join(lines)


def difference_pix_applied_text(
    new_service: str,
    diff_cents: int,
    copy_paste: str | None,
    ttl_minutes: int,
    *,
    source: str | None = None,
) -> str:
    """TASK-085 R D-R5 (`applied_awaiting_payment`): the change is ALREADY made; the Pix of the
    difference follows. Unpaid, the clinic settles the difference with the patient (never
    undone)."""
    value = format_brl(diff_cents)
    if source == SOURCE_HUB:
        head = f"A clínica alterou sua consulta para {new_service}."
    else:
        head = f"Pronto, sua consulta já foi alterada para {new_service}."
    lines = [
        head,
        f"Falta pagar a diferença do sinal: {value} via Pix.",
        f"O código vale por {ttl_minutes} minutos. Se não for pago, a clínica combina essa "
        "diferença com você.",
    ]
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


def applied_underpaid_text(diff_cents: int, paid_cents: int) -> str:
    """TASK-085 R (fix round 1, M3): the Pix of a change that was ALREADY made arrived SHORT.
    Neutral: the change stands; the clinic settles the rest (no new Pix, nothing refunded)."""
    missing = max(diff_cents - paid_cents, 0)
    return (
        f"Recebemos {format_brl(paid_cents)} no Pix da diferença do sinal; faltam "
        f"{format_brl(missing)}. A clínica combina o restante com você."
    )


def applied_orphan_text(diff_cents: int) -> str:
    """TASK-085 R (fix round 1, I1): the Pix of an applied change arrived after its consultation
    was cancelled. Neutral, never promises a refund (the clinic decides, through its item)."""
    return (
        f"Recebemos o Pix da diferença ({format_brl(diff_cents)}), mas sua consulta já tinha "
        "sido cancelada. A clínica vai entrar em contato com você."
    )


def adjustment_paid_after_applied_text(new_service: str, diff_cents: int) -> str:
    """TASK-085 R D-R5: the Pix of a change that was ALREADY made arrived."""
    return (
        f"Recebemos o Pix da diferença ({format_brl(diff_cents)}). Obrigado! "
        f"Sua consulta segue como {new_service}."
    )


def kept_old_notice_text(old_service: str) -> str:
    """TASK-085 R D-R8/D-R11 (3rd round, mapa-u §4.6): the change was not made (no answer in
    time, no room left to ask, "Manter a antiga", or a payment that could not apply it) and the
    old consultation came BACK to the agenda as it was (`old_slot.restore_old_slot` said
    "restored"); the new window was freed."""
    return f"Pronto! Sua consulta antiga voltou para a agenda, como era: {old_service}."


def old_slot_lost_text(old_service: str) -> str:
    """D-R11: the old consultation could not come back - another patient took its time while
    the Pix waited. The paid sinal still counts; the patient picks another time ("Outro
    horário") or waits for the clinic. Sent with `lost_slot_buttons`."""
    return (
        f"O horário da sua consulta antiga ({old_service}) foi ocupado enquanto esperávamos o "
        "Pix. O sinal que você pagou continua valendo. Toque em \"Outro horário\" para escolher "
        "um novo horário, ou aguarde: a clínica vai falar com você."
    )


def price_unpaid_text(diff_cents: int) -> str:
    """D-R14: the Pix of the difference after the CLINIC changed the price expired unpaid. The
    consultation stays booked; the clinic settles the difference (item `service_diff_unpaid`).
    `diff_cents` is accepted for the callers' one signature; the text names no value."""
    return (
        "O Pix da diferença do sinal venceu. Sua consulta continua marcada; a clínica vai "
        "combinar a diferença com você."
    )


def unpaid_collect_text(new_service: str, diff_cents: int) -> str:
    """TASK-085 R D-R5: the Pix of a change that was ALREADY made expired unpaid. The change
    stands; the clinic settles the difference (never an automatic charge or refund)."""
    return (
        f"O Pix da diferença do sinal ({format_brl(diff_cents)}) venceu sem pagamento. "
        f"Sua consulta continua como {new_service}; a clínica vai combinar essa diferença "
        "com você."
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
    # TASK-085 R
    "choice_pending": (
        "O Pix da diferença da troca anterior venceu e ainda falta você escolher o que fazer. "
        "Use os botões daquela mensagem antes de pedir outra troca."
    ),
    "slot_taken": (
        "O novo horário não está mais disponível para guardar enquanto você paga a diferença. "
        f"{_KEPT_AS_IT_WAS} Escolha outro horário, por favor."
    ),
}
_BLOCKED_DEFAULT = (
    f"Não foi possível calcular o sinal do novo serviço. {_KEPT_AS_IT_WAS} "
    "Para trocar, fale com a clínica."
)


# TASK-085: the same refusal for a change of the UNIT only (the new unit's price is unreadable).
_BLOCKED_UNIT_PRICE = (
    f"Não foi possível calcular o sinal da consulta nessa unidade. {_KEPT_AS_IT_WAS} "
    "Para trocar de unidade, fale com a clínica."
)


def blocked_text(reason: str | None, *, unit_only: bool = False) -> str:
    """What the patient reads when `request_service_change` was `blocked` (nothing changed).
    `unit_only` (TASK-085): the change was of the unit only (`ServiceChangeResult.label` set), so
    an unreadable price is the UNIT's, not a "novo serviço"'s."""
    if unit_only and (reason or "") not in _BLOCKED_TEXT:
        return _BLOCKED_UNIT_PRICE
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
    # TASK-083 follow-up E1: a unit picked with the new service travels with it. Absent key =
    # the unit stays as it is (every edit without a unit, every payload stored before this).
    if "unit_id" in edit:
        out["unit_id"] = _str_or_none(edit.get("unit_id"))
    out["expected"] = {
        "start_at": _iso(appointment.start_at),
        # The event's window before the change: the agenda hook re-checks a window that grew.
        "end_at": _iso(appointment.end_at),
        "appointment_type": appointment.appointment_type,
        "google_event_id": appointment.google_event_id,
        "professional_id": _str_or_none(appointment.professional_id),
    }
    if "unit_id" in edit:
        # Follow-up final review: the unit the row had, so a unit moved meanwhile is kept.
        out["expected"]["unit_id"] = _str_or_none(getattr(appointment, "unit_id", None))
    # TASK-085 R: the convênio the row had (a `mode` change is applied only onto that same row).
    out["expected"]["insurance"] = getattr(appointment, "insurance", None)
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
    if "unit_id" in data:
        edit["unit_id"] = _uuid_or_none(data.get("unit_id"))
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


# `_window_fits(professional_id=...)` default: the agenda the appointment is on.
_SAME_AGENDA: Any = object()


async def _window_fits(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    start: datetime,
    end: datetime,
    *,
    professional_id: Any = _SAME_AGENDA,
    exclude_adjustment_id: UUID | None = None,
) -> bool:
    """Does [start, end) still fit for this row? A window inside the row's own current one
    always fits on its OWN agenda (max(old, new) length is what is checked). Otherwise no other
    live booking of the same agenda (doctor, or the clinic's when there is none), no live hold
    and - TASK-083 - nothing of the same doctor at another clinic sharing his agenda may overlap.

    TASK-085 R: `professional_id` - the agenda the change LANDS on (a change of doctor is checked
    on the new doctor's agenda, where the row's own window counts for nothing);
    `exclude_adjustment_id` - the change's own hold never refuses the change.
    3rd round (D-R10, mapa-u §1.3): a consultation SUSPENDED by its own change left the agenda,
    so it does not occupy its slot (`occupies_slot_clause`).
    Database only: the Google agenda is re-checked by `update_agenda_after_payment` after the
    commit (no network call while the webhook holds the row lock)."""
    agenda_id = (
        appointment.professional_id
        if professional_id is _SAME_AGENDA
        else _uuid_or_none(professional_id)
    )
    current_start = as_utc(appointment.start_at)
    current_end = as_utc(appointment.end_at or appointment.start_at)
    if (
        agenda_id == appointment.professional_id
        and start >= current_start
        and end <= current_end
    ):
        return True
    same_agenda = (
        Appointment.professional_id == agenda_id
        if agenda_id is not None
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
            occupies_slot_clause(),
        )
        .limit(1)
    )
    if clash is not None:
        return False
    if await booking_hold.live_hold_overlaps(
        session,
        tenant.id,
        start,
        end,
        professional_id=agenda_id,
        exclude_adjustment_id=exclude_adjustment_id,
    ):
        return False
    # TASK-083 spec §7 item 5: the same doctor at ANOTHER clinic (a shared person's agenda) -
    # bare windows only; nothing without an agenda (no read at all for the clinic's agenda).
    return not await agenda.shared_busy(session, agenda_id, start, end)


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
        mode=adjustment.mode,
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


# What `open_adjustment` counts as still open (and blocks another change of the same sinal).
OPEN_ADJUSTMENT_STATUSES: tuple[str, ...] = (
    STATUS_AWAITING_PAYMENT,
    STATUS_ASKED,
    STATUS_AWAITING_CHOICE,
    STATUS_RESTORING,
    STATUS_OLD_SLOT_LOST,
)


async def open_adjustment(
    session: AsyncSession, *, tenant_id: UUID, deposit_id: UUID
) -> DepositAdjustment | None:
    """The adjustment still waiting for something (an unpaid difference, an unanswered
    question or - TASK-085 R - the patient's choice after an expired difference Pix; 3rd round:
    a change whose old consultation is being brought back, `restoring`, or whose old slot was
    lost, `old_slot_lost`), or None. Tasks 6/7/8 read it (menu state, cancellation)."""
    return await session.scalar(
        select(DepositAdjustment)
        .where(
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.deposit_id == deposit_id,
            DepositAdjustment.status.in_(OPEN_ADJUSTMENT_STATUSES),
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
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    service: str,
    professional_id,
    unit_id: UUID | None,
) -> int | None:
    """The new service's price, resolved exactly like the sinal's (owner's own catalog first)
    at `unit_id` (TASK-085: that unit's own price when it has one, else the base)."""
    probe = SimpleNamespace(
        appointment_type=service,
        professional_id=_uuid_or_none(professional_id) if professional_id is not None
        else appointment.professional_id,
        unit_id=unit_id,
    )
    name, price = await _resolve_service_and_price(session, tenant, probe)
    if name is None:
        return None
    return parse_brl_to_cents(price)


# `preview_service_change(unit_id=...)` default: the unit the appointment is at.
UNIT_OF_APPOINTMENT: Any = object()


def _unit_of_change(appointment: Appointment, pending_edit: dict) -> UUID | None:
    """The unit a change lands on (TASK-085 contract): `pending_edit["unit_id"]` when the KEY is
    there (None = no unit), otherwise the unit the appointment is at."""
    if "unit_id" in pending_edit:
        return _uuid_or_none(pending_edit["unit_id"])
    return getattr(appointment, "unit_id", None)


async def unit_change_moves_price(
    session: AsyncSession, *, tenant: Tenant, appointment: Appointment, unit_id
) -> bool:
    """Whether moving `appointment` - same service, same doctor - to `unit_id` (a UUID or its
    text; None = no unit) changes its price (TASK-085 R3). Compared in cents: two unreadable
    prices are "the same" (no sinal can be computed on either, so nothing changes for the
    money)."""
    service = appointment.appointment_type
    unit_id = _uuid_or_none(unit_id)
    if not service or unit_id == getattr(appointment, "unit_id", None):
        return False
    professional_id = appointment.professional_id
    before = await _price_cents(
        session, tenant, appointment, service, professional_id, appointment.unit_id
    )
    after = await _price_cents(session, tenant, appointment, service, professional_id, unit_id)
    return before != after


def unit_change_label(service: str, unit_name: str | None) -> str:
    """ "{serviço} na Unidade {unidade}" - the patient's name for a consultation that changes
    PLACE only (the same spelling as the booking's confirmation line, TASK-085 A7). A unit
    already called "Unidade ..." is not doubled; no unit is the service alone."""
    name = (unit_name or "").strip()
    if not name:
        return service
    if name.casefold().startswith("unidade"):
        return f"{service} na {name}"
    return f"{service} na Unidade {name}"


# `preview_service_change(insurance=...)` default: the convênio the appointment has.
INSURANCE_OF_APPOINTMENT: Any = object()

LABEL_MAX = 120


def _insurance_of_change(appointment: Appointment, pending_edit: dict) -> Any:
    """The convênio a change lands on: `pending_edit["insurance"]` when the KEY is there (None =
    particular), otherwise the appointment's own (`INSURANCE_OF_APPOINTMENT`)."""
    if "insurance" in pending_edit:
        return pending_edit["insurance"]
    return INSURANCE_OF_APPOINTMENT


def _same_text(a: str | None, b: str | None) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


async def _doctor_name(session: AsyncSession, tenant: Tenant, professional_id) -> str | None:
    if professional_id is None:
        return None
    row = await session.get(Professional, professional_id)
    if row is None or row.tenant_id != tenant.id:
        return None
    return (row.name or "").strip() or None


async def _change_labels(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    new_service: str,
    unit_id: UUID | None,
    professional_id: Any = UNIT_OF_APPOINTMENT,
    insurance: Any = INSURANCE_OF_APPOINTMENT,
) -> tuple[str, str]:
    """(old, new) labels of a change, for the adjustment, the Pix and the patient's texts.

    The service's names, as always; for a change of UNIT only (same service, same doctor, another
    unit) the names carry the place, so "trocar sua consulta para Consulta" reads "para Consulta
    na Unidade Centro". TASK-085 R (OQ-13): a change of doctor adds " com {médico}" and a change
    of convênio " pelo {convênio}" / " (particular)", in that order, on both sides; cut at 120.
    Units and doctors are read tenant-scoped: an unknown one names nothing. `professional_id` left
    out = the appointment's doctor."""
    if professional_id is UNIT_OF_APPOINTMENT:
        professional_id = appointment.professional_id
    old_service = appointment.appointment_type or ""
    old_label, new_label = old_service, new_service
    current_unit = getattr(appointment, "unit_id", None)
    doctor_moves = professional_id != appointment.professional_id
    new_insurance = appointment.insurance if insurance is INSURANCE_OF_APPOINTMENT else insurance
    insurance_moves = not _same_text(new_insurance, appointment.insurance)
    if new_service == old_service and unit_id != current_unit and not doctor_moves:
        old_unit = await unit_booking.tenant_unit(session, tenant.id, current_unit)
        new_unit = await unit_booking.tenant_unit(session, tenant.id, unit_id)
        old_label = unit_change_label(old_service, old_unit.name if old_unit is not None else None)
        new_label = unit_change_label(new_service, new_unit.name if new_unit is not None else None)
    if doctor_moves:
        old_doctor = await _doctor_name(session, tenant, appointment.professional_id)
        new_doctor = await _doctor_name(session, tenant, professional_id)
        if old_doctor:
            old_label = f"{old_label} com {old_doctor}"
        if new_doctor:
            new_label = f"{new_label} com {new_doctor}"
    if insurance_moves:
        old_label = f"{old_label}{_plan_suffix(appointment.insurance)}"
        new_label = f"{new_label}{_plan_suffix(new_insurance)}"
    return old_label[:LABEL_MAX], new_label[:LABEL_MAX]


def _plan_suffix(name: str | None) -> str:
    name = (name or "").strip()
    return f" pelo {name}" if name else " (particular)"


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
    unit_id: Any = UNIT_OF_APPOINTMENT,
    insurance: Any = INSURANCE_OF_APPOINTMENT,
) -> ChangeDecision:
    """The policy decision for a change on this PAID sinal - what `request_paid_change` will do,
    computed the same way (price resolved like the sinal's, what the clinic holds, the tenant's
    minimum days). Read-only: writes, charges and locks nothing, so the confirmation card can show
    the difference before Confirmar.

    `professional_id`: the doctor the change lands on (his own price). `unit_id` (TASK-085): the
    unit - its own price when it has one; left out, the appointment's unit; None, no unit (the
    base). `insurance` (TASK-085 R): the convênio text (None = particular); left out, the
    appointment's. Accepted for the callers' one signature; it never moves the sinal (D-R12: a
    convênio never exempts it)."""
    unit_id = (
        getattr(appointment, "unit_id", None)
        if unit_id is UNIT_OF_APPOINTMENT
        else _uuid_or_none(unit_id)
    )
    professional_id = _uuid_or_none(professional_id)
    price = await _price_cents(session, tenant, appointment, new_service, professional_id, unit_id)
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


def confirm_card_line_text(
    action: str,
    diff_cents: int,
    *,
    unit_only: bool = False,
    timing: str = "after",
    reserved: bool = True,
) -> str | None:
    """The line the confirmation card adds for a change on a paid sinal (None when there is
    nothing to say: no difference, or blocked - Confirmar then explains). Never promises money
    back. `unit_only` (TASK-085 R3): only the unit changes, so it is the UNIT that waits for the
    payment. `timing` (TASK-085 R, `deposit_gate.timing_is_before`): "before" - the change waits
    for the Pix while the current consultation leaves the agenda and the new time is reserved
    (D-R4/D-R10); "after" -
    the change is made at once and the Pix of the difference follows (D-R5). `reserved` (S7a):
    False = a "before" change that falls into the legacy deferred mode (no room for a suspension
    before the visits, contrato §1) - nothing is reserved, so the line never promises it."""
    value = format_brl(diff_cents)
    if action == "increase_charge":
        if timing == "before":
            what = "a unidade" if unit_only else "o serviço"
            line = (
                f"Diferença do sinal: {value}. Ao confirmar, enviamos o Pix dessa diferença; "
                f"{what} só muda depois do pagamento."
            )
            if not reserved:
                return line
            # 3rd round (D-R10, mapa-u §4.6): the current consultation LEAVES the agenda.
            return (
                f"{line} Até lá, sua consulta atual sai da agenda e o novo horário fica "
                "reservado."
            )
        return (
            f"Diferença do sinal: {value}. Ao confirmar, a troca vale na hora e enviamos o Pix "
            "dessa diferença."
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


def _pending_window(appointment: Appointment, pending_edit: dict) -> tuple[datetime, datetime]:
    """[start, end) of the change: the pending edit's own window, else the row's."""
    start = pending_edit.get("start_at") or appointment.start_at
    end = pending_edit.get("end_at") or appointment.end_at or start
    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    if isinstance(end, str):
        end = datetime.fromisoformat(end)
    return as_utc(start), as_utc(end)


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
    """Decide and do the money side of a change on a PAID sinal. Never commits.

    `pending_edit` is the R6 edit dict the caller would have written - the WHOLE target
    (`appointment_type`, `professional_id`, `unit_id`, `insurance`, `attendee_name`,
    `google_event_id`, `start_at`, `end_at`, `time_changed`, `doctor_changed`, ...). The caller
    keeps its own guards (live row, the reschedule limit when the time moves, the calendar) and
    must NOT write the edit itself: this function writes it when the change stands now. `source`
    is the status-log source of that write (`SOURCE_FLOW` / `SOURCE_HUB`).

    Priced at the doctor (`professional_id`), unit (`unit_id`: the KEY present = that value, even
    None) and convênio (`insurance`: the KEY present = that value; it names the change but never
    moves the sinal - D-R12) of the change (TASK-085 / R D-R1/D-R3). TASK-085 R, a dearer
    change by >= R$ 10 (contrato.md §1):

    * clinic "after" (D-R5) -> the edit is written NOW and a Pix of only the difference follows
      (`applied_awaiting_payment`, mode `applied`);
    * clinic "before" (D-R4) -> nothing written; the new window must be free on the agenda the
      change lands on (else `blocked/slot_taken`, nothing charged), a Pix of the difference until
      `suspend_pix_expiry` and the new window HELD for it (`suspended`, mode `suspend`); without
      room for a useful Pix -> the legacy deferred mode (`awaiting_payment`, mode NULL);
    * either: the provider down -> applied now, the clinic collects (`increase_collect`); a
      permanent refusal -> `blocked/charge_failed`, nothing written.
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
        reason = {
            STATUS_AWAITING_PAYMENT: "adjustment_pending",
            STATUS_AWAITING_CHOICE: "choice_pending",
            STATUS_RESTORING: "choice_pending",
            STATUS_OLD_SLOT_LOST: "choice_pending",
        }.get(pending.status, "refund_question_pending")
        return ServiceChangeResult(kind="blocked", reason=reason)
    # Follow-up review I3: a previous difference the clinic is still collecting (provider was
    # down, or - TASK-085 R D-R5 - its Pix expired unpaid) blocks ANY further change - also back
    # to the old service - until the clinic resolves that item: never a second charge, never a
    # stale "cobrar" for a moot difference.
    if await adjustment_queries.open_collect_adjustments(
        session, tenant_id=tenant.id, deposit=locked
    ):
        return ServiceChangeResult(kind="blocked", reason="collect_pending")

    unit_id = _unit_of_change(appointment, pending_edit)
    professional_id = _uuid_or_none(
        pending_edit.get("professional_id", appointment.professional_id)
    )
    insurance = _insurance_of_change(appointment, pending_edit)
    decision = await preview_service_change(
        session,
        tenant=tenant,
        appointment=appointment,
        deposit=locked,
        new_service=new_service,
        professional_id=professional_id,
        start_at=pending_edit.get("start_at") or appointment.start_at,
        now=now,
        unit_id=unit_id,
        insurance=insurance,
    )
    log = {"tenant_id": str(tenant.id), "appointment_id": str(appointment.id)}
    old_service, new_label = await _change_labels(
        session, tenant, appointment, new_service, unit_id, professional_id, insurance
    )
    label = new_label if new_label != new_service else None
    if decision.action == "blocked":
        logger.info("service_change_blocked", reason=decision.reason, **log)
        return ServiceChangeResult(
            kind="blocked", reason=decision.reason, decision="blocked", label=label
        )

    if decision.action == "increase_charge":
        charge = {
            "tenant": tenant, "appointment": appointment, "deposit": locked,
            "old_service": old_service, "new_service": new_service,
            "diff_cents": decision.diff_cents, "pending_edit": pending_edit, "now": now,
            "source": source, "label": label,
        }
        if not timing_is_before(tenant):
            return await _charge_difference(session, **charge, mode=MODE_APPLIED)
        new_start, new_end = _pending_window(appointment, pending_edit)
        expires = suspend_pix_expiry(
            now, _pix_ttl_minutes(tenant), appointment.start_at, new_start
        )
        if expires is None:
            # Contrato §1: no room for a minimally useful Pix before the visits - the legacy
            # deferred mode (no hold, no suspension; unpaid = the old one stands, today's text).
            logger.info("service_change_suspend_no_room", **log)
            return await _charge_difference(session, **charge, mode=None)
        # Lock order (contrato §4): deposit row (above) -> the NEW agenda -> the window check ->
        # the charge -> the hold. Nobody else's hold can land on the window in between.
        await booking_hold.serialize_agenda_in(session, tenant.id, professional_id)
        if not await _window_fits(
            session, tenant, appointment, new_start, new_end, professional_id=professional_id
        ):
            logger.info("service_change_blocked", reason="slot_taken", **log)
            return ServiceChangeResult(
                kind="blocked", reason="slot_taken", decision="increase_charge", label=label
            )
        return await _charge_difference(
            session, **charge, mode=MODE_SUSPEND, expires_at=expires
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
            new_service=(label or new_service)[:120],
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
        label=label,
    )


# TASK-085 R: the name every caller uses from now on - a change of ANY money field (service, unit,
# doctor, convênio) on a paid sinal goes through the same engine (D-R1/D-R3). Same function, same
# signature (contrato.md).
request_paid_change = request_service_change


# --------------------------------------------------------------------------
# TASK-085 R 3rd round (D-R14): the CLINIC changed the price of a booked consultation
# --------------------------------------------------------------------------

# `pending_edit["source"]` of a price-change adjustment (nothing of the consultation changes).
SOURCE_PRICE_CHANGE = "price_change"
# The decreases already settled with the patient as an abatement on the day.
_KEPT_DECREASE_STATUSES = (STATUS_CLINIC_KEPT, STATUS_KEPT_CREDIT)


def _recorded_sinal(pending_edit: dict | None) -> int | None:
    """The sinal a price-change adjustment recorded (`new_sinal_cents`); None for any other."""
    value = (pending_edit or {}).get("new_sinal_cents")
    if not is_price_change(pending_edit) or isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# Fix round 2 (re-review R1): within the SAME instant, what moves money first. A paid rise
# (or a difference to collect) re-bases what is held; an abatement is told before a refund asked
# on top of it (a refund is always asked about a drop BEYOND what was already abated).
_REPLAY_RANK = {
    (KIND_INCREASE, STATUS_APPLIED): 0,
    (KIND_INCREASE, STATUS_COLLECT_DUE): 0,
    (KIND_DECREASE, STATUS_CLINIC_KEPT): 1,
    (KIND_DECREASE, STATUS_KEPT_CREDIT): 1,
    (KIND_DECREASE, STATUS_REFUND_REQUESTED): 2,
}
_NEVER = datetime.min.replace(tzinfo=UTC)


def _replay_key(row) -> tuple:
    """The replay order of `_sinal_of_record`, deterministic (never the row's random id): when
    the money moved (`resolved_at`, unsettled rows last - they are skipped anyway), then when
    the row was born (`created_at`), then `_REPLAY_RANK`, then the sinal a price change recorded,
    highest first (drops chain downwards). Rows still tied after that have the same effect on
    the replay, whatever their order."""
    kind, status, _diff, _charge, pending_edit, resolved_at, created_at = row
    recorded = _recorded_sinal(pending_edit)
    return (
        resolved_at is None,
        as_utc(resolved_at) if resolved_at is not None else _NEVER,
        as_utc(created_at) if created_at is not None else _NEVER,
        _REPLAY_RANK.get((kind, status), 3),
        -(recorded if recorded is not None else 0),
    )


async def _sinal_of_record(session: AsyncSession, deposit: PixDeposit) -> int:
    """Fix round 1 (review I1): the sinal the patient was last TOLD this consultation costs,
    replaying the sinal's settled adjustments in the order they took effect (`_replay_key`;
    what the clinic holds starts at
    `amount_cents`, and so does the sinal of record):

    * a PAID increase (`applied`, with a charge): held += diff; the sinal of record = held (the
      rise was measured against what was held, so every earlier abatement is used up);
    * a decrease asked back (`refund_requested`): held -= diff; the sinal of record = the new one;
    * a decrease kept as an abatement (`clinic_kept` / `kept_credit`): held unchanged; the sinal of
      record = the new one - the abatement still OUTSTANDING is `held - record`;
    * a difference to collect (`collect_due`): the sinal of record = held + diff.

    "The new one" is `pending_edit["new_sinal_cents"]` for a price change (its `diff_cents` is
    the part beyond what was already abated) and `held -/+ diff` for an edit (its `diff_cents` is
    measured against what was held). Anything else (open, cancelled, expired) never moved money.
    Residual: a change that leaves NO adjustment (`increase_free`, `no_change` - a sinal moved by
    less than R$ 10, or back to exactly what was held) also uses up an abatement but is not seen
    here; the abatement then still counts until a paid rise or a refund."""
    rows = (
        await session.execute(
            select(
                DepositAdjustment.kind,
                DepositAdjustment.status,
                DepositAdjustment.diff_cents,
                DepositAdjustment.provider_charge_id,
                DepositAdjustment.pending_edit,
                DepositAdjustment.resolved_at,
                DepositAdjustment.created_at,
            ).where(
                DepositAdjustment.tenant_id == deposit.tenant_id,
                DepositAdjustment.deposit_id == deposit.id,
            )
        )
    ).all()
    held = record = int(deposit.amount_cents or 0)
    for kind, status, diff, charge_id, pending_edit, _resolved, _created in sorted(
        rows, key=_replay_key
    ):
        diff = int(diff or 0)
        recorded = _recorded_sinal(pending_edit)
        if kind == KIND_INCREASE:
            if status == STATUS_APPLIED and charge_id is not None:
                held += diff
                record = held
            elif status == STATUS_COLLECT_DUE:
                record = recorded if recorded is not None else held + diff
        elif kind == KIND_DECREASE:
            if status == STATUS_REFUND_REQUESTED:
                held -= diff
                record = recorded if recorded is not None else held
            elif status in _KEPT_DECREASE_STATUSES:
                record = recorded if recorded is not None else held - diff
    return record


async def _outstanding_abatement_cents(session: AsyncSession, deposit: PixDeposit) -> int:
    """What of the sinal held is already promised as an abatement on the day and still stands
    (`held_deposit_cents - _sinal_of_record`, never negative). A new drop must not tell, abate
    or ask back that part again; a paid rise or a refund consumes it."""
    held = await held_deposit_cents(session, deposit)
    return max(0, held - await _sinal_of_record(session, deposit))


async def preview_price_requote(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    now: datetime,
) -> ChangeDecision:
    """D-R14, read-only: what the price IN FORCE does to this consultation's PAID sinal - the
    `preview_service_change` of the row itself (its service, doctor, unit), with one netting:
    a drop is measured against what the clinic holds MINUS the abatement already promised and
    still outstanding (`_outstanding_abatement_cents`; fix round 1: a paid rise or a refund since
    then uses it up), so the same difference is never told, abated or asked back twice (a
    `refund_requested` is already out of what is held). A rise is measured against what is held
    (a promised abatement never makes the patient pay more). The sweep's
    `price_change.price_notice_needed` should use this too (UB2)."""
    if not appointment.appointment_type:
        return ChangeDecision(action="blocked", diff_cents=0, reason="price_unavailable")
    decision = await preview_service_change(
        session,
        tenant=tenant,
        appointment=appointment,
        deposit=deposit,
        new_service=appointment.appointment_type,
        professional_id=appointment.professional_id,
        start_at=appointment.start_at,
        now=now,
    )
    if decision.action not in ("decrease_ask", "decrease_keep"):
        return decision
    left = decision.diff_cents - await _outstanding_abatement_cents(session, deposit)
    if left <= 0:
        # UB2 fix round 1: the drop is already covered by an abatement told before - the price
        # did not "go back"; `reason` lets the opening say so.
        return ChangeDecision(action="no_change", diff_cents=0, reason="already_abated")
    days_until = (as_utc(appointment.start_at) - as_utc(now)).total_seconds() / 86400
    min_days = tenant.pix_difference_refund_min_days
    asks = left >= MIN_DIFFERENCE_CENTS and days_until >= (
        DEFAULT_MIN_DAYS if min_days is None else min_days
    )
    return ChangeDecision(action="decrease_ask" if asks else "decrease_keep", diff_cents=left)


def _price_change_edit(appointment: Appointment, held_cents: int, new_sinal_cents: int) -> dict:
    """The `pending_edit` of a price-change adjustment: no edit at all (the consultation does not
    change), only what the sinal was and becomes and the row it was decided on."""
    return {
        "source": SOURCE_PRICE_CHANGE,
        "appointment_type": appointment.appointment_type,
        "old_sinal_cents": int(held_cents),
        "new_sinal_cents": int(new_sinal_cents),
        "expected": {
            "start_at": _iso(appointment.start_at),
            "end_at": _iso(appointment.end_at),
            "appointment_type": appointment.appointment_type,
            "google_event_id": appointment.google_event_id,
            "professional_id": _str_or_none(appointment.professional_id),
            "unit_id": _str_or_none(getattr(appointment, "unit_id", None)),
            "insurance": getattr(appointment, "insurance", None),
        },
    }


def is_price_change(pending_edit: dict | None) -> bool:
    """Whether an adjustment came from the clinic's price change (D-R14), not from an edit."""
    return (pending_edit or {}).get("source") == SOURCE_PRICE_CHANGE


async def price_requote(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    now: datetime,
) -> ServiceChangeResult:
    """D-R14 (mapa-u §2.3, §4.4): the patient opened the price-change link - decide WITH THE PRICE
    OF NOW on a PAID sinal. Never commits; never writes the consultation (nothing of it changes).

    The consultation, then the deposit, are locked (every change of a sinal is serialized).
    Refused (`blocked`, nothing charged or recorded): the consultation no longer live or already
    started (`appointment_not_live`, fix round 1), not PAID (`deposit_not_paid`), an adjustment
    still open (`adjustment_pending` / `refund_question_pending` / `choice_pending`), a
    difference the clinic collects on the day (ANY `collect_due` of this sinal, open item or
    not: `collect_pending` - what was paid does not count it, so a new figure would ask for it
    again), an unreadable price (`price_unavailable`).
    Otherwise `preview_price_requote`:

    * `increase_charge` -> a Pix of ONLY the difference, `mode="applied"` in ANY timing (the
      consultation stays as it is; unpaid -> `collect_due` + `service_diff_unpaid` +
      `price_unpaid_text`) -> "applied_awaiting_payment". The provider DOWN -> `blocked/outage`
      (a retry may work); a PERMANENT refusal -> `blocked/charge_failed`; no account ->
      `blocked/no_payment_account` (nothing recorded; UB2 fix round 1: the two refusals carry
      `diff_cents` so the opening can hand the difference to the clinic);
    * `decrease_ask` -> an `asked` decrease (the refund question, `svcdiffyes|`/`svcdiffno|`) ->
      "asked";
    * `decrease_keep` -> a `clinic_kept` decrease ("abatida na consulta") -> "applied_now";
    * `increase_free` / `no_change` -> "applied_now", no adjustment (`reason="already_abated"`
      when a drop is fully covered by an abatement already told - UB2 fix round 1).

    Every adjustment carries `pending_edit = {"source": "price_change", "old_sinal_cents",
    "new_sinal_cents", "expected"}` and names the service on both sides."""
    if (
        deposit.tenant_id != tenant.id
        or appointment.tenant_id != tenant.id
        or deposit.appointment_id != appointment.id
    ):
        raise ValueError("deposit, appointment and tenant do not match")
    # Fix round 1 (review M2): lock order appointment -> deposit (-> adjustment), like every
    # cancellation; a consultation no longer live or already started is never charged.
    row = await session.scalar(
        select(Appointment)
        .where(Appointment.id == appointment.id, Appointment.tenant_id == tenant.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not _visit_upcoming(row, now):
        return ServiceChangeResult(kind="blocked", reason="appointment_not_live")
    appointment = row
    locked = await session.scalar(
        select(PixDeposit)
        .where(PixDeposit.id == deposit.id, PixDeposit.tenant_id == tenant.id)
        .with_for_update()
    )
    if locked is None or locked.status != PixDepositStatus.PAID:
        return ServiceChangeResult(kind="blocked", reason="deposit_not_paid")
    pending = await open_adjustment(session, tenant_id=tenant.id, deposit_id=locked.id)
    if pending is not None:
        reason = {
            STATUS_AWAITING_PAYMENT: "adjustment_pending",
            STATUS_ASKED: "refund_question_pending",
        }.get(pending.status, "choice_pending")
        return ServiceChangeResult(kind="blocked", reason=reason)
    collecting = await session.scalar(
        select(DepositAdjustment.id)
        .where(
            DepositAdjustment.tenant_id == tenant.id,
            DepositAdjustment.deposit_id == locked.id,
            DepositAdjustment.status == STATUS_COLLECT_DUE,
        )
        .limit(1)
    )
    if collecting is not None:
        return ServiceChangeResult(kind="blocked", reason="collect_pending")

    decision = await preview_price_requote(
        session, tenant=tenant, appointment=appointment, deposit=locked, now=now
    )
    log = {"tenant_id": str(tenant.id), "appointment_id": str(appointment.id)}
    if decision.action == "blocked":
        logger.info("price_requote_blocked", reason=decision.reason, **log)
        return ServiceChangeResult(kind="blocked", reason=decision.reason, decision="blocked")
    service = (appointment.appointment_type or DEFAULT_SERVICE_LABEL)[:120]
    held = await held_deposit_cents(session, locked)

    if decision.action == "increase_charge":
        adjustment_id = uuid4()
        expires_at = as_utc(now) + timedelta(minutes=_pix_ttl_minutes(tenant))
        charged = await _create_difference_charge(
            session, tenant=tenant, deposit=locked, adjustment_id=adjustment_id,
            diff_cents=decision.diff_cents, shown=service, now=now, expires_at=expires_at,
            log=log,
        )
        if isinstance(charged, str):
            # UB2 fix round 1: `outage` (a retry may work) is kept apart from a PERMANENT refusal
            # (`charge_failed`, `no_payment_account`); the refusals carry the difference so the
            # caller can hand it to the clinic.
            reason = charged if charged in ("no_payment_account", "outage") else "charge_failed"
            logger.info("price_requote_blocked", reason=reason, **log)
            return ServiceChangeResult(
                kind="blocked",
                reason=reason,
                decision="increase_charge",
                diff_cents=decision.diff_cents,
            )
        session.add(
            DepositAdjustment(
                id=adjustment_id,
                tenant_id=tenant.id,
                deposit_id=locked.id,
                appointment_id=appointment.id,
                kind=KIND_INCREASE,
                mode=MODE_APPLIED,
                status=STATUS_AWAITING_PAYMENT,
                old_service=service,
                new_service=service,
                pending_edit=_price_change_edit(
                    appointment, held, held + decision.diff_cents
                ),
                diff_cents=decision.diff_cents,
                provider_charge_id=charged.provider_charge_id,
                expires_at=expires_at,
            )
        )
        await session.flush()
        logger.info("price_requote_charged", adjustment_id=str(adjustment_id), **log)
        return ServiceChangeResult(
            kind="applied_awaiting_payment",
            diff_cents=decision.diff_cents,
            pix_copy_paste=charged.copy_paste,
            adjustment_id=adjustment_id,
            expires_at=expires_at,
            decision="increase_charge",
        )

    if decision.action in ("decrease_ask", "decrease_keep"):
        asks = decision.action == "decrease_ask"
        credited = await _outstanding_abatement_cents(session, locked)
        adjustment = DepositAdjustment(
            id=uuid4(),
            tenant_id=tenant.id,
            deposit_id=locked.id,
            appointment_id=appointment.id,
            kind=KIND_DECREASE,
            status=STATUS_ASKED if asks else STATUS_CLINIC_KEPT,
            old_service=service,
            new_service=service,
            pending_edit=_price_change_edit(
                appointment, held, held - credited - decision.diff_cents
            ),
            diff_cents=decision.diff_cents,
            asked_at=now if asks else None,
            resolved_at=None if asks else now,
        )
        session.add(adjustment)
        await session.flush()
        logger.info("price_requote_decrease", decision=decision.action, **log)
        return ServiceChangeResult(
            kind="asked" if asks else "applied_now",
            diff_cents=decision.diff_cents,
            adjustment_id=adjustment.id,
            decision=decision.action,
        )

    logger.info("price_requote_unchanged", decision=decision.action, **log)
    return ServiceChangeResult(
        kind="applied_now",
        diff_cents=decision.diff_cents,
        decision=decision.action,
        reason=decision.reason,  # UB2 fix round 1: "already_abated" on a netted drop
    )


def _pix_ttl_minutes(tenant: Tenant) -> int:
    return tenant.deposit_hold_ttl_minutes or DEFAULT_PIX_TTL_MINUTES


@dataclass(frozen=True)
class _Charged:
    copy_paste: str
    provider_charge_id: str


async def _create_difference_charge(
    session: AsyncSession,
    *,
    tenant: Tenant,
    deposit: PixDeposit,
    adjustment_id: UUID,
    diff_cents: int,
    shown: str,
    now: datetime,
    expires_at: datetime,
    log: dict,
) -> _Charged | str:
    """Create the Pix of ONLY the difference (`external_reference` = the adjustment id). Returns
    the charge, or why not: `no_payment_account`, `charge_failed` (a permanent refusal, or a
    charge without its code - voided), `outage` (the provider is unavailable: a retry may work).
    Never writes an adjustment."""
    provider_name = deposit.provider
    provider = registry.find_provider(provider_name)
    account = await accounts.load_charge_account(
        session, tenant_id=tenant.id, provider=provider_name, timing="after"
    )
    patient = await session.get(Patient, deposit.patient_id) if deposit.patient_id else None
    if provider is None or account is None or patient is None or patient.tenant_id != tenant.id:
        logger.warning("service_change_no_payment_account", **log)
        return "no_payment_account"
    customer_id = await accounts.get_customer_id(
        session, tenant_id=tenant.id, patient_id=patient.id, provider=provider_name
    )
    description = f"Diferença do sinal - {shown} - {tenant.clinic_name}"
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
        return "outage" if outage else "charge_failed"
    if not created.copy_paste:
        # The patient must SEE what they pay: a charge without its code is voided, not kept.
        try:
            await provider.cancel_charge(account, created.provider_charge_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("service_change_void_failed", error_type=type(exc).__name__, **log)
        logger.warning("service_change_charge_without_code", **log)
        return "charge_failed"
    if created.provider_customer_id and created.provider_customer_id != customer_id:
        await accounts.remember_customer_id(
            session,
            tenant_id=tenant.id,
            patient_id=patient.id,
            provider=provider_name,
            provider_customer_id=created.provider_customer_id,
        )
    return _Charged(copy_paste=created.copy_paste, provider_charge_id=created.provider_charge_id)


async def _void_charge(
    session: AsyncSession, tenant: Tenant, deposit: PixDeposit, charge_id: str, log: dict
) -> bool:
    """Best-effort void of a charge the patient never saw. True when voided."""
    return await _cancel_at_provider(session, tenant.id, deposit.provider, charge_id, log)


async def _cancel_at_provider(
    session: AsyncSession, tenant_id: UUID, provider_name: str, charge_id: str, log: dict
) -> bool:
    """`cancel_charge` with the clinic's own account; reads only. Never raises."""
    try:
        provider = registry.find_provider(provider_name)
        account = await cfg.get_payment_account(session, tenant_id, provider_name)
        if provider is None or account is None:
            logger.warning("service_change_void_failed", error_type="no_account", **log)
            return False
        await provider.cancel_charge(account, charge_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("service_change_void_failed", error_type=type(exc).__name__, **log)
        return False
    return True


# --------------------------------------------------------------------------
# TASK-085 R (S7a): the Pix of a change whose edit is rolled back (never sent to the patient)
# --------------------------------------------------------------------------

VoidOutcome = Literal["ok", "failed"]
# Results whose transaction created a Pix of the difference (`_charge_difference`).
CHARGED_KINDS: frozenset[str] = frozenset(
    {"applied_awaiting_payment", "suspended", "awaiting_payment"}
)


@dataclass(frozen=True)
class UnseenCharge:
    """A difference Pix created in a transaction that is about to be ROLLED BACK - the patient
    never saw it (every Pix message goes out after the commit). The caller reads it before the
    rollback (`unseen_charge_of`: the adjustment row disappears with it), voids it at the
    provider (`void_unseen_charge`) and, when that fails, opens the clinic's item AFTER the
    rollback (`open_unvoided_charge_item`). Ids, amounts, dates and the label only."""

    tenant_id: UUID
    provider: str
    charge_id: str
    adjustment_id: UUID
    patient_id: UUID | None
    amount_cents: int
    service_label: str | None = None
    slot_start: datetime | None = None
    slot_end: datetime | None = None


async def unseen_charge_of(
    session: AsyncSession, *, tenant: Tenant, deposit: PixDeposit, result: ServiceChangeResult
) -> UnseenCharge | None:
    """The Pix `result` created in THIS transaction (None for a result without one). Reads only;
    tenant-scoped (an adjustment of another clinic is never returned)."""
    if result.kind not in CHARGED_KINDS or result.adjustment_id is None:
        return None
    adjustment = await session.get(DepositAdjustment, result.adjustment_id)
    if (
        adjustment is None
        or adjustment.tenant_id != tenant.id
        or not adjustment.provider_charge_id
    ):
        return None
    data = adjustment.pending_edit or {}
    try:
        start = datetime.fromisoformat(data["start_at"]) if data.get("start_at") else None
        end = datetime.fromisoformat(data["end_at"]) if data.get("end_at") else None
    except (TypeError, ValueError):
        start = end = None
    return UnseenCharge(
        tenant_id=tenant.id,
        provider=deposit.provider,
        charge_id=adjustment.provider_charge_id,
        adjustment_id=adjustment.id,
        patient_id=deposit.patient_id,
        amount_cents=int(adjustment.diff_cents),
        service_label=adjustment.new_service,
        slot_start=start,
        slot_end=end,
    )


async def void_unseen_charge(
    session: AsyncSession, *, tenant_id: UUID, charge: UnseenCharge
) -> VoidOutcome:
    """Cancel `charge` at the provider with the clinic's own account. Provider only: reads the
    account, writes NOTHING in the session (the caller's transaction is about to roll back, or
    already did - the tenant is passed by id, so an expired row is never read). "failed" (never
    raises) = the charge may still be payable: the caller opens the clinic's item AFTER its
    rollback (`open_unvoided_charge_item`)."""
    log = {"tenant_id": str(tenant_id), "adjustment_id": str(charge.adjustment_id)}
    if charge.tenant_id != tenant_id:
        logger.warning("service_change_void_failed", error_type="other_tenant", **log)
        return "failed"
    if await _cancel_at_provider(session, tenant_id, charge.provider, charge.charge_id, log):
        logger.info("service_change_unseen_charge_voided", **log)
        return "ok"
    return "failed"


async def open_unvoided_charge_item(session: AsyncSession, charge: UnseenCharge) -> None:
    """The ONE `unseen_charge_to_void` item of a charge `void_unseen_charge` could not cancel:
    the clinic cancels it in the provider's panel (the patient never got its code). Call it
    AFTER the rollback of the transaction that created it, in a live transaction; does not
    commit. Not linked to the sinal (`deposit_id` NULL: it is not money held). A retry dedupes."""
    await attention.open_attention_item(
        session,
        tenant_id=charge.tenant_id,
        reason=REASON_UNSEEN_CHARGE_TO_VOID,
        dedupe_key=attention.attention_dedupe_key(
            charge.provider, charge.charge_id, REASON_UNSEEN_CHARGE_TO_VOID
        ),
        provider=charge.provider,
        provider_charge_id=charge.charge_id,
        patient_id=charge.patient_id,
        amount_cents=charge.amount_cents,
        slot_start=charge.slot_start,
        slot_end=charge.slot_end,
        service_label=charge.service_label[:120] if charge.service_label else None,
    )
    logger.warning(
        "service_change_unseen_charge_item_opened",
        tenant_id=str(charge.tenant_id),
        adjustment_id=str(charge.adjustment_id),
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
    label: str | None = None,
    mode: str | None = None,
    expires_at: datetime | None = None,
) -> ServiceChangeResult:
    """`new_service` is what is written; `label` (TASK-085: a unit/doctor/convênio change) is what
    the patient and the clinic read in its place - the Pix, the adjustment, the notices.

    `mode` (TASK-085 R): None = the legacy deferred mode (`awaiting_payment`: nothing written, no
    hold); `suspend` = D-R4 (nothing written; the new window HELD until `expires_at` + grace -
    the caller already checked it under the agenda lock); `applied` = D-R5 (the edit is written
    NOW, after the charge exists - a refusal writes nothing)."""
    log = {"tenant_id": str(tenant.id), "appointment_id": str(appointment.id)}
    shown = label or new_service
    adjustment_id = uuid4()
    if expires_at is None:
        expires_at = as_utc(now) + timedelta(minutes=_pix_ttl_minutes(tenant))
    charged = await _create_difference_charge(
        session, tenant=tenant, deposit=deposit, adjustment_id=adjustment_id,
        diff_cents=diff_cents, shown=shown, now=now, expires_at=expires_at, log=log,
    )
    if charged == "outage":
        # The provider is down: the clinic collects (follow-up B). The same id is kept - a
        # timeout may come after the charge was created with it as the reference.
        return await _apply_and_collect(
            session, tenant=tenant, appointment=appointment, deposit=deposit,
            old_service=old_service, new_service=new_service, diff_cents=diff_cents,
            pending_edit=pending_edit, now=now, source=source, adjustment_id=adjustment_id,
            label=label,
        )
    if isinstance(charged, str):
        # A refusal that will happen again (key, validation, unknown): never waived.
        return ServiceChangeResult(
            kind="blocked", reason=charged, decision="increase_charge", label=label
        )

    adjustment = DepositAdjustment(
        id=adjustment_id,
        tenant_id=tenant.id,
        deposit_id=deposit.id,
        appointment_id=appointment.id,
        kind=KIND_INCREASE,
        mode=mode,
        status=STATUS_AWAITING_PAYMENT,
        old_service=old_service[:120],
        new_service=shown[:120],
        pending_edit={
            **serialize_edit(pending_edit, appointment, source=source),
            "appointment_type": new_service,
        },
        diff_cents=diff_cents,
        provider_charge_id=charged.provider_charge_id,
        expires_at=expires_at,
    )
    session.add(adjustment)
    await session.flush()
    result = {
        "diff_cents": diff_cents,
        "pix_copy_paste": charged.copy_paste,
        "adjustment_id": adjustment_id,
        "expires_at": expires_at,
        "decision": "increase_charge",
        "label": label,
    }

    if mode == MODE_APPLIED:
        edit = deserialize_edit(adjustment.pending_edit)
        written = await _write(
            session, tenant, appointment, edit, source=source,
            key=f"service_change:{appointment.id}:{adjustment_id}",
        )
        logger.info(
            "service_change_applied_awaiting_payment", adjustment_id=str(adjustment_id), **log
        )
        return ServiceChangeResult(
            kind="applied_awaiting_payment",
            changed_fields=written.fields,
            notice_id=written.notice_id,
            **result,
        )

    if mode == MODE_SUSPEND:
        new_start, new_end = _pending_window(appointment, pending_edit)
        data = adjustment.pending_edit
        held = await booking_hold.place_difference_hold_in(
            session,
            tenant_id=tenant.id,
            adjustment_id=adjustment_id,
            patient_id=deposit.patient_id,
            professional_id=_uuid_or_none(data.get("professional_id")),
            unit_id=_unit_of_change(appointment, pending_edit),
            appointment_type=new_service[:120],
            insurance=data.get("insurance"),
            attendee_name=data.get("attendee_name"),
            start_at=new_start,
            end_at=new_end,
            expires_at=difference_hold_expiry(expires_at, new_start),
        )
        if held is None:
            # Checked free under the same agenda lock, so this is a defence only: the Pix the
            # patient never saw is voided and the change refused. A late payment of a charge we
            # could not void is an ORPHAN item (the adjustment is no longer awaiting).
            voided = await _void_charge(
                session, tenant, deposit, charged.provider_charge_id, log
            )
            await move_adjustment(
                session, adjustment, from_status=STATUS_AWAITING_PAYMENT,
                to_status=STATUS_CANCELLED if voided else STATUS_CANCELLED_VOID_PENDING,
                now=now,
            )
            logger.warning("service_change_blocked", reason="slot_taken", **log)
            return ServiceChangeResult(
                kind="blocked", reason="slot_taken", decision="increase_charge", label=label
            )
        logger.info("service_change_suspended", adjustment_id=str(adjustment_id), **log)
        return ServiceChangeResult(kind="suspended", **result)

    logger.info("service_change_awaiting_payment", adjustment_id=str(adjustment_id), **log)
    return ServiceChangeResult(kind="awaiting_payment", **result)


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
    label: str | None = None,
) -> ServiceChangeResult:
    """Follow-up B (2026-10-27): the Pix of the difference could not be created because the
    provider was unavailable (`provider_unavailable`). The change stands NOW - the
    `increase_free` way - so the patient never has to ask (or pay) again here; a
    `collect_due` adjustment records it (never money held: `held_deposit_cents` counts only
    PAID differences) and ONE `service_diff_collect_due` item tells the clinic to collect
    `diff_cents` itself.

    The item is not linked to the sinal (`deposit_id` NULL): it is money still to RECEIVE, so it
    must not stamp/flag the sinal or show the sinal's payment date. Never commits.
    `label`: see `_charge_difference`."""
    shown = label or new_service
    adjustment = DepositAdjustment(
        id=adjustment_id or uuid4(),
        tenant_id=tenant.id,
        deposit_id=deposit.id,
        appointment_id=appointment.id,
        kind=KIND_INCREASE,
        status=STATUS_COLLECT_DUE,
        old_service=old_service[:120],
        new_service=shown[:120],
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
        service_label=shown,
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
        label=label,
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
    if adjustment.mode is not None:
        # TASK-085 R: a `mode` row carries the WHOLE edit (service, doctor, convênio, unit, time).
        return await _apply_full_edit(session, tenant, adjustment, appointment, now=now)
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
    # Follow-up E1: the unit picked for the new service (absent = unchanged, older payloads).
    # Final review: only onto the unit the change was asked for - a unit staff moved the
    # booking to meanwhile is kept (an edit made meanwhile is never undone).
    if "unit_id" in data:
        unit_as_asked = "unit_id" not in expected or _str_or_none(
            appointment.unit_id
        ) == expected.get("unit_id")
        if unit_as_asked:
            edit["unit_id"] = _uuid_or_none(data.get("unit_id"))
        elif edit["appointment_type"] == appointment.appointment_type and _str_or_none(
            appointment.unit_id
        ) != _str_or_none(data.get("unit_id")):
            # TASK-085 final review: a change of the UNIT only whose unit moved meanwhile has
            # nothing left to apply - never "Sua consulta agora é ... na Unidade X".
            logger.warning("service_change_not_applicable", cause="unit", **log)
            return None
    written = await _write(
        session, tenant, appointment, edit,
        source=data.get("source") or SOURCE_SYSTEM,
        key=f"service_change:{appointment.id}:{adjustment.id}",
    )
    # The writer re-resolves the plan ids from the convênio text: keep the row's own ones.
    appointment.insurance_plan_id, appointment.insurance_professional_plan_id = plan_ids
    if time_changed and _patient_asked(data):
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


def _patient_asked(pending_edit: dict) -> bool:
    """TASK-085 R (S7a): only a move the PATIENT asked for spends the sinal's reschedule allowance
    (`deposit_lifecycle.register_reschedule`); a move the clinic made on its agenda
    (`source == SOURCE_HUB`) never does - the hub's own edit/reschedule routes do not count
    either."""
    return pending_edit.get("source") != SOURCE_HUB


def _row_as_requested(appointment: Appointment, expected: dict) -> bool:
    """TASK-085 R: the row is exactly the one the change was asked against (start, service,
    doctor, event, and the unit / convênio when recorded). A suspended visit cannot be edited by
    anybody (S3/S4 guards), so any difference means the change no longer applies."""
    if _iso(appointment.start_at) != expected.get("start_at"):
        return False
    if appointment.appointment_type != expected.get("appointment_type"):
        return False
    if _str_or_none(appointment.professional_id) != expected.get("professional_id"):
        return False
    if "google_event_id" in expected and appointment.google_event_id != expected.get(
        "google_event_id"
    ):
        return False
    if "unit_id" in expected and _str_or_none(
        getattr(appointment, "unit_id", None)
    ) != expected.get("unit_id"):
        return False
    return "insurance" not in expected or _same_text(
        appointment.insurance, expected.get("insurance")
    )


async def _apply_full_edit(
    session: AsyncSession,
    tenant: Tenant,
    adjustment: DepositAdjustment,
    appointment: Appointment | None,
    *,
    now: datetime,
) -> tuple[str, ...] | None:
    """TASK-085 R (`mode="suspend"`, D-R4): the Pix of the difference was paid - the WHOLE pending
    edit is written now (service, doctor, convênio + its plans re-resolved, unit, time) onto the
    row it was asked against, on the agenda of the doctor it lands on (`_window_fits` excluding
    the change's own hold), and that hold is released. The Google event follows after the commit
    (`update_agenda_after_payment`: a change of doctor creates it on the new agenda first), so the
    row keeps its event id/link/source here. None = not applicable (nothing written; the caller
    opens the ORPHAN item and releases the hold). An `applied` row never gets here: it was written
    at the request."""
    data = adjustment.pending_edit or {}
    expected = data.get("expected") or {}
    log = {"tenant_id": str(tenant.id), "adjustment_id": str(adjustment.id)}
    if adjustment.mode != MODE_SUSPEND:
        logger.warning("service_change_not_applicable", cause="mode", **log)
        return None
    if (
        appointment is None
        or not is_live_status(appointment.status)
        or appointment.start_at is None
        or as_utc(appointment.start_at) <= as_utc(now)
        or not _row_as_requested(appointment, expected)
    ):
        logger.warning("service_change_not_applicable", cause="row", **log)
        return None
    try:
        start, end = _pending_window(appointment, data)
    except (TypeError, ValueError):
        start = end = None
    # Contrato §4: appointment -> adjustment (the caller) -> the NEW agenda, then the window check.
    await booking_hold.serialize_agenda_in(
        session, tenant.id, _uuid_or_none(data.get("professional_id"))
    )
    if (
        start is None
        or end <= start
        or not await _window_fits(
            session,
            tenant,
            appointment,
            start,
            end,
            professional_id=data.get("professional_id"),
            exclude_adjustment_id=adjustment.id,
        )
    ):
        logger.warning("service_change_not_applicable", cause="window", **log)
        return None
    edit = deserialize_edit(data)
    edit["start_at"], edit["end_at"] = start, end
    edit["calendar_changed"] = False
    edit["google_event_id"] = appointment.google_event_id
    edit["google_event_link"] = appointment.google_event_link
    written = await _write(
        session, tenant, appointment, edit,
        source=data.get("source") or SOURCE_SYSTEM,
        key=f"service_change:{appointment.id}:{adjustment.id}",
    )
    if edit["time_changed"] and _patient_asked(data):
        # The time moves only now (the change waited for the Pix): it counts now. A limit
        # reached meanwhile never undoes a paid change - logged, like flow_runner's race.
        allowed, _count = await register_reschedule(
            session, tenant=tenant, appointment=appointment
        )
        if not allowed:
            logger.warning("service_change_reschedule_limit_race", **log)
    await booking_hold.release_difference_hold_in(session, adjustment.id)
    logger.info("service_change_paid_applied", mode=adjustment.mode, **log)
    return written.fields


async def _reload_after_rollback(session: AsyncSession, tenant: Tenant) -> None:
    """A rollback EXPIRES every row of the session, the tenant the hooks and their caller keep
    using included (reading an expired attribute outside the greenlet = MissingGreenlet). Reload
    it in place; a tenant from another session was never expired. Never raises."""
    try:
        if tenant in session:
            await session.refresh(tenant)
    except Exception as exc:  # noqa: BLE001 - the next step reloads what it needs itself
        logger.warning("service_change_tenant_reload_failed", error_type=type(exc).__name__)


async def _run_hooks(
    hooks: list[AdjustmentHook], session: AsyncSession, tenant: Tenant, notice: AdjustmentNotice
) -> None:
    """Each hook on its own (TASK-085 R, S7a): what a hook did is committed before the next one
    runs, and a failing hook rolls back only its own work - the tenant is reloaded, so the next
    hooks (and the caller) never read an expired row. The hooks re-load from the session what they
    need (the notice carries only plain values)."""
    for hook in list(hooks):
        try:
            await hook(session, tenant, notice)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - a notice never undoes the money state
            await session.rollback()
            await _reload_after_rollback(session, tenant)
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
        await _reload_after_rollback(session, tenant)
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
    if adjustment.mode == MODE_APPLIED:
        # TASK-085 R D-R5: the change was written (and followed up) at the request already.
        return
    # TASK-085 R D-R4: a suspended visit was hidden from the patient (and its reminders) until
    # now - its reminders are re-planned even when the time did not move.
    suspended = adjustment.mode == MODE_SUSPEND
    data = adjustment.pending_edit or {}
    moved = bool(data.get("time_changed"))
    if not moved and not suspended:
        return
    appointment = await session.scalar(
        select(Appointment).where(
            Appointment.id == adjustment.appointment_id, Appointment.tenant_id == tenant.id
        )
    )
    if appointment is None or not is_live_status(appointment.status):
        return
    if moved and _iso(appointment.start_at) != _iso(data.get("start_at")):
        # A later reschedule won (`_paid_window`): THIS change did not move the time, so it
        # neither re-plans nor announces a move (the reschedule did its own).
        if not suspended:
            return
        moved = False
    tenant_id, appointment_id, new_start = tenant.id, appointment.id, appointment.start_at
    replan = (
        suspended
        or reminder_hooks.enabled_for(tenant)
        or (appointment.confirmation_count or 0) > 0
    )
    previous_raw = (data.get("expected") or {}).get("start_at")
    previous_start = datetime.fromisoformat(previous_raw) if previous_raw else None
    # End this session's read transaction before the hook opens its own.
    await session.commit()
    if replan:
        await reminder_hooks.after_appointment_rescheduled(appointment_id)
    if moved and data.get("source") == SOURCE_FLOW:
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

    TASK-085 R 3rd round: first `old_slot.retry_vacates` (old events still to leave the agenda);
    an `awaiting_choice` past its deadline -> `keep_old_on_silence` (the old consultation is
    brought back, or `old_slot_lost` / `restoring`); every `restoring` is tried again
    (`_retry_restoring`); a change closed without its old consultation coming back has its
    leftover restore event discarded once (`_discard_left_restore_events`, fix round 1)."""
    resolved = 0
    # 3rd round (UA1 contract): the old events whose deletion is still pending (or never ran -
    # the post-commit `vacate_old_slot` is best-effort) leave the agenda first. Never raises;
    # not counted as resolved (nothing of the change itself is decided here).
    await old_slot.retry_vacates(session, now=now, limit=limit)
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
            expired = await _expire_one(session, adjustment_id, now, enqueue)
        except Exception as exc:  # noqa: BLE001 - one adjustment never stops the batch
            await session.rollback()
            logger.warning("service_change_expiry_failed", error_type=type(exc).__name__)
            untouched += 1
            continue
        if expired is None:
            untouched += 1
            continue
        resolved += 1
        tenant, outcome = expired
        if outcome is None:
            await run_expired_hooks(session, tenant, adjustment_id)
        else:
            await run_unpaid_followup(session, tenant, adjustment_id, outcome, now=now)
        await session.commit()
    # Rows left as they were (provider down, account disconnected, a paid one handed to the
    # webhook path) move the window on, so they never keep a healthy row out of the batch.
    _SWEEP_OFFSET = offset + untouched if len(due) == limit else 0
    resolved += await _keep_old_after_silence(session, now=now, limit=limit)
    resolved += await _retry_restoring(session, now=now, limit=limit)
    await _discard_left_restore_events(session, now=now, limit=limit)
    resolved += await _retry_pending_voids(session, now=now, enqueue=enqueue, limit=limit)
    return resolved


async def _keep_old_after_silence(session: AsyncSession, *, now: datetime, limit: int) -> int:
    """TASK-085 R D-R8: every `awaiting_choice` past its deadline -> `kept_old` (the old visit
    stands, the new window is freed) and the patient is told once. Commits per row."""
    overdue = list(
        (
            await session.scalars(
                select(DepositAdjustment.id)
                .where(
                    DepositAdjustment.status == STATUS_AWAITING_CHOICE,
                    DepositAdjustment.expires_at.is_not(None),
                    DepositAdjustment.expires_at <= now,
                )
                .order_by(DepositAdjustment.expires_at, DepositAdjustment.id)
                .limit(limit)
            )
        ).all()
    )
    done = 0
    for adjustment_id in overdue:
        try:
            state = await keep_old_on_silence(session, adjustment_id, now=now)
        except Exception as exc:  # noqa: BLE001 - one row never stops the batch
            await session.rollback()
            logger.warning("service_change_choice_silence_failed", error_type=type(exc).__name__)
            continue
        if state is not None:
            done += 1
    return done


# Fix round 1 (review M1): how long after a suspended change closed the sweep still checks that
# its restore event is gone (the post-commit fast path may have died with its process).
LEFT_RESTORE_EVENT_WINDOW = timedelta(days=7)
# The closed states a restore event must not outlive (`kept_old` keeps it - it IS the
# consultation's event; `rebooked` went through `old_slot.ensure_old_event_gone`).
_CLOSED_WITHOUT_RESTORE = (STATUS_CANCELLED, STATUS_EXPIRED)


async def _discard_left_restore_events(
    session: AsyncSession, *, now: datetime, limit: int
) -> int:
    """Fix round 1 (review M1): every suspended change closed without bringing the old
    consultation back ("Cancelar ambas", a cancellation, "Reenviar o Pix") whose old event had left
    the agenda (`old_slot='released'`) gets `old_slot.discard_restore_event` ONCE from the sweep
    - so a `difkeep` left by a failed restore never outlives its change even when the post-commit
    task (`adjustment_queries.discard_restore_after_commit`) died with its process. Done once per
    change: `old_slot_at` is then set to the change's `resolved_at` (the candidates are those with
    `old_slot_at` before it). Idempotent (the helper reads before deleting); never raises.
    Returns how many were checked."""
    try:
        rows = (
            await session.execute(
                select(DepositAdjustment.id, DepositAdjustment.tenant_id)
                .where(
                    DepositAdjustment.mode == MODE_SUSPEND,
                    DepositAdjustment.status.in_(_CLOSED_WITHOUT_RESTORE),
                    DepositAdjustment.old_slot == OLD_SLOT_RELEASED,
                    DepositAdjustment.resolved_at.is_not(None),
                    DepositAdjustment.resolved_at >= now - LEFT_RESTORE_EVENT_WINDOW,
                    or_(
                        DepositAdjustment.old_slot_at.is_(None),
                        DepositAdjustment.old_slot_at < DepositAdjustment.resolved_at,
                    ),
                )
                .order_by(DepositAdjustment.resolved_at, DepositAdjustment.id)
                .limit(limit)
            )
        ).all()
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - the next tick tries again
        await session.rollback()
        logger.warning("service_change_left_restore_query_failed", error_type=type(exc).__name__)
        return 0
    checked = 0
    for adjustment_id, tenant_id in rows:
        await old_slot.discard_restore_event(tenant_id, adjustment_id)  # never raises
        try:
            await session.execute(
                update(DepositAdjustment)
                .where(
                    DepositAdjustment.id == adjustment_id,
                    DepositAdjustment.tenant_id == tenant_id,
                    DepositAdjustment.status.in_(_CLOSED_WITHOUT_RESTORE),
                    DepositAdjustment.old_slot == OLD_SLOT_RELEASED,
                )
                .values(old_slot_at=DepositAdjustment.resolved_at)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            checked += 1
        except Exception as exc:  # noqa: BLE001 - checked again on the next tick
            await session.rollback()
            logger.warning(
                "service_change_left_restore_mark_failed", error_type=type(exc).__name__
            )
    return checked


async def _retry_restoring(session: AsyncSession, *, now: datetime, limit: int) -> int:
    """3rd round (mapa-u §1.2): every `restoring` change (its old consultation could not come
    back yet - the agenda did not answer) is tried again on each tick
    (`restore_and_tell(trigger="no_room")`): back -> `kept_old`; taken, or the old visit less
    than `CHOICE_MIN_LEAD` away -> `old_slot_lost`. Returns how many it ended."""
    rows = (
        await session.execute(
            select(DepositAdjustment.id, DepositAdjustment.tenant_id)
            .where(
                DepositAdjustment.mode == MODE_SUSPEND,
                DepositAdjustment.status == STATUS_RESTORING,
            )
            .order_by(DepositAdjustment.created_at, DepositAdjustment.id)
            .limit(limit)
        )
    ).all()
    await session.commit()
    done = 0
    for adjustment_id, tenant_id in rows:
        try:
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None:
                continue
            state = await restore_and_tell(
                session, tenant, adjustment_id, trigger="no_room", now=now
            )
        except Exception as exc:  # noqa: BLE001 - one row never stops the batch
            await session.rollback()
            logger.warning("service_change_restore_retry_failed", error_type=type(exc).__name__)
            continue
        if state in ("kept_old", "old_slot_lost", "cancelled"):
            done += 1
    return done


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
) -> tuple[Tenant, str | None] | None:
    """One overdue unpaid difference. None = left as it was (the next tick retries). A legacy
    row (`mode` NULL) -> `expired` and `(tenant, None)` (today's notice); a TASK-085 R row ->
    `on_difference_unpaid` and `(tenant, outcome)` (its own notice, `run_unpaid_followup`)."""
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
    if adjustment.mode is not None:
        outcome = await on_difference_unpaid(
            session, tenant=tenant, adjustment=adjustment, now=now
        )
        if outcome in ("payment_in_flight", "void_failed", "not_applicable"):
            await session.rollback()
            return None
        await session.commit()
        logger.info("service_change_unpaid", outcome=outcome, **log)
        return tenant, outcome
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
    return tenant, None


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
        # TASK-083 follow-up: the row's CURRENT unit keeps its lines after a service change.
        **await unit_booking.booking_unit_fields(
            session,
            tenant.id,
            getattr(appointment, "unit_id", None),
            getattr(appointment, "professional_id", None),
        ),
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
    Sends nothing to the patient (the default notice does, once).

    TASK-085 R: a `mode="applied"` change was written - and its agenda moved by its caller - at
    the request: nothing to do. A `mode="suspend"` change of DOCTOR moves the event to the new
    doctor's agenda (`_move_event_to_new_agenda`). 3rd round (D-R10): a `mode="suspend"` change
    whose old event already left the agenda (`old_slot` releasing/released) gets a NEW event
    where it landed (`old_slot.place_new_event_after_payment`; a consultation no longer live gets
    none); a failure opens `service_diff_agenda_pending` (the consultation has no event until the
    clinic puts it - mapa-u risk 3). `old_slot` NULL = today's path."""
    if notice.kind != KIND_INCREASE or notice.appointment_id is None:
        return
    if notice.mode == MODE_APPLIED:
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
    patient_name = patient.name if patient is not None else None
    log = {"tenant_id": str(tenant.id), "adjustment_id": str(adjustment.id)}
    if adjustment.mode == MODE_SUSPEND and adjustment.old_slot is not None:
        # 3rd round (D-R10): the old event already left the agenda (or is leaving it) - a NEW
        # event is created where the change landed (`old_slot.place_new_event_after_payment`);
        # "replaced" = a repeated hook (nothing to do).
        if adjustment.old_slot == OLD_SLOT_REPLACED:
            return
        adjustment_id, appointment_id = adjustment.id, appointment.id
        try:
            if adjustment.old_slot not in (OLD_SLOT_RELEASING, OLD_SLOT_RELEASED):
                raise ValueError("old_slot_unexpected")
            await old_slot.place_new_event_after_payment(
                session,
                tenant=tenant,
                appointment=appointment,
                adjustment=adjustment,
                patient_name=patient_name,
            )
        except Exception as exc:  # noqa: BLE001 - the payment stands; the clinic puts the event
            await session.rollback()
            await _reload_after_rollback(session, tenant)
            logger.warning(
                "service_change_agenda_not_updated", error_type=type(exc).__name__, **log
            )
            await _open_agenda_item(session, tenant, adjustment_id, appointment_id)
            return
        logger.info("service_change_agenda_placed", **log)
        return
    agenda_moves = adjustment.mode == MODE_SUSPEND and _str_or_none(
        appointment.professional_id
    ) != expected.get("professional_id")
    adjustment_id, appointment_id = adjustment.id, appointment.id
    try:
        if agenda_moves:
            await _move_event_to_new_agenda(
                session, tenant, appointment, expected, patient_name=patient_name, log=log
            )
        else:
            calendar = await calendar_for_appointment(session, tenant, appointment)
            if calendar is None:
                raise LookupError("no_calendar")
            await sync_agenda_event(
                session,
                tenant,
                appointment,
                calendar,
                patient_name=patient_name,
                window_changed=window_changed,
            )
    except Exception as exc:  # noqa: BLE001 - the payment stands; the clinic fixes the agenda
        logger.warning(
            "service_change_agenda_not_updated", error_type=type(exc).__name__, **log
        )
        await _open_agenda_item(session, tenant, adjustment_id, appointment_id)
        return
    logger.info(
        "service_change_agenda_updated", window_changed=window_changed, moved=agenda_moves, **log
    )


async def _open_agenda_item(
    session: AsyncSession, tenant: Tenant, adjustment_id: UUID, appointment_id: UUID
) -> None:
    adjustment = await session.get(DepositAdjustment, adjustment_id)
    appointment = await session.get(Appointment, appointment_id)
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
        slot_start=appointment.start_at if appointment is not None else None,
        slot_end=appointment.end_at if appointment is not None else None,
        service_label=adjustment.new_service,
    )
    await session.commit()


async def _move_event_to_new_agenda(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    expected: dict,
    *,
    patient_name: str | None,
    log: dict,
) -> None:
    """TASK-085 R (D-R3/D-R4): a paid change of DOCTOR. The row already carries the new doctor
    (`_apply_full_edit`) but still points at the OLD event. The event is created on the new
    doctor's agenda (free there first; exclusively on the secretarIA agenda), the row re-pointed
    and committed, and only THEN the old event is deleted on the agenda that owned it - so a
    failure never leaves the consultation without an event. Raises on any failure before the
    re-point (the caller opens `service_diff_agenda_pending`); a failure to delete the old event
    opens the same item (the clinic removes it)."""
    old_event_id = (appointment.google_event_id or "").strip()
    old_owner = SimpleNamespace(
        professional_id=_uuid_or_none(expected.get("professional_id")),
        google_calendar_source=appointment.google_calendar_source,
    )
    new_source = "professional" if appointment.professional_id is not None else "clinic"
    new_owner = SimpleNamespace(
        professional_id=appointment.professional_id, google_calendar_source=new_source
    )
    new_calendar = await calendar_for_appointment(session, tenant, new_owner)
    if new_calendar is None:
        raise LookupError("no_new_calendar")
    new_calendar = await unit_booking.calendar_at_unit(
        session, new_calendar, tenant.id, getattr(appointment, "unit_id", None)
    )
    start = as_utc(appointment.start_at)
    end = as_utc(appointment.end_at or appointment.start_at)
    if not await new_calendar.is_slot_free(start, end):
        raise CalendarSlotTakenError("service_change_new_agenda_taken")
    description = build_event_description(
        service=appointment.appointment_type,
        insurance=appointment.insurance,
        attendee_name=appointment.attendee_name,
        **await unit_booking.booking_unit_fields(
            session,
            tenant.id,
            getattr(appointment, "unit_id", None),
            appointment.professional_id,
        ),
    )
    created = await create_event_exclusive(
        new_calendar,
        start=start,
        end=end,
        summary=agenda_summary(appointment, patient_name),
        description=description,
    )
    new_event_id = str(created.get("id") or "").strip()
    if not new_event_id:
        raise LookupError("no_event_id")
    appointment_id = appointment.id
    appointment.google_event_id = new_event_id
    appointment.google_event_link = created.get("htmlLink")
    appointment.google_calendar_source = new_source
    await session.commit()
    if not old_event_id:
        return
    try:
        old_calendar = await calendar_for_appointment(session, tenant, old_owner)
        if old_calendar is None:
            raise LookupError("no_old_calendar")
        await old_calendar.cancel_event(old_event_id)
    except Exception as exc:  # noqa: BLE001 - the new event stands; the clinic removes the old
        logger.warning(
            "service_change_old_event_not_deleted", error_type=type(exc).__name__, **log
        )
        await _open_agenda_item(
            session, tenant, UUID(log["adjustment_id"]), appointment_id
        )


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
    if notice.mode == MODE_APPLIED:
        # TASK-085 R D-R5: the change was made at the request; this only thanks the payment.
        body = adjustment_paid_after_applied_text(notice.new_service, notice.diff_cents)
    else:
        body = adjustment_applied_text(notice.new_service, notice.diff_cents)
    await notify.deliver(session, tenant, patient, [notify.Text(body)])


async def notify_patient_adjustment_not_applied(
    session: AsyncSession, tenant: Tenant, notice: AdjustmentNotice
) -> None:
    patient = await _patient_of(session, tenant, notice)
    if patient is None:
        return
    underpaid = notice.paid_cents is not None and notice.paid_cents < notice.diff_cents
    if notice.mode == MODE_APPLIED:
        # TASK-085 R D-R5: the change already stood - never "a troca não foi feita".
        body = (
            applied_underpaid_text(notice.diff_cents, notice.paid_cents)
            if underpaid
            else applied_orphan_text(notice.diff_cents)
        )
    elif underpaid:
        body = adjustment_underpaid_text(notice.new_service, notice.diff_cents, notice.paid_cents)
    else:
        body = adjustment_not_applied_text(notice.new_service, notice.diff_cents)
    await notify.deliver(session, tenant, patient, [notify.Text(body)])


# --------------------------------------------------------------------------
# TASK-085 R - frozen signatures, implemented by S1 (tasks/TASK-085/sinal-trocas/contrato.md)
# --------------------------------------------------------------------------


async def _lock_appointment_then(
    session: AsyncSession, adjustment: DepositAdjustment
) -> Appointment | None:
    """Contrato §4 lock order: the APPOINTMENT row first (FOR UPDATE), then the adjustment is
    re-read - what the transaction we queued behind decided."""
    appointment = None
    if adjustment.appointment_id is not None:
        appointment = await session.scalar(
            select(Appointment)
            .where(
                Appointment.id == adjustment.appointment_id,
                Appointment.tenant_id == adjustment.tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    await session.scalar(
        select(DepositAdjustment)
        .where(
            DepositAdjustment.id == adjustment.id,
            DepositAdjustment.tenant_id == adjustment.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return appointment


def _visit_upcoming(appointment: Appointment | None, now: datetime) -> bool:
    return (
        appointment is not None
        and is_live_status(appointment.status)
        and appointment.start_at is not None
        and as_utc(appointment.start_at) > as_utc(now)
    )


# The same test under its public name (the webhook decides `restoring` vs `cancelled` with it).
visit_upcoming = _visit_upcoming


async def reopen_as_restoring(
    session: AsyncSession, adjustment: DepositAdjustment, *, from_status: str
) -> bool:
    """3rd round (mapa-u §1.2): a paid `suspend` change that cannot be applied goes from
    `from_status` (`awaiting_payment`, or `applied` once the payment was claimed) to `restoring`
    - still OPEN (`resolved_at` NULL) - and frees the new window's hold; the old consultation is
    brought back after the commit (`restore_and_tell`). Guarded; never commits. False = the row
    was no longer in `from_status` (nothing changed)."""
    claimed = await session.execute(
        update(DepositAdjustment)
        .where(
            DepositAdjustment.id == adjustment.id,
            DepositAdjustment.tenant_id == adjustment.tenant_id,
            DepositAdjustment.mode == MODE_SUSPEND,
            DepositAdjustment.status == from_status,
        )
        .values(status=STATUS_RESTORING, resolved_at=None)
        .returning(DepositAdjustment.id)
        .execution_options(synchronize_session=False)
    )
    won = claimed.scalar_one_or_none() is not None
    await session.refresh(adjustment)
    if won:
        await booking_hold.release_difference_hold_in(session, adjustment.id)
        logger.info(
            "service_change_restoring",
            from_status=from_status,
            tenant_id=str(adjustment.tenant_id),
            adjustment_id=str(adjustment.id),
        )
    return won


def _new_start_of(adjustment: DepositAdjustment, appointment: Appointment) -> datetime:
    return _pending_window(appointment, adjustment.pending_edit or {})[0]


async def _to_awaiting_choice(
    session: AsyncSession,
    adjustment: DepositAdjustment,
    *,
    deadline: datetime,
    now: datetime,
) -> bool:
    """Atomic `awaiting_payment -> awaiting_choice` (still open: `resolved_at` stays NULL)."""
    claimed = await session.execute(
        update(DepositAdjustment)
        .where(
            DepositAdjustment.id == adjustment.id,
            DepositAdjustment.tenant_id == adjustment.tenant_id,
            DepositAdjustment.status == STATUS_AWAITING_PAYMENT,
        )
        .values(status=STATUS_AWAITING_CHOICE, asked_at=now, expires_at=deadline)
        .returning(DepositAdjustment.id)
        .execution_options(synchronize_session=False)
    )
    won = claimed.scalar_one_or_none() is not None
    await session.refresh(adjustment)
    return won


async def on_difference_unpaid(
    session: AsyncSession,
    *,
    tenant: Tenant,
    adjustment: DepositAdjustment,
    now: datetime,
) -> UnpaidOutcome:
    """The Pix of a `mode` change (`suspend` / `applied`) is past `expires_at`, or the provider
    said EXPIRED. Called by the webhook EXPIRED path and by the expiry sweep INSTEAD of today's
    `awaiting_payment -> expired` for a row whose `mode` is not NULL. Never commits.

    `get_charge` first (PAID -> "payment_in_flight", nothing changed), then the charge is voided
    (failure -> "void_failed", nothing changed, the next tick retries). Then, under the lock order
    appointment -> adjustment: `suspend` -> `awaiting_choice` (asked_at=now, expires_at =
    `choice_deadline(now, old_start, new_start)`, hold extended to `difference_hold_expiry`)
    -> "choice_pending" (buttons with "Reenviar" only when `resend_fits`), or,
    with no room for a choice, -> `restoring` (hold released; 3rd round: the old consultation is
    brought back after the commit) -> "restoring"; `applied` ->
    `collect_due` + ONE `service_diff_unpaid` item -> "collect_due"; the consultation gone or
    not live -> `cancelled` (hold released) -> "cancelled". The patient's message (the three
    buttons, the kept notice, the collect notice) is sent by the caller AFTER the commit
    (`run_unpaid_followup`)."""
    if (
        adjustment.tenant_id != tenant.id
        or adjustment.mode is None
        or adjustment.status != STATUS_AWAITING_PAYMENT
    ):
        return "not_applicable"
    log = {"tenant_id": str(tenant.id), "adjustment_id": str(adjustment.id)}
    deposit = await session.get(PixDeposit, adjustment.deposit_id)
    if deposit is None or deposit.tenant_id != tenant.id:
        return "not_applicable"
    charge_id = adjustment.provider_charge_id
    if charge_id:
        provider = registry.find_provider(deposit.provider)
        account = await cfg.get_payment_account(session, tenant.id, deposit.provider)
        if provider is None or account is None:
            logger.warning("service_change_unpaid_no_account", **log)
            return "void_failed"
        try:
            state = await provider.get_charge(account, charge_id)
        except Exception as exc:  # noqa: BLE001 - cannot tell: never decide blind
            logger.warning(
                "service_change_unpaid_state_unknown", error_type=type(exc).__name__, **log
            )
            return "void_failed"
        if state.status is PaymentStatus.PAID:
            return "payment_in_flight"
        try:
            await provider.cancel_charge(account, charge_id)
        except Exception as exc:  # noqa: BLE001 - not voided: retry next tick
            gone = isinstance(exc, ProviderError) and exc.status_code == 404
            if not gone:
                logger.warning(
                    "service_change_unpaid_void_failed", error_type=type(exc).__name__, **log
                )
                return "void_failed"

    appointment = await _lock_appointment_then(session, adjustment)
    if adjustment.status != STATUS_AWAITING_PAYMENT:
        return "not_applicable"
    # Fix round 1 (I2): an `applied` change is only "cancelled" when its consultation is GONE
    # (missing or cancelled); one that already happened (attended, no-show, started) stood, so
    # the clinic is told to collect the difference (D-R5).
    applied_gone = adjustment.mode == MODE_APPLIED and (
        appointment is None or appointment.status == AppointmentStatus.CANCELLED
    )
    suspend_gone = adjustment.mode == MODE_SUSPEND and not _visit_upcoming(appointment, now)
    if applied_gone or suspend_gone:
        if not await move_adjustment(
            session, adjustment, from_status=STATUS_AWAITING_PAYMENT,
            to_status=STATUS_CANCELLED, now=now,
        ):
            return "not_applicable"
        await booking_hold.release_difference_hold_in(session, adjustment.id)
        logger.info("service_change_unpaid_cancelled", **log)
        return "cancelled"

    if adjustment.mode == MODE_APPLIED:
        if not await move_adjustment(
            session, adjustment, from_status=STATUS_AWAITING_PAYMENT,
            to_status=STATUS_COLLECT_DUE, now=now,
        ):
            return "not_applicable"
        # Money still to RECEIVE: not linked to the sinal (like `_apply_and_collect`).
        await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason=REASON_SERVICE_DIFF_UNPAID,
            dedupe_key=adjustment_queries.unpaid_item_key(deposit.provider, adjustment.id),
            provider=deposit.provider,
            patient_id=deposit.patient_id,
            amount_cents=adjustment.diff_cents,
            slot_start=appointment.start_at,
            slot_end=appointment.end_at,
            service_label=adjustment.new_service,
        )
        logger.info("service_change_unpaid_collect_due", **log)
        return "collect_due"

    deadline = choice_deadline(now, appointment.start_at, _new_start_of(adjustment, appointment))
    if deadline is None:
        # 3rd round (D-R10/D-R11): the old consultation left the agenda - it is brought back
        # AFTER the commit (`run_unpaid_followup` -> `old_slot.restore_old_slot(trigger=
        # "no_room")`); `restoring` frees the new window's hold and is retried by the sweep.
        if not await old_slot.to_restoring(
            session, adjustment=adjustment, from_status=STATUS_AWAITING_PAYMENT, now=now
        ):
            return "not_applicable"
        logger.info("service_change_unpaid_restoring", **log)
        return "restoring"
    if not await _to_awaiting_choice(session, adjustment, deadline=deadline, now=now):
        return "not_applicable"
    new_start = _new_start_of(adjustment, appointment)
    await booking_hold.extend_difference_hold_in(
        session, adjustment.id, expires_at=difference_hold_expiry(deadline, new_start)
    )
    logger.info("service_change_unpaid_choice_pending", **log)
    return "choice_pending"


def _now() -> datetime:
    """The engine's clock for the steps that run after a commit without a `now` of their own
    (replaceable in tests)."""
    return datetime.now(UTC)


async def _old_slot_free_for_message(
    session: AsyncSession,
    tenant: Tenant,
    adjustment: DepositAdjustment,
    appointment: Appointment | None,
) -> bool:
    """Whether the choice message offers "Manter a antiga" (True) or "Outro horário" (False).
    `old_slot` NULL = the old event never left the agenda (it is the patient's own); an agenda
    that cannot be read counts as free (mapa-u §1.5: the tap checks again). Needs a session
    without pending writes (`old_slot.old_slot_free` ends the read before the agenda answers)."""
    if adjustment.old_slot is None or appointment is None:
        return True
    try:
        free = await old_slot.old_slot_free(
            session, tenant=tenant, appointment=appointment, adjustment=adjustment
        )
    except Exception as exc:  # noqa: BLE001 - unreadable: offer "Manter", the tap re-checks
        logger.warning("service_change_old_slot_unreadable", error_type=type(exc).__name__)
        free = None
    return True if free is None else bool(free)


async def run_unpaid_followup(
    session: AsyncSession,
    tenant: Tenant,
    adjustment_id: UUID,
    outcome: str,
    *,
    now: datetime | None = None,
) -> None:
    """AFTER the commit that decided an unpaid difference (`on_difference_unpaid`, or the choice
    deadline passing in silence): the patient's ONE message - the three buttons
    ("choice_pending"; "Reenviar o Pix" only while the new window is held and a useful Pix still
    fits; 3rd round: "Outro horário" in the place of "Manter a antiga" when the old slot was taken,
    `old_slot.old_slot_free` - an unreadable agenda reads as free), the kept notice ("kept_old",
    plus `followup_kept_old`), the lost-slot notice with "Outro horário" / "Cancelar consulta"
    ("old_slot_lost") or the collect notice ("collect_due"; `price_unpaid_text` for a price
    change, D-R14). "restoring" (3rd round: no room for a choice) first tries to bring the old
    consultation back (`restore_and_tell(trigger="no_room")`), which sends its own notice.
    Best-effort: a failure is logged, the money state stands."""
    if outcome == "restoring":
        await restore_and_tell(
            session, tenant, adjustment_id, trigger="no_room", now=now or _now()
        )
        return
    tenant_id = tenant.id
    appointment_id = mode = None
    try:
        adjustment = await session.get(DepositAdjustment, adjustment_id)
        if adjustment is None or adjustment.tenant_id != tenant_id:
            return
        # The buttons say what was true when the choice opened (`asked_at`), not later.
        if now is None:
            now = as_utc(adjustment.asked_at) if adjustment.asked_at else _now()
        deposit = await session.get(PixDeposit, adjustment.deposit_id)
        patient = await _patient_of(
            session, tenant, _notice(adjustment, deposit.patient_id if deposit else None)
        )
        appointment_id = adjustment.appointment_id
        mode = adjustment.mode
        message = None
        if outcome == "choice_pending" and adjustment.status == STATUS_AWAITING_CHOICE:
            appointment = (
                await session.get(Appointment, appointment_id) if appointment_id else None
            )
            resend = False
            if appointment is not None:
                held = await booking_hold.difference_hold_in(session, adjustment.id)
                resend = held is not None and resend_fits(
                    now,
                    _pix_ttl_minutes(tenant),
                    appointment.start_at,
                    _new_start_of(adjustment, appointment),
                )
            free = await _old_slot_free_for_message(session, tenant, adjustment, appointment)
            message = notify.Buttons(
                choice_message_text(
                    adjustment.old_service,
                    adjustment.new_service,
                    adjustment.diff_cents,
                    old_slot_free=free,
                    resend=resend,
                ),
                tuple(choice_buttons(adjustment.id, resend=resend, old_slot_free=free)),
            )
        elif outcome == "kept_old":
            message = notify.Text(kept_old_notice_text(adjustment.old_service))
        elif outcome == "old_slot_lost":
            message = notify.Buttons(
                old_slot_lost_text(adjustment.old_service),
                tuple(lost_slot_buttons(adjustment.id)),
            )
        elif outcome == "collect_due":
            if is_price_change(adjustment.pending_edit):
                message = notify.Text(price_unpaid_text(adjustment.diff_cents))
            else:
                message = notify.Text(
                    unpaid_collect_text(adjustment.new_service, adjustment.diff_cents)
                )
        if message is not None and patient is not None:
            await notify.deliver(session, tenant, patient, [message])
    except Exception as exc:  # noqa: BLE001 - the decision stands; only the notice is lost
        await session.rollback()
        await _reload_after_rollback(session, tenant)
        logger.warning("service_change_unpaid_notice_failed", error_type=type(exc).__name__)
        # Fall through: the re-plan of the old visit below does not depend on the notice.
    # The old visit is no longer suspended: re-plan its reminders (fix round 1, M4 - also when a
    # suspended change ended `cancelled`).
    if appointment_id is not None and (
        outcome == "kept_old" or (outcome == "cancelled" and mode == MODE_SUSPEND)
    ):
        await followup_kept_old(tenant_id, appointment_id)


# What `restore_and_tell` ended in. "already_decided": somebody else decided (nothing sent here).
RestoreFollowup = Literal["kept_old", "old_slot_lost", "restoring", "cancelled", "already_decided"]


async def restore_and_tell(
    session: AsyncSession,
    tenant: Tenant,
    adjustment_id: UUID,
    *,
    trigger: old_slot.RestoreTrigger,
    now: datetime,
) -> RestoreFollowup:
    """3rd round (D-R8/D-R11, mapa-u §1.2): a suspended change ended without being applied - by
    silence at the choice deadline ("silence"), with no room left for a choice ("no_room", also
    the sweep's retry of `restoring`), or a payment that could not apply it ("not_applied") -
    bring the old consultation back (`old_slot.restore_old_slot`, which COMMITS) and tell the
    patient. Call it with a session WITHOUT pending writes, after the commit that decided.

    * "restored" -> `kept_old_notice_text` + `followup_kept_old` -> "kept_old";
    * "slot_taken" (another patient took the old time), or the old consultation is less than
      `CHOICE_MIN_LEAD` away (or already started) and still not back ->
      `old_slot.mark_old_slot_lost` (the clinic's `service_diff_rebook_due` item; the paid sinal
      is kept) + `old_slot_lost_text` with "Outro horário" / "Cancelar consulta" (only for a
      visit still ahead) -> "old_slot_lost";
    * "calendar_unavailable" with time left -> `restoring` (from `awaiting_choice`) and the sweep
      tries again on its next tick -> "restoring";
    * the consultation is gone (cancelled meanwhile) -> `cancelled` -> "cancelled".

    Never raises."""
    tenant_id = tenant.id
    try:
        outcome = await old_slot.restore_old_slot(
            session, tenant_id=tenant_id, adjustment_id=adjustment_id, now=now, trigger=trigger
        )
        # The restore may have rolled this session back (every row expired): reload the tenant.
        await _reload_after_rollback(session, tenant)
        if outcome == "restored":
            await run_unpaid_followup(session, tenant, adjustment_id, "kept_old", now=now)
            return "kept_old"
        if outcome == "already_decided":
            return "already_decided"
        state, tell = await _after_failed_restore(
            session, tenant, adjustment_id, outcome=outcome, now=now
        )
    except Exception as exc:  # noqa: BLE001 - the next sweep tick tries again
        await session.rollback()
        await _reload_after_rollback(session, tenant)
        logger.warning(
            "service_change_restore_followup_failed",
            error_type=type(exc).__name__,
            tenant_id=str(tenant_id),
            adjustment_id=str(adjustment_id),
        )
        return "restoring"
    if state == "old_slot_lost" and tell:
        await run_unpaid_followup(session, tenant, adjustment_id, "old_slot_lost", now=now)
    if state == "cancelled":
        # A `difkeep` kept by a restore that failed after creating it must not outlive the
        # change (UA1 delta 10).
        await old_slot.discard_restore_event(tenant_id, adjustment_id)
    logger.info(
        "service_change_restore_followup",
        trigger=trigger,
        restore=outcome,
        state=state,
        tenant_id=str(tenant_id),
        adjustment_id=str(adjustment_id),
    )
    return state


async def _after_failed_restore(
    session: AsyncSession,
    tenant: Tenant,
    adjustment_id: UUID,
    *,
    outcome: str,
    now: datetime,
) -> tuple[RestoreFollowup, bool]:
    """The old consultation did not come back (`outcome` of `restore_old_slot`): decide what the
    change becomes, under the lock order appointment -> adjustment. COMMITS. Returns the state
    and whether the patient is told (a lost slot of a visit still ahead)."""
    adjustment = await session.get(DepositAdjustment, adjustment_id)
    if adjustment is None or adjustment.tenant_id != tenant.id:
        return "already_decided", False
    appointment = await _lock_appointment_then(session, adjustment)
    status = adjustment.status
    if adjustment.mode != MODE_SUSPEND or status not in RESTORABLE_STATUSES:
        await session.rollback()
        return "already_decided", False
    if appointment is None or not is_live_status(appointment.status):
        # Cancelled meanwhile (the cancellation's own settle missed it): the change ends with it.
        won = await move_adjustment(
            session, adjustment, from_status=status, to_status=STATUS_CANCELLED, now=now
        )
        if won:
            await booking_hold.release_difference_hold_in(session, adjustment.id)
        await session.commit()
        return ("cancelled" if won else "already_decided"), False
    upcoming = _visit_upcoming(appointment, now)
    too_close = not upcoming or as_utc(now) >= as_utc(appointment.start_at) - CHOICE_MIN_LEAD
    if outcome in ("slot_taken", "not_applicable") or too_close:
        won = await old_slot.mark_old_slot_lost(
            session, tenant=tenant, adjustment=adjustment, now=now
        )
        await session.commit()
        return ("old_slot_lost" if won else "already_decided"), won and upcoming
    if status == STATUS_AWAITING_CHOICE:
        won = await old_slot.to_restoring(
            session, adjustment=adjustment, from_status=status, now=now
        )
        await session.commit()
        return ("restoring" if won else "already_decided"), False
    await session.rollback()
    return "restoring", False


async def keep_old_on_silence(
    session: AsyncSession, adjustment_id: UUID, *, now: datetime
) -> RestoreFollowup | None:
    """D-R8/D-R11: an `awaiting_choice` whose deadline passed with no answer -> the engine tries to
    bring the old consultation back (`restore_and_tell(trigger="silence")`: back -> `kept_old`;
    its time taken -> `old_slot_lost`; the agenda down -> `restoring`, retried). Commits (the sweep
    owns this session). Returns what THIS call decided, None when it decided nothing."""
    adjustment = await session.get(DepositAdjustment, adjustment_id)
    if adjustment is None or adjustment.status != STATUS_AWAITING_CHOICE:
        return None
    expires = adjustment.expires_at
    if expires is None or as_utc(expires) > as_utc(now):
        return None
    tenant = await session.get(Tenant, adjustment.tenant_id)
    if tenant is None:
        return None
    await session.commit()  # end the read: the restore opens its own transactions
    state = await restore_and_tell(session, tenant, adjustment_id, trigger="silence", now=now)
    if state == "already_decided":
        return None
    logger.info(
        "service_change_choice_silence",
        state=state,
        tenant_id=str(tenant.id),
        adjustment_id=str(adjustment_id),
    )
    return state


async def choose_after_expiry(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    adjustment_id: UUID,
    choice: Choice,
    now: datetime,
) -> ChoiceOutcome:
    """The patient's answer to the buttons (D-R4/D-R11). Never commits; idempotent (a second tap
    is "already_decided"). Scoped by `tenant_id` only - the caller checks the patient (the
    sinal's `patient_id` against the tapping conversation's). Lock order: the appointment row
    FOR UPDATE, then the adjustment. Outcomes in `ChoiceKind`:

    * "keep" -> ALWAYS "already_decided" (3rd round, mapa-u §4.4): "Manter a antiga" brings the
      old consultation back through `old_slot.restore_old_slot(trigger="tap")` (the caller),
      which COMMITS and can find the old slot taken - nothing is decided here;
    * "resend" (only `awaiting_choice`, `now < expires_at`) -> `resend_fits(now, ttl, old_start,
      new_start)` False -> "no_window"; else the new window must still be held or holdable
      (`_window_fits` excluding its own hold); a NEW adjustment (same `pending_edit`,
      `diff_cents`, mode `suspend`, new charge, `expires_at` = `suspend_pix_expiry(...)`, and the
      old event's state `old_slot`/`old_slot_at`) takes over, the old one -> `expired`, the hold
      is handed over -> "resent" (caller sends `difference_pix_text`); no window -> "no_window";
      the provider fails -> "resend_failed" (both: nothing changed, the caller re-sends the
      buttons);
    * "both" (`awaiting_choice` before the deadline, or `old_slot_lost` - its "Cancelar
      consulta") -> `cancelled`, hold released, the rebook item closed -> "cancel_old"; the
      CALLER then cancels the old consultation as a patient cancellation
      (`workers/shared/actions._execute_appointment_cancel`: the paid sinal's cancellation rules);
    * "other" ("Outro horário": `awaiting_choice` before the deadline, or `old_slot_lost`, which
      has none) -> the new window's hold released, the status UNCHANGED -> "rebook"; the caller
      commits and enters the patient's reschedule of the old consultation (`finish_rebook` closes
      the change when the new time is confirmed).

    3rd round: a change closed here without bringing the old consultation back ("both",
    "resend") has its `difkeep` restore event discarded after the caller's commit
    (`adjustment_queries.discard_restore_after_commit`, UA1 delta 10)."""
    adjustment = await session.scalar(
        select(DepositAdjustment).where(
            DepositAdjustment.id == adjustment_id, DepositAdjustment.tenant_id == tenant_id
        )
    )
    if adjustment is None or adjustment.mode != MODE_SUSPEND or adjustment.kind != KIND_INCREASE:
        return ChoiceOutcome(kind="not_found")
    base = {
        "adjustment_id": adjustment.id,
        "appointment_id": adjustment.appointment_id,
        "diff_cents": adjustment.diff_cents,
        "old_service": adjustment.old_service,
        "new_service": adjustment.new_service,
    }
    if choice == CHOICE_KEEP:
        return ChoiceOutcome(kind="already_decided", **base)
    appointment = await _lock_appointment_then(session, adjustment)
    expires = adjustment.expires_at
    choosing = (
        adjustment.status == STATUS_AWAITING_CHOICE
        and expires is not None
        and as_utc(now) < as_utc(expires)
    )
    lost = adjustment.status == STATUS_OLD_SLOT_LOST
    log = {"tenant_id": str(tenant_id), "adjustment_id": str(adjustment.id)}

    if choice == CHOICE_OTHER:
        if not (choosing or lost) or adjustment.status not in REBOOKABLE_STATUSES:
            return ChoiceOutcome(kind="already_decided", **base)
        # Fix round 1 (review M3, mapa-u §7 item 4): a lost slot stays rebookable until the clinic
        # resolves it - even past the old start (it may be lost less than an hour before); a
        # choice still needs its visit ahead.
        alive = (
            appointment is not None and is_live_status(appointment.status)
            if lost
            else _visit_upcoming(appointment, now)
        )
        if not alive:
            return ChoiceOutcome(kind="already_decided", **base)
        # The change is abandoned: the new window is nobody's any more ("Reenviar o Pix" is
        # gone with it). The status stays - `finish_rebook` closes it at the new time.
        await booking_hold.release_difference_hold_in(session, adjustment.id)
        logger.info("service_change_choice", choice=choice, **log)
        return ChoiceOutcome(kind="rebook", **base)

    if choice == CHOICE_BOTH:
        if not (choosing or lost):
            return ChoiceOutcome(kind="already_decided", **base)
        from_status = adjustment.status
        if not await move_adjustment(
            session, adjustment, from_status=from_status, to_status=STATUS_CANCELLED, now=now
        ):
            return ChoiceOutcome(kind="already_decided", **base)
        await booking_hold.release_difference_hold_in(session, adjustment.id)
        if from_status == STATUS_OLD_SLOT_LOST:
            await _close_lost_item(session, adjustment, now=now)
        adjustment_queries.discard_restore_after_commit(session, adjustment)
        logger.info("service_change_choice", choice=choice, from_status=from_status, **log)
        return ChoiceOutcome(kind="cancel_old", **base)

    if (
        choice != CHOICE_RESEND
        or not choosing
        or appointment is None
        or not _visit_upcoming(appointment, now)
    ):
        return ChoiceOutcome(kind="already_decided", **base)
    return await _resend(session, adjustment, appointment, now=now, base=base, log=log)


async def _close_lost_item(
    session: AsyncSession, adjustment: DepositAdjustment, *, now: datetime
) -> None:
    """"Cancelar consulta" of an `old_slot_lost` change: its rebook item is closed (the same
    rule as a cancellation, `adjustment_queries.settle_open_adjustments`)."""
    deposit = await session.get(PixDeposit, adjustment.deposit_id)
    tenant = await session.get(Tenant, adjustment.tenant_id)
    if deposit is None or tenant is None or deposit.tenant_id != tenant.id:
        return
    await adjustment_queries.close_rebook_item(session, tenant, deposit, adjustment, now=now)


async def _resend(
    session: AsyncSession,
    adjustment: DepositAdjustment,
    appointment: Appointment,
    *,
    now: datetime,
    base: dict,
    log: dict,
) -> ChoiceOutcome:
    """"Reenviar o Pix": the SAME change (`pending_edit`, `diff_cents` - never re-priced) under a
    NEW adjustment with a new Pix; the hold handed over; the old adjustment `expired` (a late
    payment of its voided Pix is an ORPHAN item). Nothing changes on "no_window" /
    "resend_failed". 3rd round: the new adjustment copies `old_slot`/`old_slot_at` (the old
    event is already off the agenda); the old one's `difkeep` is discarded after the commit."""
    tenant = await session.get(Tenant, adjustment.tenant_id)
    deposit = await session.get(PixDeposit, adjustment.deposit_id)
    if tenant is None or deposit is None or deposit.tenant_id != tenant.id:
        return ChoiceOutcome(kind="resend_failed", **base)
    data = adjustment.pending_edit or {}
    new_start, new_end = _pending_window(appointment, data)
    ttl = _pix_ttl_minutes(tenant)
    if not resend_fits(now, ttl, appointment.start_at, new_start):
        return ChoiceOutcome(kind="no_window", **base)
    professional_id = _uuid_or_none(data.get("professional_id"))
    held = await booking_hold.difference_hold_in(session, adjustment.id)
    if held is None:
        # The hold ran out: never revived blind - the window is re-checked under its agenda lock.
        await booking_hold.serialize_agenda_in(session, tenant.id, professional_id)
        if not await _window_fits(
            session, tenant, appointment, new_start, new_end,
            professional_id=professional_id, exclude_adjustment_id=adjustment.id,
        ):
            return ChoiceOutcome(kind="no_window", **base)
    expires = suspend_pix_expiry(now, ttl, appointment.start_at, new_start)
    if expires is None:  # pragma: no cover - `resend_fits` checked it
        return ChoiceOutcome(kind="no_window", **base)
    new_id = uuid4()
    charged = await _create_difference_charge(
        session, tenant=tenant, deposit=deposit, adjustment_id=new_id,
        diff_cents=adjustment.diff_cents, shown=adjustment.new_service, now=now,
        expires_at=expires, log=log,
    )
    if isinstance(charged, str):
        return ChoiceOutcome(kind="resend_failed", **base)
    new = DepositAdjustment(
        id=new_id,
        tenant_id=tenant.id,
        deposit_id=deposit.id,
        appointment_id=appointment.id,
        kind=KIND_INCREASE,
        mode=MODE_SUSPEND,
        status=STATUS_AWAITING_PAYMENT,
        old_service=adjustment.old_service,
        new_service=adjustment.new_service,
        pending_edit=dict(data),
        diff_cents=adjustment.diff_cents,
        provider_charge_id=charged.provider_charge_id,
        expires_at=expires,
        # 3rd round: the old event already left the agenda (or is leaving it) - the NEW change
        # owns that state now (nothing is deleted twice; `retry_vacates` follows the new row).
        old_slot=adjustment.old_slot,
        old_slot_at=adjustment.old_slot_at,
    )
    session.add(new)
    await session.flush()
    hold_until = difference_hold_expiry(expires, new_start)
    moved = await booking_hold.extend_difference_hold_in(
        session, adjustment.id, expires_at=hold_until, to_adjustment_id=new_id
    )
    if moved is None:
        moved = await booking_hold.place_difference_hold_in(
            session,
            tenant_id=tenant.id,
            adjustment_id=new_id,
            patient_id=deposit.patient_id,
            professional_id=professional_id,
            unit_id=_uuid_or_none(data["unit_id"]) if "unit_id" in data
            else getattr(appointment, "unit_id", None),
            appointment_type=(data.get("appointment_type") or "")[:120] or None,
            insurance=data.get("insurance"),
            attendee_name=data.get("attendee_name"),
            start_at=new_start,
            end_at=new_end,
            expires_at=hold_until,
        )
    if moved is None:
        # Defence only (checked under the lock): undo the new row, void the unseen Pix.
        await _void_charge(session, tenant, deposit, charged.provider_charge_id, log)
        await session.delete(new)
        await session.flush()
        return ChoiceOutcome(kind="no_window", **base)
    await booking_hold.release_difference_hold_in(session, adjustment.id)
    if not await move_adjustment(
        session, adjustment, from_status=STATUS_AWAITING_CHOICE, to_status=STATUS_EXPIRED,
        now=now,
    ):  # pragma: no cover - the row is locked
        raise RuntimeError("choice moved under its own lock")
    # UA1 delta 10: a `difkeep` of the OLD change would read as "somebody else's event" when the
    # new change tries to bring the old consultation back - discarded after the commit.
    adjustment_queries.discard_restore_after_commit(session, adjustment)
    logger.info("service_change_choice_resent", new_adjustment_id=str(new_id), **log)
    return ChoiceOutcome(
        kind="resent",
        adjustment_id=new_id,
        appointment_id=appointment.id,
        diff_cents=adjustment.diff_cents,
        pix_copy_paste=charged.copy_paste,
        expires_at=expires,
        old_service=adjustment.old_service,
        new_service=adjustment.new_service,
    )


async def followup_kept_old(tenant_id: UUID, appointment_id: UUID) -> None:
    """After the commit that moved a change to `kept_old` (a tap or silence): the old
    consultation is visible again, so its reminders are re-planned
    (`reminder_hooks.after_appointment_rescheduled`). Own session, best-effort, never raises."""
    from secretaria.services import reminder_hooks

    try:
        await reminder_hooks.after_appointment_rescheduled(appointment_id)
    except Exception as exc:  # noqa: BLE001 - reminders are best-effort
        logger.warning(
            "service_change_kept_old_followup_failed",
            tenant_id=str(tenant_id),
            error_type=type(exc).__name__,
        )


def choice_message_text(
    old_service: str,
    new_service: str,
    diff_cents: int,
    *,
    old_slot_free: bool = True,
    resend: bool = True,
) -> str:
    """The body sent with the buttons when the Pix of a suspended change expired (mapa-u §4.6):
    it spells the options out (D-R7) - go back to the OLD consultation (or, when its time was
    taken, D-R11, choose another time for it with the paid sinal still valid), resend the Pix to
    make the change, or cancel both - and what happens without an answer: the engine tries to
    bring the old one back (D-R8), or, with its time taken, the clinic gets in touch. `resend`
    False leaves the "Reenviar o Pix" line out, like its button (`choice_buttons`)."""
    if old_slot_free:
        first = (
            f"• {LABEL_CHOICE_KEEP}: voltar para a sua consulta de antes ({old_service}), no "
            "mesmo horário."
        )
        silence = "Se você não responder a tempo, tentamos voltar para a consulta antiga."
    else:
        first = (
            f"• {LABEL_CHOICE_OTHER}: o horário da sua consulta antiga foi ocupado; escolha um "
            "novo horário para ela, com o mesmo serviço e profissional. O sinal pago continua "
            "valendo."
        )
        silence = "Se você não responder a tempo, a clínica vai falar com você para remarcar."
    lines = [
        f"O Pix da diferença ({format_brl(diff_cents)}) para trocar sua consulta para "
        f"{new_service} venceu sem pagamento. O que você prefere?",
        first,
    ]
    if resend:
        lines.append(f"• {LABEL_CHOICE_RESEND}: receber um novo Pix para fazer a troca.")
    lines.append(
        f"• {LABEL_CHOICE_BOTH}: cancelar as duas, com as regras do sinal pago."
    )
    lines.append(silence)
    return "\n".join(lines)


def choice_answer_text(outcome: ChoiceOutcome) -> str:
    """The ONE reply to a tap on the choice, per `outcome.kind` (the caller sends it; on "resent"
    the Pix text `difference_pix_text` follows in the same message, on "cancel_old" the
    cancellation path sends its own notice; on "slot_taken" the caller re-sends the buttons with
    "Outro horário" - `choice_buttons(old_slot_free=False)`)."""
    old = outcome.old_service or "a consulta antiga"
    new = outcome.new_service or "a nova consulta"
    texts = {
        "kept_old": kept_old_notice_text(old),
        "resent": (
            f"Certo! Enviamos um novo Pix da diferença ({format_brl(outcome.diff_cents)}) "
            f"para trocar sua consulta para {new}."
        ),
        "cancel_old": "Certo! A troca foi desfeita e vamos cancelar sua consulta.",
        # Fix round 1 (review M4): neutral - with the old slot taken, "manter" is not offered.
        "no_window": (
            "O novo horário não está mais disponível, então não dá para reenviar o Pix. "
            "Escolha uma das outras opções."
        ),
        "resend_failed": (
            "Não foi possível gerar um novo Pix agora. Tente de novo em instantes ou escolha "
            "outra opção."
        ),
        "already_decided": "Essa escolha já foi feita (ou o prazo para responder acabou).",
        # 3rd round (D-R11, mapa-u §4.6)
        "rebook": (
            f"Certo! Escolha o novo dia da sua consulta ({old}). O sinal pago continua valendo."
        ),
        "slot_taken": (
            "O horário da consulta antiga acabou de ser ocupado. Você pode escolher outro "
            "horário, reenviar o Pix ou cancelar."
        ),
        "restore_failed": "Não consegui devolver sua consulta agora. Tente de novo em instantes.",
    }
    return texts.get(outcome.kind, "Não encontrei essa troca de consulta.")


# --------------------------------------------------------------------------
# TASK-085 R (S7a): what a pending change would change, for the clinic's agenda
# --------------------------------------------------------------------------

SUMMARY_SEPARATOR = "; "
_CLINIC_AGENDA = "Agenda da clínica"
_OTHER_PROFESSIONAL = "Outro profissional"
_NO_UNIT = "Sem unidade"
_OTHER_UNIT = "Outra unidade"
_PRIVATE = "Particular"
DEFAULT_SERVICE_LABEL = "Consulta"


def _id_text(value) -> str:
    return "" if value in (None, "") else str(value)


def _plan_text(value) -> str | None:
    text = (value or "").strip()
    return None if not text or text.casefold() == _PRIVATE.casefold() else text


def _doctor_label(professional_id: str, professional_names: dict) -> str:
    if not professional_id:
        return _CLINIC_AGENDA
    return professional_names.get(professional_id) or _OTHER_PROFESSIONAL


def _unit_label(unit_id: str, unit_names: dict) -> str:
    if not unit_id:
        return _NO_UNIT
    name = unit_names.get(unit_id)
    if not name:
        return _OTHER_UNIT
    return name if name.casefold().startswith("unidade") else f"Unidade {name}"


def _summary_when(value: str | None, tz) -> datetime | None:
    try:
        return as_utc(datetime.fromisoformat(value)).astimezone(tz) if value else None
    except (TypeError, ValueError):
        return None


def pending_change_summary_text(
    pending_edit: dict | None, *, tz, professional_names: dict, unit_names: dict
) -> str | None:
    """What an open change on a sinal would change, in plain Portuguese, for the clinic's agenda
    ("Consulta → Retorno; Dra. A → Dr. B; Unidade Centro → Unidade Leste; Particular → Unimed;
    10:00 → 11:30"): only the fields that change, in that order, from the row the change was
    asked on (`pending_edit["expected"]`) to the target. Times in the clinic's `tz` (the dates
    too when the day changes). `professional_names` / `unit_names` map ids (text) to names of
    THIS clinic only - any other id reads "Outro profissional" / "Outra unidade", never raw. None
    when nothing can be said (no change recorded, or a payload without the row it was asked on).
    3rd round (D-R14): a price change (`source="price_change"`) reads "Novo preço: sinal R$ A →
    R$ B" (what was paid -> the sinal of the price in force)."""
    data = pending_edit or {}
    if is_price_change(data):
        # D-R14: the clinic changed the price - only the sinal moves.
        old_cents, new_cents = data.get("old_sinal_cents"), data.get("new_sinal_cents")
        if isinstance(old_cents, int) and isinstance(new_cents, int):
            return f"Novo preço: sinal {format_brl(old_cents)} → {format_brl(new_cents)}"
        return None
    expected = data.get("expected")
    if not isinstance(expected, dict):
        return None
    parts: list[str] = []
    old_service = (expected.get("appointment_type") or "").strip()
    new_service = (data.get("appointment_type") or "").strip()
    if new_service and old_service.casefold() != new_service.casefold():
        parts.append(f"{old_service or DEFAULT_SERVICE_LABEL} → {new_service}")
    if "professional_id" in data and "professional_id" in expected:
        old_doctor = _id_text(expected.get("professional_id"))
        new_doctor = _id_text(data.get("professional_id"))
        if old_doctor != new_doctor:
            parts.append(
                " → ".join(
                    _doctor_label(doctor, professional_names) for doctor in (old_doctor, new_doctor)
                )
            )
    if "unit_id" in data and "unit_id" in expected:
        old_unit, new_unit = _id_text(expected.get("unit_id")), _id_text(data.get("unit_id"))
        if old_unit != new_unit:
            parts.append(
                f"{_unit_label(old_unit, unit_names)} → {_unit_label(new_unit, unit_names)}"
            )
    if "insurance" in data and "insurance" in expected:
        old_plan = _plan_text(expected.get("insurance"))
        new_plan = _plan_text(data.get("insurance"))
        if (old_plan or "").casefold() != (new_plan or "").casefold():
            parts.append(f"{old_plan or _PRIVATE} → {new_plan or _PRIVATE}")
    old_start = _summary_when(expected.get("start_at"), tz)
    new_start = _summary_when(data.get("start_at"), tz)
    if old_start is not None and new_start is not None and old_start != new_start:
        fmt = "%H:%M" if old_start.date() == new_start.date() else "%d/%m %H:%M"
        parts.append(f"{old_start.strftime(fmt)} → {new_start.strftime(fmt)}")
    return SUMMARY_SEPARATOR.join(parts) or None


async def pending_change_summaries(
    session: AsyncSession, tenant: Tenant, edits: dict[UUID, dict]
) -> dict[UUID, str]:
    """`pending_change_summary_text` of a whole page (key -> `pending_edit`), with the names of
    THIS clinic's professionals and units read in at most two tenant-scoped queries. Keys whose
    change says nothing are absent. Read-only."""
    if not edits:
        return {}
    doctor_ids: set[UUID] = set()
    unit_ids: set[UUID] = set()
    for data in edits.values():
        for side in (data or {}, (data or {}).get("expected") or {}):
            if isinstance(side, dict):
                doctor = _uuid_or_none(side.get("professional_id"))
                unit = _uuid_or_none(side.get("unit_id"))
                if doctor is not None:
                    doctor_ids.add(doctor)
                if unit is not None:
                    unit_ids.add(unit)
    professional_names: dict[str, str] = {}
    if doctor_ids:
        rows = await session.execute(
            select(Professional.id, Professional.name).where(
                Professional.tenant_id == tenant.id, Professional.id.in_(doctor_ids)
            )
        )
        professional_names = {str(pid): name for pid, name in rows.all() if name}
    unit_names: dict[str, str] = {}
    if unit_ids:
        rows = await session.execute(
            select(Unit.id, Unit.name).where(Unit.tenant_id == tenant.id, Unit.id.in_(unit_ids))
        )
        unit_names = {str(uid): name for uid, name in rows.all() if name}
    tz = _tenant_tz(tenant)
    out: dict[UUID, str] = {}
    for key, data in edits.items():
        text = pending_change_summary_text(
            data, tz=tz, professional_names=professional_names, unit_names=unit_names
        )
        if text:
            out[key] = text
    return out


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
    "request_paid_change",
    "request_service_change",
    "sync_agenda_event",
    "update_agenda_after_payment",
    # TASK-085 R (S0 frozen surface)
    "ACTION_CHOICE_BOTH",
    "ACTION_CHOICE_KEEP",
    "ACTION_CHOICE_RESEND",
    "CHOICE_ACTIONS",
    "CHOICE_BOTH",
    "CHOICE_KEEP",
    "CHOICE_MIN_LEAD",
    "CHOICE_OF_ACTION",
    "CHOICE_RESEND",
    "CHOICE_TTL",
    "DIFFERENCE_HOLD_GRACE",
    "LABEL_CHOICE_BOTH",
    "LABEL_CHOICE_KEEP",
    "LABEL_CHOICE_RESEND",
    "ChoiceOutcome",
    "choice_answer_text",
    "choice_buttons",
    "choice_deadline",
    "difference_hold_expiry",
    "resend_fits",
    "suspend_cutoff",
    "suspend_pix_expiry",
    "MIN_SUSPEND_PIX",
    "SWEEP_MARGIN",
    "choice_message_text",
    "choose_after_expiry",
    "followup_kept_old",
    "on_difference_unpaid",
    # TASK-085 R (S7a)
    "CHARGED_KINDS",
    "UnseenCharge",
    "VoidOutcome",
    "open_unvoided_charge_item",
    "pending_change_summaries",
    "pending_change_summary_text",
    "unseen_charge_of",
    "void_unseen_charge",
    # TASK-085 R 3rd round (UA2, mapa-u §4.4)
    "ACTION_CHOICE_OTHER",
    "CHOICE_OTHER",
    "LABEL_CANCEL_CONSULTATION",
    "LABEL_CHOICE_OTHER",
    "OPEN_ADJUSTMENT_STATUSES",
    "SOURCE_PRICE_CHANGE",
    "RestoreFollowup",
    "is_price_change",
    "keep_old_on_silence",
    "lost_slot_buttons",
    "old_slot_lost_text",
    "preview_price_requote",
    "price_requote",
    "price_unpaid_text",
    "reopen_as_restoring",
    "restore_and_tell",
    "run_unpaid_followup",
    "visit_upcoming",
]
