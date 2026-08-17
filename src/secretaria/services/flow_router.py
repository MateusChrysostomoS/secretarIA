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

from secretaria.ai.formatter import (
    MAX_LIST_BODY_CHARS,
    MAX_LIST_ROWS,
    Bubble,
    ButtonBubble,
    SlotsBubble,
    TextBubble,
)
from secretaria.ai.scoped_help import run_professional_help, run_service_help
from secretaria.core.logging import get_logger
from secretaria.models import FlowState
from secretaria.services.booking_scope import (
    canonical_service_name,
    resolve_booking_owner_id,
)
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

# Multi-doctor menu (tenants with 2+ active professionals): the effective menu
# becomes exactly these 3 buttons, replacing the configured ones. All <=20
# chars — WhatsApp truncates reply-button titles at 20 and the tap echoes the
# truncated label. The manage flow stays reachable by TYPING the configured
# manage label (matched before the menu mapping, same as today); WhatsApp's
# 3-buttons-per-message cap leaves no visible slot for it here.
#
# The second slot used to be "Procurar médico", which sent one fixed opener and
# then parked the conversation in sticky FlowState.LLM — a button that looked
# deterministic but WAS the LLM door. "Escolher serviço" replaces it with the
# mirror image of "Escolher médico": pick from the clinic's unified service
# catalog first and let the SERVICE narrow the doctors. Zero LLM calls, and it
# answers the question a patient who doesn't know the roster actually has.
BTN_CHOOSE_PROFESSIONAL = "Escolher médico"
BTN_CHOOSE_SERVICE = "Escolher serviço"
LABEL_OTHER = "Outro"

# Convênio step fixed rows (row titles: <=24 chars) + the free-text prompt a
# patient gets after tapping "Outro convênio".
LABEL_INSURANCE_PARTICULAR = "Particular"
LABEL_INSURANCE_OTHER = "Outro convênio"
INSURANCE_PROMPT_OTHER = "Qual é o nome do seu convênio?"
# Structured reasons for `insurance_step_skipped` (see _insurance_step_skip_reason).
# Count-only observability: which CONFIGURATION silenced the step, never which
# plan a patient picked.
INSURANCE_SKIP_DISABLED = "disabled"
INSURANCE_SKIP_EMPTY_CATALOG = "empty_catalog"

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

# The greeting's fixed, product-defined action sets (workers/tasks.py's
# _greeting_buttons_for - NEVER the clinic's own free text; see
# docs/CHECKPOINT_fixed_greeting_buttons.md). A patient with NO upcoming
# appointment gets [LABEL_BOOK, LABEL_OTHER]; one WITH a live appointment gets
# the [LABEL_RESCHEDULE, LABEL_CANCEL_APPT, LABEL_OTHER] trio. Every one of
# these labels is matched by `route()`'s IDLE dispatch below on BOTH single-
# and multi-doctor tenants.
#
# LABEL_MANAGE_APPOINTMENT is no longer OFFERED on the greeting card: for a
# patient with nothing booked it could only ever reach `_enter_manage`'s dead
# end ("Você não tem nenhuma consulta agendada no momento."), and a patient
# who DOES have one is already served by the Remarcar/Cancelar pair. It stays
# a first-class route() entry regardless - the label is still tappable in any
# older WhatsApp thread, and typing it still works.
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
# Service-FIRST branch ("Escolher serviço" on the multi-doctor menu): the
# clinic's unified catalog, then the doctors who offer the picked service.
# Both live in SERVICE_CATALOG like every other booking step.
STEP_AWAITING_CATALOG_SERVICE = "awaiting_catalog_service"
STEP_AWAITING_SERVICE_PROFESSIONAL = "awaiting_service_professional"
# Scoped-help ("Não sei") steps, still within SERVICE_CATALOG. The *_FINAL
# variant marks the last allowed exchange: entered after the node's single
# clarifying question, and a clarify outcome there escalates instead - the
# 1-2-exchange bound is enforced by this step machine, not by the prompt.
STEP_PROFESSIONAL_HELP = "professional_help"
STEP_PROFESSIONAL_HELP_FINAL = "professional_help_final"
STEP_SERVICE_HELP = "service_help"
STEP_SERVICE_HELP_FINAL = "service_help_final"

# Free-text escalation inside a day step. A date the router cannot parse used
# to `delegate_llm` on the spot - a silent, unbounded leak, and (with the
# calendar-is-None leak next to it) very likely the single largest source of
# `llm_activated reason=router_delegated` in production. The three steps are
# the SAME picker rendered three ways: the plain question, the same question
# after one miss ("Não entendi a data"), and, only after TWO misses, the same
# question with an explicit "Outro" escape row. Same bounded-step-machine
# precedent as the scoped-help nodes' *_FINAL variants above: the bound lives
# in this state machine, not in a prompt, and the escape is a visible tap the
# log can count - never a silent fallthrough.
STEP_AWAITING_DAY_RETRY = "awaiting_day_retry"
STEP_AWAITING_DAY_ESCAPE = "awaiting_day_escape"

# flow_step values within MANAGE_BOOKING. The reschedule day/slot/confirm steps
# mirror the booking ones but persist via update_event instead of create_event.
STEP_MANAGE_PICK = "manage_pick"
STEP_MANAGE_ACTION = "manage_action"
STEP_MANAGE_CANCEL_CONFIRM = "manage_cancel_confirm"
STEP_MANAGE_DAY = "manage_day"
STEP_MANAGE_DAY_RETRY = "manage_day_retry"
STEP_MANAGE_DAY_ESCAPE = "manage_day_escape"
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
# Short weekday prefix for a day-picker row title ("Seg, 18/08"). WhatsApp caps
# list-row titles at 24 chars, so the full "Segunda-feira" never fits.
_WEEKDAY_SHORT_PT = ("Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom")

# --------------------------------------------------------------------------
# Reusable day/time branch (shared by first booking, patient reschedule, and
# — once it lands — the rebooking that follows a doctor-side cancellation).
# --------------------------------------------------------------------------

# How far ahead the day scan looks, and how many days one page shows. The
# window is a SEARCH bound; what limits the screen is the page size, because
# WhatsApp allows at most MAX_LIST_ROWS(10) rows per list message. 8 days
# leaves exactly two control slots ("Ver mais dias" + "Voltar"/"Outro"), so a
# page is always <= 10 rows whatever combination of controls applies.
DAY_PICKER_WINDOW_DAYS = 20
DAY_PICKER_PAGE_SIZE = 8
# Slots per day page: same reserve, for "Escolher outro dia" + "Voltar".
SLOT_PICKER_MAX_SLOTS = 8

