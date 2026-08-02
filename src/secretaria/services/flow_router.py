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
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from secretaria.ai.formatter import Bubble, ButtonBubble, SlotsBubble, TextBubble
from secretaria.ai.scoped_help import run_professional_help, run_service_help
from secretaria.core.logging import get_logger
from secretaria.models import FlowState
from secretaria.services.calendar import CalendarService, CalendarUnavailableError
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
)

if TYPE_CHECKING:
    from secretaria.models import Conversation, Tenant

try:  # dateparser is optional; without it free-text dates fall back to the LLM.
    import dateparser
except Exception:  # pragma: no cover - exercised only when the dep is missing
    dateparser = None

logger = get_logger(__name__)

DEFAULT_MENU_BUTTONS = ["Serviços e Custo", "Remarcar/Cancelar", "Outro"]
DEFAULT_MENU_LABEL = "Como posso te ajudar?"

# Label that opens the cancel/reschedule sub-flow. Matched (case-insensitively,
# 20-char truncation aware) before the index-based menu mapping, so a tenant can
# place it in any menu slot via initial_flows.buttons.
LABEL_MANAGE = "Remarcar/Cancelar"

# Multi-doctor menu (tenants with 2+ active professionals AND flows enabled):
# the effective menu becomes exactly these 3 buttons, replacing the configured
# ones. All <=20 chars — WhatsApp truncates reply-button titles at 20 and the
# tap echoes the truncated label. The manage flow stays reachable by TYPING the
# configured manage label (matched before the menu mapping, same as today);
# WhatsApp's 3-buttons-per-message cap leaves no visible slot for it here.
BTN_CHOOSE_PROFESSIONAL = "Escolher médico"
BTN_FIND_PROFESSIONAL = "Procurar médico"
LABEL_OTHER = "Outro"

# Deterministic opener sent on a "Procurar médico" tap, right before the
# conversation flips to sticky LLM mode — the recommendation itself is the
# agent's job (list_professionals over specialty+about; see ai/prompts.py).
FIND_PROFESSIONAL_OPENER = (
    "Me conta o que você está sentindo ou o motivo da consulta, "
    "que eu te indico o profissional certo."
)

# Convênio step fixed rows (row titles: <=24 chars) + the free-text prompt a
# patient gets after tapping "Outro convênio".
LABEL_INSURANCE_PARTICULAR = "Particular"
LABEL_INSURANCE_OTHER = "Outro convênio"
INSURANCE_PROMPT_OTHER = "Qual é o nome do seu convênio?"

# WhatsApp caps interactive lists at 10 rows. Professionals beyond the cap are
# dropped (with a warning) — pagination is out of scope this round. Insurance
# plans keep 2 slots for the fixed Particular/Outro rows. The professional and
# service lists both reserve their LAST row for the fixed "Não sei"
# scoped-help entry, so real options cap at 9 there — without the reserve,
# send_list's silent [:10] would drop the help row exactly when the catalog is
# fullest.
MAX_PROFESSIONAL_ROWS = 10
MAX_CATALOG_OPTION_ROWS = MAX_PROFESSIONAL_ROWS - 1
MAX_INSURANCE_PLAN_ROWS = 8
# Same hard WhatsApp limit for the manage flow's "pick an appointment" list
# (_manage_pick_list_bubble). A patient with more upcoming appointments than
# this is not expected in practice; extras are dropped (with a warning).
MAX_MANAGE_APPOINTMENT_ROWS = 10

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

# Button labels used inside the manage (cancel/reschedule) flow.
LABEL_RESCHEDULE = "Remarcar"
LABEL_CANCEL_APPT = "Cancelar"
LABEL_BACK = "Voltar"
LABEL_YES = "Sim"
LABEL_NO = "Não"

# The greeting's fixed, product-defined action trios (workers/tasks.py's
# _greeting_buttons_for - NEVER the clinic's own free text; see
# docs/CHECKPOINT_fixed_greeting_buttons.md). The initial/generic greeting
# sends [LABEL_BOOK, LABEL_MANAGE_APPOINTMENT, LABEL_OTHER]; the
# returning-patient-with-upcoming-appointment greeting keeps its own
# [LABEL_RESCHEDULE, LABEL_CANCEL_APPT, LABEL_OTHER] trio unchanged. Every one
# of these labels is matched by `route()`'s IDLE dispatch below on BOTH
# single- and multi-doctor tenants. LABEL_MANAGE_APPOINTMENT consolidates the
# old separate Remarcar/Cancelar slots: it opens the SAME manage sub-flow
# (_enter_manage - identify the appointment first, THEN ask
# reschedule-or-cancel via _manage_action_card), freeing a slot for
# LABEL_OTHER. 18 chars - under WhatsApp's 20-char button-title cap, so the
# tap echoes it untruncated.
LABEL_BOOK = "Agendar"
LABEL_MANAGE_APPOINTMENT = "Gerenciar consulta"

# Fixed last row appended to BOTH catalog lists (professional and service):
# tapping it opens the matching scoped-help LLM node (STEP_PROFESSIONAL_HELP /
# STEP_SERVICE_HELP below) instead of requiring the patient to pick blind.
LABEL_DONT_KNOW = "Não sei"

# Fixed, scope-specific openers each "Não sei" tap replies with (the LLM only
# enters on the patient's ANSWER, one turn later). Deliberately two distinct
# nodes with two distinct questions - "which professional fits my case" and
# "which service fits my need" are different scopes, and neither is the
# open-ended "Outro" hand-off (ai/scoped_help.py's module docstring).
PROFESSIONAL_HELP_OPENER = (
    "O que você está sentindo, ou o que você precisa? "
    "Vou te ajudar a escolher o profissional certo."
)
SERVICE_HELP_OPENER = (
    "Me conta o que você precisa, que eu te ajudo a escolher o serviço certo."
)
# Sent when a scoped-help node gives up (bounded at one clarifying question) -
# the conversation is then flipped to human handover (action="handover").
SCOPED_HELP_ESCALATE_MESSAGE = (
    "Vou te conectar com alguém da nossa equipe para te ajudar. Só um momento. 🙏"
)

# flow_step values within SERVICE_CATALOG. The two professional-branch steps
# (multi-doctor tenants) sit AHEAD of the existing ones: professional -> service
# -> service confirm -> [insurance] -> day -> slot -> confirm.
STEP_AWAITING_PROFESSIONAL = "awaiting_professional"
STEP_AWAITING_SERVICE = "awaiting_service"
STEP_AWAITING_SERVICE_CONFIRM = "awaiting_service_confirm"
STEP_AWAITING_INSURANCE = "awaiting_insurance"
STEP_AWAITING_DAY = "awaiting_day"
STEP_AWAITING_SLOT = "awaiting_slot"
STEP_AWAITING_CONFIRMATION = "awaiting_confirmation"
STEP_AWAITING_RETRY = "awaiting_retry_choice"
# Scoped-help ("Não sei") steps, still within SERVICE_CATALOG. The *_FINAL
# variant marks the last allowed exchange: entered after the node's single
# clarifying question, and a clarify outcome there escalates instead - the
# 1-2-exchange bound is enforced by this step machine, not by the prompt.
STEP_PROFESSIONAL_HELP = "professional_help"
STEP_PROFESSIONAL_HELP_FINAL = "professional_help_final"
STEP_SERVICE_HELP = "service_help"
STEP_SERVICE_HELP_FINAL = "service_help_final"

