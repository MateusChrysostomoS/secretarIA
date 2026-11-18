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

The sinal (TASK-085 R, tasks/TASK-085/sinal-trocas/contrato.md §3.7/§3.9). An edit that changes
a MONEY field - service, unit (a doctor change that clears it), doctor or convênio
(`service_change_policy.money_fields_changed`) - consults the consultation's sinal:

* PAID -> the ONE money engine, `service_change.request_paid_change` (source "hub"), gets the
  WHOLE target (service, doctor, unit, convênio, time, for-whom) and decides (D-R1/D-R3):
  - written NOW (`applied_now`, `asked`, and - a clinic that books before the sinal is paid,
    "after" - a dearer change: `applied_awaiting_payment`, the Pix of the difference follows,
    D-R5): Google moves to the target first (as without a sinal), then the engine writes the
    row and the doctor's ONE notice;
  - WAITING for the Pix (a clinic that books only after the sinal, "before", and a dearer change:
    `suspended`, D-R4 - or the legacy `awaiting_payment` without room for a useful Pix): only
    the contact phone is written now; nothing of the agenda moves (the target window is checked
    free first); the visit is suspended until the Pix is paid or the patient chooses;
  - `blocked` -> a 4xx/502 `service_change_blocked` and NOTHING of the edit stands.
  Which of the two is known before Google (`_deferral`, the engine's own read-only reckoning).
* AWAITING (not paid yet, D-R6) -> the edit is written as without a sinal and, as the LAST
  fallible step before the commit, `deposit_lifecycle.requote_awaiting` voids the open Pix when
  the amount changed (the route then sends the new one, `recharge_after_edit`); a Pix being paid
  or that cannot be voided undoes the whole edit (409/502 `sinal_requote_blocked`).
* none -> after the commit the route may charge the sinal the edit made due (OQ-10).

A SUSPENDED consultation (D-R9) cannot be edited at all: 409 `appointment_suspended` (the clinic
may only "Liberar horário").
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
from secretaria.services import professional_units
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
    CalendarUnavailableError,
    GoogleTokenRevokedError,
    build_event_description,
    overlaps_other_agenda,
    own_agenda_view,
)
from secretaria.services.patient_context import as_utc
from secretaria.services.payments import (
    adjustment_suspension,
    deposit_lifecycle,
    service_change,
)
from secretaria.services.payments.service_change_policy import MoneyTerms, money_fields_changed
from secretaria.services.professional_edit_outbox import record_professional_edit
from secretaria.services.service_catalog import (
    load_service_catalog,
    normalize as normalize_service_name,
)
from secretaria.services.service_terms import resolve_entry_at_unit
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
)
from secretaria.services.unit_booking import booking_unit_fields

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
    """The money side of a change on a PAID sinal (Task 7; TASK-085 R: any money field). `status`
    is the engine's kind: `suspended` (D-R4: nothing changed yet, the visit waits for the Pix of
    the difference the route sends), `awaiting_payment` (the legacy deferred mode: the same,
    without suspension), `applied_awaiting_payment` (D-R5: changed now, the Pix of the difference
    follows), `asked` (changed; the patient is asked about the refund) or `applied_now`
    (changed; nothing to pay or ask). `new_service` is the engine's label of the new consultation
    when it is more than the service ("Consulta com Dr. X"). `pix_copy_paste` is for the patient
    message only - the hub response never carries it. Absent (None on the outcome) when the
    engine found nothing to settle (`no_change`)."""

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
    # TASK-083 spec §7.4: the new window overlaps the doctor's time at another clinic sharing
    # his agenda. A warning for the hub's answer; the edit was applied all the same.
    conflicts_other_agenda: bool = False
    # TASK-085 R (contrato §3.7): the route sends a new sinal after the commit
    # (`deposit_lifecycle.recharge_after_edit`) - the open one was voided (D-R6), or the edit
    # made one due on a consultation that had none (OQ-10). None = nothing to send.
    sinal_recharge: "SinalRecharge | None" = None


@dataclass(frozen=True)
class SinalRecharge:
    """`recharge_after_edit(old_amount_cents=...)`: the voided sinal's amount, or None when the
    consultation had no sinal (OQ-10)."""

    old_amount_cents: int | None