# List-row id prefixes for the picker. The payload rides back in the body as
# "<title> (<payload>)" — see schemas/webhook.py::_PAYLOAD_ROW_PREFIXES. That
# is what lets the page cursor live in the TAP instead of in a new
# conversations column: nothing about pagination has to be persisted.
ROW_DAY_PREFIX = "day|"
ROW_DAY_MORE_PREFIX = "daymore|"
ROW_DAY_AGAIN_PREFIX = "dayagain|"
ROW_DAY_BACK_PREFIX = "dayback|"

LABEL_MORE_DAYS = "Ver mais dias"
LABEL_ANOTHER_DAY = "Escolher outro dia"

# Where a `Voltar` row goes back to. Passed into `enter_day_picker` by whoever
# opens it and echoed in the row id, so the tap is self-describing:
#   - "service": the service list (the booking flow's own previous step);
#   - "professional": the doctor list (what a rebooking-after-cancellation
#     flow needs, since the patient may want a different doctor entirely).
# None renders no `Voltar` row at all.
BACK_TARGET_SERVICE = "service"
BACK_TARGET_PROFESSIONAL = "professional"

DAY_PICKER_BODY = "Para quando você gostaria? Escolha um dia:"
DAY_PICKER_RESCHEDULE_BODY = "Para quando você gostaria de remarcar? Escolha um dia:"
# Prefix prepended to the picker body after a free-text date we could not read.
DAY_NOT_UNDERSTOOD_PREFIX = "Não entendi a data."
# ...and the extra line shown once the "Outro" escape row is offered.
DAY_ESCAPE_HINT = 'Se preferir falar com a gente, toque em "Outro".'
# The day the patient picked filled up between the two taps (someone else
# booked it). Deterministic answer: the same picker, refreshed.
DAY_NO_LONGER_FREE_PREFIX = "Esse dia não tem mais horário livre."
NO_AVAILABLE_DAYS_MESSAGE = (
    "Não encontrei horários livres nos próximos dias. "
    "Escolha uma opção abaixo e a gente te ajuda."
)


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


@dataclass(frozen=True)
class DayBranch:
    """Which flow the shared day/time branch is currently running inside.

    The branch itself (`enter_day_picker` / `_handle_day_step` /
    `_enter_slot_picker` / `_handle_slot_step`) is identical everywhere: same
    row budget, same pagination, same bounded free-text handling, same
    calendar contract. Only the flow-state coordinates and the copy differ,
    and those are exactly what this carries — so "consolidate, don't copy"
    (the reschedule path used to be a near-duplicate) is enforced by there
    being ONE implementation and two descriptors.
    """

    flow_state: FlowState
    day_step: str
    day_retry_step: str
    day_escape_step: str
    slot_step: str
    day_body: str
    slot_body_prefix: str


BOOKING_DAY_BRANCH = DayBranch(
    flow_state=FlowState.SERVICE_CATALOG,
    day_step=STEP_AWAITING_DAY,
    day_retry_step=STEP_AWAITING_DAY_RETRY,
    day_escape_step=STEP_AWAITING_DAY_ESCAPE,
    slot_step=STEP_AWAITING_SLOT,
    day_body=DAY_PICKER_BODY,
    slot_body_prefix="Horários livres em",
)

MANAGE_DAY_BRANCH = DayBranch(
    flow_state=FlowState.MANAGE_BOOKING,
    day_step=STEP_MANAGE_DAY,
    day_retry_step=STEP_MANAGE_DAY_RETRY,
    day_escape_step=STEP_MANAGE_DAY_ESCAPE,
    slot_step=STEP_MANAGE_SLOT,
    day_body=DAY_PICKER_RESCHEDULE_BODY,
    slot_body_prefix="Novos horários em",
)

# Every step that the day branch owns, per branch — used by `_catalog_step` /
# `_manage_step` to dispatch the retry/escape renders to the same handler.
_BOOKING_DAY_STEPS = (STEP_AWAITING_DAY, STEP_AWAITING_DAY_RETRY, STEP_AWAITING_DAY_ESCAPE)
_MANAGE_DAY_STEPS = (STEP_MANAGE_DAY, STEP_MANAGE_DAY_RETRY, STEP_MANAGE_DAY_ESCAPE)


@dataclass
class _DayPickerState:
    """Conversation-shaped carrier for entries that hold no conversation row.

    `enter_manage_action` is reachable from a reminder button tap and from an
    LLM sentinel hand-back — neither of which loads a Conversation snapshot —
    yet the day picker still has to carry the appointment being rescheduled
    forward. Rather than have the picker special-case its input, those entries
    hand it this: the same attribute names `route()`'s real snapshot exposes,
    and nothing else.
    """

    id: UUID | None = None
    flow_step: str | None = None
    flow_selected_type: str | None = None
    flow_selected_day: str | None = None
    flow_selected_professional_id: UUID | None = None
    flow_selected_insurance: str | None = None
    flow_managing_appointment_id: UUID | None = None


# --------------------------------------------------------------------------
# Public config helpers (shared with the worker greeting path)
# --------------------------------------------------------------------------