# flow_step values within MANAGE_BOOKING. The reschedule day/slot/confirm steps
# mirror the booking ones but persist via update_event instead of create_event.
STEP_MANAGE_PICK = "manage_pick"
STEP_MANAGE_ACTION = "manage_action"
STEP_MANAGE_CANCEL_CONFIRM = "manage_cancel_confirm"
STEP_MANAGE_DAY = "manage_day"
STEP_MANAGE_SLOT = "manage_slot"
STEP_MANAGE_CONFIRM = "manage_confirm"
# Pick-list steps for the direct "Remarcar"/"Cancelar" entries
# (`enter_manage_action`): same tappable list as STEP_MANAGE_PICK, but the tap
# resolves straight into the picked intent instead of the neutral action card.
STEP_MANAGE_PICK_RESCHEDULE = "manage_pick_reschedule"
STEP_MANAGE_PICK_CANCEL = "manage_pick_cancel"

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
        "handover"            - send `bubbles`, then flip the conversation to
                                human handover (a scoped-help node escalated).
                                No owner alert email - unlike a calendar
                                outage, nothing is broken; the human secretary
                                sees the chat in their own WhatsApp app.
    """

    action: Literal["reply", "delegate_llm", "calendar_unavailable", "handover"]
    bubbles: list = field(default_factory=list)
    flow_state: FlowState = FlowState.IDLE
    flow_step: str | None = None
    flow_selected_type: str | None = None
    flow_selected_day: str | None = None
    flow_selected_slot: str | None = None
    # Multi-doctor branch: the professional the patient picked, and the
    # convênio label they chose/typed. Written unconditionally by the caller
    # (like every field above), so any result that should keep them must carry
    # them explicitly.
    flow_selected_professional_id: UUID | None = None
    flow_selected_insurance: str | None = None
    # The appointment being cancelled/rescheduled inside MANAGE_BOOKING
    # (replaces the old flow_selected_type overload). Written unconditionally
    # by the caller like every field above, so any manage-flow result that
    # should keep it must carry it explicitly; non-manage results leave it
    # None, and the completed cancel/reschedule results clear it on purpose.
    flow_managing_appointment_id: UUID | None = None
    # When set, an event was created and the caller should persist a row.
    appointment: dict | None = None
    # When set, the matching appointment row should be flipped to CANCELLED.
    appointment_cancel_id: str | None = None
    # When set, the matching appointment row should be moved (RESCHEDULED):
    # {"google_event_id", "start_at", "end_at"}.
    appointment_reschedule: dict | None = None


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


def _is_multi_professional(professionals: list | None) -> bool:
    """2+ active professionals -> the multi-doctor menu replaces the default."""
    return len(professionals or []) > 1


def menu_buttons_for(tenant: Tenant, multi_professional: bool) -> list[str]:
    """Effective menu labels: the fixed multi-doctor trio, or the tenant's own.

    Shared with workers/tasks.py (the greeting card doubles as the menu when
    flows are enabled, and the reactivation-reset path re-renders it) so every
    surface shows the same effective menu.
    """
    if multi_professional:
        return [BTN_CHOOSE_PROFESSIONAL, BTN_FIND_PROFESSIONAL, LABEL_OTHER]
    return menu_buttons(tenant)


def menu_label(tenant: Tenant) -> str:
    """The question shown above the menu buttons."""
    return str((tenant.initial_flows or {}).get("menu_label") or DEFAULT_MENU_LABEL)


def manage_label(tenant: Tenant) -> str:
    """The button label that opens the cancel/reschedule sub-flow."""
    return str((tenant.initial_flows or {}).get("manage_label") or LABEL_MANAGE)


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


def _label_match(body: str | None, label: str) -> bool:
    """True when `body` equals `label` (or its 20-char button truncation)."""
    target = _norm(body)
    return bool(target) and (target == _norm(label) or target == _norm(label[:20]))


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


def _menu_bubbles(tenant: Tenant, professionals: list | None = None) -> list:
    """The menu prompt rendered as a single reply-button card."""
    labels = menu_buttons_for(tenant, _is_multi_professional(professionals))
    return [MenuBubble(body=menu_label(tenant), labels=labels)]


def _selected_professional_id(conversation: Conversation) -> UUID | None:
    """The conversation's picked professional (getattr: snapshots may predate it)."""
    return getattr(conversation, "flow_selected_professional_id", None)


def _selected_insurance(conversation: Conversation) -> str | None:
    """The conversation's stored convênio label (getattr: same rationale)."""
    return getattr(conversation, "flow_selected_insurance", None)


def _selected_managing_appointment_id(conversation: Conversation) -> UUID | None:
    """The conversation's in-progress manage-flow target (getattr: same rationale)."""
    return getattr(conversation, "flow_managing_appointment_id", None)


