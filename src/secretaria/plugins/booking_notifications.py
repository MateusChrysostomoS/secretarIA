"""CORE post_booking hook: confirm a booking to the clinic and to the Portal patient.

Complements `plugins/professional_notification.py` (which mails the doctor):
  * the CLINIC (`Tenant.contact_email`) gets a record of every booking;
  * a PORTAL patient gets a confirmation e-mail AND a Google Calendar invite.

The patient's real address is asked of brain-api for THIS booking
(`services/brain_patients.py`); when brain-api answers it wins and refreshes the
local copy (`Patient.email`), and when it is down the stored copy is used. That
column is registered in the pseudonymizer, so the LLM can never see the value;
this module never logs it. Every step is
independent and best-effort — no e-mail outage, missing address or Google
refusal may disturb the already-committed booking (`registry.run_post_booking`
also contains hook exceptions). Each destination is claimed through
`ProcessedEvent` (`bookingnotif:<kind>:<appointment_id>`) so a re-run of the hook
never mails or invites twice. Nothing here logs an address or patient text.
"""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Patient, ProcessedEvent, Professional
from secretaria.plugins.base import PluginSpec, PostBookingContext
from secretaria.plugins.professional_notification import _as_utc, _local_when
from secretaria.plugins.registry import register
from secretaria.services.brain_patients import fetch_patient_email_result
from secretaria.services.calendar import CalendarService, build_patient_calendar_link
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome, send_transactional_email_result
from secretaria.services.insurance_catalog import load_appointment_plans
from secretaria.services.tenant_config import load_tenant_config, resolve_professional_calendar

logger = get_logger(__name__)

PATIENT_TEMPLATE = "appointment_booked_patient"
CLINIC_TEMPLATE = "appointment_booked_clinic"


def _key(kind: str, appointment_id: UUID) -> str:
    return f"bookingnotif:{kind}:{appointment_id}"


async def _claim(key: str) -> bool:
    async with async_session_factory() as session:
        try:
            async with session.begin():
                if await session.scalar(
                    select(ProcessedEvent.id).where(ProcessedEvent.event_id == key)
                ):
                    return False
                session.add(ProcessedEvent(event_id=key))
        except IntegrityError:
            return False
    return True


async def _release(key: str) -> None:
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == key))
    except Exception as exc:
        logger.warning("booking_notification_release_failed", error_type=type(exc).__name__)


async def _insurance_line(tenant, appointment) -> str:
    """Only configured, tenant-scoped plan rows may supply an email's plan name."""
    insurance = (getattr(appointment, "insurance", None) or "").strip()
    tenant_plan_id = getattr(appointment, "insurance_plan_id", None)
    professional_plan_id = getattr(appointment, "insurance_professional_plan_id", None)
    if tenant_plan_id is not None or professional_plan_id is not None:
        try:
            async with async_session_factory() as session:
                plans = await load_appointment_plans(
                    session,
                    tenant.id,
                    tenant_plan_ids=[tenant_plan_id],
                    professional_plan_ids=[professional_plan_id],
                )
            plan = plans.resolve(tenant_plan_id, professional_plan_id)
            if plan is not None:
                return f"Convênio: {plan.name}\n"
        except Exception as exc:
            logger.warning("booking_notification_plan_lookup_failed", error_type=type(exc).__name__)
    # Legacy catalog strings and unmatched patient answers are untrusted text.
    return "Convênio informado; consulte o agendamento.\n" if insurance else ""


def _variables(tenant, patient, appointment, insurance_line: str) -> dict:
    return {
        "clinic_name": tenant.clinic_name,
        "appointment_id": str(appointment.id),
        "service": appointment.appointment_type or "Consulta",
        "when": _local_when(appointment.start_at, tenant.timezone),
        "insurance_line": insurance_line,
    }


