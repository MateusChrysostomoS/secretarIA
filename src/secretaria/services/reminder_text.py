"""The words of an appointment reminder, built once for every surface (TASK-032 R2).

Spec: docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md §4.2.

One content record (`ReminderContent`, loaded by `load_reminder_content`) feeds
every rendering, so the WhatsApp card, the WhatsApp templates, the Portal chat
bubble and the Portal e-mail can never disagree about the doctor, the day or
the time:

* `reminder_sentence` - the spec sentence, verbatim.
* `build_reminder_body` - sentence + requirement bullets, capped at the
  interactive-body limit by dropping bullets (never the sentence).
* `single_line_text` - the same in one line: the only variable of the plain
  fallback template and of the Pix deposit template.
* `template_variables` - the six parameters of the approved v2 template
  (docs/LEMBRETES_MODELOS_META.md).

Dates and times are always rendered in the CLINIC's timezone; a naive
timestamp (SQLite read-back) is UTC. Meta refuses a template parameter with a
newline, a tab or more than four consecutive spaces, so every parameter goes
through `_one_line`. Nothing here logs patient content.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS
from secretaria.models import Appointment, Professional, Tenant
from secretaria.services.service_catalog import (
    load_service_catalog,
    normalize as normalize_service_name,
)
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
)

logger = get_logger(__name__)

DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_SERVICE_NAME = "Consulta"
REQUIREMENTS_HEADER = "Orientações para a consulta:"
NO_REQUIREMENTS_PARAM = "Sem orientações especiais."
# Display-only caps (nothing matches against these strings, so a plain marked
# cut is right - skill third-party-text-limits). A Meta template body is at
# most 1024 characters INCLUDING its fixed text, so the variables stay well
# under that.
REQUIREMENTS_PARAM_MAX_CHARS = 400
SINGLE_LINE_MAX_CHARS = 900
TEMPLATE_PARAM_COUNT = 6
CUT_MARK = "…"


@dataclass(frozen=True)
class ReminderContent:
    """Everything a reminder says, already resolved from the booking."""

    clinic_name: str
    timezone: str | None
    start_at: datetime
    service_name: str
    doctor_name: str | None = None
    attendee_name: str | None = None
    requirements: tuple[str, ...] = ()


def _one_line(value: object) -> str:
    """Collapse every run of whitespace (newline, tab, 5 spaces) into one space."""
    return " ".join(str(value or "").split())


def _cut(text: str, limit: int) -> str:
    """Keep the head and mark the cut. Idempotent for text already within `limit`."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + CUT_MARK


def _clinic_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_start(content: ReminderContent) -> datetime:
    """The appointment start on the clinic's wall clock."""
    start = content.start_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return start.astimezone(_clinic_tz(content.timezone))


def _subject(content: ReminderContent) -> str:
    attendee = _one_line(content.attendee_name)
    return f"A consulta de {attendee}" if attendee else "Sua consulta"


def _doctor(content: ReminderContent) -> str:
    return _one_line(content.doctor_name) or f"a equipe da {_one_line(content.clinic_name)}"


def _service(content: ReminderContent) -> str:
    return _one_line(content.service_name) or DEFAULT_SERVICE_NAME


def _requirement_items(requirements: Iterable[object]) -> list[str]:
    items: list[str] = []
    for raw in requirements or ():
        item = _one_line(raw).rstrip(" .;")
        if item:
            items.append(item)
    return items


def reminder_sentence(content: ReminderContent) -> str:
    """The spec sentence (§4.2), with date AND time in the clinic's timezone."""
    local = local_start(content)
    return (
        f"LEMBRE-SE: {_subject(content)} com {_doctor(content)} está marcada para o dia "
        f"{local:%d/%m/%Y} às {local:%H:%M} para {_service(content)}."
    )


def build_reminder_body(
    content: ReminderContent, *, max_chars: int = MAX_INTERACTIVE_BODY_CHARS
) -> str:
    """Sentence + one bullet per requirement, within `max_chars`.

    Over budget, requirement bullets are dropped from the end (a final "…"
    line says some were left out); the sentence itself is never cut unless it
    alone is over budget.
    """
    sentence = reminder_sentence(content)
    items = _requirement_items(content.requirements)
    for keep in range(len(items), 0, -1):
        bullets = "\n".join(f"• {item}" for item in items[:keep])
        if keep < len(items):
            bullets += f"\n{CUT_MARK}"
        body = f"{sentence}\n\n{REQUIREMENTS_HEADER}\n{bullets}"
        if len(body) <= max_chars:
            return body
    return _cut(sentence, max_chars)


