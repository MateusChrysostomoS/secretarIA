"""Doctor hub — calendar platform endpoints (authenticated).

GET   /tenants/me/calendar/events                      - agenda read model.
POST  /tenants/me/calendar/appointments                - create consultation.
POST  /tenants/me/calendar/appointments/{id}/cancel    - cancel + notify patient.
POST  /tenants/me/calendar/appointments/{id}/reschedule - reschedule + notify.
POST  /tenants/me/calendar/blocks                      - block slot (no notification).
PATCH /tenants/me/calendar/appointments/{id}/status    - mark attended / no-show / etc.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, AppointmentStatus, Tenant
from secretaria.models.patient import Patient
from secretaria.schemas.calendar import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentRead,
    AppointmentReschedule,
    AppointmentStatusUpdate,
    BlockCreate,
    CalendarEventRead,
)
from secretaria.services.calendar import CalendarService
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.tenant_config import load_tenant_config

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/calendar", tags=["hub-calendar"])


def _appointment_read(
    appt: Appointment,
    *,
    deposit_status: str | None = None,
    deposit_outcome: str | None = None,
) -> AppointmentRead:
    return AppointmentRead(
        id=str(appt.id),
        tenant_id=str(appt.tenant_id),
        patient_id=str(appt.patient_id) if appt.patient_id else None,
        conversation_id=str(appt.conversation_id) if appt.conversation_id else None,
        google_event_id=appt.google_event_id,
        google_event_link=appt.google_event_link,
        appointment_type=appt.appointment_type,
        start_at=appt.start_at,
        end_at=appt.end_at,
        phone=appt.phone,
        status=appt.status,
        created_at=appt.created_at,
        updated_at=appt.updated_at,
        deposit_status=deposit_status,
        deposit_outcome=deposit_outcome,
    )


async def _deposit_status_value(session: AsyncSession, appointment_id: UUID) -> str | None:
    """The PixDeposit status VALUE for one appointment, or None when there is
    no deposit at all. A single indexed lookup — every hub-calendar endpoint
    acts on exactly ONE appointment, so this is trivially free of the N+1
    concern a real list endpoint would have to guard against."""
    deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment_id)
    return deposit.status.value if deposit is not None else None


async def _get_calendar(session: AsyncSession, tenant: Tenant) -> CalendarService:
    """Load a per-tenant CalendarService. Raises 422 when Calendar is not connected."""
    config = await load_tenant_config(session, tenant)
    if not config.google_refresh_token:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Google Calendar not connected. Complete OAuth onboarding first.",
        )
    return CalendarService.from_tenant_config(config)


async def _get_appointment(
    session: AsyncSession, tenant: Tenant, appointment_id: str
) -> Appointment:
    """Load an appointment by id, scoped to the tenant. Raises 404 if not found."""
    try:
        appt_uuid = UUID(appointment_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found") from None
    appt = await session.scalar(
        select(Appointment).where(
            Appointment.id == appt_uuid,
            Appointment.tenant_id == tenant.id,
        )
    )
    if appt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    return appt


# ---------------------------------------------------------------------------
# GET /events — agenda read model
# ---------------------------------------------------------------------------


@router.get("/events", response_model=list[CalendarEventRead])
async def list_events(
    start: datetime,
    end: datetime,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[CalendarEventRead]:
    cal = await _get_calendar(session, tenant)
    events = await cal.check_availability(start, end)
    return [
        CalendarEventRead(
            id=e["id"],
            summary=e.get("summary"),
            start=e["start"],
            end=e["end"],
        )
        for e in events
    ]


# ---------------------------------------------------------------------------
# POST /appointments — create consultation with patient linking
# ---------------------------------------------------------------------------


@router.post("/appointments", response_model=AppointmentRead, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    body: AppointmentCreate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AppointmentRead:
    cal = await _get_calendar(session, tenant)
    event = await cal.create_event(
        start=body.start,
        end=body.end,
        summary=body.summary,
        description=body.description,
    )
    google_event_id = event.get("id", "")

    patient_uuid: UUID | None = None
    phone = body.phone
    if body.patient_id:
        try:
            patient_uuid = UUID(body.patient_id)
            # Resolve phone from Patient row if not explicitly provided.
            if not phone:
                patient = await session.scalar(
                    select(Patient).where(
                        Patient.id == patient_uuid,
                        Patient.tenant_id == tenant.id,
                    )
                )
                if patient:
                    phone = patient.wa_id
        except ValueError:
            pass

    appt = Appointment(
        tenant_id=tenant.id,
        patient_id=patient_uuid,
        google_event_id=google_event_id,
        google_event_link=event.get("htmlLink"),
        start_at=body.start,
        end_at=body.end,
        phone=phone,
        status=AppointmentStatus.SCHEDULED,
    )
    session.add(appt)
    await session.commit()
    await session.refresh(appt)
    logger.info(
        "calendar_appointment_created",
        appointment_id=str(appt.id),
        google_event_id=google_event_id,
    )
    return _appointment_read(appt)


# ---------------------------------------------------------------------------
# POST /blocks — block a time slot without patient notification
# ---------------------------------------------------------------------------


@router.post("/blocks", response_model=AppointmentRead, status_code=status.HTTP_201_CREATED)
async def create_block(
    body: BlockCreate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AppointmentRead:
    cal = await _get_calendar(session, tenant)
    event = await cal.create_event(
        start=body.start,
        end=body.end,
        summary=body.summary,
        description=body.description,
    )
    appt = Appointment(
        tenant_id=tenant.id,
        patient_id=None,
        google_event_id=event.get("id", ""),
        google_event_link=event.get("htmlLink"),
        appointment_type="Bloqueado",
        start_at=body.start,
        end_at=body.end,
        phone=None,
        status=AppointmentStatus.SCHEDULED,
    )
    session.add(appt)
    await session.commit()
    await session.refresh(appt)
    logger.info("calendar_block_created", appointment_id=str(appt.id))
    return _appointment_read(appt)


# ---------------------------------------------------------------------------
# POST /appointments/{id}/cancel — cancel + optionally notify patient
# ---------------------------------------------------------------------------


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentRead)
async def cancel_appointment(
    appointment_id: str,
    body: AppointmentCancel,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AppointmentRead:
    if not body.confirm:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "confirm must be true")

    appt = await _get_appointment(session, tenant, appointment_id)
    if appt.status == AppointmentStatus.CANCELLED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Appointment already cancelled")

    cal = await _get_calendar(session, tenant)
    await cal.cancel_event(appt.google_event_id)

    appt.status = AppointmentStatus.CANCELLED
    appt.updated_at = datetime.now(UTC)

    # Money hook (PROMPT S3 section 4): resolve the deposit's outcome for
    # this cancellation in the SAME transaction. waba_token=None — the
    # lifecycle sends no message itself (see its docstring); this endpoint
    # decides whether/how to notify via the existing custom_message path.
    deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(
        session, tenant=tenant, appointment=appt, waba_token=None
    )
    notice: str | None = None
    if deposit_outcome is not None:
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
        if deposit is not None:
            notice = deposit_lifecycle.cancellation_notice(deposit_outcome, tenant, deposit)

    await session.commit()
    await session.refresh(appt)

    # Enqueue patient notification if we have a phone and a message to send.
    # The honest deposit notice (when any) rides along with a custom message
    # — never sent on its own (this endpoint's notification path has always
    # been opt-in via custom_message, and stays that way).
    message_body = body.custom_message
    if message_body and notice:
        message_body = f"{message_body}\n\n{notice}"
    if appt.phone and message_body:
        arq_pool = getattr(request.app.state, "arq_pool", None)
        if arq_pool:
            await arq_pool.enqueue_job(
                "send_patient_notification",
                str(tenant.id),
                appt.phone,
                message_body,
            )

    logger.info(
        "calendar_appointment_cancelled",
        appointment_id=str(appt.id),
        deposit_outcome=deposit_outcome,
    )
    deposit_status = await _deposit_status_value(session, appt.id)
    return _appointment_read(appt, deposit_status=deposit_status, deposit_outcome=deposit_outcome)


# ---------------------------------------------------------------------------
# POST /appointments/{id}/reschedule — move event + optionally notify
# ---------------------------------------------------------------------------


@router.post("/appointments/{appointment_id}/reschedule", response_model=AppointmentRead)
async def reschedule_appointment(
    appointment_id: str,
    body: AppointmentReschedule,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AppointmentRead:
    appt = await _get_appointment(session, tenant, appointment_id)
    if appt.status == AppointmentStatus.CANCELLED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot reschedule a cancelled appointment")

    cal = await _get_calendar(session, tenant)
    await cal.update_event(appt.google_event_id, body.new_start, body.new_end)

    # BUGFIX (PROMPT S3): this endpoint moved the Calendar event but never
    # mirrored the new window onto the platform row — start_at/end_at stayed
    # stale forever after a doctor-initiated reschedule. Mirrors the
    # deterministic flow's own reschedule persist (workers/tasks.py
    # ::_apply_flow_result).
    appt.start_at = body.new_start
    appt.end_at = body.new_end
    appt.status = AppointmentStatus.RESCHEDULED
    appt.updated_at = datetime.now(UTC)
    # Deliberately NOT calling deposit_lifecycle.register_reschedule here:
    # this is a DOCTOR-initiated move (the hub), not a patient-initiated one
    # — it must never consume the patient's own pix_reschedule_limit
    # allowance. The existing deposit (if any) simply carries over untouched,
    # exactly like every other reschedule (see models/pix_deposit.py's
    # module docstring on why a reschedule never re-points the deposit's FK).
    await session.commit()
    await session.refresh(appt)

    if appt.phone and body.custom_message:
        arq_pool = getattr(request.app.state, "arq_pool", None)
        if arq_pool:
            await arq_pool.enqueue_job(
                "send_patient_notification",
                str(tenant.id),
                appt.phone,
                body.custom_message,
            )

    logger.info("calendar_appointment_rescheduled", appointment_id=str(appt.id))
    deposit_status = await _deposit_status_value(session, appt.id)
    return _appointment_read(appt, deposit_status=deposit_status)


# ---------------------------------------------------------------------------
# PATCH /appointments/{id}/status — mark confirmed / attended / no-show
# ---------------------------------------------------------------------------


@router.patch("/appointments/{appointment_id}/status", response_model=AppointmentRead)
async def update_appointment_status(
    appointment_id: str,
    body: AppointmentStatusUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AppointmentRead:
    appt = await _get_appointment(session, tenant, appointment_id)
    appt.status = body.status
    appt.updated_at = datetime.now(UTC)

    # Money hooks (PROMPT S3 section 4): PATCH doesn't touch Google Calendar
    # today (unchanged) — but a CANCELLED/NO_SHOW status transition is still
    # a real money event for a Pix deposit, exactly like the dedicated
    # POST /cancel endpoint or a no-show marked from any other surface.
    deposit_outcome: str | None = None
    if body.status == AppointmentStatus.CANCELLED:
        deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appt, waba_token=None
        )
    elif body.status == AppointmentStatus.NO_SHOW:
        deposit_outcome = await deposit_lifecycle.on_no_show(
            session, tenant=tenant, appointment=appt
        )

    await session.commit()
    await session.refresh(appt)
    logger.info(
        "calendar_appointment_status_updated",
        appointment_id=str(appt.id),
        status=body.status.value,
        deposit_outcome=deposit_outcome,
    )
    deposit_status = await _deposit_status_value(session, appt.id)
    return _appointment_read(appt, deposit_status=deposit_status, deposit_outcome=deposit_outcome)
