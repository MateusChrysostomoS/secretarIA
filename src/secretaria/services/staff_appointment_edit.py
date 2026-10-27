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
   event carries no phone). Before Google, a new window or doctor goes through the hub's
   `slot_guard` (the Pix booking-hold gate, D4) - an encaixe does not bypass it.
3. The row, locked, through the SAME writer the patient's "Alterar Dados" uses
   (services/appointment_edit_write.py), plus the contact phone; the doctor e-mail goes
   to R6's outbox only when date, time or doctor changed. A row that stopped being live -
   or was edited by someone else - meanwhile undoes step 2 (409).
4. After the commit, the old event of a doctor move is deleted from the old agenda (best
   effort, like R6's `_finish_edit`).

The hub route does the rest after this returns: the reminders replan, the doctor e-mail
enqueue and the patient notice. A clinic move never consumes the patient's reschedule
allowance (same rule as POST /reschedule) and the patient's identity (`Patient.wa_id`,
e-mail, `Patient.name`) is never touched. Logs carry ids and codes.

A PAID Pix sinal and a new SERVICE (plan 2026-10-26 Task 7): the service part goes through
the one money engine, `services/payments/service_change.request_service_change` (source
"hub"), in the same transaction as the rest of the edit. Every OTHER field of the request
(date, time, doctor, convênio, ...) is applied now exactly as without a sinal; then:

* dearer by >= R$ 10 -> `awaiting_payment`: a Pix of only the difference; the row and the
  agenda keep the OLD service (and duration) until it is paid - the webhook applies it and
  the registered after-payment agenda hook moves the event. No doctor e-mail for it now;
* cheaper / small difference -> written now by the engine (ONE doctor notice, merged with the
  other fields of this edit), the event follows through `service_change.sync_agenda_event`;
  a cheaper one far enough ahead is `asked` (the route asks the patient);
* `blocked` -> a 4xx/502 `service_change_blocked` and NOTHING of the edit stands.

The new window of a longer service is checked on the agenda before any charge (unless the
clinic squeezes it in, `allow_overlap`); the booking-hold gate covers it as before.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    Patient,
    PixDeposit,
    Professional,
    ProfessionalEditNotice,
    Tenant,
    is_live_status,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services.appointment_calendar_origin import (
    CalendarOriginUnresolved,
    calendar_professional_id,
)
from secretaria.services.appointment_edit import appointment_email_version
from secretaria.services.appointment_edit_write import write_appointment_edit
from secretaria.services.appointment_status import SOURCE_HUB
from secretaria.services.calendar import (
    GOOGLE_RECONNECT_AGENDA_MESSAGE,
    GOOGLE_RECONNECT_CODE,
    CalendarSlotTakenError,
    CalendarUnavailableError,
    GoogleTokenRevokedError,
    build_event_description,
)
from secretaria.services.patient_context import as_utc
from secretaria.services.payments import service_change
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
# (start, end, professional_id) -> raises when a live booking hold (a patient paying the Pix
# deposit) owns that window: decision D4, the SAME check the hub's create and /reschedule run.
SlotGuard = Callable[[datetime, datetime, UUID | None], Awaitable[None]]


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
class ServiceChangeOutcome:
    """The money side of a service change on a PAID sinal (Task 7). `status` is the
    engine's kind: `awaiting_payment` (Pix of the difference sent to the patient by the
    route; service unchanged), `asked` (changed; the patient is asked about the refund) or
    `applied_now` (changed; nothing to pay or ask). `pix_copy_paste` is for the patient
    message only - the hub response never carries it."""

    status: str
    diff_cents: int
    old_service: str
    new_service: str
    adjustment_id: UUID | None = None
    expires_at: datetime | None = None
    pix_copy_paste: str | None = None
    # The engine's decision (`service_change.DECISION_COLLECT`: `applied_now` without a Pix -
    # the provider did not answer - and the clinic collects the difference; follow-up B).
    decision: str | None = None


@dataclass(frozen=True)
class StaffEditOutcome:
    appointment_id: UUID
    changes: tuple[FieldChange, ...]
    changed_fields: tuple[str, ...]  # R6 vocabulary, for the doctor e-mail
    time_changed: bool
    notice_id: str | None = None
    notice_version: str | None = None
    service_change: ServiceChangeOutcome | None = None