def _service_list_bubble(tenant: Tenant, services: list[dict]) -> Bubble:
    if len(services) > MAX_CATALOG_OPTION_ROWS:
        logger.warning(
            "flow_service_list_truncated",
            total=len(services),
            shown=MAX_CATALOG_OPTION_ROWS,
        )
    rows: list[tuple[str, str]] = [
        (f"svc|{s.get('name', '')}", str(s.get("name", ""))[:24])
        for s in services[:MAX_CATALOG_OPTION_ROWS]
    ]
    # Fixed last row: opens the scoped service-help node (STEP_SERVICE_HELP).
    # The id prefix is NOT "svc|" on purpose - extract_inbound_body echoes the
    # row TITLE for unknown prefixes, so the tap arrives as plain "Não sei".
    rows.append(("svchelp|0", LABEL_DONT_KNOW))
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
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
        flow_managing_appointment_id=_selected_managing_appointment_id(conversation),
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
    upcoming_appointments: list[dict] | None = None,
    professionals: list | None = None,
) -> FlowRouterResult:
    """Decide the next deterministic step for this inbound turn.

    `patient_name` and `upcoming_appointments` are passed in (read by the caller)
    so this function performs no DB I/O and can run its calendar network calls
    without holding a session. `upcoming_appointments` is the patient's future
    SCHEDULED appointments (dicts: id, google_event_id, appointment_type,
    start_at, end_at, professional_id), loaded by the worker only when the
    manage flow is active.

    `professionals` is the tenant's ACTIVE professionals snapshot (plain
    objects: id, name, specialty, about, appointment_types), loaded by the
    worker when flows are enabled — same pattern as `upcoming_appointments`.
    With 2+ entries the menu becomes the multi-doctor trio and the
    professional-selection branch activates; None/0/1 keeps today's behavior
    unchanged. When the conversation already has a selected professional, the
    caller passes THAT professional's resolved CalendarService as `calendar`.
    """
    if not flows_enabled(tenant):
        return _preserve(conversation, "delegate_llm")

    state = conversation.flow_state

    # Once in full LLM mode, stay there until a /menu reset (or the agent's
    # show_main_menu tool). The selected professional/insurance survive so the
    # agent keeps that doctor's context across LLM turns.
    if state == FlowState.LLM:
        return FlowRouterResult(
            action="delegate_llm",
            flow_state=FlowState.LLM,
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=_selected_insurance(conversation),
        )

    if state == FlowState.SERVICE_CATALOG:
        return await _catalog_step(
            conversation, tenant, calendar, inbound_body, patient_name, professionals
        )

    if state == FlowState.MANAGE_BOOKING:
        return await _manage_step(
            conversation, tenant, calendar, inbound_body, upcoming_appointments or [], professionals
        )

    # IDLE / MENU / BUSINESS_HOURS: interpret as a menu interaction. The manage
    # label is matched first so a tenant can place it in any menu slot (and so
    # it stays reachable by typing on the multi-doctor menu, which has no
    # visible manage button).
    if _label_match(inbound_body, manage_label(tenant)):
        return _enter_manage(tenant, upcoming_appointments or [], professionals)

    # Direct greeting-button taps: skip the neutral menu and go straight for
    # the tapped intent. "Agendar"/"Gerenciar consulta"/"Outro" is the
    # initial-greeting trio; "Remarcar"/"Cancelar" still arrive from the
    # returning-patient-with-upcoming-appointment trio (unchanged - see
    # _greeting_buttons_for). All checked before the multi-doctor dispatch so
    # they win on BOTH single- and multi-doctor tenants alike (the
    # multi-doctor menu has no visible manage button, but these labels can
    # still arrive via the greeting buttons). Safe against the booking flow's
    # own "Cancelar"/"Confirmar": those only ever arrive while
    # flow_state == SERVICE_CATALOG, dispatched above this block (state-based
    # dispatch runs first - see the SERVICE_CATALOG/MANAGE_BOOKING branches
    # earlier in this function).
    if _label_match(inbound_body, LABEL_BOOK):
        return enter_booking(tenant, professionals)
    # "Gerenciar consulta": the same manage sub-flow as the classic manage
    # label - identify the appointment first (with _enter_manage's
    # single-appointment shortcut), THEN ask reschedule-or-cancel via the
    # existing _manage_action_card. Never re-asks doctor/service.
    if _label_match(inbound_body, LABEL_MANAGE_APPOINTMENT):
        return _enter_manage(tenant, upcoming_appointments or [], professionals)
    if _label_match(inbound_body, LABEL_RESCHEDULE):
        return enter_manage_action(
            "reschedule", tenant, upcoming_appointments or [], professionals
        )
    if _label_match(inbound_body, LABEL_CANCEL_APPT):
        return enter_manage_action("cancel", tenant, upcoming_appointments or [], professionals)

    # "Outro" from the greeting trio must always reach the LLM, even on a
    # single-doctor tenant whose configured menu buttons don't include it
    # (the index-based mapping below only knows the configured labels). Same
    # place-it-anywhere semantics as the manage label above; identical result
    # to _enter_menu_choice's 3rd slot and _menu_choice_multi's labels[2].
    if _label_match(inbound_body, LABEL_OTHER):
        return FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM)

    if _is_multi_professional(professionals):
        return _menu_choice_multi(conversation, tenant, inbound_body, professionals or [])

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


def _menu_choice_multi(
    conversation: Conversation, tenant: Tenant, body: str, professionals: list
) -> FlowRouterResult:
    """Menu interaction for a multi-doctor tenant (fixed 3-button menu).

    "Escolher médico" opens the tappable doctor list; "Procurar médico" sends
    one deterministic opener and flips to sticky LLM mode (the agent does the
    matching — see ai/prompts.py); "Outro" hands off exactly like today.
    """
    labels = menu_buttons_for(tenant, True)
    if _label_match(body, labels[0]):
        return _enter_professional_list(tenant, professionals)
    if _label_match(body, labels[1]):
        return FlowRouterResult(
            action="reply",
            bubbles=[TextBubble(body=FIND_PROFESSIONAL_OPENER)],
            flow_state=FlowState.LLM,
        )
    if _label_match(body, labels[2]):
        return FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM)
    if conversation.flow_state == FlowState.MENU:
        # Free text at the menu -> the patient wants something custom.
        return FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM)
    # IDLE/BUSINESS_HOURS: (re)present the effective menu.
    return FlowRouterResult(
        action="reply", bubbles=_menu_bubbles(tenant, professionals), flow_state=FlowState.MENU
    )


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


def enter_booking(tenant: Tenant, professionals: list | None = None) -> FlowRouterResult:
    """Deterministic entry for a direct "Agendar" tap (fixed greeting button).

    Mirrors `_enter_menu_choice`'s index-0 branch (single-doctor: straight to
    the service catalog) and `_menu_choice_multi`'s "Escolher médico" branch
    (multi-doctor: the tappable doctor list first) - same empty-state replies
    as those existing paths, reused as-is. `route()`'s IDLE dispatch calls
    this BEFORE the multi-doctor check (like LABEL_RESCHEDULE/LABEL_CANCEL_APPT
    above), so "Agendar" works identically on single- and multi-doctor tenants.
    """
    if _is_multi_professional(professionals):
        return _enter_professional_list(tenant, professionals or [])
    services = active_appointment_types(tenant)
    if not services:
        return FlowRouterResult(
            action="reply",
            bubbles=[TextBubble(body="No momento não há serviços disponíveis para agendamento.")],
            flow_state=FlowState.IDLE,
        )
    return FlowRouterResult(
        action="reply",
        bubbles=[_service_list_bubble(tenant, services)],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE,
    )


# --------------------------------------------------------------------------
# Professional selection (multi-doctor branch of SERVICE_CATALOG)
# --------------------------------------------------------------------------


def _professional_id_from_body(body: str | None) -> UUID | None:
    """Parse the professional UUID out of a list-row tap ("Dra. Ana (uuid)").

    Mirrors `_slot_iso_from_body`: schemas.webhook.extract_inbound_body turns a
    "prof|<uuid>" row id into "<title> (<uuid>)", so the UUID rides in the
    trailing parentheses (or is the whole body when the row had no title).
    """
    if not body:
        return None
    match = re.search(r"\(([^)]+)\)\s*$", body)
    raw = match.group(1).strip() if match else body.strip()
    try:
        return UUID(raw)
    except ValueError:
        return None


def _find_professional_by_id(professionals: list | None, professional_id) -> Any | None:
    if professional_id is None:
        return None
    for professional in professionals or []:
        if professional.id == professional_id:
            return professional
    return None


def _match_professional(professionals: list, body: str | None) -> Any | None:
    """Resolve a tapped/typed professional: embedded UUID first, then name.

    The name fallback (24-char list-title truncation aware, like
    `_match_service`) covers a patient TYPING a doctor's name instead of
    tapping the row. None -> the caller degrades to the LLM.
    """
    tapped_id = _professional_id_from_body(body)
    if tapped_id is not None:
        found = _find_professional_by_id(professionals, tapped_id)
        if found is not None:
            return found
    target = _norm(body)
    if not target:
        return None
    for professional in professionals:
        name = str(professional.name)
        if _norm(name[:24]) == target or _norm(name) == target:
            return professional
    return None