# The engine's kinds of a change that waits for the Pix of its difference (nothing written).
WAITING_KINDS = frozenset({"suspended", "awaiting_payment"})
# `ServiceChangeResult.decision` when the money fields changed but the sinal did not move.
NO_MONEY_CHANGE = "no_change"
# `_deferral`: how a PAID change will wait for its Pix ("before" clinic, dearer by >= R$ 10).
DEFER_SUSPEND = "suspend"  # D-R4: suspended visit + held new window
DEFER_LEGACY = "legacy"  # contrato §1: no room for a useful Pix before the visits

# D-R9 (contrato §3.9): every mutation of a SUSPENDED consultation by the hub.
APPOINTMENT_SUSPENDED = "appointment_suspended"
APPOINTMENT_SUSPENDED_MESSAGE = (
    "Essa consulta aguarda o Pix da diferença de uma troca; enquanto isso, só é possível "
    "liberar o horário."
)

# `request_service_change` refused (`ServiceChangeResult.reason`): what the clinic reads.
# Nothing of the edit stands (the transaction rolls back and Google is put back).
_NOTHING_CHANGED = "Nada foi alterado."
SERVICE_CHANGE_BLOCKED = "service_change_blocked"
# Staff-side refusal (not an engine reason): the legacy deferred mode cannot carry the change.
REASON_NO_ROOM = "no_room"
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
    # TASK-085 R
    "choice_pending": (
        409,
        "O Pix da diferença de uma troca anterior venceu e o paciente ainda está escolhendo o "
        f"que fazer. {_NOTHING_CHANGED} Aguarde a resposta dele (ou o prazo acabar).",
    ),
    "slot_taken": (
        409,
        "O novo horário não está livre para ficar reservado enquanto o paciente paga a "
        f"diferença do sinal. {_NOTHING_CHANGED} Escolha outro horário.",
    ),
    REASON_NO_ROOM: (
        409,
        "A consulta está perto demais para o paciente pagar a diferença do sinal antes dela, e "
        f"essa troca de médico, convênio ou paciente só vale com o pagamento. {_NOTHING_CHANGED} "
        "Troque só o serviço ou o horário, ou combine a diferença com o paciente.",
    ),
}

