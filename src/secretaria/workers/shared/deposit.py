"""deposit - split out of workers/tasks.py (TASK-023)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS
from secretaria.models import (
    Appointment,
    FlowState,
    PixDepositStatus,
    Tenant,
)
from secretaria.services.flow_router import (
    LABEL_CONFIRM,
    STEP_EDIT_CONFIRM,
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_CONFIRM,
    STEP_MANAGE_DAY,
    STEP_MANAGE_DAY_ESCAPE,
    STEP_MANAGE_DAY_RETRY,
    STEP_MANAGE_SLOT,
    FlowRouterResult,
    MenuBubble,
)
from secretaria.services.insurance_catalog import resolve_booking_plan_ids
from secretaria.services.payments import adjustment_queries, deposit_lifecycle, service_change
from secretaria.services.payments.deposit_lifecycle import _tenant_tz
from secretaria.services.payments.money import format_brl
from secretaria.services.payments.service_change_policy import (
    MONEY_FIELD_UNIT,
    MoneyTerms,
    money_fields_changed,
)
from secretaria.services.whatsapp import (
    WhatsAppClient,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.sender import (
    _reply_sender,
)
from secretaria.workers.shared.text import (
    _as_utc,
)

logger = get_logger(__name__)


# Steps `_apply_deposit_awareness` inspects. The day-picker retry/escape
# renders are the SAME step of the flow as STEP_MANAGE_DAY, just re-drawn
# after an unreadable free-text date, so the reschedule-limit pre-check has to
# recognise them too — otherwise a blocked target could slip past the gate by
# mistyping a date once. The slot list and the reschedule confirmation card
# joined in TASK-030 P3: the AI's manage hand-back can land a reschedule
# straight on them, skipping the day picker where the pre-check used to run.
# For the buttons it changes nothing in practice - a target already at the
# limit never got past the day picker - and it catches a limit reached between
# the day pick and the time pick.
_RESCHEDULE_PRECHECK_STEPS = (
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_DAY,
    STEP_MANAGE_DAY_RETRY,
    STEP_MANAGE_DAY_ESCAPE,
    STEP_MANAGE_SLOT,
    STEP_MANAGE_CONFIRM,
)


def _at_reschedule_limit(deposit, tenant: Tenant) -> bool:
    """Whether `deposit` has used up the clinic's reschedule allowance."""
    return deposit is not None and deposit.reschedule_count >= tenant.pix_reschedule_limit


async def _reschedule_limit_hit(tenant: Tenant, appointment_id) -> tuple[int, int] | None:
    """`(count, limit)` when this appointment can no longer be rescheduled, else None.

    The read-only half of `_apply_deposit_awareness`'s reschedule pre-check, for a caller
    that must know BEFORE it builds a landing: the AI's manage hand-back (TASK-030 P3)
    computes no new day for a reschedule the clinic will refuse. Non-incrementing.
    """
    async with async_session_factory() as session:
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment_id)
    if not _at_reschedule_limit(deposit, tenant):
        return None
    return deposit.reschedule_count, tenant.pix_reschedule_limit


def _pix_retention_warning_line(tenant: Tenant, deposit) -> str:
    """The pt-BR retention-policy line for a cancellation landing inside the
    refund window. Shared VERBATIM by the deterministic flow's cancel-confirm
    question (`_apply_deposit_awareness`) and the reminder-button `apptcancel`
    warn (`_handle_action_button`) - same wording, different surrounding
    call-to-action text.

    The partial amount is computed on what the clinic HOLDS (sinal + paid service differences,
    `adjustment_queries.held_cents_hint` - stamped by `get_deposit_for_appointment`), the same
    figure `on_appointment_cancelled` will ask the clinic to return.
    """
    if tenant.pix_retention_policy == "partial":
        held = adjustment_queries.held_cents_hint(deposit)
        partial_amount = round(held * tenant.pix_partial_refund_percent / 100)
        return (
            f"Cancelamentos com menos de {tenant.pix_refund_window_hours}h têm reembolso "
            f"parcial de {tenant.pix_partial_refund_percent}% ({format_brl(partial_amount)})."
        )
    return (
        f"Cancelamentos com menos de {tenant.pix_refund_window_hours}h de antecedência não "
        "são reembolsáveis."
    )

