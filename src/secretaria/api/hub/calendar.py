"""Doctor hub — calendar platform endpoints (authenticated).

GET   /tenants/me/calendar/events                      - agenda read model.
POST  /tenants/me/calendar/appointments                - create consultation.
POST  /tenants/me/calendar/appointments/{id}/cancel    - cancel + notify patient.
POST  /tenants/me/calendar/appointments/{id}/release    - free an unconfirmed slot.
POST  /tenants/me/calendar/appointments/{id}/reschedule - reschedule + notify.
POST  /tenants/me/calendar/blocks                      - block slot (no notification).
PATCH /tenants/me/calendar/appointments/{id}/status    - mark attended / no-show / etc.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import Row, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import (
    TERMINAL_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentReminder,
    AppointmentStatus,
    Professional,
    Tenant,
)
from secretaria.models.appointment import LIVE_APPOINTMENT_STATUSES
from secretaria.models.patient import Patient
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.schemas.calendar import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentRead,
    AppointmentRelease,
    AppointmentReleaseRead,
    AppointmentReschedule,
    AppointmentStatusUpdate,
    BlockCreate,
    CalendarDepositRead,
    CalendarEventRead,
    CalendarInsurancePlanRead,
    CalendarReminderRead,
    CancelPreviewRead,
)
from secretaria.services import cancellation_notice, reminder_hooks, reminder_schedule
from secretaria.services.appointment_status import (
    CANCEL_REASON_UNCONFIRMED,
    SOURCE_HUB,
    log_status_transition,
)
from secretaria.services.calendar import CalendarService
from secretaria.services.insurance_catalog import AppointmentPlan, load_appointment_plans
from secretaria.services.patient_context import as_utc
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.tenant_config import load_tenant_config, resolve_professional_calendar

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
        confirmation_count=appt.confirmation_count,
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


def _detail(code: str, message: str, **extra) -> dict:
    """A machine-readable error body: the front switches on `code`, shows `message`."""
    return {"code": code, "message": message, **extra}


async def _owning_calendar(session: AsyncSession, tenant: Tenant, appt: Appointment):
    """The Google calendar that owns `appt`'s event.

    A booking made with a professional lives on THAT professional's calendar;
    `cancel_event` treats a 404 as "already gone" (success), so deleting on the
    wrong calendar would silently no-op while the slot stays occupied. When the
    owner cannot be resolved this refuses (409) instead of guessing the tenant
    calendar - same "don't guess, degrade" rule as
    workers/shared/actions.py::_calendar_for_appointment.
    """
    if appt.professional_id is None:
        return await _get_calendar(session, tenant)
    professional = await session.scalar(
        select(Professional).where(
            Professional.id == appt.professional_id, Professional.tenant_id == tenant.id
        )
    )
    unresolved = HTTPException(
        status.HTTP_409_CONFLICT,
        _detail(
            "calendar_unresolved",
            "Não foi possível identificar a agenda do profissional desta consulta.",
        ),
    )
    if professional is None:
        raise unresolved
    try:
        config = await load_tenant_config(session, tenant)
        return await resolve_professional_calendar(
            session, tenant, professional, tenant_config=config
        )
    except Exception as exc:
        logger.warning(
            "release_owning_calendar_failed",
            appointment_id=str(appt.id),
            error_type=type(exc).__name__,
        )
        raise unresolved from exc


# ---------------------------------------------------------------------------
# GET /events — agenda read model
# ---------------------------------------------------------------------------


def _insurance_label(value: str | None) -> str | None:
    """The patient's own convênio answer, trimmed; blank means "no answer"."""
    return (value or "").strip() or None


def _insurance_plan_read(plan: AppointmentPlan | None) -> CalendarInsurancePlanRead | None:
    if plan is None:
        return None
    return CalendarInsurancePlanRead(
        id=str(plan.id), name=plan.name, charge_deposit=plan.charge_deposit
    )


def _deposit_read(
    view: deposit_lifecycle.AppointmentDepositView | None,
) -> CalendarDepositRead | None:
    """The state of the money for the agenda (status VALUE + amount), or None."""
    if view is None:
        return None
    return CalendarDepositRead(status=view.status.value, amount_cents=view.amount_cents)


def _reminder_read(reminder: AppointmentReminder) -> CalendarReminderRead:
    def _opt(value: datetime | None) -> datetime | None:
        return as_utc(value) if value is not None else None

    return CalendarReminderRead(
        kind=reminder.kind,
        status=reminder.status,
        due_at=as_utc(reminder.due_at),
        sent_at=_opt(reminder.sent_at),
        answered_at=_opt(reminder.answered_at),
        answer=reminder.answer,
        warned_at=_opt(reminder.warned_at),
        warn_kind=reminder.warn_kind,
    )