# D-R6 (`deposit_lifecycle.requote_awaiting` refused): the whole edit was undone.
SINAL_REQUOTE_BLOCKED = "sinal_requote_blocked"
_REQUOTE_BLOCKED: dict[str, tuple[int, str]] = {
    "payment_in_flight": (
        409,
        "O Pix do sinal desta consulta acabou de ser pago. "
        f"{_NOTHING_CHANGED} Aguarde a confirmação do pagamento e tente de novo.",
    ),
    "void_failed": (
        502,
        "Não foi possível cancelar o Pix do sinal em aberto para gerar um com o novo valor. "
        f"{_NOTHING_CHANGED} Tente de novo em instantes.",
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
    # TASK-085: the unit the booking has after this edit (`_unit_kept`): where the new
    # service's length (and, through the money engine, its price) is read.
    unit_id: UUID | None = None


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
    # TASK-083 follow-up: the booking's unit, so a restored event keeps its unit lines.
    unit_id: UUID | None = None


@dataclass(frozen=True)
class _Moved:
    event_id: str
    link: str | None
    calendar_changed: bool
    old_event_id: str
    old_calendar: Any
    new_calendar: Any
    conflicts_other_agenda: bool = False


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


async def _unit_kept(
    session: AsyncSession,
    tenant_id: UUID,
    unit_id: UUID | None,
    professional_id: UUID | None,
    *,
    doctor_changed: bool,
) -> UUID | None:
    """The unit a booking keeps after a hub edit (TASK-083 final wave I3): a doctor change keeps
    it only if the new doctor attends it (an active link); time moves never touch it."""
    if unit_id is None or not doctor_changed:
        return unit_id
    new_doctor = await _professional(session, tenant_id, professional_id)
    offered = (
        await professional_units.available_unit_ids(session, new_doctor)
        if new_doctor is not None
        else []
    )
    return unit_id if unit_id in offered else None


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
    # TASK-083 final wave I3 / TASK-085: the unit the booking keeps (a doctor change keeps it
    # only where the new doctor attends; no query otherwise).
    unit_id = await _unit_kept(
        session,
        tenant.id,
        getattr(appointment, "unit_id", None),
        professional_id,
        doctor_changed=professional_id != appointment.professional_id,
    )
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
            # D6: only a NEW service changes the booked length - the doctor's own at the unit
            # the booking keeps (TASK-085 D2/D7), else the service's base exactly as before.
            own = await resolve_entry_at_unit(session, tenant.id, professional_id, match, unit_id)
            minutes = (
                own.duration_min
                if own.own and own.duration_min is not None
                else int(match.get("duration_min") or tenant.appointment_duration_min or 30)
            )
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
        unit_id=unit_id,
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
    """The clinic's OWN agenda must be free (TASK-083 spec §7.4: the doctor's time at another
    clinic sharing his agenda only warns - `overlaps_other_agenda` - and never refuses here)."""
    try:
        free = await own_agenda_view(calendar).is_slot_free(
            start, end, ignore_event_id=ignore_event_id
        )
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
    where: dict | None = None,
) -> _Moved:
    """Patch (or, on another agenda, create) the event as `target`. `check_end`: the free
    check covers up to there instead of `target.end_at` (a paid service change whose longer
    window is written only later). `where`: the unit lines of the booking's unit after this
    edit (`unit_booking.booking_unit_fields`), so the rebuilt description keeps them."""
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
    conflicts = False
    if window_changed or calendar_changed:
        conflicts = await overlaps_other_agenda(new_calendar, target.start_at, check_end)
    if (window_changed or calendar_changed) and not request.allow_overlap:
        await _ensure_free(
            new_calendar,
            target.start_at,
            check_end,
            None if calendar_changed else before.event_id,
        )
    summary = _summary(target.service, target.attendee_name, patient_name)
    description = build_event_description(
        service=target.service,
        insurance=target.insurance,
        attendee_name=target.attendee_name,
        **(where or {}),
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
                conflicts_other_agenda=conflicts,
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
            conflicts_other_agenda=conflicts,
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
    appointment_id: UUID,
    moved: _Moved | None,
    before: _Before,
    patient_name: str | None,
    where: dict | None = None,
) -> None:
    """Best effort: take Google back to the committed row after a failed write (`where`: that
    row's unit lines, see `_move_event`)."""
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
                **(where or {}),
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
    if await adjustment_suspension.is_suspended(
        session, tenant_id=tenant_id, appointment_id=appointment_id
    ):
        raise suspended_error()
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
        unit_id=getattr(appointment, "unit_id", None),
    )
    names = await _names(session, tenant_id, {before.professional_id, target.professional_id})
    tz = _clinic_tz(tenant)
    changes = _changes(tz, before, target, names, appointment.phone)
    if not changes:
        raise StaffEditError(422, "nothing_changed", "Nada foi alterado.")
    patient_name = None
    if appointment.patient_id is not None:
        patient_name = await session.scalar(
            select(Patient.name).where(
                Patient.id == appointment.patient_id, Patient.tenant_id == tenant_id
            )
        )
    phone_before = appointment.phone
    doctor_changed = target.professional_id != before.professional_id

    # TASK-085 R (contrato §3.4/§3.9): a change of a MONEY field - service, unit, doctor,
    # convênio - consults the sinal. PAID -> the money engine decides for the WHOLE target
    # (D-R1/D-R3); AWAITING -> the edit is written and the open Pix re-quoted (D-R6); no sinal ->
    # maybe a new one after the commit (OQ-10). Any other edit is exactly today's.
    money_changed = money_fields_changed(_terms(before), _terms(target))
    deposit = await _deposit_of(session, tenant_id, appointment_id) if money_changed else None
    paid = deposit if deposit is not None and deposit.status == PixDepositStatus.PAID else None
    awaiting = (
        deposit if deposit is not None and deposit.status == PixDepositStatus.AWAITING else None
    )
    # How a PAID change will stand: written NOW (None), or waiting for the difference Pix with
    # nothing of the money/time written ("suspend": D-R4; "legacy": the "before" fallback
    # without room, contrato §1) - known BEFORE Google, so the agenda is touched only for what
    # is written now.
    deferral = (
        await _deferral(session, tenant, appointment, paid, target, now)
        if paid is not None
        else None
    )
    if deferral == DEFER_LEGACY and (
        doctor_changed
        or _clean(before.insurance) != _clean(target.insurance)
        or _clean(before.attendee_name) != _clean(target.attendee_name)
    ):
        # The legacy deferred mode re-applies only service, time and unit at payment (doctor,
        # convênio and for-whom are read from the row then): never charge for a change it cannot
        # write.
        status_code, message = _BLOCKED[REASON_NO_ROOM]
        raise StaffEditError(status_code, SERVICE_CHANGE_BLOCKED, message, reason=REASON_NO_ROOM)
    now_target = target if deferral is None else _phone_only(before, target)
    now_changes = (
        changes if deferral is None else _changes(tz, before, now_target, names, phone_before)
    )
    time_changed = now_target.start_at != before.start_at

    window_or_doctor_changed = (target.start_at, target.end_at) != (
        before.start_at,
        before.end_at,
    ) or doctor_changed
    if slot_guard is not None and window_or_doctor_changed:
        await slot_guard(target.start_at, target.end_at, target.professional_id)

    moved = None
    conflicts_other_agenda = False
    needs_google = bool(before.event_id) and any(c.label != LABEL_PHONE for c in now_changes)
    if needs_google:
        moved = await _move_event(
            appointment_id,
            before,
            now_target,
            request,
            calendar_for,
            patient_name,
            where=await booking_unit_fields(
                session, tenant_id, now_target.unit_id, now_target.professional_id
            ),
        )
        conflicts_other_agenda = moved.conflicts_other_agenda
    elif deferral is not None and before.event_id and window_or_doctor_changed:
        # The change waits for the Pix: its window must be free on the agenda it lands on
        # BEFORE any Pix is charged (the engine checks the database; Google is checked here).
        conflicts_other_agenda = await _check_deferred_window(
            before, target, request, calendar_for
        )

    money: service_change.ServiceChangeResult | None = None
    requote: str | None = None
    old_sinal_cents = awaiting.amount_cents if awaiting is not None else None
    fields: list[str] = []
    told = now_changes
    unseen: service_change.UnseenCharge | None = None
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
        # D-R9 under the row lock: a patient's change that suspended it meanwhile wins.
        if await adjustment_suspension.is_suspended(
            session, tenant_id=tenant_id, appointment_id=appointment_id
        ):
            raise suspended_error()
        notice = None
        if paid is not None:
            # The engine writes the WHOLE target itself when the change stands now (and records
            # the doctor's ONE notice), or nothing at all.
            money = await service_change.request_paid_change(
                session,
                tenant=tenant,
                appointment=locked,
                deposit=paid,
                new_service=str(target.service),
                pending_edit=_pending_edit(locked, before, target, moved),
                now=now,
                source=SOURCE_HUB,
            )
            # Read now: the adjustment row disappears with a rollback (S7a).
            unseen = await service_change.unseen_charge_of(
                session, tenant=tenant, deposit=paid, result=money
            )
            if money.kind == "blocked":
                status_code, message = _BLOCKED.get(
                    money.reason or "", _BLOCKED["price_unavailable"]
                )
                raise StaffEditError(
                    status_code, SERVICE_CHANGE_BLOCKED, message, reason=money.reason
                )
            if _decision_moved(deferral, money):
                # Decided a moment ago, and the engine - under the lock - answered with the other
                # shape AND a Pix (the clinic's timing or prices changed in between): "now" that
                # turned into "wait" (Google already moved), or "wait" that turned into
                # `applied_awaiting_payment` (the agenda would have to move AFTER the Pix). Never
                # commit either: the Pix the patient never saw is voided, then everything undone.
                logger.warning(
                    "hub_edit_money_decision_moved",
                    appointment_id=str(appointment_id),
                    kind=money.kind,
                )
                raise _sinal_changed()  # the except below voids the unseen Pix
            if money.kind not in WAITING_KINDS:
                fields = list(money.changed_fields)
                if deferral is not None:
                    # Decided "wait" and the engine wrote it now WITHOUT a Pix (`applied_now`:
                    # the provider was down and the clinic collects, or a cheaper/smaller change
                    # after a price moved; `asked`): the agenda follows now - no Pix exists (the
                    # Pix kind was refused above), so a refusal here still undoes everything.
                    moved = await _move_event(
                        appointment_id,
                        before,
                        target,
                        request,
                        calendar_for,
                        patient_name,
                        where=await booking_unit_fields(
                            session, tenant_id, target.unit_id, target.professional_id
                        ),
                    )
                    conflicts_other_agenda = moved.conflicts_other_agenda
                    _repoint_event(locked, moved)
                    time_changed = target.start_at != before.start_at
                told = changes
                if money.notice_id is not None:
                    notice = await session.get(ProfessionalEditNotice, money.notice_id)
        else:
            fields = await write_appointment_edit(
                session,
                locked,
                tenant_id=tenant_id,
                timezone=tenant.timezone,
                edit=_row_edit(locked, before, now_target, moved),
                source=SOURCE_HUB,
                idempotency_key=f"hubedit:{appointment_id}:{target.start_at.isoformat()}",
            )
        locked.phone = now_target.phone
        locked.updated_at = now
        if notice is None and DOCTOR_EMAIL_FIELDS.intersection(fields):
            notice = await record_professional_edit(session, locked, fields)
        notice_id = str(notice.id) if notice is not None else None
        notice_version = appointment_email_version(locked) if notice is not None else None
        if awaiting is not None:
            # D-R6 - the LAST fallible step (contrato §3.7): once "voided" the old Pix is gone
            # at the provider, so nothing that can fail may follow it before the commit.
            await session.flush()
            try:
                requote = await deposit_lifecycle.requote_awaiting(
                    session, tenant=tenant, appointment=locked, deposit=awaiting, now=now
                )
            except Exception as exc:  # noqa: BLE001 - contrato §3.7: an exception = void_failed
                logger.warning(
                    "hub_edit_requote_failed",
                    appointment_id=str(appointment_id),
                    error_type=type(exc).__name__,
                )
                requote = "void_failed"
            if requote in _REQUOTE_BLOCKED:
                status_code, message = _REQUOTE_BLOCKED[requote]
                raise StaffEditError(status_code, SINAL_REQUOTE_BLOCKED, message, reason=requote)
        await session.commit()
    except Exception:
        await session.rollback()
        await _void_unseen_after_rollback(session, tenant_id, unseen)
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
                unit_id=getattr(committed, "unit_id", None),
            )
        restore_where = (
            await booking_unit_fields(
                session, tenant_id, restore.unit_id, restore.professional_id
            )
            if moved is not None
            else {}
        )
        await _undo_move(appointment_id, moved, restore, patient_name, restore_where)
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
    outcome_change = None
    if money is not None and money.decision != NO_MONEY_CHANGE:
        outcome_change = ServiceChangeOutcome(
            status=money.kind,
            diff_cents=money.diff_cents,
            old_service=before.service or DEFAULT_SERVICE,
            new_service=money.label or str(target.service),
            adjustment_id=money.adjustment_id,
            expires_at=money.expires_at,
            pix_copy_paste=money.pix_copy_paste,
            decision=money.decision,
        )
    recharge = None
    if requote == "voided":
        recharge = SinalRecharge(old_amount_cents=old_sinal_cents)
    elif deposit is None and money_changed and _may_newly_charge(tenant, appointment):
        recharge = SinalRecharge(old_amount_cents=None)
    logger.info(
        "hub_appointment_edited",
        appointment_id=str(appointment_id),
        changed=len(told),
        time_changed=time_changed,
        doctor_email=notice_id is not None,
        service_change=money.kind if money is not None else None,
        sinal_requote=requote,
    )
    return StaffEditOutcome(
        appointment_id=appointment_id,
        changes=tuple(told),
        changed_fields=tuple(fields),
        time_changed=time_changed,
        notice_id=notice_id,
        notice_version=notice_version,
        service_change=outcome_change,
        conflicts_other_agenda=conflicts_other_agenda,
        sinal_recharge=recharge,
    )


