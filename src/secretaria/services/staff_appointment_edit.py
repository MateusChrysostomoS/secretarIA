"""The clinic's "Editar/Remarcar" on one appointment (TASK-032 R7, spec 2026-10-09 §1/§3/§4).

One action changes any of: date/time, service (and with it the duration), doctor,
convênio, who the appointment is for (`attendee_name`) and the appointment's contact
phone. In order:

1. Resolve and validate every requested value against THIS clinic - a foreign or
   inactive professional, a service the doctor does not offer, a start in the past or a
   malformed phone is 422 - and refuse a request that changes nothing.
2. Google first, FAIL CLOSED: patch the event on its own agenda, or - when the doctor
   moves to another agenda - create it there. If Google refuses, nothing in the database
   changed (502). The slot must be free and inside the hours unless the clinic says
   `allow_overlap` (an "encaixe"). A contact-phone-only change never calls Google (the
   event carries no phone).
3. The row, locked, through the SAME writer the patient's "Alterar Dados" uses
   (services/appointment_edit_write.py), plus the contact phone; the doctor e-mail goes
   to R6's outbox only when date, time or doctor changed. A row that stopped being live -
   or was edited by someone else - meanwhile undoes step 2 (409).
4. After the commit, the old event of a doctor move is deleted from the old agenda (best
   effort, like R6's `_finish_edit`).

The hub route does the rest after this returns: the reminders replan, the doctor e-mail
enqueue and the patient notice. Never touches the Pix deposit (a clinic move does not
consume the patient's reschedule allowance - same rule as POST /reschedule) nor the
patient's identity (`Patient.wa_id`, e-mail, `Patient.name`). Logs carry ids and codes.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, Professional, Tenant, is_live_status
from secretaria.services.appointment_calendar_origin import calendar_professional_id
from secretaria.services.appointment_edit import appointment_email_version
from secretaria.services.appointment_edit_write import write_appointment_edit
from secretaria.services.appointment_status import SOURCE_HUB
from secretaria.services.calendar import CalendarUnavailableError, build_event_description
from secretaria.services.patient_context import as_utc
from secretaria.services.professional_edit_outbox import record_professional_edit
from secretaria.services.service_catalog import (
    load_service_catalog,
    normalize as normalize_service_name,
)
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
)

logger = get_logger(__name__)

EDITABLE_FIELDS: tuple[str, ...] = (
    "start_at",
    "service",
    "professional_id",
    "insurance",
    "attendee_name",
    "phone",
)
# Spec §3 / R6: the doctor is e-mailed when THEIR agenda changed.
DOCTOR_EMAIL_FIELDS = frozenset({"data", "horário", "médico"})

LABEL_DATE = "Data"
LABEL_TIME = "Horário"
LABEL_SERVICE = "Serviço"
LABEL_DOCTOR = "Médico"
LABEL_INSURANCE = "Convênio"
LABEL_ATTENDEE = "Paciente"
LABEL_PHONE = "Telefone de contato"
NOT_INFORMED = "não informado"
THE_PATIENT = "você"
NO_DOCTOR = "a clínica"
DEFAULT_SERVICE = "Consulta"
_NOT_DIGIT = re.compile(r"\D")

CalendarFor = Callable[[UUID | None], Awaitable[Any]]


class StaffEditError(Exception):
    """A refusal the hub maps to HTTP: `status_code`, machine `code`, Portuguese `message`."""

    def __init__(self, status_code: int, code: str, message: str, **extra) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class StaffEditRequest:
    """`fields` names what the clinic SENT; a sent null/"" clears insurance/attendee/phone."""

    fields: frozenset[str]
    start_at: datetime | None = None
    service: str | None = None
    professional_id: UUID | None = None
    insurance: str | None = None
    attendee_name: str | None = None
    phone: str | None = None
    allow_overlap: bool = False


@dataclass(frozen=True)
class FieldChange:
    """One line of the patient notice: "<label>: <before> → <after>"."""

    label: str
    before: str
    after: str


@dataclass(frozen=True)
class StaffEditOutcome:
    appointment_id: UUID
    changes: tuple[FieldChange, ...]
    changed_fields: tuple[str, ...]  # R6 vocabulary, for the doctor e-mail
    time_changed: bool
    notice_id: str | None = None
    notice_version: str | None = None


@dataclass(frozen=True)
class _Target:
    start_at: datetime
    end_at: datetime
    service: str | None
    professional_id: UUID | None
    insurance: str | None
    attendee_name: str | None
    phone: str | None


@dataclass(frozen=True)
class _Before:
    start_at: datetime
    end_at: datetime
    service: str | None
    professional_id: UUID | None
    insurance: str | None
    attendee_name: str | None
    event_id: str
    google_calendar_source: str | None


@dataclass(frozen=True)
class _Moved:
    event_id: str
    link: str | None
    calendar_changed: bool
    old_event_id: str
    old_calendar: Any
    new_calendar: Any


def normalize_phone(raw: str | None) -> str | None:
    """Digits only, with country code (12-15 digits); None for blank; ValueError otherwise."""
    if raw is None or not raw.strip():
        return None
    digits = _NOT_DIGIT.sub("", raw)
    if not 12 <= len(digits) <= 15:
        raise ValueError("a contact phone needs 12 to 15 digits, country code included")
    return digits


def _clean(value: str | None) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


def _clinic_tz(tenant: Tenant) -> ZoneInfo:
    try:
        return ZoneInfo(tenant.timezone or "America/Sao_Paulo")
    except Exception:
        return ZoneInfo("America/Sao_Paulo")


async def _professional(
    session: AsyncSession, tenant_id: UUID, professional_id
) -> Professional | None:
    if professional_id is None:
        return None
    return await session.scalar(
        select(Professional).where(
            Professional.id == professional_id, Professional.tenant_id == tenant_id
        )
    )


async def _offered_service(
    session: AsyncSession, tenant: Tenant, professional_id: UUID | None, wanted: str | None
) -> dict | None:
    owner = await _professional(session, tenant.id, professional_id)
    catalog = await load_service_catalog(session, tenant.id)
    offered = (
        professional_appointment_types(owner, tenant, catalog or None)
        if owner is not None
        else active_appointment_types(tenant, catalog or None)
    )
    key = normalize_service_name(wanted)
    return next((s for s in offered if normalize_service_name(s.get("name")) == key), None)


async def _resolve_target(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    request: StaffEditRequest,
    now: datetime,
) -> _Target:
    fields = request.fields
    start = as_utc(appointment.start_at)
    end = (
        as_utc(appointment.end_at)
        if appointment.end_at is not None
        else start + timedelta(minutes=tenant.appointment_duration_min or 30)
    )
    duration = end - start
    professional_id = appointment.professional_id
    if "professional_id" in fields:
        professional = await _professional(session, tenant.id, request.professional_id)
        if professional is None or not professional.is_active:
            raise StaffEditError(
                422, "unknown_professional", "Este profissional não pertence a esta clínica."
            )
        professional_id = professional.id
    service = appointment.appointment_type
    if "service" in fields or professional_id != appointment.professional_id:
        wanted = _clean(request.service) if "service" in fields else appointment.appointment_type
        match = await _offered_service(session, tenant, professional_id, wanted)
        if match is None:
            raise StaffEditError(
                422, "service_not_offered", "Este serviço não é oferecido por este profissional."
            )
        service = str(match.get("name"))
        if normalize_service_name(service) != normalize_service_name(appointment.appointment_type):
            minutes = int(match.get("duration_min") or tenant.appointment_duration_min or 30)
            duration = timedelta(minutes=minutes)
    if "start_at" in fields:
        start = as_utc(request.start_at)
        if start <= now:
            raise StaffEditError(422, "start_in_past", "Escolha um horário no futuro.")
    phone = appointment.phone
    if "phone" in fields:
        try:
            phone = normalize_phone(request.phone)
        except ValueError:
            raise StaffEditError(
                422,
                "invalid_phone",
                "Informe o telefone com DDI e DDD, por exemplo 5511988887777.",
            ) from None
    return _Target(
        start_at=start,
        end_at=start + duration,
        service=service,
        professional_id=professional_id,
        insurance=_clean(request.insurance) if "insurance" in fields else appointment.insurance,
        attendee_name=(
            _clean(request.attendee_name)
            if "attendee_name" in fields
            else appointment.attendee_name
        ),
        phone=phone,
    )


async def _names(session: AsyncSession, tenant_id: UUID, ids) -> dict:
    wanted = [i for i in ids if i is not None]
    if not wanted:
        return {}
    rows = await session.execute(
        select(Professional.id, Professional.name).where(
            Professional.tenant_id == tenant_id, Professional.id.in_(wanted)
        )
    )
    return {row.id: row.name for row in rows}


def _changes(
    tz: ZoneInfo, before: _Before, target: _Target, names: dict, phone_before
) -> list[FieldChange]:
    out: list[FieldChange] = []
    old = before.start_at.astimezone(tz)
    new = target.start_at.astimezone(tz)
    if old.date() != new.date():
        out.append(FieldChange(LABEL_DATE, f"{old:%d/%m/%Y}", f"{new:%d/%m/%Y}"))
    if (old.hour, old.minute) != (new.hour, new.minute):
        out.append(FieldChange(LABEL_TIME, f"{old:%H:%M}", f"{new:%H:%M}"))
    if normalize_service_name(before.service) != normalize_service_name(target.service):
        out.append(
            FieldChange(
                LABEL_SERVICE, before.service or DEFAULT_SERVICE, target.service or DEFAULT_SERVICE
            )
        )
    if before.professional_id != target.professional_id:
        out.append(
            FieldChange(
                LABEL_DOCTOR,
                names.get(before.professional_id) or NO_DOCTOR,
                names.get(target.professional_id) or NO_DOCTOR,
            )
        )
    if _clean(before.insurance) != _clean(target.insurance):
        out.append(
            FieldChange(
                LABEL_INSURANCE,
                _clean(before.insurance) or NOT_INFORMED,
                _clean(target.insurance) or NOT_INFORMED,
            )
        )
    if _clean(before.attendee_name) != _clean(target.attendee_name):
        out.append(
            FieldChange(
                LABEL_ATTENDEE,
                _clean(before.attendee_name) or THE_PATIENT,
                _clean(target.attendee_name) or THE_PATIENT,
            )
        )
    if _clean(phone_before) != _clean(target.phone):
        out.append(
            FieldChange(
                LABEL_PHONE, _clean(phone_before) or NOT_INFORMED, target.phone or NOT_INFORMED
            )
        )
    return out


def _summary(service: str | None, attendee: str | None, patient_name: str | None) -> str:
    return f"{service or DEFAULT_SERVICE} - {attendee or patient_name or 'Paciente'}"


async def _move_event(
    appointment_id: UUID,
    before: _Before,
    target: _Target,
    request: StaffEditRequest,
    calendar_for: CalendarFor,
    patient_name: str | None,
) -> _Moved:
    old_calendar = await calendar_for(calendar_professional_id(before))
    doctor_moved = target.professional_id != before.professional_id
    new_calendar = await calendar_for(target.professional_id) if doctor_moved else old_calendar
    calendar_changed = (
        doctor_moved
        and new_calendar is not old_calendar
        and not old_calendar.references_same_calendar(new_calendar)
    )
    window_changed = (target.start_at, target.end_at) != (before.start_at, before.end_at)
    unavailable = StaffEditError(
        502,
        "calendar_unavailable",
        "Não foi possível alterar o evento no Google Agenda. Nada foi alterado; "
        "tente de novo em instantes.",
    )
    if (window_changed or calendar_changed) and not request.allow_overlap:
        try:
            free = await new_calendar.is_slot_free(
                target.start_at,
                target.end_at,
                ignore_event_id=None if calendar_changed else before.event_id,
            )
        except CalendarUnavailableError as exc:
            raise unavailable from exc
        if not free:
            raise StaffEditError(409, "slot_unavailable", "Este horário não está livre na agenda.")
    summary = _summary(target.service, target.attendee_name, patient_name)
    description = build_event_description(
        service=target.service, insurance=target.insurance, attendee_name=target.attendee_name
    )
    try:
        if calendar_changed:
            created = await new_calendar.create_event(
                target.start_at, target.end_at, summary, description
            )
            return _Moved(
                event_id=str(created.get("id")),
                link=created.get("htmlLink"),
                calendar_changed=True,
                old_event_id=before.event_id,
                old_calendar=old_calendar,
                new_calendar=new_calendar,
            )
        await old_calendar.update_event_details(
            before.event_id, target.start_at, target.end_at, summary, description
        )
        return _Moved(
            event_id=before.event_id,
            link=None,
            calendar_changed=False,
            old_event_id=before.event_id,
            old_calendar=old_calendar,
            new_calendar=new_calendar,
        )
    except StaffEditError:
        raise
    except Exception as exc:
        logger.error(
            "hub_edit_calendar_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        raise unavailable from exc


async def _undo_move(
    appointment_id: UUID, moved: _Moved | None, before: _Before, patient_name: str | None
) -> None:
    """Best effort: take Google back to the committed row after a failed write."""
    if moved is None:
        return
    try:
        if moved.calendar_changed:
            await moved.new_calendar.cancel_event(moved.event_id)
            return
        await moved.old_calendar.update_event_details(
            before.event_id,
            before.start_at,
            before.end_at,
            _summary(before.service, before.attendee_name, patient_name),
            build_event_description(
                service=before.service,
                insurance=before.insurance,
                attendee_name=before.attendee_name,
            ),
        )
    except Exception as exc:
        logger.error(
            "hub_edit_calendar_undo_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )


async def apply_staff_edit(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    request: StaffEditRequest,
    *,
    calendar_for: CalendarFor,
    now: datetime | None = None,
) -> StaffEditOutcome:
    """Validate, move Google, write the row; commits. See the module docstring."""
    now = now or datetime.now(UTC)
    appointment_id, tenant_id = appointment.id, tenant.id
    if appointment.patient_id is None and not appointment.phone:
        raise StaffEditError(
            422, "not_a_patient_appointment", "Este horário não pertence a um paciente."
        )
    if not is_live_status(appointment.status) or appointment.start_at is None:
        raise StaffEditError(
            409,
            "not_live",
            "Esta consulta já foi cancelada ou encerrada.",
            status=appointment.status.value,
        )
    target = await _resolve_target(session, tenant, appointment, request, now)
    before = _Before(
        start_at=as_utc(appointment.start_at),
        end_at=as_utc(appointment.end_at or appointment.start_at),
        service=appointment.appointment_type,
        professional_id=appointment.professional_id,
        insurance=appointment.insurance,
        attendee_name=appointment.attendee_name,
        event_id=(appointment.google_event_id or "").strip(),
        google_calendar_source=appointment.google_calendar_source,
    )
    names = await _names(session, tenant_id, {before.professional_id, target.professional_id})
    changes = _changes(_clinic_tz(tenant), before, target, names, appointment.phone)
    if not changes:
        raise StaffEditError(422, "nothing_changed", "Nada foi alterado.")
    patient_name = None
    if appointment.patient_id is not None:
        patient_name = await session.scalar(
            select(Patient.name).where(
                Patient.id == appointment.patient_id, Patient.tenant_id == tenant_id
            )
        )
    time_changed = target.start_at != before.start_at
    phone_before = appointment.phone
    needs_google = bool(before.event_id) and any(c.label != LABEL_PHONE for c in changes)
    moved = (
        await _move_event(appointment_id, before, target, request, calendar_for, patient_name)
        if needs_google
        else None
    )

    try:
        locked = await session.scalar(
            select(Appointment)
            .where(Appointment.id == appointment_id, Appointment.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is None or not is_live_status(locked.status):
            raise StaffEditError(
                409,
                "not_live",
                "Esta consulta já foi cancelada ou encerrada.",
                status=locked.status.value if locked is not None else None,
            )
        if (
            (locked.google_event_id or "").strip() != before.event_id
            or as_utc(locked.start_at) != before.start_at
            or as_utc(locked.end_at or locked.start_at) != before.end_at
            or locked.appointment_type != before.service
            or locked.professional_id != before.professional_id
            or locked.google_calendar_source != before.google_calendar_source
            or locked.insurance != before.insurance
            or locked.attendee_name != before.attendee_name
            or locked.phone != phone_before
        ):
            raise StaffEditError(
                409,
                "appointment_changed",
                "A consulta foi alterada por outra pessoa. Recarregue e tente de novo.",
            )
        edit = {
            "appointment_type": target.service,
            "professional_id": target.professional_id,
            "insurance": target.insurance,
            "attendee_name": target.attendee_name,
            "google_event_id": moved.event_id if moved is not None else locked.google_event_id,
            "google_event_link": moved.link if moved is not None else locked.google_event_link,
            "start_at": target.start_at,
            "end_at": target.end_at,
            "time_changed": time_changed,
            "doctor_changed": target.professional_id != before.professional_id,
            "calendar_changed": bool(moved is not None and moved.calendar_changed),
        }
        fields = await write_appointment_edit(
            session,
            locked,
            tenant_id=tenant_id,
            timezone=tenant.timezone,
            edit=edit,
            source=SOURCE_HUB,
            idempotency_key=f"hubedit:{appointment_id}:{target.start_at.isoformat()}",
        )
        locked.phone = target.phone
        locked.updated_at = now
        notice = None
        if DOCTOR_EMAIL_FIELDS.intersection(fields):
            notice = await record_professional_edit(session, locked, fields)
        notice_id = str(notice.id) if notice is not None else None
        notice_version = appointment_email_version(locked) if notice is not None else None
        await session.commit()
    except Exception:
        await session.rollback()
        # Restore the winner's committed details, never the losing edit's stale view.
        committed = await session.scalar(
            select(Appointment)
            .where(Appointment.id == appointment_id, Appointment.tenant_id == tenant_id)
            .execution_options(populate_existing=True)
        )
        restore = before
        if committed is not None and committed.start_at is not None:
            restore = _Before(
                start_at=as_utc(committed.start_at),
                end_at=as_utc(committed.end_at or committed.start_at),
                service=committed.appointment_type,
                professional_id=committed.professional_id,
                insurance=committed.insurance,
                attendee_name=committed.attendee_name,
                event_id=(committed.google_event_id or "").strip(),
                google_calendar_source=committed.google_calendar_source,
            )
        await _undo_move(appointment_id, moved, restore, patient_name)
        raise

    if moved is not None and moved.calendar_changed:
        try:
            await moved.old_calendar.cancel_event(moved.old_event_id)
        except Exception as exc:
            logger.warning(
                "hub_edit_old_event_delete_failed",
                appointment_id=str(appointment_id),
                error_type=type(exc).__name__,
            )
    logger.info(
        "hub_appointment_edited",
        appointment_id=str(appointment_id),
        changed=len(changes),
        time_changed=time_changed,
        doctor_email=notice_id is not None,
    )
    return StaffEditOutcome(
        appointment_id=appointment_id,
        changes=tuple(changes),
        changed_fields=tuple(fields),
        time_changed=time_changed,
        notice_id=notice_id,
        notice_version=notice_version,
    )