def _enter_professional_list(tenant: Tenant, professionals: list) -> FlowRouterResult:
    """Render the tappable doctor list ("Escolher médico").

    Each row carries the professional's specialty as the WhatsApp list-row
    description — the "apresentação dos médicos" made tappable. Real options
    cap at MAX_CATALOG_OPTION_ROWS (the 10-row WhatsApp hard limit minus the
    reserved "Não sei" scoped-help row appended last); beyond that we log and
    truncate, pagination is explicitly out of scope.
    """
    if len(professionals) > MAX_CATALOG_OPTION_ROWS:
        logger.warning(
            "flow_professional_list_truncated",
            total=len(professionals),
            shown=MAX_CATALOG_OPTION_ROWS,
        )
    rows = [
        (
            f"prof|{professional.id}",
            str(professional.name)[:24],
            (getattr(professional, "specialty", None) or None),
        )
        for professional in professionals[:MAX_CATALOG_OPTION_ROWS]
    ]
    # Fixed last row: opens the scoped professional-help node
    # (STEP_PROFESSIONAL_HELP). Not "prof|"-prefixed on purpose - the tap must
    # arrive as the plain "Não sei" title (see extract_inbound_body), never as
    # a pseudo-professional id.
    rows.append(("profhelp|0", LABEL_DONT_KNOW, "Te ajudo a escolher"))
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body="Com qual profissional você gostaria de agendar?",
                rows=rows,
                button_label="Ver profissionais",
                section_title="Profissionais",
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_PROFESSIONAL,
    )


def _professional_greeting_body(professional: Any) -> str:
    """v1 doctor greeting body: `specialty` (short line) then `about` verbatim.

    Both fields are hub-editable; `about` is documented patient-facing on the
    model. NEVER `context_doctor_message` here — that is LLM-internal persona
    text (ai/prompts.py injects it with an explicit do-not-recite
    instruction); reciting it would leak internal instructions to a patient.
    Empty when neither field is set, so no empty card is ever sent.
    """
    parts = [
        str(part).strip()
        for part in (
            getattr(professional, "specialty", None),
            getattr(professional, "about", None),
        )
        if part and str(part).strip()
    ]
    return "\n\n".join(parts)


def _enter_professional_services(professional: Any, tenant: Tenant) -> FlowRouterResult:
    """The selected doctor's greeting + THEIR services list.

    Factored out of the tap handler because the LLM hand-back tool
    (`select_professional_and_continue`) re-enters the deterministic flow
    through this exact sequence — see workers/tasks.py.
    """
    bubbles: list = []
    greeting = _professional_greeting_body(professional)
    if greeting:
        bubbles.append(TextBubble(body=greeting))
    services = professional_appointment_types(professional, tenant)
    if not services:
        bubbles.append(
            TextBubble(body="No momento não há serviços disponíveis para agendamento.")
        )
        return FlowRouterResult(
            action="reply",
            bubbles=bubbles,
            flow_state=FlowState.IDLE,
            flow_selected_professional_id=professional.id,
        )
    bubbles.append(_service_list_bubble(tenant, services))
    return FlowRouterResult(
        action="reply",
        bubbles=bubbles,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE,
        flow_selected_professional_id=professional.id,
    )


# --------------------------------------------------------------------------
# Convênio step (multi-doctor branch; informational only, by design)
# --------------------------------------------------------------------------


def _tenant_insurances(tenant: Tenant) -> list[str]:
    return [str(plan) for plan in (getattr(tenant, "insurances", None) or []) if str(plan).strip()]


def _wants_insurance_step(conversation: Conversation, tenant: Tenant) -> bool:
    """Convênio step: professional branch only, when the clinic collects it.

    Clinic-wide by design (tenants.insurances / collect_insurance) — it never
    depends on WHICH doctor was picked and never filters doctors or slots.
    Single-professional tenants keep today's flow untouched (no step).
    """
    return (
        _selected_professional_id(conversation) is not None
        and bool(getattr(tenant, "collect_insurance", False))
        and bool(_tenant_insurances(tenant))
    )


def _match_insurance_plan(tenant: Tenant, body: str | None) -> str | None:
    """Canonical plan (or "Particular") whose row title matches the tap/text."""
    target = _norm(body)
    if not target:
        return None
    if _label_match(body, LABEL_INSURANCE_PARTICULAR):
        return LABEL_INSURANCE_PARTICULAR
    for plan in _tenant_insurances(tenant):
        # send_list caps row titles at 24 chars, so compare on that prefix too.
        if _norm(plan[:24]) == target or _norm(plan) == target:
            return plan
    return None


def _enter_insurance(conversation: Conversation, tenant: Tenant) -> FlowRouterResult:
    """Render the convênio list: the clinic's plans + Particular + Outro."""
    plans = _tenant_insurances(tenant)
    if len(plans) > MAX_INSURANCE_PLAN_ROWS:
        logger.warning(
            "flow_insurance_list_truncated", total=len(plans), shown=MAX_INSURANCE_PLAN_ROWS
        )
    rows: list[tuple[str, str]] = [
        (f"ins|{plan}", plan[:24]) for plan in plans[:MAX_INSURANCE_PLAN_ROWS]
    ]
    rows.append(("ins|particular", LABEL_INSURANCE_PARTICULAR))
    rows.append(("ins|outro", LABEL_INSURANCE_OTHER))
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body="Você vai usar convênio? Escolha uma opção:",
                rows=rows,
                button_label="Ver convênios",
                section_title="Convênios",
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_INSURANCE,
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_professional_id=_selected_professional_id(conversation),
    )


def _handle_insurance(conversation: Conversation, tenant: Tenant, body: str) -> FlowRouterResult:
    """Record the convênio answer, then ask the day. Informational only.

    Tapping "Outro convênio" asks for the plan's name and stays on this step;
    anything else — a listed plan's tap, "Particular", or free text — is
    stored as-is (canonicalized to the full plan name when it matches one)
    and copied onto the appointment at booking time. Never filters anything.
    """
    if _label_match(body, LABEL_INSURANCE_OTHER):
        return FlowRouterResult(
            action="reply",
            bubbles=[TextBubble(body=INSURANCE_PROMPT_OTHER)],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_AWAITING_INSURANCE,
            flow_selected_type=conversation.flow_selected_type,
            flow_selected_professional_id=_selected_professional_id(conversation),
        )
    stored = (_match_insurance_plan(tenant, body) or (body or "").strip())[:120] or None
    result = _ask_day(conversation)
    result.flow_selected_insurance = stored
    return result


# --------------------------------------------------------------------------
# Scoped-help nodes ("Não sei" on the professional / service lists)
# --------------------------------------------------------------------------
#
# The "Não sei" tap itself is deterministic (a fixed scope-specific opener +
# a step flip); the LLM only runs on the patient's ANSWER, via
# ai/scoped_help.py — a single structured decision grounded on the exact
# options snapshot this router is holding, never the full agent. Its pick is
# re-validated here through the same matchers a direct tap uses
# (_match_professional/_match_service), so the hand-back re-enters the flow
# exactly like a tap would; anything unresolvable escalates to a human
# (action="handover") instead of looping. An LLM/network failure degrades to
# the general agent (delegate_llm), same as any other unhandled turn.


def _enter_professional_help(conversation: Conversation) -> FlowRouterResult:
    """Reply with the professional-scope opener; the LLM runs on the answer."""
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=PROFESSIONAL_HELP_OPENER)],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_PROFESSIONAL_HELP,
        flow_selected_insurance=_selected_insurance(conversation),
    )