def suspended_error() -> StaffEditError:
    """D-R9 (contrato §3.9): the clinic may only free a suspended consultation's slot."""
    return StaffEditError(409, APPOINTMENT_SUSPENDED, APPOINTMENT_SUSPENDED_MESSAGE)


def _decision_moved(
    deferral: str | None, money: service_change.ServiceChangeResult
) -> bool:
    """The engine's answer does not have the shape `_deferral` predicted, and a Pix exists."""
    if deferral is None:
        return money.kind in WAITING_KINDS
    return money.kind == "applied_awaiting_payment"


async def _void_unseen_after_rollback(
    session: AsyncSession, tenant_id: UUID, unseen: service_change.UnseenCharge | None
) -> None:
    """TASK-085 R (S7a): the edit rolled back after the engine created the Pix of its difference
    (never sent: the hub tells the patient only after the commit). Void it at the provider
    (`service_change.void_unseen_charge`, provider only); a refused void becomes ONE clinic item
    (`unseen_charge_to_void`), committed on its own now that the edit's transaction is gone.
    Never raises: the clinic's answer is the edit's own error."""
    if unseen is None:
        return
    try:
        outcome = await service_change.void_unseen_charge(
            session, tenant_id=tenant_id, charge=unseen
        )
        if outcome == "failed":
            await service_change.open_unvoided_charge_item(session, unseen)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - logged; the provider's own expiry still applies
        await session.rollback()
        logger.error(
            "hub_edit_unseen_pix_not_voided",
            tenant_id=str(tenant_id),
            error_type=type(exc).__name__,
        )