# `request_service_change` refused (`ServiceChangeResult.reason`): what the clinic reads.
# Nothing of the edit stands (the transaction rolls back and Google is put back).
_NOTHING_CHANGED = "Nada foi alterado."
SERVICE_CHANGE_BLOCKED = "service_change_blocked"
_BLOCKED: dict[str, tuple[int, str]] = {
    "adjustment_pending": (
        409,
        "Já existe um Pix da diferença do sinal aguardando pagamento nesta consulta. "
        f"{_NOTHING_CHANGED} Aguarde o pagamento (ou o prazo dele acabar) para trocar o "
        "serviço de novo.",
    ),
    "collect_pending": (
        409,
        "A diferença do sinal da troca anterior ainda está para ser cobrada pela clínica (veja "
        f"Pagamentos que precisam de decisão). {_NOTHING_CHANGED} Resolva esse item para "
        "trocar o serviço de novo.",
    ),
    "refund_question_pending": (
        409,
        "O paciente ainda não respondeu se quer de volta a diferença da troca anterior. "
        f"{_NOTHING_CHANGED} Aguarde a resposta (até 24 horas) para trocar o serviço de novo.",
    ),
    "price_unavailable": (
        422,
        "Não foi possível calcular o sinal do novo serviço: confira o preço dele no cadastro "
        f"de serviços. {_NOTHING_CHANGED}",
    ),
    "no_payment_account": (
        422,
        "A conta de pagamentos da clínica não está disponível para cobrar a diferença do "
        f"sinal. {_NOTHING_CHANGED}",
    ),
    "charge_failed": (
        502,
        "Não foi possível gerar o Pix da diferença do sinal agora. "
        f"{_NOTHING_CHANGED} Tente de novo em instantes.",
    ),
    "deposit_not_paid": (
        409,
        f"O sinal desta consulta mudou de situação. {_NOTHING_CHANGED} "
        "Recarregue e tente de novo.",
    ),
}


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


def _calendar_unavailable() -> StaffEditError:
    return StaffEditError(
        502,
        "calendar_unavailable",
        "Não foi possível alterar o evento na agenda. Nada foi alterado; "
        "tente de novo em instantes.",
    )


def _calendar_reconnect() -> StaffEditError:
    # Google refused the stored grant itself: retrying never helps, reconnecting does
    # (the hub's other calendar routes answer the same - api/hub/calendar.py).
    return StaffEditError(409, GOOGLE_RECONNECT_CODE, GOOGLE_RECONNECT_AGENDA_MESSAGE)


def _slot_taken() -> StaffEditError:
    return StaffEditError(409, "slot_unavailable", "Este horário não está livre na agenda.")


def _owner_id(before: _Before) -> UUID | None:
    try:
        return calendar_professional_id(before)
    except CalendarOriginUnresolved:
        raise StaffEditError(
            409, "calendar_unresolved",
            "Não foi possível identificar a agenda do profissional desta consulta.",
        ) from None


async def _ensure_free(calendar, start: datetime, end: datetime, ignore_event_id) -> None:
    try:
        free = await calendar.is_slot_free(start, end, ignore_event_id=ignore_event_id)
    except GoogleTokenRevokedError as exc:
        raise _calendar_reconnect() from exc
    except CalendarUnavailableError as exc:
        raise _calendar_unavailable() from exc
    if not free:
        raise _slot_taken()