def _current_reminders(row: Row, reminders: list[AppointmentReminder]) -> list[AppointmentReminder]:
    """Only the rows of the appointment's current start, oldest due first."""
    reminders = [r for r in reminders if r.invalidated_at is None]
    if row.start_at is not None:
        current_start = as_utc(row.start_at)
        reminders = [r for r in reminders if as_utc(r.appointment_start_at) == current_start]
    return sorted(reminders, key=lambda r: as_utc(r.due_at))


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
    booked: dict[str, Row] = {}
    if google_ids:
        # The convênio columns ride along on the SAME single query: the plan
        # ids are only needed to resolve names, never sent to the client.
        rows = await session.execute(
            select(
                Appointment.google_event_id,
                Appointment.id,
                Appointment.insurance,
                Appointment.insurance_plan_id,
                Appointment.insurance_professional_plan_id,
                Appointment.status,
                Appointment.confirmation_count,
                Appointment.start_at,
            ).where(
                Appointment.tenant_id == tenant.id,
                Appointment.google_event_id.in_(google_ids),
            )
        )
        booked = {row.google_event_id: row for row in rows.all()}

    # At most ONE query per plan table for the whole page, both tenant-scoped
    # (services/insurance_catalog.py::load_appointment_plans) - a plan id from
    # another clinic never resolves, whatever the appointment row says.
    plans = await load_appointment_plans(
        session,
        tenant.id,
        tenant_plan_ids=[row.insurance_plan_id for row in booked.values()],
        professional_plan_ids=[row.insurance_professional_plan_id for row in booked.values()],
    )
    # ONE query for the whole page's deposit state, also tenant-scoped: a
    # pix_deposits row of another clinic never resolves. Read-only - nothing in
    # the deposit lifecycle is touched by listing the agenda.
    deposits = await deposit_lifecycle.load_deposit_views(
        session, tenant.id, [row.id for row in booked.values()]
    )

    # ONE tenant-scoped query for the whole page's reminder rows (never one per
    # event); rows of another clinic never resolve.
    reminders_by_appointment: dict[UUID, list[AppointmentReminder]] = {}
    if booked:
        reminder_rows = await session.scalars(
            select(AppointmentReminder).where(
                AppointmentReminder.tenant_id == tenant.id,
                AppointmentReminder.invalidated_at.is_(None),
                AppointmentReminder.appointment_id.in_([row.id for row in booked.values()]),
            )
        )
        for reminder in reminder_rows:
            reminders_by_appointment.setdefault(reminder.appointment_id, []).append(reminder)

    reads: list[CalendarEventRead] = []
    for e in events:
        row = booked.get(e["id"])
        plan = (
            plans.resolve(row.insurance_plan_id, row.insurance_professional_plan_id)
            if row is not None
            else None
        )
        current = (
            _current_reminders(row, reminders_by_appointment.get(row.id, []))
            if row is not None
            else None
        )
        state = reminder_schedule.display_state(row, current) if row is not None else None
        reads.append(
            CalendarEventRead(
                id=e["id"],
                summary=e.get("summary"),
                start=e["start"],
                end=e["end"],
                appointment_id=str(row.id) if row is not None else None,
                insurance=_insurance_label(row.insurance) if row is not None else None,
                insurance_plan=_insurance_plan_read(plan),
                deposit=_deposit_read(deposits.get(row.id)) if row is not None else None,
                status=row.status if row is not None else None,
                confirmation_count=row.confirmation_count if row is not None else None,
                display_state=state,
                attention=(state == reminder_schedule.DISPLAY_ATTENTION)
                if state is not None
                else None,
                reminders=[_reminder_read(r) for r in current] if current is not None else None,
            )
        )
    return reads


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
    # TASK-032 R2: plan the reminders of a consultation booked for a known
    # patient (a phone-only booking has nobody the engine can resolve).
    if appt.patient_id is not None and reminder_hooks.enabled_for(tenant):
        await reminder_hooks.after_appointment_booked(appt.id)
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
    if reminder_hooks.enabled_for(tenant):
        await reminder_hooks.after_appointment_closed(appt.id, reason="cancelled")

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
# POST /appointments/{id}/release — free the slot of an unconfirmed appointment
# ---------------------------------------------------------------------------


