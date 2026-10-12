"""What an edit turn needs from the database and the calendars (TASK-032 R6).

The router is pure, so the worker prepares its inputs: every agenda the edit may touch
and the Pix guards of the appointment being edited. Built once per edit turn
(`workers/orchestrator.py`) and handed to `route(edit_context=...)`; also used by the
reminder tap that OPENS the edit (`workers/shared/actions.py`).
"""

from typing import Any

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, PixDepositStatus, Tenant
from secretaria.services.appointment_edit import EditContext
from secretaria.services.calendar import CalendarService
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.tenant_config import resolve_professional_calendar

logger = get_logger(__name__)


async def edit_guards(session, tenant: Tenant, appointment: Appointment) -> tuple[bool, bool]:
    """(paid_deposit, reschedule_blocked): what the menu must hide for a paid Pix deposit.

    A PAID deposit hides service / doctor / convênio (the price may differ); a deposit
    whose reschedule counter reached `tenant.pix_reschedule_limit` also hides date / time.
    """
    deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
    paid = deposit is not None and deposit.status == PixDepositStatus.PAID
    blocked = paid and (deposit.reschedule_count or 0) >= (tenant.pix_reschedule_limit or 0)
    return paid, bool(blocked)


async def build_edit_context(
    session,
    tenant: Tenant,
    tenant_config,
    conversation,
    professional_rows: list | None,
    upcoming: list[dict] | None,
) -> EditContext:
    """Every agenda an edit may read or write, plus the guards of the appointment in edit."""
    calendars: dict[str, Any] = {}
    if tenant_config is not None:
        calendars["tenant"] = CalendarService.from_tenant_config(tenant_config)
    for row in professional_rows or []:
        try:
            calendars[str(row.id)] = await resolve_professional_calendar(
                session, tenant, row, tenant_config=tenant_config
            )
        except Exception as exc:  # count-only: the router degrades on a missing agenda
            logger.warning(
                "edit_professional_calendar_failed",
                professional_id=str(row.id),
                error_type=type(exc).__name__,
            )
            calendars[str(row.id)] = None
    paid = blocked = False
    appointment_id = getattr(conversation, "flow_managing_appointment_id", None)
    if appointment_id is not None:
        appointment = await session.get(Appointment, appointment_id)
        if appointment is not None and appointment.tenant_id == tenant.id:
            paid, blocked = await edit_guards(session, tenant, appointment)
    return EditContext(calendars=calendars, paid_deposit=paid, reschedule_blocked=blocked)