def _enter_service_help(conversation: Conversation) -> FlowRouterResult:
    """Reply with the service-scope opener; keeps the selected professional."""
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=SERVICE_HELP_OPENER)],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_SERVICE_HELP,
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
    )


def _scoped_help_escalate() -> FlowRouterResult:
    """Bounded exit: fixed message + human handover, flow reset to IDLE."""
    return FlowRouterResult(
        action="handover",
        bubbles=[TextBubble(body=SCOPED_HELP_ESCALATE_MESSAGE)],
        flow_state=FlowState.IDLE,
        flow_step=None,
    )


async def _handle_professional_help(
    conversation: Conversation, tenant: Tenant, body: str, professionals: list
) -> FlowRouterResult:
    final_round = conversation.flow_step == STEP_PROFESSIONAL_HELP_FINAL
    try:
        outcome = await run_professional_help(
            conversation_id=getattr(conversation, "id", None),
            professionals=professionals,
            patient_message=body,
            final_round=final_round,
        )
    except Exception as exc:
        logger.warning("flow_professional_help_failed", error=str(exc))
        return _preserve(conversation, "delegate_llm")
    if outcome.kind == "pick":
        professional = _match_professional(professionals, outcome.choice)
        if professional is not None:
            return _enter_professional_services(professional, tenant)
        # A pick that doesn't resolve against the real roster (hallucinated /
        # deactivated mid-exchange) must never be offered back to the patient.
        logger.warning("flow_professional_help_pick_unresolved")
        return _scoped_help_escalate()
    if outcome.kind == "clarify" and not final_round:
        return FlowRouterResult(
            action="reply",
            bubbles=[TextBubble(body=outcome.question or "")],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_PROFESSIONAL_HELP_FINAL,
            flow_selected_insurance=_selected_insurance(conversation),
        )
    return _scoped_help_escalate()


async def _handle_service_help(
    conversation: Conversation, tenant: Tenant, body: str, services: list[dict]
) -> FlowRouterResult:
    final_round = conversation.flow_step == STEP_SERVICE_HELP_FINAL
    try:
        outcome = await run_service_help(
            conversation_id=getattr(conversation, "id", None),
            services=services,
            patient_message=body,
            final_round=final_round,
        )
    except Exception as exc:
        logger.warning("flow_service_help_failed", error=str(exc))
        return _preserve(conversation, "delegate_llm")
    if outcome.kind == "pick":
        service = _match_service(services, outcome.choice)
        if service is not None:
            return _enter_service_detail(service, conversation, tenant)
        logger.warning("flow_service_help_pick_unresolved")
        return _scoped_help_escalate()
    if outcome.kind == "clarify" and not final_round:
        return FlowRouterResult(
            action="reply",
            bubbles=[TextBubble(body=outcome.question or "")],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_SERVICE_HELP_FINAL,
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=_selected_insurance(conversation),
        )
    return _scoped_help_escalate()


async def _catalog_step(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    patient_name: str | None,
    professionals: list | None = None,
) -> FlowRouterResult:
    step = conversation.flow_step

    # Multi-doctor branch: with a professional selected, every later step is
    # scoped to THAT professional — their services here, their calendar via
    # the `calendar` the worker resolved. A stale selection (deactivated /
    # removed mid-flow) must never book against the wrong scope: degrade to
    # the LLM instead.
    selected_professional = _find_professional_by_id(
        professionals, _selected_professional_id(conversation)
    )
    if _selected_professional_id(conversation) is not None and selected_professional is None:
        return _preserve(conversation, "delegate_llm")
    services = (
        professional_appointment_types(selected_professional, tenant)
        if selected_professional is not None
        else active_appointment_types(tenant)
    )

    if step == STEP_AWAITING_PROFESSIONAL:
        if _label_match(body, LABEL_DONT_KNOW):
            return _enter_professional_help(conversation)
        professional = _match_professional(professionals or [], body)
        if professional is None:
            return _preserve(conversation, "delegate_llm")
        return _enter_professional_services(professional, tenant)

    if step in (STEP_PROFESSIONAL_HELP, STEP_PROFESSIONAL_HELP_FINAL):
        return await _handle_professional_help(conversation, tenant, body, professionals or [])

    if step == STEP_AWAITING_SERVICE:
        if _label_match(body, LABEL_DONT_KNOW):
            return _enter_service_help(conversation)
        service = _match_service(services, body)
        if service is None:
            return _preserve(conversation, "delegate_llm")
        return _enter_service_detail(service, conversation, tenant)

    if step in (STEP_SERVICE_HELP, STEP_SERVICE_HELP_FINAL):
        return await _handle_service_help(conversation, tenant, body, services)

    if step == STEP_AWAITING_SERVICE_CONFIRM:
        if _norm(body) == _norm(LABEL_BOOK_SERVICE):
            if _wants_insurance_step(conversation, tenant):
                return _enter_insurance(conversation, tenant)
            return _ask_day(conversation)
        if _norm(body) == _norm(LABEL_OTHER_SERVICE):
            return FlowRouterResult(
                action="reply",
                bubbles=[_service_list_bubble(tenant, services)],
                flow_state=FlowState.SERVICE_CATALOG,
                flow_step=STEP_AWAITING_SERVICE,
                flow_selected_professional_id=_selected_professional_id(conversation),
            )
        return _preserve(conversation, "delegate_llm")

    if step == STEP_AWAITING_INSURANCE:
        return _handle_insurance(conversation, tenant, body)

    if step == STEP_AWAITING_DAY:
        return await _handle_day(conversation, tenant, calendar, body, services)

    if step == STEP_AWAITING_SLOT:
        return _handle_slot(conversation, body)

    if step == STEP_AWAITING_CONFIRMATION:
        return await _handle_confirmation(
            conversation, tenant, calendar, body, patient_name, services
        )

    if step == STEP_AWAITING_RETRY:
        if _norm(body) == _norm(LABEL_RETRY_MENU):
            return FlowRouterResult(
                action="reply",
                bubbles=_menu_bubbles(tenant, professionals),
                flow_state=FlowState.MENU,
            )
        if _norm(body) == _norm(LABEL_RETRY_YES):
            return await _handle_day(
                conversation, tenant, calendar, conversation.flow_selected_day or "", services
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


def _enter_service_detail(
    service: dict, conversation: Conversation, tenant: Tenant
) -> FlowRouterResult:
    """The matched service's detail + Sim/Outro serviço confirm card.

    Factored out of `_catalog_step`'s STEP_AWAITING_SERVICE branch because the
    scoped service-help node (`_handle_service_help`) re-enters the
    deterministic flow through this exact result after a validated pick —
    mirroring how `_enter_professional_services` serves both the tap and the
    LLM hand-backs.
    """
    return FlowRouterResult(
        action="reply",
        bubbles=[
            ButtonBubble(
                body=_service_detail_text(service, tenant),
                confirm_label=LABEL_BOOK_SERVICE,
                cancel_label=LABEL_OTHER_SERVICE,
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type=str(service.get("name", "")),
        flow_selected_professional_id=_selected_professional_id(conversation),
    )


def _ask_day(conversation: Conversation) -> FlowRouterResult:
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="Para quando você gostaria? (ex: amanhã, sexta, 12/06)")],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_DAY,
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
    )


