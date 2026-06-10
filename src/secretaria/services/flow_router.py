"""Deterministic (zero-LLM) conversation flows.

The router turns a tenant's `initial_flows` config + the conversation's
`flow_state`/`flow_step` into the next set of WhatsApp bubbles, entirely
without an LLM call for the happy path (menu -> service -> day -> slot ->
confirm -> booked). Anything it can't handle deterministically degrades to
`delegate_llm`, which the worker turns into the normal LangGraph agent turn.

Design:
- `route()` is pure-ish: it reads the conversation/tenant/calendar and returns
  a `FlowRouterResult`. It performs calendar network calls (list_free_slots,
  create_event) but never opens its own DB transaction — the caller persists
  the returned `appointment` and applies the returned flow-state fields.
- Button taps arrive as their label text; slot-list taps arrive as
  "<label> (<iso>)" (see schemas.webhook.extract_inbound_body). The matching
  here relies on that contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from secretaria.ai.formatter import Bubble, ButtonBubble, SlotsBubble, TextBubble
from secretaria.core.logging import get_logger
from secretaria.models import FlowState
from secretaria.services.calendar import CalendarService, CalendarUnavailableError
from secretaria.services.tenant_config import active_appointment_types

if TYPE_CHECKING:
    from secretaria.models import Conversation, Tenant

try:  # dateparser is optional; without it free-text dates fall back to the LLM.
    import dateparser
except Exception:  # pragma: no cover - exercised only when the dep is missing
    dateparser = None

logger = get_logger(__name__)

DEFAULT_MENU_BUTTONS = ["Serviços e Custo", "Horários", "Outro"]
DEFAULT_MENU_LABEL = "Como posso te ajudar?"

# Reactivation ("welcome back" / resume) defaults. Overridable per tenant under
# initial_flows.reactivation.
DEFAULT_REACTIVATION_GAP_MINUTES = 360  # 6h of silence => treat as returning.
DEFAULT_CONTINUE_PROMPT = "Você quer continuar com a nossa última conversa?"
DEFAULT_REACTIVATION_BUTTONS = ["Sim", "Não"]

# Button labels used inside the catalog flow (also the text taps come back as).
LABEL_BOOK_SERVICE = "Sim"
LABEL_OTHER_SERVICE = "Outro serviço"
LABEL_CONFIRM = "Confirmar"
LABEL_CANCEL = "Cancelar"
LABEL_RETRY_YES = "Sim"
LABEL_RETRY_MENU = "Menu principal"

# flow_step values within SERVICE_CATALOG.
STEP_AWAITING_SERVICE = "awaiting_service"
STEP_AWAITING_SERVICE_CONFIRM = "awaiting_service_confirm"
STEP_AWAITING_DAY = "awaiting_day"
STEP_AWAITING_SLOT = "awaiting_slot"
STEP_AWAITING_CONFIRMATION = "awaiting_confirmation"
STEP_AWAITING_RETRY = "awaiting_retry_choice"

_WEEKDAY_PT = {
    "monday": "Segunda",
    "tuesday": "Terça",
    "wednesday": "Quarta",
    "thursday": "Quinta",
    "friday": "Sexta",
    "saturday": "Sábado",
    "sunday": "Domingo",
}
_WEEKDAY_ORDER = list(_WEEKDAY_PT.keys())


@dataclass
class FlowRouterResult:
    """The router's decision for one inbound turn.

    action:
        "reply"               - send `bubbles`, then persist `appointment` (if
                                any) and write the flow-state fields.
        "delegate_llm"        - no bubbles; the caller runs the LLM agent. The
                                flow-state fields are still written first.
        "calendar_unavailable"- the calendar refused/failed; the caller sends
                                the unavailability message and hands off.
    """

    action: Literal["reply", "delegate_llm", "calendar_unavailable"]
    bubbles: list = field(default_factory=list)
    flow_state: FlowState = FlowState.IDLE
    flow_step: str | None = None
    flow_selected_type: str | None = None
    flow_selected_day: str | None = None
    flow_selected_slot: str | None = None
    # When set, an event was created and the caller should persist a row.
    appointment: dict | None = None


@dataclass
class MenuBubble:
    """An N-button reply card (the formatter's ButtonBubble is confirm/cancel
    only). Produced solely by the deterministic flow; the worker renders it via
    WhatsAppClient.send_buttons. The tapped label comes back as the next body.
    """

    body: str
    labels: list[str] = field(default_factory=list)
    kind: Literal["menu"] = "menu"


# --------------------------------------------------------------------------
# Public config helpers (shared with the worker greeting path)
# --------------------------------------------------------------------------


def flows_enabled(tenant: Tenant) -> bool:
    """True when this tenant uses the deterministic entry flows."""
    return bool((tenant.initial_flows or {}).get("enabled"))


def menu_buttons(tenant: Tenant) -> list[str]:
    """The up-to-3 menu button labels (falls back to the MVP defaults)."""
    buttons = (tenant.initial_flows or {}).get("buttons") or DEFAULT_MENU_BUTTONS
    return [str(b) for b in buttons][:3]


def menu_label(tenant: Tenant) -> str:
    """The question shown above the menu buttons."""
    return str((tenant.initial_flows or {}).get("menu_label") or DEFAULT_MENU_LABEL)


# --------------------------------------------------------------------------
# Reactivation config (shared with the worker's returning-patient path)
# --------------------------------------------------------------------------


def _reactivation_config(tenant: Tenant) -> dict:
    return (tenant.initial_flows or {}).get("reactivation") or {}


def reactivation_enabled(tenant: Tenant) -> bool:
    """True when this tenant offers a 'welcome back / continue?' prompt."""
    return bool(_reactivation_config(tenant).get("enabled"))


def reactivation_gap_minutes(tenant: Tenant) -> int:
    """Minutes of silence after which a returning patient is offered to resume."""
    try:
        value = _reactivation_config(tenant).get("gap_minutes")
        return int(value) if value is not None else DEFAULT_REACTIVATION_GAP_MINUTES
    except (TypeError, ValueError):
        return DEFAULT_REACTIVATION_GAP_MINUTES


def reactivation_continue_prompt(tenant: Tenant) -> str:
    """The question appended to the returning greeting (e.g. 'Quer continuar?')."""
    return str(_reactivation_config(tenant).get("continue_prompt") or DEFAULT_CONTINUE_PROMPT)


def reactivation_choice_buttons(tenant: Tenant) -> list[str]:
    """The [yes, no] reply-button labels for the continue prompt (max 3)."""
    buttons = _reactivation_config(tenant).get("buttons") or DEFAULT_REACTIVATION_BUTTONS
    return [str(b) for b in buttons][:3]


def classify_yes_no(body: str | None, tenant: Tenant) -> str:
    """Classify a continue-prompt answer as "yes", "no", or "other".

    Matched against the tenant's configured buttons (and their 20-char tap
    truncation). Anything that is neither button is "other" — the caller treats
    that as "just route their message normally against the preserved state".
    """
    buttons = reactivation_choice_buttons(tenant)
    yes_label = buttons[0] if buttons else DEFAULT_REACTIVATION_BUTTONS[0]
    no_label = buttons[1] if len(buttons) > 1 else DEFAULT_REACTIVATION_BUTTONS[1]
    target = _norm(body)
    if target and (target == _norm(no_label) or target == _norm(no_label[:20])):
        return "no"
    if target and (target == _norm(yes_label) or target == _norm(yes_label[:20])):
        return "yes"
    return "other"


def format_business_hours(hours: dict) -> str:
    """Render business_hours as a readable Portuguese block."""
    lines: list[str] = []
    for day in _WEEKDAY_ORDER:
        windows = (hours or {}).get(day)
        if not windows:
            continue
        ranges = " e ".join(f"{w['start']} às {w['end']}" for w in windows)
        lines.append(f"{_WEEKDAY_PT[day]}: {ranges}")
    return "\n".join(lines) if lines else "Horários ainda não configurados."


# --------------------------------------------------------------------------
# Small internals
# --------------------------------------------------------------------------


def _norm(text: str | None) -> str:
    return (text or "").strip().casefold()


def _menu_index(tenant: Tenant, body: str | None) -> int | None:
    """Index of the menu button whose label matches `body`, or None."""
    target = _norm(body)
    if not target:
        return None
    for index, label in enumerate(menu_buttons(tenant)):
        # send_buttons caps titles at 20 chars, so the tap echoes the truncated
        # label; match on both the full and truncated forms.
        if _norm(label) == target or _norm(label[:20]) == target:
            return index
    return None


def _service_duration(service: dict, tenant: Tenant) -> int:
    try:
        return int(service.get("duration_min") or tenant.appointment_duration_min)
    except (TypeError, ValueError):
        return tenant.appointment_duration_min or 30


def _match_service(services: list[dict], body: str | None) -> dict | None:
    """Find the active service whose (list-truncated) name matches `body`."""
    target = _norm(body)
    if not target:
        return None
    for service in services:
        name = str(service.get("name", ""))
        # send_list caps row titles at 24 chars, so compare on that prefix.
        if _norm(name[:24]) == target or _norm(name) == target:
            return service
    return None


def _parse_day(body: str | None, now: datetime) -> datetime | None:
    """Resolve free-text like "amanhã" / "sexta" / "12/06" into a date.

    Uses dateparser (pt) when available; otherwise a small manual fallback.
    Returns a naive datetime at midnight (clinic-local) or None.
    """
    if not body:
        return None
    text = body.strip()

    # A fully-resolved ISO date (e.g. a stored flow_selected_day reused on
    # retry) round-trips directly, bypassing the ambiguous DMY free-text path.
    try:
        return datetime.fromisoformat(text).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
    except ValueError:
        pass

    text = text.lower()
    manual = _parse_day_manual(text, now)
    if manual is not None:
        return manual

    if dateparser is not None:
        parsed = dateparser.parse(
            body,
            languages=["pt"],
            settings={
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": now.replace(tzinfo=None),
                "DATE_ORDER": "DMY",
            },
        )
        if parsed is not None:
            return parsed.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    return None


_WEEKDAY_WORDS = {
    "segunda": 0, "segunda-feira": 0, "seg": 0,
    "terça": 1, "terca": 1, "terça-feira": 1, "terca-feira": 1, "ter": 1,
    "quarta": 2, "quarta-feira": 2, "qua": 2,
    "quinta": 3, "quinta-feira": 3, "qui": 3,
    "sexta": 4, "sexta-feira": 4, "sex": 4,
    "sábado": 5, "sabado": 5, "sab": 5,
    "domingo": 6, "dom": 6,
}


def _parse_day_manual(text: str, now: datetime) -> datetime | None:
    base = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    if "depois de amanhã" in text or "depois de amanha" in text:
        return base + timedelta(days=2)
    if "amanhã" in text or "amanha" in text:
        return base + timedelta(days=1)
    if "hoje" in text:
        return base

    # dd/mm or dd/mm/yyyy. The leading negative lookbehind stops it from
    # matching the MM-DD slice inside an ISO YYYY-MM-DD string.
    match = re.search(r"(?<![\d/\-])(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?", text)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        year = int(match.group(3)) if match.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            candidate = base.replace(year=year, month=month, day=day)
            # A bare dd/mm in the past rolls to next year.
            if match.group(3) is None and candidate < base:
                candidate = candidate.replace(year=year + 1)
            return candidate
        except ValueError:
            return None

    for word, weekday in _WEEKDAY_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", text):
            ahead = (weekday - base.weekday()) % 7
            if ahead == 0:
                ahead = 7  # "segunda" said on a Monday means next Monday
            return base + timedelta(days=ahead)
    return None


def _slot_iso_from_body(body: str | None) -> datetime | None:
    """Parse the ISO datetime out of a slot-row tap ("14:00 (2026-06-12T14:00)")."""
    if not body:
        return None
    match = re.search(r"\(([^)]+)\)\s*$", body)
    iso = match.group(1).strip() if match else body.strip()
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _menu_bubbles(tenant: Tenant) -> list:
    """The menu prompt rendered as a single reply-button card."""
    return [MenuBubble(body=menu_label(tenant), labels=menu_buttons(tenant))]


def _service_list_bubble(tenant: Tenant, services: list[dict]) -> Bubble:
    rows: list[tuple[str, str]] = [
        (f"svc|{s.get('name', '')}", str(s.get("name", ""))[:24]) for s in services
    ]
    return SlotsBubble(
        body="Qual serviço você gostaria de agendar?",
        rows=rows,
        button_label="Ver serviços",
        section_title="Serviços",
    )


def _preserve(conversation: Conversation, action: str) -> FlowRouterResult:
    """Keep the conversation's current flow fields (used for delegate_llm)."""
    return FlowRouterResult(
        action=action,  # type: ignore[arg-type]
        flow_state=conversation.flow_state,
        flow_step=conversation.flow_step,
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_day=conversation.flow_selected_day,
        flow_selected_slot=conversation.flow_selected_slot,
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


async def route(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    inbound_body: str,
    patient_name: str | None = None,
) -> FlowRouterResult:
    """Decide the next deterministic step for this inbound turn.

    `patient_name` is passed in (read by the caller) so this function performs
    no DB I/O and can run its calendar network calls without holding a session.
    """
    if not flows_enabled(tenant):
        return _preserve(conversation, "delegate_llm")

    state = conversation.flow_state

    # Once in full LLM mode, stay there until a /menu reset.
    if state == FlowState.LLM:
        return FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM)

    if state == FlowState.SERVICE_CATALOG:
        return await _catalog_step(conversation, tenant, calendar, inbound_body, patient_name)

    # IDLE / MENU / BUSINESS_HOURS: interpret as a menu interaction.
    index = _menu_index(tenant, inbound_body)
    if index is None:
        if state == FlowState.MENU:
            # Free text at the menu -> the patient wants something custom.
            return FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM)
        # IDLE/BUSINESS_HOURS: (re)present the menu.
        return FlowRouterResult(
            action="reply", bubbles=_menu_bubbles(tenant), flow_state=FlowState.MENU
        )
    return await _enter_menu_choice(index, tenant, calendar)