def _sinal_changed() -> StaffEditError:
    status_code, message = _BLOCKED["deposit_not_paid"]
    return StaffEditError(status_code, SERVICE_CHANGE_BLOCKED, message, reason="deposit_not_paid")


def _terms(side: _Before | _Target) -> MoneyTerms:
    return MoneyTerms(
        service=side.service,
        professional_id=side.professional_id,
        unit_id=side.unit_id,
        insurance=side.insurance,
    )


async def _deposit_of(
    session: AsyncSession, tenant_id: UUID, appointment_id: UUID
) -> PixDeposit | None:
    """This appointment's sinal row (tenant-scoped), whatever its status, or None."""
    return await session.scalar(
        select(PixDeposit).where(
            PixDeposit.appointment_id == appointment_id, PixDeposit.tenant_id == tenant_id
        )
    )


async def _deferral(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    deposit: PixDeposit,
    target: _Target,
    now: datetime,
) -> str | None:
    """Whether the engine will keep a PAID change WAITING for the difference Pix (contrato §1):
    only in a clinic that books after the sinal ("before") and only for a dearer change by at
    least R$ 10 - `suspend` (D-R4) or, without room for a useful Pix before the visits, `legacy`.
    The same read-only reckoning `request_paid_change` makes (`preview_service_change` at the
    target's doctor, unit and convênio; `suspend_pix_expiry` from the row's start to the
    target's). Anything else the engine writes now: None."""
    if not service_change.timing_is_before(tenant):
        return None
    decision = await service_change.preview_service_change(
        session,
        tenant=tenant,
        appointment=appointment,
        deposit=deposit,
        new_service=str(target.service),
        professional_id=target.professional_id,
        start_at=target.start_at,
        now=now,
        unit_id=target.unit_id,
        insurance=target.insurance,
    )
    if decision.action != "increase_charge":
        return None
    ttl = tenant.deposit_hold_ttl_minutes or service_change.DEFAULT_PIX_TTL_MINUTES
    expires = service_change.suspend_pix_expiry(now, ttl, appointment.start_at, target.start_at)
    return DEFER_SUSPEND if expires is not None else DEFER_LEGACY