async def _handle_day(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    services: list[dict] | None = None,
) -> FlowRouterResult:
    """List free slots for the parsed day.

    `services` is the already-scoped catalog (the selected professional's own
    when the multi-doctor branch is active, else the tenant's) — `calendar` is
    scoped the same way by the caller, so slot listing and duration agree.
    """
    if calendar is None:
        return _preserve(conversation, "delegate_llm")
    now = datetime.now(calendar.tzinfo)
    target = _parse_day(body, now)
    if target is None:
        # Could not understand the date deterministically; let the LLM try.
        return _preserve(conversation, "delegate_llm")

    if services is None:
        services = active_appointment_types(tenant)
    service = _match_service(services, conversation.flow_selected_type)
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
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=_selected_insurance(conversation),
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
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=_selected_insurance(conversation),
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
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
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
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
    )


async def _handle_confirmation(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    patient_name: str | None,
    services: list[dict] | None = None,
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
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=_selected_insurance(conversation),
        )
    if _norm(body) != _norm(LABEL_CONFIRM):
        return _preserve(conversation, "delegate_llm")
    if calendar is None or not conversation.flow_selected_slot:
        return _preserve(conversation, "delegate_llm")

    service_type = conversation.flow_selected_type or "Consulta"
    if services is None:
        services = active_appointment_types(tenant)
    service = _match_service(services, service_type)
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
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=_selected_insurance(conversation),
        )

    appointment = {
        "google_event_id": event.get("id") or "",
        "google_event_link": event.get("htmlLink"),
        "appointment_type": service_type[:120],
        "start_at": start,
        "end_at": end,
    }
    # Multi-doctor branch: attach the selection to the booked row. Both keys
    # are OMITTED (not None) outside that branch, so a single-professional
    # tenant's appointment dict stays byte-identical to today's.
    professional_id = _selected_professional_id(conversation)
    if professional_id is not None:
        appointment["professional_id"] = professional_id
    insurance = _selected_insurance(conversation)
    if insurance:
        appointment["insurance"] = insurance
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
# Manage flow (cancel / reschedule an existing appointment)
# --------------------------------------------------------------------------


def _appt_row_label(appt: dict) -> str:
    """Compact list-row title for one appointment (WhatsApp caps titles at 24)."""
    start = appt.get("start_at")
    when = start.strftime("%d/%m %H:%M") if isinstance(start, datetime) else "?"
    appt_type = str(appt.get("appointment_type") or "Consulta")
    return f"{when} {appt_type}"[:24]


def _appt_summary(appt: dict) -> str:
    """Full one-line description used in confirmation bubbles."""
    start = appt.get("start_at")
    label = str(appt.get("appointment_type") or "Consulta")
    if isinstance(start, datetime):
        return f"{label}\n{start.strftime('%d/%m/%Y às %H:%M')}"
    return label