async def _enter_menu_choice(
    index: int, tenant: Tenant, calendar: CalendarService | None
) -> FlowRouterResult:
    if index == 0:  # Serviços e Custo
        services = active_appointment_types(tenant)
        if not services:
            return FlowRouterResult(
                action="reply",
                bubbles=[
                    TextBubble(
                        body="No momento não há serviços disponíveis para agendamento."
                    )
                ],
                flow_state=FlowState.IDLE,
            )
        return FlowRouterResult(
            action="reply",
            bubbles=[_service_list_bubble(tenant, services)],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_AWAITING_SERVICE,
        )
    if index == 1:  # Horários (one-shot)
        text = "Nosso horário de atendimento:\n" + format_business_hours(
            tenant.business_hours or {}
        )
        return FlowRouterResult(
            action="reply", bubbles=[TextBubble(body=text)], flow_state=FlowState.IDLE
        )
    # "Outro" (or any 3rd button): hand to the LLM.
    return FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM)


async def _catalog_step(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    patient_name: str | None,
) -> FlowRouterResult:
    step = conversation.flow_step
    services = active_appointment_types(tenant)

    if step == STEP_AWAITING_SERVICE:
        service = _match_service(services, body)
        if service is None:
            return _preserve(conversation, "delegate_llm")
        name = str(service.get("name", ""))
        detail = _service_detail_text(service, tenant)
        return FlowRouterResult(
            action="reply",
            bubbles=[
                ButtonBubble(
                    body=detail,
                    confirm_label=LABEL_BOOK_SERVICE,
                    cancel_label=LABEL_OTHER_SERVICE,
                )
            ],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_AWAITING_SERVICE_CONFIRM,
            flow_selected_type=name,
        )

    if step == STEP_AWAITING_SERVICE_CONFIRM:
        if _norm(body) == _norm(LABEL_BOOK_SERVICE):
            return _ask_day(conversation)
        if _norm(body) == _norm(LABEL_OTHER_SERVICE):
            return FlowRouterResult(
                action="reply",
                bubbles=[_service_list_bubble(tenant, services)],
                flow_state=FlowState.SERVICE_CATALOG,
                flow_step=STEP_AWAITING_SERVICE,
            )
        return _preserve(conversation, "delegate_llm")

    if step == STEP_AWAITING_DAY:
        return await _handle_day(conversation, tenant, calendar, body)

    if step == STEP_AWAITING_SLOT:
        return _handle_slot(conversation, body)

    if step == STEP_AWAITING_CONFIRMATION:
        return await _handle_confirmation(conversation, tenant, calendar, body, patient_name)

    if step == STEP_AWAITING_RETRY:
        if _norm(body) == _norm(LABEL_RETRY_MENU):
            return FlowRouterResult(
                action="reply", bubbles=_menu_bubbles(tenant), flow_state=FlowState.MENU
            )
        if _norm(body) == _norm(LABEL_RETRY_YES):
            return await _handle_day(
                conversation, tenant, calendar, conversation.flow_selected_day or ""
            )
        return _preserve(conversation, "delegate_llm")

    # Unknown step -> let the LLM recover, keep state.
    return _preserve(conversation, "delegate_llm")


