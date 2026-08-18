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
from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, AppointmentStatus, Professional, Tenant
from secretaria.models.patient import Patient
from secretaria.schemas.calendar import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentRead,
    AppointmentReschedule,
    AppointmentStatusUpdate,
    BlockCreate,
    CalendarEventRead,
    CancelPreviewRead,
)
from secretaria.services import cancellation_notice
from secretaria.services.appointment_status import SOURCE_HUB, log_status_transition
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


async def _professional_name(
    session: AsyncSession, tenant: Tenant, appt: Appointment
) -> str | None:
    """Display name of the professional who owns `appt`, scoped to `tenant`.

    `None` when the appointment has no owner (a clinic with zero or 2+ active
    professionals and no explicit pick) or when the id does not belong to this
    clinic — the patient-facing notice then falls back to "responsável" rather
    than naming somebody else's doctor.
    """
    if appt.professional_id is None:
        return None
    return await session.scalar(
        select(Professional.name).where(
            Professional.id == appt.professional_id,
            Professional.tenant_id == tenant.id,
        )
    )


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

    # Attach the LOCAL Appointment.id to every event that has one, so the
    # agenda can call cancel/reschedule (which key on it) instead of only
    # being able to display. See CalendarEventRead's docstring.
    #
    # ONE query for the whole page, joined in memory — not one lookup per
    # event. A month of a busy clinic is hundreds of events, and the N+1
    # version would put that many round trips behind a screen the doctor
    # opens constantly.
    #
    # The `tenant_id` filter is the isolation invariant, not decoration: it is
    # what stops a google_event_id from another clinic's calendar (a shared or
    # mis-configured Google account) resolving to that clinic's appointment
    # and handing this doctor a working cancel button for someone else's
    # patient.
    google_ids = [e["id"] for e in events if e.get("id")]
    local_ids: dict[str, str] = {}
    if google_ids:
        rows = await session.execute(
            select(Appointment.google_event_id, Appointment.id).where(
                Appointment.tenant_id == tenant.id,
                Appointment.google_event_id.in_(google_ids),
            )
        )
        local_ids = {google_id: str(appt_id) for google_id, appt_id in rows.all()}

    return [
        CalendarEventRead(
            id=e["id"],
            summary=e.get("summary"),
            start=e["start"],
            end=e["end"],
            appointment_id=local_ids.get(e["id"]),
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


@router.get(
    "/appointments/{appointment_id}/cancel-preview",
    response_model=CancelPreviewRead,
)
async def cancel_preview(
    appointment_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> CancelPreviewRead:
    """What cancelling this appointment would cost, before anything happens.

    Read-only. Exists because the honest answer to "will the patient be told?"
    depends on something the hub cannot see: whether the patient wrote within
    Meta's last 24h. Outside that window WhatsApp accepts no free-form message
    at all and a notification means a BILLED template — so the doctor is shown
    the price and the free alternative (writing from their own phone) instead
    of being charged silently, or worse, being told "avisado" when nothing
    could be sent.
    """
    appt = await _get_appointment(session, tenant, appointment_id)
    settings = get_settings()

    last_inbound = None
    if appt.patient_id is not None:
        last_inbound = await cancellation_notice.last_inbound_at(
            session, tenant.id, appt.patient_id
        )

    return CancelPreviewRead(
        inside_window=cancellation_notice.is_inside_window(last_inbound),
        professional_name=await _professional_name(session, tenant, appt),
        template_cost_brl=settings.CANCEL_TEMPLATE_COST_BRL,
        cost_is_estimate=settings.CANCEL_TEMPLATE_COST_IS_ESTIMATE,
        whatsapp_link=cancellation_notice.whatsapp_deep_link(appt.phone),
    )


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

    previous_status = appt.status
    appt.status = AppointmentStatus.CANCELLED
    appt.updated_at = datetime.now(UTC)
    log_status_transition(
        appointment_id=appt.id,
        tenant_id=tenant.id,
        old_status=previous_status,
        new_status=AppointmentStatus.CANCELLED,
        source=SOURCE_HUB,
        idempotency_key=f"cancel:{appt.id}",
    )

    # Who the patient is told cancelled. Tenant-scoped like every other read
    # here: a professional_id pointing outside this clinic resolves to None and
    # the notice says "responsável" rather than naming another clinic's doctor.
    professional_name = await _professional_name(session, tenant, appt)

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

    # Notify the patient. UNCONDITIONAL now — this used to fire only when the
    # doctor typed something, so a blank box meant the patient found out by
    # turning up to a consultation that no longer existed. The body is composed
    # server-side (services/cancellation_notice.py); the doctor's text is a
    # justification quoted inside it, not the message.
    #
    # The honest deposit notice, when there is one, rides along rather than
    # arriving as a second message.
    if appt.phone:
        arq_pool = getattr(request.app.state, "arq_pool", None)
        if arq_pool:
            await arq_pool.enqueue_job(
                "send_cancellation_notice",
                str(tenant.id),
                str(appt.id),
                professional_name,
                body.justification,
                notice,
                body.notify_outside_window,
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
    # The SAME booking, moved - RESCHEDULED is a LIVE status, not a tombstone
    # (PROMPT_FIX_16, taxonomy on models/appointment.py). The row keeps its id,
    # its google_event_id and its deposit, so it stays upcoming, manageable and
    # remindable at the NEW window.
    previous_status = appt.status
    appt.status = AppointmentStatus.RESCHEDULED
    appt.updated_at = datetime.now(UTC)
    log_status_transition(
        appointment_id=appt.id,
        tenant_id=tenant.id,
        old_status=previous_status,
        new_status=AppointmentStatus.RESCHEDULED,
        source=SOURCE_HUB,
        idempotency_key=f"resched:{appt.google_event_id}:{body.new_start.isoformat()}",
    )
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
    previous_status = appt.status
    appt.status = body.status
    appt.updated_at = datetime.now(UTC)
    log_status_transition(
        appointment_id=appt.id,
        tenant_id=tenant.id,
        old_status=previous_status,
        new_status=body.status,
        source=SOURCE_HUB,
        idempotency_key=f"status:{appt.id}:{body.status.value}",
    )

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
