"""A deterministic doctor email for a committed edit, with references-only ARQ args.

Never run booking plugins again: notifying an edit must not repeat Pix/PreCheck.
Queued revisions are revalidated so a later edit/cancellation cannot send stale data
to the wrong doctor. ProcessedEvent claims are per edit, separate from booking mail.
"""

from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from arq import Retry
from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, Professional, Tenant, Unit, is_live_status
from secretaria.plugins.professional_notification import _retry_decision
from secretaria.services.appointment_edit import appointment_email_version
from secretaria.services.brain_professionals import fetch_professional_emails
from secretaria.services.email import EmailOutcome, send_transactional_email_result
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.patient_context import as_utc
from secretaria.workers.shared.jobs import _claim_event, _release_event

logger = get_logger(__name__)
JOB_NAME = "send_professional_edit_notification"
_FIELDS = ("data", "horário", "serviço", "médico", "convênio", "paciente")


async def enqueue_professional_edit_notification(redis, tenant_id, applied) -> None:
    """Best-effort after commit; an unavailable queue never costs the edited booking."""
    if not applied.notice_id or not applied.changed_fields:
        return
    if redis is None:
        logger.warning(
            "professional_edit_email_not_queued",
            reason="no_redis",
            appointment_id=str(applied.appointment_id),
        )
        return
    try:
        await redis.enqueue_job(
            JOB_NAME,
            str(tenant_id),
            str(applied.appointment_id),
            applied.notice_id,
            applied.notice_version,
            list(applied.changed_fields),
            _job_id=f"profedit:{applied.notice_id}",
        )
    except Exception as exc:
        logger.warning(
            "professional_edit_email_not_queued",
            reason="queue_failed",
            appointment_id=str(applied.appointment_id),
            error_type=type(exc).__name__,
        )


def _variables(tenant, patient, appointment, professional, unit, fields) -> dict:
    tz = ZoneInfo(tenant.timezone or "America/Sao_Paulo")
    start, end = (
        as_utc(appointment.start_at).astimezone(tz),
        as_utc(appointment.end_at).astimezone(tz),
    )
    patient_name = (patient.name if patient is not None else None) or "Paciente"
    attendee = appointment.attendee_name or patient_name
    insurance = appointment.insurance or "não informado"
    service = appointment.appointment_type or "Consulta"
    values = {
        "data": start.strftime("%d/%m/%Y"),
        "horário": start.strftime("%H:%M"),
        "serviço": service,
        "médico": professional.name,
        "convênio": insurance,
        "paciente": attendee,
    }
    labels = {
        "data": "Data",
        "horário": "Horário",
        "serviço": "Serviço",
        "médico": "Médico",
        "convênio": "Convênio",
        "paciente": "Paciente",
    }
    changes = "\n".join(
        f"• {labels[field]}: {values[field]}" for field in _FIELDS if field in fields
    )
    agenda = (get_settings().DOCTOR_AGENDA_URL or "").strip()
    return {
        "clinic_name": tenant.clinic_name,
        "professional_name": professional.name,
        "attendee_name": attendee,
        "patient_name": patient_name,
        "service": service,
        "insurance": insurance,
        "date": values["data"],
        "time": values["horário"],
        "end_time": end.strftime("%H:%M"),
        "when": start.strftime("%d/%m/%Y às %H:%M"),
        "changes": changes,
        "unit_line": (
            f"Unidade: {unit.name}\n" + (f"Endereço: {unit.address}\n" if unit.address else "")
        )
        if unit is not None
        else "",
        "agenda_line": f"Ver na agenda:\n{agenda}\n\n" if agenda else "",
        "calendar_line": f"Ver no Google Agenda:\n{appointment.google_event_link}\n\n"
        if appointment.google_event_link
        else "",
    }


def _retry(ctx, appointment_id, reason) -> None:
    defer = _retry_decision(ctx)
    logger.warning(
        "professional_edit_email_retry" if defer else "professional_edit_email_abandoned",
        appointment_id=str(appointment_id),
        reason=reason,
    )
    if defer is not None:
        raise Retry(defer=defer)


async def _load_snapshot(session, tid, aid, version, *, lock=False):
    """Fresh tenant-scoped recipient and data, optionally serialized with appointment edits."""
    stmt = select(Appointment).where(Appointment.id == aid, Appointment.tenant_id == tid)
    if lock:
        stmt = stmt.with_for_update()
    appointment = await session.scalar(stmt)
    tenant = await session.get(Tenant, tid)
    if (
        tenant is None
        or not tenant.is_active
        or appointment is None
        or not is_live_status(appointment.status)
        or appointment.start_at is None
        or appointment.end_at is None
        or as_utc(appointment.start_at) <= datetime.now(UTC)
        or appointment_email_version(appointment) != version
        or appointment.professional_id is None
    ):
        return None
    professional = await session.scalar(
        select(Professional).where(
            Professional.id == appointment.professional_id,
            Professional.tenant_id == tid,
        )
    )
    if professional is None or not professional.is_active:
        return None
    patient = (
        await session.scalar(
            select(Patient).where(
                Patient.id == appointment.patient_id,
                Patient.tenant_id == tid,
            )
        )
        if appointment.patient_id
        else None
    )
    unit = (
        await session.scalar(
            select(Unit).where(
                Unit.id == appointment.unit_id,
                Unit.tenant_id == tid,
            )
        )
        if appointment.unit_id
        else None
    )
    return tenant, patient, appointment, professional, unit


async def send_professional_edit_notification(
    ctx: dict,
    tenant_id: str,
    appointment_id: str,
    notice_id: str,
    version: str,
    fields: list[str],
) -> None:
    """One confirmed edit; failures retry, duplicate deliveries never resend a success."""
    try:
        tid, aid, nid = UUID(tenant_id), UUID(appointment_id), UUID(notice_id)
    except ValueError:
        logger.warning("professional_edit_email_skipped", reason="invalid_refs")
        return
    if not fields or any(field not in _FIELDS for field in fields):
        return
    async with async_session_factory() as session:
        loaded = await _load_snapshot(session, tid, aid, version)
    if loaded is None:
        return
    summary = await get_entitlements(tid, ctx.get("redis"))
    if summary is None:
        _retry(ctx, aid, "entitlement_unavailable")
        return
    if not (summary.active and summary.secretaria_enabled):
        return
    emails = await fetch_professional_emails(tid)
    if emails is None:
        _retry(ctx, aid, "email_lookup_unavailable")
        return
    key = f"profedit:{tid.hex}:{nid.hex}"
    async with async_session_factory() as session:
        async with session.begin():
            # Lookups can await another service. Reload afterwards and keep the
            # appointment lock through SMTP so concurrent edits take effect either
            # before or after this mail, never halfway through recipient selection.
            loaded = await _load_snapshot(session, tid, aid, version, lock=True)
            if loaded is None:
                return
            tenant, patient, appointment, professional, unit = loaded
            to = emails.get(str(professional.id))
            if not to:
                return
            variables = _variables(tenant, patient, appointment, professional, unit, fields)
            if not await _claim_event(key):
                return
            outcome = await send_transactional_email_result(
                to=to,
                template="appointment_changed_professional",
                variables=variables,
            )
    if outcome is EmailOutcome.SENT:
        logger.info(
            "professional_edit_email_sent",
            tenant_id=str(tid),
            appointment_id=str(aid),
            professional_id=str(professional.id),
            changed_count=len(fields),
        )
        return
    await _release_event(key, event="professional_edit_email_release_failed")
    if outcome.is_transient:
        _retry(ctx, aid, outcome.value)