def flows_enabled(tenant: Tenant) -> bool:
    """ALWAYS True — the deterministic entry flows ARE the product.

    Used to be an opt-in per-tenant switch (`initial_flows.enabled`), which made
    every clinic start life on the all-LLM path: `Tenant.initial_flows` defaults to
    `{}`, no hub screen ever wrote the key, and nothing in provisioning seeded it.
    The visible symptom was a clinic whose greeting card rendered the fixed
    [Agendar, Gerenciar consulta, Outro] trio (workers/tasks.py::_greeting_buttons
    sends it regardless) but whose Agendar/Gerenciar taps then degraded to the
    "contact us" reply in `_handle_greeting_button_unavailable` — the deterministic
    booking flow looked broken because it had never been switched on.

    The flow is not a per-clinic choice: it is the same flow for everyone, and it
    already adapts itself to the clinic's own DATA (2+ active professionals swap the
    menu for the multi-doctor trio and insert the choose-a-doctor step — see
    `menu_buttons_for` / `_is_multi_professional`; `collect_insurance` inserts the
    convênio step; the services catalog drives the options list). So "which flow"
    is derived, never configured, and the gate had nothing left to gate.

    `initial_flows` is NOT dead: `menu_label`, `buttons` and `reactivation` are still
    read from it (see below). Only the on/off key is gone — including an explicitly
    stored `{"enabled": false}`, which no longer disables anything.

    Kept as a function (rather than deleting the ~8 call sites) so the flows-vs-LLM
    decision keeps a single named home; the LLM stays reachable as the deliberate
    last resort — the "Outro" button, `delegate_llm` fallbacks, and the scoped-help
    "Não sei" rows — never as a silent default for a whole tenant.
    """
    return True


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
        return [BTN_CHOOSE_PROFESSIONAL, BTN_CHOOSE_SERVICE, LABEL_OTHER]
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
    """True when this tenant offers a 'welcome back / continue?' prompt.

    A configured returning greeting is itself an opt-in from the hub UI. The
    lower-level ``initial_flows.reactivation.enabled`` flag remains an explicit
    override so internal provisioning can still disable or enable the behavior.
    """
    config = _reactivation_config(tenant)
    if "enabled" in config:
        return bool(config["enabled"])
    return bool((getattr(tenant, "returning_greeting_message", None) or "").strip())


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
    """Find the active service whose (list-truncated) name matches `body`.

    The name rule itself lives in `services/booking_scope.canonical_service_name`
    (shared with the LLM tools and the Pix price lookup, so a service resolves
    identically on every booking surface); this only maps the canonical name
    back to its catalog entry, which the router needs for duration/price.
    """
    canonical = canonical_service_name(services, body)
    if canonical is None:
        return None
    return next((s for s in services if str(s.get("name", "")) == canonical), None)


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


