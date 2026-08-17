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

from uuid import UUID

from secretaria.core.logging import get_logger
from secretaria.models.appointment import AppointmentStatus, is_live_status

logger = get_logger(__name__)

# Where the write came from. A closed vocabulary so the field stays groupable.
SOURCE_FLOW = "flow"
SOURCE_BUTTON = "button"
SOURCE_HUB = "hub"
SOURCE_SYSTEM = "system"


def log_status_transition(
    *,
    appointment_id: UUID | str | None,
    tenant_id: UUID | str | None,
    old_status: AppointmentStatus | None,
    new_status: AppointmentStatus,
    source: str,
    idempotency_key: str | None = None,
) -> None:
    """Record one appointment status transition, sanitized.

    `idempotency_key` is whatever makes a REPLAY of this exact transition
    recognisable in the logs (the moved window for a reschedule, the
    appointment id for a confirm/cancel). It is a correlation aid, not a
    guarantee — the actual replay protection is the `processed_events` ledger
    and the fact that every write here is idempotent by construction.

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
        still_live=is_live_status(new_status),
    )