def _phone_only(before: _Before, target: _Target) -> _Target:
    """What a WAITING change writes now (contrato §3.9 / OQ-5): the contact phone only - service,
    doctor, unit, convênio, time and for-whom wait with the Pix (they are the pending edit)."""
    return _Target(
        start_at=before.start_at,
        end_at=before.end_at,
        service=before.service,
        professional_id=before.professional_id,
        insurance=before.insurance,
        attendee_name=before.attendee_name,
        phone=target.phone,
        unit_id=before.unit_id,
    )


async def _check_deferred_window(
    before: _Before, target: _Target, request: StaffEditRequest, calendar_for: CalendarFor
) -> bool:
    """The window of a change that waits for the Pix, on the Google agenda it lands on (an
    encaixe skips the free check - the engine's database check still refuses a booked or held
    window). Returns the shared-agenda warning (TASK-083 §7.4)."""
    old_calendar = await calendar_for(_owner_id(before))
    doctor_moved = target.professional_id != before.professional_id
    new_calendar = await calendar_for(target.professional_id) if doctor_moved else old_calendar
    same_calendar = new_calendar is old_calendar or old_calendar.references_same_calendar(
        new_calendar
    )
    try:
        conflicts = await overlaps_other_agenda(new_calendar, target.start_at, target.end_at)
    except Exception:  # noqa: BLE001 - a warning only; the free check below fails closed
        conflicts = False
    if not request.allow_overlap:
        await _ensure_free(
            new_calendar,
            target.start_at,
            target.end_at,
            before.event_id if same_calendar else None,
        )
    return conflicts


