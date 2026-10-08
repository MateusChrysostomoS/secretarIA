"""Apply a confirmed "Alterar Dados" edit to the SAME appointment row (TASK-032 R6).

`apply_appointment_edit` runs inside `_apply_flow_result`'s transaction (so a failed
persist rolls the whole edit back together with the flow state); `_finish_edit` runs
AFTER the commit, best-effort, like R3's `replacement._finish_replacement`: the old
Google event of a doctor change is deleted on the agenda that owned it, and a failure is
logged and never undoes the edit the patient just confirmed.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    PixDeposit,
    Tenant,
    is_live_status,
)
from secretaria.services import reminder_hooks
from secretaria.services.appointment_edit import EditDraft, appointment_email_version
from secretaria.services.appointment_status import SOURCE_FLOW, log_status_transition
from secretaria.services.calendar import build_event_description
from secretaria.services.insurance_catalog import resolve_booking_plan_ids
from secretaria.services.patient_context import as_utc
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.tenant_config import list_active_professionals
from secretaria.workers.shared.appointment_edit_support import edit_guards
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class AppliedEdit:
    appointment_id: UUID
    moved: bool  # the time changed AND reminders exist/are enabled -> replan them
    old_event_id: str | None  # delete after the commit (a doctor change)
    old_professional_id: UUID | None
    notice_id: str | None = None
    notice_version: str | None = None
    changed_fields: tuple[str, ...] = ()


def _row_draft(appointment, timezone) -> EditDraft:
    return EditDraft.from_appointment(
        {
            "id": str(appointment.id), "appointment_type": appointment.appointment_type,
            "professional_id": appointment.professional_id, "start_at": appointment.start_at,
            "end_at": appointment.end_at, "insurance": appointment.insurance,
            "attendee_name": appointment.attendee_name,
        }, ZoneInfo(timezone or "America/Sao_Paulo"),
    )


async def apply_appointment_edit(
    session,
    *,
    tenant,
    tenant_id,
    patient_id,
    edit: dict,
) -> AppliedEdit | None:
    """Write the edit onto the live appointment; None (nothing written) when it is not live."""
    appointment = await session.scalar(
        select(Appointment)
        .where(
            Appointment.id == edit["appointment_id"],
            Appointment.tenant_id == tenant_id,
            Appointment.patient_id == patient_id,
        )
        .with_for_update()
    )
    current_tenant = await session.get(Tenant, tenant_id)
    if (
        appointment is None
        or not is_live_status(appointment.status)
        or as_utc(appointment.start_at) <= datetime.now(UTC)
        or not reminder_hooks.enabled_for(current_tenant)
    ):
        logger.info(
            "appointment_edit_skipped", tenant_id=str(tenant_id), found=appointment is not None
        )
        return None
    before = _row_draft(appointment, current_tenant.timezone)
    if edit.get("original") is not None:
        if (
            before.original != edit["original"]
            or appointment.google_event_id != edit["old_google_event_id"]
        ):
            return None
    # Serialize the money counter too: a stale card cannot consume the same allowance twice.
    await session.scalar(
        select(PixDeposit)
        .where(
            PixDeposit.appointment_id == appointment.id,
            PixDeposit.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    paid, blocked = await edit_guards(session, current_tenant, appointment)
    money_changed = (
        edit["appointment_type"] != appointment.appointment_type
        or edit["professional_id"] != appointment.professional_id
        or edit["insurance"] != appointment.insurance
    )
    if (paid and money_changed) or (blocked and edit["time_changed"]):
        return None
    if edit["time_changed"]:
        allowed, _count = await deposit_lifecycle.register_reschedule(
            session,
            tenant=current_tenant,
            appointment=appointment,
        )
        if not allowed:
            return None
    previous = appointment.status
    plan_id, professional_plan_id = await resolve_booking_plan_ids(
        session, tenant_id, edit["insurance"], edit["professional_id"]
    )
    appointment.appointment_type = edit["appointment_type"] or appointment.appointment_type
    appointment.professional_id = edit["professional_id"]
    appointment.insurance = edit["insurance"]
    appointment.insurance_plan_id = plan_id
    appointment.insurance_professional_plan_id = professional_plan_id
    appointment.attendee_name = edit["attendee_name"]
    appointment.google_event_id = edit["google_event_id"]
    if edit.get("calendar_changed", edit["doctor_changed"]):
        appointment.google_event_link = edit.get("google_event_link")
    appointment.end_at = as_utc(edit["end_at"]).astimezone(UTC)
    if edit["time_changed"]:
        appointment.start_at = as_utc(edit["start_at"]).astimezone(UTC)
        appointment.status = AppointmentStatus.RESCHEDULED
        log_status_transition(
            appointment_id=appointment.id,
            tenant_id=tenant_id,
            old_status=previous,
            new_status=AppointmentStatus.RESCHEDULED,
            source=SOURCE_FLOW,
            idempotency_key=f"edit:{appointment.id}:{edit['start_at'].isoformat()}",
        )
    moved = bool(edit["time_changed"]) and (
        reminder_hooks.enabled_for(tenant) or (appointment.confirmation_count or 0) > 0
    )
    after = _row_draft(appointment, current_tenant.timezone)
    fields = EditDraft(str(appointment.id), after.current, before.current).changed()
    return AppliedEdit(
        appointment_id=appointment.id,
        moved=moved,
        old_event_id=edit["old_google_event_id"]
        if edit.get("calendar_changed", edit["doctor_changed"])
        else None,
        old_professional_id=edit.get("old_professional_id"),
        notice_id=str(uuid4()) if fields else None,
        notice_version=appointment_email_version(appointment) if fields else None,
        changed_fields=tuple(fields),
    )


async def compensate_appointment_edit(tenant, patient_id, edit: dict) -> None:
    """Restore Calendar to the committed row, or remove only our uncommitted new event.

    Another turn may have won the race. Restore its current committed values rather than
    overwriting that success with our old snapshot. Never remove the old event on failure.
    """
    calendar = edit.get("calendar")
    if calendar is None:
        return
    try:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(Appointment).where(
                    Appointment.id == edit["appointment_id"],
                    Appointment.tenant_id == tenant.id,
                    Appointment.patient_id == patient_id,
                )
            )
            if edit.get("calendar_changed", edit["doctor_changed"]):
                if row is None or row.google_event_id != edit["google_event_id"]:
                    await calendar.cancel_event(edit["google_event_id"])
                return
            if row is None or row.google_event_id != edit["google_event_id"]:
                return
            if not is_live_status(row.status):
                await calendar.cancel_event(row.google_event_id)
                return
            patient = await session.get(Patient, patient_id)
            who = row.attendee_name or (patient.name if patient is not None else None) or "Paciente"
            start, end = as_utc(row.start_at), as_utc(row.end_at)
            summary = f"{row.appointment_type or 'Consulta'} - {who}"
            description = build_event_description(
                service=row.appointment_type,
                insurance=row.insurance,
                attendee_name=row.attendee_name,
            )
        await calendar.update_event_details(
            edit["google_event_id"], start, end, summary, description
        )
    except Exception as exc:
        logger.error(
            "appointment_edit_compensation_failed",
            appointment_id=str(edit["appointment_id"]),
            error_type=type(exc).__name__,
        )


async def _finish_edit(tenant, applied: AppliedEdit) -> None:
    """Delete the old Google event of a doctor change on its own agenda. Never raises."""
    if not applied.old_event_id:
        return
    try:
        async with async_session_factory() as session:
            rows = await list_active_professionals(session, tenant.id)
            target = _appointment_calendar_target(
                {"professional_id": applied.old_professional_id}, rows
            )
            calendar = await _appointment_calendar(session, tenant, target)
        if calendar is None:
            logger.warning(
                "appointment_edit_calendar_missing",
                tenant_id=str(tenant.id),
                appointment_id=str(applied.appointment_id),
            )
            return
        await calendar.cancel_event(applied.old_event_id)
    except Exception as exc:
        logger.warning(
            "appointment_edit_old_event_delete_failed",
            tenant_id=str(tenant.id),
            appointment_id=str(applied.appointment_id),
            error_type=type(exc).__name__,
        )
