"""The pure pieces of the "Alterar Dados" flow (TASK-032 R6).

Spec: docs/superpowers/specs/2026-10-07-lembretes-alterar-dados-design.md.

Nothing here touches the database, the calendar or a channel: labels and texts, the
`EditDraft` (what the appointment WOULD look like, next to what it is today), the
`EditContext` the worker hands the router (calendars + the Pix guards) and the
complete confirmation text. The steps live in `appointment_edit_flow.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from hashlib import sha256
from typing import Any

from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS


def appointment_email_version(appointment) -> str:
    """Opaque revision of email-relevant booking data; never export their values to a job.

    Ignore confirmation_count/updated_at: replanning reminders legitimately changes
    those after an edit. Canonical UTC keeps the digest stable across database drivers.
    """
    def stamp(value):
        if value is None:
            return None
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat(timespec="microseconds")

    data = [
        str(appointment.id), str(appointment.tenant_id), str(appointment.patient_id),
        str(appointment.professional_id), str(appointment.unit_id), appointment.appointment_type,
        appointment.insurance, appointment.attendee_name, stamp(appointment.start_at),
        stamp(appointment.end_at), appointment.google_event_id, appointment.google_event_link,
    ]
    return sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

# --- Labels: <= 20 characters (Portal buttons, WhatsApp list titles) -------------------
LABEL_EDIT_DATE = "Mudar data"
LABEL_EDIT_TIME = "Mudar horário"
LABEL_EDIT_SERVICE = "Mudar serviço"
LABEL_EDIT_DOCTOR = "Mudar médico"
LABEL_EDIT_MORE = "Outro"
LABEL_EDIT_INSURANCE = "Mudar convênio"
LABEL_EDIT_PATIENT = "Mudar paciente"
LABEL_EDIT_BACK = "Voltar"
LABEL_EDIT_MORE_DATA = "Alterar Mais Dados"
LABEL_TIME_TOO_YES = "Sim, mudar horário"
LABEL_TIME_TOO_NO = "Não, manter horário"

# Availability marks on the doctor list (owner, 2026-10-07).
MARK_FREE = "✅"
MARK_BUSY = "❌"

# --- Texts -------------------------------------------------------------------------------
EDIT_MENU_BODY = (
    "*Alterar Dados*: mudar o horário, a data, o serviço, o médico ou qualquer outra "
    "coisa dessa sua consulta já marcada.\n\n"
    "O que você quer mudar?\n\n"
    "• *Mudar data*: escolher outro dia.\n"
    "• *Mudar horário*: escolher outro horário no mesmo dia.\n"
    "• *Mudar serviço*: trocar o serviço.\n"
    "• *Mudar médico*: trocar o médico.\n"
    "• *Outro*: mudar o convênio ou o paciente."
)
EDIT_MORE_BODY = (
    "Mais opções:\n\n"
    "• *Mudar convênio*: trocar o convênio da consulta.\n"
    "• *Mudar paciente*: a consulta passa a ser para outra pessoa (ou volta a ser sua)."
)
EDIT_TIME_TOO_BODY = "Quer mudar o horário também?"
EDIT_TIME_BUSY = "Esse horário não está livre nesse dia. Escolha outro:"
EDIT_RESLOT_NOTICE = (
    "Com essa mudança o horário atual não está livre. Escolha um novo dia e horário:"
)
EDIT_KEPT = "Tudo bem, mantive sua consulta como estava."
EDIT_APPLIED = "Pronto! Sua consulta foi atualizada. ✅"
EDIT_NOTHING_CHANGED = "Você ainda não mudou nada. O que você quer alterar?"
EDIT_STALE = "Essa consulta não está mais ativa."
EDIT_PIX_NOTICE = (
    "Como essa consulta tem sinal pago, para mudar serviço, médico ou convênio fale com a clínica."
)
EDIT_LIMIT_NOTICE = (
    "O limite de remarcações dessa consulta foi atingido; "
    "para mudar data ou horário fale com a clínica."
)

EDIT_NO_SERVICES = "Esse médico não tem serviços disponíveis no momento."
EDIT_SERVICE_BODY = "Qual serviço você quer para essa consulta?"
EDIT_DOCTOR_BODY = (
    "Qual médico você quer?\n\n"
    f"{MARK_FREE} = livre no dia e horário da consulta\n"
    f"{MARK_BUSY} = ocupado nesse dia e horário"
)

_WEEKDAYS = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)
_FIELDS = ("service", "professional_id", "start_at", "end_at", "insurance", "attendee_name")


def _local_naive(value: datetime, tz: tzinfo) -> str:
    """`value` in the clinic zone as a naive ISO minute ("2026-10-16T15:20")."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(tz).replace(tzinfo=None).isoformat(timespec="minutes")