def _row_edit(row: Appointment, before: _Before, target: _Target, moved: _Moved | None) -> dict:
    """The R6 edit dict `write_appointment_edit` applies for `target` (no money engine)."""
    edit = {
        "appointment_type": target.service,
        "professional_id": target.professional_id,
        "insurance": target.insurance,
        "attendee_name": target.attendee_name,
        "google_event_id": moved.event_id if moved is not None else row.google_event_id,
        "google_event_link": moved.link if moved is not None else row.google_event_link,
        "start_at": target.start_at,
        "end_at": target.end_at,
        "time_changed": target.start_at != before.start_at,
        "doctor_changed": target.professional_id != before.professional_id,
        "calendar_changed": bool(moved is not None and moved.calendar_changed),
    }
    # TASK-083 final wave I3: a doctor change keeps the booking's unit only if the new doctor
    # attends it (`_unit_kept`, already in `target`); time moves never touch the unit.
    if edit["doctor_changed"] and target.unit_id != before.unit_id:
        edit["unit_id"] = target.unit_id
    return edit


def _pending_edit(
    row: Appointment, before: _Before, target: _Target, moved: _Moved | None
) -> dict:
    """The WHOLE target the money engine prices and writes (contrato §3.6: service, doctor, unit,
    convênio, time, for-whom) - now, or when the difference is paid. `unit_id` is always there
    (None = no unit: the engine prices at the base). `time_changed` says whether the START moves
    (the row writer writes the start only then)."""
    doctor_changed = target.professional_id != before.professional_id
    return {
        "appointment_type": target.service,
        "professional_id": target.professional_id,
        "old_professional_id": before.professional_id if doctor_changed else None,
        "old_google_calendar_source": row.google_calendar_source if doctor_changed else None,
        "insurance": target.insurance,
        "attendee_name": target.attendee_name,
        "google_event_id": moved.event_id if moved is not None else row.google_event_id,
        "google_event_link": moved.link if moved is not None else row.google_event_link,
        "start_at": target.start_at,
        "end_at": target.end_at,
        "time_changed": target.start_at != before.start_at,
        "doctor_changed": doctor_changed,
        "calendar_changed": bool(moved is not None and moved.calendar_changed),
        "unit_id": target.unit_id,
    }


def _repoint_event(row: Appointment, moved: _Moved) -> None:
    """The engine wrote the target onto the row with the event it had; after a late agenda move
    (a waiting change that was written now after all) the row follows the event Google has."""
    row.google_event_id = moved.event_id
    if moved.calendar_changed:
        row.google_event_link = moved.link
        row.google_calendar_source = (
            "professional" if row.professional_id is not None else "clinic"
        )


def _may_newly_charge(tenant: Tenant, appointment: Appointment) -> bool:
    """OQ-10 (contrato §3.7): a consultation WITHOUT any sinal row whose money fields changed may
    owe one now - `recharge_after_edit(old_amount_cents=None)` decides the amount (0 = nothing).
    Only for a patient's own booking in a clinic with the sinal on: a consultation the clinic
    booked itself (no conversation - the hub never asks for a deposit) or one confirmed without
    its sinal on purpose (`deposit_waived_reason`) is never charged by a clinic edit."""
    return bool(
        tenant.pix_deposit_enabled
        and appointment.patient_id is not None
        and appointment.conversation_id is not None
        and not appointment.deposit_waived_reason
    )