def _service_list_bubble(
    tenant: Tenant, services: list[dict], header: str | None = None
) -> Bubble:
    """The tappable service list, optionally introduced by `header`.

    `header` folds the selected doctor's presentation INTO this card instead
    of spending a whole separate WhatsApp message on it (see
    `_enter_professional_services`): the patient confirms they are on the
    right doctor and picks the service on one screen. Capped at the list
    body's own limit so a long hub-written `about` can never make the message
    unsendable.
    """
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
    question = "Qual serviço você gostaria de agendar?"
    body = f"{header}\n\n{question}" if header else question
    return SlotsBubble(
        body=body[:MAX_LIST_BODY_CHARS],
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
        return await enter_manage_action(
            "reschedule",
            tenant,
            upcoming_appointments or [],
            professionals,
            calendar=calendar,
        )
    if _label_match(inbound_body, LABEL_CANCEL_APPT):
        return await enter_manage_action(
            "cancel", tenant, upcoming_appointments or [], professionals
        )

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

    "Escolher médico" opens the tappable doctor list; "Escolher serviço" opens
    the clinic's unified service catalog and lets the picked service narrow
    the doctors (`_enter_clinic_service_catalog`) - both fully deterministic;
    "Outro" hands off exactly like today.
    """
    labels = menu_buttons_for(tenant, True)
    if _label_match(body, labels[0]):
        return _enter_professional_list(tenant, professionals)
    if _label_match(body, labels[1]):
        return _enter_clinic_service_catalog(tenant, professionals)
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


# --------------------------------------------------------------------------
# Service-FIRST branch ("Escolher serviço", multi-doctor tenants only)
# --------------------------------------------------------------------------


def _clinic_service_catalog(tenant: Tenant, professionals: list) -> list[dict]:
    """The clinic's unified bookable catalog: every active doctor's services.

    Union over `professional_appointment_types(p, tenant)`, deduplicated by
    canonical name (first occurrence wins, so a service two doctors both offer
    appears once) and sorted the same way every other catalog list is —
    sort_order, then name. Tenant-scoped by construction: `professionals` is
    THIS tenant's active roster and every fallback is to THIS tenant's own
    appointment_types, so no other clinic's services can enter here.
    """
    seen: set[str] = set()
    union: list[dict] = []
    for professional in professionals or []:
        for service in professional_appointment_types(professional, tenant):
            name = str(service.get("name", "")).strip()
            key = _norm(name)
            if not key or key in seen:
                continue
            seen.add(key)
            union.append(service)
    return sorted(union, key=lambda s: (s.get("sort_order", 0), s.get("name", "")))


def _professionals_offering(tenant: Tenant, professionals: list, service_name: str) -> list:
    """The active professionals whose own catalog contains `service_name`."""
    return [
        professional
        for professional in professionals or []
        if canonical_service_name(
            professional_appointment_types(professional, tenant), service_name
        )
        is not None
    ]


def _enter_clinic_service_catalog(tenant: Tenant, professionals: list) -> FlowRouterResult:
    """Render the clinic-wide service list ("Escolher serviço").

    Deliberately WITHOUT the "Não sei" scoped-help row the doctor-first list
    carries: that node's hand-back re-enters at `_enter_service_detail`, which
    assumes the professional is already known — true after "Escolher médico",
    false here. Wiring help into this branch needs its own pair of steps and
    is out of this round's scope; the row is omitted rather than shipped
    pointing at the wrong re-entry.
    """
    services = _clinic_service_catalog(tenant, professionals)
    if not services:
        return FlowRouterResult(
            action="reply",
            bubbles=[
                TextBubble(body="No momento não há serviços disponíveis para agendamento."),
                *_menu_bubbles(tenant, professionals),
            ],
            flow_state=FlowState.MENU,
        )
    if len(services) > MAX_PROFESSIONAL_ROWS:
        logger.warning(
            "flow_clinic_service_list_truncated",
            total=len(services),
            shown=MAX_PROFESSIONAL_ROWS,
        )
    rows = [
        (f"svc|{s.get('name', '')}", str(s.get("name", ""))[:24])
        for s in services[:MAX_PROFESSIONAL_ROWS]
    ]
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body="Qual serviço você gostaria de agendar?",
                rows=rows,
                button_label="Ver serviços",
                section_title="Serviços",
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CATALOG_SERVICE,
    )


def _enter_service_professional_list(
    tenant: Tenant, professionals: list, service_name: str
) -> FlowRouterResult:
    """The doctors who offer `service_name`, with the service already stored.

    The tap lands on STEP_AWAITING_SERVICE_PROFESSIONAL, which re-enters the
    normal flow at that service's own detail card — never back at the service
    list, which the patient has already answered.
    """
    rows = [
        (
            f"prof|{professional.id}",
            str(professional.name)[:24],
            (getattr(professional, "specialty", None) or None),
        )
        for professional in professionals[:MAX_PROFESSIONAL_ROWS]
    ]
    if len(professionals) > MAX_PROFESSIONAL_ROWS:
        logger.warning(
            "flow_service_professional_list_truncated",
            total=len(professionals),
            shown=MAX_PROFESSIONAL_ROWS,
        )
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=f"Quem você prefere para {service_name}?",
                rows=rows,
                button_label="Ver profissionais",
                section_title="Profissionais",
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE_PROFESSIONAL,
        flow_selected_type=service_name,
    )


def _handle_catalog_service(
    conversation: Conversation, tenant: Tenant, body: str, professionals: list
) -> FlowRouterResult:
    """Resolve a clinic-catalog pick, then let the SERVICE narrow the doctors.

    Exactly one professional offers it -> straight to that service's detail
    card with the doctor already selected (the same card the doctor-first
    branch reaches, so both branches converge one step later). Two or more ->
    the filtered doctor list. None -> the catalog changed under us mid-flow;
    a fixed reply plus the menu, never the LLM.
    """
    services = _clinic_service_catalog(tenant, professionals)
    service = _match_service(services, body)
    if service is None:
        return _preserve(conversation, "delegate_llm")
    service_name = str(service.get("name", ""))
    offering = _professionals_offering(tenant, professionals, service_name)
    if len(offering) == 1:
        professional = offering[0]
        result = _enter_service_detail(service, conversation, tenant)
        result.flow_selected_professional_id = professional.id
        return result
    if not offering:
        logger.warning("flow_catalog_service_without_professional")
        return FlowRouterResult(
            action="reply",
            bubbles=[
                TextBubble(
                    body="Esse serviço não está disponível para agendamento no momento."
                ),
                *_menu_bubbles(tenant, professionals),
            ],
            flow_state=FlowState.MENU,
        )
    return _enter_service_professional_list(tenant, offering, service_name)


def _handle_service_professional(
    conversation: Conversation, tenant: Tenant, body: str, professionals: list
) -> FlowRouterResult:
    """Doctor tap inside the service-first branch: straight to the detail card.

    The service was already chosen (`flow_selected_type`), so this must NOT
    re-render that doctor's whole service list the way
    `_enter_professional_services` does — that would ask a question the
    patient has already answered.
    """
    professional = _match_professional(professionals or [], body)
    if professional is None:
        return _preserve(conversation, "delegate_llm")
    service = _match_service(
        professional_appointment_types(professional, tenant), conversation.flow_selected_type
    )
    if service is None:
        # Deactivated between the two taps: fall back to that doctor's own
        # list rather than booking a service they no longer offer.
        return _enter_professional_services(professional, tenant)
    result = _enter_service_detail(service, conversation, tenant)
    result.flow_selected_professional_id = professional.id
    return result


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


def _professional_card_header(professional: Any) -> str:
    """The doctor's presentation, as the service card's own header block.

    Name first (so the card itself confirms WHICH doctor the patient landed
    on), then `_professional_greeting_body`'s specialty/about text. Empty when
    the doctor has neither field AND no name, which cannot happen in practice
    but keeps the caller from prepending a bare blank line.
    """
    parts = [
        str(part).strip()
        for part in (getattr(professional, "name", None), _professional_greeting_body(professional))
        if part and str(part).strip()
    ]
    return "\n\n".join(parts)


def _enter_professional_services(professional: Any, tenant: Tenant) -> FlowRouterResult:
    """The selected doctor's services list, headed by their own presentation.

    ONE message, not two: the presentation (name + specialty + about) used to
    go out as its own TextBubble right before the list, which cost a whole
    WhatsApp message per booking to say something the list card has room for.
    It is now folded into the list body (`_service_list_bubble(header=...)`) —
    same information, same order, one message less.

    Factored out of the tap handler because the LLM hand-back tool
    (`select_professional_and_continue`) re-enters the deterministic flow
    through this exact result — see workers/tasks.py.
    """
    services = professional_appointment_types(professional, tenant)
    if not services:
        bubbles: list = []
        greeting = _professional_greeting_body(professional)
        if greeting:
            bubbles.append(TextBubble(body=greeting))
        bubbles.append(
            TextBubble(body="No momento não há serviços disponíveis para agendamento.")
        )
        return FlowRouterResult(
            action="reply",
            bubbles=bubbles,
            flow_state=FlowState.IDLE,
            flow_selected_professional_id=professional.id,
        )
    return FlowRouterResult(
        action="reply",
        bubbles=[
            _service_list_bubble(tenant, services, header=_professional_card_header(professional))
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE,
        flow_selected_professional_id=professional.id,
    )


# --------------------------------------------------------------------------
# Convênio step (multi-doctor branch; informational only, by design)
# --------------------------------------------------------------------------


def _tenant_insurances(tenant: Tenant) -> list[str]:
    return [str(plan) for plan in (getattr(tenant, "insurances", None) or []) if str(plan).strip()]


def _insurance_step_skip_reason(tenant: Tenant) -> str | None:
    """Why the convênio step must be skipped for this tenant, or None to ask.

    `collect_insurance` and `insurances` are CLINIC-wide settings (they live on
    `tenants`, the hub edits them once for the whole clinic) and the answer is
    informational only — it never filters doctors, services or slots. So the
    step depends on the clinic's configuration and NOTHING else: zero, one and
    many active professionals all get the same question after the service is
    confirmed.

    It used to additionally require `flow_selected_professional_id`, which only
    a multi-doctor tenant ever sets (`_enter_professional_services`). A clinic
    with a single professional — the common case — could switch the toggle on
    in the hub and never be asked, so the saved setting produced no behaviour
    and the booking lost operational information the clinic had asked for.
    """
    if not bool(getattr(tenant, "collect_insurance", False)):
        return INSURANCE_SKIP_DISABLED
    if not _tenant_insurances(tenant):
        return INSURANCE_SKIP_EMPTY_CATALOG
    return None


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
    logger.info(
        "insurance_step_presented",
        conversation_id=str(getattr(conversation, "id", None)),
        # Count only — WHICH plans a clinic offers is fine to size, but never
        # which one this patient is about to choose.
        plan_count=len(plans),
    )
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


async def _handle_insurance(
    conversation: Conversation,
    tenant: Tenant,
    body: str,
    calendar: CalendarService | None = None,
    services: list[dict] | None = None,
    professionals: list | None = None,
) -> FlowRouterResult:
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
    matched = _match_insurance_plan(tenant, body)
    stored = (matched or (body or "").strip())[:120] or None
    logger.info(
        "insurance_selected",
        conversation_id=str(getattr(conversation, "id", None)),
        # NEVER the plan itself (it identifies a patient's coverage): only
        # whether it came from the clinic's own catalog or was typed free.
        from_catalog=matched is not None,
        stored=stored is not None,
    )
    result = await _ask_day(conversation, tenant, calendar, services, professionals)
    if result.flow_state is FlowState.SERVICE_CATALOG:
        # Only the picker itself carries the booking fields; the no-availability
        # fallback resets to MENU on purpose and must not resurrect them.
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
    # Scoped help IS an LLM call (a single grounded decision, not a free chat).
    # Counted here so "how often do we fall back to the model?" has one honest
    # answer across every entry point - see workers/tasks.py::_llm_activation_reason.
    logger.info(
        "llm_activated",
        reason="scoped_help_professional",
        conversation_id=str(getattr(conversation, "id", None)),
        final_round=final_round,
    )
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
    # Same accounting as _handle_professional_help above.
    logger.info(
        "llm_activated",
        reason="scoped_help_service",
        conversation_id=str(getattr(conversation, "id", None)),
        final_round=final_round,
    )
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

    if step == STEP_AWAITING_CATALOG_SERVICE:
        return _handle_catalog_service(conversation, tenant, body, professionals or [])

    if step == STEP_AWAITING_SERVICE_PROFESSIONAL:
        return _handle_service_professional(conversation, tenant, body, professionals or [])

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
            skip_reason = _insurance_step_skip_reason(tenant)
            if skip_reason is None:
                return _enter_insurance(conversation, tenant)
            logger.info(
                "insurance_step_skipped",
                reason=skip_reason,
                conversation_id=str(getattr(conversation, "id", None)),
                professional_count=len(professionals or []),
            )
            return await _ask_day(conversation, tenant, calendar, services, professionals)
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
        return await _handle_insurance(
            conversation, tenant, body, calendar, services, professionals
        )

    if step in _BOOKING_DAY_STEPS:
        return await _handle_day_step(
            conversation,
            tenant,
            calendar,
            body,
            duration_minutes=_booking_duration(conversation, tenant, services),
            branch=BOOKING_DAY_BRANCH,
            services=services,
            back_target=BACK_TARGET_SERVICE,
            professionals=professionals,
        )

    if step == STEP_AWAITING_SLOT:
        control = await _handle_slot_controls(
            conversation,
            tenant,
            calendar,
            body,
            duration_minutes=_booking_duration(conversation, tenant, services),
            branch=BOOKING_DAY_BRANCH,
            services=services,
            back_target=BACK_TARGET_SERVICE,
            professionals=professionals,
        )
        return control if control is not None else _handle_slot(conversation, body)

    if step == STEP_AWAITING_CONFIRMATION:
        return await _handle_confirmation(
            conversation, tenant, calendar, body, patient_name, services, professionals
        )

    if step == STEP_AWAITING_RETRY:
        if _norm(body) == _norm(LABEL_RETRY_MENU):
            return FlowRouterResult(
                action="reply",
                bubbles=_menu_bubbles(tenant, professionals),
                flow_state=FlowState.MENU,
            )
        if _norm(body) == _norm(LABEL_RETRY_YES):
            return await _relist_stored_day(
                conversation, tenant, calendar, services, professionals
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


# --------------------------------------------------------------------------
# The reusable day/time branch
# --------------------------------------------------------------------------
#
# ONE implementation, used by every flow that needs "which day, then which
# time": the first booking, the patient's own reschedule, and (when it lands)
# the rebooking that follows a doctor-side cancellation. What used to be here
# was a free-text question — "Para quando você gostaria? (ex: amanhã, sexta,
# 12/06)" — whose parser fell through to `delegate_llm` on anything it could
# not read, and a second `delegate_llm` when the calendar was missing. Both
# are gone: the patient taps a day that is provably free, then a time, and a
# date we cannot read re-asks with the same tappable list instead of quietly
# spending an agent call. Typed dates still work — they are a shortcut now,
# not the only way in.


def _day_row_label(day: datetime) -> str:
    """A day-picker row title: "Seg, 18/08" (WhatsApp caps titles at 24)."""
    return f"{_WEEKDAY_SHORT_PT[day.weekday()]}, {day.strftime('%d/%m')}"


_ROW_PAYLOAD_RE = re.compile(r"\s*\(([^)]*)\)\s*$")


def _row_payload(body: str | None) -> str | None:
    """The "(payload)" a data-carrying row tap appends to its title, or None.

    Mirrors `_slot_iso_from_body`/`_professional_id_from_body`, but returns
    None (rather than the whole body) when there are no trailing parentheses,
    so a TYPED "Voltar" is distinguishable from a tapped one.
    """
    if not body:
        return None
    match = _ROW_PAYLOAD_RE.search(body)
    return match.group(1).strip() if match else None


def _control_match(body: str | None, label: str) -> bool:
    """`_label_match`, but for the picker's payload-carrying control rows.

    "Voltar" arrives as "Voltar (service)" and "Ver mais dias" as "Ver mais
    dias (2)" — the cursor/destination rides in the row id precisely so the
    picker needs no stored state. Matching therefore has to look past that
    suffix; a typed "Voltar" (no suffix) still matches the plain form. Scoped
    to these fixed labels, never used for free text.
    """
    return _label_match(body, label) or _label_match(_ROW_PAYLOAD_RE.sub("", body or ""), label)


def _day_from_body(body: str | None) -> tuple[datetime | None, int]:
    """Parse a day-row tap into (day, page-it-was-listed-on).

    The row id is "day|<iso date>|<page>", so the body arrives as
    "Seg, 18/08 (2026-08-18|2)". The page rides along so "Escolher outro dia"
    on the next screen can return to the very page the patient was reading —
    no extra conversation column, no server-side cursor.
    """
    payload = _row_payload(body)
    if payload is None:
        return None, 0
    parts = payload.split("|")
    try:
        day = datetime.fromisoformat(parts[0]).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
    except ValueError:
        return None, 0
    return day, _as_page(parts[1] if len(parts) > 1 else None)


def _as_page(raw: str | None) -> int:
    try:
        return max(int(str(raw)), 0)
    except (TypeError, ValueError):
        return 0


def _day_branch_fields(conversation: Conversation, branch: DayBranch) -> dict:
    """The flow-state fields THIS branch must carry through the day/slot steps.

    Every FlowRouterResult field is written unconditionally by the caller, so
    a result that does not name a field clears it. The booking branch must
    keep the service/doctor/convênio it has already collected; the manage
    branch must keep the appointment it is rescheduling.
    """
    if branch.flow_state is FlowState.MANAGE_BOOKING:
        return {
            "flow_managing_appointment_id": _selected_managing_appointment_id(conversation)
        }
    return {
        "flow_selected_type": conversation.flow_selected_type,
        "flow_selected_professional_id": _selected_professional_id(conversation),
        "flow_selected_insurance": _selected_insurance(conversation),
    }


def _calendar_unavailable(
    conversation: Conversation, branch: DayBranch, step: str
) -> FlowRouterResult:
    """No calendar, or the calendar refused: hand to a human, never to the LLM.

    A missing/unreachable agenda is not a comprehension gap the model can fill
    — anything it said about availability would be invented. `route()`'s caller
    turns this into the unavailability message plus a handover.
    """
    return FlowRouterResult(
        action="calendar_unavailable",
        flow_state=branch.flow_state,
        flow_step=step,
        **_day_branch_fields(conversation, branch),
    )


async def enter_day_picker(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    *,
    duration_minutes: int,
    branch: DayBranch = BOOKING_DAY_BRANCH,
    page: int = 0,
    back_target: str | None = None,
    prefix: str | None = None,
    step: str | None = None,
    professionals: list | None = None,
) -> FlowRouterResult:
    """Render the tappable day list: only days that actually have a free slot.

    Costs ONE calendar call for the whole window
    (`CalendarService.list_available_days`), never one per day. Rows:

        <= DAY_PICKER_PAGE_SIZE(8) day rows
        + "Ver mais dias"          when the window holds more
        + "Voltar"                 when `back_target` is set,
          replaced by "Outro"      once the free-text escape is being offered

    which is at most MAX_LIST_ROWS(10) in every combination — the API's hard
    cap, and the reason the original "17 dias úteis + Outro" idea could not be
    built as one screen. `step` overrides the step written (the retry/escape
    renders reuse this function verbatim), `prefix` prepends one short line.
    """
    step = step or branch.day_step
    if calendar is None:
        return _calendar_unavailable(conversation, branch, step)
    try:
        days = await calendar.list_available_days(
            start_day=datetime.now(calendar.tzinfo),
            days=DAY_PICKER_WINDOW_DAYS,
            slot_minutes=duration_minutes,
        )
    except CalendarUnavailableError:
        return _calendar_unavailable(conversation, branch, step)

    if not days:
        # Genuinely nothing bookable in the window — not a failure, and not
        # something to keep the patient staring at an empty list for.
        return FlowRouterResult(
            action="reply",
            bubbles=[
                TextBubble(body=NO_AVAILABLE_DAYS_MESSAGE),
                *_menu_bubbles(tenant, professionals),
            ],
            flow_state=FlowState.MENU,
        )

    page = max(page, 0)
    offset = page * DAY_PICKER_PAGE_SIZE
    if offset >= len(days):  # stale cursor (availability shrank): restart.
        page, offset = 0, 0
    shown = days[offset : offset + DAY_PICKER_PAGE_SIZE]
    rows: list[tuple[str, str]] = [
        (f"{ROW_DAY_PREFIX}{day.date().isoformat()}|{page}", _day_row_label(day))
        for day in shown
    ]
    if len(days) > offset + DAY_PICKER_PAGE_SIZE:
        rows.append((f"{ROW_DAY_MORE_PREFIX}{page + 1}", LABEL_MORE_DAYS))
    escape = step == branch.day_escape_step
    if escape:
        # The escape row takes the last control slot, displacing "Voltar":
        # once we have failed to read the patient twice, getting them to a
        # human matters more than one step back. Its id deliberately carries
        # NO payload prefix so the tap arrives as the plain "Outro" label.
        rows.append(("dayescape|0", LABEL_OTHER))
    elif back_target:
        rows.append((f"{ROW_DAY_BACK_PREFIX}{back_target}", LABEL_BACK))

    body = f"{prefix} {branch.day_body}" if prefix else branch.day_body
    if escape:
        body = f"{body}\n\n{DAY_ESCAPE_HINT}"
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=body[:MAX_LIST_BODY_CHARS],
                rows=rows[:MAX_LIST_ROWS],
                button_label="Ver dias",
                section_title="Dias disponíveis",
            )
        ],
        flow_state=branch.flow_state,
        flow_step=step,
        **_day_branch_fields(conversation, branch),
    )


async def _enter_slot_picker(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    target: datetime,
    *,
    duration_minutes: int,
    branch: DayBranch = BOOKING_DAY_BRANCH,
    page: int = 0,
    back_target: str | None = None,
    professionals: list | None = None,
) -> FlowRouterResult:
    """Step 2 of the branch: the free times on `target`, plus its two exits.

    A day that comes back empty was free when the picker was built and filled
    in between — so the deterministic answer is the picker again, refreshed
    and prefixed, not a text message asking the patient to try another day.
    """
    if calendar is None:
        return _calendar_unavailable(conversation, branch, branch.day_step)
    try:
        slots = await calendar.list_free_slots(
            day=target, slot_minutes=duration_minutes, max_slots=SLOT_PICKER_MAX_SLOTS
        )
    except CalendarUnavailableError:
        return _calendar_unavailable(conversation, branch, branch.day_step)

    if not slots:
        return await enter_day_picker(
            conversation,
            tenant,
            calendar,
            duration_minutes=duration_minutes,
            branch=branch,
            page=page,
            back_target=back_target,
            prefix=DAY_NO_LONGER_FREE_PREFIX,
            professionals=professionals,
        )

    rows: list[tuple[str, str]] = [(f"slot|{s['start']}", s["label"]) for s in slots]
    rows.append((f"{ROW_DAY_AGAIN_PREFIX}{page}", LABEL_ANOTHER_DAY))
    if back_target:
        rows.append((f"{ROW_DAY_BACK_PREFIX}{back_target}", LABEL_BACK))
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=f"{branch.slot_body_prefix} {target.strftime('%d/%m')}:",
                rows=rows[:MAX_LIST_ROWS],
                button_label="Ver horários",
                section_title="Horários livres",
            )
        ],
        flow_state=branch.flow_state,
        flow_step=branch.slot_step,
        flow_selected_day=target.date().isoformat(),
        **_day_branch_fields(conversation, branch),
    )


def _handle_day_back(
    conversation: Conversation,
    tenant: Tenant,
    services: list[dict],
    professionals: list | None,
    target: str | None,
) -> FlowRouterResult:
    """The `Voltar` row: one step back, with every earlier answer preserved.

    "service" re-renders the service list, "professional" the doctor list;
    anything else (or an unusable target) falls back to the menu. Whatever the
    destination, the service / doctor / convênio already chosen ride along —
    going back must never silently erase them.
    """
    if target == BACK_TARGET_PROFESSIONAL and _is_multi_professional(professionals):
        result = _enter_professional_list(tenant, professionals or [])
        result.flow_selected_type = conversation.flow_selected_type
        result.flow_selected_insurance = _selected_insurance(conversation)
        return result
    if target == BACK_TARGET_SERVICE and services:
        return FlowRouterResult(
            action="reply",
            bubbles=[_service_list_bubble(tenant, services)],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_AWAITING_SERVICE,
            flow_selected_type=conversation.flow_selected_type,
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=_selected_insurance(conversation),
        )
    return FlowRouterResult(
        action="reply",
        bubbles=_menu_bubbles(tenant, professionals),
        flow_state=FlowState.MENU,
    )


async def _handle_day_step(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    *,
    duration_minutes: int,
    branch: DayBranch,
    services: list[dict],
    back_target: str | None,
    professionals: list | None,
) -> FlowRouterResult:
    """One inbound turn on a day step (plain, retry, or escape render).

    Never returns `delegate_llm` on its own: a date it cannot read re-asks
    with the picker and escalates the step, and only the escape row — visible
    to the patient, and logged under its own `flow_step` — reaches the model.
    """
    step = conversation.flow_step or branch.day_step
    if _control_match(body, LABEL_BACK):
        return _handle_day_back(
            conversation, tenant, services, professionals, _row_payload(body) or back_target
        )
    if step == branch.day_escape_step and _control_match(body, LABEL_OTHER):
        return FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM)
    if _control_match(body, LABEL_MORE_DAYS):
        return await enter_day_picker(
            conversation,
            tenant,
            calendar,
            duration_minutes=duration_minutes,
            branch=branch,
            page=_as_page(_row_payload(body)),
            back_target=back_target,
            step=step,
            professionals=professionals,
        )

    if calendar is None:
        return _calendar_unavailable(conversation, branch, step)
    target, page = _day_from_body(body)
    if target is None:
        # Free text is still honoured as a shortcut ("amanhã", "12/06").
        target = _parse_day(body, datetime.now(calendar.tzinfo))
    if target is not None:
        return await _enter_slot_picker(
            conversation,
            tenant,
            calendar,
            target,
            duration_minutes=duration_minutes,
            branch=branch,
            page=page,
            back_target=back_target,
            professionals=professionals,
        )

    # Unreadable. Re-ask with the same tappable list, one step further along
    # the bounded escalation - never a silent hand-off to the model.
    next_step = (
        branch.day_retry_step if step == branch.day_step else branch.day_escape_step
    )
    logger.info(
        "flow_day_not_understood",
        conversation_id=str(getattr(conversation, "id", None)),
        step=step,
        next_step=next_step,
    )
    return await enter_day_picker(
        conversation,
        tenant,
        calendar,
        duration_minutes=duration_minutes,
        branch=branch,
        back_target=back_target,
        prefix=DAY_NOT_UNDERSTOOD_PREFIX,
        step=next_step,
        professionals=professionals,
    )


async def _handle_slot_controls(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    *,
    duration_minutes: int,
    branch: DayBranch,
    services: list[dict],
    back_target: str | None,
    professionals: list | None,
) -> FlowRouterResult | None:
    """The slot list's two non-slot rows, or None when this isn't one of them."""
    if _control_match(body, LABEL_BACK):
        return _handle_day_back(
            conversation, tenant, services, professionals, _row_payload(body) or back_target
        )
    if _control_match(body, LABEL_ANOTHER_DAY):
        return await enter_day_picker(
            conversation,
            tenant,
            calendar,
            duration_minutes=duration_minutes,
            branch=branch,
            page=_as_page(_row_payload(body)),
            back_target=back_target,
            professionals=professionals,
        )
    return None


def _booking_duration(
    conversation: Conversation, tenant: Tenant, services: list[dict] | None
) -> int:
    """Minutes the booking branch should slot on: the chosen service's own."""
    service = _match_service(
        services if services is not None else active_appointment_types(tenant),
        conversation.flow_selected_type,
    )
    if service is None:
        return tenant.appointment_duration_min or 30
    return _service_duration(service, tenant)


async def _ask_day(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    services: list[dict] | None = None,
    professionals: list | None = None,
) -> FlowRouterResult:
    """Open the booking branch's day picker (the step after service/convênio)."""
    return await enter_day_picker(
        conversation,
        tenant,
        calendar,
        duration_minutes=_booking_duration(conversation, tenant, services),
        branch=BOOKING_DAY_BRANCH,
        back_target=BACK_TARGET_SERVICE,
        professionals=professionals,
    )


async def _relist_stored_day(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    services: list[dict] | None = None,
    professionals: list | None = None,
) -> FlowRouterResult:
    """Re-list fresh times for the day already stored on the conversation.

    Serves the "Sim, outro horário" retry and the returning-patient resume of
    STEP_AWAITING_SLOT. `flow_selected_day` is an ISO date, so it round-trips
    through `_parse_day`'s ISO fast path rather than being re-read as DMY free
    text; with nothing stored (or the day since gone) it falls back to the
    picker instead of guessing.
    """
    duration = _booking_duration(conversation, tenant, services)
    if calendar is None:
        return _calendar_unavailable(conversation, BOOKING_DAY_BRANCH, STEP_AWAITING_DAY)
    target = _parse_day(conversation.flow_selected_day, datetime.now(calendar.tzinfo))
    if target is None:
        return await _ask_day(conversation, tenant, calendar, services, professionals)
    return await _enter_slot_picker(
        conversation,
        tenant,
        calendar,
        target,
        duration_minutes=duration,
        branch=BOOKING_DAY_BRANCH,
        back_target=BACK_TARGET_SERVICE,
        professionals=professionals,
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
    professionals: list | None = None,
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

    # The catalog's own spelling, never whatever `flow_selected_type` drifted
    # into (a service renamed/deactivated mid-flow). None keeps the stored
    # label so the patient's confirmation still names what they picked, and
    # the Pix hook then skips with an honest "no such service" reason instead
    # of pricing the wrong entry - see services/booking_scope.py.
    canonical_type = canonical_service_name(services, service_type)
    appointment = {
        "google_event_id": event.get("id") or "",
        "google_event_link": event.get("htmlLink"),
        "appointment_type": (canonical_type or service_type)[:120],
        "start_at": start,
        "end_at": end,
    }
    # WHO this booking belongs to: the multi-doctor selection when there is
    # one, else the tenant's single active professional — whose catalog,
    # hours and calendar this very booking already used. The key stays
    # OMITTED (not None) when no owner can be proven, so a tenant with zero
    # or several professionals and no valid pick keeps today's plain dict.
    professional_id = resolve_booking_owner_id(
        professionals, _selected_professional_id(conversation)
    )
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


async def enter_manage_action(
    intent: Literal["reschedule", "cancel"],
    tenant: Tenant,
    appointments: list[dict],
    professionals: list | None = None,
    preselected_id: UUID | None = None,
    calendar: CalendarService | None = None,
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

    `calendar` is the agenda that OWNS the appointment being rescheduled — the
    reschedule branches open the day picker straight away, and a picker built
    on the wrong (or a missing) calendar would offer days that agenda never
    had. None makes those branches answer `calendar_unavailable`, never a
    guessed availability; the cancel branches never touch it.
    """
    if preselected_id is not None:
        preselected = _find_appt_by_id(appointments, str(preselected_id))
        if preselected is not None:
            managing_id = _appt_uuid(preselected)
            if intent == "reschedule":
                return await _begin_reschedule(
                    managing_id, preselected, tenant, calendar, professionals
                )
            return _begin_cancel(managing_id, preselected)
    if not appointments:
        return _enter_manage(tenant, appointments, professionals)
    if len(appointments) == 1:
        appt = appointments[0]
        managing_id = _appt_uuid(appt)
        if intent == "reschedule":
            return await _begin_reschedule(managing_id, appt, tenant, calendar, professionals)
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


async def _begin_reschedule(
    managing_id: UUID | None,
    appt: dict | None,
    tenant: Tenant,
    calendar: CalendarService | None,
    professionals: list | None = None,
) -> FlowRouterResult:
    """Open the day picker for the new date, targeting `managing_id`.

    Shared by STEP_MANAGE_ACTION's "Remarcar" branch and
    `enter_manage_action`'s single-appointment / preselected shortcuts. Uses
    the SAME branch the first booking does (`MANAGE_DAY_BRANCH`), so the
    reschedule day/time experience is the booking one by construction rather
    than by a second copy kept in sync by hand. `appt` supplies the original
    appointment's own length; None falls back to the tenant default.

    No `back_target`: this step's predecessor is the Remarcar/Cancelar action
    card, which is one tap away from the menu already.
    """
    # PROMPT 4 hook: when the payment lifecycle lands, carry the existing
    # deposit onto the new slot (no refund, no re-charge), subject to
    # reschedule_limit.
    return await enter_day_picker(
        _DayPickerState(flow_managing_appointment_id=managing_id),
        tenant,
        calendar,
        duration_minutes=(
            _appt_duration_minutes(appt, tenant)
            if appt is not None
            else (tenant.appointment_duration_min or 30)
        ),
        branch=MANAGE_DAY_BRANCH,
        professionals=professionals,
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
        return await _begin_reschedule(
            _appt_uuid(appt), appt, tenant, calendar, professionals
        )

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
        appt = _find_appt_by_id(appointments, _managing_appt_id_str(conversation))
        if _norm(body) == _norm(LABEL_CANCEL_APPT):
            return _begin_cancel(managing_id, appt)
        if _norm(body) == _norm(LABEL_RESCHEDULE):
            return await _begin_reschedule(managing_id, appt, tenant, calendar, professionals)
        return _preserve(conversation, "delegate_llm")

    if step == STEP_MANAGE_CANCEL_CONFIRM:
        return await _manage_cancel(
            conversation, tenant, calendar, body, appointments, professionals
        )

    if step in _MANAGE_DAY_STEPS:
        return await _handle_day_step(
            conversation,
            tenant,
            calendar,
            body,
            duration_minutes=_manage_duration(conversation, tenant, appointments),
            branch=MANAGE_DAY_BRANCH,
            services=[],
            back_target=None,
            professionals=professionals,
        )

    if step == STEP_MANAGE_SLOT:
        control = await _handle_slot_controls(
            conversation,
            tenant,
            calendar,
            body,
            duration_minutes=_manage_duration(conversation, tenant, appointments),
            branch=MANAGE_DAY_BRANCH,
            services=[],
            back_target=None,
            professionals=professionals,
        )
        if control is not None:
            return control
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


def _manage_duration(
    conversation: Conversation, tenant: Tenant, appointments: list[dict]
) -> int:
    """Minutes the reschedule branch should slot on: the appointment's own.

    Falls back to the tenant default when the target can no longer be resolved
    in this turn's snapshot — the shared branch stays usable rather than
    dead-ending, and `_manage_reschedule` re-resolves the row before it writes
    anything anyway.
    """
    appt = _find_appt_by_id(appointments, _managing_appt_id_str(conversation))
    if appt is None:
        return tenant.appointment_duration_min or 30
    return _appt_duration_minutes(appt, tenant)


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
    `calendar_unavailable` (via the day branch) when the calendar is needed but
    missing — the caller then hands the conversation to a human rather than
    letting the model improvise availability.

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

    if step == STEP_AWAITING_CATALOG_SERVICE:
        return _enter_clinic_service_catalog(tenant, professionals or [])

    if step == STEP_AWAITING_SERVICE_PROFESSIONAL:
        offering = _professionals_offering(
            tenant, professionals or [], conversation.flow_selected_type or ""
        )
        if not offering:
            return _menu_fallback()
        return _enter_service_professional_list(
            tenant, offering, conversation.flow_selected_type or ""
        )

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

    if step in _BOOKING_DAY_STEPS:
        # Re-render the picker from scratch: availability moves, and a resumed
        # conversation is exactly when it has had time to.
        return await _ask_day(conversation, tenant, calendar, services, professionals)

    if step == STEP_AWAITING_SLOT:
        # Re-list fresh slots for the saved day (availability may have changed).
        return await _relist_stored_day(conversation, tenant, calendar, services, professionals)

    if step == STEP_AWAITING_CONFIRMATION:
        recap = _confirmation_recap(conversation)
        if recap is None:
            # Lost the slot somehow: re-ask the day.
            return await _ask_day(conversation, tenant, calendar, services, professionals)
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