def _cancel_warning_line(
    tenant: Tenant, deposit, appointment: Appointment, now: datetime
) -> str | None:
    """The money sentence a patient must read BEFORE cancelling a PAID deposit, or None when
    cancelling now simply returns it. Three surfaces share it (the flow's cancel-confirm, the
    reminder button and the reminder give-up) so they always agree with
    `deposit_lifecycle.on_appointment_cancelled`: every reschedule used -> the clinic keeps the
    deposit; inside the refund window -> the retention policy line."""
    if deposit is None or deposit.status != PixDepositStatus.PAID:
        return None
    if deposit_lifecycle.at_reschedule_limit(tenant, deposit):
        return (
            f"Você já usou as {tenant.pix_reschedule_limit} remarcações permitidas: ao cancelar "
            "agora, o sinal fica com a clínica, sem devolução."
        )
    hours_until = _hours_until_start(appointment, now)
    if hours_until is not None and hours_until <= tenant.pix_refund_window_hours:
        return _pix_retention_warning_line(tenant, deposit)
    return None


def _hours_until_start(appointment: Appointment, now: datetime) -> float | None:
    """Hours between `now` and `appointment.start_at`, or None when unknown.

    Mirrors services/payments/deposit_lifecycle.py::on_appointment_cancelled's
    own window computation exactly (None reads as "outside the window" on
    both sides, same as that function) - duplicated locally rather than
    imported, matching this codebase's existing convention of small
    per-module `_as_utc`-adjacent helpers (see plugins/reminders.py).
    """
    if appointment.start_at is None:
        return None
    return (_as_utc(appointment.start_at) - now).total_seconds() / 3600

# The sentence of the "before" card line that promises both times reserved (D-R4); dropped when
# the change falls back to the legacy deferred mode (no room for a suspension, contrato §1).
_RESERVED_SENTENCE = " Até lá,"


async def _send_reschedule_limit_buttons(
    client: WhatsAppClient, to: str | None, appointment_id, count: int, limit: int
) -> None:
    """The keep-or-cancel message sent once an appointment's deposit has hit
    `pix_reschedule_limit`. Shared by the reminder-button `apptresched`
    handler and the deterministic flow's own reschedule-entry pre-check
    (`_apply_deposit_awareness`) so both surfaces say exactly the same thing
    and use the same apptconfirm|/apptcancel| button ids.
    """
    body = (
        f"Você já remarcou essa consulta {count}x — o limite com sinal pago é {limit} e não é "
        "possível remarcar de novo. Se cancelar agora, o sinal fica com a clínica, sem devolução. "
        "Prefere manter o horário ou cancelar?"
    )
    await client.send_buttons(
        to,
        body,
        [
            (f"apptconfirm|{appointment_id}", "Manter horário"),
            (f"apptcancel|{appointment_id}", "Cancelar"),
        ],
    )

def _draft_target(current: dict, appointment: Appointment) -> dict:
    """The draft's money terms as an R6 edit dict (`MoneyTerms.with_edit` keys) - the same shape
    `appointment_edit_flow._apply_confirm` hands the worker, so the card and Confirmar agree."""
    target = {
        "appointment_type": current.get("service"),
        "professional_id": current.get("professional_id", appointment.professional_id),
        "insurance": current.get("insurance", appointment.insurance),
    }
    if "unit_id" in current:
        target["unit_id"] = current.get("unit_id")
    if "attendee_name" in current:
        target["attendee_name"] = current.get("attendee_name")
    return target


def _uuid(value) -> UUID | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


async def _paid_card_line(
    session, tenant: Tenant, appointment: Appointment, deposit, target: dict, start_at, changed
) -> str | None:
    """PAID sinal: the line of the decision `request_paid_change` takes at Confirmar (TASK-085 R:
    every money field, the clinic's timing)."""
    now = datetime.now(UTC)
    unit = {"unit_id": target["unit_id"]} if "unit_id" in target else {}
    decision = await service_change.preview_service_change(
        session,
        tenant=tenant,
        appointment=appointment,
        deposit=deposit,
        new_service=str(target["appointment_type"]),
        professional_id=target["professional_id"],
        start_at=start_at,
        now=now,
        insurance=target["insurance"],
        **unit,
    )
    # Lazy: appointment_edit_apply imports this package's support module.
    from secretaria.workers.shared import appointment_edit_apply as apply

    before = service_change.timing_is_before(tenant)
    no_room = (
        before
        and decision.action == "increase_charge"
        and apply.legacy_without_room(tenant, appointment, start_at, now)
    )
    if no_room and apply.legacy_cannot_carry(appointment, target, changed):
        # The legacy deferred mode never writes doctor / convênio / who: Confirmar refuses it
        # (`LEGACY_NO_ROOM_TEXT`) - the card says so instead of a Pix of the difference.
        return apply.LEGACY_NO_ROOM_CARD_TEXT
    line = service_change.confirm_card_line_text(
        decision.action,
        decision.diff_cents,
        unit_only=changed == frozenset({MONEY_FIELD_UNIT}),
        timing="before" if before else "after",
    )
    if line and no_room:
        # No room for a suspension before the visits (contrato §1): the legacy deferred mode
        # reserves nothing - the card must not promise it.
        line = line.split(_RESERVED_SENTENCE, 1)[0]
    return line