def _service_detail_text(service: dict, tenant: Tenant) -> str:
    name = str(service.get("name", "Consulta"))
    price = service.get("price")
    long_description = service.get("long_description") or service.get("description")
    parts = [name]
    if price:
        parts[0] = f"{name} — {price}"
    if long_description:
        parts.append(str(long_description))
    parts.append("Deseja agendar esse serviço?")
    return "\n\n".join(parts)


def _ask_day(conversation: Conversation) -> FlowRouterResult:
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="Para quando você gostaria? (ex: amanhã, sexta, 12/06)")],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_DAY,
        flow_selected_type=conversation.flow_selected_type,
    )


async def _handle_day(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
) -> FlowRouterResult:
    if calendar is None:
        return _preserve(conversation, "delegate_llm")
    now = datetime.now(calendar.tzinfo)
    target = _parse_day(body, now)
    if target is None:
        # Could not understand the date deterministically; let the LLM try.
        return _preserve(conversation, "delegate_llm")

    service = _match_service(
        active_appointment_types(tenant), conversation.flow_selected_type
    )
    duration = _service_duration(service, tenant) if service else (
        tenant.appointment_duration_min or 30
    )
    try:
        slots = await calendar.list_free_slots(
            day=target, slot_minutes=duration, max_slots=8
        )
    except CalendarUnavailableError:
        return FlowRouterResult(
            action="calendar_unavailable",
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_AWAITING_DAY,
            flow_selected_type=conversation.flow_selected_type,
        )

    day_iso = target.date().isoformat()
    if not slots:
        return FlowRouterResult(
            action="reply",
            bubbles=[
                TextBubble(
                    body=(
                        f"Não encontrei horários livres em "
                        f"{target.strftime('%d/%m')}. Quer tentar outro dia?"
                    )
                )
            ],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_AWAITING_DAY,
            flow_selected_type=conversation.flow_selected_type,
            flow_selected_day=day_iso,
        )

    rows = [(f"slot|{s['start']}", s["label"]) for s in slots]
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=f"Horários livres em {target.strftime('%d/%m')}:",
                rows=rows,
                button_label="Ver horários",
                section_title="Horários livres",
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SLOT,
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_day=day_iso,
    )