def _appt_duration_minutes(appt: dict, tenant: Tenant) -> int:
    """Original appointment length, derived from its window (tenant default)."""
    start, end = appt.get("start_at"), appt.get("end_at")
    if isinstance(start, datetime) and isinstance(end, datetime) and end > start:
        return max(int((end - start).total_seconds() // 60), 1)
    return tenant.appointment_duration_min or 30


def _find_appt_by_iso(appointments: list[dict], body: str) -> dict | None:
    """Resolve the appointment whose start matches the tapped slot-row ISO."""
    tapped = _slot_iso_from_body(body)
    if tapped is None:
        return None
    for appt in appointments:
        start = appt.get("start_at")
        if isinstance(start, datetime) and start == tapped:
            return appt
    return None


def _find_appt_by_id(appointments: list[dict], appt_id: str | None) -> dict | None:
    """Re-resolve the selected appointment by its stored UUID string."""
    if not appt_id:
        return None
    for appt in appointments:
        if str(appt.get("id")) == appt_id:
            return appt
    return None


def _managing_appt_id_str(conversation: Conversation) -> str | None:
    """The conversation's manage-flow target, as the string `_find_appt_by_id`
    compares against (that helper deliberately keeps comparing strings)."""
    managing_id = _selected_managing_appointment_id(conversation)
    return str(managing_id) if managing_id is not None else None


def _appt_uuid(appt: dict) -> UUID | None:
    """UUID of `appt["id"]` (a string - see patient_context.load_upcoming_appointments)."""
    try:
        return UUID(str(appt.get("id")))
    except (TypeError, ValueError):
        return None


def _manage_pick_list_bubble(appointments: list[dict], body: str) -> SlotsBubble:
    """The tappable list of the patient's future appointments.

    Factored out of `_enter_manage` so `enter_manage_action`'s multi-
    appointment branch can reuse it with an intent-specific `body`. Capped at
    MAX_MANAGE_APPOINTMENT_ROWS (WhatsApp's hard list-row limit) — beyond that
    we log a COUNT-ONLY warning and truncate, mirroring `_enter_professional_list`.
    Never logs appointment contents (dates, types).
    """
    valid = [appt for appt in appointments if isinstance(appt.get("start_at"), datetime)]
    if len(valid) > MAX_MANAGE_APPOINTMENT_ROWS:
        logger.warning(
            "flow_manage_appointment_list_truncated",
            total=len(valid),
            shown=MAX_MANAGE_APPOINTMENT_ROWS,
        )
    rows = [
        (f"slot|{appt['start_at'].isoformat()}", _appt_row_label(appt))
        for appt in valid[:MAX_MANAGE_APPOINTMENT_ROWS]
    ]
    return SlotsBubble(
        body=body,
        rows=rows,
        button_label="Ver consultas",
        section_title="Suas consultas",
    )


def _enter_manage(
    tenant: Tenant, appointments: list[dict], professionals: list | None = None
) -> FlowRouterResult:
    """Open the cancel/reschedule flow: list the patient's future appointments.

    Exactly ONE upcoming appointment skips the pick list and goes straight to
    its action card (`_manage_action_card` - the same Remarcar/Cancelar/Voltar
    question a pick would land on): a one-row "qual consulta?" list asks a
    question whose answer the system already knows. Same shortcut precedent as
    `enter_manage_action`'s single-appointment branch. Serves both entries
    into this function - the greeting's "Gerenciar consulta" button and the
    classic configurable manage label.
    """
    if not appointments:
        return FlowRouterResult(
            action="reply",
            bubbles=[
                TextBubble(body="Você não tem nenhuma consulta agendada no momento."),
                *_menu_bubbles(tenant, professionals),
            ],
            flow_state=FlowState.MENU,
        )
    if len(appointments) == 1:
        appt = appointments[0]
        return FlowRouterResult(
            action="reply",
            bubbles=_manage_action_card(appt),
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=STEP_MANAGE_ACTION,
            flow_managing_appointment_id=_appt_uuid(appt),
        )
    return FlowRouterResult(
        action="reply",
        bubbles=[
            _manage_pick_list_bubble(appointments, "Qual consulta você quer remarcar ou cancelar?")
        ],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_PICK,
    )


def enter_manage_action(
    intent: Literal["reschedule", "cancel"],
    tenant: Tenant,
    appointments: list[dict],
    professionals: list | None = None,
    preselected_id: UUID | None = None,
) -> FlowRouterResult:
    """Deterministic entry for a direct "Remarcar"/"Cancelar" tap at the menu.

    Unlike `_enter_manage` (which always shows the neutral "o que você
    gostaria de fazer?" action card after a pick), this goes straight for the
    tapped intent:
      - `preselected_id` set AND found in `appointments` -> begins that intent
        directly on IT, regardless of how many other appointments the patient
        has (PROMPT S3: a reminder's action-button tap already names the exact
        appointment - see workers/tasks.py::_handle_action_button - so the
        normal 0/1/2+ disambiguation below would be actively wrong: it could
        preselect the wrong one, or make the patient pick again for no
        reason). Not found (stale/foreign/no-longer-upcoming) falls through
        to the normal behavior below, as if no preselection had been given.
      - no appointments -> `_enter_manage`'s empty-list reply, reused as-is.
      - exactly one -> begins that intent directly on it (NEAREST == only,
        nothing to disambiguate).
      - 2+ -> the same tappable pick-list `_enter_manage` shows, with an
        intent-specific prompt; the tap resolves through
        STEP_MANAGE_PICK_RESCHEDULE / STEP_MANAGE_PICK_CANCEL (`_manage_step`).
    """
    if preselected_id is not None:
        preselected = _find_appt_by_id(appointments, str(preselected_id))
        if preselected is not None:
            managing_id = _appt_uuid(preselected)
            if intent == "reschedule":
                return _begin_reschedule(managing_id)
            return _begin_cancel(managing_id, preselected)
    if not appointments:
        return _enter_manage(tenant, appointments, professionals)
    if len(appointments) == 1:
        appt = appointments[0]
        managing_id = _appt_uuid(appt)
        if intent == "reschedule":
            return _begin_reschedule(managing_id)
        return _begin_cancel(managing_id, appt)
    prompt = (
        "Qual consulta você quer remarcar?"
        if intent == "reschedule"
        else "Qual consulta você quer cancelar?"
    )
    step = STEP_MANAGE_PICK_RESCHEDULE if intent == "reschedule" else STEP_MANAGE_PICK_CANCEL
    return FlowRouterResult(
        action="reply",
        bubbles=[_manage_pick_list_bubble(appointments, prompt)],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=step,
    )


def _manage_action_card(appt: dict) -> list:
    """The Remarcar / Cancelar / Voltar choices for the picked appointment."""
    return [
        MenuBubble(
            body=f"{_appt_summary(appt)}\n\nO que você gostaria de fazer?",
            labels=[LABEL_RESCHEDULE, LABEL_CANCEL_APPT, LABEL_BACK],
        )
    ]


def _begin_reschedule(managing_id: UUID | None) -> FlowRouterResult:
    """Ask for the new day, targeting `managing_id`.

    Shared by STEP_MANAGE_ACTION's "Remarcar" branch and
    `enter_manage_action`'s single-appointment shortcut - identical
    patient-visible behavior to what STEP_MANAGE_ACTION always did.
    """
    # PROMPT 4 hook: when the payment lifecycle lands, carry the existing
    # deposit onto the new slot (no refund, no re-charge), subject to
    # reschedule_limit.
    return FlowRouterResult(
        action="reply",
        bubbles=[
            TextBubble(body="Para quando você gostaria de remarcar? (ex: amanhã, sexta, 12/06)")
        ],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_DAY,
        flow_managing_appointment_id=managing_id,
    )


def _begin_cancel(managing_id: UUID | None, appt: dict | None) -> FlowRouterResult:
    """Ask Sim/Não to confirm cancelling `managing_id` (`appt` drives the summary text).

    Shared by STEP_MANAGE_ACTION's "Cancelar" branch and
    `enter_manage_action`'s single-appointment shortcut. `appt` may be None
    (e.g. a stale pick no longer present in the loaded snapshot) - the
    confirmation still proceeds with a generic summary, matching what
    STEP_MANAGE_ACTION always did.
    """
    summary = _appt_summary(appt) if appt else "essa consulta"
    return FlowRouterResult(
        action="reply",
        bubbles=[
            MenuBubble(
                body=f"Confirmar o cancelamento?\n\n{summary}",
                labels=[LABEL_YES, LABEL_NO],
            )
        ],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CANCEL_CONFIRM,
        flow_managing_appointment_id=managing_id,
    )


async def _manage_step(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    appointments: list[dict],
    professionals: list | None = None,
) -> FlowRouterResult:
    step = conversation.flow_step

    if step == STEP_MANAGE_PICK:
        appt = _find_appt_by_iso(appointments, body)
        if appt is None:
            return _preserve(conversation, "delegate_llm")
        return FlowRouterResult(
            action="reply",
            bubbles=_manage_action_card(appt),
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=STEP_MANAGE_ACTION,
            flow_managing_appointment_id=_appt_uuid(appt),
        )

    if step == STEP_MANAGE_PICK_RESCHEDULE:
        appt = _find_appt_by_iso(appointments, body)
        if appt is None:
            return _preserve(conversation, "delegate_llm")
        return _begin_reschedule(_appt_uuid(appt))

    if step == STEP_MANAGE_PICK_CANCEL:
        appt = _find_appt_by_iso(appointments, body)
        if appt is None:
            return _preserve(conversation, "delegate_llm")
        return _begin_cancel(_appt_uuid(appt), appt)

    if step == STEP_MANAGE_ACTION:
        if _norm(body) == _norm(LABEL_BACK):
            return FlowRouterResult(
                action="reply",
                bubbles=_menu_bubbles(tenant, professionals),
                flow_state=FlowState.MENU,
            )
        managing_id = _selected_managing_appointment_id(conversation)
        if _norm(body) == _norm(LABEL_CANCEL_APPT):
            appt = _find_appt_by_id(appointments, _managing_appt_id_str(conversation))
            return _begin_cancel(managing_id, appt)
        if _norm(body) == _norm(LABEL_RESCHEDULE):
            return _begin_reschedule(managing_id)
        return _preserve(conversation, "delegate_llm")

    if step == STEP_MANAGE_CANCEL_CONFIRM:
        return await _manage_cancel(
            conversation, tenant, calendar, body, appointments, professionals
        )

    if step == STEP_MANAGE_DAY:
        return await _manage_handle_day(conversation, tenant, calendar, body, appointments)

    if step == STEP_MANAGE_SLOT:
        return _manage_handle_slot(conversation, appointments, body)

    if step == STEP_MANAGE_CONFIRM:
        return await _manage_reschedule(
            conversation, tenant, calendar, body, appointments, professionals
        )

    # Unknown step -> let the LLM recover, keep state.
    return _preserve(conversation, "delegate_llm")


async def _manage_cancel(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    appointments: list[dict],
    professionals: list | None = None,
) -> FlowRouterResult:
    # PROMPT 4 hook: deposit refund/retention on cancellation is Prompt 4's
    # scope — nothing charged or refunded here.
    if _norm(body) == _norm(LABEL_NO):
        return FlowRouterResult(
            action="reply",
            bubbles=[
                TextBubble(body="Tudo bem, sua consulta foi mantida."),
                *_menu_bubbles(tenant, professionals),
            ],
            flow_state=FlowState.MENU,
        )
    if _norm(body) != _norm(LABEL_YES):
        return _preserve(conversation, "delegate_llm")

    appt = _find_appt_by_id(appointments, _managing_appt_id_str(conversation))
    if appt is None or calendar is None:
        return _preserve(conversation, "delegate_llm")
    event_id = str(appt.get("google_event_id") or "")
    if not event_id:
        return _preserve(conversation, "delegate_llm")

    try:
        await calendar.cancel_event(event_id)
    except CalendarUnavailableError:
        return FlowRouterResult(
            action="calendar_unavailable",
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=STEP_MANAGE_CANCEL_CONFIRM,
            flow_managing_appointment_id=_selected_managing_appointment_id(conversation),
        )
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="Pronto! Sua consulta foi cancelada. ✅")],
        flow_state=FlowState.IDLE,
        flow_step=None,
        appointment_cancel_id=event_id,
    )