async def _requote_card_line(
    session, tenant: Tenant, appointment: Appointment, deposit, target: dict
) -> str | None:
    """UNPAID sinal (D-R6): "O sinal passa de X para Y; ao confirmar, cancelamos o Pix anterior e
    enviamos um novo." - the amount `requote_awaiting` will compute at Confirmar."""
    if deposit.deposit_timing == "before":
        return None  # `requote_awaiting` never touches such a row
    professional_id = _uuid(target["professional_id"])
    plan_id, professional_plan_id = await resolve_booking_plan_ids(
        session, tenant.id, target["insurance"], professional_id
    )
    probe = SimpleNamespace(
        id=appointment.id,
        appointment_type=target["appointment_type"],
        professional_id=professional_id,
        unit_id=_uuid(target["unit_id"]) if "unit_id" in target else appointment.unit_id,
        insurance_plan_id=plan_id,
        insurance_professional_plan_id=professional_plan_id,
    )
    new_cents = await deposit_lifecycle.requote_amount_cents(
        session, tenant=tenant, appointment=probe
    )
    if new_cents == deposit.amount_cents:
        return None
    return deposit_lifecycle.requote_card_line_text(deposit.amount_cents, new_cents)


async def _with_service_difference(result: FlowRouterResult, tenant: Tenant) -> FlowRouterResult:
    """The confirmation card of an edit that changes a MONEY field (service, unit, doctor,
    convênio - `service_change_policy.money_fields_changed`) of a consultation with a sinal gets
    one more line. Read-only and best-effort: any doubt leaves the card as it was (Confirmar still
    explains).

    * PAID sinal: the difference of the sinal (to pay by Pix - "after": the change is made at
      once; "before": it waits for the Pix with both times reserved -, or above the new value),
      from the same decision `service_change.request_paid_change` takes at Confirmar (browser
      proof 2026-10-26; TASK-085 R: every money field, the clinic's timing). A change that does
      not move the price (`no_change`) adds nothing.
    * UNPAID sinal (TASK-085 R D-R6): the re-quote - the old Pix is cancelled and a new one with
      the new amount is sent at Confirmar.
    """
    draft = result.flow_edit_draft if isinstance(result.flow_edit_draft, dict) else {}
    current = draft.get("current") or {}
    new_service = current.get("service")
    if not new_service:
        return result
    card = next(
        (b for b in result.bubbles if isinstance(b, MenuBubble) and LABEL_CONFIRM in b.labels),
        None,
    )
    if card is None:
        return result
    appointment_id = result.flow_managing_appointment_id
    try:
        async with async_session_factory() as session:
            deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment_id)
            if deposit is None or deposit.status not in (
                PixDepositStatus.PAID,
                PixDepositStatus.AWAITING,
            ):
                return result
            appointment = await session.get(Appointment, appointment_id)
            if appointment is None or appointment.tenant_id != tenant.id:
                return result
            target = _draft_target(current, appointment)
            terms = MoneyTerms.of_appointment(appointment)
            changed = money_fields_changed(terms, terms.with_edit(target))
            if not changed:
                return result
            if deposit.status == PixDepositStatus.AWAITING:
                line = await _requote_card_line(session, tenant, appointment, deposit, target)
            else:
                start = current.get("start_at")
                start_at = (
                    datetime.fromisoformat(start).replace(tzinfo=_tenant_tz(tenant))
                    if start
                    else appointment.start_at
                )
                line = await _paid_card_line(
                    session, tenant, appointment, deposit, target, start_at, changed
                )
    except Exception as exc:  # noqa: BLE001 - the card goes out as it was
        logger.warning(
            "edit_confirm_difference_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return result
    body = f"{card.body.rstrip()}\n\n{line}" if line else card.body
    if line and len(body) <= MAX_INTERACTIVE_BODY_CHARS:
        card.body = body
    return result


async def _apply_deposit_awareness(
    reply: _ReplyContext,
    result: FlowRouterResult,
    tenant: Tenant | None,
    patient_wa: str | None,
    waba_token: str | None,
) -> FlowRouterResult:
    """Fold Pix-deposit awareness into a manage-flow `FlowRouterResult`
    BEFORE it is persisted/dispatched by `_apply_flow_result` (money hooks,
    PROMPT S3 section 4). Two moments, both reached from every entry path
    (direct menu tap, LLM sentinel hand-back, or a reminder-button
    preselection) since they all funnel through `_apply_flow_result`:

      - STEP_MANAGE_CANCEL_CONFIRM: prepend the retention-policy warning
        (`_pix_retention_warning_line`, same wording as the reminder-button
        `apptcancel` warn) to the Sim/Não question when the target has a PAID
        deposit inside the refund window. The Sim/Não buttons themselves are
        untouched — tapping "Sim" still runs `_manage_cancel`, which this
        module's cancel-site hook (below, in `_apply_flow_result`) makes
        deposit-aware too.
      - a reschedule render (STEP_MANAGE_DAY*, STEP_MANAGE_SLOT / STEP_MANAGE_CONFIRM):
        when the target is AT/OVER `pix_reschedule_limit`, replace the
        day-ask with the SAME keep-or-cancel button message
        (`_send_reschedule_limit_buttons`) the reminder's own blocked-
        reschedule reply uses — sent directly here (no Bubble type carries
        custom button ids) — and the flow is reset to MENU so those buttons
        (routed by id, independent of flow_state — see
        schemas/webhook.py::extract_action_button) are the only way forward.
        Non-incrementing: this is a PRE-check, exactly like the button
        carrier's own (`_handle_action_button`) — `register_reschedule` only
        ever runs at actual completion (this module's reschedule-site hook).

    Implemented HERE, not threaded into flow_router.py's pure functions:
    that module's own docstring commits it to doing no DB I/O of its own —
    this keeps that invariant at the cost of one extra short-lived
    session/query for these two specific turns. Every other turn (including
    STEP_MANAGE_DAY retries for an UNBLOCKED target — cheap, idempotent,
    re-checked each time rather than tracked) returns `result` unchanged.

    Plus the edit flow's confirmation card (STEP_EDIT_CONFIRM) of a new service on a PAID
    sinal: `_with_service_difference` adds the difference line before Confirmar.
    """
    if (
        result.action == "reply"
        and tenant is not None
        and result.flow_managing_appointment_id is not None
        and result.flow_step == STEP_EDIT_CONFIRM
    ):
        return await _with_service_difference(result, tenant)
    if (
        result.action != "reply"
        or tenant is None
        or result.flow_managing_appointment_id is None
        or result.flow_step not in _RESCHEDULE_PRECHECK_STEPS
    ):
        return result

    appointment_id = result.flow_managing_appointment_id
    warning: str | None = None
    limit_count: int | None = None
    limit_value: int | None = None

    async with async_session_factory() as session:
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment_id)

        if result.flow_step == STEP_MANAGE_CANCEL_CONFIRM:
            if deposit is None or deposit.status != PixDepositStatus.PAID:
                return result
            appointment = await session.get(Appointment, appointment_id)
            if appointment is None:
                return result
            warning = _cancel_warning_line(tenant, deposit, appointment, datetime.now(UTC))
            if warning is None:
                return result
        else:  # any reschedule render: day picker, slot list or the confirmation card
            if not _at_reschedule_limit(deposit, tenant):
                # Still allowed: the first render (the day picker) tells the patient how many
                # reschedules are left and what happens after the last one.
                if (
                    result.flow_step == STEP_MANAGE_DAY
                    and deposit is not None
                    and deposit.status == PixDepositStatus.PAID
                    and (tenant.pix_reschedule_limit or 0) > 0
                    and result.bubbles
                ):
                    left = tenant.pix_reschedule_limit - (deposit.reschedule_count or 0)
                    word = "remarcação" if left == 1 else "remarcações"
                    result.bubbles[0].body = (
                        f"Atenção: com sinal pago, restam {left} {word} (limite de "
                        f"{tenant.pix_reschedule_limit}). Depois disso não será possível "
                        "remarcar e, se você cancelar, o sinal fica com a clínica, sem "
                        f"devolução.\n\n{result.bubbles[0].body}"
                    )
                return result
            limit_count = deposit.reschedule_count
            limit_value = tenant.pix_reschedule_limit

    if warning is not None:
        if result.bubbles:
            result.bubbles[0].body = f"{warning}\n\n{result.bubbles[0].body}"
        return result

    client = _reply_sender(reply, tenant, waba_token)
    if client is None:
        # Fail closed (PROMPT_FIX_21): without this tenant's own credentials
        # the keep-or-cancel card cannot be sent, so leave the flow result
        # untouched rather than resetting to MENU with nothing delivered.
        logger.error("worker_reschedule_limit_no_credential", appointment_id=str(appointment_id))
        return result
    await _send_reschedule_limit_buttons(
        client, patient_wa, appointment_id, limit_count, limit_value
    )
    result.bubbles = []
    result.flow_state = FlowState.MENU
    result.flow_step = None
    result.flow_managing_appointment_id = None
    return result