def _handle_slot(conversation: Conversation, body: str) -> FlowRouterResult:
    start = _slot_iso_from_body(body)
    if start is None:
        return _preserve(conversation, "delegate_llm")
    slot_iso = start.replace(tzinfo=None).isoformat(timespec="minutes")
    recap = (
        f"{conversation.flow_selected_type or 'Consulta'}\n"
        f"{start.strftime('%d/%m/%Y às %H:%M')}"
    )
    return FlowRouterResult(
        action="reply",
        bubbles=[ButtonBubble(body=recap, confirm_label=LABEL_CONFIRM, cancel_label=LABEL_CANCEL)],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_day=conversation.flow_selected_day,
        flow_selected_slot=slot_iso,
    )


async def _handle_confirmation(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    patient_name: str | None,
) -> FlowRouterResult:
    if _norm(body) == _norm(LABEL_CANCEL):
        return FlowRouterResult(
            action="reply",
            bubbles=[
                MenuBubble(
                    body="Sem problema! Quer escolher outro horário?",
                    labels=[LABEL_RETRY_YES, LABEL_RETRY_MENU],
                )
            ],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_AWAITING_RETRY,
            flow_selected_type=conversation.flow_selected_type,
            flow_selected_day=conversation.flow_selected_day,
            flow_selected_slot=conversation.flow_selected_slot,
        )
    if _norm(body) != _norm(LABEL_CONFIRM):
        return _preserve(conversation, "delegate_llm")
    if calendar is None or not conversation.flow_selected_slot:
        return _preserve(conversation, "delegate_llm")

    service_type = conversation.flow_selected_type or "Consulta"
    service = _match_service(active_appointment_types(tenant), service_type)
    duration = _service_duration(service, tenant) if service else (
        tenant.appointment_duration_min or 30
    )
    start = datetime.fromisoformat(conversation.flow_selected_slot)
    if start.tzinfo is None:
        start = start.replace(tzinfo=calendar.tzinfo)
    end = start + timedelta(minutes=duration)

    summary = f"{service_type} - {patient_name}" if patient_name else service_type
    try:
        event = await calendar.create_event(start=start, end=end, summary=summary)
    except CalendarUnavailableError:
        return FlowRouterResult(
            action="calendar_unavailable",
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_AWAITING_CONFIRMATION,
            flow_selected_type=conversation.flow_selected_type,
            flow_selected_day=conversation.flow_selected_day,
            flow_selected_slot=conversation.flow_selected_slot,
        )

    appointment = {
        "google_event_id": event.get("id") or "",
        "google_event_link": event.get("htmlLink"),
        "appointment_type": service_type[:120],
        "start_at": start,
        "end_at": end,
    }
    confirmation = (
        "Pronto! Seu agendamento está confirmado. ✅\n\n"
        f"{service_type}\n{start.strftime('%d/%m/%Y às %H:%M')}"
    )
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=confirmation)],
        flow_state=FlowState.IDLE,
        flow_step=None,
        # Cleared: booking complete.
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        appointment=appointment,
    )