@dataclass(frozen=True)
class EditDraft:
    """What the appointment would look like (`current`) next to what it is (`original`).

    Immutable: every change returns a new draft. `stage` is transient bookkeeping for
    the step in progress (`mode`, `target_day`, ...) and is never part of the diff.
    `current`/`original` hold: service, professional_id (str | None), start_at/end_at
    (naive clinic-local ISO minutes), insurance, attendee_name (None = the patient).
    """

    appointment_id: str
    current: dict[str, str | None]
    original: dict[str, str | None]
    stage: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_appointment(cls, appt: dict, tz: tzinfo) -> EditDraft:
        values: dict[str, str | None] = {
            "service": appt.get("appointment_type") or None,
            "professional_id": str(appt["professional_id"])
            if appt.get("professional_id")
            else None,
            "start_at": _local_naive(appt["start_at"], tz),
            "end_at": _local_naive(appt["end_at"], tz),
            "insurance": appt.get("insurance") or None,
            "attendee_name": appt.get("attendee_name") or None,
        }
        return cls(appointment_id=str(appt["id"]), current=dict(values), original=dict(values))

    def to_json(self) -> dict:
        return {
            "appointment_id": self.appointment_id,
            "current": dict(self.current),
            "original": dict(self.original),
            "stage": dict(self.stage),
        }

    @classmethod
    def from_json(cls, raw: Any) -> EditDraft | None:
        if not isinstance(raw, dict):
            return None
        current, original = raw.get("current"), raw.get("original")
        if not isinstance(current, dict) or not isinstance(original, dict):
            return None
        if not all(key in current and key in original for key in _FIELDS):
            return None
        if not raw.get("appointment_id"):
            return None
        stage = raw.get("stage")
        return cls(
            appointment_id=str(raw["appointment_id"]),
            current=dict(current),
            original=dict(original),
            stage=dict(stage) if isinstance(stage, dict) else {},
        )

    def with_changes(self, **fields: str | None) -> EditDraft:
        unknown = set(fields) - set(_FIELDS)
        if unknown:
            raise ValueError(f"unknown draft fields: {sorted(unknown)}")
        return replace(self, current={**self.current, **fields})

    def with_stage(self, **stage: str) -> EditDraft:
        return replace(self, stage=dict(stage))

    def changed(self) -> list[str]:
        cur, org = self.current, self.original
        out: list[str] = []
        if str(cur["start_at"])[:10] != str(org["start_at"])[:10]:
            out.append("data")
        if str(cur["start_at"])[11:16] != str(org["start_at"])[11:16]:
            out.append("horário")
        if cur["service"] != org["service"]:
            out.append("serviço")
        if cur["professional_id"] != org["professional_id"]:
            out.append("médico")
        if cur["insurance"] != org["insurance"]:
            out.append("convênio")
        if cur["attendee_name"] != org["attendee_name"]:
            out.append("paciente")
        return out

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(str(self.current["start_at"]))

    @property
    def end(self) -> datetime:
        return datetime.fromisoformat(str(self.current["end_at"]))

    @property
    def duration_minutes(self) -> int:
        return max(1, int((self.end - self.start).total_seconds() // 60))


@dataclass
class EditContext:
    """What the worker hands the router for an edit turn (the router does no I/O).

    `calendars` maps a professional id (str) or "tenant" to that agenda's CalendarService
    (None when it could not be built). `paid_deposit` hides service/doctor/convênio
    (the price may differ); `reschedule_blocked` hides date/time (the Pix reschedule
    limit was reached).
    """

    calendars: dict[str, Any] = field(default_factory=dict)
    paid_deposit: bool = False
    reschedule_blocked: bool = False

    def calendar_for(self, professional_id: str | None) -> Any | None:
        return self.calendars.get(professional_id or "tenant")

    def same_calendar(self, first: str | None, second: str | None) -> bool:
        left, right = self.calendar_for(first), self.calendar_for(second)
        if left is None or right is None:
            return False
        if left is right:
            return True
        compare = getattr(left, "references_same_calendar", None)
        return bool(compare(right)) if compare is not None else False


def _recap_lines(
    draft: EditDraft,
    *,
    doctor: str | None,
    address: str | None,
    requirements: list[str],
    include_extras: bool,
    title: str,
) -> str:
    start = draft.start
    cur = draft.current
    lines = [title, ""]
    lines.append(f"Serviço: {cur['service'] or 'Consulta'}")
    if doctor:
        lines.append(f"Médico: {doctor}")
    lines.append(f"Data: {start.strftime('%d/%m/%Y')} ({_WEEKDAYS[start.weekday()]})")
    lines.append(f"Horário: {start.strftime('%H:%M')}")
    lines.append(f"Convênio: {cur['insurance'] or 'não informado'}")
    lines.append(f"Paciente: {cur['attendee_name']}" if cur["attendee_name"] else "Paciente: você")
    if include_extras and address:
        lines.append(f"Endereço: {address}")
    if include_extras and requirements:
        lines += ["", "Orientações:", *[f"• {item}" for item in requirements]]
    changed = draft.changed()
    lines += ["", f"O que mudou: {', '.join(changed)}." if changed else "Ainda não mudou nada."]
    return "\n".join(lines)


def build_edit_recap(
    draft: EditDraft,
    *,
    doctor: str | None,
    address: str | None,
    requirements: list[str],
    title: str = "Confira como vai ficar sua consulta:",
) -> str:
    """The ONE complete confirmation text: everything the patient needs to check.

    Service, doctor, date (with weekday), time, convênio, patient, the clinic's address
    and the service's orientations (when there are any), then what changed. When it would
    pass the 1024-character card limit the address and the orientations are dropped first;
    the facts the patient is confirming are never cut.
    """
    text = _recap_lines(
        draft,
        doctor=doctor,
        address=address,
        requirements=requirements,
        include_extras=True,
        title=title,
    )
    if len(text) <= MAX_INTERACTIVE_BODY_CHARS:
        return text
    return _recap_lines(
        draft,
        doctor=doctor,
        address=address,
        requirements=requirements,
        include_extras=False,
        title=title,
    )[:MAX_INTERACTIVE_BODY_CHARS]
