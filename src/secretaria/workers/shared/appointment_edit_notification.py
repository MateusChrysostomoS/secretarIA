"""A deterministic doctor email for a committed edit, with references-only ARQ args.

Never run booking plugins again: notifying an edit must not repeat Pix/PreCheck.
Queued revisions are revalidated so a later edit/cancellation cannot send stale data
to the wrong doctor. ProcessedEvent claims are per edit, separate from booking mail.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from arq import Retry
from sqlalchemy import select, update

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    Patient,
    Professional,
    ProfessionalEditNotice,
    Tenant,
    Unit,
    is_live_status,
)
from secretaria.plugins.professional_notification import _retry_decision
from secretaria.services.appointment_edit import appointment_email_version
from secretaria.services.brain_professionals import fetch_professional_emails
from secretaria.services.email import EmailOutcome, send_transactional_email_result
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.patient_context import as_utc
from secretaria.workers.shared.channel_policy import policy_for
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


async def _finish_notice(nid, status, reason=None):
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                update(ProfessionalEditNotice)
                .where(
                    ProfessionalEditNotice.id == nid,
                    ProfessionalEditNotice.status == "pending",
                )
                .values(status=status, last_error_code=reason)
            )


async def _retry(ctx, appointment_id, reason, nid=None) -> None:
    defer = _retry_decision(ctx)
    if nid is not None:
        async with async_session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(ProfessionalEditNotice)
                    .where(
                        ProfessionalEditNotice.id == nid,
                    )
                    .with_for_update()
                )
                if row is not None:
                    if row.status != "pending":
                        return
                    row.attempts += 1
                    if row.attempts >= 5:
                        defer = None
                    row.status = "pending" if defer is not None else "failed"
                    row.last_error_code = reason
                    row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=defer or 0)
    logger.warning(
        "professional_edit_email_retry" if defer else "professional_edit_email_abandoned",
        appointment_id=str(appointment_id),
        reason=reason,
    )
    if defer is not None:
        raise Retry(defer=defer)


async def dispatch_pending_professional_edits(ctx: dict, *, now=None) -> None:
    """Recover committed intents after lost enqueue/crash; bounded lease and batch.

    SMTP acceptance followed by a process/commit failure can be delivered again.
    This is recoverable at-least-once, not distributed exactly-once delivery.
    """
    redis = ctx.get("redis")
    if redis is None:
        return
    now = now or datetime.now(UTC)
    async with async_session_factory() as session:
        async with session.begin():
            rows = await session.scalars(
                select(ProfessionalEditNotice)
                .where(
                    ProfessionalEditNotice.status == "pending",
                    ProfessionalEditNotice.next_attempt_at <= now,
                )
                .order_by(ProfessionalEditNotice.next_attempt_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            for row in rows:
                if row.dispatch_attempts >= 24:
                    row.status, row.last_error_code = "failed", "dispatch_budget_exhausted"
                    logger.warning(
                        "professional_edit_email_abandoned",
                        reason=row.last_error_code,
                        appointment_id=str(row.appointment_id),
                    )
                    continue
                row.dispatch_attempts += 1
                row.next_attempt_at = now + timedelta(minutes=2)
                try:
                    await redis.enqueue_job(
                        JOB_NAME,
                        str(row.tenant_id),
                        str(row.appointment_id),
                        str(row.id),
                        row.version,
                        list(row.changed_fields),
                        _job_id=f"profedit:{row.id}:recovery:{row.dispatch_attempts}",
                    )
                except Exception as exc:
                    row.last_error_code = "queue_failed"
                    logger.warning(
                        "professional_edit_email_not_queued",
                        reason="queue_failed",
                        appointment_id=str(row.appointment_id),
                        error_type=type(exc).__name__,
                    )


async def _load_snapshot(session, tid, aid, version, *, lock=False):
    """Fresh tenant-scoped recipient and data, optionally serialized with appointment edits."""
    stmt = select(Appointment).where(Appointment.id == aid, Appointment.tenant_id == tid)
    if lock:
        stmt = stmt.with_for_update()
    appointment = await session.scalar(stmt)
    tenant = await session.get(Tenant, tid)
    if (
        tenant is None
        or appointment is None
        or not is_live_status(appointment.status)
        or appointment.start_at is None
        or appointment.end_at is None
        or as_utc(appointment.start_at) <= datetime.now(UTC)
        or appointment_email_version(appointment) != version
        or appointment.professional_id is None
    ):
        logger.info(
            "professional_edit_email_skipped",
            appointment_id=str(aid),
            reason="appointment_missing_stale_or_incomplete",
        )
        return None
    professional = await session.scalar(
        select(Professional).where(
            Professional.id == appointment.professional_id,
            Professional.tenant_id == tid,
        )
    )
    if professional is None or not professional.is_active:
        logger.info(
            "professional_edit_email_skipped",
            appointment_id=str(aid),
            reason="professional_missing_or_inactive",
        )
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
    # is_active is the WhatsApp go-live flag, not the Portal subscription gate.
    # Match Portal ingress policy; entitlements are checked independently below.
    if not tenant.is_active and (
        patient is None or policy_for(patient.channel).requires_whatsapp_activation
    ):
        logger.info(
            "professional_edit_email_skipped", appointment_id=str(aid), reason="whatsapp_off"
        )
        return None
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
        logger.info("professional_edit_email_skipped", reason="invalid_fields")
        return
    async with async_session_factory() as session:
        record = await session.get(ProfessionalEditNotice, nid)
        if record is not None and (
            record.tenant_id != tid
            or record.appointment_id != aid
            or record.status != "pending"
            or record.version != version
            or record.changed_fields != fields
        ):
            logger.info(
                "professional_edit_email_skipped",
                appointment_id=str(aid),
                reason="notice_mismatch_or_terminal",
            )
            return
        loaded = await _load_snapshot(session, tid, aid, version)
    if loaded is None:
        if record is not None:
            await _finish_notice(nid, "skipped", "appointment_stale_or_incomplete")
        return
    summary = await get_entitlements(tid, ctx.get("redis"))
    if summary is None:
        await _retry(ctx, aid, "entitlement_unavailable", nid if record is not None else None)
        return
    if not (summary.active and summary.secretaria_enabled):
        logger.info("professional_edit_email_skipped", appointment_id=str(aid), reason="unentitled")
        if record is not None:
            await _finish_notice(nid, "skipped", "unentitled")
        return
    emails = await fetch_professional_emails(tid)
    if emails is None:
        await _retry(ctx, aid, "email_lookup_unavailable", nid if record is not None else None)
        return
    key = f"profedit:{tid.hex}:{nid.hex}"
    async with async_session_factory() as session:
        async with session.begin():
            # Lookups can await another service. Reload afterwards and keep the
            # appointment lock through SMTP so concurrent edits take effect either
            # before or after this mail, never halfway through recipient selection.
            loaded = await _load_snapshot(session, tid, aid, version, lock=True)
            locked_notice = await session.scalar(
                select(ProfessionalEditNotice)
                .where(
                    ProfessionalEditNotice.id == nid,
                )
                .with_for_update()
            )
            if locked_notice is not None and locked_notice.status != "pending":
                return
            if loaded is None:
                if locked_notice is not None:
                    locked_notice.status, locked_notice.last_error_code = (
                        "skipped",
                        "appointment_stale",
                    )
                return
            tenant, patient, appointment, professional, unit = loaded
            to = emails.get(str(professional.id))
            if not to:
                logger.info(
                    "professional_edit_email_skipped",
                    appointment_id=str(aid),
                    reason="professional_contact_missing",
                )
                if locked_notice is not None:
                    locked_notice.status, locked_notice.last_error_code = (
                        "skipped",
                        "professional_contact_missing",
                    )
                return
            variables = _variables(tenant, patient, appointment, professional, unit, fields)
            if locked_notice is None and not await _claim_event(key):
                return
            outcome = await send_transactional_email_result(
                to=to,
                template="appointment_changed_professional",
                variables=variables,
            )
            if locked_notice is not None:
                if outcome is EmailOutcome.SENT:
                    locked_notice.status, locked_notice.last_error_code = "sent", None
                elif not outcome.is_transient:
                    locked_notice.status, locked_notice.last_error_code = "skipped", outcome.value
    if outcome is EmailOutcome.SENT:
        logger.info(
            "professional_edit_email_sent",
            tenant_id=str(tid),
            appointment_id=str(aid),
            professional_id=str(professional.id),
            changed_count=len(fields),
        )
        return
    if record is None:
        await _release_event(key, event="professional_edit_email_release_failed")
    if outcome.is_transient:
        await _retry(ctx, aid, outcome.value, nid if record is not None else None)