# --------------------------------------------------------------------------
# Resume (re-render the current step without consuming input)
# --------------------------------------------------------------------------


def _preserve_reply(
    conversation: Conversation, bubbles: list, *, step: str | None = None
) -> FlowRouterResult:
    """A `reply` result that keeps the conversation's current flow fields."""
    return FlowRouterResult(
        action="reply",
        bubbles=bubbles,
        flow_state=conversation.flow_state,
        flow_step=conversation.flow_step if step is None else step,
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_day=conversation.flow_selected_day,
        flow_selected_slot=conversation.flow_selected_slot,
    )


def _confirmation_recap(conversation: Conversation) -> str | None:
    """Rebuild the confirmation recap text from the stored slot, or None."""
    slot = conversation.flow_selected_slot
    if not slot:
        return None
    try:
        start = datetime.fromisoformat(slot)
    except ValueError:
        return None
    return (
        f"{conversation.flow_selected_type or 'Consulta'}\n"
        f"{start.strftime('%d/%m/%Y às %H:%M')}"
    )


async def resume_bubbles(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
) -> FlowRouterResult:
    """Re-emit the prompt for the conversation's CURRENT flow step.

    Used when a returning patient taps "continue": it re-renders where they left
    off without consuming input, so the saved flow_state/flow_step/flow_selected_*
    stay unchanged. Falls back to the menu for MENU/unknown states, and to
    `delegate_llm` (via `_handle_day` / `_preserve`) when the calendar is needed
    but unavailable — the caller then degrades to the LLM with history intact.
    """
    state = conversation.flow_state
    step = conversation.flow_step
    services = active_appointment_types(tenant)

    # MENU, or any state without a catalog step: re-present the menu.
    if state != FlowState.SERVICE_CATALOG or step is None:
        return FlowRouterResult(
            action="reply", bubbles=_menu_bubbles(tenant), flow_state=FlowState.MENU
        )

    if step == STEP_AWAITING_SERVICE:
        if not services:
            return FlowRouterResult(
                action="reply", bubbles=_menu_bubbles(tenant), flow_state=FlowState.MENU
            )
        return _preserve_reply(conversation, [_service_list_bubble(tenant, services)])

    if step == STEP_AWAITING_SERVICE_CONFIRM:
        service = _match_service(services, conversation.flow_selected_type)
        if service is None:
            return _preserve_reply(
                conversation,
                [_service_list_bubble(tenant, services)],
                step=STEP_AWAITING_SERVICE,
            )
        return _preserve_reply(
            conversation,
            [
                ButtonBubble(
                    body=_service_detail_text(service, tenant),
                    confirm_label=LABEL_BOOK_SERVICE,
                    cancel_label=LABEL_OTHER_SERVICE,
                )
            ],
        )

    if step == STEP_AWAITING_DAY:
        return _ask_day(conversation)

    if step == STEP_AWAITING_SLOT:
        # Re-list fresh slots for the saved day (availability may have changed).
        return await _handle_day(
            conversation, tenant, calendar, conversation.flow_selected_day or ""
        )

    if step == STEP_AWAITING_CONFIRMATION:
        recap = _confirmation_recap(conversation)
        if recap is None:
            return _ask_day(conversation)  # lost the slot somehow: re-ask the day.
        return _preserve_reply(
            conversation,
            [ButtonBubble(body=recap, confirm_label=LABEL_CONFIRM, cancel_label=LABEL_CANCEL)],
        )

    if step == STEP_AWAITING_RETRY:
        return _preserve_reply(
            conversation,
            [
                MenuBubble(
                    body="Quer escolher outro horário?",
                    labels=[LABEL_RETRY_YES, LABEL_RETRY_MENU],
                )
            ],
        )

    # Unknown step: re-present the menu.
    return FlowRouterResult(
        action="reply", bubbles=_menu_bubbles(tenant), flow_state=FlowState.MENU
    )
