"""What the patient is told when the clinic acts on the agenda (TASK-032 R7).

Spec: docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md §2.

* "Confirmar" (staff) -> "Seu médico confirmou ... Você está ciente?" + Confirmar /
  Cancelar / Alterar Dados.
* "Editar/Remarcar"   -> "A clínica alterou ..." with before -> after + the same buttons.
* "Compareceu"        -> the post-consult message, once per appointment.

The buttons are R2's reminder ids pointing at an `appointment_reminders` row of kind
`staff_confirm` / `staff_edit` (services/reminder_schedule.py::ensure_staff_notice_row),
so a tap runs the unchanged reminder flow (workers/shared/reminder_actions.py): Confirmar
counts one more confirmation (R1 dedupes on the row), Cancelar and Alterar Dados follow
R3/R6. Buttons go only where they can work: the clinic's reminders switch ON, fewer than
two confirmations, the appointment not started - otherwise the same text goes alone.

Delivery is `staff_patient_message.send_clinic_notice` (channel, window, paid template).
These functions commit (the card's row must exist before the patient can tap it) and
never raise. Logs carry ids and codes only.
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, Professional, Tenant, is_live_status
from secretaria.models.appointment_reminder import (
    REMINDER_CHANNEL_CHAT,
    REMINDER_CHANNEL_WHATSAPP,
    REMINDER_KIND_STAFF_CONFIRM,
    REMINDER_KIND_STAFF_EDIT,
)
from secretaria.services import reminder_hooks, reminder_schedule
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.patient_context import as_utc
from secretaria.services.reminder_text import (
    ReminderContent,
    load_reminder_content,
    local_start,
    reminder_buttons,
)
from secretaria.services.staff_appointment_edit import FieldChange
from secretaria.services.staff_patient_message import (
    NOTICE_FAILED,
    NoticeResult,
    send_clinic_notice,
)

logger = get_logger(__name__)

CONFIRM_TEXT = "Seu médico confirmou {subject} de {day} às {time} com {doctor}. Você está ciente?"


def _one_line(value) -> str:
    return " ".join(str(value or "").split())


def _subject(content: ReminderContent) -> str:
    attendee = _one_line(content.attendee_name)
    return f"a consulta de {attendee}" if attendee else "sua consulta"


def _doctor(content: ReminderContent) -> str:
    """The professional's name, or R2's fallback "a equipe da <clínica>"."""
    return _one_line(content.doctor_name) or f"a equipe da {_one_line(content.clinic_name)}"


def confirm_text(content: ReminderContent) -> str:
    local = local_start(content)
    return CONFIRM_TEXT.format(
        subject=_subject(content),
        day=f"{local:%d/%m/%Y}",
        time=f"{local:%H:%M}",
        doctor=_doctor(content),
    )


def buttons_allowed(tenant: Tenant, appointment: Appointment, now: datetime) -> bool:
    """Only buttons a tap can honour (the R6 handler refuses every other case)."""
    return (
        is_live_status(appointment.status)
        and reminder_hooks.enabled_for(tenant)
        and (appointment.confirmation_count or 0) < reminder_schedule.MAX_CONFIRMATIONS
        and appointment.start_at is not None
        and as_utc(appointment.start_at) > now
    )


def _row_channel(patient: Patient) -> str:
    return (
        REMINDER_CHANNEL_CHAT
        if patient.channel == CHANNEL_BRAIN_MESSAGE
        else REMINDER_CHANNEL_WHATSAPP
    )


async def _card_notice(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    kind: str,
    body: str,
    allow_paid: bool,
    now: datetime,
    once_per_start: bool,
    expected_start: datetime | None = None,
) -> NoticeResult | None:
    """Row first (committed, so a tap can resolve it), then the card.

    `once_per_start`: None (nothing sent) when this start already has an active `kind`
    row - the patient already has that card. A card that did not reach the patient
    retires the row it just created, so a later retry starts clean.
    """
    appointment = await reminder_schedule._lock_appointment(session, appointment)
    tenant_id, appointment_id = tenant.id, appointment.id
    if once_per_start and (
        not is_live_status(appointment.status)
        or appointment.start_at is None
        or as_utc(appointment.start_at) <= now
        or (expected_start is not None and as_utc(appointment.start_at) != expected_start)
    ):
        return None
    usage_key = f"{kind}:{appointment_id}"
    if (
        patient is None
        or patient.tenant_id != tenant_id
        or appointment.patient_id is None
        or appointment.start_at is None
    ):
        return await send_clinic_notice(
            session,
            tenant,
            appointment,
            patient,
            body=body,
            allow_paid=allow_paid,
            usage_key=usage_key,
            now=now,
        )
    if once_per_start and (
        await reminder_schedule.current_staff_notice_row(session, appointment, kind=kind)
        is not None
    ):
        logger.info(
            "clinic_notice_skipped",
            appointment_id=str(appointment_id),
            kind=kind,
            reason="card_already_shown",
        )
        return None
    with_prompt = buttons_allowed(tenant, appointment, now)
    row, created = await reminder_schedule.ensure_staff_notice_row(
        session,
        appointment,
        kind=kind,
        channel=_row_channel(patient),
        with_prompt=with_prompt,
        now=now,
    )
    if once_per_start and not created:
        return None
    row_id = row.id
    await session.commit()
    result = await send_clinic_notice(
        session,
        tenant,
        appointment,
        patient,
        body=body,
        buttons=reminder_buttons(row_id) if with_prompt else None,
        allow_paid=allow_paid,
        usage_key=f"{kind}:{row_id}",
        now=now,
    )
    if created and not result.delivered:
        await reminder_schedule.retire_staff_notice_row(session, row_id, tenant_id=tenant_id)
        await session.commit()
    logger.info(
        "clinic_notice",
        appointment_id=str(appointment_id),
        kind=kind,
        patient_notice=result.code,
        with_buttons=with_prompt,
    )
    return result


async def notify_staff_confirmation(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    allow_paid: bool,
    now: datetime,
) -> NoticeResult | None:
    """The clinic confirmed: "Você está ciente?" once per appointment time.

    None when nothing was due: the appointment already started (the buttons would be
    refused) or the patient already has this time's confirm card.
    """
    appointment = await reminder_schedule._lock_appointment(session, appointment)
    if (
        not is_live_status(appointment.status)
        or appointment.start_at is None
        or as_utc(appointment.start_at) <= now
    ):
        return None
    expected_start = as_utc(appointment.start_at)
    content = await load_reminder_content(session, tenant, appointment)
    return await _card_notice(
        session,
        tenant,
        appointment,
        patient,
        kind=REMINDER_KIND_STAFF_CONFIRM,
        body=confirm_text(content),
        allow_paid=allow_paid,
        now=now,
        once_per_start=True,
        expected_start=expected_start,
    )


# --- "Compareceu" -> the post-consult message (spec §2) -----------------------------

POST_CONSULT_DEFAULT = (
    "Como foi a sua consulta{doctor}? Conte pra gente como você está se sentindo. "
    "Se precisar de algo, é só responder por aqui."
)


def post_consult_text(tenant: Tenant, doctor_name: str | None) -> str:
    """The clinic's own `post_consult_message`, verbatim; else the default question."""
    own = (tenant.post_consult_message or "").strip()
    if own:
        return own
    doctor = _one_line(doctor_name)
    return POST_CONSULT_DEFAULT.format(doctor=f" com {doctor}" if doctor else "")