async def _move_event(
    appointment_id: UUID,
    before: _Before,
    target: _Target,
    request: StaffEditRequest,
    calendar_for: CalendarFor,
    patient_name: str | None,
    *,
    check_end: datetime | None = None,
) -> _Moved:
    """Patch (or, on another agenda, create) the event as `target`. `check_end`: the free
    check covers up to there instead of `target.end_at` (a paid service change whose longer
    window is written only later)."""
    old_calendar = await calendar_for(_owner_id(before))
    doctor_moved = target.professional_id != before.professional_id
    new_calendar = await calendar_for(target.professional_id) if doctor_moved else old_calendar
    calendar_changed = (
        doctor_moved
        and new_calendar is not old_calendar
        and not old_calendar.references_same_calendar(new_calendar)
    )
    check_end = check_end or target.end_at
    window_changed = (target.start_at, check_end) != (before.start_at, before.end_at)
    unavailable = _calendar_unavailable()
    reconnect = _calendar_reconnect()
    if (window_changed or calendar_changed) and not request.allow_overlap:
        await _ensure_free(
            new_calendar,
            target.start_at,
            check_end,
            None if calendar_changed else before.event_id,
        )
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
    except GoogleTokenRevokedError as exc:
        logger.warning("hub_edit_calendar_reconnect_required", appointment_id=str(appointment_id))
        raise reconnect from exc
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
    slot_guard: SlotGuard | None = None,
    now: datetime | None = None,
) -> StaffEditOutcome:
    """Validate, move Google, write the row; commits. See the module docstring.

    `slot_guard` runs before Google whenever the window or the doctor changes - even on an
    `allow_overlap` encaixe: an encaixe may overlap a booked consultation, never a slot
    another patient is paying for (D4)."""
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

    # A PAID sinal and a new service: everything else is applied now; the service (and its
    # window) is the money engine's to write - now, or when the difference is paid.
    deposit = None
    if normalize_service_name(target.service) != normalize_service_name(before.service):
        deposit = await _paid_deposit(session, tenant_id, appointment_id)
    now_target = target
    now_changes = changes
    if deposit is not None:
        old_duration = (
            before.end_at - before.start_at
            if appointment.end_at is not None
            else timedelta(minutes=tenant.appointment_duration_min or 30)
        )
        now_target = replace(
            target, service=before.service, end_at=target.start_at + old_duration
        )
        now_changes = [c for c in changes if c.label != LABEL_SERVICE]
    window_grows = target.end_at > now_target.end_at
    # The slot must hold BOTH windows this edit may leave on the agenda: `now_target` (written
    # now - with a paid dearer service, the OLD length at the new time until the Pix is paid)
    # and `target` (written now or at payment). A shorter dearer service must not leave the
    # old service's tail unchecked.
    widest_end = max(target.end_at, now_target.end_at)
    window_or_doctor_changed = (target.start_at, widest_end) != (
        before.start_at,
        before.end_at,
    ) or target.professional_id != before.professional_id
    if slot_guard is not None and window_or_doctor_changed:
        await slot_guard(target.start_at, widest_end, target.professional_id)

    needs_google = bool(before.event_id) and any(c.label != LABEL_PHONE for c in now_changes)
    moved = None
    if needs_google:
        moved = await _move_event(
            appointment_id,
            before,
            now_target,
            request,
            calendar_for,
            patient_name,
            check_end=widest_end,
        )
    elif deposit is not None and before.event_id and window_grows and not request.allow_overlap:
        # Only the service changes: its longer window must fit BEFORE any Pix is charged.
        await _ensure_free(
            await calendar_for(_owner_id(before)), target.start_at, target.end_at, before.event_id
        )

    money: service_change.ServiceChangeResult | None = None
    synced_on = None  # the agenda the service change was written on (for the undo)
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
            "appointment_type": now_target.service,
            "professional_id": now_target.professional_id,
            "insurance": now_target.insurance,
            "attendee_name": now_target.attendee_name,
            "google_event_id": moved.event_id if moved is not None else locked.google_event_id,
            "google_event_link": moved.link if moved is not None else locked.google_event_link,
            "start_at": now_target.start_at,
            "end_at": now_target.end_at,
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
        if deposit is not None:
            money = await service_change.request_service_change(
                session,
                tenant=tenant,
                appointment=locked,
                deposit=deposit,
                new_service=str(target.service),
                pending_edit=_pending_service_edit(locked, target),
                now=now,
                source=SOURCE_HUB,
            )
            if money.kind == "blocked":
                status_code, message = _BLOCKED.get(
                    money.reason or "", _BLOCKED["price_unavailable"]
                )
                raise StaffEditError(
                    status_code, SERVICE_CHANGE_BLOCKED, message, reason=money.reason
                )
        if money is not None and money.kind != "awaiting_payment":
            # The engine wrote the service and recorded the doctor's ONE notice: the event
            # follows, and that notice also names the rest of this edit (one e-mail).
            if (locked.google_event_id or "").strip():
                synced_on = (
                    moved.new_calendar
                    if moved is not None
                    else await calendar_for(_owner_id(before))
                )
                await _sync_service_event(
                    session,
                    tenant,
                    locked,
                    synced_on,
                    patient_name=patient_name,
                    window_grows=window_grows and not request.allow_overlap,
                )
            fields = _merged(fields, money.changed_fields)
            if money.notice_id is not None:
                notice = await session.get(ProfessionalEditNotice, money.notice_id)
            if notice is not None:
                notice.changed_fields = list(fields)
        if notice is None and DOCTOR_EMAIL_FIELDS.intersection(fields):
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
        if moved is None and synced_on is not None:
            # Only the service was written on the agenda: put that event back too.
            moved = _Moved(
                event_id=before.event_id,
                link=None,
                calendar_changed=False,
                old_event_id=before.event_id,
                old_calendar=synced_on,
                new_calendar=synced_on,
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
    told = changes
    outcome_change = None
    if money is not None:
        outcome_change = ServiceChangeOutcome(
            status=money.kind,
            diff_cents=money.diff_cents,
            old_service=before.service or DEFAULT_SERVICE,
            new_service=str(target.service),
            adjustment_id=money.adjustment_id,
            expires_at=money.expires_at,
            pix_copy_paste=money.pix_copy_paste,
            decision=money.decision,
        )
        if money.kind == "awaiting_payment":
            told = now_changes  # the service did not change yet
    logger.info(
        "hub_appointment_edited",
        appointment_id=str(appointment_id),
        changed=len(told),
        time_changed=time_changed,
        doctor_email=notice_id is not None,
        service_change=money.kind if money is not None else None,
    )
    return StaffEditOutcome(
        appointment_id=appointment_id,
        changes=tuple(told),
        changed_fields=tuple(fields),
        time_changed=time_changed,
        notice_id=notice_id,
        notice_version=notice_version,
        service_change=outcome_change,
    )


async def _paid_deposit(
    session: AsyncSession, tenant_id: UUID, appointment_id: UUID
) -> PixDeposit | None:
    """This appointment's sinal when it is PAID (tenant-scoped), else None."""
    deposit = await session.scalar(
        select(PixDeposit).where(
            PixDeposit.appointment_id == appointment_id, PixDeposit.tenant_id == tenant_id
        )
    )
    if deposit is None or deposit.status != PixDepositStatus.PAID:
        return None
    return deposit


def _pending_service_edit(row: Appointment, target: _Target) -> dict:
    """The R6 edit the money engine writes (now, or when the difference is paid): the rest
    of the clinic's edit is already on `row`, so only the service and its window change.
    `time_changed` stays False - the clinic moved the time now, and a clinic move never
    counts against the patient's reschedule allowance (not even at payment)."""
    return {
        "appointment_type": target.service,
        "professional_id": row.professional_id,
        "insurance": row.insurance,
        "attendee_name": row.attendee_name,
        "google_event_id": row.google_event_id,
        "google_event_link": row.google_event_link,
        "start_at": target.start_at,
        "end_at": target.end_at,
        "time_changed": False,
        "doctor_changed": False,
        "calendar_changed": False,
    }


def _merged(first, second) -> list[str]:
    out = list(first)
    out += [name for name in second if name not in out]
    return out


async def _sync_service_event(
    session: AsyncSession,
    tenant: Tenant,
    row: Appointment,
    calendar,
    *,
    patient_name: str | None,
    window_grows: bool,
) -> None:
    """The service written now: the event follows through the ONE helper the patient's edit
    and the after-payment hook use. A longer window is checked (free, no booking hold) and
    written exclusively; a refusal undoes the whole edit."""
    try:
        await service_change.sync_agenda_event(
            session, tenant, row, calendar, patient_name=patient_name, window_changed=window_grows
        )
    except CalendarSlotTakenError as exc:
        raise _slot_taken() from exc
    except GoogleTokenRevokedError as exc:
        raise _calendar_reconnect() from exc
    except Exception as exc:
        logger.error(
            "hub_edit_service_calendar_failed",
            appointment_id=str(row.id),
            error_type=type(exc).__name__,
        )
        raise _calendar_unavailable() from exc