async def _invite_patient(tenant, appointment, email: str) -> None:
    """Add the patient to the booking's Google event (Google sends the invite)."""
    if not appointment.google_event_id:
        return
    async with async_session_factory() as session:
        tenant_config = await load_tenant_config(session, tenant)
        calendar = None
        if appointment.professional_id is not None:
            professional = await session.scalar(
                select(Professional).where(
                    Professional.id == appointment.professional_id,
                    Professional.tenant_id == tenant.id,
                )
            )
            if professional is not None:
                calendar = await resolve_professional_calendar(
                    session, tenant, professional, tenant_config=tenant_config
                )
        if calendar is None and tenant_config is not None:
            calendar = CalendarService.from_tenant_config(tenant_config)
    if calendar is None:
        return
    await calendar.add_attendee(appointment.google_event_id, email)


async def _remember_email(patient, email: str | None) -> None:
    """Keep the local copy of the patient's e-mail current (`Patient.email`).

    The column is registered in `load_pseudonymizer`, which is what guarantees the
    LLM never reads it. Best-effort and never logged: a failure only means the
    next booking asks brain-api again.
    """
    patient_id = getattr(patient, "id", None)
    if patient_id is None:
        return
    try:
        async with async_session_factory() as session:
            async with session.begin():
                row = await session.get(Patient, patient_id)
                if row is not None:
                    row.email = email
    except Exception as exc:
        logger.warning("patient_email_remember_failed", error_type=type(exc).__name__)


async def _send_once(
    kind: str, appointment_id: UUID, to: str, template: str, variables: dict
) -> None:
    key = _key(kind, appointment_id)
    if not await _claim(key):
        return
    try:
        outcome = await send_transactional_email_result(
            to=to, template=template, variables=variables
        )
    except Exception:
        await _release(key)
        raise
    if outcome is not EmailOutcome.SENT:
        await _release(key)
        logger.warning(
            "booking_notification_not_sent",
            kind=kind,
            appointment_id=str(appointment_id),
            reason=outcome.value,
        )


async def _post_booking(ctx: PostBookingContext) -> None:
    tenant, patient, appointment = ctx.tenant, ctx.patient, ctx.appointment
    insurance_line = await _insurance_line(tenant, appointment)
    variables = _variables(tenant, patient, appointment, insurance_line)

    clinic_email = (getattr(tenant, "contact_email", None) or "").strip()
    if clinic_email:
        try:
            await _send_once("clinic", appointment.id, clinic_email, CLINIC_TEMPLATE, variables)
        except Exception as exc:
            logger.warning("booking_notification_clinic_failed", error_type=type(exc).__name__)

    if patient is None or getattr(patient, "channel", None) != CHANNEL_BRAIN_MESSAGE:
        return
    external_id = getattr(patient, "external_id", None)
    if not external_id:
        return
    try:
        fetched = await fetch_patient_email_result(tenant.id, external_id)
        stored = (getattr(patient, "email", None) or "").strip() or None
        # brain-api is the identity authority: when it answers, it wins and the
        # local copy follows; when it is down, the stored copy still works.
        email = fetched.email if fetched.available else stored
        if fetched.available and fetched.email != stored:
            await _remember_email(patient, fetched.email)
        if not email:
            return
        if await _claim(_key("invite", appointment.id)):
            try:
                await _invite_patient(tenant, appointment, email)
            except Exception as exc:
                await _release(_key("invite", appointment.id))
                logger.warning(
                    "booking_notification_invite_failed", error_type=type(exc).__name__
                )
        link = build_patient_calendar_link(
            _as_utc(appointment.start_at),
            _as_utc(appointment.end_at),
            appointment.appointment_type or "Consulta",
        )
        patient_vars = {**variables, "calendar_line": f"Adicionar à sua agenda:\n{link}\n\n"}
        await _send_once("patient", appointment.id, email, PATIENT_TEMPLATE, patient_vars)
    except Exception as exc:
        logger.warning("booking_notification_patient_failed", error_type=type(exc).__name__)


BOOKING_NOTIFICATIONS_SPEC = PluginSpec(
    id="booking_notifications",
    entitlement_keys=(),
    post_booking=_post_booking,
)
register(BOOKING_NOTIFICATIONS_SPEC)