@router.post("/appointments/{appointment_id}/release", response_model=AppointmentReleaseRead)
async def release_appointment(
    appointment_id: str,
    body: AppointmentRelease,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AppointmentReleaseRead:
    """The clinic frees the slot of an appointment the patient never confirmed.

    Order matters and every step is deliberate:

    1. Guards (404 foreign/malformed, 422 block, 409 not live, 409 confirmed).
    2. Delete the Google event on the OWNING calendar. FAIL CLOSED: if Google
       refuses, answer 502 and change nothing - marking the row cancelled while
       the event still blocks the slot would be the silent half-release this
       endpoint exists to avoid. The delete is idempotent (404/410 = success), so
       a retry after a partial failure is safe.
    3. A guarded `UPDATE ... WHERE status IN live` has exactly one winner when two
       requests race (a double click, two staff); the loser answers 409 and
       neither touches the money nor tells the patient twice.
    4. In the SAME transaction: the deposit outcome and the reminder rows.
    """
    appt = await _get_appointment(session, tenant, appointment_id)
    if appt.patient_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            _detail("not_a_patient_appointment", "Este horário não pertence a um paciente."),
        )
    if appt.status not in LIVE_APPOINTMENT_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            _detail(
                "not_live",
                "Esta consulta já foi cancelada ou encerrada.",
                status=appt.status.value,
            ),
        )
    if appt.confirmation_count >= 1 and not body.release_confirmed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            _detail(
                "already_confirmed",
                "O paciente já confirmou esta consulta.",
                confirmation_count=appt.confirmation_count,
            ),
        )

    # Money first, Google second: a release whose deposit was PAID moves money
    # (retained / partially or fully refunded), so the clinic must have read the
    # consequence. Raised before anything is touched.
    deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appt.id)
    if (
        deposit is not None
        and deposit.status is PixDepositStatus.PAID
        and not body.acknowledge_retention
    ):
        outcome = deposit_lifecycle.preview_cancellation_outcome(tenant, deposit, appt)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            _detail(
                "retention_ack_required",
                deposit_lifecycle.release_warning_text(outcome, tenant, deposit),
                deposit_outcome=outcome,
                amount_cents=deposit.amount_cents,
            ),
        )

    if appt.google_event_id:
        calendar = await _owning_calendar(session, tenant, appt)
        try:
            await calendar.cancel_event(appt.google_event_id)
        except Exception as exc:
            logger.error(
                "calendar_release_delete_failed",
                appointment_id=str(appt.id),
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                _detail(
                    "calendar_unavailable",
                    "Não foi possível apagar o evento no Google Agenda. "
                    "Nada foi alterado; tente de novo em instantes.",
                ),
            ) from exc

    previous_status = appt.status
    now = datetime.now(UTC)
    claimed = await session.execute(
        update(Appointment)
        .where(
            Appointment.id == appt.id,
            Appointment.tenant_id == tenant.id,
            Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
        )
        .values(status=AppointmentStatus.CANCELLED, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            _detail("not_live", "Esta consulta já foi cancelada ou encerrada."),
        )
    await session.refresh(appt)
    log_status_transition(
        appointment_id=appt.id,
        tenant_id=tenant.id,
        old_status=previous_status,
        new_status=AppointmentStatus.CANCELLED,
        source=SOURCE_HUB,
        idempotency_key=f"release:{appt.id}",
        reason=CANCEL_REASON_UNCONFIRMED,
    )
    await reminder_schedule.cancel_reminders(session, appt.id, reason="released")
    deposit_outcome = await deposit_lifecycle.on_appointment_cancelled(
        session, tenant=tenant, appointment=appt, waba_token=None
    )
    await session.commit()
    await session.refresh(appt)

    logger.info(
        "calendar_appointment_released",
        appointment_id=str(appt.id),
        deposit_outcome=deposit_outcome,
    )
    deposit_status = await _deposit_status_value(session, appt.id)
    read = _appointment_read(appt, deposit_status=deposit_status, deposit_outcome=deposit_outcome)
    return AppointmentReleaseRead(**read.model_dump())


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
    # TASK-032 R2: retire the old reminders and plan the new ones; with the
    # switch OFF only when there is a confirmation count to zero.
    if reminder_hooks.enabled_for(tenant) or (appt.confirmation_count or 0) > 0:
        await reminder_hooks.after_appointment_rescheduled(appt.id)

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

    # TASK-032: the reminder schedule follows the status. The status above is
    # still assigned exactly as before (no new transition validation).
    now = datetime.now(UTC)
    if body.status == AppointmentStatus.CONFIRMED:
        await reminder_schedule.register_confirmation(
            session,
            appointment=appt,
            reminder_id=None,
            source=reminder_schedule.CONFIRMATION_SOURCE_STAFF,
            now=now,
        )
    elif body.status == AppointmentStatus.SCHEDULED:
        reminder_schedule.reset_confirmation(appt)
    elif body.status in TERMINAL_APPOINTMENT_STATUSES:
        await reminder_schedule.cancel_reminders(
            session, appt.id, reason=f"status_{body.status.value}"
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