def flatten_requirements(
    requirements: Iterable[object], *, max_chars: int = REQUIREMENTS_PARAM_MAX_CHARS
) -> str:
    """The requirements as ONE template-safe line (never empty: Meta rejects it)."""
    items = _requirement_items(requirements)
    if not items:
        return NO_REQUIREMENTS_PARAM
    return _cut(f"Orientações: {'; '.join(items)}.", max_chars)


def single_line_text(content: ReminderContent) -> str:
    """Sentence + flattened requirements in one line (single-variable templates)."""
    line = _one_line(f"{reminder_sentence(content)} {flatten_requirements(content.requirements)}")
    return _cut(line, SINGLE_LINE_MAX_CHARS)


def template_variables(content: ReminderContent) -> list[str]:
    """The six parameters of REMINDER_V2_TEMPLATE_NAME, in Meta's {{1}}..{{6}} order."""
    local = local_start(content)
    values = [
        _subject(content),
        _doctor(content),
        f"{local:%d/%m/%Y}",
        f"{local:%H:%M}",
        _service(content),
        flatten_requirements(content.requirements),
    ]
    return [_one_line(value) or "-" for value in values]


async def load_reminder_content(
    session: AsyncSession, tenant: Tenant, appointment: Appointment
) -> ReminderContent:
    """Resolve doctor, service requirements and attendee for one appointment.

    Same catalog resolution as the greeting's upcoming-appointment block
    (workers/shared/greeting.py::_load_upcoming_greeting_data), but the
    professional lookup is scoped to the tenant: a stray foreign id names
    nobody. A catalog failure degrades to "no requirements", never raises.
    """
    owner = None
    if appointment.professional_id is not None:
        owner = await session.scalar(
            select(Professional).where(
                Professional.id == appointment.professional_id,
                Professional.tenant_id == tenant.id,
            )
        )
    service_name = (appointment.appointment_type or "").strip() or DEFAULT_SERVICE_NAME
    requirements: tuple[str, ...] = ()
    try:
        # A SAVEPOINT: on Postgres a failed statement aborts the whole
        # transaction, and the engine's later queries would die with it.
        async with session.begin_nested():
            services = await load_service_catalog(session, tenant.id)
        catalog = (
            professional_appointment_types(owner, tenant, services)
            if owner is not None
            else active_appointment_types(tenant, services)
        )
        target = normalize_service_name(service_name)
        match = next(
            (svc for svc in catalog if normalize_service_name(svc.get("name")) == target),
            None,
        )
        if match is not None:
            requirements = tuple(
                str(item).strip() for item in (match.get("requirements") or []) if str(item).strip()
            )
    except Exception as exc:
        logger.warning(
            "reminder_content_catalog_failed",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
            error_type=type(exc).__name__,
        )
    return ReminderContent(
        clinic_name=tenant.clinic_name,
        timezone=tenant.timezone,
        start_at=appointment.start_at,
        service_name=service_name,
        doctor_name=owner.name if owner is not None else None,
        attendee_name=appointment.attendee_name,
        requirements=requirements,
    )


# --- Buttons ------------------------------------------------------------------
# "<action>|<reminder_id>": the reminder ROW id carries tenant, patient and
# appointment, so a tap can be checked against the patient who tapped
# (workers/shared/reminder_actions.py) - the older "apptconfirm|<appointment_id>"
# family can only be checked against the tenant. The decoder side lists the
# same prefixes in schemas/webhook.py::_ACTION_BUTTON_PREFIXES; tests/
# test_reminder_v2_text.py pins that both lists agree.
ACTION_CONFIRM = "remconfirm"
ACTION_CANCEL = "remcancel"
ACTION_OTHER = "remother"
ACTION_EDIT = "remedit"
REMINDER_ACTIONS: tuple[str, ...] = (ACTION_CONFIRM, ACTION_CANCEL, ACTION_EDIT)

# <= 20 characters each (core/whatsapp_limits.py::MAX_BUTTON_LABEL_CHARS). The
# approved template keeps its legacy third label until resubmission to Meta.
LABEL_CONFIRM = "Confirmar"
LABEL_CANCEL = "Cancelar"
LABEL_OTHER = "Outro"
# TASK-032 R6 (owner, 2026-10-07): the third button of the reminder is "Alterar Dados".
# The approved WhatsApp TEMPLATE (used outside the 24 h window) still carries "Outro" until
# the owner resubmits it to Meta; its payload is positional, so that button already acts as
# "Alterar Dados" (docs/LEMBRETES_MODELOS_META.md).
LABEL_EDIT = "Alterar Dados"
# The Pix paid-deposit variant keeps its historic trio
# (plugins/reminders.py::_DEPOSIT_REMINDER_BUTTONS). Only Confirmar moves to the
# reminder id (so it counts); Reagendar/Cancelar keep the appointment-scoped ids
# whose handlers apply the Pix reschedule limit and refund window
# (workers/shared/actions.py).
LABEL_DEPOSIT_RESCHEDULE = "Reagendar"