async def _manage_handle_day(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    appointments: list[dict],
) -> FlowRouterResult:
    if calendar is None:
        return _preserve(conversation, "delegate_llm")
    appt = _find_appt_by_id(appointments, _managing_appt_id_str(conversation))
    if appt is None:
        return _preserve(conversation, "delegate_llm")
    now = datetime.now(calendar.tzinfo)
    target = _parse_day(body, now)
    if target is None:
        return _preserve(conversation, "delegate_llm")

    managing_id = _selected_managing_appointment_id(conversation)
    duration = _appt_duration_minutes(appt, tenant)
    try:
        slots = await calendar.list_free_slots(
            day=target, slot_minutes=duration, max_slots=8
        )
    except CalendarUnavailableError:
        return FlowRouterResult(
            action="calendar_unavailable",
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=STEP_MANAGE_DAY,
            flow_managing_appointment_id=managing_id,
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
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=STEP_MANAGE_DAY,
            flow_managing_appointment_id=managing_id,
            flow_selected_day=day_iso,
        )

    rows = [(f"slot|{s['start']}", s["label"]) for s in slots]
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=f"Novos horários em {target.strftime('%d/%m')}:",
                rows=rows,
                button_label="Ver horários",
                section_title="Horários livres",
            )
        ],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_SLOT,
        flow_managing_appointment_id=managing_id,
        flow_selected_day=day_iso,
    )


def _manage_handle_slot(
    conversation: Conversation, appointments: list[dict], body: str
) -> FlowRouterResult:
    start = _slot_iso_from_body(body)
    if start is None:
        return _preserve(conversation, "delegate_llm")
    slot_iso = start.replace(tzinfo=None).isoformat(timespec="minutes")
    appt = _find_appt_by_id(appointments, _managing_appt_id_str(conversation))
    appt_type = str(appt.get("appointment_type") or "Consulta") if appt else "Consulta"
    recap = f"Remarcar para:\n{appt_type}\n{start.strftime('%d/%m/%Y às %H:%M')}"
    return FlowRouterResult(
        action="reply",
        bubbles=[
            ButtonBubble(body=recap, confirm_label=LABEL_CONFIRM, cancel_label=LABEL_CANCEL)
        ],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CONFIRM,
        flow_managing_appointment_id=_selected_managing_appointment_id(conversation),
        flow_selected_day=conversation.flow_selected_day,
        flow_selected_slot=slot_iso,
    )


async def _manage_reschedule(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    appointments: list[dict],
    professionals: list | None = None,
) -> FlowRouterResult:
    # PROMPT 4 hook: when the payment lifecycle lands, carry the existing
    # deposit onto the new slot (no refund, no re-charge), subject to
    # reschedule_limit.
    if _norm(body) == _norm(LABEL_CANCEL):
        return FlowRouterResult(
            action="reply",
            bubbles=_menu_bubbles(tenant, professionals),
            flow_state=FlowState.MENU,
        )
    if _norm(body) != _norm(LABEL_CONFIRM):
        return _preserve(conversation, "delegate_llm")
    appt = _find_appt_by_id(appointments, _managing_appt_id_str(conversation))
    if appt is None or calendar is None or not conversation.flow_selected_slot:
        return _preserve(conversation, "delegate_llm")
    event_id = str(appt.get("google_event_id") or "")
    if not event_id:
        return _preserve(conversation, "delegate_llm")

    start = datetime.fromisoformat(conversation.flow_selected_slot)
    if start.tzinfo is None:
        start = start.replace(tzinfo=calendar.tzinfo)
    end = start + timedelta(minutes=_appt_duration_minutes(appt, tenant))

    try:
        await calendar.update_event(event_id, start=start, end=end)
    except CalendarUnavailableError:
        return FlowRouterResult(
            action="calendar_unavailable",
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=STEP_MANAGE_CONFIRM,
            flow_managing_appointment_id=_selected_managing_appointment_id(conversation),
            flow_selected_day=conversation.flow_selected_day,
            flow_selected_slot=conversation.flow_selected_slot,
        )
    return FlowRouterResult(
        action="reply",
        bubbles=[
            TextBubble(
                body=(
                    "Pronto! Sua consulta foi remarcada. ✅\n\n"
                    f"{start.strftime('%d/%m/%Y às %H:%M')}"
                )
            )
        ],
        flow_state=FlowState.IDLE,
        flow_step=None,
        appointment_reschedule={
            "google_event_id": event_id,
            "start_at": start,
            "end_at": end,
        },
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
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
        flow_managing_appointment_id=_selected_managing_appointment_id(conversation),
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
    professionals: list | None = None,
) -> FlowRouterResult:
    """Re-emit the prompt for the conversation's CURRENT flow step.

    Used when a returning patient taps "continue": it re-renders where they left
    off without consuming input, so the saved flow_state/flow_step/flow_selected_*
    stay unchanged. Falls back to the menu for MENU/unknown states, and to
    `delegate_llm` (via `_handle_day` / `_preserve`) when the calendar is needed
    but unavailable — the caller then degrades to the LLM with history intact.

    `professionals`/`calendar` follow the same contract as `route()`: the
    active-professionals snapshot, and a calendar already scoped to the
    selected professional when the conversation has one.
    """
    state = conversation.flow_state
    step = conversation.flow_step

    def _menu_fallback() -> FlowRouterResult:
        return FlowRouterResult(
            action="reply",
            bubbles=_menu_bubbles(tenant, professionals),
            flow_state=FlowState.MENU,
        )

    # MENU, or any state without a catalog step: re-present the menu.
    if state != FlowState.SERVICE_CATALOG or step is None:
        return _menu_fallback()

    # Multi-doctor branch: a stale selection can't be resumed — offer the menu.
    selected_professional = _find_professional_by_id(
        professionals, _selected_professional_id(conversation)
    )
    if _selected_professional_id(conversation) is not None and selected_professional is None:
        return _menu_fallback()
    services = (
        professional_appointment_types(selected_professional, tenant)
        if selected_professional is not None
        else active_appointment_types(tenant)
    )

    if step == STEP_AWAITING_PROFESSIONAL:
        if not _is_multi_professional(professionals):
            return _menu_fallback()
        return _enter_professional_list(tenant, professionals or [])

    if step == STEP_AWAITING_INSURANCE:
        return _enter_insurance(conversation, tenant)

    if step == STEP_AWAITING_SERVICE:
        if not services:
            return _menu_fallback()
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
            conversation, tenant, calendar, conversation.flow_selected_day or "", services
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
    return _menu_fallback()