async def notify_attended(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    allow_paid: bool,
    now: datetime,
) -> NoticeResult | None:
    """The post-consult message right after "Compareceu", once per appointment.

    `post_consult_notified_at` is claimed with a conditional UPDATE (two clicks, two
    staff: one winner) and committed BEFORE the send; it is cleared again when the
    message did not reach the patient, so the next-open follow-up
    (services/patient_context.py::find_post_consult_followup) still asks later. None =
    already sent for this appointment.
    """
    tenant_id, appointment_id = tenant.id, appointment.id
    claimed = await session.execute(
        update(Appointment)
        .where(
            Appointment.id == appointment_id,
            Appointment.tenant_id == tenant_id,
            Appointment.post_consult_notified_at.is_(None),
        )
        .values(post_consult_notified_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        logger.info("post_consult_notice_skipped", appointment_id=str(appointment_id))
        return None
    await session.commit()
    doctor = None
    if appointment.professional_id is not None:
        doctor = await session.scalar(
            select(Professional.name).where(
                Professional.id == appointment.professional_id,
                Professional.tenant_id == tenant_id,
            )
        )
    result = await send_clinic_notice(
        session,
        tenant,
        appointment,
        patient,
        body=post_consult_text(tenant, doctor),
        allow_paid=allow_paid,
        usage_key=f"postconsult:{appointment_id}",
        now=now,
    )
    if not result.delivered:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == appointment_id, Appointment.tenant_id == tenant_id)
            .values(post_consult_notified_at=None)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    logger.info(
        "post_consult_notice", appointment_id=str(appointment_id), patient_notice=result.code
    )
    return result


# --- "Editar/Remarcar" -> what changed + the same three buttons (spec §2) -----------

EDIT_TITLE = "A clínica alterou {subject}:"
EDIT_NOW = "Agora: {day} às {time} com {doctor}."


def edit_text(changes: Sequence[FieldChange], content: ReminderContent) -> str:
    """Before -> after of each field the clinic changed, then the appointment as it is now."""
    local = local_start(content)
    lines = [EDIT_TITLE.format(subject=_subject(content))]
    lines += [f"• {change.label}: {change.before} → {change.after}" for change in changes]
    lines += [
        "",
        EDIT_NOW.format(day=f"{local:%d/%m/%Y}", time=f"{local:%H:%M}", doctor=_doctor(content)),
    ]
    return "\n".join(lines)


async def notify_staff_edit(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    changes: Sequence[FieldChange],
    allow_paid: bool,
    now: datetime,
) -> NoticeResult:
    """Every edit is told; the card row of this start is re-used across edits."""
    content = await load_reminder_content(session, tenant, appointment)
    result = await _card_notice(
        session,
        tenant,
        appointment,
        patient,
        kind=REMINDER_KIND_STAFF_EDIT,
        body=edit_text(changes, content),
        allow_paid=allow_paid,
        now=now,
        once_per_start=False,
    )
    # once_per_start=False never skips; the fallback only keeps the return type honest.
    return result if result is not None else NoticeResult(NOTICE_FAILED)
