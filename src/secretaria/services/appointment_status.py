"""Appointment status transitions — the one observability seam (PROMPT_FIX_16).

The taxonomy itself (LIVE vs TERMINAL, and the full transition table) lives on
`models/appointment.py`, next to the enum it describes. This module carries the
BEHAVIOUR that goes with it: a single sanitized log line every carrier emits
when it moves an appointment from one status to another.

Four carriers write appointment status, and they used to say nothing at all
when they did — which is why a booking silently disappearing from the manage
flow, the reminders sweep and the greeting was invisible in production:

  * `flow`   - the deterministic router (workers/tasks.py::_apply_flow_result)
  * `button` - a reminder action-button tap (workers/tasks.py::_handle_action_button)
  * `hub`    - the doctor hub (api/hub/calendar.py)
  * `system` - the Pix deposit-expiry sweep (services/payments/deposit_lifecycle.py)

LGPD: internal ids, status names and a reason code only. Never the patient's
phone, name, the appointment type, or any clinical detail — see
`core/logging.py`'s redactor for the backstop.
"""

from datetime import UTC, datetime
from uuid import UUID

from secretaria.core.logging import get_logger
from secretaria.models.appointment import AppointmentStatus, is_live_status

logger = get_logger(__name__)

# Where the write came from. A closed vocabulary so the field stays groupable.
SOURCE_FLOW = "flow"
SOURCE_BUTTON = "button"
SOURCE_HUB = "hub"
SOURCE_SYSTEM = "system"

# WHY a hub cancellation happened, for the log only (there is no reason column).
# `unconfirmed`: the clinic freed the slot because the patient never confirmed
# (api/hub/calendar.py::release_appointment, TASK-032 R4).
CANCEL_REASON_UNCONFIRMED = "unconfirmed"


def log_status_transition(
    *,
    appointment_id: UUID | str | None,
    tenant_id: UUID | str | None,
    old_status: AppointmentStatus | None,
    new_status: AppointmentStatus,
    source: str,
    idempotency_key: str | None = None,
    reason: str | None = None,
) -> None:
    """Record one appointment status transition, sanitized.

    `idempotency_key` is whatever makes a REPLAY of this exact transition
    recognisable in the logs (the moved window for a reschedule, the
    appointment id for a confirm/cancel). It is a correlation aid, not a
    guarantee — the actual replay protection is the `processed_events` ledger
    and the fact that every write here is idempotent by construction.
    `reason` is an optional closed-vocabulary code (see CANCEL_REASON_*), never free text.

    `still_live` is emitted so a status a reader does not recognise (a member
    added later, say) shows up as a countable anomaly rather than as a booking
    that quietly stopped being upcoming.
    """
    logger.info(
        "appointment_status_transition",
        appointment_id=str(appointment_id) if appointment_id is not None else None,
        tenant_id=str(tenant_id) if tenant_id is not None else None,
        old_status=old_status.value if old_status is not None else None,
        new_status=new_status.value,
        source=source,
        idempotency_key=idempotency_key,
        reason=reason,
        still_live=is_live_status(new_status),
    )


# --- TASK-032 R7: what a staff PATCH /status may do ----------------------------------


class StaffTransitionRefused(ValueError):
    """The PATCH would contradict the appointment's state (spec 2026-10-09 §1/§3)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


# After the start the agenda offers both "Compareceu" and "Faltou", so a mis-click must
# be correctable; nothing else ever leaves a terminal status through this endpoint.
STAFF_STATUS_CORRECTIONS: dict[AppointmentStatus, tuple[AppointmentStatus, ...]] = {
    AppointmentStatus.ATTENDED: (AppointmentStatus.NO_SHOW,),
    AppointmentStatus.NO_SHOW: (AppointmentStatus.ATTENDED,),
}


def staff_transition(
    current: AppointmentStatus,
    target: AppointmentStatus,
    *,
    start_at: datetime | None,
    now: datetime,
) -> bool:
    """True = apply `target`; False = a repeated attended/no_show (a no-op). Pure.

    * `cancelled` keeps its pre-R7, unguarded behaviour (and its money hook).
    * Any other target needs a LIVE appointment - a cancelled booking never comes back
      (it used to: PATCH confirmed "resurrected" it) - except the attended <-> no_show
      correction.
    * `no_show` before the start is refused: "Faltou" does not exist before the time.

    Raises `StaffTransitionRefused` with code `not_live` or `no_show_before_start`.
    """
    if target == AppointmentStatus.CANCELLED:
        return True
    if target in STAFF_STATUS_CORRECTIONS and current == target:
        return False
    if not (is_live_status(current) or current in STAFF_STATUS_CORRECTIONS.get(target, ())):
        raise StaffTransitionRefused("not_live", "Esta consulta já foi cancelada ou encerrada.")
    if target == AppointmentStatus.NO_SHOW and start_at is not None:
        start = start_at if start_at.tzinfo is not None else start_at.replace(tzinfo=UTC)
        if start > now:
            raise StaffTransitionRefused(
                "no_show_before_start",
                "Só é possível marcar falta depois do horário da consulta.",
            )
    return True
