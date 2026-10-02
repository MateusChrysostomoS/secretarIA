"""deposit - split out of workers/tasks.py (TASK-023)."""

from datetime import UTC, datetime

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    FlowState,
    PixDepositStatus,
    Tenant,
)
from secretaria.services.flow_router import (
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_DAY,
    STEP_MANAGE_DAY_ESCAPE,
    STEP_MANAGE_DAY_RETRY,
    FlowRouterResult,
)
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.payments.money import format_brl
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
# mistyping a date once.
_RESCHEDULE_PRECHECK_STEPS = (
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_DAY,
    STEP_MANAGE_DAY_RETRY,
    STEP_MANAGE_DAY_ESCAPE,
)

def _pix_retention_warning_line(tenant: Tenant, deposit) -> str:
    """The pt-BR retention-policy line for a cancellation landing inside the
    refund window. Shared VERBATIM by the deterministic flow's cancel-confirm
    question (`_apply_deposit_awareness`) and the reminder-button `apptcancel`
    warn (`_handle_action_button`) - same wording, different surrounding
    call-to-action text.
    """
    if tenant.pix_retention_policy == "partial":
        partial_amount = round(deposit.amount_cents * tenant.pix_partial_refund_percent / 100)
        return (
            f"Cancelamentos com menos de {tenant.pix_refund_window_hours}h têm reembolso "
            f"parcial de {tenant.pix_partial_refund_percent}% ({format_brl(partial_amount)})."
        )
    return (
        f"Cancelamentos com menos de {tenant.pix_refund_window_hours}h de antecedência não "
        "são reembolsáveis."
    )

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
        f"Você já remarcou essa consulta {count}x — o limite com sinal é {limit}. "
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
      - a freshly-targeted STEP_MANAGE_DAY (a reschedule was just begun):
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
    """
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
            hours_until = _hours_until_start(appointment, datetime.now(UTC))
            if hours_until is None or hours_until > tenant.pix_refund_window_hours:
                return result
            warning = _pix_retention_warning_line(tenant, deposit)
        else:  # any STEP_MANAGE_DAY* render
            if deposit is None or deposit.reschedule_count < tenant.pix_reschedule_limit:
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