def reminder_buttons(reminder_id) -> list[tuple[str, str]]:
    """(id, label) pairs of a reminder: Confirmar / Cancelar / Alterar Dados."""
    return [
        (f"{ACTION_CONFIRM}|{reminder_id}", LABEL_CONFIRM),
        (f"{ACTION_CANCEL}|{reminder_id}", LABEL_CANCEL),
        (f"{ACTION_EDIT}|{reminder_id}", LABEL_EDIT),
    ]


def deposit_reminder_buttons(reminder_id, appointment_id) -> list[tuple[str, str]]:
    """The Pix paid-deposit trio: Confirmar / Reagendar / Cancelar."""
    return [
        (f"{ACTION_CONFIRM}|{reminder_id}", LABEL_CONFIRM),
        (f"apptresched|{appointment_id}", LABEL_DEPOSIT_RESCHEDULE),
        (f"apptcancel|{appointment_id}", LABEL_CANCEL),
    ]


def button_payloads(buttons: list[tuple[str, str]]) -> list[str]:
    """Just the ids, for a template's quick-reply `button_payloads` (same order)."""
    return [button_id for button_id, _label in buttons]


# --- The "Cancelar" path (TASK-032 R3, spec §4.3) -------------------------------
# Same "<action>|<reminder_id>" shape as the trio above, so every tap of the
# path is checked against the patient who tapped
# (workers/shared/reminder_actions.py). `REMINDER_ACTIONS` stays the trio the
# reminder itself carries; the dispatcher and the Portal decoder use the union.
ACTION_RESCHEDULE_THIS = "remresched"
ACTION_BOOK_ANOTHER = "remnew"
ACTION_GIVE_UP = "remgiveup"
ACTION_GIVE_UP_CONFIRM = "remgiveupyes"
ACTION_KEEP = "remkeep"
# Ids R2/R3 put on cards a patient may still have on screen (or in a template tapped
# later): they keep working (workers/shared/reminder_actions.py maps them) but no new card
# carries them. `remgiveup` is today's "Cancelar"; the other three open the edit flow.
LEGACY_REMINDER_ACTIONS: tuple[str, ...] = (
    ACTION_OTHER,
    ACTION_RESCHEDULE_THIS,
    ACTION_BOOK_ANOTHER,
    ACTION_GIVE_UP,
)
# The two steps of the cancel confirmation card (`give_up_confirm_buttons`).
CANCEL_PATH_ACTIONS: tuple[str, ...] = (ACTION_GIVE_UP_CONFIRM, ACTION_KEEP)
REMINDER_ROW_ACTIONS: tuple[str, ...] = (
    REMINDER_ACTIONS + LEGACY_REMINDER_ACTIONS + CANCEL_PATH_ACTIONS
)

# Labels of the cancel confirmation card: <= 20 characters each.
LABEL_GIVE_UP_CONFIRM = "Sim, cancelar"
LABEL_KEEP = "Manter consulta"


def give_up_confirm_buttons(reminder_id) -> list[tuple[str, str]]:
    """The give-up confirmation: Sim, cancelar / Manter consulta."""
    return [
        (f"{ACTION_GIVE_UP_CONFIRM}|{reminder_id}", LABEL_GIVE_UP_CONFIRM),
        (f"{ACTION_KEEP}|{reminder_id}", LABEL_KEEP),
    ]


def portal_conversation_link(tenant_id) -> str | None:
    """Where the reminder e-mail sends a Portal patient: the clinic's invite link.

    `{BRAIN_MESSAGE_PORTAL_URL}/clinicas/?convite=<tenant uuid>` is a shape
    brain-api already accepts (core/invite_codes.py::parse_invite reads a bare
    clinic UUID; tenant ids are shared across the mesh), so no brain-api call
    and no new contract. A patient with a live session lands in the
    conversation; one without signs in first. None while the URL is unset.
    """
    base = (get_settings().BRAIN_MESSAGE_PORTAL_URL or "").strip().rstrip("/")
    return f"{base}/clinicas/?convite={tenant_id}" if base else None
