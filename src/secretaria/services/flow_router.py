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
  "<label> (<iso>)" (see schemas.webhook.inbound_routing_text). The matching
  here relies on that contract. That string is the ROUTING text, recomposed in
  memory from a tap's title and its row id - the stored `Message.body` is the
  label alone, because it is what the staff console and the patient portal
  show.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from secretaria.ai.formatter import (
    MAX_LIST_BODY_CHARS,
    MAX_LIST_ROWS,
    Bubble,
    ButtonBubble,
    OptionSource,
    SlotsBubble,
    TextBubble,
)
from secretaria.ai.scoped_help import run_professional_help, run_service_help
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import (
    EMOJI_AFFIRMATIVE,
    EMOJI_BACK,
    EMOJI_DOCTOR,
    EMOJI_NEGATIVE,
    EMOJI_SCHEDULE,
    EMOJI_SERVICE,
    MAX_INTERACTIVE_BODY_CHARS,
    MAX_LIST_ROW_DESCRIPTION_CHARS,
    MAX_LIST_ROW_TITLE_CHARS,
    MAX_TEXT_MESSAGE_CHARS,
    decorate,
    decorate_and_truncate,
    decorate_if_fits,
    strip_decoration,
    truncate_button_label,
    truncate_list_row_title,
    truncate_plain,
)
from secretaria.models import FlowState
from secretaria.services import flow_units
from secretaria.services.appointment_edit import EditContext
from secretaria.services.attendee import (
    ATTENDEE_NAME_INVALID,
    ATTENDEE_NAME_REQUEST,
    ATTENDEE_QUESTION_BODY,
    ATTENDEE_SELF,
    LABEL_ATTENDEE_AUTH_BACK,
    LABEL_ATTENDEE_AUTH_CONFIRM,
    LABEL_ATTENDEE_OTHER,
    LABEL_ATTENDEE_SELF,
    authorization_body,
    parse_attendee_name,
    real_attendee_name,
)
from secretaria.services.availability import (
    at_or_after,
    available_day_starts,
    booking_buffer_minutes,
    booking_notice_floor,
    booking_notice_hours,
    buffer_taken_at_confirm,
    held_slots,
    offer_consultation_windows,
    padded,
    slot_scan_limit,
    slot_taken_at_confirm,
    without_holds,
)
from secretaria.services.booking_dates import WEEKDAY_SHORT, short_day_label, short_day_phrase
from secretaria.services.booking_details import service_price_text
from secretaria.services.booking_hold import BookingGate
from secretaria.services.booking_scope import (
    canonical_service_name,
    resolve_booking_owner_id,
)
from secretaria.services.calendar import (
    CalendarService,
    CalendarSlotTakenError,
    CalendarUnavailableError,
    build_event_description,
    build_patient_calendar_link,
    build_patient_link_details,
)
from secretaria.services.insurance_acceptance import (
    has_exceptions,
    is_accepted,
    log_refusal,
    refuses_all_services,
    refuses_everywhere,
    snapshot_exceptions,
)
from secretaria.services.insurance_catalog import match_plan
from secretaria.services.local_calendar import create_event_exclusive, update_event_exclusive
from secretaria.services.patient_context import as_utc
from secretaria.services.payments import payer_data
from secretaria.services.payments.deposit_lifecycle import BEFORE_BLOCKED_TEXT
from secretaria.services.payments.failure_reason import coerce as coerce_failure_reason
from secretaria.services.payments.money import format_brl, parse_brl_to_cents
from secretaria.services.pending_identity import BOOKING_SLOT_TAKEN_MESSAGE
from secretaria.services.sensitive_claim_guard import unbacked_claim
from secretaria.services.service_catalog import (
    entry_service_id,
    normalize,
    professionals_offering,
)
from secretaria.services.service_terms import resolve_terms
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
    professional_business_hours,
)
from secretaria.services.unit_booking import (
    blocked_on,
    confirmation_unit_lines,
    event_unit_fields,
    service_day_refusal,
    unit_fields,
    unit_place_label,
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
# The per-row mark on the doctor list once the patient named one of the
# clinic's catalog plans (owner, 2026-09-23: show EVERY doctor, mark the ones
# who take the plan, never hide the others). It rides in the list-row
# description, where the specialty already goes, so WhatsApp and the Portal
# render it with no new UI.
INSURANCE_ACCEPTED_MARK = f"{EMOJI_AFFIRMATIVE} Aceita seu convênio"
# TASK-086 (SPEC E6, inventory A2/A4): the convênio matrix refuses the patient's plan - for
# every service of a doctor (doctor row) or for one service (service row). Marked, never
# hidden (owner, 2026-09-23: "mostrar todos").
INSURANCE_REFUSED_MARK = f"{EMOJI_NEGATIVE} Não atende este convênio"
# The "não atende este convênio" step (TASK-086 A5, inventory R4): the matrix leaves the
# service with no place for the patient's plan. Never the clinic's "0 unidades" alert -
# nothing is misconfigured. Three ways on: another doctor who accepts, another service,
# another convênio (each button only when it leads somewhere).
STEP_AWAITING_PLAN_REFUSED = "awaiting_plan_refused"
LABEL_PLAN_OTHER_DOCTOR = "Outro médico"
LABEL_PLAN_OTHER_SERVICE = "Outro serviço"
LABEL_PLAN_CHANGE_INSURANCE = "Trocar convênio"
PLAN_REFUSED_HOW_TO_GO_ON = "Como você prefere seguir?"
PLAN_REFUSED_BODY = (
    "{quem} não atende o convênio {convenio} em {servico}. " + PLAN_REFUSED_HOW_TO_GO_ON
)
# The final guard (TASK-086 A7): the clinic changed the matrix while the patient was booking.
# With the convênio step off (T9 review minor 6) the lead goes on the "não atende" menu instead.
PLAN_REFUSED_AT_CONFIRM_LEAD = (
    "Desculpe, o convênio {convenio} não é mais aceito para {servico}{quem}{onde}. "
    "Nada foi agendado."
)
PLAN_REFUSED_AT_CONFIRM = PLAN_REFUSED_AT_CONFIRM_LEAD + " Escolha outro convênio ou Particular:"
# TASK-088 (owner, 2026-11-20): the convênio is asked LAST - after service, doctor and unit -
# and its list holds only the plans that combination accepts. Nothing accepted: no list, the
# booking goes on as Particular with ONE short line before the days. The "unit" variants are for
# a clinic whose doctor attends at units; without any, there is no "nessa unidade" to say.
INSURANCE_NONE_ACCEPTED_UNIT = "Para esse serviço nessa unidade, o atendimento é particular."
INSURANCE_NONE_ACCEPTED = "Para esse serviço, o atendimento é particular."
# A convênio that arrived BEFORE the step (AI draft, a parked chat) and the combination refuses
# it: never swapped for Particular in silence - the filtered list with this line on top, or,
# with nothing else accepted, Particular saying so.
INSURANCE_REFUSED_LIST_UNIT = (
    "Nessa unidade o convênio que você informou não é aceito para esse serviço. "
    "Escolha uma opção:"
)
INSURANCE_REFUSED_LIST = (
    "O convênio que você informou não é aceito para esse serviço. Escolha uma opção:"
)
INSURANCE_REFUSED_PARTICULAR_UNIT = (
    "Nessa unidade esse convênio não é aceito para o serviço; seguimos como Particular."
)
INSURANCE_REFUSED_PARTICULAR = (
    "Esse convênio não é aceito para o serviço; seguimos como Particular."
)
INSURANCE_QUESTION = "Você vai usar convênio? Escolha uma opção:"
# Structured reasons for `insurance_step_skipped` (see _insurance_step_skip_reason).
# Count-only observability: which CONFIGURATION silenced the step, never which
# plan a patient picked.
INSURANCE_SKIP_DISABLED = "disabled"
INSURANCE_SKIP_EMPTY_CATALOG = "empty_catalog"
# TASK-008: the clinic has not chosen one of the three acceptance modes yet
# (services/tenant_config.py::insurance_mode_configured). Same effective
# behaviour as an empty catalog - the owner's explicit decision is that this
# case gets NO default, ever (SPEC §2).
INSURANCE_SKIP_NO_MODE = "no_mode"

# WhatsApp caps interactive lists at 10 rows. Insurance plans keep 2 slots for the
# fixed Particular/Outro rows. The professional and service lists both reserve their
# LAST row for the fixed "Não sei" scoped-help entry, so a list of up to 9 fits one
# page there - without the reserve, send_list's silent [:10] would drop the help row
# exactly when the catalog is fullest. Longer lists PAGE (TASK-082 S4, `catalog_page`);
# nothing is dropped any more.
MAX_PROFESSIONAL_ROWS = 10
MAX_CATALOG_OPTION_ROWS = MAX_PROFESSIONAL_ROWS - 1
# TASK-082 S4: services and doctors page on WhatsApp the way convênios do. A list that
# fits keeps its single page; a longer one shows CATALOG_PAGE_SIZE items + "Ver mais ..."
# + the list's fixed rows ("Não sei"), never more than MAX_LIST_ROWS. The page rides in
# the tap - `svcmore|<page>` / `profmore|<page>`, registered in
# schemas/webhook.py::_PAYLOAD_ROW_PREFIXES - so paging needs no conversation column.
# WhatsApp only: a Portal card carries the whole list behind its `source` pointer and
# pages it itself (S1). Typed text at a list is NOT searched (TASK-082 F1, CONTRACT.md
# D8): an exact name is recognised as before, anything else goes to the model; a typed
# "ver mais" on WhatsApp becomes the tap before routing (workers/whatsapp/typed_more.py).
CATALOG_PAGE_SIZE = 8
ROW_SERVICE_MORE_PREFIX = "svcmore|"
ROW_PROFESSIONAL_MORE_PREFIX = "profmore|"
LABEL_MORE_SERVICES = "Ver mais serviços"
LABEL_MORE_PROFESSIONALS = "Ver mais profissionais"
MAX_INSURANCE_PLAN_ROWS = 8
# A clinic may accept any number of convênios: past MAX_INSURANCE_PLAN_ROWS the list
# pages, INSURANCE_PAGE_SIZE plans + "Ver mais convênios" + Particular + Outro = 10 rows.
# The page cursor rides in the tap (`insmore|<page>`, schemas/webhook.py), exactly like
# the day picker's "Ver mais dias": no conversation column. A clinic with up to
# MAX_INSURANCE_PLAN_ROWS plans keeps its single, unpaged list.
INSURANCE_PAGE_SIZE = MAX_INSURANCE_PLAN_ROWS - 1
ROW_INSURANCE_MORE_PREFIX = "insmore|"
LABEL_MORE_INSURANCES = "Ver mais convênios"
# Same hard WhatsApp limit for the manage flow's "pick an appointment" list
# (_manage_pick_list_bubble). A patient with more upcoming appointments than
# this is not expected in practice; extras are dropped (with a warning).
MAX_MANAGE_APPOINTMENT_ROWS = 10

# Reactivation ("welcome back" / resume) defaults. Overridable per tenant under
# initial_flows.reactivation.
DEFAULT_REACTIVATION_GAP_MINUTES = 360  # 6h of silence => treat as returning.
DEFAULT_CONTINUE_PROMPT = "Você quer continuar com a nossa última conversa?"
DEFAULT_REACTIVATION_BUTTONS = [
    decorate(EMOJI_AFFIRMATIVE, "Sim"),
    decorate(EMOJI_NEGATIVE, "Não"),
]

# Button labels used inside the catalog flow (also the text taps come back as).
#
# The ✅/❌ pairing is SEMANTIC, not literal: "Confirmar"/"Cancelar" and
# "Sim"/"Outro serviço" are the same affirm/decline choice as "Sim"/"Não" wearing
# different words, and PreCheck - the conductor this look is borrowed from - marks
# them the same way ("✅ Concordo", "✅ Terminei!"). Marking only the two literal
# Sim/Não pairs would have left a patient reading a checked "Sim" on one screen
# and a bare "Confirmar" on the next for no reason they could see.
#
# Every label here is matched through `_norm`, which strips the prefix, so a
# patient who TYPES "sim" or "cancelar" - or taps a card rendered before this
# shipped - still resolves. See core/whatsapp_limits.py's decoration section.
LABEL_BOOK_SERVICE = decorate(EMOJI_AFFIRMATIVE, "Sim")
LABEL_OTHER_SERVICE = decorate(EMOJI_NEGATIVE, "Outro serviço")
LABEL_CONFIRM = decorate(EMOJI_AFFIRMATIVE, "Confirmar")
LABEL_CANCEL = decorate(EMOJI_NEGATIVE, "Cancelar")
# The retry card these two belong to is being replaced wholesale by FEAT 45
# (`_handle_confirmation` / STEP_AWAITING_RETRY), which decides its own
# formatting - decorating them here would be work thrown away twice.
LABEL_RETRY_YES = "Sim"
LABEL_RETRY_MENU = "Menu principal"

# Button labels used inside the manage (cancel/reschedule) flow.
# "Remarcar" and "Voltar" stay bare: neither is an affirm/decline, and the
# request scoped the arrow to the picker's back ROWS, not to buttons.
LABEL_RESCHEDULE = "Remarcar"
LABEL_CANCEL_APPT = decorate(EMOJI_NEGATIVE, "Cancelar")
LABEL_BACK = "Voltar"
LABEL_YES = decorate(EMOJI_AFFIRMATIVE, "Sim")
LABEL_NO = decorate(EMOJI_NEGATIVE, "Não")

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
# older WhatsApp thread, and typing it still works. It IS offered on the main
# menu, but only to a patient with a live future appointment
# (`main_menu_buttons(has_upcoming=True)`), where it cannot dead-end.
LABEL_BOOK = "Agendar"
LABEL_MANAGE_APPOINTMENT = "Gerenciar consulta"

# Fixed last row appended to BOTH catalog lists (professional and service):
# tapping it opens the matching scoped-help LLM node (STEP_PROFESSIONAL_HELP /
# STEP_SERVICE_HELP below) instead of requiring the patient to pick blind.
LABEL_DONT_KNOW = "Não sei"

# Fixed, scope-specific openers each "Não sei" tap replies with (the LLM only
# enters on the patient's ANSWER, one turn later). Deliberately two distinct
# nodes about the configured professionals or services and booking steps.
# Neither asks the patient to describe symptoms (TASK-022: "o que você está
# sentindo?" drew the model into clinical questions), and neither is the
# open-ended "Outro" hand-off (ai/scoped_help.py's module docstring). Worded
# like a person, with administrative examples (owner, 2026-10-07, TASK-038).
PROFESSIONAL_HELP_OPENER = (
    "Sem problema, eu te ajudo a escolher! É uma primeira consulta, um retorno ou "
    "um atendimento específico? Me conta o que você procura que eu te indico o "
    "profissional certo."
)
SERVICE_HELP_OPENER = (
    "Sem problema, eu te ajudo a escolher! É uma primeira consulta, um retorno ou "
    "um exame? Me conta que eu te indico o serviço certo."
)
# The fixed question an "Outro" tap on the menu replies with (owner, 2026-10-06). The tap
# never reaches the model: left to it, the opener drifted (sometimes "Essa consulta é pra
# você?", a booking question the flow asks itself later). The LLM runs one turn later, on
# the patient's ANSWER, with this question already in the history as its context.
OTHER_OPENER = "O que te traz à clínica?"
# TASK-038 (owner, 2026-10-07): the fixed question the agent's offer_human_handoff
# hand-back renders, with LABEL_YES / LABEL_NO. Asked, never assumed: "✅ Sim" hands
# the conversation to the team (the same path as a scoped-help escalation, staff alert
# included); "❌ Não" keeps the free conversation going; anything else is the patient
# carrying on, so
# the model answers it and the offer is dropped. The wait is FlowState.LLM +
# STEP_HUMAN_OFFER, so the LLM-state expiry bounds it like any free conversation.
STEP_HUMAN_OFFER = "awaiting_human_offer"
HUMAN_OFFER_BODY = (
    "Não sou capaz de atender essa sua necessidade por aqui. Quer que eu chame nosso "
    "atendente humano? (Pode demorar alguns minutos)"
)
# The same card, asked when a deposit-flow booking could not be made (TASK-071 Plan 3 follow-up,
# owner 2026-10-29): the time was NOT booked, so the generic "Não sou capaz..." opener does not
# fit. Only the body differs - same step, labels and `_handle_human_offer` (recognised by
# STEP_HUMAN_OFFER, never by this text).
HUMAN_OFFER_BOOKING_BODY = (
    "Quer que eu chame nosso atendente humano para te ajudar a marcar? "
    "(Pode demorar alguns minutos)"
)
# "❌ Não" keeps the conversation with the AI (owner, 2026-10-07): no menu, so a
# patient who was mid-booking does not lose what they had chosen.
HUMAN_OFFER_DECLINED_BODY = (
    "Tudo bem! Pode continuar me contando o que você precisa que eu sigo te ajudando por aqui."
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
STEP_AWAITING_NO_PLAN_AT_UNIT = "awaiting_no_plan_at_unit"
STEP_AWAITING_DAY = "awaiting_day"
STEP_AWAITING_SLOT = "awaiting_slot"
STEP_AWAITING_CONFIRMATION = "awaiting_confirmation"
STEP_AWAITING_RETRY = "awaiting_retry_choice"
# Service-FIRST branch ("Escolher serviço" on the multi-doctor menu): the
# clinic's unified catalog, then the doctors who offer the picked service.
# Both live in SERVICE_CATALOG like every other booking step.
STEP_AWAITING_CATALOG_SERVICE = "awaiting_catalog_service"
# "Essa consulta é pra você?" (services/attendee.py) - the FIRST booking
# question, ahead of everything above: pra-quem -> [name -> authorization].
# While on these steps `flow_selected_type` holds WHICH booking entry the
# patient came from (ATTENDEE_NEXT_*), so the answer continues into the exact
# list the entry would have opened; it is otherwise unused this early.
STEP_AWAITING_ATTENDEE_CHOICE = "awaiting_attendee_choice"
STEP_AWAITING_ATTENDEE_NAME = "awaiting_attendee_name"
STEP_AWAITING_ATTENDEE_AUTH = "awaiting_attendee_auth"
ATTENDEE_STEPS = (
    STEP_AWAITING_ATTENDEE_CHOICE,
    STEP_AWAITING_ATTENDEE_NAME,
    STEP_AWAITING_ATTENDEE_AUTH,
)
ATTENDEE_NEXT_BOOK = "__attendee_next_book__"
ATTENDEE_NEXT_CATALOG = "__attendee_next_catalog__"

STEP_AWAITING_SERVICE_PROFESSIONAL = "awaiting_service_professional"
# The steps an AI booking draft may wait on (`Conversation.flow_draft`, TASK-030): the
# pra-quem steps (P2) and, by the owner's 2026-10-03 ruling, the convênio, doctor and
# service questions (P3). The patient's answer to any of them resumes the draft
# (`_attendee_step`, `_resume_parked_draft`); every other step drops it (`_carry_draft`).
# TASK-088 (booking order service -> doctor -> unit -> convênio -> day): the service-first
# lists (the clinic's catalog, the doctors of a service) and the unit question are questions
# the resolver asks now, so a draft holding a day or a time waits on them too.
DRAFT_WAIT_STEPS = (
    *ATTENDEE_STEPS,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_NO_PLAN_AT_UNIT,
    STEP_AWAITING_PROFESSIONAL,
    STEP_AWAITING_SERVICE,
    STEP_AWAITING_CATALOG_SERVICE,
    STEP_AWAITING_SERVICE_PROFESSIONAL,
    flow_units.STEP_AWAITING_UNIT,
    flow_units.STEP_AWAITING_UNIT_CONFIRM,
)
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

# TASK-032 R6 ("Alterar Dados"): the steps of FlowState.EDIT_BOOKING
# (services/appointment_edit_flow.py). The day/slot steps belong to EDIT_DAY_BRANCH.
STEP_EDIT_MENU = "edit_menu"
STEP_EDIT_MORE = "edit_more"
STEP_EDIT_DAY = "edit_day"
STEP_EDIT_DAY_RETRY = "edit_day_retry"
STEP_EDIT_DAY_ESCAPE = "edit_day_escape"
STEP_EDIT_SLOT = "edit_slot"
STEP_EDIT_TIME_TOO = "edit_time_too"
STEP_EDIT_SERVICE = "edit_service"
STEP_EDIT_DOCTOR = "edit_doctor"
# Follow-up E1: the unit list of "Alterar Dados" (a new service or a new doctor needs it).
STEP_EDIT_UNIT = "edit_unit"
STEP_EDIT_INSURANCE = "edit_insurance"
STEP_EDIT_INSURANCE_OTHER = "edit_insurance_other"
STEP_EDIT_ATT_CHOICE = "edit_att_choice"
STEP_EDIT_ATT_NAME = "edit_att_name"
STEP_EDIT_ATT_AUTH = "edit_att_auth"
STEP_EDIT_CONFIRM = "edit_confirm"

MANAGE_APPOINTMENT_PAYLOAD_PREFIX = "appointment:"

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
_WEEKDAY_SHORT_PT = WEEKDAY_SHORT

# --------------------------------------------------------------------------
# Reusable day/time branch (shared by first booking, patient reschedule, and
# — once it lands — the rebooking that follows a doctor-side cancellation).
# --------------------------------------------------------------------------

# How far ahead the day scan looks, and how many days one page shows. The
# window is a SEARCH bound; what limits the screen is the page size, because
# WhatsApp allows at most MAX_LIST_ROWS(10) rows per list message. 8 days
# leaves exactly two control slots ("Outra data" or "Ver mais dias" + "Escolher
# outro Serviço"/"Outro"), so a page is always <= 10 rows whatever combination
# of controls applies.
DAY_PICKER_WINDOW_DAYS = 20
DAY_PICKER_PAGE_SIZE = 8
# "Outra data" (owner, 2026-11-04: a patient may want a date far ahead): how far its scan
# looks. Still one calendar read for the whole window, made only when the patient asks.
DAY_CALENDAR_WINDOW_DAYS = 180
# Slots per day page: same reserve, for "Escolher outro dia" + "Escolher
# outro Serviço".
SLOT_PICKER_MAX_SLOTS = 8

# List-row id prefixes for the picker. The payload rides back in the ROUTING
# text as "<title> (<payload>)" — see schemas/webhook.py::_PAYLOAD_ROW_PREFIXES
# and inbound_routing_text (the stored message body is the title alone). That
# is what lets the page cursor live in the TAP instead of in a new
# conversations column: nothing about pagination has to be persisted.
ROW_DAY_PREFIX = "day|"
ROW_DAY_MORE_PREFIX = "daymore|"
ROW_DAY_CALENDAR_PREFIX = "daycal|"
ROW_DAY_AGAIN_PREFIX = "dayagain|"
ROW_DAY_BACK_PREFIX = "dayback|"
# Second field of a "Ver mais dias" row inside the long scan: the next page stays in it.
DAY_CURSOR_CALENDAR = "cal"


def calendar_day_row_id(day: str) -> str:
    """The row id a day tapped on a card's calendar comes back as (page 0: the short list)."""
    return f"{ROW_DAY_PREFIX}{day}|0"


# "Ver mais dias" stays bare on purpose: it moves FORWARD through the same list,
# so wearing the back arrow would point the wrong way, and it is not a day.
LABEL_MORE_DAYS = "Ver mais dias"
LABEL_OTHER_DATE = decorate(EMOJI_SCHEDULE, "Outra data")
# Shortened from "Escolher outro dia" / "Escolher outro Serviço" to make room for
# the arrow. This is not cosmetic: "⬅️ " costs THREE characters (the arrow carries
# a U+FE0F variation selector), and "⬅️ Escolher outro Serviço" is 25 - one over
# MAX_LIST_ROW_TITLE_CHARS. send_list would have cut it to "⬅️ Escolher outro
# Servi…", which `_control_match` compares against the full constant and would no
# longer recognise: the back row would render fine and then do nothing when
# tapped. Both labels are kept parallel and short so neither can drift back over.
LABEL_ANOTHER_DAY = decorate(EMOJI_BACK, "Outro dia")
LABEL_ANOTHER_SERVICE = decorate(EMOJI_BACK, "Outro serviço")
# TASK-088: the day picker's way back to the convênio list when it was asked in this booking.
# NOT "Outro convênio": the router compares labels without their emoji, and that is the list's
# own "Outro convênio" row ("ins|outro"). 18 characters with the arrow, well under 24.
# It overlaps the OTHER way too: the "not accepted" card's own button is "Trocar convênio"
# (`LABEL_PLAN_CHANGE_INSURANCE`) - the same words, without the arrow. The two never share a
# screen (they answer different steps) and both mean "ask the convênio again".
LABEL_ANOTHER_INSURANCE = decorate(EMOJI_BACK, "Trocar convênio")

# Where the picker's back-control row goes back to. Passed into
# `enter_day_picker` by whoever opens it and echoed in the row id, so the tap
# is self-describing:
#   - "service": the service list (the booking flow's own previous step);
#   - "professional": the doctor list (what a rebooking-after-cancellation
#     flow needs, since the patient may want a different doctor entirely).
# None renders no back-control row at all.
BACK_TARGET_SERVICE = "service"
BACK_TARGET_PROFESSIONAL = "professional"
# TASK-083: the unit list ("⬅️ Outra unidade"), for a professional with units to choose from.
BACK_TARGET_UNIT = flow_units.BACK_TARGET_UNIT
# TASK-088: the convênio list, one step back from the days when the patient was asked it.
BACK_TARGET_INSURANCE = "insurance"
# TASK-088 (fix round 1): "Outro serviço" of the REBOOKING after a doctor's cancellation - the
# candidate doctor's own service list, as it always was. The booking's "service" back row is the
# clinic catalog on a multi-doctor clinic (the service is the first thing it chose).
BACK_TARGET_DOCTOR_SERVICES = "doctorservice"


def _day_back_label(back_target: str | None) -> str:
    """The back-control row's title for a given `back_target`.

    "⬅️ Outro serviço" for the only destination anything currently
    wires up (`BACK_TARGET_SERVICE`); the historical "Voltar" otherwise, so
    the not-yet-triggered `BACK_TARGET_PROFESSIONAL` destination (a rebooking
    row that would actually return to the DOCTOR list) never gets mislabeled
    as a service choice.
    """
    if back_target == BACK_TARGET_UNIT:
        return flow_units.LABEL_ANOTHER_UNIT
    if back_target == BACK_TARGET_INSURANCE:
        return LABEL_ANOTHER_INSURANCE
    if back_target in (BACK_TARGET_SERVICE, BACK_TARGET_DOCTOR_SERVICES):
        return LABEL_ANOTHER_SERVICE
    return LABEL_BACK


def _is_day_back(body: str | None, back_target: str | None) -> bool:
    """The picker's back row. On the booking branch either booking back row counts: a picker
    drawn with "Outro serviço" (a re-offer, an AI landing) stays tappable on a turn whose own
    target is the unit list, and the row's payload says where it goes (TASK-083)."""
    if _control_match(body, _day_back_label(back_target)):
        return True
    booking_targets = (
        BACK_TARGET_SERVICE,
        BACK_TARGET_UNIT,
        BACK_TARGET_INSURANCE,
        BACK_TARGET_DOCTOR_SERVICES,
    )
    return back_target in booking_targets and any(
        _control_match(body, _day_back_label(target)) for target in booking_targets
    )


def _back_target_of(body: str | None) -> str:
    """Where a back row's tap goes: its payload (`dayback|unit` -> "unit"), else the destination
    its label names, else the service (the oldest and safest destination)."""
    payload = _row_payload(body)
    targets = (
        BACK_TARGET_UNIT,
        BACK_TARGET_INSURANCE,
        BACK_TARGET_SERVICE,
        BACK_TARGET_DOCTOR_SERVICES,
    )
    if payload in targets:
        return str(payload)
    return next(
        (target for target in targets if _control_match(body, _day_back_label(target))),
        BACK_TARGET_SERVICE,
    )


DAY_PICKER_BODY = "Para quando você gostaria? Escolha um dia:"
DAY_PICKER_RESCHEDULE_BODY = "Para quando você gostaria de remarcar? Escolha um dia:"
DAY_CALENDAR_BODY = "Escolha a data que preferir:"
# Prefix prepended to the picker body after a free-text date we could not read.
DAY_NOT_UNDERSTOOD_PREFIX = "Não entendi a data."
# ...and the extra line shown once the "Outro" escape row is offered.
DAY_ESCAPE_HINT = 'Se preferir falar com a gente, toque em "Outro".'
# The day the patient picked filled up between the two taps (someone else
# booked it). Deterministic answer: the same picker, refreshed.
DAY_NO_LONGER_FREE_PREFIX = "Esse dia não tem mais horário livre."
# TASK-052 (owner decision 2026-10-11): a day whose free times are held by other patients'
# unfinished bookings (an unpaid Pix, or an e-mail code still pending) is offered, and picking it
# says which times are held - never by whom - and that they may free up.
HELD_SLOTS_NOTICE = (
    "Os horários {times} deste dia estão reservados para outro paciente, aguardando a "
    "confirmação ou o pagamento do sinal. Se o prazo terminar sem pagamento, eles voltam a "
    "ficar livres."
)
ALL_HELD_NOTICE = (
    "Todos os horários livres deste dia ({times}) estão reservados para outro paciente, "
    "aguardando a confirmação ou o pagamento do sinal. Se o prazo terminar sem pagamento, eles "
    "voltam a ficar livres: tente de novo mais tarde ou escolha outro dia."
)
HELD_TIMES_SHOWN = 8


def held_times_text(labels: list[str]) -> str:
    """"08:00", "08:00 e 08:30", "08:00, 08:30 e 09:00", "... e mais 3" - times only."""
    shown = labels[:HELD_TIMES_SHOWN]
    rest = len(labels) - len(shown)
    if rest > 0:
        return f"{', '.join(shown)} e mais {rest}"
    if len(shown) <= 1:
        return "".join(shown)
    return f"{', '.join(shown[:-1])} e {shown[-1]}"


# The exact time the patient CONFIRMED was filled on the agenda between the list
# and the tap - the clinic, another channel or a person in Google Calendar got
# there first (`availability.slot_taken_at_confirm`). One short line; on the booking
# branch it prefixes the day picker, on reschedule the same day's times follow it
# (`_reoffer_taken_slot`).
SLOT_TAKEN_AT_CONFIRM_MESSAGE = "Esse horário acabou de ser ocupado 😕"


def booking_notice_message(hours: int) -> str:
    """The line a patient reads when a time is inside the doctor's minimum notice."""
    unit = "hora" if hours == 1 else "horas"
    return f"Os agendamentos precisam de pelo menos {hours} {unit} de antecedência."
NO_AVAILABLE_DAYS_MESSAGE = (
    "Não encontrei horários livres nos próximos dias. "
    "Escolha uma opção abaixo e a gente te ajuda."
)

# --------------------------------------------------------------------------
# "This professional cannot be booked AT ALL" (FEAT 41)
# --------------------------------------------------------------------------
#
# Two situations that look identical to a patient and are nothing alike to the
# clinic:
#
#   * the agenda is genuinely full in the window we look at -> normal, fixes
#     itself, NO_AVAILABLE_DAYS_MESSAGE above, nobody is alerted;
#   * the professional has no availability window (or no service) configured at
#     all -> nothing will ever come free, and only a human can fix it.
#
# Before this, both fell into the first branch: the patient got a polite "não
# encontrei horários" and the clinic learned nothing. So the second case now
# gets its own detection (STATIC config, read before any calendar call), its
# own copy, and its own `action` so the worker can alert whoever can fix it.
#
# Gap codes carried by `FlowRouterResult.professional_config_gap`.
PROFESSIONAL_GAP_SERVICES = "services"
PROFESSIONAL_GAP_HOURS = "hours"

# Patient-facing copy for the HOURS gap. Deliberately NOT
# NO_AVAILABLE_DAYS_MESSAGE — keeping that string reserved for a genuinely full
# agenda is what stops the two cases collapsing back into one.
PROFESSIONAL_NO_HOURS_MESSAGE = (
    "No momento não é possível agendar com {name}. "
    "Nossa equipe já foi avisada e vai regularizar em breve."
)
# Same message for a professional with no display name worth showing (`name` is
# NOT NULL in practice; this is the belt-and-braces branch).
PROFESSIONAL_NO_HOURS_FALLBACK = (
    "No momento não é possível agendar. Nossa equipe já foi avisada e vai regularizar em breve."
)
# The SERVICES gap keeps the wording it already had — only the signalling is new.
PROFESSIONAL_NO_SERVICES_MESSAGE = "No momento não há serviços disponíveis para agendamento."


# How a booked window is spelled to the patient. One constant because the
# confirmation bubble and the gate notice must name the same instant in the
# same words - they are two halves of one booking now.
WHEN_FORMAT = "%d/%m/%Y às %H:%M"

# The gate for the turn currently being routed, or None.
#
# A ContextVar rather than a parameter on the eight functions between
# `route()` and the slot picker. Two reasons, in order of weight:
#
#   * EVERY listing path has to see it, not just the booking one. A slot a
#     Portal visitor is holding must also disappear from the reschedule
#     picker, and those two paths do not share a call chain — threading an
#     argument would have meant remembering each of them, which is precisely
#     the kind of "remember to pass it" rule that silently stops holding.
#   * `contextvars` is per-task under asyncio, so two conversations routed
#     concurrently in the same worker never see each other's gate. A module
#     global would have been the bug this avoids.
#
# Set and reset by `route()` alone, always in a try/finally, so nothing can
# leak the previous turn's gate into the next one.
_ACTIVE_GATE: ContextVar[BookingGate | None] = ContextVar("flow_router_gate", default=None)


async def _hold_windows(professional_id: UUID | None) -> list[tuple[datetime, datetime]]:
    """Windows other conversations are holding on this agenda, or []."""
    gate = _ACTIVE_GATE.get()
    if gate is None:
        return []
    return await gate.busy_windows(professional_id)


def _hold_owner(conversation: Conversation, professionals: list | None) -> UUID | None:
    """WHOSE holds a slot list must hide: the agenda a booking would be placed on.

    The doctor the conversation selected; with none selected, the clinic's sole active
    professional - because that is the `professional_id` a hold is PLACED with
    (`_handle_confirmation` -> `resolve_booking_owner_id`). Looking holds up under None on
    a single-professional clinic missed every one of them (TASK-030 P2).
    """
    selected = _selected_professional_id(conversation)
    if selected is not None:
        return selected
    return resolve_booking_owner_id(professionals, None)


@contextmanager
def booking_gate_scope(gate: BookingGate | None) -> Iterator[None]:
    """Publish `gate` on `_ACTIVE_GATE` for the block, and ALWAYS take it down again.

    `route()` runs inside one; so does the AI draft resolver (services/booking_draft.py),
    which runs OUTSIDE `route()` and would otherwise see a slot another conversation is
    holding as free (TASK-030 P2, spec §7).
    """
    token = _ACTIVE_GATE.set(gate)
    try:
        yield
    finally:
        _ACTIVE_GATE.reset(token)


# --------------------------------------------------------------------------
# TASK-088 section B (Task 5): the doctor's buffer between consultations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BufferRule:
    """The buffer the day and slot lists apply, and the booking they must not pad.

    `minutes` is `booking_buffer_minutes` of the professional who OWNS the agenda being read;
    None = derive it from the conversation's booking owner (`_booking_professional`).
    `moved_event_id` is the `google_event_id` of the booking being moved (reschedule, "Alterar
    dados"): its own old window never pads its new time. A new booking ignores nothing.
    """

    minutes: int | None = None
    moved_event_id: str | None = None


# The booking being moved, for the reschedule / edit pickers (the same reason as `_ACTIVE_GATE`:
# the pickers sit under a dozen entry points that do not all hold the appointment). Set and
# reset by the reschedule and "Alterar dados" entries, never by the first-booking branch.
_MOVING: ContextVar[BufferRule | None] = ContextVar("flow_router_moving", default=None)


@contextmanager
def moving_booking_scope(rule: BufferRule | None) -> Iterator[None]:
    """Publish `rule` (the booking being moved) for the pickers of the block; ALWAYS reset."""
    token = _MOVING.set(rule)
    try:
        yield
    finally:
        _MOVING.reset(token)


def moving_event_scope(event_id: str | None):
    """`moving_booking_scope` for "Alterar dados": only the moved event is known up front (the
    doctor - hence the buffer - follows the draft and is read off the carrier)."""
    return moving_booking_scope(BufferRule(moved_event_id=event_id or None))


def _booking_buffer(professional: Any | None) -> BufferRule:
    """The rule of a NEW booking with `professional`: nothing moves."""
    return BufferRule(minutes=booking_buffer_minutes(professional))


def _appointment_owner(appt: dict | None, professionals: list | None) -> Any | None:
    """The doctor (snapshot row) who owns `appt`, or None when it cannot be read."""
    parsed = _appointment_owner_and_unit_id(appt, professionals) if appt else None
    return parsed[0] if parsed is not None else None


def _appointment_buffer(appt: dict | None, professionals: list | None) -> BufferRule:
    """The rule of rescheduling `appt`: the buffer of the doctor who owns it, and its own event
    never pads itself (R31, R32). No readable appointment: no rule from here (the conversation's
    booking owner decides)."""
    if not appt:
        return BufferRule()
    owner = _appointment_owner(appt, professionals)
    return BufferRule(
        minutes=booking_buffer_minutes(owner),
        moved_event_id=str(appt.get("google_event_id") or "") or None,
    )


def _picker_buffer(
    conversation: Any, professionals: list | None, branch: DayBranch, rule: BufferRule | None
) -> tuple[int, str | None]:
    """(buffer minutes, moved event id) a day / slot list of `branch` applies.

    An explicit `rule` wins; the reschedule and edit branches read the booking being moved off
    the scope their entry opened (`moving_booking_scope`); a rule without minutes - and the
    first-booking branch - takes the buffer of `_booking_professional` (the doctor whose agenda
    the picker reads). The clinic's own agenda (hub) never gets here.
    """
    if rule is None and branch is not BOOKING_DAY_BRANCH:
        rule = _MOVING.get()
    if rule is None:
        rule = BufferRule()
    minutes = rule.minutes
    if minutes is None:
        minutes = booking_buffer_minutes(_booking_professional(conversation, professionals))
    return max(minutes, 0), rule.moved_event_id


async def _inside_buffer(
    calendar: CalendarService,
    start: datetime,
    end: datetime,
    professional: Any | None,
    *,
    source: str,
    conversation_id: object = None,
    professional_id: UUID | None = None,
    ignore_event_id: str | None = None,
) -> bool:
    """True when the chosen window is inside the buffer of `professional` (refused like a slot
    taken between the list and the tap, R35). Asked right after `slot_taken_at_confirm`, on the
    SAME agenda; `ignore_event_id` is the booking being moved; the holds passed on are OTHER
    conversations' only (R33). No buffer: False, with no read at all."""
    minutes = booking_buffer_minutes(professional)
    if minutes <= 0:
        return False
    try:
        holds = await _hold_windows(professional_id)
    except Exception as exc:  # noqa: BLE001 - fail-open, like every confirm recheck
        logger.warning("booking_buffer_holds_failed", error_type=type(exc).__name__)
        holds = []
    return await buffer_taken_at_confirm(
        calendar,
        start,
        end,
        buffer_minutes=minutes,
        source=source,
        ignore_event_id=ignore_event_id,
        holds=holds,
        conversation_id=conversation_id,
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
        "professional_config_incomplete"
                              - the professional the patient reached cannot be
                                booked at all: their STATIC config is missing
                                hours or services (never "the agenda is full",
                                which stays a plain "reply"). The caller sends
                                `bubbles` and alerts the clinic AND that doctor
                                by email, debounced. No handover: nothing is
                                broken for the OTHER doctors, and a human
                                secretary cannot conjure a schedule either -
                                what is needed is a config change, which is
                                exactly what the email asks for.
    """

    action: Literal[
        "reply",
        "delegate_llm",
        "calendar_unavailable",
        "handover",
        "professional_config_incomplete",
    ]
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
    flow_insurance_auto: bool | None = None
    # TASK-083 spec §8: the unit the booking happens at (`flow_units`). Carried by
    # `_carry_unit` only on the steps after it was chosen (day, slot, confirmation), so it is
    # dropped exactly when the patient goes back to the service/doctor lists or the booking ends.
    flow_selected_unit_id: UUID | None = None
    # Who the booking in progress is FOR when it is not the patient themself
    # (services/attendee.py). Unlike the fields above it does NOT have to be
    # carried by hand: `_carry_attendee` (applied once, in `route()` and
    # `resume_bubbles()`) copies it forward on every booking/LLM result, so
    # only the attendee steps and `_handle_confirmation` ever name it.
    flow_attendee_name: str | None = None
    # True on exactly the result of the "Confirmar" tap under the
    # authorization sentence; the caller writes one
    # ConsentEvent(kind="third_party_booking_authorized") for it.
    attendee_authorized: bool = False
    # The AI's booking draft parked while the patient answers pra-quem (TASK-030 P2,
    # services/booking_draft.py record shape). Written unconditionally by the caller like
    # every field above; `_carry_booking` keeps it ONLY on the attendee steps, so it is
    # cleared the moment the conversation leaves them.
    flow_draft: dict | None = None
    # True on the result of the tap that answers pra-quem while a draft is parked: the
    # worker (workers/shared/draft_resolution.py::_resume_booking_draft) re-runs the
    # resolver over the draft instead of showing this result's list. Never persisted.
    resume_draft: bool = False
    # TASK-088 R41: the professional whose AGENDA the booking day picker was just drawn on (the
    # state's explicit selection; None on a clinic where none is stored, whose calendar is the
    # tenant's). Set by `_ask_day` on every result that reached `enter_day_picker` (the days, the
    # calendar view, the "no days" menu, "calendar unavailable"), so a result built on the wrong
    # agenda is recognisable whatever it turned out to be (`calendar_owner_changed`). A temporary
    # signal for the worker, like `resume_draft`: never persisted (`_apply_flow_result` copies
    # named fields only).
    agenda_owner_id: UUID | None = None
    # True when this result DELIBERATELY leaves `flow_selected_insurance` empty (TASK-088: the
    # automatic Particular dropped on "back"), so `_carry_insurance` does not copy the old answer
    # back onto it. Never persisted.
    insurance_dropped: bool = False
    # The previously selected unit no longer applies. Do not copy it back into this result.
    unit_dropped: bool = False
    # On an action="calendar_unavailable" result of a booking whose deposit was DROPPED
    # (TASK-071 Plan 3 review fix, 2026-10-29): the worker keeps the clinic's calendar alert but
    # offers a person (Sim / Não) instead of handing over. Never persisted.
    deposit_waived_reason: str | None = None
    # WHICH static config the professional is missing, on an
    # action="professional_config_incomplete" result and nowhere else. WHO it
    # is missing rides on `flow_selected_professional_id` above rather than in
    # a parallel field: that column already means "the professional this turn
    # is about", and the empty-services branch has always written it.
    professional_config_gap: Literal["services", "hours"] | None = None
    # The appointment being cancelled/rescheduled inside MANAGE_BOOKING
    # (replaces the old flow_selected_type overload). Written unconditionally
    # by the caller like every field above, so any manage-flow result that
    # should keep it must carry it explicitly; non-manage results leave it
    # None, and the completed cancel/reschedule results clear it on purpose.
    flow_managing_appointment_id: UUID | None = None
    # When set, an event was created and the caller should persist a row.
    appointment: dict | None = None
    # When set, the patient stated WHY they will not rebook after their doctor
    # cancelled; the caller writes one `RebookingDecline` row. Business data,
    # not a log line — see models/rebooking_decline.py.
    # {"appointment_id": UUID, "reason_code": str, "reason_text": str | None}
    decline_reason: dict | None = None
    # When set, the matching appointment row should be flipped to CANCELLED.
    appointment_cancel_id: str | None = None
    # When set, the patient CONFIRMED a slot but no appointment was created:
    # the slot is reserved (`services/booking_hold.py`) and a 6-digit code is
    # in their inbox. The caller sends the gate notice as the code card and
    # parks the conversation in AWAITING_EMAIL_CODE; the appointment is
    # created later, from `workers/tasks.py::_promote_booking_hold`, and only
    # if the code is verified before the reservation expires. Mutually
    # exclusive with `appointment` by construction — the whole point is that
    # exactly one of the two exists for any confirmed slot.
    # {"hold_id": UUID, "email_masked": str | None, "when": str, "minutes": int}
    booking_hold: dict | None = None
    # When set (`before` timing), the patient CONFIRMED a slot and the gate held it for a
    # deposit: a Pix exists and NOTHING was created on Google or in `appointments`. The caller
    # persists `flow_state = AWAITING_DEPOSIT` (the selection is carried, so "Trocar horário"
    # can re-open the picker) and then sends the Pix; if that send fails it voids the charge.
    # Mutually exclusive with `appointment` and `booking_hold`.
    # {"deposit_id", "amount_cents", "copy_paste", "expires_at", "start_at", "end_at",
    #  "service", "minutes", "payment_note"}
    deposit_request: dict | None = None
    # When set, the matching appointment row should be moved (RESCHEDULED):
    # {"google_event_id", "start_at", "end_at"}.
    appointment_reschedule: dict | None = None
    # TASK-032 R3 ("Marcar outra consulta"): the live appointment the booking in
    # progress REPLACES. Written unconditionally by `_apply_flow_result` like
    # every flow field; `_carry_replacement` keeps it only while the
    # conversation stays inside the booking, and the booking tail cancels the
    # original in the same transaction as the new row
    # (services/appointment_replacement.py).
    flow_replaces_appointment_id: UUID | None = None
    # TASK-032 R6 ("Alterar Dados"): the edit draft (`EditDraft.to_json()`), written
    # unconditionally by `_apply_flow_result` like every flow field and kept by
    # `_carry_edit_draft` only while the conversation stays in `EDIT_BOOKING`.
    flow_edit_draft: dict | None = None
    # TASK-032 R6: the confirmed edit, applied to the SAME appointment row by
    # `workers/shared/appointment_edit_apply.py` in `_apply_flow_result`'s transaction:
    # {"appointment_id": UUID, "old_google_event_id", "google_event_id", "google_event_link",
    #  "appointment_type", "professional_id": UUID | None, "old_professional_id": UUID | None,
    #  "insurance", "attendee_name", "start_at", "end_at", "time_changed": bool,
    #  "doctor_changed": bool}. Never persisted on the conversation.
    appointment_edit: dict | None = None
    # TASK-083 follow-up (spec §6.3): the patient reached a (doctor, service) that no bookable
    # unit does - `(professional_id, service_id, service name)`. The caller tells the clinic
    # (debounced e-mail, no patient data); the patient's own reply is in `bubbles`. Never
    # persisted on the conversation.
    notify_no_unit: tuple[UUID, UUID, str] | None = None


@dataclass
class MenuBubble:
    """An N-button reply card (the formatter's ButtonBubble is confirm/cancel
    only). Produced solely by the deterministic flow; the worker renders it via
    WhatsAppClient.send_buttons. The tapped label comes back as the next body.
    """

    body: str
    labels: list[str] = field(default_factory=list)
    kind: Literal["menu"] = "menu"


@dataclass
class ActionButtonsBubble:
    """A reply card whose buttons carry their OWN ids, `<action>|<uuid>`: a tap is an ACTION
    (`schemas/webhook.py::decode_action_id` -> `workers/shared/actions._handle_action_button`)
    on both channels, whatever the conversation is doing at the time - unlike a MenuBubble,
    whose tapped label comes back as the next body. First use: the "quer a diferença do sinal
    de volta?" question after a change to a cheaper service (2026-10-26). At most 3 buttons."""

    body: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    kind: Literal["action_buttons"] = "action_buttons"


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

EDIT_DAY_BRANCH = DayBranch(
    flow_state=FlowState.EDIT_BOOKING,
    day_step=STEP_EDIT_DAY,
    day_retry_step=STEP_EDIT_DAY_RETRY,
    day_escape_step=STEP_EDIT_DAY_ESCAPE,
    slot_step=STEP_EDIT_SLOT,
    day_body="Qual o novo dia da consulta?",
    slot_body_prefix="Horários livres em",
)

# Every step that the day branch owns, per branch — used by `_catalog_step` /
# `_manage_step` to dispatch the retry/escape renders to the same handler.
_BOOKING_DAY_STEPS = (STEP_AWAITING_DAY, STEP_AWAITING_DAY_RETRY, STEP_AWAITING_DAY_ESCAPE)

# TASK-088 R41: the steps that can call the LLM (the scoped help nodes). A tap that selects the
# doctor in the same turn goes straight to the days now (no service card in between), so the worker
# hands `route()` the calendar of the doctor stored BEFORE the turn - the clinic's - and runs it
# again on the picked doctor's (`calendar_owner_changed`).
LLM_HELP_STEPS = frozenset(
    {
        STEP_PROFESSIONAL_HELP,
        STEP_PROFESSIONAL_HELP_FINAL,
        STEP_SERVICE_HELP,
        STEP_SERVICE_HELP_FINAL,
    }
)


def calendar_owner_changed(conversation: Any, result: FlowRouterResult) -> bool:
    """Whether `result` was drawn on another doctor's agenda than the one the turn picked (R41).

    True when the booking day picker was reached (`agenda_owner_id` is set by `_ask_day` on every
    result that got there - days, calendar view, no days at all, calendar unavailable) on behalf
    of a professional the conversation had NOT stored yet: the turn's calendar was the previous
    owner's (the clinic's), so the result - whatever it says - must be drawn again. Never for a turn
    that started on a help step (it can call the LLM: it is never run twice - and it never needs
    to be, see the worker). A parked AI draft does not exempt it: the resolver only uses this
    result as its fallback, and a resumed draft builds its own agendas. Pure."""
    owner = result.agenda_owner_id
    return (
        owner is not None
        and owner != _selected_professional_id(conversation)
        and getattr(conversation, "flow_step", None) not in LLM_HELP_STEPS
    )


_MANAGE_DAY_STEPS = (STEP_MANAGE_DAY, STEP_MANAGE_DAY_RETRY, STEP_MANAGE_DAY_ESCAPE)
_EDIT_DAY_STEPS = (STEP_EDIT_DAY, STEP_EDIT_DAY_RETRY, STEP_EDIT_DAY_ESCAPE)


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
    flow_insurance_auto: bool | None = None
    flow_managing_appointment_id: UUID | None = None
    flow_attendee_name: str | None = None
    flow_edit_draft: dict | None = None
    flow_selected_unit_id: UUID | None = None


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


# Per-clinic switch for the AI draft v2 (TASK-030): the six-field `set_booking_draft`, every
# hand-back landed by the resolver (services/booking_draft.py) and, from P3-P5 on, the
# express confirmation, the availability tool and the new prompt. Lives in the same
# `initial_flows` JSON as `menu_label`/`buttons`/`reactivation`. NOT part of the switch:
# asking pra-quem when it is unknown - that safety fix applies to every clinic.
AI_DRAFT_V2_FLAG = "ai_draft_v2"


def ai_draft_v2_enabled(tenant: Any) -> bool:
    """True only when the clinic's `initial_flows` holds the JSON literal `true`.

    OFF by default and by every malformed value (a string "true" included): the owner turns
    it on for one clinic by a data change and watches the logs before anyone else. A hub
    save that ever rewrites `initial_flows` without the key turns it OFF - the safe side.
    """
    flows = getattr(tenant, "initial_flows", None) or {}
    return isinstance(flows, dict) and flows.get(AI_DRAFT_V2_FLAG) is True


def menu_buttons(tenant: Tenant) -> list[str]:
    """The up-to-3 menu button labels (falls back to the MVP defaults)."""
    buttons = (tenant.initial_flows or {}).get("buttons") or DEFAULT_MENU_BUTTONS
    return [str(b) for b in buttons][:3]


def _is_multi_professional(professionals: list | None) -> bool:
    """2+ active professionals -> the multi-doctor menu replaces the default."""
    return len(professionals or []) > 1


def menu_buttons_for(tenant: Tenant, multi_professional: bool) -> list[str]:
    """Effective menu labels: the fixed multi-doctor pair, or the tenant's own.

    Shared with workers/tasks.py (the greeting card doubles as the menu when
    flows are enabled, and the reactivation-reset path re-renders it) so every
    surface shows the same effective menu.

    TASK-088: "Escolher médico" left the multi-doctor menu - the booking starts with
    the SERVICE, and the doctor comes after it. The label stays RECOGNISED
    (`BTN_CHOOSE_PROFESSIONAL`, `_menu_choice_multi`, greeting's name guard): a typed one,
    or a tap on an older card that still shows it, opens the same service-first booking.
    """
    if multi_professional:
        return [BTN_CHOOSE_SERVICE, LABEL_OTHER]
    return menu_buttons(tenant)


def main_menu_buttons(has_upcoming: bool = False) -> list[str]:
    """The buttons RENDERED under the main menu: [🗓️ Agendar, Outro], for every clinic.

    Owner, 2026-10-05: the booking flow now starts with "Essa consulta é pra
    você?" (then, since TASK-088, serviço → médico → unidade → convênio), so the
    old entries ("Escolher médico"/"Escolher serviço", or a clinic's own
    "Serviços e Custo") offered a choice the flow itself makes later. Only what is DRAWN changes:
    `menu_buttons_for` keeps the full vocabulary, so a typed old label (or a
    tap on an older card still on screen) routes exactly as before. Both
    labels are matched in `route()` ahead of the multi-doctor and index
    dispatch, so this pair works on single- and multi-doctor clinics alike.

    `has_upcoming`: the patient has a live future appointment, so the menu
    also offers "Gerenciar consulta" in the middle slot (3 buttons, WhatsApp's
    cap). Without it the pair above left that patient no visible way to
    reschedule or cancel from the menu - found live on the Portal on
    2026-10-05, and the WhatsApp side effect the TASK-035 checkpoint recorded
    as pending. The label is the one `route()` already dispatches to
    `_enter_manage` (one appointment: straight to its Remarcar/Cancelar card),
    so nothing new is routed. The router never knows the answer itself (it
    does no DB I/O): the worker decides it right before sending, after this
    turn's own cancel/booking committed - see
    workers/shared/flow_runner.py::_offer_manage_on_main_menu.
    """
    if has_upcoming:
        return [decorate(EMOJI_SCHEDULE, LABEL_BOOK), LABEL_MANAGE_APPOINTMENT, LABEL_OTHER]
    return [decorate(EMOJI_SCHEDULE, LABEL_BOOK), LABEL_OTHER]


def is_main_menu(bubble) -> bool:
    """True for a card that IS the main menu (`main_menu_buttons()` as drawn).

    Matched by its labels because every surface builds it the same way -
    `_menu_bubbles`, `/menu` and the agent's `show_main_menu` - so the worker
    can offer the manage entry on all of them at one seam.
    """
    return isinstance(bubble, MenuBubble) and list(bubble.labels) == main_menu_buttons()


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


def reactivation_prompt_enabled(tenant: Tenant) -> bool:
    """True when the returning "quer continuar?" prompt may be offered.

    Universal by default, unlike ``reactivation_enabled``. This prompt ships a
    product default text (``DEFAULT_CONTINUE_PROMPT``), so it needs no
    per-tenant config to work, and it is the visible half of a SAFETY exit: a
    returning patient must never be silently rerouted just because their clinic
    left a hub field blank. Gating it on a filled-in column is precisely the
    anti-pattern that once left default tenants with no time-based exit from
    ``FlowState.LLM`` at all (see ``llm_state_ttl_minutes``).

    ``initial_flows.reactivation.enabled`` stays an explicit override so
    internal provisioning can still turn the prompt OFF for one tenant - config
    TUNES this, it does not ENABLE it. Turning it off stays safe because the
    silent floor (``workers/tasks.py::_expire_stale_llm_state``) runs BEFORE the
    prompt and is what actually bounds the stay.
    """
    config = _reactivation_config(tenant)
    if "enabled" in config:
        return bool(config["enabled"])
    return True


def reactivation_gap_minutes(tenant: Tenant) -> int:
    """Minutes of silence after which a returning patient is offered to resume."""
    try:
        value = _reactivation_config(tenant).get("gap_minutes")
        return int(value) if value is not None else DEFAULT_REACTIVATION_GAP_MINUTES
    except (TypeError, ValueError):
        return DEFAULT_REACTIVATION_GAP_MINUTES


def llm_state_ttl_minutes(tenant: Tenant) -> int:
    """Minutes a conversation may sit in ``FlowState.LLM`` before it is dropped.

    Full LLM mode is sticky BY DESIGN (see ``route()``'s first branch and
    ``FlowState.LLM``'s docstring): mid-conversation every turn has to keep
    reaching the agent, or a patient halfway through an answer would be yanked
    back to the menu. What it must not be is PERMANENT — and it was. Every exit
    is patient- or agent-initiated (``/menu``, or one of the four hand-back
    tools: ``show_main_menu``, ``start_guided_booking``,
    ``select_professional_and_continue``, ``manage_existing_appointment``), plus
    the returning-patient offer — which only runs for tenants where
    ``reactivation_enabled`` is true, i.e. those who filled in
    ``returning_greeting_message`` in the hub. Default tenants have
    ``initial_flows == {}`` and that column NULL, so nothing bounded the stay at
    all, and ``Conversation`` is ONE row per patient forever
    (``uq_conversations_tenant_patient``) — there is no next conversation that
    would start clean. One "Outro" tap parked a patient on the free LLM for
    every future contact.

    The threshold is deliberately the SAME silence gap as the returning-patient
    offer: "long enough that the next inbound is a fresh contact" is a single
    notion in this product, and it already has a name and a per-tenant knob
    (``initial_flows.reactivation.gap_minutes``). Kept as its own function so
    the LLM-expiry policy kept a single named home should the two ever need to
    diverge — the same reason ``flows_enabled`` survives as a function.
    """
    return reactivation_gap_minutes(tenant)


# How long a Brain-Message conversation may sit waiting for an e-mail or for
# the 6-digit code before the active state is dropped and a bounded
# "quer continuar?" gate is offered.
#
# NOT per-tenant, and that is deliberate: `llm_state_ttl_minutes` is tunable
# because clinics genuinely differ on how long a free-form chat should stay
# open, while these two states are steps of a platform-owned identity flow that
# no clinic configures, sees, or can reason about. A knob here would be an
# unconsumed config field — the thing `plugins/precheck_handoff.py` argues
# against for its own message.
#
# 60 minutes, against `llm_state_ttl_minutes`' default: long enough that a
# patient who walks away to fetch the code from another device comes back to a
# live prompt, short enough that an abandoned visit does not park the
# conversation for the rest of the day. brain-api expires the code itself after
# 10 minutes and the whole visit after 24 hours (CHECKPOINT §6.7), so an hour
# sits inside the outer bound and comfortably outside the inner one.
PENDING_IDENTITY_TTL_MINUTES = 60

# How long the AI's parked booking draft (`Conversation.flow_draft`, TASK-030 P2) stays
# usable while the patient answers "Essa consulta é pra você?". Past it, the answer simply
# continues the button flow: two minutes is a pause, half an hour is another conversation.
# Not per-tenant, for the same reason PENDING_IDENTITY_TTL_MINUTES is not.
FLOW_DRAFT_TTL_MINUTES = 30


def pending_identity_ttl_minutes(tenant: Tenant) -> int:
    """Silence budget for AWAITING_EMAIL / AWAITING_EMAIL_CODE / AWAITING_NAME, in minutes.

    Takes `tenant` and ignores it, exactly as `flows_enabled` takes one and
    returns True: the decision keeps ONE home, so the day it does become
    per-clinic there is a single function to change and every caller already
    passes what it would need. See this module's convention note on
    `flows_enabled`.
    """
    return PENDING_IDENTITY_TTL_MINUTES


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
    if target and (target == _norm(no_label) or target == _norm(truncate_button_label(no_label))):
        return "no"
    if target and (target == _norm(yes_label) or target == _norm(truncate_button_label(yes_label))):
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
    """The comparison key for every label/name match in this module.

    `strip_decoration` runs FIRST so a label and the tap it produces share a key
    even though only one of them carries an emoji. Every comparison below is
    `_norm(body) == _norm(LABEL_X)`, i.e. the strip applies to BOTH sides, which
    is what makes the decoration invisible to all ~20 of them at once:

        _norm("✅ Sim") == _norm("Sim") == _norm("sim")

    All three forms reach the router in practice - the tap, a card rendered
    before FEAT 44 shipped and still sitting in the patient's thread, and the
    patient who simply types "sim" instead of tapping. That last one is the
    common case for a yes/no question, and it is the one a naive
    `LABEL_YES = "✅ Sim"` would have silently broken.
    """
    return strip_decoration(text).casefold()


def _label_match(body: str | None, label: str) -> bool:
    """True when `body` equals `label` (or its 20-char button truncation)."""
    target = _norm(body)
    return bool(target) and (
        target == _norm(label) or target == _norm(truncate_button_label(label))
    )


def _menu_index(tenant: Tenant, body: str | None) -> int | None:
    """Index of the menu button whose label matches `body`, or None."""
    target = _norm(body)
    if not target:
        return None
    for index, label in enumerate(menu_buttons(tenant)):
        # send_buttons caps titles at 20 chars, so the tap echoes the truncated
        # label; match on both the full and truncated forms.
        if _norm(label) == target or _norm(truncate_button_label(label)) == target:
            return index
    return None


def _service_duration(service: dict, tenant: Tenant) -> int:
    try:
        return int(service.get("duration_min") or tenant.appointment_duration_min)
    except (TypeError, ValueError):
        return tenant.appointment_duration_min or 30


def _service_duration_at(
    service: dict, tenant: Tenant, professional: Any | None, unit_id: UUID | None
) -> int:
    """`_service_duration` at `unit_id` for `professional` (TASK-085 D2/D7).

    The unit's own duration (`professional.service_unit_terms`, carried by the snapshot) when it
    has one; otherwise exactly `_service_duration` - no unit, no professional, no exception or a
    row that only changes the price is today's value, byte for byte.
    """
    resolved = flow_units.terms_at(professional, service, unit_id)
    if resolved.own and resolved.duration_min is not None:
        return resolved.duration_min
    return _service_duration(service, tenant)


def _priced_entry(service: dict, professional: Any | None, unit_id: UUID | None = None) -> dict:
    """`service` as a text may price it (TASK-085 D3/R2, `flow_units.shown_terms`).

    The SAME dict when the price to show is the entry's own (every clinic without exceptions);
    a copy with the unit's price when it differs; a copy WITHOUT a price when it differs between
    the units and the unit is not known yet - never a range. Callers format it as before
    (`booking_details.service_price_text`).
    """
    shown = flow_units.shown_terms(professional, service, unit_id)
    if shown.price_hidden:
        return {**service, "price": None}
    if shown.price == resolve_terms(service, None, None).price:
        return service
    return {**service, "price": shown.price}


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


# --------------------------------------------------------------------------
# Free text that is talk, not an answer (TASK-038)
# --------------------------------------------------------------------------
#
# Owner, 2026-10-07: a message that is not an answer to the step reaches the model
# for one turn - the step and its card stay, so the next tap still works - instead
# of being swallowed as an answer ("qual o endereço?" stored as the convênio) or
# met with "Não entendi a data". Deliberately small and conservative: an answer the
# step can read always wins, and these only decide what happens to what it cannot.

_QUESTION_OPENERS = frozenset(
    {
        "qual", "quais", "quanto", "quanta", "quantos", "quantas", "como", "onde",
        "aonde", "quando", "porque", "pq", "quem", "voces", "vcs", "vc", "voce",
    }
)
_CONVERSATION_PREFIXES = ("por que", "o que", "nao sei")
# More words than any plan name or date answer a patient types.
_CONVERSATION_MIN_WORDS = 7


def _reads_as_conversation(text: str | None) -> bool:
    """True for a question or a sentence - talk to answer, not a value to store."""
    key = normalize(text)
    if not key:
        return False
    words = key.split()
    return (
        "?" in key
        or len(words) >= _CONVERSATION_MIN_WORDS
        or words[0] in _QUESTION_OPENERS
        or key.startswith(_CONVERSATION_PREFIXES)
    )


_NO_INSURANCE_ANSWERS = frozenset(
    {"nao", "nenhum", "nenhuma", "nao tenho", "nao uso", "sem convenio", "nao tenho convenio"}
)


def _says_no_insurance(text: str | None) -> bool:
    """ "não tenho" / "sem convênio" at the convênio question: that is Particular."""
    return normalize(text).strip(" .!") in _NO_INSURANCE_ANSWERS


_MONTH_NAMES = frozenset(
    {
        "janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto",
        "setembro", "outubro", "novembro", "dezembro",
    }
)


def _looks_like_date_attempt(text: str | None) -> bool:
    """Whether unreadable text still TRIED to name a day ("dia 35", "31/02").

    Those keep the "Não entendi a data" re-ask; anything else is talk for the model.
    """
    key = normalize(text)
    if any(ch.isdigit() for ch in key):
        return True
    words = set(re.findall(r"[a-z]+(?:-feira)?", key))
    full_names = _WEEKDAY_KEYS - _WEEKDAY_ABBREVIATIONS
    return bool(
        words & (full_names | _MONTH_NAMES | {"hoje", "amanha"})
        or _bare_weekday_abbreviations(key)
    )


_WEEKDAY_WORDS = {
    "segunda": 0, "segunda-feira": 0, "seg": 0,
    "terça": 1, "terca": 1, "terça-feira": 1, "terca-feira": 1, "ter": 1,
    "quarta": 2, "quarta-feira": 2, "qua": 2,
    "quinta": 3, "quinta-feira": 3, "qui": 3,
    "sexta": 4, "sexta-feira": 4, "sex": 4,
    "sábado": 5, "sabado": 5, "sab": 5,
    "domingo": 6, "dom": 6,
}
_WEEKDAY_KEYS = frozenset(normalize(word) for word in _WEEKDAY_WORDS)
# The 3-letter forms are also everyday words ("posso TER desconto?" read as a
# Tuesday in production, 2026-10-06), so they only count when they ARE the answer:
# alone, or with filler like "na", "pode ser", "dia" and time words ("qua às 10",
# "sex de manhã", "seg 14h" - digits never count as words here).
_WEEKDAY_ABBREVIATIONS = frozenset({"seg", "ter", "qua", "qui", "sex", "sab", "dom"})
_DAY_ANSWER_FILLER = frozenset(
    {
        "a", "o", "e", "ou", "na", "no", "da", "do", "de", "dia", "pode", "ser",
        "prefiro", "proxima", "proximo", "que", "vem", "essa", "esta", "nessa", "nesta",
        "feira", "as", "pela", "pelo", "manha", "tarde", "noite", "cedo", "h", "hs",
        "hr", "hrs", "hora", "horas", "min",
    }
)


def _bare_weekday_abbreviations(text: str) -> frozenset[str]:
    """The abbreviations when they ARE the answer ("ter", "na sex", "ter ou qua"), else none."""
    words = [w for w in re.findall(r"[a-z]+", normalize(text)) if w not in _DAY_ANSWER_FILLER]
    if words and all(w in _WEEKDAY_ABBREVIATIONS for w in words):
        return frozenset(words)
    return frozenset()


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

    bare_abbreviations = _bare_weekday_abbreviations(text)
    for word, weekday in _WEEKDAY_WORDS.items():
        if word in _WEEKDAY_ABBREVIATIONS and word not in bare_abbreviations:
            continue
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
    """The menu prompt rendered as a single reply-button card (`main_menu_buttons`)."""
    return [MenuBubble(body=menu_label(tenant), labels=main_menu_buttons())]


def is_generic_menu_result(result: FlowRouterResult) -> bool:
    """True for the plain menu card `route()` shows when a message from IDLE means nothing specific.

    TASK-032 R3: on the turn the chat opens with the appointment reminder, that
    card (whose "Outro" already offers everything the menu would) replaces this
    one, so the worker skips it (`workers/shared/flow_runner.py::_run_flow`).
    Anything that records, books, moves, cancels or asks a specific question is
    NOT generic and is always answered.
    """
    return (
        result.action == "reply"
        and result.flow_state == FlowState.MENU
        and result.flow_step is None
        and result.appointment is None
        and result.appointment_cancel_id is None
        and result.appointment_reschedule is None
        and result.decline_reason is None
        and result.booking_hold is None
        and bool(result.bubbles)
        and all(isinstance(bubble, MenuBubble) for bubble in result.bubbles)
    )


def _selected_professional_id(conversation: Conversation) -> UUID | None:
    """The conversation's picked professional (getattr: snapshots may predate it)."""
    return getattr(conversation, "flow_selected_professional_id", None)


def _selected_insurance(conversation: Conversation) -> str | None:
    """The conversation's stored convênio label (getattr: same rationale)."""
    return getattr(conversation, "flow_selected_insurance", None)


def _attendee_name(conversation: Conversation) -> str | None:
    """The in-progress booking's pra-quem answer (tri-state, see services/attendee.py).

    None = not asked yet; ATTENDEE_SELF ("") = the patient themself; else the
    authorized third party's name. Use `real_attendee_name` before persisting.
    """
    return getattr(conversation, "flow_attendee_name", None)


def _carry_attendee(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """Keep the attendee on every result that stays inside the booking (or LLM).

    `_apply_flow_result` writes every flow field unconditionally, and the
    attendee is chosen FIRST - before a dozen builders (professional list,
    unit step, day picker...) that know nothing about it. Rather than
    thread it through each of them, the two public entries apply this once.

    Leaves alone: a result that already names an attendee, the attendee steps
    themselves (they decide it explicitly, including clearing it on
    "Cancelar"), and anything leaving the booking (menu, manage flow, the
    Brain-Message hold - which carries the name on the hold row instead), so
    the name is dropped exactly when the booking it belongs to ends.
    """
    if result.flow_attendee_name is not None or result.flow_step in ATTENDEE_STEPS:
        return result
    if result.flow_state in (FlowState.SERVICE_CATALOG, FlowState.LLM):
        result.flow_attendee_name = _attendee_name(conversation)
    return result


def _carry_insurance(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """Keep the convênio answer on every result that stays inside the booking.

    Same reason as `_carry_attendee`: a convênio can be known BEFORE the booking reaches its
    question (an AI draft that named it, a chat parked on the retired first question, a
    booking whose convênio list is reopened), so the service list, the doctor list, the unit
    step and the scoped-help nodes that come first must not lose it - `_apply_flow_result`
    writes `flow_selected_insurance` unconditionally. (TASK-088: in the button flow the
    convênio is asked LAST, after the unit - `_insurance_gate` - and judged there.)

    A patient can open the convênio picker explicitly to change Particular. Keep their choice
    until they give a different answer, so returning from that picker does not erase it.
    """
    if result.insurance_dropped:
        return result
    selected = _selected_insurance(conversation)
    if (
        result.flow_selected_insurance is None
        and result.flow_state in (FlowState.SERVICE_CATALOG, FlowState.LLM)
        and (
            result.flow_step != STEP_AWAITING_INSURANCE
            or _chosen_particular(conversation)
        )
    ):
        result.flow_selected_insurance = selected
    if (
        selected is not None
        and result.flow_selected_insurance == selected
        and result.flow_insurance_auto is None
    ):
        result.flow_insurance_auto = getattr(conversation, "flow_insurance_auto", None)
    return result


def _carry_draft(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """Keep the AI's parked draft while the patient is still on a step it waits on.

    `_apply_flow_result` writes `flow_draft` unconditionally, so any result that does not
    name it clears it - which is the point: the draft waits only on `DRAFT_WAIT_STEPS`
    (pra-quem, and since TASK-030 P3 the convênio, doctor and service questions), and a
    result on any other step (the menu, the LLM, a help node, a list further on) ends that
    wait.
    """
    if result.flow_draft is None and result.flow_step in DRAFT_WAIT_STEPS:
        result.flow_draft = getattr(conversation, "flow_draft", None)
    return result


# The states a "Marcar outra" booking may pass through (TASK-032 R3): the
# booking itself, the AI answering a question in the middle of it (TASK-030
# hands back to the same card), and the Portal's code wait after "Confirmar"
# (the appointment is born at `_promote_booking_hold`).
REPLACEMENT_KEEP_STATES: tuple[FlowState, ...] = (
    FlowState.SERVICE_CATALOG,
    FlowState.LLM,
    FlowState.AWAITING_EMAIL_CODE,
)
REPLACEMENT_NOTICE = "Ao confirmar, sua consulta anterior será cancelada."


def _replaces_appointment_id(conversation: Conversation) -> UUID | None:
    """The appointment a booking in progress replaces (getattr: snapshots may predate it)."""
    return getattr(conversation, "flow_replaces_appointment_id", None)


def _carry_replacement(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """Keep the "Marcar outra" marker while the patient is still booking.

    `_apply_flow_result` writes `flow_replaces_appointment_id` unconditionally,
    so a result that does not name it clears it - which is the point: the
    marker must never outlive the booking it belongs to (an abandoned "Marcar
    outra" leaves the original untouched). Kept only when the conversation is
    ALREADY in one of `REPLACEMENT_KEEP_STATES` and the result stays in one, so
    a marker left behind on an IDLE conversation can never ride into a booking
    started later from the menu. A result that books (`appointment`) never
    carries it: the caller consumes the stored value in that same transaction.
    """
    if result.flow_replaces_appointment_id is not None or result.appointment is not None:
        return result
    if (
        getattr(conversation, "flow_state", None) in REPLACEMENT_KEEP_STATES
        and result.flow_state in REPLACEMENT_KEEP_STATES
    ):
        result.flow_replaces_appointment_id = _replaces_appointment_id(conversation)
    return result


def _replacement_line(conversation: Conversation) -> str:
    """The confirmation card's warning while a "Marcar outra" booking is in progress."""
    return f"\n\n{REPLACEMENT_NOTICE}" if _replaces_appointment_id(conversation) else ""


def _carry_edit_draft(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """Keep the "Alterar Dados" draft while the edit continues (TASK-032 R6).

    `_apply_flow_result` writes `flow_edit_draft` unconditionally, so a result that does
    not name it clears it - exactly right for a result that LEAVES the edit (menu, idle,
    the AI). A result that stays in `EDIT_BOOKING` without naming a draft (a scoped-help
    or day-picker result built without it) keeps the conversation's own.
    """
    if result.flow_edit_draft is not None:
        return result
    if (
        getattr(conversation, "flow_state", None) == FlowState.EDIT_BOOKING
        and result.flow_state == FlowState.EDIT_BOOKING
    ):
        result.flow_edit_draft = getattr(conversation, "flow_edit_draft", None)
    return result


# The steps after the unit was chosen (TASK-083 spec §8): only they keep it.
UNIT_KEEP_STEPS: tuple[str | None, ...] = (
    STEP_AWAITING_DAY,
    STEP_AWAITING_DAY_RETRY,
    STEP_AWAITING_DAY_ESCAPE,
    STEP_AWAITING_SLOT,
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_RETRY,
)
# The states a chosen unit survives in: the booking itself, the AI answering a question in the
# middle of it, and the deposit waits that replay "Confirmar" / re-open the picker.
UNIT_KEEP_STATES: tuple[FlowState, ...] = (
    FlowState.SERVICE_CATALOG,
    FlowState.LLM,
    FlowState.AWAITING_DEPOSIT,
    FlowState.AWAITING_PAYER_DATA,
)


def _carry_unit(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """Keep the chosen unit while the booking it belongs to goes on (TASK-083 spec §8).

    Same reason as `_carry_insurance`: `_apply_flow_result` writes `flow_selected_unit_id`
    unconditionally and the day/slot builders know nothing about it. Kept only on a result for
    the SAME professional that stays past the unit step (day, slot, confirmation) or in a state
    the booking survives in; the unit step itself, the lists before it (service, doctor) and a
    booking that ended (`appointment`) all drop it.
    """
    selected = flow_units.selected_unit_id(conversation)
    if (
        selected is None
        or result.unit_dropped
        or result.flow_selected_unit_id is not None
        or result.appointment is not None
        or result.flow_step == flow_units.STEP_AWAITING_UNIT
        or result.flow_state not in UNIT_KEEP_STATES
        or result.flow_selected_professional_id != _selected_professional_id(conversation)
    ):
        return result
    if result.flow_state is FlowState.SERVICE_CATALOG and result.flow_step not in UNIT_KEEP_STEPS:
        return result
    result.flow_selected_unit_id = selected
    return result


def _carry_booking(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """The carries, applied once per public entry (route, resume, hand-back)."""
    return _carry_unit(conversation, _carry_edit_draft(conversation, _carry_replacement(
        conversation,
        _carry_draft(
            conversation, _carry_insurance(conversation, _carry_attendee(conversation, result))
        ),
    )))


def _selected_managing_appointment_id(conversation: Conversation) -> UUID | None:
    """The conversation's in-progress manage-flow target (getattr: same rationale)."""
    return getattr(conversation, "flow_managing_appointment_id", None)


def _service_row_title(name: str | None) -> str:
    """A `svc|` row's visible title: "🏥 <name>" when the name fits, else the cut.

    The ONE place the service emoji is decided, shared by both catalog renders
    (`_service_list_bubble`, the doctor-first list; `_enter_clinic_service_catalog`,
    the clinic-wide one) so the two screens cannot show the same service
    differently.

    Conditional, not unconditional: a `svc|` tap arrives as the displayed title
    and nothing else, so this string is the lookup key `canonical_service_name`
    resolves. Spending the emoji's two characters on a name already long enough
    to be cut would shorten the distinguishing tail that keeps
    "Consulta de rotina adulto" and "…infantil" from collapsing into one row -
    the exact failure core/whatsapp_limits.py was written to prevent. Long names
    therefore keep the budget they have today and go undecorated.
    """
    return decorate_if_fits(EMOJI_SERVICE, str(name or ""))


# --------------------------------------------------------------------------
# Portal rows of the catalogue lists (TASK-082). The router stays channel-neutral:
# each list bubble carries BOTH the WhatsApp rows (cut, capped - unchanged) and an
# `OptionSource` with the whole list as Portal rows; `workers/shared/bubbles.py`
# picks one per channel. `services/list_options.py` rebuilds the same rows from a
# card's stored scope, through these same helpers, for the options route and taps.
# --------------------------------------------------------------------------

# The fixed last row of the doctor-first service list (`svchelp|0`, "Não sei").
SERVICE_HELP_ROW: tuple[str, str, str | None] = ("svchelp|0", LABEL_DONT_KNOW, None)
PROFESSIONAL_HELP_ROW: tuple[str, str, str | None] = (
    "profhelp|0",
    LABEL_DONT_KNOW,
    "Te ajudo a escolher",
)


def portal_service_title(name: str | None) -> str:
    """A `svc|` row's Portal title: WhatsApp's emoji rule, never the 24-char cut."""
    value = str(name or "").strip()
    if len(EMOJI_SERVICE) + 1 + len(value) <= MAX_LIST_ROW_TITLE_CHARS:
        return decorate(EMOJI_SERVICE, value)
    return value


def portal_service_rows(
    services: list[dict], refused: frozenset[str] = frozenset()
) -> list[tuple[str, str, str | None]]:
    """Every service as a Portal row, in the order given (the router's own order).

    `refused` (TASK-086): names the convênio matrix refuses for the patient's plan - their row
    says so (INSURANCE_REFUSED_MARK), like WhatsApp's."""
    return [
        (
            f"svc|{s.get('name', '')}",
            portal_service_title(s.get("name")),
            INSURANCE_REFUSED_MARK if str(s.get("name", "")) in refused else None,
        )
        for s in services
    ]


def service_scope(professional_id: UUID | str | None) -> dict:
    """The stored scope of a service list built from `professional_id`'s catalogue.

    Mirrors how every router branch picks `services`: the selected doctor's own
    `professional_appointment_types`, else the tenant's `active_appointment_types`.
    """
    if professional_id is None:
        return {"list": "tenant"}
    return {"list": "doctor", "professional_id": str(professional_id)}


def _service_name(service: dict) -> str:
    return str(service.get("name", ""))


def service_page_rows(
    services: list[dict],
    page: int,
    *,
    fixed_rows: int,
    event: str,
    refused: frozenset[str] = frozenset(),
) -> list[tuple]:
    """One WhatsApp page of a `svc|` list, plus its "Ver mais serviços" row when needed.

    Logs `event` (the old `*_truncated` name, kept for the dashboards) when the list
    needs pages.
    """
    shown, next_page = catalog_page(services, page, fixed_rows=fixed_rows)
    if len(shown) < len(services):
        logger.info(event, total=len(services), shown=len(shown), page=page, paged=True)
    rows: list[tuple] = []
    for s in shown:
        row_id, title = f"svc|{_service_name(s)}", _service_row_title(s.get("name"))
        # TASK-086: only a refused service gets a description (a 3-tuple); every other row
        # stays the 2-tuple it always was.
        rows.append(
            (row_id, title, INSURANCE_REFUSED_MARK)
            if _service_name(s) in refused
            else (row_id, title)
        )
    rows.extend(catalog_more_row(ROW_SERVICE_MORE_PREFIX, LABEL_MORE_SERVICES, next_page))
    return rows


def _service_list_bubble(
    tenant: Tenant,
    services: list[dict],
    header: str | None = None,
    *,
    scope: dict | None = None,
    page: int = 0,
    professional: Any | None = None,
    insurance: str | None = None,
) -> Bubble:
    """The tappable service list, optionally introduced by `header`.

    `header` folds the selected doctor's presentation INTO this card instead
    of spending a whole separate WhatsApp message on it (see
    `_enter_professional_services`): the patient confirms they are on the
    right doctor and picks the service on one screen. Capped at the list
    body's own limit so a long hub-written `about` can never make the message
    unsendable.

    `page` (TASK-082 S4): the WhatsApp page; the Portal pointer is the same on every page.

    `professional`/`insurance` (TASK-086 A4): whose services these are and the patient's
    convênio - a service the matrix refuses for it says "não atende este convênio". While the
    matrix can act, the stored scope carries the convênio (`list_options` re-marks the pages).
    """
    refused = service_refused_names(tenant, professional, services, insurance)
    if (
        scope is not None
        and professional is not None
        and _matrix_plan_id(tenant, insurance) is not None
    ):
        scope = {**scope, "insurance": insurance, "unit_marks": True}
    rows = service_page_rows(
        services, page, fixed_rows=1, event="flow_service_list_truncated", refused=refused
    )
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
        source=(
            OptionSource(
                kind="service",
                scope=scope,
                items=portal_service_rows(services, refused),
                pinned=[SERVICE_HELP_ROW],
            )
            if scope is not None
            else None
        ),
    )


def _preserve(conversation: Conversation, action: str) -> FlowRouterResult:
    """Keep the conversation's current flow fields (used for delegate_llm)."""
    return FlowRouterResult(
        action=action,  # type: ignore[arg-type]
        flow_edit_draft=getattr(conversation, "flow_edit_draft", None),
        flow_state=conversation.flow_state,
        flow_step=conversation.flow_step,
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_day=conversation.flow_selected_day,
        flow_selected_slot=conversation.flow_selected_slot,
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
        flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
        flow_managing_appointment_id=_selected_managing_appointment_id(conversation),
        flow_attendee_name=_attendee_name(conversation),
        flow_selected_unit_id=flow_units.selected_unit_id(conversation),
    )


def _delegate_llm_keeping_draft(conversation: Conversation) -> FlowRouterResult:
    """Hand the turn to the LLM while preserving the booking draft.

    The worker writes every flow field unconditionally. Keep the answers
    already collected, but drop the button step and slot so an old selection
    cannot be resurrected when the agent returns to the guided flow.
    """
    return FlowRouterResult(
        action="delegate_llm",
        flow_state=FlowState.LLM,
        flow_selected_type=getattr(conversation, "flow_selected_type", None),
        flow_selected_day=getattr(conversation, "flow_selected_day", None),
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
        flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
        flow_attendee_name=_attendee_name(conversation),
        flow_selected_unit_id=flow_units.selected_unit_id(conversation),
    )


def _enter_other(conversation: Conversation | None = None) -> FlowRouterResult:
    """ "Outro" on the menu: ask `OTHER_OPENER` and park in LLM mode for the answer.

    Lands exactly where a delegation would (with `conversation`, the booking answers
    survive like `_delegate_llm_keeping_draft`), but this turn is a plain reply: the
    model only runs on what the patient says next.
    """
    result = (
        _delegate_llm_keeping_draft(conversation)
        if conversation is not None
        else FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM)
    )
    result.action = "reply"
    result.bubbles = [TextBubble(body=OTHER_OPENER)]
    return result


def enter_human_offer(
    conversation: Conversation | None = None, *, body: str = HUMAN_OFFER_BODY
) -> FlowRouterResult:
    """The fixed "quer que eu chame nosso atendente humano?" card (TASK-038).

    Waits in LLM mode on STEP_HUMAN_OFFER, keeping the booking answers like any
    delegation; `_handle_human_offer` reads the tap. `body` is the question
    (`HUMAN_OFFER_BOOKING_BODY` in the deposit flow); the mechanics never change.
    """
    result = (
        _delegate_llm_keeping_draft(conversation)
        if conversation is not None
        else FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM)
    )
    result.action = "reply"
    result.bubbles = [MenuBubble(body=body, labels=[LABEL_YES, LABEL_NO])]
    result.flow_step = STEP_HUMAN_OFFER
    return result


def _handle_human_offer(
    conversation: Conversation, tenant: Tenant, body: str, professionals: list | None
) -> FlowRouterResult:
    """The patient's answer to `enter_human_offer`'s card."""
    if _label_match(body, LABEL_YES):
        logger.info(
            "human_offer_answered",
            accepted=True,
            conversation_id=str(getattr(conversation, "id", None)),
        )
        # action="handover": workers/shared/flow_runner.py commits the human state and
        # alerts the clinic BEFORE this message goes out. Built on the conversation's own
        # answers, exactly what a direct request_human_handoff leaves behind: the flow
        # fields are written first, and the alert reads the chosen doctor back from them
        # (workers/shared/handover.py) - a bare result would erase whom to e-mail.
        result = _delegate_llm_keeping_draft(conversation)
        result.action = "handover"
        result.bubbles = [TextBubble(body=SCOPED_HELP_ESCALATE_MESSAGE)]
        return result
    if _label_match(body, LABEL_NO):
        logger.info(
            "human_offer_answered",
            accepted=False,
            conversation_id=str(getattr(conversation, "id", None)),
        )
        # Stay in the free conversation with every booking answer kept; the model
        # takes the patient's next message.
        result = _delegate_llm_keeping_draft(conversation)
        result.action = "reply"
        result.bubbles = [TextBubble(body=HUMAN_OFFER_DECLINED_BODY)]
        return result
    # Neither: the patient kept talking - the model answers, the offer is dropped.
    return _delegate_llm_keeping_draft(conversation)


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
    gate: BookingGate | None = None,
    edit_context: EditContext | None = None,
) -> FlowRouterResult:
    """Route one turn with `gate` in scope for every listing/booking branch.

    The whole body lives in `_route`; this wrapper exists only to publish the
    gate on `_ACTIVE_GATE` and take it down again, in a `finally`, so a raise
    inside the router cannot leave the next turn holding this turn's gate.
    See `_ACTIVE_GATE` for why the gate travels this way rather than as an
    argument on every function between here and the slot picker.
    """
    with booking_gate_scope(gate):
        result = await _route(
            conversation,
            tenant,
            calendar,
            inbound_body,
            patient_name,
            upcoming_appointments=upcoming_appointments,
            professionals=professionals,
            gate=gate,
            edit_context=edit_context,
        )
        return _carry_booking(conversation, result)


async def _route(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    inbound_body: str,
    patient_name: str | None = None,
    upcoming_appointments: list[dict] | None = None,
    professionals: list | None = None,
    gate: BookingGate | None = None,
    edit_context: EditContext | None = None,
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

    # The open "quer que eu chame nosso atendente humano?" card (TASK-038). Checked
    # before the sticky LLM branch below, which would otherwise swallow the tap.
    if state == FlowState.LLM and conversation.flow_step == STEP_HUMAN_OFFER:
        return _handle_human_offer(conversation, tenant, inbound_body, professionals)

    # Once in full LLM mode, stay there until a /menu reset (or the agent's
    # show_main_menu tool). The selected professional/insurance survive so the
    # agent keeps that doctor's context across LLM turns.
    if state == FlowState.LLM:
        return _delegate_llm_keeping_draft(conversation)

    # Why-not-rebooking (FEAT_34 §8). Keyed on the STEP rather than a state of
    # its own: it is a single question with a single answer, and inventing a
    # FlowState for one turn would add a mode every other branch must reason
    # about. Checked before the state branches so the answer is never
    # reinterpreted as a menu tap.
    if conversation.flow_step == STEP_DECLINE_REASON:
        return _handle_decline_reason(conversation, inbound_body)

    if state == FlowState.SERVICE_CATALOG:
        return await _catalog_step(
            conversation, tenant, calendar, inbound_body, patient_name, professionals, gate
        )

    if state == FlowState.MANAGE_BOOKING:
        return await _manage_step(
            conversation,
            tenant,
            calendar,
            inbound_body,
            upcoming_appointments or [],
            professionals,
            patient_name=patient_name,
        )

    if state == FlowState.EDIT_BOOKING:
        from secretaria.services.appointment_edit_flow import edit_step

        return await edit_step(
            conversation, tenant, inbound_body, upcoming_appointments or [], professionals,
            edit_context, patient_name,
        )

    # AWAITING_EMAIL / AWAITING_EMAIL_CODE / AWAITING_NAME should never reach
    # here: all three are intercepted upstream by `workers/tasks.py`'s identity
    # gates, which own the whole turn and return before dispatch. Reaching this line means
    # the gate let one through (a channel mismatch, a state left behind by an
    # older build), and the safe answer is the menu — falling through to the
    # branches below does exactly that, and the result they build carries an
    # explicit `flow_state`, so the conversation leaves the stranded state on
    # this very turn rather than re-entering it. Named here so the next reader
    # does not have to re-derive that it is deliberate.
    if state in (
        FlowState.AWAITING_EMAIL,
        FlowState.AWAITING_EMAIL_CODE,
        FlowState.AWAITING_NAME,
        FlowState.AWAITING_DEPOSIT,
        FlowState.AWAITING_PAYER_DATA,
    ):
        logger.warning(
            "flow_router_pending_identity_state_leaked",
            state=state.value,
            conversation_id=str(conversation.id),
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

    # "Outro" from the greeting trio must always open the free conversation, even
    # on a single-doctor tenant whose configured menu buttons don't include it
    # (the index-based mapping below only knows the configured labels). Same
    # place-it-anywhere semantics as the manage label above; the same fixed
    # question as _enter_menu_choice's 3rd slot and _menu_choice_multi's
    # labels[2] (only this path carries the booking draft along).
    if _label_match(inbound_body, LABEL_OTHER):
        return _enter_other(conversation)

    if _is_multi_professional(professionals):
        return _menu_choice_multi(conversation, tenant, inbound_body, professionals or [])

    index = _menu_index(tenant, inbound_body)
    if index is None:
        if state == FlowState.MENU:
            # Free text at the menu -> the patient wants something custom.
            return _delegate_llm_keeping_draft(conversation)
        # IDLE/BUSINESS_HOURS: (re)present the menu.
        return FlowRouterResult(
            action="reply", bubbles=_menu_bubbles(tenant), flow_state=FlowState.MENU
        )
    if index == 0:
        # "Serviços e Custo" is the single-doctor menu's ONLY way into a
        # booking (it is also where `show_main_menu` lands a patient), so it
        # asks pra-quem first like "Agendar" does. Its continuation is the same
        # service list `_enter_menu_choice` renders for index 0.
        return _ask_attendee_first(ATTENDEE_NEXT_BOOK, tenant, professionals)
    return await _enter_menu_choice(index, tenant, calendar)


def _menu_choice_multi(
    conversation: Conversation, tenant: Tenant, body: str, professionals: list
) -> FlowRouterResult:
    """Menu interaction for a multi-doctor tenant (fixed 2-button menu).

    "Escolher serviço" opens the clinic's unified service catalog and lets the picked
    service narrow the doctors (`_enter_clinic_service_catalog`) - fully deterministic;
    "Outro" hands off exactly like today. TASK-088: the old "Escolher médico" door is
    gone from the menu, but a typed one (or a tap on an older card) is NOT an error: it
    opens the very same service-first booking.
    """
    labels = menu_buttons_for(tenant, True)
    if _label_match(body, BTN_CHOOSE_PROFESSIONAL):
        return _ask_attendee_first(ATTENDEE_NEXT_BOOK, tenant, professionals)
    if _label_match(body, labels[0]):
        return _ask_attendee_first(ATTENDEE_NEXT_CATALOG, tenant, professionals)
    if _label_match(body, labels[1]):
        return _enter_other(conversation)
    if conversation.flow_state == FlowState.MENU:
        # Free text at the menu -> the patient wants something custom.
        return _delegate_llm_keeping_draft(conversation)
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
            bubbles=[_service_list_bubble(tenant, services, scope=service_scope(None))],
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
    # "Outro" (or any 3rd button): the fixed question, then the LLM on the answer.
    return _enter_other()


def enter_booking(tenant: Tenant, professionals: list | None = None) -> FlowRouterResult:
    """Deterministic entry for a direct "Agendar" tap: "Essa consulta é pra você?".

    Asks pra-quem first (services/attendee.py); the answer continues into
    `_start_booking`, which is what this function returned before the question
    existed.
    """
    return _ask_attendee_first(ATTENDEE_NEXT_BOOK, tenant, professionals)


def _booking_entry(
    next_step: str | None, tenant: Tenant, professionals: list | None
) -> FlowRouterResult:
    """The first list a booking opens: the SERVICE (TASK-088, owner 2026-11-20).

    Both multi-doctor menu entries ("Escolher serviço" and the retired "Escolher médico",
    `next_step`) open the same thing: the clinic's unified service catalog, whose pick then
    narrows the doctors. A clinic with ONE active doctor opens his own service list - no
    doctor step. Also the early dead-end check: a clinic whose doctors offer no service at
    all answers "não há serviços" + the menu here, never a list whose every tap dead-ends.
    """
    if _is_multi_professional(professionals):
        return _enter_clinic_service_catalog(tenant, professionals or [])
    return _start_booking(tenant, professionals)


def _booking_continuation(
    next_step: str | None,
    tenant: Tenant,
    professionals: list | None,
    *,
    insurance: str | None = None,
) -> FlowRouterResult:
    """What a booking entry opens once pra-quem is answered: the service, never the convênio.

    The order is now pra-quem -> serviço -> médico -> unidade -> convênio -> dia -> horário
    (owner, 2026-11-20): the convênio only means something once service, doctor and unit are
    known, so it is asked later, from the day step's choke point (`_ask_day` ->
    `_insurance_gate`) and only when the clinic collects it. It used to be the first
    question (owner, 2026-09-23), filtering nothing and only marking doctors.

    `insurance` is an answer that ALREADY exists (an AI draft that landed with one, a parked
    chat): it rides along and is checked against the combination when the booking gets
    there - kept when accepted, asked again (never swapped for Particular in silence) when not.
    """
    entry = _booking_entry(next_step, tenant, professionals)
    if entry.flow_state != FlowState.SERVICE_CATALOG:
        return entry
    if insurance is not None:
        entry.flow_selected_insurance = insurance
    return entry


def _attendee_question(next_step: str) -> FlowRouterResult:
    return FlowRouterResult(
        action="reply",
        bubbles=[
            MenuBubble(
                body=ATTENDEE_QUESTION_BODY,
                labels=[LABEL_ATTENDEE_SELF, LABEL_ATTENDEE_OTHER],
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_ATTENDEE_CHOICE,
        flow_selected_type=next_step,
    )


def _ask_attendee_first(
    next_step: str, tenant: Tenant, professionals: list | None
) -> FlowRouterResult:
    """"Essa consulta é pra você?" in front of a booking entry - unless it dead-ends.

    The continuation is computed first: when it would not open a booking at all
    (no services, no bookable doctor) the patient gets that answer straight
    away instead of a question whose every answer leads to "não há serviços".
    """
    entry = _booking_entry(next_step, tenant, professionals)
    if entry.flow_state != FlowState.SERVICE_CATALOG:
        return entry
    return _attendee_question(next_step)


def _attendee_name_request(next_step: str | None, *, invalid: bool = False) -> FlowRouterResult:
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=ATTENDEE_NAME_INVALID if invalid else ATTENDEE_NAME_REQUEST)],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_ATTENDEE_NAME,
        flow_selected_type=next_step,
    )


def _attendee_authorization_card(next_step: str | None, name: str) -> FlowRouterResult:
    return FlowRouterResult(
        action="reply",
        bubbles=[
            ButtonBubble(
                body=authorization_body(name),
                confirm_label=LABEL_ATTENDEE_AUTH_CONFIRM,
                cancel_label=LABEL_ATTENDEE_AUTH_BACK,
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_ATTENDEE_AUTH,
        flow_selected_type=next_step,
        flow_attendee_name=name,
    )


def _has_draft(conversation: Conversation) -> bool:
    """Whether an AI draft is parked on the conversation (TASK-030 P2)."""
    return bool(getattr(conversation, "flow_draft", None))


def _resume_parked_draft(conversation: Conversation, result: FlowRouterResult) -> FlowRouterResult:
    """The answer to a question an AI draft is waiting on: the worker resumes the draft.

    Set on the convênio answer and on the doctor / service taps (`_catalog_step`) when a
    draft is parked (TASK-030 P3; the pra-quem answer is P2's, in `_attendee_step`).
    workers/shared/draft_resolution.py::_resume_booking_draft folds the answer into the
    draft and re-runs the resolver over FRESH data; on any failure it keeps `result`. A
    result still ON the convênio step ("Outro convênio" asks for the name, an empty answer
    re-asks) has not answered yet and keeps waiting.

    TASK-088: so does any result that stays on the step the patient is already on (a "Ver mais"
    page, a re-asked list) - it moved nowhere, and re-running the draft would draw the list
    again from its first page.
    """
    if (
        _has_draft(conversation)
        and result.action == "reply"
        and result.flow_state is FlowState.SERVICE_CATALOG
        and result.flow_step != STEP_AWAITING_INSURANCE
        and result.flow_step != getattr(conversation, "flow_step", None)
    ):
        result.resume_draft = True
    return result


def _attendee_step(
    conversation: Conversation, tenant: Tenant, body: str, professionals: list | None
) -> FlowRouterResult:
    """pra-quem -> [name -> authorization] -> the booking entry's own list.

    Never delegates the NAME step to the LLM: whatever is typed there is either
    a name or a re-ask, so a third party's name cannot reach the model through
    an unmatched-input fallback. The two button steps fall back to the LLM on
    free text like every other catalog step (state kept, next tap still works).
    """
    step = conversation.flow_step
    next_step = conversation.flow_selected_type

    if step == STEP_AWAITING_ATTENDEE_CHOICE:
        if _label_match(body, LABEL_ATTENDEE_SELF):
            logger.info("attendee_choice", choice="self")
            result = _booking_continuation(
                next_step, tenant, professionals, insurance=_selected_insurance(conversation)
            )
            # Remember the answer: NULL would read as "not asked yet" and the
            # LLM hand-back would ask pra-quem a second time (services/attendee.py).
            if result.flow_state == FlowState.SERVICE_CATALOG:
                result.flow_attendee_name = ATTENDEE_SELF
                # A parked AI draft continues from here (workers/shared/draft_resolution.py).
                result.resume_draft = _has_draft(conversation)
            return result
        if _label_match(body, LABEL_ATTENDEE_OTHER):
            logger.info("attendee_choice", choice="other")
            return _attendee_name_request(next_step)
        return _preserve(conversation, "delegate_llm")

    if step == STEP_AWAITING_ATTENDEE_NAME:
        name = parse_attendee_name(body)
        # A boolean only - never the value (skill pii-field-capture).
        logger.info("attendee_name_answered", parsed=name is not None)
        if name is None:
            return _attendee_name_request(next_step, invalid=True)
        return _attendee_authorization_card(next_step, name)

    # STEP_AWAITING_ATTENDEE_AUTH
    name = _attendee_name(conversation)
    if not name or _label_match(body, LABEL_ATTENDEE_AUTH_BACK):
        # Back one step, name dropped: the pra-quem question again.
        return _attendee_question(next_step or ATTENDEE_NEXT_BOOK)
    if _label_match(body, LABEL_ATTENDEE_AUTH_CONFIRM):
        result = _booking_continuation(
            next_step, tenant, professionals, insurance=_selected_insurance(conversation)
        )
        if result.flow_state == FlowState.SERVICE_CATALOG:
            result.flow_attendee_name = name
            result.attendee_authorized = True
            result.resume_draft = _has_draft(conversation)
        return result
    return _preserve(conversation, "delegate_llm")


def _start_booking(
    tenant: Tenant, professionals: list | None = None, *, insurance: str | None = None
) -> FlowRouterResult:
    """The service list of a clinic with ONE active doctor (an empty roster lands here too).

    TASK-088: only for a roster of one (or none), where it renders the doctor's own service list
    (decision recorded in docs/CHECKPOINT_convenio_catalogo.md: "show every doctor" over a roster
    of one is a list of one). Both callers - `_booking_entry` and the AI draft resolver
    (`booking_draft`) - send a multi-doctor clinic to the clinic's service catalog
    (`_enter_clinic_service_catalog`) instead, so this never lists the doctors.

    `insurance` is the patient's convênio answer, when it was already given: it only MARKS the
    services the doctor does not take that plan for.
    """
    services = active_appointment_types(tenant)
    if not services:
        return FlowRouterResult(
            action="reply",
            bubbles=[TextBubble(body="No momento não há serviços disponíveis para agendamento.")],
            flow_state=FlowState.IDLE,
        )
    sole = (professionals or [None])[0] if len(professionals or []) == 1 else None
    return FlowRouterResult(
        action="reply",
        bubbles=[
            _service_list_bubble(
                tenant,
                services,
                scope=service_scope(None),
                professional=sole,
                insurance=insurance,
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE,
    )


# --------------------------------------------------------------------------
# Professional selection (multi-doctor branch of SERVICE_CATALOG)
# --------------------------------------------------------------------------


def _professional_id_from_body(body: str | None) -> UUID | None:
    """Parse the professional UUID out of a list-row tap ("Dra. Ana (uuid)").

    Mirrors `_slot_iso_from_body`: schemas.webhook.inbound_routing_text
    re-attaches a "prof|<uuid>" row id to its title as "<title> (<uuid>)", so
    the UUID rides in the trailing parentheses (or is the whole text when the
    row had no title). That is the routing text only - the stored message body
    is the title alone.
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
        if _norm(truncate_list_row_title(name)) == target or _norm(name) == target:
            return professional
    return None


def _professional_row_title(name: str | None) -> str:
    """A `prof|` row's visible title: "🥼 <name>", truncated to absorb it.

    Unlike `_service_row_title`, this is unconditional: a `prof|` row's tap
    always carries the professional's UUID in the trailing parens
    (`_PAYLOAD_ROW_PREFIXES`, schemas/webhook.py — the comment there notes two
    doctors CAN share a 24-char truncated title already), so the title is
    never the sole lookup key the way a `svc|` title is. There is no tail to
    protect, so every doctor gets the coat every time, exactly like the
    LLM-facing `[SLOTS]` calendar emoji (`ai/formatter.py`), not like the
    length-guarded service one.
    """
    return decorate_and_truncate(EMOJI_DOCTOR, str(name or ""))


def _selected_plan(tenant: Tenant, insurance: str | None) -> dict | None:
    """The clinic plan the patient's convênio answer names, or None.

    None for "Particular", a typed "Outro convênio", no answer at all, and on a
    tenant snapshot without catalog ids (legacy strings): nothing to mark then.
    """
    plan = match_plan(_tenant_insurance_plans(tenant), insurance)
    return plan if plan is not None and plan.get("id") else None


def _accepts_plan(tenant: Tenant, professional: Any, plan_id: str) -> bool:
    """TASK-008 §4.2: who counts as accepting a plan depends on `insurance_mode`.

    `shared` - every active professional accepts every clinic plan; no
    per-doctor row is ever consulted. `clinic_with_exceptions` - the doctor's
    own row if they have customized (`Professional.insurance_plans_customized`
    - absent from `insurance_accepted_by` means "never customized", which
    inherits ALL clinic plans, per the owner's TASK-008 decision, SPEC §2).
    `independent` - only the doctor's own row decides, with NO clinic-wide
    inheritance (there is no clinic list for this mode to inherit from).
    `None` (mode not chosen yet, or a legacy bare `Tenant` that predates the
    mode column) preserves TASK-006's ORIGINAL default instead: a doctor who
    never took a plan explicitly does not accept it - this decision is not
    reopened, only scoped down to the one mode that changes it.
    """
    mode = getattr(tenant, "insurance_mode", None)
    if mode == "shared":
        return True
    accepted_by = getattr(tenant, "insurance_accepted_by", None) or {}
    professional_id = str(professional.id)
    if professional_id in accepted_by:
        return plan_id in accepted_by[professional_id]
    return mode == "clinic_with_exceptions"


# --------------------------------------------------------------------------
# TASK-086: the convênio acceptance matrix (services/insurance_acceptance.py) on the flow.
# The matrix only ever SUBTRACTS: with no refused cell (or no plan with an id - Particular, a
# typed "Outro", a legacy name) every helper below is a no-op and the flow is today's, byte for
# byte (scopes included: the extra keys are written only while the matrix can act).
# --------------------------------------------------------------------------


def _matrix_plan_id(tenant: Tenant, insurance: str | None) -> str | None:
    """The plan id the matrix may refuse for `insurance`, or None when it cannot act."""
    if not has_exceptions(snapshot_exceptions(tenant)):
        return None
    plan = _selected_plan(tenant, insurance)
    return str(plan["id"]) if plan is not None else None


def _plan_kwargs(tenant: Tenant, insurance: str | None) -> dict:
    """`flow_units`' convênio filter kwargs for the booking's plan ({} = today's filter)."""
    plan_id = _matrix_plan_id(tenant, insurance)
    if plan_id is None:
        return {}
    return {"plan_id": plan_id, "refused": snapshot_exceptions(tenant)}


def _doctor_service_id(tenant: Tenant, professional: Any, service_name: str | None) -> Any:
    """(entry, service_id) of `professional`'s own catalog entry for `service_name`."""
    entry = _match_service(professional_appointment_types(professional, tenant), service_name)
    return entry, (entry_service_id(entry) if entry is not None else None)


def _matrix_refuses_doctor(
    tenant: Tenant, professional: Any, plan_id: str | None, service_name: str | None = None
) -> bool:
    """True when the MATRIX is why `professional` cannot take `plan_id` (A1).

    Service known and offered by the doctor: refused at every place it is bookable. Otherwise:
    refused in every one of the doctor's services (a service outside the matrix accepts)."""
    exc = snapshot_exceptions(tenant)
    if plan_id is None or not has_exceptions(exc):
        return False
    if service_name:
        entry, service_id = _doctor_service_id(tenant, professional, service_name)
        if entry is not None:
            return service_id is not None and refuses_everywhere(
                exc, professional, service_id, plan_id
            )
    return refuses_all_services(
        exc, professional, professional_appointment_types(professional, tenant), plan_id
    )


def service_refused_names(
    tenant: Tenant, professional: Any | None, services: list[dict], insurance: str | None
) -> frozenset[str]:
    """The names of `services` the matrix refuses for `insurance` with `professional` (A4)."""
    plan_id = _matrix_plan_id(tenant, insurance)
    if plan_id is None or professional is None:
        return frozenset()
    exc = snapshot_exceptions(tenant)
    refused: set[str] = set()
    for service in services:
        service_id = entry_service_id(service)
        if service_id is not None and refuses_everywhere(exc, professional, service_id, plan_id):
            refused.add(str(service.get("name", "")))
    return frozenset(refused)


def insurance_plans_for_service(
    tenant: Tenant,
    plans: list,
    professionals: list | None,
    service_name: str | None,
    professional_id: Any = None,
) -> list:
    """The clinic's plans accepted somewhere for `service_name` (A3, SPEC E6).

    By that doctor when one is chosen (or the clinic has one), else by ANY doctor offering the
    service. A plan without id (legacy) is kept. Without the matrix, an unknown service, or no
    roster to judge by: every plan, as today."""
    exc = snapshot_exceptions(tenant)
    if not service_name or not has_exceptions(exc) or not professionals:
        return list(plans)
    roster = list(professionals)
    if professional_id is not None:
        doctor = _find_professional_by_id(roster, professional_id)
        if doctor is None:
            return list(plans)
        candidates = [doctor]
    elif len(roster) == 1:
        candidates = roster
    else:
        candidates = _professionals_offering(tenant, roster, service_name)
    if not candidates:
        return list(plans)
    pairs = [(doc, _doctor_service_id(tenant, doc, service_name)[1]) for doc in candidates]

    def _taken(plan: dict) -> bool:
        plan_id = plan.get("id")
        if not plan_id:
            return True
        return any(
            sid is None or not refuses_everywhere(exc, doc, sid, str(plan_id)) for doc, sid in pairs
        )

    return [plan for plan in plans if _taken(plan)]


def _professional_rows(
    tenant: Tenant,
    professionals: list,
    insurance: str | None,
    *,
    service_name: str | None = None,
) -> tuple[list[tuple[str, str, str | None]], dict | None]:
    """One `prof|` row per doctor - ALL of them - marked when they take the plan.

    Owner, 2026-09-23: "quando for escolher o médico é necessário mostrar todos
    porém ter uma especificação que certos médicos aceitam o convênio da
    pessoa". So this never filters: a doctor who does not take the plan keeps
    their row exactly as before (specialty as description); one who does gets
    INSURANCE_ACCEPTED_MARK in front of it. Returns the matched plan too, so
    the caller can name it in the list body. `service_name` (TASK-086): the
    service already chosen, so the convênio matrix is asked about THAT service.
    """
    plan = _selected_plan(tenant, insurance)
    rows: list[tuple[str, str, str | None]] = []
    marked = 0
    for professional in professionals:
        description, accepted = _professional_description(
            tenant, professional, plan, service_name=service_name
        )
        marked += accepted
        rows.append(
            (f"prof|{professional.id}", _professional_row_title(professional.name), description)
        )
    if plan is not None:
        # Counts only - never WHICH plan (it identifies the patient's coverage).
        logger.info("professional_list_insurance_marked", shown=len(rows), marked=marked)
    return rows, plan


def _professional_description(
    tenant: Tenant, professional: Any, plan: dict | None, *, service_name: str | None = None
) -> tuple[str | None, bool]:
    """A `prof|` row's description (specialty, marked when they take `plan`) + the mark.

    TASK-086: a doctor whose list takes the plan but whom the convênio matrix refuses (for
    `service_name`, or for all their services) gets INSURANCE_REFUSED_MARK instead - listed,
    never hidden."""
    specialty = (getattr(professional, "specialty", None) or "").strip() or None
    if plan is not None and _accepts_plan(tenant, professional, str(plan["id"])):
        if _matrix_refuses_doctor(tenant, professional, str(plan["id"]), service_name):
            return (
                truncate_plain(
                    f"{INSURANCE_REFUSED_MARK} · {specialty}"
                    if specialty
                    else INSURANCE_REFUSED_MARK,
                    MAX_LIST_ROW_DESCRIPTION_CHARS,
                ),
                False,
            )
        return (
            truncate_plain(
                f"{INSURANCE_ACCEPTED_MARK} · {specialty}" if specialty
                else INSURANCE_ACCEPTED_MARK,
                MAX_LIST_ROW_DESCRIPTION_CHARS,
            ),
            True,
        )
    return specialty, False


def portal_professional_rows(
    tenant: Tenant,
    professionals: list,
    insurance: str | None,
    *,
    service_name: str | None = None,
) -> list[tuple[str, str, str | None]]:
    """Every doctor as a Portal row: full "🥼 <name>", the same description WhatsApp shows."""
    plan = _selected_plan(tenant, insurance)
    return [
        (
            f"prof|{professional.id}",
            decorate(EMOJI_DOCTOR, str(professional.name or "")),
            _professional_description(tenant, professional, plan, service_name=service_name)[0],
        )
        for professional in professionals
    ]


def professional_scope(
    insurance: str | None,
    pool: list | None = None,
    *,
    service: str | None = None,
    unit_marks: bool = False,
) -> dict:
    """The stored scope of a doctor list; `pool` narrows it to those doctors only.

    TASK-086: `service` (the service already chosen) and `unit_marks` (the rows depend on each
    doctor's units through the convênio matrix) are written only while the matrix can act, so
    `list_options` re-marks later pages the same way (`needs_unit_options`)."""
    scope: dict = {"insurance": insurance}
    if pool is not None:
        scope["only_ids"] = [str(p.id) for p in pool]
    if service:
        scope["service"] = service
    if unit_marks:
        scope["unit_marks"] = True
    return scope


def _matrix_professional_scope(
    tenant: Tenant, insurance: str | None, pool: list | None, service_name: str | None
) -> dict:
    """`professional_scope` with the TASK-086 keys exactly when the matrix can mark the list."""
    matrix = _matrix_plan_id(tenant, insurance) is not None
    return professional_scope(
        insurance,
        pool,
        service=service_name if matrix else None,
        unit_marks=matrix,
    )


def _professional_list_body(question: str, plan: dict | None) -> str:
    if plan is None:
        return question
    return f"{question}\n\n{EMOJI_AFFIRMATIVE} = aceita {plan['name']}"


def professional_page(
    professionals: list, page: int, *, fixed_rows: int, event: str
) -> tuple[list, list[tuple[str, str]]]:
    """One WhatsApp page of a doctor list: (doctors shown, its "Ver mais" row or nothing).

    Logs `event` (the old `*_truncated` name, kept for the dashboards) when the list
    needs pages.
    """
    shown, next_page = catalog_page(professionals, page, fixed_rows=fixed_rows)
    if len(shown) < len(professionals):
        logger.info(event, total=len(professionals), shown=len(shown), page=page, paged=True)
    more = catalog_more_row(ROW_PROFESSIONAL_MORE_PREFIX, LABEL_MORE_PROFESSIONALS, next_page)
    return shown, more


def _enter_professional_list(
    tenant: Tenant,
    professionals: list,
    *,
    insurance: str | None = None,
    pool: bool = False,
    page: int = 0,
    service_name: str | None = None,
) -> FlowRouterResult:
    """Render the tappable doctor list (the doctors the booking can still choose from).

    TASK-088: there is no "Escolher médico" door any more; this list is reached from the
    callers that hold a doctor-first booking (the AI draft's legacy landing, a rebooking
    after a doctor's cancellation, a parked chat) - the service-first order lists the doctors
    of the chosen service through `_enter_service_professional_list`.

    Each row carries the professional's specialty as the WhatsApp list-row
    description — the "apresentação dos médicos" made tappable — prefixed with
    INSURANCE_ACCEPTED_MARK when `insurance` names a plan that doctor takes
    (`_professional_rows`: every doctor listed, never filtered). Up to
    MAX_CATALOG_OPTION_ROWS doctors fit one page with the reserved "Não sei"
    scoped-help row appended last; a longer roster pages (`catalog_page`, TASK-082
    S4: `page`). The Portal pointer is the whole roster on every page.
    `service_name` (TASK-086): the service already chosen, for the convênio marks.
    """
    shown, more = professional_page(
        professionals, page, fixed_rows=1, event="flow_professional_list_truncated"
    )
    # The service only when known: the 3-argument call is the one other modules/tests rely on.
    by_service = {"service_name": service_name} if service_name else {}
    rows, plan = _professional_rows(tenant, shown, insurance, **by_service)
    rows.extend((row_id, title, None) for row_id, title in more)
    # Fixed last row: opens the scoped professional-help node
    # (STEP_PROFESSIONAL_HELP). Not "prof|"-prefixed on purpose - the tap must
    # arrive as the plain "Não sei" title (see extract_inbound_body), never as
    # a pseudo-professional id.
    rows.append(("profhelp|0", LABEL_DONT_KNOW, "Te ajudo a escolher"))
    # `pool`: a caller-chosen subset (rebooking's "Outro médico"), not the whole roster -
    # the stored scope names those doctors so later pages never widen the list.
    source = OptionSource(
        kind="professional",
        scope=_matrix_professional_scope(
            tenant, insurance, professionals if pool else None, service_name
        ),
        items=portal_professional_rows(tenant, professionals, insurance, service_name=service_name),
        pinned=[PROFESSIONAL_HELP_ROW],
    )
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=_professional_list_body(
                    "Com qual profissional você gostaria de agendar?", plan
                ),
                rows=rows,
                button_label="Ver profissionais",
                section_title="Profissionais",
                source=source,
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_PROFESSIONAL,
    )


# --------------------------------------------------------------------------
# Service-FIRST branch ("Escolher serviço", multi-doctor tenants only)
# --------------------------------------------------------------------------


def _clinic_service_catalog(
    tenant: Tenant, professionals: list, services: list | None = None
) -> list[dict]:
    """The clinic's unified bookable catalog: every active doctor's services.

    Union over `professional_appointment_types(p, tenant)`, deduplicated by
    canonical name (first occurrence wins, so a service two doctors both offer
    appears once) and sorted the same way every other catalog list is —
    sort_order, then name. Tenant-scoped by construction: `professionals` is
    THIS tenant's active roster and every fallback is to THIS tenant's own
    appointment_types, so no other clinic's services can enter here.
    `services` is the clinic's canonical catalog; omitted, the entries are read as
    stored (every pre-TASK-030 caller).
    """
    seen: set[str] = set()
    union: list[dict] = []
    for professional in professionals or []:
        for service in professional_appointment_types(professional, tenant, services):
            name = str(service.get("name", "")).strip()
            key = _norm(name)
            if not key or key in seen:
                continue
            seen.add(key)
            union.append(service)
    return sorted(union, key=lambda s: (s.get("sort_order", 0), s.get("name", "")))


def _professionals_offering(
    tenant: Tenant, professionals: list, service_name: str, services: list | None = None
) -> list:
    """The active professionals whose own catalog contains `service_name`."""
    return [
        professional
        for professional in professionals or []
        if canonical_service_name(
            professional_appointment_types(professional, tenant, services), service_name
        )
        is not None
    ]


def _enter_clinic_service_catalog(
    tenant: Tenant,
    professionals: list,
    *,
    page: int = 0,
) -> FlowRouterResult:
    """Render the clinic-wide service list ("Escolher serviço").

    Deliberately WITHOUT the "Não sei" scoped-help row the doctor's own service list
    carries: that node's hand-back (`_handle_service_help` -> `_continue_with_service`)
    assumes the doctor of the list it was asked on, and the clinic catalog has none yet.
    Wiring help into this branch needs its own pair of steps and is out of scope; the row is
    omitted rather than shipped pointing at the wrong re-entry.

    Up to MAX_LIST_ROWS services fit one page; a longer catalogue pages (`page`,
    TASK-082 S4).
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
    rows = service_page_rows(
        services, page, fixed_rows=0, event="flow_clinic_service_list_truncated"
    )
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body="Qual serviço você gostaria de agendar?",
                rows=rows,
                button_label="Ver serviços",
                section_title="Serviços",
                source=OptionSource(
                    kind="service",
                    scope={"list": "clinic"},
                    items=portal_service_rows(services),
                ),
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CATALOG_SERVICE,
    )


def _enter_service_professional_list(
    tenant: Tenant,
    professionals: list,
    service_name: str,
    *,
    insurance: str | None = None,
    page: int = 0,
) -> FlowRouterResult:
    """The doctors who DO `service_name`, with the service already stored.

    The second step of the booking since TASK-088 (service -> doctor -> unit -> convênio):
    the patient picked the service, and the list is the doctors that offer it - showing one
    who does not would only lead to a tap that falls back to that doctor's own services.
    (It used to list the whole roster, marking who took the patient's plan; the convênio is
    not known at this point any more.) `insurance` only matters to a conversation that
    already carries an answer (an AI draft, a parked chat) and marks the rows as before.
    An empty match - the catalog changed under the patient - lists everyone, as before.

    The tap lands on STEP_AWAITING_SERVICE_PROFESSIONAL, which goes on with that service
    (`_continue_with_service`: the unit, the convênio, the days) — never back at the service
    list, which the patient has already answered.

    Pages like every doctor list (`page`, TASK-082 S4).
    """
    offering = _professionals_offering(tenant, professionals, service_name) or list(professionals)
    shown, more = professional_page(
        offering, page, fixed_rows=0, event="flow_service_professional_list_truncated"
    )
    rows, plan = _professional_rows(tenant, shown, insurance, service_name=service_name)
    rows.extend((row_id, title, None) for row_id, title in more)
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=_professional_list_body(f"Quem você prefere para {service_name}?", plan),
                rows=rows,
                button_label="Ver profissionais",
                section_title="Profissionais",
                source=OptionSource(
                    kind="professional",
                    # `pool`: when not everyone does the service, only these doctors - on the
                    # Portal's later pages too (a full roster keeps the scope it always had).
                    scope=_matrix_professional_scope(
                        tenant,
                        insurance,
                        offering if len(offering) != len(professionals) else None,
                        service_name,
                    ),
                    items=portal_professional_rows(
                        tenant, offering, insurance, service_name=service_name
                    ),
                ),
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE_PROFESSIONAL,
        flow_selected_type=service_name,
    )


async def _continue_with_service(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    service: dict,
    professionals: list | None,
    *,
    professional: Any | None = None,
    services: list[dict] | None = None,
) -> FlowRouterResult:
    """The service was tapped: straight to the next step (TASK-088).

    The "Deseja agendar esse serviço?" card (name, price, long description, Sim / Outro serviço)
    is gone from the path - the price now waits for the final confirmation and the description
    is not repeated. What is left to ask after a service is what `_ask_day` asks, in the owner's
    order: the unit (when the doctor has several), then the convênio (when the clinic collects
    it), then the days. The doctor, when the service was tapped from a list that does not carry
    one (the clinic catalog with a single doctor offering it, the doctors of a service), is
    `professional`; else the conversation's own - a single-doctor clinic never stores one.

    The pick is in a throwaway `_DayPickerState`, not on the conversation yet - the same carrier
    the unit pick uses - so `_ask_day` reads the NEW service (and drops nothing the card used to
    drop: no unit, no day, the convênio the patient already gave rides along to be judged at the
    combination). `calendar` is the agenda as the worker resolved it; `_ask_day` scopes it."""
    state = _DayPickerState(
        id=getattr(conversation, "id", None),
        flow_selected_type=str(service.get("name", "")),
        flow_selected_professional_id=(
            professional.id if professional is not None else _selected_professional_id(conversation)
        ),
        flow_selected_insurance=_selected_insurance(conversation),
        flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
        flow_attendee_name=_attendee_name(conversation),
    )
    changed = (
        _norm(state.flow_selected_type) != _norm(conversation.flow_selected_type)
        or state.flow_selected_professional_id != _selected_professional_id(conversation)
        or flow_units.selected_unit_id(conversation) is not None
    )
    return await _ask_day_after_choice(
        state, tenant, calendar, services, professionals, changed=changed
    )


async def _handle_catalog_service(
    conversation: Conversation,
    tenant: Tenant,
    body: str,
    professionals: list,
    calendar: CalendarService | None = None,
) -> FlowRouterResult:
    """Resolve a clinic-catalog pick, then let the SERVICE narrow the doctors.

    Exactly one professional offers it -> straight on (unit / convênio / days) with the doctor
    already selected - TASK-088: no service card in between. Two or more ->
    the filtered doctor list. None -> the catalog changed under us mid-flow;
    a fixed reply plus the menu, never the LLM.
    """
    more = catalog_more_page(body, LABEL_MORE_SERVICES)
    if more is not None:
        return _enter_clinic_service_catalog(tenant, professionals, page=more)
    services = _clinic_service_catalog(tenant, professionals)
    service = _match_service(services, body)
    if service is None:
        return _preserve(conversation, "delegate_llm")
    service_name = str(service.get("name", ""))
    offering = _professionals_offering(tenant, professionals, service_name)
    if len(offering) == 1:
        professional = offering[0]
        return await _continue_with_service(
            conversation,
            tenant,
            calendar,
            service,
            professionals,
            professional=professional,
            services=professional_appointment_types(professional, tenant),
        )
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
    return _enter_service_professional_list(
        tenant, professionals, service_name, insurance=_selected_insurance(conversation)
    )


async def _handle_service_professional(
    conversation: Conversation,
    tenant: Tenant,
    body: str,
    professionals: list,
    calendar: CalendarService | None = None,
) -> FlowRouterResult:
    """Doctor tap inside the service-first branch: straight on (unit / convênio / days).

    The service was already chosen (`flow_selected_type`), so this must NOT
    re-render that doctor's whole service list the way
    `_enter_professional_services` does - that would ask a question the
    patient has already answered. TASK-088: nor does it draw the service card any more.
    A "Ver mais profissionais" tap pages the list
    (TASK-082 S4); anything that names no doctor goes to the model, as before.
    """
    more = catalog_more_page(body, LABEL_MORE_PROFESSIONALS)
    if more is not None:
        return _preserve_reply(
            conversation,
            _enter_service_professional_list(
                tenant,
                professionals or [],
                conversation.flow_selected_type or "",
                insurance=_selected_insurance(conversation),
                page=more,
            ).bubbles,
        )
    professional = _match_professional(professionals or [], body)
    if professional is None:
        return _preserve(conversation, "delegate_llm")
    doctor_services = professional_appointment_types(professional, tenant)
    service = _match_service(doctor_services, conversation.flow_selected_type)
    if service is None:
        # Deactivated between the two taps: fall back to that doctor's own
        # list rather than booking a service they no longer offer.
        return _enter_professional_services(
            professional, tenant, insurance=_selected_insurance(conversation)
        )
    return await _continue_with_service(
        conversation,
        tenant,
        calendar,
        service,
        professionals,
        professional=professional,
        services=doctor_services,
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


def _professional_config_incomplete(
    professional: Any, gap: str, *, bubbles: list | None = None
) -> FlowRouterResult:
    """THE result for "this doctor cannot be booked at all" — both gaps.

    Pure, like every other builder here: it decides and describes, it does not
    alert anyone. Sending the email (and debouncing it) is the worker's half,
    keyed on the `action`/`gap`/`flow_selected_professional_id` this carries —
    see workers/tasks.py::_handle_professional_config_incomplete.

    Lands on `FlowState.IDLE`, matching what the empty-services branch has
    always done: there is no next step to offer, so leaving the patient parked
    mid-booking would only make their next message land on a step that cannot
    move. `bubbles` lets the services gap keep the wording (and the doctor's
    presentation) it already had; the hours gap composes its own.
    """
    name = str(getattr(professional, "name", "") or "").strip()
    if bubbles is None:
        body = (
            PROFESSIONAL_NO_HOURS_MESSAGE.format(name=name)
            if name
            else PROFESSIONAL_NO_HOURS_FALLBACK
        )
        bubbles = [TextBubble(body=body)]
    return FlowRouterResult(
        action="professional_config_incomplete",
        bubbles=bubbles,
        flow_state=FlowState.IDLE,
        flow_selected_professional_id=getattr(professional, "id", None),
        professional_config_gap=gap,
    )


def _enter_professional_services(
    professional: Any, tenant: Tenant, *, insurance: str | None = None
) -> FlowRouterResult:
    """The selected doctor's services list, headed by their own presentation.

    ONE message, not two: the presentation (name + specialty + about) used to
    go out as its own TextBubble right before the list, which cost a whole
    WhatsApp message per booking to say something the list card has room for.
    It is now folded into the list body (`_service_list_bubble(header=...)`) —
    same information, same order, one message less.

    Factored out of the tap handler because the LLM hand-back tool
    (`select_professional_and_continue`) re-enters the deterministic flow
    through this exact result — see workers/tasks.py.

    `insurance` (TASK-086 A4): the patient's convênio, for the "não atende este convênio" rows.
    """
    services = professional_appointment_types(professional, tenant)
    if not services:
        # Same bubbles as ever — what changed is that the turn now SAYS why it
        # is a dead end, so the worker can tell the clinic (FEAT 41). This
        # check was already the non-ambiguous one: `professional_appointment_
        # types` is pure stored config, so an empty list can only mean nobody
        # configured a service, never a transient calendar condition.
        bubbles: list = []
        greeting = _professional_greeting_body(professional)
        if greeting:
            bubbles.append(TextBubble(body=greeting))
        bubbles.append(TextBubble(body=PROFESSIONAL_NO_SERVICES_MESSAGE))
        return _professional_config_incomplete(
            professional, PROFESSIONAL_GAP_SERVICES, bubbles=bubbles
        )
    return FlowRouterResult(
        action="reply",
        bubbles=[
            _service_list_bubble(
                tenant,
                services,
                header=_professional_card_header(professional),
                scope=service_scope(professional.id),
                professional=professional,
                insurance=insurance,
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE,
        flow_selected_professional_id=professional.id,
    )


# --------------------------------------------------------------------------
# Convênio step - asked LAST since 2026-11-20 (TASK-088): after service, doctor and unit,
# listing only the plans that combination accepts (it was the FIRST question, marking doctors)
# --------------------------------------------------------------------------


def _tenant_insurance_plans(tenant: Tenant) -> list[dict]:
    """The clinic's plans as `{"id", "name"}` dicts, catalog-backed when possible.

    The worker's tenant snapshot carries `insurance_plans` (the clinic's rows in
    `tenant_insurance_plans`, services/insurance_catalog.py) - the only source
    with ids, so the only one that can mark doctors. A bare `Tenant` (a caller
    that never built the snapshot) has no such attribute and falls back to the
    legacy `insurances` strings, which the hub keeps mirrored during the
    transition: same names, no ids, so no marks - never a wrong one.
    """
    plans = getattr(tenant, "insurance_plans", None)
    if plans is not None:
        return [plan for plan in plans if str(plan.get("name") or "").strip()]
    return [
        {"id": None, "name": str(plan)}
        for plan in (getattr(tenant, "insurances", None) or [])
        if str(plan).strip()
    ]


def _tenant_insurances(tenant: Tenant) -> list[str]:
    return [str(plan["name"]) for plan in _tenant_insurance_plans(tenant)]


def insurance_plan_names(tenant: Tenant) -> list[str]:
    """Public read of the clinic's convênio names (LLM context, services/llm_context.py)."""
    return _tenant_insurances(tenant)


def _insurance_step_skip_reason(tenant: Tenant) -> str | None:
    """Why the convênio step must be skipped for this tenant, or None to ask.

    `collect_insurance` and the clinic's plans are CLINIC-wide settings (the
    hub edits them once for the whole clinic) and the answer never filters
    doctors, services or slots. So the step depends on the clinic's
    configuration and NOTHING else: zero, one and many active professionals all
    get the same question - since TASK-088 right before the days, once service,
    doctor and unit are known (`_insurance_gate`).

    It used to additionally require `flow_selected_professional_id`, which only
    a multi-doctor tenant ever sets (`_enter_professional_services`). A clinic
    with a single professional — the common case — could switch the toggle on
    in the hub and never be asked, so the saved setting produced no behaviour
    and the booking lost operational information the clinic had asked for.
    """
    if not bool(getattr(tenant, "collect_insurance", False)):
        return INSURANCE_SKIP_DISABLED
    if getattr(tenant, "insurance_mode", None) is None:
        return INSURANCE_SKIP_NO_MODE
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
    # send_list caps row titles at 24 chars; match_plan compares on that too.
    plan = match_plan(_tenant_insurance_plans(tenant), body)
    return str(plan["name"]) if plan is not None else None



match_insurance_plan = _match_insurance_plan

def insurance_page(plans: list, page: int, *, reserved: int = 0) -> tuple[list, int | None]:
    """The plans one convênio-list page shows, and the next page's cursor (None = last).

    Up to MAX_INSURANCE_PLAN_ROWS plans fit one page with Particular + Outro. Past that,
    every page shows INSURANCE_PAGE_SIZE plans and keeps one row for "Ver mais
    convênios", so no plan is ever cut (Instituto Gicovate, 2026-10-27: 9 plans and the
    ninth, Unimed, never appeared). A cursor past the end restarts at the first page.

    `reserved` (TASK-088): rows the list carries besides those - the booking's back row
    ("Outro serviço" / "Outra unidade") - so the page still ends within WhatsApp's ten.
    """
    if len(plans) <= MAX_INSURANCE_PLAN_ROWS - reserved:
        return list(plans), None
    size = INSURANCE_PAGE_SIZE - reserved
    pages = -(-len(plans) // size)
    page = page if 0 <= page < pages else 0
    start = page * size
    return list(plans[start : start + size]), (page + 1 if page + 1 < pages else None)


def catalog_page(items: list, page: int, *, fixed_rows: int) -> tuple[list, int | None]:
    """The items one WhatsApp page of a service/doctor list shows, and the next cursor.

    `fixed_rows` is how many rows the list always carries besides its items ("Não sei"
    = 1). Up to `MAX_LIST_ROWS - fixed_rows` items fit one page - the list stays
    exactly what it was before paging existed. Past that, every page shows
    CATALOG_PAGE_SIZE items and keeps a row for "Ver mais ...". A cursor past the end
    (or negative) restarts at the first page, like `insurance_page`.
    """
    capacity = MAX_LIST_ROWS - fixed_rows
    if len(items) <= capacity:
        return list(items), None
    size = min(CATALOG_PAGE_SIZE, capacity - 1)
    pages = -(-len(items) // size)
    page = page if 0 <= page < pages else 0
    start = page * size
    return list(items[start : start + size]), (page + 1 if page + 1 < pages else None)


def catalog_more_row(prefix: str, label: str, next_page: int | None) -> list[tuple[str, str]]:
    """The "Ver mais ..." row for `next_page` (`<prefix><page>`), or nothing."""
    if next_page is None:
        return []
    return [(f"{prefix}{next_page}", label)]


def _more_page(payload: str | None) -> int:
    """The page of a "Ver mais ..." payload; anything after a second "|" is ignored.

    A card built by the pre-F1 branch carried a search key there (`svcmore|1|cardio`,
    CONTRACT.md D10): such a tap still pages, the key is dropped.
    """
    return _as_page((payload or "").partition("|")[0])


def catalog_more_page(body: str | None, label: str) -> int | None:
    """The page a "Ver mais serviços/profissionais" TAP asks for, or None.

    Only the tap (title + its "(page)" payload) counts: a typed "Ver mais ..." with no
    payload is not this control here, so typed text routes exactly as on `main` (where
    the row did not exist). On WhatsApp a typed "ver mais" is turned into the tap of
    the latest card's row before routing (workers/whatsapp/typed_more.py, D10).
    """
    payload = _row_payload(body)
    if payload is None or not _control_match(body, label):
        return None
    return _more_page(payload)


INSURANCE_PINNED_ROWS: tuple[tuple[str, str, str | None], ...] = (
    ("ins|particular", LABEL_INSURANCE_PARTICULAR, None),
    ("ins|outro", LABEL_INSURANCE_OTHER, None),
)


def insurance_plans_have_clinic_notes(plans: list) -> bool:
    """Does any of these plans carry the clinic's own note (TASK-085 R U1b)?

    The clinic's "Outro" plans and, in `independent` mode, a doctor's own "Outro" carry
    `note`; catalog plans and legacy names never do. One non-blank note is enough for the
    Portal to offer "Ver regras de pagamento dos convênios" (owner, 2026-11-18) - the same
    plans `patient_insurance_overview` then lists.
    """
    return any(isinstance(plan.get("note"), str) and plan["note"].strip() for plan in plans)


def portal_insurance_rows(plans: list) -> list[tuple[str, str, str | None]]:
    """Every clinic plan as a Portal row: the full name, no description (TASK-085 R D-R12: the
    per-plan payment text was removed - owner: "TIRE ISSO")."""
    return [(f"ins|{plan['name']}", str(plan["name"]), None) for plan in plans]


def insurance_more_row(next_page: int | None) -> list[tuple[str, str]]:
    """The "Ver mais convênios" row for `next_page`, or nothing on the last page."""
    return catalog_more_row(ROW_INSURANCE_MORE_PREFIX, LABEL_MORE_INSURANCES, next_page)


def insurance_more_page(body: str | None) -> int | None:
    """The page a "Ver mais convênios" tap asks for, or None when `body` is not that tap.

    As on `main`: a typed "Ver mais convênios" (no payload) is the first page. A
    pre-F1 card's trailing "|<key>" is ignored (`_more_page`).
    """
    if not _control_match(body, LABEL_MORE_INSURANCES):
        return None
    return _more_page(_row_payload(body))


def _insurance_unit(professional: Any | None, service_id: UUID | None, unit_id: Any) -> Any | None:
    """The unit the convênio is judged at: the picked one, else the doctor's only (implied) one.

    None when the doctor has none, or has several and the patient has not chosen yet (the list
    then falls back to "accepted at some unit of the service")."""
    if professional is None:
        return None
    state = _DayPickerState(flow_selected_unit_id=unit_id)
    return flow_units.effective_unit(state, professional, service_id)


def plans_accepted_for(
    tenant: Tenant, plans: list, professional: Any, service_id: UUID | None, unit: Any | None
) -> list:
    """The plans of `plans` accepted for (`service_id`, `professional`, `unit`) - TASK-088.

    Two rules, both already the product's: the doctor takes the plan under the clinic's
    `insurance_mode` (`_accepts_plan`) AND the convênio matrix does not refuse the cell
    (`is_accepted`). With a `unit` that exact cell is asked; without one (a doctor with no
    unit link: the "no unit" slice; a doctor with units not yet chosen: accepted at some unit
    that does the service). A plan without an id (a legacy name) is kept - nothing can judge
    it. Without a service the matrix has nothing to say, only the doctor's own list does."""
    exc = snapshot_exceptions(tenant)
    judge = service_id is not None and has_exceptions(exc)
    accepted = []
    for plan in plans:
        plan_id = plan.get("id")
        if not plan_id:
            accepted.append(plan)
            continue
        plan_id = str(plan_id)
        if not _accepts_plan(tenant, professional, plan_id):
            continue
        if judge:
            if unit is not None:
                taken = is_accepted(exc, professional.id, service_id, unit.id, plan_id)
            else:
                taken = not refuses_everywhere(exc, professional, service_id, plan_id)
            if not taken:
                continue
        accepted.append(plan)
    return accepted


def insurance_plans_for_booking(
    tenant: Tenant,
    plans: list,
    professionals: list | None,
    service_name: str | None,
    professional_id: Any = None,
    unit_id: Any = None,
) -> list:
    """The convênio list of a booking whose combination is known (TASK-088), as one function.

    The router builds page 1 with it and `services/list_options.py` rebuilds every later
    Portal page from the stored scope with the SAME call, so the two never differ. Doctor =
    the chosen one, else the clinic's only one. Doctor or service unknown: the older "accepted
    somewhere for the service" filter (`insurance_plans_for_service`), which an AI draft that
    landed on the question before the doctor still uses."""
    roster = list(professionals or [])
    if professional_id is not None:
        doctor = _find_professional_by_id(roster, professional_id)
    else:
        doctor = roster[0] if len(roster) == 1 else None
    if doctor is None or not service_name:
        return insurance_plans_for_service(
            tenant, plans, professionals, service_name, professional_id
        )
    service_id = _doctor_service_id(tenant, doctor, service_name)[1]
    return plans_accepted_for(
        tenant, plans, doctor, service_id, _insurance_unit(doctor, service_id, unit_id)
    )


def _enter_insurance(
    tenant: Tenant,
    conversation: Conversation | None = None,
    *,
    page: int = 0,
    professionals: list | None = None,
    unit_id: Any = None,
    notice: str | None = None,
    with_back: bool = True,
) -> FlowRouterResult:
    """Render the convênio list: the plans the booking accepts + Particular (+ Outro) + back.

    TASK-088 (owner, 2026-11-20): this is the LAST choice before the days. `conversation`
    names the booking in progress - service, doctor and (`unit_id`, else the conversation's
    pick) unit are known, so the list holds ONLY the plans accepted for that combination
    (`insurance_plans_for_booking`). The fixed rows: Particular always; "Outro convênio" only
    when at least one plan is accepted (with none, the owner's rule is "Particular direct" -
    `_insurance_gate` never renders this - and "Outro" would reopen the doubt). The unit rides
    along so the answer lands on the day picker of that unit, and the stored scope says which
    combination the list is for, so the Portal's later pages and taps see the same plans.

    `conversation` is None at the legacy START of a booking (every plan, scope empty) and, with
    no doctor to judge by (an AI draft that landed with the service only), the list keeps the
    older "accepted somewhere for the service" filter. `page` is the "Ver mais convênios" cursor
    (`insurance_page`). `notice` replaces the question line (a convênio that arrived early and
    the combination refuses; a line a caller wants on top).

    Back row (R18): a booking whose service is known ends the list with the way back - the unit
    list when the patient had one to choose from, else the service step he came from. Its
    ("Outra unidade" / "Outro serviço") payload is the day picker's own (`dayback|...`), so the
    Portal card carries it as a pinned row. `with_back=False` is for a caller that is not the
    booking (the reschedule's list, which has no booking step to go back to).
    """
    plans = _tenant_insurance_plans(tenant)
    scope: dict = {}
    service_name = getattr(conversation, "flow_selected_type", None) if conversation else None
    doctor = unit = None
    back_target = BACK_TARGET_SERVICE
    if service_name and conversation is not None:
        doctor = _booking_professional(conversation, professionals)
        if doctor is not None:
            service_id = _doctor_service_id(tenant, doctor, service_name)[1]
            back_target = _back_target_for(flow_units.unit_choice(doctor, service_id))
            unit = _insurance_unit(
                doctor, service_id, unit_id or flow_units.selected_unit_id(conversation)
            )
            plans = plans_accepted_for(tenant, plans, doctor, service_id, unit)
            scope = {"service": service_name, "professional_id": str(doctor.id)}
            if has_exceptions(snapshot_exceptions(tenant)):
                # The matrix is cell-by-cell: later Portal pages need the unit and every
                # doctor's units (`list_options.needs_unit_options`). Without a refused cell
                # the doctor's own list is all that decides, so the scope stays that small.
                scope["unit_marks"] = True
                if unit is not None:
                    scope["unit_id"] = str(unit.id)
        elif professionals and has_exceptions(snapshot_exceptions(tenant)):
            professional_id = _selected_professional_id(conversation)
            plans = insurance_plans_for_service(
                tenant, plans, professionals, service_name, professional_id
            )
            scope = {"service": service_name, "unit_marks": True}
            if professional_id is not None:
                scope["professional_id"] = str(professional_id)
    back_row: list[tuple[str, str]] = (
        [(f"{ROW_DAY_BACK_PREFIX}{back_target}", _day_back_label(back_target))]
        if with_back and service_name and conversation is not None
        else []
    )
    shown, next_page = insurance_page(plans, page, reserved=len(back_row))
    # Title only (TASK-085 R D-R12): the per-plan payment text under each convênio was removed
    # (owner: "TIRE ISSO"); the clinic's own note is still never shown before the choice.
    rows: list[tuple] = [
        (f"ins|{plan['name']}", truncate_list_row_title(str(plan["name"]))) for plan in shown
    ]
    rows.extend(insurance_more_row(next_page))
    rows.append(("ins|particular", LABEL_INSURANCE_PARTICULAR))
    pinned = list(INSURANCE_PINNED_ROWS)
    if plans:
        rows.append(("ins|outro", LABEL_INSURANCE_OTHER))
    else:
        pinned = pinned[:1]
    rows.extend(back_row)
    pinned.extend((row_id, title, None) for row_id, title in back_row)
    # The Portal pages the WHOLE catalogue itself (no "Ver mais convênios" row there).
    source = OptionSource(
        kind="insurance",
        scope=scope,
        items=portal_insurance_rows(plans),
        pinned=pinned,
        has_clinic_notes=insurance_plans_have_clinic_notes(plans),
    )
    logger.info(
        "insurance_step_presented",
        conversation_id=str(getattr(conversation, "id", None)),
        # Count only — WHICH plans a clinic offers is fine to size, but never
        # which one this patient is about to choose.
        plan_count=len(plans),
        booking_started=conversation is None,
        combination_known=doctor is not None,
    )
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=notice or INSURANCE_QUESTION,
                rows=rows,
                button_label="Ver convênios",
                section_title="Convênios",
                source=source,
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_INSURANCE,
        flow_selected_type=conversation.flow_selected_type if conversation else None,
        flow_selected_professional_id=(
            _selected_professional_id(conversation) if conversation else None
        ),
        flow_selected_unit_id=unit.id if unit is not None else None,
    )


def particular_survives(
    tenant: Tenant,
    insurance: str | None,
    *,
    named_now: bool = False,
    recorded: Any | None = None,
    professionals: list | None = None,
    opens_choice: bool = False,
    landing_type: str | None = None,
    landing_owner: Any | None = None,
    landing_unit: Any | None = None,
) -> bool:
    """Keep a chosen Particular. Recheck an automatic one when the combination changes."""
    if (
        not _label_match(insurance, LABEL_INSURANCE_PARTICULAR)
        or named_now
        or not _insurance_is_automatic(recorded)
        or _insurance_step_skip_reason(tenant) is not None
    ):
        return True
    recorded_type = getattr(recorded, "flow_selected_type", None)
    if (
        opens_choice
        or recorded is None
        or not landing_type
        or not recorded_type
        or recorded_type in (ATTENDEE_NEXT_BOOK, ATTENDEE_NEXT_CATALOG)
        or _norm(recorded_type) != _norm(landing_type)
    ):
        return False
    recorded_owner_id = _selected_professional_id(recorded)
    if recorded_owner_id is None:
        recorded_owner_id = getattr(_booking_professional(recorded, professionals), "id", None)
    if recorded_owner_id != getattr(landing_owner, "id", None):
        return False
    # The recorded unit is the one used by the old gate, even if it has since been removed.
    return flow_units.selected_unit_id(recorded) == getattr(landing_unit, "id", None)


def _accepted_plans(
    tenant: Tenant, professional: Any | None, entry: dict | None, unit: Any | None
) -> list:
    """The clinic's plans the booking accepts: (service of `entry`, `professional`, `unit`).

    ONE function for the three that need it - the convênio gate (`_insurance_gate`), the day
    picker's back row (`_day_back_target`) and the AI draft's landing (`booking_draft`) - so the
    question the patient is asked and the way back to it can never disagree. Without a doctor
    there is nobody to judge by: every plan."""
    plans = _tenant_insurance_plans(tenant)
    if professional is None:
        return plans
    return plans_accepted_for(
        tenant, plans, professional, entry_service_id(entry) if entry is not None else None, unit
    )


def _day_back_target(
    tenant: Tenant,
    professional: Any | None,
    entry: dict | None,
    unit: Any | None,
    choice: flow_units.UnitChoice,
) -> str:
    """Where the day and slot pickers' back row goes (R18): the convênio list when the patient
    was asked it in this booking (the clinic collects it and the combination accepts at least
    one plan - otherwise Particular was set without a question), else the unit list when he had
    one to choose from, else the service step."""
    if _insurance_step_skip_reason(tenant) is None and _accepted_plans(
        tenant, professional, entry, unit
    ):
        return BACK_TARGET_INSURANCE
    return _back_target_for(choice)


@dataclass
class _InsuranceGate:
    """What to ask next, or whether to assign Particular and open the days."""

    ask: FlowRouterResult | None = None
    offer_units: FlowRouterResult | None = None
    particular: bool = False
    notice: str | None = None


def _insurance_alternative_units(
    tenant: Tenant, professional: Any, entry: dict, origin: Any
) -> list[tuple[Any, list[dict]]]:
    """Other bookable units for this service and doctor, with their accepted plans."""
    service_id = entry_service_id(entry)
    alternatives = []
    for unit in flow_units.units_for_service(professional, service_id):
        if unit.id == origin.id:
            continue
        plans = _accepted_plans(tenant, professional, entry, unit)
        if plans:
            alternatives.append((unit, plans))
    return alternatives


def _enter_no_plan_at_unit(
    conversation: Conversation, unit: Any, *, refused_known: bool, prefix: str | None = None
) -> FlowRouterResult:
    notice = (
        flow_units.UNIT_INSURANCE_REFUSED if refused_known else flow_units.UNIT_INSURANCE_NONE
    )
    fields = _day_branch_fields(conversation, BOOKING_DAY_BRANCH)
    fields.update(
        flow_selected_unit_id=unit.id, flow_selected_insurance=None, flow_insurance_auto=None
    )
    bubbles = [TextBubble(body=prefix)] if prefix else []
    bubbles.append(
        ButtonBubble(
            body=f"{notice} Você pode ver outras unidades ou seguir como particular aqui.",
            confirm_label=flow_units.LABEL_UNITS_WITH_INSURANCE,
            cancel_label=flow_units.LABEL_CONTINUE_PARTICULAR,
        )
    )
    return FlowRouterResult(
        action="reply",
        bubbles=bubbles,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_NO_PLAN_AT_UNIT,
        insurance_dropped=True,
        **fields,
    )


def _insurance_gate(
    conversation: Conversation,
    tenant: Tenant,
    professionals: list | None,
    professional: Any | None,
    entry: dict | None,
    unit: Any | None,
    prefix: str | None = None,
    unit_asked: bool = False,
) -> _InsuranceGate:
    """The convênio point of the booking: after the unit, before the days (TASK-088 R5/R8).

    Only where the clinic collects it (`_insurance_step_skip_reason`). The combination is
    (service, doctor, unit); the plans it accepts are `plans_accepted_for`.

    * no answer yet: some accepted -> the filtered list; NONE accepted -> offer other units
      if they accept plans, otherwise Particular with a short line before the days;
    * an answer already there (an AI draft, a parked chat): Particular, a typed plan or a
      plan the combination accepts is kept and the days open; a plan it refuses is NEVER
      swapped for Particular in silence - the filtered list with a line on top, or, with
      nothing else accepted, Particular saying so (`INSURANCE_REFUSED_PARTICULAR*`).

    Logs counts and booleans only - never a plan's name.
    """
    reason = _insurance_step_skip_reason(tenant)
    if reason is not None:
        logger.info(
            "insurance_step_skipped",
            reason=reason,
            conversation_id=str(getattr(conversation, "id", None)),
            professional_count=len(professionals or []),
        )
        return _InsuranceGate()
    known = _selected_insurance(conversation)
    accepted = _accepted_plans(tenant, professional, entry, unit)
    refused_known = False
    # Only describe the absence of confirmed plans at a unit the patient actually chose.
    where_unit = unit is not None and unit_asked
    if known:
        plan = _selected_plan(tenant, known)
        if plan is None:
            # Unlisted plans are kept on the ordinary path. At a chosen unit
            # without confirmed plans, offer the alternatives without claiming
            # that the clinic explicitly refused the patient's unlisted plan.
            if accepted or not where_unit or _label_match(known, LABEL_INSURANCE_PARTICULAR):
                return _InsuranceGate()
        elif any(str(each.get("id")) == str(plan["id"]) for each in accepted):
            return _InsuranceGate()
        else:
            refused_known = True
    if not accepted:
        if where_unit and entry is not None and (alternatives := _insurance_alternative_units(
            tenant, professional, entry, unit
        )):
            logger.info("insurance_step_units_offered", other_units_with_plans=len(alternatives))
            return _InsuranceGate(
                offer_units=_enter_no_plan_at_unit(
                    conversation, unit, refused_known=refused_known, prefix=prefix
                )
            )
        logger.info(
            "insurance_step_particular_direct",
            conversation_id=str(getattr(conversation, "id", None)),
            refused_known=refused_known,
        )
        if refused_known:
            line = INSURANCE_REFUSED_PARTICULAR_UNIT if where_unit else INSURANCE_REFUSED_PARTICULAR
        else:
            line = INSURANCE_NONE_ACCEPTED_UNIT if where_unit else INSURANCE_NONE_ACCEPTED
        return _InsuranceGate(particular=True, notice=line)
    question = INSURANCE_QUESTION
    if refused_known:
        question = INSURANCE_REFUSED_LIST_UNIT if where_unit else INSURANCE_REFUSED_LIST
    notice = f"{prefix} {question}" if prefix else (question if refused_known else None)
    ask = _enter_insurance(
        tenant,
        conversation,
        professionals=professionals,
        unit_id=unit.id if unit is not None else None,
        notice=notice,
    )
    return _InsuranceGate(ask=ask)


async def _handle_insurance(
    conversation: Conversation,
    tenant: Tenant,
    body: str,
    calendar: CalendarService | None = None,
    services: list[dict] | None = None,
    professionals: list | None = None,
) -> FlowRouterResult:
    """Record the convênio answer, then continue the booking.

    Tapping "Outro convênio" asks for the plan's name and stays on this step;
    a listed plan's tap, "Particular" ("não tenho" counts as Particular), or a
    typed plan name is stored as-is (canonicalized to the full plan name when it
    matches one) and copied onto the appointment at booking time. A question or
    a sentence is NOT a plan name: it gets one model turn on this same step
    (owner, 2026-10-07, TASK-038 - "qual o endereço?" used to be stored here).

    What comes next (TASK-088): the convênio is the LAST choice, so the service (and the
    doctor, and the unit) are known and the answer opens the days of that unit - `_ask_day`
    checks the answer against the combination once more. Only a booking that does not have
    its service yet (a chat parked on the retired first question, an AI draft that asked the
    convênio before anything else) keeps the answer and goes on from where it is: the doctor's
    own services when the doctor is already chosen, the doctors of the chosen service when only
    the service is, else the service-first entry (`_booking_entry`: the clinic catalog, or the
    sole doctor's list) - and the answer is judged later, at the day step.
    """
    more = insurance_more_page(body)
    if more is not None:
        return _enter_insurance(tenant, conversation, page=more, professionals=professionals)
    if _label_match(body, LABEL_INSURANCE_OTHER):
        return FlowRouterResult(
            action="reply",
            bubbles=[TextBubble(body=INSURANCE_PROMPT_OTHER)],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_AWAITING_INSURANCE,
            flow_selected_type=conversation.flow_selected_type,
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_unit_id=flow_units.selected_unit_id(conversation),
        )
    matched = _match_insurance_plan(tenant, body)
    if matched is None and _says_no_insurance(body):
        matched = LABEL_INSURANCE_PARTICULAR
    if matched is None and _reads_as_conversation(body):
        logger.info(
            "insurance_free_text_to_llm",
            conversation_id=str(getattr(conversation, "id", None)),
        )
        return _preserve(conversation, "delegate_llm")
    stored = (matched or (body or "").strip())[:120] or None
    if stored is None:
        # No text at all (e.g. a media message): re-ask here, rather than walk
        # on with no answer and ask the same question again at "Sim, agendar".
        if conversation.flow_selected_type:
            # TASK-088: the same decision as the first time (`_insurance_gate`) - the list, or
            # Particular with its line when the combination now accepts nothing - never a list
            # that holds only Particular.
            return await _ask_day(
                conversation, tenant, calendar, services, professionals, reask_insurance=True
            )
        return _enter_insurance(tenant, conversation, professionals=professionals)
    logger.info(
        "insurance_selected",
        conversation_id=str(getattr(conversation, "id", None)),
        # NEVER the plan itself (it identifies a patient's coverage): only
        # whether it came from the clinic's own catalog or was typed free.
        from_catalog=matched is not None,
        stored=stored is not None,
    )
    result = None
    selected = _find_professional_by_id(professionals, _selected_professional_id(conversation))
    multi = _is_multi_professional(professionals)
    if not conversation.flow_selected_type and multi and selected is not None:
        # The doctor is already chosen (an AI draft landed here with the doctor in hand,
        # TASK-030): their own services next - never the doctor list again. A doctor with
        # nothing configured ends in the clinic alert, returned as-is.
        result = _enter_professional_services(selected, tenant, insurance=stored)
    elif conversation.flow_selected_type and multi and selected is None:
        # The service is known but not the doctor (a chat parked by an older AI draft whose
        # service two or more doctors offer): the doctors of THAT service, the new order's own
        # second step (TASK-088), with the service kept.
        result = _enter_service_professional_list(
            tenant,
            professionals or [],
            str(conversation.flow_selected_type),
            insurance=stored,
        )
    elif not conversation.flow_selected_type:
        # A chat parked on the retired first question (no service yet): the answer is kept
        # and judged later, at the day step; the booking itself takes the NEW order - the
        # service first (SPEC A: "cai no fluxo novo na próxima escolha").
        result = _booking_entry(None, tenant, professionals)
        if result.flow_state != FlowState.SERVICE_CATALOG and not multi:
            # The button flow never asks the convênio in front of a clinic with
            # no services (`_booking_continuation`), so a dead end here means a
            # typeless LLM hand-back (`enter_guided_booking` with no catalog) on a clinic
            # with ONE doctor: it books typeless, straight to the day, exactly as it did
            # before the convênio moved to the front. With several doctors there is nobody to
            # book with and no service: the "no services" answer and the menu, as is.
            result = None
    if result is None:
        state = _DayPickerState(
            id=getattr(conversation, "id", None),
            flow_selected_type=conversation.flow_selected_type,
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=stored,
            flow_insurance_auto=False,
            flow_attendee_name=_attendee_name(conversation),
            # The unit chosen BEFORE this question (the new order) - the days are its days.
            flow_selected_unit_id=flow_units.selected_unit_id(conversation),
        )
        result = await _ask_day(
            state, tenant, calendar, services, professionals, announce_unit=True
        )
    if (
        result.flow_state is FlowState.SERVICE_CATALOG
        and result.flow_selected_insurance is None
        and result.flow_step != STEP_AWAITING_INSURANCE
    ):
        # Only a result still inside the booking carries it; a dead end (no services, no
        # availability) resets on purpose and must not resurrect it. A day step that already
        # named the answer (Particular, when the plan is not accepted there) or asked it again
        # (the refused plan was dropped on purpose) keeps its own.
        result.flow_selected_insurance = stored
    if result.flow_selected_insurance is not None and result.flow_insurance_auto is None:
        result.flow_insurance_auto = False
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
# exactly like a tap would; anything unresolvable hands to the LLM with the
# booking draft intact (the agent decides whether a human is needed via
# ai/tools.py::request_human_handoff). An LLM/network failure degrades to
# the general agent (delegate_llm), same as any other unhandled turn.


def _enter_professional_help(conversation: Conversation) -> FlowRouterResult:
    """Reply with the professional-scope opener; the LLM runs on the answer."""
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=PROFESSIONAL_HELP_OPENER)],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_PROFESSIONAL_HELP,
        flow_selected_insurance=_selected_insurance(conversation),
        flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
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
        flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
    )


# Same cap as a hand-back tool's words (ai/tools.py::HANDBACK_MESSAGE_MAX).
_PICK_MESSAGE_MAX = 600


def _with_pick_message(result: FlowRouterResult, message: str | None) -> FlowRouterResult:
    """The help node's one-line reason, in front of the picked option's card (TASK-038).

    Without it the patient who explained what they need saw a list or card with no word
    about it. Model prose, so it passes the same unbacked-claim check as any LLM reply;
    a sentence that check would rewrite is dropped - the card alone still answers.
    """
    text = (message or "").strip()[:_PICK_MESSAGE_MAX].rstrip()
    if text and result.action == "reply" and unbacked_claim(text) is None:
        result.bubbles = [TextBubble(body=text), *result.bubbles]
    return result


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
            return _with_pick_message(
                _enter_professional_services(
                    professional, tenant, insurance=_selected_insurance(conversation)
                ),
                outcome.message,
            )
        # A pick that doesn't resolve against the real roster (hallucinated /
        # deactivated mid-exchange) must never be offered back to the patient.
        logger.warning("flow_professional_help_pick_unresolved")
        return _delegate_llm_keeping_draft(conversation)
    if outcome.kind == "clarify" and not final_round:
        return FlowRouterResult(
            action="reply",
            bubbles=[TextBubble(body=outcome.question or "")],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_PROFESSIONAL_HELP_FINAL,
            flow_selected_insurance=_selected_insurance(conversation),
            flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
        )
    return _delegate_llm_keeping_draft(conversation)


async def _handle_service_help(
    conversation: Conversation,
    tenant: Tenant,
    body: str,
    services: list[dict],
    *,
    calendar: CalendarService | None = None,
    professionals: list | None = None,
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
            # TASK-088 R38 ("valor só no fim"): the model gets NO price - its one-line reason and
            # its clarifying question reach the patient in the middle of the flow, and a convênio
            # patient must never be told the Particular value. Money is said only on the final
            # confirmation card.
            services=[{**service, "price": None} for service in services],
            patient_message=body,
            final_round=final_round,
        )
    except Exception as exc:
        logger.warning("flow_service_help_failed", error=str(exc))
        return _preserve(conversation, "delegate_llm")
    if outcome.kind == "pick":
        service = _match_service(services, outcome.choice)
        if service is not None:
            # TASK-088: the validated pick goes straight on - no service card.
            return _with_pick_message(
                await _continue_with_service(
                    conversation,
                    tenant,
                    calendar,
                    service,
                    professionals,
                    services=services,
                ),
                outcome.message,
            )
        logger.warning("flow_service_help_pick_unresolved")
        return _delegate_llm_keeping_draft(conversation)
    if outcome.kind == "clarify" and not final_round:
        return FlowRouterResult(
            action="reply",
            bubbles=[TextBubble(body=outcome.question or "")],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_SERVICE_HELP_FINAL,
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=_selected_insurance(conversation),
            flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
        )
    return _delegate_llm_keeping_draft(conversation)


async def _catalog_step(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    patient_name: str | None,
    professionals: list | None = None,
    gate: BookingGate | None = None,
) -> FlowRouterResult:
    step = conversation.flow_step

    if step in ATTENDEE_STEPS:
        return _attendee_step(conversation, tenant, body, professionals)

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
    # TASK-083 spec §8: from the unit on, every read and write is the chosen unit's.
    booking_owner = _booking_professional(conversation, professionals)
    unscoped_calendar = calendar
    # Follow-up §6.1: only the units that do the booking's service count, everywhere below.
    service_id = _booking_service_id(conversation, tenant, services, booking_owner)
    # TASK-088: the units are the service's, whatever the convênio (`_ask_day` asks the same).
    unit_choice = flow_units.unit_choice(booking_owner, service_id)
    booking_unit = flow_units.effective_unit(conversation, booking_owner, service_id)
    # TASK-087: every list below is also without the service's blocked days at that unit, and a
    # blocked day typed on the day step is refused in plain words (`day_check`).
    service_entry = _booking_service_entry(conversation, tenant, services, booking_owner)
    calendar = _service_scoped(calendar, booking_owner, service_entry, booking_unit)
    day_check = _service_day_check(booking_owner, service_entry, booking_unit)
    back_target = _day_back_target(tenant, booking_owner, service_entry, booking_unit, unit_choice)

    if step == STEP_AWAITING_NO_PLAN_AT_UNIT:
        return await _handle_unit_insurance_choice(
            conversation, tenant, unscoped_calendar, body, services, professionals
        )
    if step == flow_units.STEP_AWAITING_UNIT:
        return await _handle_unit_step(
            conversation, tenant, unscoped_calendar, body, services, professionals
        )
    if step == flow_units.STEP_AWAITING_UNIT_CONFIRM:
        return await _handle_unit_confirm_step(
            conversation, tenant, calendar, body, services, professionals
        )
    picked = flow_units.find_unit(
        booking_owner, flow_units.selected_unit_id(conversation), service_id
    )
    picked_gone = flow_units.selected_unit_id(conversation) is not None and picked is None
    automatic_unit_changed = _automatic_insurance_unit_changed(conversation, booking_unit)
    if (
        step in UNIT_KEEP_STEPS
        and unit_choice.kind in ("list", "confirm")
        and picked is None
        and not automatic_unit_changed
    ):
        # Never a day, time or booking without the unit (a link added mid-booking, a unit
        # deactivated, a hand-back that skipped the question, a unit picked for another
        # service): ask it first.
        return _enter_unit_choice(conversation, booking_owner, unit_choice)
    if (
        step in UNIT_KEEP_STEPS
        and (
            automatic_unit_changed
            or (
                (picked_gone or unit_choice.kind == "none")
                and flow_units.bookable_units(booking_owner)
            )
        )
    ):
        # (`unit_choice` above is asked without the convênio - the unit comes first since
        # TASK-088 - so a "none" here is never `by_plan`: it really is a unit gone.)
        # Final wave M1: the picked unit is gone and ONE is left. Its day and time were chosen
        # within the gone unit's hours, so never carry them over silently: the days again,
        # within the unit left (`_ask_day` records it as the pick). No unit left for the
        # service: `_ask_day` says so ("0 unidades").
        return await _ask_day(
            conversation,
            tenant,
            unscoped_calendar,
            services,
            professionals,
            prefix=flow_units.UNIT_GONE_NOTICE if picked_gone else None,
        )

    if step == STEP_AWAITING_PROFESSIONAL:
        roster = professionals or []
        more = catalog_more_page(body, LABEL_MORE_PROFESSIONALS)
        if more is not None:
            # The whole roster, as the first page listed it (a rebooking pool's later
            # pages list the roster too - the tap is matched against it anyway).
            return _preserve_reply(
                conversation,
                _enter_professional_list(
                    tenant,
                    roster,
                    insurance=_selected_insurance(conversation),
                    page=more,
                    service_name=conversation.flow_selected_type,
                ).bubbles,
            )
        if _label_match(body, LABEL_DONT_KNOW):
            return _enter_professional_help(conversation)
        professional = _match_professional(roster, body)
        if professional is None:
            return _preserve(conversation, "delegate_llm")
        if conversation.flow_selected_type:
            # A service may already be chosen when an empty LLM handback
            # reopens the professional list. Recheck the doctor's catalogue
            # before continuing with the selected service.
            return _resume_parked_draft(
                conversation,
                await _handle_service_professional(
                    conversation, tenant, body, roster, unscoped_calendar
                ),
            )
        return _resume_parked_draft(
            conversation,
            _enter_professional_services(
                professional, tenant, insurance=_selected_insurance(conversation)
            ),
        )

    if step == STEP_AWAITING_PLAN_REFUSED:
        if _label_match(body, LABEL_PLAN_CHANGE_INSURANCE):
            # The convênio asked again for what is booked so far: the unit (cleared by the card)
            # first, then the list - or Particular with its line, by the one gate (`_ask_day`).
            return await _ask_day(
                _DayPickerState(
                    id=getattr(conversation, "id", None),
                    flow_selected_type=conversation.flow_selected_type,
                    flow_selected_professional_id=_selected_professional_id(conversation),
                    flow_attendee_name=_attendee_name(conversation),
                ),
                tenant,
                calendar,
                services,
                professionals,
            )
        return _handle_plan_refused(conversation, tenant, body, professionals)

    if step == STEP_AWAITING_CATALOG_SERVICE:
        # TASK-088: the clinic catalog and the doctors of a service are the AI draft's questions
        # too - the answer resumes a parked draft like the service / doctor taps do.
        return _resume_parked_draft(
            conversation,
            await _handle_catalog_service(
                conversation, tenant, body, professionals or [], unscoped_calendar
            ),
        )

    if step == STEP_AWAITING_SERVICE_PROFESSIONAL:
        return _resume_parked_draft(
            conversation,
            await _handle_service_professional(
                conversation, tenant, body, professionals or [], unscoped_calendar
            ),
        )

    if step in (STEP_PROFESSIONAL_HELP, STEP_PROFESSIONAL_HELP_FINAL):
        return await _handle_professional_help(conversation, tenant, body, professionals or [])

    if step == STEP_AWAITING_SERVICE:
        scope = service_scope(_selected_professional_id(conversation))
        more = catalog_more_page(body, LABEL_MORE_SERVICES)
        if more is not None:
            return _preserve_reply(
                conversation,
                [
                    _service_list_bubble(
                        tenant,
                        services,
                        scope=scope,
                        page=more,
                        professional=booking_owner,
                        insurance=_selected_insurance(conversation),
                    )
                ],
            )
        if _label_match(body, LABEL_DONT_KNOW):
            return _enter_service_help(conversation)
        service = _match_service(services, body)
        if service is None:
            return _preserve(conversation, "delegate_llm")
        refused_service_id = _matrix_refused_service(
            tenant, booking_owner, service, _selected_insurance(conversation)
        )
        if refused_service_id is not None:
            # TASK-086 A4: a "não atende este convênio" row was tapped - its ways on, not its card.
            return _enter_plan_refused(
                conversation,
                tenant,
                professionals,
                booking_owner,
                str(service.get("name", "")),
                refused_service_id,
            )
        # TASK-088: no service card - the pick goes straight to the next step.
        return _resume_parked_draft(
            conversation,
            await _continue_with_service(
                conversation,
                tenant,
                unscoped_calendar,
                service,
                professionals,
                services=services,
            ),
        )

    if step in (STEP_SERVICE_HELP, STEP_SERVICE_HELP_FINAL):
        return await _handle_service_help(
            conversation,
            tenant,
            body,
            services,
            calendar=unscoped_calendar,
            professionals=professionals,
        )

    if step == STEP_AWAITING_SERVICE_CONFIRM:
        if _norm(body) == _norm(LABEL_BOOK_SERVICE):
            # TASK-088: the convênio is no longer asked here - it comes after the unit, from
            # the day step's own choke point, where service, doctor and unit are known
            # (`_ask_day` -> `_insurance_gate`), for every path that reaches the days.
            return await _ask_day(conversation, tenant, calendar, services, professionals)
        if _norm(body) == _norm(LABEL_OTHER_SERVICE):
            return FlowRouterResult(
                action="reply",
                bubbles=[
                    _service_list_bubble(
                        tenant,
                        services,
                        scope=service_scope(_selected_professional_id(conversation)),
                        professional=booking_owner,
                        insurance=_selected_insurance(conversation),
                    )
                ],
                flow_state=FlowState.SERVICE_CATALOG,
                flow_step=STEP_AWAITING_SERVICE,
                flow_selected_professional_id=_selected_professional_id(conversation),
            )
        return _preserve(conversation, "delegate_llm")

    if step == STEP_AWAITING_INSURANCE:
        if _is_day_back(body, BACK_TARGET_SERVICE):
            # The list's back row (R18): one step back, never an "answer" - so a parked AI draft
            # is NOT resumed (it would land on this very question again).
            return _handle_day_back(
                conversation, tenant, services, professionals, _back_target_of(body)
            )
        answered = await _handle_insurance(
            conversation, tenant, body, calendar, services, professionals
        )
        return _resume_parked_draft(conversation, answered)

    if step in _BOOKING_DAY_STEPS:
        return await _handle_day_step(
            conversation,
            tenant,
            calendar,
            body,
            duration_minutes=_booking_duration(conversation, tenant, services, professionals),
            branch=BOOKING_DAY_BRANCH,
            services=services,
            back_target=back_target,
            professionals=professionals,
            refuse_day=day_check,
        )

    if step == STEP_AWAITING_SLOT:
        control = await _handle_slot_controls(
            conversation,
            tenant,
            calendar,
            body,
            duration_minutes=_booking_duration(conversation, tenant, services, professionals),
            branch=BOOKING_DAY_BRANCH,
            services=services,
            back_target=back_target,
            professionals=professionals,
        )
        if control is not None:
            return control
        return _handle_slot(
            conversation,
            body,
            professional=_recap_professional(conversation, tenant, professionals),
            unit=booking_unit,
            money_lines=_money_lines(
                booking_price_line(conversation, service_entry, booking_owner, booking_unit)
            ),
        )

    if step == STEP_AWAITING_CONFIRMATION:
        return await _handle_confirmation(
            conversation, tenant, calendar, body, patient_name, services, professionals, gate
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
    """A day-picker row title: "🗓️ Seg, 18/08" (WhatsApp caps titles at 24).

    Unconditionally decorated, unlike a service row: the format is fixed at 10
    characters, so 13 with the calendar emoji is nowhere near the cap, and the
    row's identity rides in its `day|` id rather than in this text.
    """
    return decorate(EMOJI_SCHEDULE, f"{_WEEKDAY_SHORT_PT[day.weekday()]}, {day.strftime('%d/%m')}")


_ROW_PAYLOAD_RE = re.compile(r"\s*\(([^)]*)\)\s*$")


def _row_payload(body: str | None) -> str | None:
    """The "(payload)" a data-carrying row tap appends to its title, or None.

    Mirrors `_slot_iso_from_body`/`_professional_id_from_body`, but returns
    None (rather than the whole body) when there are no trailing parentheses,
    so a TYPED "Escolher outro Serviço" is distinguishable from a tapped one.
    """
    if not body:
        return None
    match = _ROW_PAYLOAD_RE.search(body)
    return match.group(1).strip() if match else None


def _control_match(body: str | None, label: str) -> bool:
    """`_label_match`, but for the picker's payload-carrying control rows.

    "Escolher outro Serviço" arrives as "Escolher outro Serviço (service)"
    and "Ver mais dias" as "Ver mais dias (2)" — the cursor/destination rides
    in the row id precisely so the picker needs no stored state. Matching
    therefore has to look past that suffix; a typed "Escolher outro Serviço"
    (no suffix) still matches the plain form. Scoped to these fixed labels,
    never used for free text.
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


def _more_days_cursor(payload: str | None) -> tuple[int, bool]:
    """A "Ver mais dias" payload as (page, still in the long scan)."""
    page, _, scan = (payload or "").partition("|")
    return _as_page(page), scan == DAY_CURSOR_CALENDAR


def _day_branch_fields(conversation: Conversation, branch: DayBranch) -> dict:
    """The flow-state fields THIS branch must carry through the day/slot steps.

    Every FlowRouterResult field is written unconditionally by the caller, so
    a result that does not name a field clears it. The booking branch must
    keep the service/doctor/convênio it has already collected; the manage
    branch must keep the appointment it is rescheduling.
    """
    if branch.flow_state is FlowState.EDIT_BOOKING:
        return {
            "flow_managing_appointment_id": _selected_managing_appointment_id(conversation),
            "flow_selected_professional_id": _selected_professional_id(conversation),
            "flow_edit_draft": getattr(conversation, "flow_edit_draft", None),
        }
    if branch.flow_state is FlowState.MANAGE_BOOKING:
        return {
            "flow_managing_appointment_id": _selected_managing_appointment_id(conversation)
        }
    return {
        "flow_selected_type": conversation.flow_selected_type,
        "flow_selected_professional_id": _selected_professional_id(conversation),
        "flow_selected_insurance": _selected_insurance(conversation),
        "flow_insurance_auto": getattr(conversation, "flow_insurance_auto", None),
        "flow_selected_unit_id": flow_units.selected_unit_id(conversation),
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
    anchor: datetime | None = None,
    calendar_view: bool = False,
    buffer_rule: BufferRule | None = None,
) -> FlowRouterResult:
    """Render the tappable day list: only days that actually have a free slot.

    The doctor's buffer between consultations (TASK-088 section B) applies on every branch: a
    day whose only free times sit inside it is not offered. `buffer_rule` says whose buffer and
    which booking is being moved when the caller knows better than the conversation
    (`_picker_buffer`).

    `anchor` centres the scan on the current appointment, showing the nearest days.

    `calendar_view` is the "Outra data" screen: the scan reaches DAY_CALENDAR_WINDOW_DAYS from
    today, under the same rules as the short list (the doctor's hours, calendar and minimum
    notice). The bubble carries every free day in `calendar_days` for a channel that draws a
    calendar; its rows are the same days paged for a channel that does not. A short window with
    nothing free opens this screen by itself instead of saying the agenda is full.

    Costs ONE calendar call for the whole window
    (`CalendarService.list_available_days`), never one per day. Rows:

        <= DAY_PICKER_PAGE_SIZE(8) day rows
        + "Outra data"               always, on the short list
        + "Ver mais dias"            on the long scan, when it holds more
        + the back-control row       when `back_target` is set — labelled via
                                      `_day_back_label` ("Escolher outro
                                      Serviço" for the service destination)
          replaced by "Outro"        once the free-text escape is being offered

    which is at most MAX_LIST_ROWS(10) in every combination — the API's hard
    cap, and the reason the original "17 dias úteis + Outro" idea could not be
    built as one screen. `step` overrides the step written (the retry/escape
    renders reuse this function verbatim), `prefix` prepends one short line.
    """
    if anchor is None and not calendar_view and branch.flow_state is FlowState.EDIT_BOOKING:
        raw = getattr(conversation, "flow_edit_draft", None)
        if isinstance(raw, dict):
            value = (raw.get("current") or {}).get("start_at")
            if isinstance(value, str):
                try:
                    anchor = datetime.fromisoformat(value)
                except ValueError:
                    pass
    step = step or branch.day_step
    if calendar is None:
        return _calendar_unavailable(conversation, branch, step)
    # TASK-052 (owner decision 2026-10-11): a day whose free slots are held by another
    # conversation (an unpaid Pix keeps one for 30-120 minutes) IS offered, as on main; the
    # slot step hides the held times and says which they are (`_enter_slot_picker`).
    try:
        start = datetime.now(calendar.tzinfo)
        if anchor is not None:
            anchor_aware = anchor if anchor.tzinfo else anchor.replace(tzinfo=calendar.tzinfo)
            start = max(start, anchor_aware - timedelta(days=DAY_PICKER_WINDOW_DAYS // 2))
        window_days = DAY_CALENDAR_WINDOW_DAYS if calendar_view else DAY_PICKER_WINDOW_DAYS
        buffer_minutes, moved_event_id = _picker_buffer(
            conversation, professionals, branch, buffer_rule
        )
        # The booking being moved never pads itself: the consultation windows are read once for
        # the whole window WITHOUT it. A new booking leaves the read to the availability layer.
        consultations = (
            await offer_consultation_windows(
                calendar,
                start,
                start + timedelta(days=window_days),
                buffer_minutes=buffer_minutes,
                ignore_event_id=moved_event_id,
            )
            if buffer_minutes > 0 and moved_event_id
            else None
        )
        days = await available_day_starts(
            calendar,
            start=start,
            window_days=window_days,
            duration_minutes=duration_minutes,
            not_before=_booking_floor(conversation, professionals, branch, calendar.tzinfo),
            buffer_minutes=buffer_minutes,
            consultations=consultations,
        )
        if anchor is not None:
            # The days NEAREST the current one come first (ties: the earlier day), so
            # pagination ("Ver mais dias") walks outwards; the page itself is shown in
            # calendar order.
            anchor_day = anchor.date()
            days = sorted(days, key=lambda day: (abs((day.date() - anchor_day).days), day))
    except CalendarUnavailableError:
        return _calendar_unavailable(conversation, branch, step)

    if not days and not calendar_view:
        # Nothing soon is not "nothing": a booked-up doctor may have room further ahead.
        return await enter_day_picker(
            conversation,
            tenant,
            calendar,
            duration_minutes=duration_minutes,
            branch=branch,
            back_target=back_target,
            prefix=prefix,
            step=step,
            professionals=professionals,
            calendar_view=True,
            buffer_rule=buffer_rule,
        )

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
    if anchor is not None:
        shown = sorted(shown)
    rows: list[tuple[str, str]] = [
        (f"{ROW_DAY_PREFIX}{day.date().isoformat()}|{page}", _day_row_label(day))
        for day in shown
    ]
    if not calendar_view:
        rows.append((f"{ROW_DAY_CALENDAR_PREFIX}{page + 1}", LABEL_OTHER_DATE))
    elif len(days) > offset + DAY_PICKER_PAGE_SIZE:
        rows.append(
            (f"{ROW_DAY_MORE_PREFIX}{page + 1}|{DAY_CURSOR_CALENDAR}", LABEL_MORE_DAYS)
        )
    escape = step == branch.day_escape_step
    if escape:
        # The escape row takes the last control slot, displacing the
        # back-control row: once we have failed to read the patient twice,
        # getting them to a human matters more than one step back. Its id
        # deliberately carries NO payload prefix so the tap arrives as the
        # plain "Outro" label.
        rows.append(("dayescape|0", LABEL_OTHER))
    elif back_target:
        rows.append((f"{ROW_DAY_BACK_PREFIX}{back_target}", _day_back_label(back_target)))

    day_body = DAY_CALENDAR_BODY if calendar_view else branch.day_body
    body = f"{prefix} {day_body}" if prefix else day_body
    if escape:
        body = f"{body}\n\n{DAY_ESCAPE_HINT}"
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=body[:MAX_LIST_BODY_CHARS],
                rows=rows[:MAX_LIST_ROWS],
                button_label="Ver datas" if calendar_view else "Ver dias",
                section_title="Datas disponíveis" if calendar_view else "Dias disponíveis",
                calendar_days=[day.date().isoformat() for day in days] if calendar_view else [],
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
    buffer_rule: BufferRule | None = None,
) -> FlowRouterResult:
    """Step 2 of the branch: the free times on `target`, plus its two exits.

    The doctor's buffer (TASK-088 section B, `_picker_buffer`) drops every time that sits closer
    than N minutes to a consultation or to another conversation's held time; the booking being
    moved never pads itself.

    A day that comes back empty was free when the picker was built and filled
    in between — so the deterministic answer is the picker again, refreshed
    and prefixed, not a text message asking the patient to try another day.
    """
    if calendar is None:
        return _calendar_unavailable(conversation, branch, branch.day_step)
    reserved = await _hold_windows(_hold_owner(conversation, professionals))
    floor = _booking_floor(conversation, professionals, branch, calendar.tzinfo)
    buffer_minutes, moved_event_id = _picker_buffer(
        conversation, professionals, branch, buffer_rule
    )
    try:
        slots = await calendar.list_free_slots(
            day=target,
            slot_minutes=duration_minutes,
            max_slots=(
                slot_scan_limit(duration_minutes)
                if reserved or floor is not None or buffer_minutes > 0
                else SLOT_PICKER_MAX_SLOTS
            ),
        )
    except CalendarUnavailableError:
        return _calendar_unavailable(conversation, branch, branch.day_step)

    # The doctor's buffer around his consultations (the whole day was read, so the times
    # after the padding still fill the page). Held times are padded below, after "held" labels.
    if buffer_minutes > 0:
        day_start = target if target.tzinfo else target.replace(tzinfo=calendar.tzinfo)
        consultations = await offer_consultation_windows(
            calendar,
            day_start,
            day_start + timedelta(days=1),
            buffer_minutes=buffer_minutes,
            ignore_event_id=moved_event_id,
        )
        slots = without_holds(
            slots,
            padded(consultations, buffer_minutes),
            duration_minutes=duration_minutes,
            tz=calendar.tzinfo,
        )

    # The doctor's minimum notice: a slot starting too soon is not offered at all. The
    # whole day was read, so the times after the floor still fill the page.
    if floor is not None:
        slots = at_or_after(slots, floor, calendar.tzinfo)
        if not reserved and buffer_minutes <= 0:
            slots = slots[:SLOT_PICKER_MAX_SLOTS]

    # A slot another conversation is HOLDING is not free, even though Google
    # has never heard of it: between "Confirmar" and a verified code (or a paid Pix) the
    # booking exists only in `booking_holds`. Applied on EVERY channel and on
    # both branches (first booking and reschedule) - a reservation half the
    # surfaces ignore is not a reservation. The patient's own hold is excluded
    # upstream, so their own choice stays visible to them.
    held: list[dict] = []
    if reserved:
        before = len(slots)
        held = held_slots(slots, reserved, duration_minutes=duration_minutes, tz=calendar.tzinfo)
        slots = without_holds(
            slots, reserved, duration_minutes=duration_minutes, tz=calendar.tzinfo
        )
        if len(slots) != before:
            logger.info(
                "flow_slots_hidden_by_hold",
                hidden=before - len(slots),
                conversation_id=str(getattr(conversation, "id", None)),
            )
        if buffer_minutes > 0:
            # A held time becomes a consultation: its neighbours are inside the buffer too (not
            # "held" - they are simply not offered).
            slots = without_holds(
                slots,
                padded(reserved, buffer_minutes),
                duration_minutes=duration_minutes,
                tz=calendar.tzinfo,
            )
        # Reserve the final two rows for the day and service navigation controls.
        slots = slots[:SLOT_PICKER_MAX_SLOTS]
    elif buffer_minutes > 0:
        slots = slots[:SLOT_PICKER_MAX_SLOTS]

    held_labels = [str(s.get("label") or "") for s in held if s.get("label")]
    if not slots and held_labels:
        # TASK-052: every free time of the day is held by someone else. The day stays offered
        # (it may free up), so going back to the day list would loop: say why, offer the day
        # list and the back control, nothing to tap on this day.
        controls: list[tuple[str, str]] = [(f"{ROW_DAY_AGAIN_PREFIX}{page}", LABEL_ANOTHER_DAY)]
        if back_target:
            controls.append((f"{ROW_DAY_BACK_PREFIX}{back_target}", _day_back_label(back_target)))
        return FlowRouterResult(
            action="reply",
            bubbles=[
                SlotsBubble(
                    body=ALL_HELD_NOTICE.format(times=held_times_text(held_labels))[
                        :MAX_LIST_BODY_CHARS
                    ],
                    rows=controls[:MAX_LIST_ROWS],
                    button_label="Ver opções",
                    section_title="Opções",
                )
            ],
            flow_state=branch.flow_state,
            flow_step=branch.slot_step,
            flow_selected_day=target.date().isoformat(),
            **_day_branch_fields(conversation, branch),
        )

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
            buffer_rule=buffer_rule,
        )

    # `s["label"]` is CalendarService's fixed "%H:%M" (5 chars), so the calendar
    # emoji is free here; the slot's identity is the ISO in the `slot|` id.
    rows: list[tuple[str, str]] = [
        (f"slot|{s['start']}", decorate(EMOJI_SCHEDULE, s["label"])) for s in slots
    ]
    rows.append((f"{ROW_DAY_AGAIN_PREFIX}{page}", LABEL_ANOTHER_DAY))
    if back_target:
        rows.append((f"{ROW_DAY_BACK_PREFIX}{back_target}", _day_back_label(back_target)))
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=(
                    f"{HELD_SLOTS_NOTICE.format(times=held_times_text(held_labels))}\n\n"
                    if held_labels
                    else ""
                )
                + f"{branch.slot_body_prefix} {short_day_phrase(target)}:",
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
    """The picker's back-control row: one step back, with every earlier
    answer preserved.

    "service" re-renders the service list, "professional" the doctor list, "unit" the unit
    list, "insurance" the convênio list (TASK-088, R18); anything else (or an unusable target)
    falls back to the menu. Whatever the
    destination, the service / doctor / convênio already chosen ride along —
    going back must never silently erase them. The one exception (TASK-088): the
    AUTOMATIC Particular (nothing was accepted at the unit/service being left,
    `_insurance_is_automatic`) is dropped, so the unit or service picked next is judged
    afresh - a plan the patient chose himself still rides along and is re-judged there.
    """
    automatic = _insurance_is_automatic(conversation)
    result = _day_back_result(conversation, tenant, services, professionals, target)
    if automatic and result.flow_state is FlowState.SERVICE_CATALOG:
        result.flow_selected_insurance = None
        result.flow_insurance_auto = None
        result.insurance_dropped = True
    return result


def _insurance_is_automatic(conversation: Any | None) -> bool:
    """Only an explicitly marked Particular was assigned by the gate."""
    return getattr(conversation, "flow_insurance_auto", None) is True


def _automatic_insurance_unit_changed(conversation: Conversation, unit: Any | None) -> bool:
    return (
        _insurance_is_automatic(conversation)
        and flow_units.selected_unit_id(conversation) != getattr(unit, "id", None)
    )


def _chosen_particular(conversation: Conversation) -> bool:
    return _label_match(
        _selected_insurance(conversation), LABEL_INSURANCE_PARTICULAR
    ) and not _insurance_is_automatic(conversation)


def _day_back_result(
    conversation: Conversation,
    tenant: Tenant,
    services: list[dict],
    professionals: list | None,
    target: str | None,
) -> FlowRouterResult:
    if target == BACK_TARGET_INSURANCE:
        # The convênio list again, the unit kept (it rides in the conversation). The answer
        # given there is not kept behind the question: it is the thing being chosen again.
        # A stale row (the combination accepts nothing any more) never draws a list that holds
        # only Particular: it goes one step further back, where the gate decides afresh.
        owner = _booking_professional(conversation, professionals)
        entry = _booking_service_entry(conversation, tenant, services, owner)
        unit = flow_units.effective_unit(
            conversation, owner, entry_service_id(entry) if entry else None
        )
        if _accepted_plans(tenant, owner, entry, unit):
            return _enter_insurance(tenant, conversation, professionals=professionals)
        target = _back_target_for(_booking_unit_choice(conversation, tenant, services, owner))
    if target == BACK_TARGET_UNIT:
        owner = _booking_professional(conversation, professionals)
        choice = _booking_unit_choice(conversation, tenant, services, owner)
        if choice.kind == "list":
            return _enter_unit_list(conversation, owner, units=choice.units)
        target = BACK_TARGET_SERVICE
    if target == BACK_TARGET_PROFESSIONAL and _is_multi_professional(professionals):
        result = _enter_professional_list(
            tenant,
            professionals or [],
            insurance=_selected_insurance(conversation),
            service_name=conversation.flow_selected_type,
        )
        result.flow_selected_type = conversation.flow_selected_type
        result.flow_selected_insurance = _selected_insurance(conversation)
        return result
    if target == BACK_TARGET_SERVICE and _is_multi_professional(professionals):
        # TASK-088 R18: on a multi-doctor clinic the service was the first choice (the clinic's
        # catalog), the doctor the second - "Outro serviço" reopens the catalog, doctor and
        # service cleared (they are not named on the result), not the chosen doctor's list.
        return _enter_clinic_service_catalog(tenant, professionals or [])
    if target in (BACK_TARGET_SERVICE, BACK_TARGET_DOCTOR_SERVICES) and services:
        return FlowRouterResult(
            action="reply",
            bubbles=[
                _service_list_bubble(
                    tenant,
                    services,
                    scope=service_scope(_selected_professional_id(conversation)),
                    professional=_booking_professional(conversation, professionals),
                    insurance=_selected_insurance(conversation),
                )
            ],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_AWAITING_SERVICE,
            flow_selected_type=conversation.flow_selected_type,
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=_selected_insurance(conversation),
            flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
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
    refuse_day: DayRefusal | None = None,
) -> FlowRouterResult:
    """One inbound turn on a day step (plain, retry, or escape render).

    A date it cannot read re-asks with the picker and escalates the step (the
    escape row then reaches the model). Talk that is not a date attempt at all
    ("vocês têm estacionamento?") gets ONE model turn on the same step instead
    (owner, 2026-10-07, TASK-038): the picker stays tappable.

    `refuse_day` (TASK-087): a day the service is not done on (typed, or tapped from a stale
    list) gets its plain refusal line over the day picker - which already leaves it out -
    instead of an empty slot list.
    """
    step = conversation.flow_step or branch.day_step
    if _is_day_back(body, back_target):
        return _handle_day_back(
            conversation, tenant, services, professionals, _row_payload(body) or back_target
        )
    if step == branch.day_escape_step and _control_match(body, LABEL_OTHER):
        return _delegate_llm_keeping_draft(conversation)
    if _control_match(body, LABEL_OTHER_DATE):
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
            calendar_view=True,
        )
    if _control_match(body, LABEL_MORE_DAYS):
        page, calendar_view = _more_days_cursor(_row_payload(body))
        return await enter_day_picker(
            conversation,
            tenant,
            calendar,
            duration_minutes=duration_minutes,
            branch=branch,
            page=page,
            back_target=back_target,
            step=step,
            professionals=professionals,
            calendar_view=calendar_view,
        )

    if calendar is None:
        return _calendar_unavailable(conversation, branch, step)
    target, page = _day_from_body(body)
    if target is None:
        # Free text is still honoured as a shortcut ("amanhã", "12/06").
        target = _parse_day(body, datetime.now(calendar.tzinfo))
    refusal = refuse_day(target) if target is not None and refuse_day is not None else None
    if refusal:
        logger.info(
            "flow_day_blocked_for_service",
            conversation_id=str(getattr(conversation, "id", None)),
            step=step,
        )
        return await enter_day_picker(
            conversation,
            tenant,
            calendar,
            duration_minutes=duration_minutes,
            branch=branch,
            back_target=back_target,
            prefix=refusal,
            professionals=professionals,
        )
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

    if _reads_as_conversation(body) or not _looks_like_date_attempt(body):
        # A question or chat, not a failed date: the model answers it and the
        # patient is still on this step (TASK-038).
        logger.info(
            "flow_day_free_text_to_llm",
            conversation_id=str(getattr(conversation, "id", None)),
            step=step,
        )
        return _preserve(conversation, "delegate_llm")

    # Unreadable date attempt. Re-ask with the same tappable list, one step
    # further along the bounded escalation.
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
    if _is_day_back(body, back_target):
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


# `_booking_duration`'s default: the unit is the booking's own (`flow_units.effective_unit`).
_BOOKING_UNIT = object()


def _booking_duration(
    conversation: Conversation,
    tenant: Tenant,
    services: list[dict] | None,
    professionals: list | None = None,
    *,
    unit: Any = _BOOKING_UNIT,
) -> int:
    """Minutes the booking branch should slot on: the chosen service's own.

    TASK-085 (D2/D7): at the booking's unit - `unit` when the caller already holds it (the unit
    step: the pick is not on the conversation yet), else the one picked or the only one on offer
    - with the booking owner's per-unit duration (`_service_duration_at`). No unit known, no
    professional, or no exception: the service's base, exactly as before. The day list, the slot
    list, the hold and the event all read this one value for the same unit.
    """
    service = _match_service(
        services if services is not None else active_appointment_types(tenant),
        conversation.flow_selected_type,
    )
    if service is None:
        return tenant.appointment_duration_min or 30
    owner = _booking_professional(conversation, professionals)
    if unit is _BOOKING_UNIT:
        unit = flow_units.effective_unit(conversation, owner, entry_service_id(service))
    return _service_duration_at(service, tenant, owner, getattr(unit, "id", None))


def _notice_now(tz) -> datetime:
    """"Now" for the minimum-notice rule - one seam, so tests can pin the clock."""
    return datetime.now(tz)


def _booking_floor(
    conversation: Conversation, professionals: list | None, branch: DayBranch, tz
) -> datetime | None:
    """The earliest start a NEW booking may have (the doctor's minimum notice), or None.

    Only the first-booking branch has one: rescheduling (MANAGE) and editing an
    appointment (EDIT) are never limited by it - the clinic's rule is about how late a
    patient may book, not about moving what is already booked.
    """
    if branch is not BOOKING_DAY_BRANCH:
        return None
    return booking_notice_floor(_booking_professional(conversation, professionals), _notice_now(tz))


def _booking_professional(conversation: Conversation, professionals: list | None) -> Any | None:
    """WHOSE agenda the booking branch is about to read.

    The doctor the patient picked, when there is one. On a clinic with a SINGLE
    active professional nothing ever picks one — `_enter_professional_services`
    is the only writer of `flow_selected_professional_id`, and that clinic never
    shows a doctor list — yet that sole row IS the clinic's effective config
    (`workers/tasks.py::_flow_tenant_snapshot` resolves the whole snapshot
    through it), so it is unambiguously the professional this booking belongs
    to. None when the roster is empty (a tenant with no professional rows at
    all) or holds several with none selected, because then there is no single
    doctor to name in a message or alert.
    """
    selected = _find_professional_by_id(professionals, _selected_professional_id(conversation))
    if selected is not None:
        return selected
    rows = professionals or []
    return rows[0] if len(rows) == 1 else None


async def _ask_day_after_choice(
    state: _DayPickerState,
    tenant: Tenant,
    calendar: CalendarService | None,
    services: list[dict] | None,
    professionals: list | None,
    *,
    changed: bool,
    announce_unit: bool = False,
    prefix: str | None = None,
) -> FlowRouterResult:
    drop_automatic = changed and _insurance_is_automatic(state)
    if drop_automatic:
        state.flow_selected_insurance = None
        state.flow_insurance_auto = None
    result = await _ask_day(
        state, tenant, calendar, services, professionals, announce_unit=announce_unit, prefix=prefix
    )
    if drop_automatic and result.flow_selected_insurance is None:
        result.insurance_dropped = True
    return result


async def _ask_day(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    services: list[dict] | None = None,
    professionals: list | None = None,
    *,
    prefix: str | None = None,
    announce_unit: bool = False,
    reask_insurance: bool = False,
) -> FlowRouterResult:
    """Open the booking branch's day picker (the step after service, doctor, unit and convênio).

    THE choke point for the static "this doctor has no hours at all" check, and
    the reason it lives here rather than back on a tap: every first entry into the
    booking day picker funnels through this one function — the service tap
    (`_continue_with_service`), the unit pick, the convênio answer (`_handle_insurance`),
    the LLM hand-back (`enter_guided_booking`), the AI draft landing and a resumed
    conversation (`_resume_bubbles`) — so another caller cannot be added that skips it.
    `enter_day_picker` itself stays free of the check because the manage/reschedule branch
    shares it and has its own remedy.

    The check is STATIC (`professional_business_hours`, pure stored config) and
    runs BEFORE any calendar call, which is exactly what separates it from
    `enter_day_picker`'s `if not days:` branch: that one fires both for "no
    hours configured" and for "configured but fully booked", and only the first
    is somebody's mistake.

    TASK-088: this is also WHERE the convênio is asked - after the unit is resolved (picked,
    implied by the only one, or no units at all) and before the days, when the clinic collects
    it (`_insurance_gate`): the filtered list, or Particular with one line when nothing is
    accepted for (service, doctor, unit), or - for an answer that was already there - kept /
    asked again, never swapped in silence.

    `prefix` explains why a supplied draft day was dropped; None renders as before.
    `announce_unit` is for the caller that has JUST stored the unit (the unit pick, the convênio
    answer): the unit's duration line (D3, no money) is then said once, as it was on the unit tap.
    """
    professional = _booking_professional(conversation, professionals)
    if professional is not None and professional_has_no_hours(professional, tenant):
        return _professional_config_incomplete(professional, PROFESSIONAL_GAP_HOURS)
    # TASK-083 spec §8 + follow-up §6.1: a professional who attends at units books at one of
    # those that do THIS service - asked when there are two or more, implied when there is one,
    # the "0 unidades" notice when there is none. No unit at all: today's picker.
    entry = _booking_service_entry(conversation, tenant, services, professional)
    service_id = entry_service_id(entry) if entry is not None else None
    # TASK-088: the unit comes BEFORE the convênio, so the units offered are the service's, never
    # narrowed by a convênio (the old TASK-086 A5 filter): the plan is judged AT the unit instead.
    choice = flow_units.unit_choice(professional, service_id)
    service_name = _service_entry_name(conversation, entry)
    unit = flow_units.effective_unit(conversation, professional, service_id)
    if _automatic_insurance_unit_changed(conversation, unit):
        state = _DayPickerState(
            id=getattr(conversation, "id", None),
            flow_selected_type=conversation.flow_selected_type,
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=_selected_insurance(conversation),
            flow_insurance_auto=True,
            flow_attendee_name=_attendee_name(conversation),
        )
        result = await _ask_day_after_choice(
            state, tenant, calendar, services, professionals,
            changed=True, announce_unit=announce_unit, prefix=prefix,
        )
        result.unit_dropped = result.flow_selected_unit_id is None
        return result
    if choice.kind == "none" and entry is not None and service_id is not None:
        return _enter_no_unit_for_service(
            conversation, tenant, professionals, professional, service_name, service_id
        )
    if choice.kind != "free" and unit is None:
        return _enter_unit_choice(
            conversation, professional, choice, service_name=service_name, prefix=prefix
        )
    if (
        reask_insurance
        and _chosen_particular(conversation)
        and _insurance_step_skip_reason(tenant) is None
        and _accepted_plans(tenant, professional, entry, unit)
    ):
        return _enter_insurance(
            tenant, conversation, professionals=professionals,
            unit_id=unit.id if unit is not None else None,
        )
    gate = _insurance_gate(
        conversation,
        tenant,
        professionals,
        professional,
        entry,
        unit,
        prefix=prefix,
        unit_asked=choice.kind in ("list", "confirm"),
    )
    if gate.ask is not None:
        return gate.ask
    if gate.offer_units is not None:
        return gate.offer_units
    if gate.notice:
        prefix = f"{prefix} {gate.notice}" if prefix else gate.notice
    result = await enter_day_picker(
        conversation,
        tenant,
        # TASK-087: without the service's blocked days at this unit (none chosen yet: only the
        # days blocked at every unit of the service, R1).
        _service_scoped(calendar, professional, entry, unit),
        duration_minutes=_booking_duration(
            conversation, tenant, services, professionals, unit=unit
        ),
        branch=BOOKING_DAY_BRANCH,
        back_target=_day_back_target(tenant, professional, entry, unit, choice),
        professionals=professionals,
        prefix=prefix,
        buffer_rule=_booking_buffer(professional),
    )
    # R41: whose agenda this was drawn on - the state's explicit doctor (a clinic that stores none
    # uses the tenant calendar, which is already right for it).
    result.agenda_owner_id = _selected_professional_id(conversation)
    if unit is not None and result.flow_state is FlowState.SERVICE_CATALOG:
        result.flow_selected_unit_id = unit.id
    if gate.particular and result.flow_state is FlowState.SERVICE_CATALOG:
        # Nothing accepted for the combination: recorded as Particular, so the appointment is.
        result.flow_selected_insurance = LABEL_INSURANCE_PARTICULAR
        result.flow_insurance_auto = True
    return _with_unit_terms_line(
        result, conversation, tenant, services, professionals, unit, fresh=announce_unit
    )


# --------------------------------------------------------------------------
# The unit step (TASK-083 spec §8; helpers in services/flow_units.py)
# --------------------------------------------------------------------------


def professional_has_no_hours(professional: Any, tenant: Tenant) -> bool:
    """The static "nobody can book this professional" hours check (FEAT 41).

    Without unit links: the professional's own resolved hours, exactly as before. With links
    (TASK-083): the units decide - a doctor whose own schedule is empty but whose units carry
    hours is bookable, one linked only to closed units is not.
    """
    if flow_units.offered_units(professional):
        return not flow_units.bookable_units(professional)
    return not professional_business_hours(professional, tenant)


def _unit_calendar(calendar: CalendarService | None, unit: Any | None) -> Any:
    """The agenda as seen from `unit` (its hours, id and location); unchanged without one."""
    return flow_units.scoped_calendar(calendar, unit)


# --------------------------------------------------------------------------
# TASK-087: the service's blocked weekdays on every patient path (SPEC 5.2)
# --------------------------------------------------------------------------

# A day -> the patient's refusal line when the service is not done that weekday, else None.
DayRefusal = Callable[[Any], str | None]


def _service_days_calendar(calendar: Any, blocked: frozenset[str]) -> Any:
    """`calendar` with EXACTLY `blocked` as the service's closed weekdays.

    Replaces, never adds: the router re-scopes calendars it received already scoped (the
    catalog step's agenda reaches the unit step, the confirmation, the resume), so a set left by
    an earlier scope is cleared when this one has none. Nothing to set or clear, or an agenda
    without the filter (test doubles): the same object.
    """
    if calendar is None:
        return None
    setter = getattr(calendar, "with_blocked_weekdays", None)
    if not callable(setter):
        return calendar
    if not blocked and not getattr(calendar, "_blocked_weekdays", None):
        return calendar
    return setter(blocked)


def _service_scoped(
    calendar: Any, professional: Any | None, entry: dict | None, unit: Any | None
) -> Any:
    """The agenda a patient books `entry` on: `unit`'s hours (`scoped_calendar`) without the
    service's blocked weekdays there (`flow_units.blocked_weekdays_of`: no unit yet = only the
    days blocked at every unit of the service, R1; no service known = nothing, R5)."""
    unit_id = getattr(unit, "id", None) if unit is not None else None
    return _service_days_calendar(
        flow_units.scoped_calendar(calendar, unit),
        flow_units.blocked_weekdays_of(professional, entry, unit_id),
    )


def _booking_calendar(
    calendar: Any,
    conversation: Any,
    tenant: Tenant,
    services: list[dict] | None,
    professional: Any | None,
    unit: Any | None,
) -> Any:
    """`_unit_calendar` for the booking in progress, also without its service's blocked days."""
    entry = _booking_service_entry(conversation, tenant, services, professional)
    return _service_scoped(calendar, professional, entry, unit)


def _service_day_check(
    professional: Any | None, entry: dict | None, unit: Any | None
) -> DayRefusal | None:
    """None when the service blocks no weekday here; else day -> the refusal line or None.

    The line is `unit_booking.service_day_refusal` ("Cirurgia com Dra. Ana não está disponível
    às segundas. Posso ver outro dia?") over the days blocked at `unit` (or, with none, at the
    units of the service - `flow_units.blocked_weekdays_of`). The day is read as given: callers
    pass the clinic's local date/time.
    """
    if entry is None:
        return None
    unit_id = getattr(unit, "id", None) if unit is not None else None
    blocked = flow_units.blocked_weekdays_of(professional, entry, unit_id)
    if not blocked:
        return None
    refusal = service_day_refusal(
        str(entry.get("name") or ""), getattr(professional, "name", None), blocked
    )

    def check(day: Any) -> str | None:
        return refusal if blocked_on(day, blocked) else None

    return check


def _appointment_owner_and_unit_id(
    appt: dict, professionals: list | None
) -> tuple[Any | None, UUID | None] | None:
    """An existing appointment's doctor (snapshot row) and stored unit id; None if unreadable."""
    owner_id = appt.get("professional_id")
    try:
        unit_id = UUID(str(appt["unit_id"])) if appt.get("unit_id") else None
        if owner_id:
            owner = _find_professional_by_id(professionals, UUID(str(owner_id)))
        else:
            rows = professionals or []
            owner = rows[0] if len(rows) == 1 else None
    except (TypeError, ValueError):
        return None
    return owner, unit_id


def _appointment_service(
    appt: dict | None, professionals: list | None, tenant: Tenant | None
) -> tuple[Any | None, dict | None, Any | None]:
    """(owner, service entry, unit) a reschedule of `appt` is checked against (TASK-087).

    The entry is the owner's own one for the appointment's service (`_match_service` over the
    owner's list); the unit is the one `flow_units.appointment_calendar` offers the new day at
    (the appointment's, or the owner's only bookable one; several and none recorded: None, so
    R1's "blocked at every unit"). No owner, no tenant or no match: no entry, no filter.
    """
    if not appt or tenant is None:
        return None, None, None
    parsed = _appointment_owner_and_unit_id(appt, professionals)
    if parsed is None:
        return None, None, None
    owner, unit_id = parsed
    if owner is None:
        return None, None, None
    entry = _match_service(
        professional_appointment_types(owner, tenant), appt.get("appointment_type")
    )
    units = flow_units.bookable_units(owner)
    unit = flow_units.find_unit(owner, unit_id) if units else None
    if unit is None and len(units) == 1:
        unit = units[0]
    return owner, entry, unit


def _appointment_service_days(
    calendar: Any, appt: dict | None, professionals: list | None, tenant: Tenant | None
) -> Any:
    """`calendar` (already the appointment's agenda) without its service's blocked days."""
    owner, entry, unit = _appointment_service(appt, professionals, tenant)
    unit_id = getattr(unit, "id", None) if unit is not None else None
    return _service_days_calendar(
        calendar, flow_units.blocked_weekdays_of(owner, entry, unit_id)
    )


def _appointment_day_check(
    appt: dict | None, professionals: list | None, tenant: Tenant | None
) -> DayRefusal | None:
    """`_service_day_check` for a reschedule of `appt`."""
    owner, entry, unit = _appointment_service(appt, professionals, tenant)
    return _service_day_check(owner, entry, unit)


def _booking_service_entry(
    conversation: Any, tenant: Tenant, services: list[dict] | None, professional: Any | None
) -> dict | None:
    """The professional's own catalog entry for the service being booked, or None.

    The SAME entry the flow already used (spec §9.11): `flow_selected_type` resolved through
    `_match_service` over the booking's service list - `services` when the caller has it, else
    the professional's (or, with nobody, the clinic's) - never a loose name comparison.
    """
    if not getattr(conversation, "flow_selected_type", None):
        return None
    if services is None:
        services = (
            professional_appointment_types(professional, tenant)
            if professional is not None
            else active_appointment_types(tenant)
        )
    return _match_service(services, conversation.flow_selected_type)


def _booking_service_id(
    conversation: Any, tenant: Tenant, services: list[dict] | None, professional: Any | None
) -> UUID | None:
    """The canonical id of the service being booked, for the unit filter (`flow_units`).

    None - no unit restriction at all - when nothing matches or the entry carries no
    `service_id`: an entry without `service_id` is unrestricted by definition (spec §9.6/§9.11).
    """
    entry = _booking_service_entry(conversation, tenant, services, professional)
    return entry_service_id(entry) if entry is not None else None


def _booking_unit_choice(
    conversation: Any, tenant: Tenant, services: list[dict] | None, professional: Any | None
) -> flow_units.UnitChoice:
    """`flow_units.unit_choice` for the booking in progress (its doctor and its service).

    TASK-088: never narrowed by the convênio - the unit comes BEFORE it, and the plan is
    judged at the unit the patient picks (`_insurance_gate`)."""
    return flow_units.unit_choice(
        professional, _booking_service_id(conversation, tenant, services, professional)
    )


def _back_target_for(choice: flow_units.UnitChoice) -> str:
    """The unit list is one step back only when the patient had a real list to pick from;
    otherwise (no units, one implied, a single confirmed one) it is the service list."""
    return BACK_TARGET_UNIT if choice.kind == "list" else BACK_TARGET_SERVICE


def _booking_back_target(
    conversation: Any,
    tenant: Tenant,
    services: list[dict] | None,
    professionals: list | None,
) -> str:
    """Where the booking day/slot pickers' back row goes: the convênio list when he was asked it
    in this booking, else the unit list when he had one to choose from (for THIS service), else
    the service list (`_day_back_target`)."""
    owner = _booking_professional(conversation, professionals)
    entry = _booking_service_entry(conversation, tenant, services, owner)
    choice = _booking_unit_choice(conversation, tenant, services, owner)
    service_id = entry_service_id(entry) if entry else None
    unit = flow_units.effective_unit(conversation, owner, service_id)
    return _day_back_target(tenant, owner, entry, unit, choice)


def _booking_unit(
    conversation: Any, tenant: Tenant, services: list[dict] | None, owner: Any | None
) -> Any | None:
    """The unit this booking happens at, within the units that do its service."""
    return flow_units.effective_unit(
        conversation, owner, _booking_service_id(conversation, tenant, services, owner)
    )


def _service_entry_name(conversation: Any, entry: dict | None) -> str:
    """The service's name as the patient sees it: the catalog entry's, else the stored one."""
    name = entry.get("name") if entry is not None else None
    return str(name or getattr(conversation, "flow_selected_type", None) or "")


# TASK-085 D3 / TASK-088 R23: "Consulta na Unidade Centro: 40 min" - the unit's duration, no money.
# A no-break space (U+00A0) binds "min" to its number, so a narrow chat never breaks "40 / min".
_NBSP = "\u00a0"
UNIT_TERMS_LINE = "{servico} na {unidade}: {duracao}" + _NBSP + "min"


def _unit_terms_text(service_name: str, unit: Any, minutes: int) -> str:
    """The unit duration line. A unit already named "Unidade ..." is not prefixed again
    ("Consulta na Unidade Centro", never "na Unidade Unidade Centro": `unit_place_label`)."""
    place = unit_place_label(getattr(unit, "name", None))
    return UNIT_TERMS_LINE.format(
        servico=service_name.strip(), unidade=place, duracao=minutes
    )


def _with_unit_terms_line(
    result: FlowRouterResult,
    conversation: Any,
    tenant: Tenant,
    services: list[dict] | None,
    professionals: list | None,
    unit: Any | None,
    *,
    fresh: bool = False,
) -> FlowRouterResult:
    """TASK-085 D3 / TASK-088 R23: the unit's DURATION, said once the unit is known - no money.

    When the day picker opens for `unit` and the service takes a different time there than its
    base (`flow_units.terms_at` / `_booking_duration`: the length the slots use), ONE text bubble
    goes before the picker: "Consulta na Unidade Centro: 45 min". The price that used to ride in
    this line ("... R$ 250,00 · 30 min") left the middle of the flow with the service card: money
    is said only on the final confirmation. Nothing at all when the unit takes the base time (no
    exception, the same duration everywhere, one unit), or when `unit` is already the
    conversation's chosen unit (a day picker drawn again for it - resume, re-entry - never repeats
    the line); so it is the same under a convênio. `conversation` is the conversation as it
    arrived, not the step's carrier. `fresh`: the caller has JUST stored `unit` - the unit pick,
    or the convênio answer that follows it - so the line is said once even though the conversation
    holds it.
    """
    if (
        unit is None
        or result.action != "reply"
        or result.flow_step not in _BOOKING_DAY_STEPS
        or (unit.id == flow_units.selected_unit_id(conversation) and not fresh)
    ):
        return result
    owner = _booking_professional(conversation, professionals)
    entry = _booking_service_entry(conversation, tenant, services, owner)
    if entry is None:
        return result
    minutes = _booking_duration(conversation, tenant, services, professionals, unit=unit)
    if minutes == _booking_duration(conversation, tenant, services, professionals, unit=None):
        return result
    line = _unit_terms_text(_service_entry_name(conversation, entry), unit, minutes)
    logger.info(
        "flow_unit_terms_line_shown", conversation_id=str(getattr(conversation, "id", None))
    )
    bubble = TextBubble(body=truncate_plain(line, MAX_TEXT_MESSAGE_CHARS))
    result.bubbles = [bubble, *result.bubbles]
    return result


def _enter_unit_choice(
    conversation: Conversation,
    professional: Any,
    choice: flow_units.UnitChoice,
    *,
    service_name: str | None = None,
    prefix: str | None = None,
) -> FlowRouterResult:
    """Ask the unit: Sim/Não on the only one for a `confirm` choice, the list for a `list` one.

    `choice.units` is passed as is: an empty list never widens to every bookable unit."""
    if choice.kind == "confirm":
        name = service_name or _service_entry_name(conversation, None)
        return _enter_unit_confirm(
            conversation,
            professional,
            choice.units[0],
            name,
            prefix=prefix,
        )
    return _enter_unit_list(conversation, professional, prefix=prefix, units=choice.units)


def _enter_unit_confirm(
    conversation: Conversation,
    professional: Any,
    unit: Any,
    service_name: str,
    *,
    prefix: str | None = None,
) -> FlowRouterResult:
    """"O serviço ... é feito apenas na unidade ...": the fixed ✅ Sim / ❌ Não card (spec §5.2).

    Asked when the service is done at one of the doctor's two or more bookable units (§9.1).
    Same buttons as the other fixed yes/no steps; the LLM only answers talk on this step
    (`_handle_unit_confirm_step`). The unit is NOT saved here - only "Sim" stores it - and the
    booking so far (service, doctor, convênio) rides along.
    """
    body = flow_units.unit_confirm_body(service_name, professional, unit)
    if prefix:
        body = f"{prefix} {body}"
    fields = _day_branch_fields(conversation, BOOKING_DAY_BRANCH)
    fields["flow_selected_unit_id"] = None
    return FlowRouterResult(
        action="reply",
        bubbles=[
            MenuBubble(body=body[:MAX_INTERACTIVE_BODY_CHARS], labels=[LABEL_YES, LABEL_NO])
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=flow_units.STEP_AWAITING_UNIT_CONFIRM,
        **fields,
    )


def _no_unit_alternatives(
    tenant: Tenant,
    professionals: list | None,
    professional: Any,
    service_name: str,
    *,
    insurance: str | None = None,
) -> list:
    """The OTHER doctors who really do `service_name`: they offer it and some bookable unit of
    theirs does it (or they attend without units). Never `professional` itself - offering it
    again would only repeat the same notice. TASK-086 (A6): with the patient's `insurance`, only
    doctors the convênio matrix lets take it for that service somewhere."""
    plan = _plan_kwargs(tenant, insurance)
    alternatives = []
    for other in _professionals_offering(tenant, professionals or [], service_name):
        if getattr(other, "id", None) == getattr(professional, "id", None):
            continue
        entry = _match_service(professional_appointment_types(other, tenant), service_name)
        other_service_id = entry_service_id(entry) if entry is not None else None
        if flow_units.unit_choice(other, other_service_id, **plan).kind == "none":
            continue
        if professional_has_no_hours(other, tenant):
            continue
        alternatives.append(other)
    return alternatives


def _no_unit_message(service_name: str, professional: Any) -> str:
    name = str(getattr(professional, "name", "") or "").strip()
    who = f" com {name}" if name else ""
    return f"Não consegui seguir com {service_name}{who} agora. Avisei a clínica."


def _enter_no_unit_for_service(
    conversation: Conversation,
    tenant: Tenant,
    professionals: list | None,
    professional: Any,
    service_name: str,
    service_id: UUID,
) -> FlowRouterResult:
    """"0 unidades" (spec §6.1/§6.3, §9.8): no bookable unit of `professional` does the service.

    The patient hears it in plain words, the clinic is told (`notify_no_unit`, sent by the
    worker), and the booking goes on - never a dead end, never a loop:

    * other doctors really do the service -> the doctor list, only them, with the service kept
      (a tap re-enters the booking with that doctor and the service kept -
      `_handle_service_professional` - and the unit, the convênio and the days follow, in that
      order, like the service-first branch);
    * nobody else (one doctor, or only this one does it, whatever was picked first) -> the
      service list of this doctor, the service still in it (§9.9: never hidden).

    The doctor's own choice and the unit are dropped either way; convênio and pra-quem ride on.
    """
    logger.info(
        "flow_no_unit_for_service",
        conversation_id=str(getattr(conversation, "id", None)),
    )
    result = _another_doctor_or_service(
        conversation,
        tenant,
        professionals,
        professional,
        service_name,
        notice=_no_unit_message(service_name, professional),
    )
    result.notify_no_unit = (professional.id, service_id, service_name)
    return result


def _another_doctor_or_service(
    conversation: Conversation,
    tenant: Tenant,
    professionals: list | None,
    professional: Any,
    service_name: str,
    *,
    notice: str | None,
) -> FlowRouterResult:
    """Where "0 unidades" and the "❌ Não" to the unit confirmation lead (spec §9.8).

    * other doctors really do the service -> the doctor list, only them, with the service kept;
    * nobody else -> this doctor's service list, the service still in it.

    `notice` (the "0 unidades" text) heads the card; None ("❌ Não") leaves each list's own
    question. The doctor's choice and the unit are dropped; convênio and pra-quem ride on.
    Nothing here tells the clinic: that is the "0 unidades" caller's `notify_no_unit`.
    """
    alternatives = _no_unit_alternatives(
        tenant,
        professionals,
        professional,
        service_name,
        insurance=_selected_insurance(conversation),
    )
    if alternatives:
        # `pool`: only these doctors on the Portal pointer too (TASK-082 x TASK-083).
        result = _enter_professional_list(
            tenant,
            alternatives,
            insurance=_selected_insurance(conversation),
            pool=True,
            service_name=conversation.flow_selected_type,
        )
        if notice:
            [bubble] = result.bubbles
            question = f"{notice} Vamos escolher outro médico?"
            plan = _selected_plan(tenant, _selected_insurance(conversation))
            bubble.body = _professional_list_body(question, plan)[:MAX_LIST_BODY_CHARS]
        result.flow_selected_type = conversation.flow_selected_type
        result.flow_selected_insurance = _selected_insurance(conversation)
        return result
    services = professional_appointment_types(professional, tenant)
    return FlowRouterResult(
        action="reply",
        bubbles=[
            _service_list_bubble(
                tenant,
                services,
                header=notice,
                scope=service_scope(getattr(professional, "id", None)),
                professional=professional,
                insurance=_selected_insurance(conversation),
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE,
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
        flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
    )


def _matrix_refused_service(
    tenant: Tenant, professional: Any | None, service: dict | None, insurance: str | None
) -> UUID | None:
    """The service's id when the convênio matrix refuses `insurance` for it with `professional`
    at every place it is bookable (the "não atende este convênio" row, A4), else None."""
    if professional is None or service is None:
        return None
    plan_id = _matrix_plan_id(tenant, insurance)
    service_id = entry_service_id(service)
    if plan_id is None or service_id is None:
        return None
    if refuses_everywhere(snapshot_exceptions(tenant), professional, service_id, plan_id):
        return service_id
    return None


def _plan_refused_labels(
    conversation: Conversation,
    tenant: Tenant,
    professionals: list | None,
    professional: Any,
    service_name: str,
) -> list[str]:
    labels: list[str] = []
    if _no_unit_alternatives(
        tenant,
        professionals,
        professional,
        service_name,
        insurance=_selected_insurance(conversation),
    ):
        labels.append(LABEL_PLAN_OTHER_DOCTOR)
    labels.append(LABEL_PLAN_OTHER_SERVICE)
    if _insurance_step_skip_reason(tenant) is None:
        labels.append(LABEL_PLAN_CHANGE_INSURANCE)
    return labels


def _enter_plan_refused(
    conversation: Conversation,
    tenant: Tenant,
    professionals: list | None,
    professional: Any,
    service_name: str,
    service_id: UUID | None = None,
) -> FlowRouterResult:
    """ "Não atende este convênio" (TASK-086 A5, inventory R4): the matrix leaves `service_name`
    with no place for the patient's convênio with `professional`.

    Nothing is misconfigured, so the clinic is NOT told (no `notify_no_unit`, unlike "0
    unidades"). The patient picks how to go on: another doctor who accepts the convênio for the
    service (only when there is one), another service of this doctor, or another convênio (only
    when the clinic asks it). Service, doctor and convênio ride along; the unit is cleared.
    """
    log_refusal(
        "flow_plan_refused_for_service",
        conversation_id=getattr(conversation, "id", None),
        professional_id=getattr(professional, "id", None),
        service_id=service_id,
    )
    name = str(getattr(professional, "name", "") or "").strip()
    body = PLAN_REFUSED_BODY.format(
        quem=name or "A clínica",
        convenio=str(_selected_insurance(conversation) or "").strip(),
        servico=service_name,
    )
    return FlowRouterResult(
        action="reply",
        bubbles=[
            MenuBubble(
                body=body[:MAX_INTERACTIVE_BODY_CHARS],
                labels=_plan_refused_labels(
                    conversation, tenant, professionals, professional, service_name
                ),
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_PLAN_REFUSED,
        flow_selected_type=service_name or conversation.flow_selected_type,
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
        flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
        flow_selected_unit_id=None,
    )


def _handle_plan_refused(
    conversation: Conversation,
    tenant: Tenant,
    body: str,
    professionals: list | None,
) -> FlowRouterResult:
    """The answer to `_enter_plan_refused`'s card (other doctor / other service). Anything else:
    one model turn, same step. "Trocar convênio" is handled before this in `_catalog_step`."""
    owner = _booking_professional(conversation, professionals)
    service_name = str(conversation.flow_selected_type or "")
    if owner is not None and _label_match(body, LABEL_PLAN_OTHER_DOCTOR):
        # Only the doctors who take the convênio for the service; none left (the matrix changed
        # meanwhile) -> this doctor's service list, like "❌ Não" on the unit card.
        return _another_doctor_or_service(
            conversation, tenant, professionals, owner, service_name, notice=None
        )
    if owner is not None and _label_match(body, LABEL_PLAN_OTHER_SERVICE):
        services = professional_appointment_types(owner, tenant)
        return FlowRouterResult(
            action="reply",
            bubbles=[
                _service_list_bubble(
                    tenant,
                    services,
                    scope=service_scope(_selected_professional_id(conversation)),
                    professional=owner,
                    insurance=_selected_insurance(conversation),
                )
            ],
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=STEP_AWAITING_SERVICE,
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=_selected_insurance(conversation),
            flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
        )
    # "Trocar convênio" never gets here: `_catalog_step` routes that label to `_ask_day` first (the
    # unit, then the convênio gate), so a list that would hold only Particular never reaches the
    # patient.
    return _preserve(conversation, "delegate_llm")


def _enter_unit_list(
    conversation: Conversation,
    professional: Any,
    *,
    prefix: str | None = None,
    units: list | None = None,
) -> FlowRouterResult:
    """"Em qual unidade?": one fixed row per bookable unit (name + address), then the back row.

    Same shape as the doctor and convênio lists: payload rows (`unit|<uuid>`), WhatsApp's row
    limits applied by `flow_units.unit_rows`, the LLM only for talk on this step. The booking so
    far rides along; the unit itself is cleared (it is what is being asked). `units` narrows the
    rows to those that do the booking's service; None lists every bookable unit (today's).
    """
    if units is None:
        units = flow_units.bookable_units(professional)
    if len(units) > flow_units.MAX_UNIT_ROWS:
        logger.warning(
            "flow_unit_list_truncated", total=len(units), shown=flow_units.MAX_UNIT_ROWS
        )
    rows: list[tuple] = list(flow_units.unit_rows(units))
    rows.append((f"{ROW_DAY_BACK_PREFIX}{BACK_TARGET_SERVICE}", LABEL_ANOTHER_SERVICE))
    body = f"{prefix} {flow_units.UNIT_LIST_BODY}" if prefix else flow_units.UNIT_LIST_BODY
    fields = _day_branch_fields(conversation, BOOKING_DAY_BRANCH)
    fields["flow_selected_unit_id"] = None
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=body[:MAX_LIST_BODY_CHARS],
                rows=rows[:MAX_LIST_ROWS],
                button_label=flow_units.UNIT_LIST_BUTTON,
                section_title=flow_units.UNIT_LIST_SECTION,
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=flow_units.STEP_AWAITING_UNIT,
        **fields,
    )


def _enter_insurance_unit_list(
    conversation: Conversation, alternatives: list[tuple[Any, list[dict]]], *, page: int = 0
) -> FlowRouterResult:
    """Keep the original unit behind the list so Particular still books there."""
    items, next_page = catalog_page(alternatives, page, fixed_rows=2)
    origin = flow_units.selected_unit_id(conversation)
    rows = [
        *flow_units.insurance_unit_rows(items),
        *catalog_more_row(flow_units.ROW_UNIT_MORE_PREFIX, flow_units.LABEL_MORE_UNITS, next_page),
        (f"{flow_units.ROW_UNIT_PARTICULAR_PREFIX}{origin}", flow_units.LABEL_CONTINUE_PARTICULAR),
        (f"{ROW_DAY_BACK_PREFIX}{BACK_TARGET_SERVICE}", LABEL_ANOTHER_SERVICE),
    ]
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body="Estas unidades aceitam convênio para o serviço com o profissional escolhido:",
                rows=rows,
                button_label=flow_units.UNIT_LIST_BUTTON,
                section_title=flow_units.UNIT_LIST_SECTION,
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=flow_units.STEP_AWAITING_UNIT,
        **_day_branch_fields(conversation, BOOKING_DAY_BRANCH),
    )


async def _handle_unit_insurance_choice(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str | None,
    services: list[dict],
    professionals: list | None,
) -> FlowRouterResult:
    """Route the no-plan card and the filtered unit list, rechecking the current roster."""
    browsing = conversation.flow_step == flow_units.STEP_AWAITING_UNIT
    owner = _booking_professional(conversation, professionals)
    entry = _booking_service_entry(conversation, tenant, services, owner)
    if entry is None:
        result = _handle_day_back(
            conversation, tenant, services, professionals, BACK_TARGET_SERVICE
        )
        result.flow_selected_type = None
        result.bubbles.insert(0, TextBubble(body="O serviço escolhido não está mais disponível."))
        return result
    service_id = entry_service_id(entry)
    origin = flow_units.find_unit(owner, flow_units.selected_unit_id(conversation), service_id)
    if origin is None:
        state = _state_at_booking_unit(conversation, None)
        result = await _ask_day(
            state, tenant, calendar, services, professionals, prefix=flow_units.UNIT_GONE_NOTICE
        )
        result.unit_dropped = result.flow_selected_unit_id is None
        return result

    if _control_match(body, flow_units.LABEL_CONTINUE_PARTICULAR) and _row_payload(body) in (
        None, str(origin.id)
    ):
        return await _continue_at_booking_unit(
            conversation, tenant, calendar, services, professionals, origin.id, particular=True
        )
    if browsing and _is_day_back(body, BACK_TARGET_SERVICE):
        return _handle_day_back(conversation, tenant, services, professionals, BACK_TARGET_SERVICE)

    if (
        _insurance_step_skip_reason(tenant) is not None
        or _accepted_plans(tenant, owner, entry, origin)
        or not (alternatives := _insurance_alternative_units(tenant, owner, entry, origin))
    ):
        result = await _ask_day(conversation, tenant, calendar, services, professionals)
        if result.flow_step in _BOOKING_DAY_STEPS:
            return _resume_parked_draft(conversation, result)
        return result

    if not browsing:
        if _label_match(body, flow_units.LABEL_UNITS_WITH_INSURANCE):
            return _enter_insurance_unit_list(conversation, alternatives)
        if body is None:
            return _enter_no_plan_at_unit(conversation, origin, refused_known=False)
        return _preserve(conversation, "delegate_llm")

    page = catalog_more_page(body, flow_units.LABEL_MORE_UNITS)
    if body is None or page is not None:
        return _enter_insurance_unit_list(conversation, alternatives, page=page or 0)
    unit = flow_units.match_unit([unit for unit, _ in alternatives], body, strict_payload=True)
    if unit is not None:
        return await _continue_at_booking_unit(
            conversation, tenant, calendar, services, professionals, unit.id
        )
    if _row_payload(body) is not None:
        return _enter_insurance_unit_list(conversation, alternatives)
    return _preserve(conversation, "delegate_llm")


async def _handle_unit_step(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    services: list[dict],
    professionals: list | None,
) -> FlowRouterResult:
    """A tap (or typed name) on the unit list: the convênio when the clinic collects it, else
    that unit's day picker (back row to the units) - TASK-088: `_ask_day` decides.

    Only the units that do the booking's service are accepted (follow-up §6.1)."""
    # The ordinary list clears the unit. A saved unit here belongs to the alternatives list.
    if flow_units.selected_unit_id(conversation) is not None:
        return await _handle_unit_insurance_choice(
            conversation, tenant, calendar, body, services, professionals
        )
    owner = _booking_professional(conversation, professionals)
    choice = _booking_unit_choice(conversation, tenant, services, owner)
    units = choice.units
    if _is_day_back(body, BACK_TARGET_SERVICE):
        return _handle_day_back(conversation, tenant, services, professionals, BACK_TARGET_SERVICE)
    if choice.kind not in ("list", "confirm"):
        # The choice went away (a unit deactivated, a link removed, the service now done in
        # one unit or none): continue without asking - `_ask_day` decides what is left.
        return await _ask_day(conversation, tenant, calendar, services, professionals)
    unit = flow_units.match_unit(units, body)
    if unit is None:
        # Talk, not a unit: one model turn on this same step (the list stays tappable).
        logger.info(
            "flow_unit_free_text_to_llm", conversation_id=str(getattr(conversation, "id", None))
        )
        return _preserve(conversation, "delegate_llm")
    logger.info(
        "flow_unit_selected",
        conversation_id=str(getattr(conversation, "id", None)),
        unit_count=len(units),
    )
    return await _continue_at_booking_unit(
        conversation, tenant, calendar, services, professionals, unit.id
    )


def _state_at_booking_unit(
    conversation: Conversation, unit_id: UUID | None, *, particular: bool = False
) -> _DayPickerState:
    return _DayPickerState(
        id=getattr(conversation, "id", None),
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=(
            LABEL_INSURANCE_PARTICULAR if particular else _selected_insurance(conversation)
        ),
        flow_insurance_auto=(
            False if particular else getattr(conversation, "flow_insurance_auto", None)
        ),
        flow_attendee_name=_attendee_name(conversation),
        flow_selected_unit_id=unit_id,
    )


async def _continue_at_booking_unit(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    services: list[dict],
    professionals: list | None,
    unit_id: UUID,
    *,
    particular: bool = False,
) -> FlowRouterResult:
    state = _state_at_booking_unit(conversation, unit_id, particular=particular)
    # The pick is in `state`, not on the conversation yet: `_ask_day` reads it from there, asks
    # the convênio for (service, doctor, THIS unit) when the clinic collects it, and otherwise
    # opens the unit's days (TASK-087: its hours, minus the service's blocked days THERE).
    # A parked AI draft continues from the pick (TASK-088 R17): the pick is an answer.
    return _resume_parked_draft(
        conversation,
        await _ask_day_after_choice(
            state, tenant, calendar, services, professionals,
            changed=unit_id != flow_units.selected_unit_id(conversation),
            announce_unit=True,
        ),
    )


async def _handle_unit_confirm_step(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    services: list[dict],
    professionals: list | None,
) -> FlowRouterResult:
    """The answer to `_enter_unit_confirm`'s card (spec §5.2-3, §9.8).

    * "❌ Não" -> the doctors who do the service, else the service list - no notice, nothing
      sent to the clinic (`_another_doctor_or_service`);
    * the choice went away (a unit or a mark changed meanwhile) -> `_ask_day` decides again;
    * "✅ Sim" -> the unit is saved, then the convênio (when the clinic collects it) or its day
      picker (back row: the service list) - `_ask_day` decides (TASK-088);
    * anything else -> one model turn on this same step (the buttons stay tappable).
    """
    owner = _booking_professional(conversation, professionals)
    entry = _booking_service_entry(conversation, tenant, services, owner)
    service_id = entry_service_id(entry) if entry is not None else None
    if owner is not None and _label_match(body, LABEL_NO):
        logger.info(
            "flow_unit_confirm_answered",
            accepted=False,
            conversation_id=str(getattr(conversation, "id", None)),
        )
        return _another_doctor_or_service(
            conversation,
            tenant,
            professionals,
            owner,
            _service_entry_name(conversation, entry),
            notice=None,
        )
    choice = flow_units.unit_choice(owner, service_id)
    if choice.kind != "confirm":
        return await _ask_day(conversation, tenant, calendar, services, professionals)
    if not _label_match(body, LABEL_YES):
        logger.info(
            "flow_unit_confirm_free_text_to_llm",
            conversation_id=str(getattr(conversation, "id", None)),
        )
        return _preserve(conversation, "delegate_llm")
    [unit] = choice.units
    logger.info(
        "flow_unit_confirm_answered",
        accepted=True,
        conversation_id=str(getattr(conversation, "id", None)),
    )
    state = _DayPickerState(
        id=getattr(conversation, "id", None),
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
        flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
        flow_attendee_name=_attendee_name(conversation),
        flow_selected_unit_id=unit.id,
    )
    return _resume_parked_draft(
        conversation,
        await _ask_day_after_choice(
            state, tenant, calendar, services, professionals,
            changed=unit.id != flow_units.selected_unit_id(conversation),
            announce_unit=True,
        ),
    )


async def enter_guided_booking(
    tenant: Tenant,
    calendar: CalendarService | None,
    appointment_type: str | None,
    *,
    conversation_id: UUID | None = None,
    professional_id: UUID | None = None,
    insurance: str | None = None,
    services: list[dict] | None = None,
    professionals: list | None = None,
    attendee_name: str | None = None,
    recorded: Any | None = None,
    named_now: bool = False,
) -> FlowRouterResult:
    """LLM hand-back entry: resume the booking AFTER the service was chosen.

    The deterministic twin of tapping the service (`_continue_with_service`: no card, straight on
    to `_ask_day`). The free-text conversation resolved the service; everything after it (the
    unit, the convênio, day, time, confirmation, the booking itself) is the button flow's again.

    TASK-088 - the order is service -> doctor -> unit -> convênio -> day: the doctor is already
    fixed here (this hand-back only serves single-doctor clinics - workers/tasks.py turns a
    multi-doctor one away to the menu) and the service arrives chosen, so what is still open is
    the unit (when the doctor has several), then the convênio. `_ask_day` is the one choke
    point that asks them, in that order, for every path that reaches the days.

    **It deliberately does NOT skip an unanswered convênio.** The literal
    ask was "open the day list", but `collect_insurance` is a clinic-wide
    setting the hub saved on purpose, and `_insurance_step_skip_reason` is the
    single place that decides whether it applies (`_insurance_gate` asks it). Skipping it here
    would mean a booking loses information the clinic asked for on every OTHER booking,
    purely because this patient happened to arrive through the LLM - an
    invariant break invisible until a receptionist notices a blank convênio.
    An answer that already exists is checked against the combination like any other: kept when
    accepted, asked again (never swapped for Particular in silence) when not.

    A patient's Particular choice survives a different booking combination. A Particular
    assigned automatically is kept only at the same service, doctor and unit.

    Takes the conversation's state as plain arguments rather than a
    Conversation: the caller (workers/tasks.py::_handle_start_guided_booking)
    holds no live row by then, the same situation `enter_manage_action` is in.
    `_DayPickerState` is the carrier that exists for it, so the chosen service
    is applied to a throwaway snapshot and NOTHING here mutates a row —
    `_apply_flow_result` persists whatever the returned result carries, as it
    does for every other transition.

    `tenant` is the SAME tenant-shaped snapshot `route()` receives
    (`workers/tasks.py::_flow_tenant_snapshot`), never the raw ORM row: on a
    clinic with one active professional that snapshot already carries THAT
    professional's own catalog, which is what the slot length is read from.
    `services` may name a catalog explicitly; omitted, it is derived from the
    snapshot, which is what every button-flow caller relies on.

    `appointment_type` arrives canonical (ai/tools.py::start_guided_booking
    validated it against the catalog before the hand-back); None means the
    clinic has no catalog, and the booking proceeds typeless exactly as it
    does elsewhere. `calendar` is the agenda to read availability from — None
    makes the picker answer `calendar_unavailable` rather than invent days.
    """
    state = _DayPickerState(
        id=conversation_id,
        flow_selected_type=appointment_type,
        flow_selected_professional_id=professional_id,
        flow_selected_insurance=insurance,
        flow_insurance_auto=(
            False if named_now else getattr(recorded, "flow_insurance_auto", None)
        ) if insurance is not None else None,
        # A pra-quem answer given in the button flow BEFORE the patient drifted
        # into the LLM survives the hand-back (services/attendee.py). The LLM
        # itself never books for someone else - see ai/tools.py.
        flow_attendee_name=attendee_name,
    )
    if (
        appointment_type
        and _insurance_step_skip_reason(tenant) is None
        and _insurance_is_automatic(state)
    ):
        owner = _booking_professional(state, professionals)
        service_id = _booking_service_id(state, tenant, services, owner)
        choice = flow_units.unit_choice(owner, service_id)
        landing_unit = (
            choice.units[0] if choice.kind in ("implied", "confirm") and choice.units else None
        )
        if not particular_survives(
            tenant,
            insurance,
            named_now=named_now,
            recorded=recorded,
            professionals=professionals,
            opens_choice=choice.kind == "list",
            landing_type=appointment_type,
            landing_owner=owner,
            landing_unit=landing_unit,
        ):
            state.flow_selected_insurance = None
            state.flow_insurance_auto = None
    return _carry_booking(state, await _ask_day(state, tenant, calendar, services, professionals))


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
    duration = _booking_duration(conversation, tenant, services, professionals)
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
        back_target=_booking_back_target(conversation, tenant, services, professionals),
        professionals=professionals,
    )


async def _slot_lost_since_confirm(
    gate: BookingGate,
    calendar: CalendarService,
    start: datetime,
    end: datetime,
    professional_id: UUID | None,
    conversation_id: object,
    *,
    buffer_minutes: int = 0,
) -> bool:
    """The slot was free at "Confirmar"; is it still? Asked again after a deposit that failed
    (TASK-071 Plan 3): the agenda the event goes to, then the holds of OTHER conversations
    (`busy_windows` already leaves this conversation's own out) - and, with a doctor's buffer
    (TASK-088), the consultations and those holds padded by it. All reads fail open."""
    if await slot_taken_at_confirm(
        calendar, start, end, source="flow_confirm_deposit_waived", conversation_id=conversation_id
    ):
        return True
    windows = await gate.busy_windows(professional_id)
    if any(other_start < end and start < other_end for other_start, other_end in windows):
        return True
    return buffer_minutes > 0 and await buffer_taken_at_confirm(
        calendar,
        start,
        end,
        buffer_minutes=buffer_minutes,
        source="flow_confirm_deposit_waived",
        holds=windows,
        conversation_id=conversation_id,
    )


async def _reoffer_taken_slot(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService,
    start: datetime,
    *,
    duration_minutes: int,
    branch: DayBranch,
    back_target: str | None,
    professionals: list | None,
) -> FlowRouterResult:
    """The confirmed time was just taken on the agenda: say so, then let the patient re-pick.

    BOOKING branch (TASK-072, owner decision 2026-10-29): back to the DAY picker - the step
    whose prompt is `DAY_PICKER_BODY` ("Para quando você gostaria? Escolha um dia:") - with
    `SLOT_TAKEN_AT_CONFIRM_MESSAGE` as its prefix line. The service, doctor, convênio and
    attendee ride along (`_day_branch_fields` + the public entry's `_carry_booking`), so the
    patient only picks a new day and time. Every booking commit point lands here: the
    confirm-time re-check and the Portal promotion after the code.

    Other branches (reschedule) keep the earlier behaviour: one short line, then the fresh
    slot list of the SAME day (a day with nothing left falls back to the day picker inside
    `_enter_slot_picker`, as it does for any stale day).
    """
    if branch is BOOKING_DAY_BRANCH:
        return await enter_day_picker(
            conversation,
            tenant,
            calendar,
            duration_minutes=duration_minutes,
            branch=branch,
            back_target=back_target,
            prefix=SLOT_TAKEN_AT_CONFIRM_MESSAGE,
            professionals=professionals,
        )
    local = start.astimezone(calendar.tzinfo) if start.tzinfo else start
    result = await _enter_slot_picker(
        conversation,
        tenant,
        calendar,
        local.replace(hour=0, minute=0, second=0, microsecond=0),
        duration_minutes=duration_minutes,
        branch=branch,
        back_target=back_target,
        professionals=professionals,
    )
    if result.action == "reply":
        result.bubbles = [TextBubble(body=SLOT_TAKEN_AT_CONFIRM_MESSAGE), *result.bubbles]
    return result


async def reoffer_taken_booking_slot(
    conversation: Any,
    tenant: Tenant,
    calendar: CalendarService,
    start: datetime,
    *,
    duration_minutes: int,
    professionals: list | None = None,
) -> FlowRouterResult:
    """`_reoffer_taken_slot` on the booking branch, for an entry that is not `route()`.

    The Portal code-gate promotion (`workers/shared/booking_hold.py::_promote_booking_hold`)
    finds the chosen window taken AFTER the code was verified, with no route() turn around
    it; it hands in a conversation-shaped snapshot rebuilt from the hold and gets back the
    day picker (prefixed with the "just taken" line) to persist. The carries are applied
    here, as every public entry does.
    """
    # TASK-083: the promotion hands in the agenda it WROTE with (the unit's id, the agenda's
    # hours); the day picker that follows lists the chosen unit's own hours.
    # TASK-087: and without the service's blocked days at that unit.
    owner = _booking_professional(conversation, professionals)
    unit = _booking_unit(conversation, tenant, None, owner)
    calendar = _booking_calendar(calendar, conversation, tenant, None, owner, unit)
    result = await _reoffer_taken_slot(
        conversation,
        tenant,
        calendar,
        start,
        duration_minutes=duration_minutes,
        branch=BOOKING_DAY_BRANCH,
        back_target=_booking_back_target(conversation, tenant, None, professionals),
        professionals=professionals,
    )
    return _carry_booking(conversation, result)


def _handle_slot(
    conversation: Conversation,
    body: str,
    *,
    professional: Any | None = None,
    unit: Any | None = None,
    money_lines: list[str] | None = None,
) -> FlowRouterResult:
    start = _slot_iso_from_body(body)
    if start is None:
        return _preserve(conversation, "delegate_llm")
    slot_iso = start.replace(tzinfo=None).isoformat(timespec="minutes")
    return FlowRouterResult(
        action="reply",
        bubbles=[
            _confirmation_card(
                conversation, start, professional=professional, unit=unit, money_lines=money_lines
            )
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_day=conversation.flow_selected_day,
        flow_selected_slot=slot_iso,
        flow_selected_professional_id=_selected_professional_id(conversation),
        flow_selected_insurance=_selected_insurance(conversation),
        flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
    )


async def _booking_insurance_refusal(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    services: list[dict] | None,
    professionals: list | None,
    owner: Any | None,
    unit: Any | None,
    insurance: str | None,
) -> FlowRouterResult | None:
    """The final convênio guard (TASK-086 A7, SPEC E6): None = book; else the refusal.

    Asks the snapshot's matrix about the booking's own cell (doctor, service, unit, plan). A
    booking with a unit asks that unit; a doctor without units asks the "no unit" slice; one
    with units but none resolved is refused only when no unit accepts. Particular, a typed
    "Outro", a legacy name or a service outside the catalog are never refused. The refusal
    books nothing, says so in plain words and asks the convênio again through `_ask_day` (the
    list of what is accepted now, or Particular with its line when nothing is), keeping
    service, doctor and unit (day and time are asked again). Logged with ids only.
    """
    plan_id = _matrix_plan_id(tenant, insurance)
    if plan_id is None:
        return None
    service_id = _booking_service_id(conversation, tenant, services, owner)
    if service_id is None:
        return None
    exc = snapshot_exceptions(tenant)
    owner_id = getattr(owner, "id", None)
    if unit is not None or not flow_units.offered_units(owner):
        accepted = is_accepted(exc, owner_id, service_id, getattr(unit, "id", None), plan_id)
    else:
        accepted = not refuses_everywhere(exc, owner, service_id, plan_id)
    if accepted:
        return None
    log_refusal(
        "flow_booking_insurance_refused",
        conversation_id=getattr(conversation, "id", None),
        professional_id=owner_id,
        service_id=service_id,
        unit_id=getattr(unit, "id", None),
        plan_id=plan_id,
    )
    name = str(getattr(owner, "name", "") or "").strip()
    unit_name = str(getattr(unit, "name", "") or "").strip() if unit is not None else ""
    values = {
        "convenio": str(insurance or "").strip(),
        "servico": str(conversation.flow_selected_type or "a consulta"),
        "quem": f" com {name}" if name else "",
        "onde": f" na unidade {unit_name}" if unit_name else "",
    }
    if _insurance_step_skip_reason(tenant) is not None:
        # TASK-086 T9 review (minor 6): the clinic no longer asks the convênio, so its list is
        # never reopened - the "não atende este convênio" step instead (another doctor / another
        # service; no "Trocar convênio" with the step off), with the same refusal sentence.
        result = _enter_plan_refused(
            conversation,
            tenant,
            professionals,
            owner,
            str(conversation.flow_selected_type or ""),
            service_id,
        )
        menu = result.bubbles[0]
        menu.body = (
            f"{PLAN_REFUSED_AT_CONFIRM_LEAD.format(**values)} {PLAN_REFUSED_HOW_TO_GO_ON}"
        )[:MAX_INTERACTIVE_BODY_CHARS]
        return result
    # TASK-088: the same decision the booking made the first time (`_insurance_gate`, through
    # `_ask_day`). Keep the unit and the refused answer so the next question can explain
    # the refusal. The day and time are asked again when the patient makes a choice.
    result = await _ask_day(
        _DayPickerState(
            id=getattr(conversation, "id", None),
            flow_selected_type=conversation.flow_selected_type,
            flow_selected_professional_id=_selected_professional_id(conversation),
            flow_selected_insurance=insurance,
            flow_attendee_name=_attendee_name(conversation),
            flow_selected_unit_id=getattr(unit, "id", None),
        ),
        tenant,
        calendar,
        services,
        professionals,
    )
    if result.action == "reply":
        # "Escolha outro convênio ou Particular:" only in front of a list; a Particular day
        # picker offers no such choice (the "nothing accepted" line says what happens).
        template = (
            PLAN_REFUSED_AT_CONFIRM
            if result.flow_step == STEP_AWAITING_INSURANCE
            else PLAN_REFUSED_AT_CONFIRM_LEAD
        )
        result.bubbles.insert(0, TextBubble(body=template.format(**values)))
    return result


async def _handle_confirmation(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    patient_name: str | None,
    services: list[dict] | None = None,
    professionals: list | None = None,
    gate: BookingGate | None = None,
) -> FlowRouterResult:
    """"Confirmar" on the recap card: either a booking, or a held slot.

    Until 2026-09-20 there was only one outcome here - create the Google event,
    hand the caller an `appointment` dict, confirm. That is still what happens
    for every WhatsApp patient and for every Brain-Message visitor who has
    already proven their address.

    What is new is the third party in the room. `gate` (armed only for a
    Brain-Message visitor who still owes a code) is asked FIRST, before the
    event exists, and it can answer three ways:

        commit      -> today's path, unchanged, byte for byte.
        held        -> the slot is reserved in Postgres and a code is in the
                       patient's inbox. NOTHING is created on Google Calendar
                       and no appointment row is written: the caller asks for
                       the code and the booking happens later, in
                       `workers/tasks.py::_promote_booking_hold`.
        slot_taken  -> another conversation is holding this exact window.

    The order matters more than it looks: the gate runs before
    `calendar.create_event`, so a gated booking never puts an event on a real
    clinic's agenda that a never-typed code would then have to remove. That is
    the orphan the previous wave produced in the opposite order.

    And BEFORE the gate, one more read: the slot list the patient tapped is a
    snapshot, and nothing reserved the time on Google while they read the card -
    WhatsApp has no hold at all, and a Portal hold only blocks other holds. So
    `slot_taken_at_confirm` asks THIS agenda (the one the event is written to
    below) about exactly [start, end). Taken: nothing is created or held, and
    the patient gets one line plus the same day's fresh times
    (`_reoffer_taken_slot`). A failed read books as before (fail-open). The
    gated path is asked again at promotion, when the event is really written.
    """
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
            flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
        )
    if _norm(body) != _norm(LABEL_CONFIRM):
        return _preserve(conversation, "delegate_llm")
    if calendar is None or not conversation.flow_selected_slot:
        return _preserve(conversation, "delegate_llm")

    service_type = conversation.flow_selected_type or "Consulta"
    if services is None:
        services = active_appointment_types(tenant)
    service = _match_service(services, service_type)
    # The doctor (the pick, or a one-doctor clinic's only one: R13 - then `services` is the
    # tenant snapshot, which IS that doctor's own list) and the unit of this booking.
    booking_owner = _booking_professional(conversation, professionals)
    unit = _booking_unit(conversation, tenant, services, booking_owner)
    # TASK-085 A6: the SAME length the day/slot lists offered at this unit, so the hold, the
    # re-checks and the event are never a different window than the slot the patient tapped.
    duration = (
        _service_duration_at(service, tenant, booking_owner, getattr(unit, "id", None))
        if service
        else (tenant.appointment_duration_min or 30)
    )
    start = datetime.fromisoformat(conversation.flow_selected_slot)
    if start.tzinfo is None:
        start = start.replace(tzinfo=calendar.tzinfo)
    end = start + timedelta(minutes=duration)

    # The event is the ATTENDEE's consultation: on a booking for someone else
    # their name titles it, not the name of the account that booked.
    # `real_attendee_name`: the "pra mim" marker ("") must never reach a hold row.
    attendee_name = real_attendee_name(_attendee_name(conversation))
    event_name = attendee_name or patient_name
    summary = f"{service_type} - {event_name}" if event_name else service_type

    # Resolved BEFORE the gate so a held slot and a committed appointment
    # record the same service, owner and convenio: the hold is what the later
    # promotion reads to build the appointment row.
    canonical_type = canonical_service_name(services, service_type)
    professional_id = resolve_booking_owner_id(
        professionals, _selected_professional_id(conversation)
    )
    insurance = _selected_insurance(conversation)

    # The doctor's minimum notice, asked one last time at the write: the list or the AI's
    # card may be older than the rule's boundary (or the AI may have proposed the time).
    # TASK-083 spec §8: where the visit happens (`unit`, resolved above). The write goes through
    # the unit's agenda (its id on a local event, its location on a Google event) and the hold /
    # row record it.
    # TASK-086 (A7): the FINAL guard - the snapshot is reread every turn, so a matrix the clinic
    # changed while the patient was booking is caught here, before the notice floor, the slot
    # re-read and the gate (so before any Google event, Portal hold or Pix).
    # TASK-088: where the pickers a re-offer draws go back to - the same destination the booking's
    # own day picker had (`_day_back_target`), not always the service list.
    reoffer_back = _booking_back_target(conversation, tenant, services, professionals)
    refusal = await _booking_insurance_refusal(
        conversation, tenant, calendar, services, professionals, booking_owner, unit, insurance
    )
    if refusal is not None:
        return refusal
    calendar = _service_scoped(calendar, booking_owner, service, unit)
    unit_kwargs: dict = {"unit_id": unit.id} if unit is not None else {}
    # TASK-087 (SPEC 5.2, R1): the service is not done that weekday at this unit - a time the AI
    # proposed, a stale list or a unit picked after the day. Refused before the notice floor,
    # the slot re-read and the gate (so before any event, hold or Pix): the reason, then the day
    # picker, which already leaves the day out.
    day_check = _service_day_check(booking_owner, service, unit)
    refusal = day_check(start.astimezone(calendar.tzinfo)) if day_check is not None else None
    if refusal:
        logger.info(
            "flow_booking_day_blocked_for_service",
            conversation_id=str(getattr(conversation, "id", None)),
            professional_id=str(getattr(booking_owner, "id", None)),
            unit_id=str(unit.id) if unit is not None else None,
        )
        return await enter_day_picker(
            conversation,
            tenant,
            calendar,
            duration_minutes=duration,
            branch=BOOKING_DAY_BRANCH,
            back_target=reoffer_back,
            prefix=refusal,
            professionals=professionals,
        )
    floor = booking_notice_floor(booking_owner, _notice_now(calendar.tzinfo))
    if floor is not None and start < floor:
        logger.info(
            "flow_booking_inside_notice",
            conversation_id=str(getattr(conversation, "id", None)),
            notice_hours=booking_notice_hours(booking_owner),
        )
        return await enter_day_picker(
            conversation,
            tenant,
            calendar,
            duration_minutes=duration,
            branch=BOOKING_DAY_BRANCH,
            back_target=reoffer_back,
            prefix=booking_notice_message(booking_notice_hours(booking_owner)),
            professionals=professionals,
        )

    # TASK-088 Task 5: and not inside the doctor's buffer of a consultation (or of another
    # conversation's held time) - refused exactly like a slot taken meanwhile (R35).
    if await slot_taken_at_confirm(
        calendar,
        start,
        end,
        source="flow_confirm",
        conversation_id=getattr(conversation, "id", None),
    ) or await _inside_buffer(
        calendar,
        start,
        end,
        booking_owner,
        source="flow_confirm",
        conversation_id=getattr(conversation, "id", None),
        professional_id=professional_id,
    ):
        return await _reoffer_taken_slot(
            conversation,
            tenant,
            calendar,
            start,
            duration_minutes=duration,
            branch=BOOKING_DAY_BRANCH,
            back_target=reoffer_back,
            professionals=professionals,
        )

    # TASK-071 Plan 3 (task 6): why this booking goes ahead WITHOUT the deposit, when it does.
    deposit_waived_reason: str | None = None
    if gate is not None and (gate.armed or gate.deposit_capable):
        decision = await gate.decide(
            start_at=start,
            end_at=end,
            professional_id=professional_id,
            appointment_type=(canonical_type or service_type)[:120],
            insurance=insurance,
            attendee_name=attendee_name,
            **unit_kwargs,
        )
        if decision.outcome == "slot_taken":
            # Offer the day again rather than a dead end: the patient picked a
            # real time a moment ago and the only thing that changed is that
            # somebody else got there first.
            return FlowRouterResult(
                action="reply",
                bubbles=[
                    MenuBubble(
                        body=BOOKING_SLOT_TAKEN_MESSAGE,
                        labels=[LABEL_RETRY_YES, LABEL_RETRY_MENU],
                    )
                ],
                flow_state=FlowState.SERVICE_CATALOG,
                flow_step=STEP_AWAITING_RETRY,
                flow_selected_type=conversation.flow_selected_type,
                flow_selected_day=conversation.flow_selected_day,
                flow_selected_slot=conversation.flow_selected_slot,
                flow_selected_professional_id=_selected_professional_id(conversation),
                flow_selected_insurance=insurance,
                flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
            )
        if decision.outcome == "held":
            # No bubbles: the caller sends the code CARD (three buttons), which
            # this module cannot build - `services/pending_identity.py` owns it
            # and `workers/tasks.py::_send_code_notice` is the single spelling.
            return FlowRouterResult(
                action="reply",
                flow_state=FlowState.AWAITING_EMAIL_CODE,
                flow_step=None,
                flow_selected_type=None,
                flow_selected_day=None,
                flow_selected_slot=None,
                # Keep the pair so the same hold can resume through the code and Pix.
                flow_selected_insurance=insurance,
                flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
                booking_hold={
                    "hold_id": decision.hold_id,
                    "email_masked": decision.email_masked,
                    "when": start.strftime(WHEN_FORMAT),
                },
            )
        if decision.outcome == "blocked":
            # A deposit is due and cannot be asked for for a reason that is NOT technical (a Pix
            # already open for this conversation, no patient): the booking is NOT made and a
            # human takes the conversation (`action="handover"`). A technical failure never
            # lands here since TASK-071 Plan 3: the gate answers `commit` with
            # `deposit_waived_reason` and the clinic is e-mailed. The copy must not say the
            # clinic was alerted.
            return FlowRouterResult(
                action="handover",
                bubbles=[TextBubble(body=BEFORE_BLOCKED_TEXT)],
                flow_state=FlowState.IDLE,
                flow_step=None,
                flow_selected_type=None,
                flow_selected_day=None,
                flow_selected_slot=None,
            )
        if decision.outcome == "needs_payer_data" and decision.payer_field is not None:
            # The provider needs a payer field (decision D7). Nothing is held or charged; the
            # conversation waits for it and the answer replays THIS confirmation
            # (`workers/tasks.py`, the AWAITING_PAYER_DATA branch of the inbound ladder).
            return FlowRouterResult(
                action="reply",
                bubbles=[
                    TextBubble(
                        body=payer_data.ask_text(decision.payer_field, decision.payer_provider)
                    )
                ],
                flow_state=FlowState.AWAITING_PAYER_DATA,
                flow_step=payer_data.step_for("before", decision.payer_field, 0),
                flow_selected_type=conversation.flow_selected_type,
                flow_selected_day=conversation.flow_selected_day,
                flow_selected_slot=conversation.flow_selected_slot,
                flow_selected_professional_id=_selected_professional_id(conversation),
                flow_selected_insurance=insurance,
                flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
                flow_attendee_name=conversation.flow_attendee_name,
            )
        if decision.outcome == "awaiting_payment" and decision.payment is not None:
            payment = decision.payment
            plan = _selected_plan(tenant, insurance)
            return FlowRouterResult(
                action="reply",
                flow_state=FlowState.AWAITING_DEPOSIT,
                flow_step=None,
                # The selection is CARRIED (written unconditionally by the caller): "Trocar
                # horário" re-opens the picker from it and "Já paguei" needs nothing else.
                flow_selected_type=conversation.flow_selected_type,
                flow_selected_day=conversation.flow_selected_day,
                flow_selected_slot=conversation.flow_selected_slot,
                flow_selected_professional_id=_selected_professional_id(conversation),
                flow_selected_insurance=insurance,
                flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
                # Carried too: "Trocar horario" must not silently turn a booking for someone
                # else into one for the patient. `leave_wait` clears it when the booking ends.
                flow_attendee_name=attendee_name,
                deposit_request={
                    "deposit_id": payment.deposit_id,
                    "amount_cents": payment.amount_cents,
                    "copy_paste": payment.copy_paste,
                    "expires_at": payment.expires_at,
                    "start_at": payment.start_at,
                    "end_at": payment.end_at,
                    "service": payment.service,
                    "minutes": payment.minutes,
                    "payment_note": (
                        str(plan["note"]).strip() if plan and plan.get("note") else None
                    ),
                },
            )
        if decision.outcome == "commit" and decision.deposit_waived_reason is not None:
            # TASK-071 Plan 3: the deposit failed for a technical reason and is dropped. The
            # reservation `begin_payment` made is gone and the provider call took time, so the
            # slot is asked about AGAIN (agenda + other conversations' holds) before the event.
            if await _slot_lost_since_confirm(
                gate,
                calendar,
                start,
                end,
                professional_id,
                getattr(conversation, "id", None),
                buffer_minutes=booking_buffer_minutes(booking_owner),
            ):
                return await _reoffer_taken_slot(
                    conversation,
                    tenant,
                    calendar,
                    start,
                    duration_minutes=duration,
                    branch=BOOKING_DAY_BRANCH,
                    back_target=reoffer_back,
                    professionals=professionals,
                )
            waived = coerce_failure_reason(decision.deposit_waived_reason)
            deposit_waived_reason = waived.value if waived is not None else None
        # "commit" falls through to exactly the code that was here before.

    try:
        # Exclusive on the secretarIA agenda: the re-check above and the write become
        # one atomic step there (services/local_calendar.py); plain on Google.
        event = await create_event_exclusive(
            calendar,
            start=start, end=end, summary=summary,
            description=build_event_description(
                service=canonical_type or service_type, insurance=insurance,
                attendee_name=attendee_name,
                # TASK-083 follow-up: at a unit, the event says where and with whom.
                **event_unit_fields(unit, booking_owner),
            ),
        )
    except CalendarSlotTakenError:
        # Somebody else's booking landed between the re-check and the write: the same
        # answer as a taken re-check.
        return await _reoffer_taken_slot(
            conversation,
            tenant,
            calendar,
            start,
            duration_minutes=duration,
            branch=BOOKING_DAY_BRANCH,
            back_target=reoffer_back,
            professionals=professionals,
        )
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
            flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
            deposit_waived_reason=deposit_waived_reason,
        )

    # `canonical_type` is the catalog's own spelling, never whatever
    # `flow_selected_type` drifted into (a service renamed/deactivated
    # mid-flow). Resolved above, before the gate, so a hold and a committed
    # appointment cannot disagree about the service - see
    # services/booking_scope.py.
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
    if professional_id is not None:
        appointment["professional_id"] = professional_id
    if insurance:
        appointment["insurance"] = insurance
    # Same omit-when-absent rule: an "é pra mim" booking keeps today's dict.
    if attendee_name:
        appointment["attendee_name"] = attendee_name
    # Same omit-when-absent rule: only a booking made without its deposit carries the mark
    # the agenda shows ("Sem sinal (falha técnica)"), persisted by `flow_runner` with the row.
    if deposit_waived_reason is not None:
        appointment["deposit_waived_reason"] = deposit_waived_reason
    # Same omit-when-absent rule (TASK-083): a booking without a unit keeps today's dict.
    appointment.update(unit_kwargs)
    # ONE bubble, not two: the patient keeps a single message they can
    # screenshot or forward, and it costs one WhatsApp send instead of two.
    # The labelled blank line is what separates the link from the confirmation
    # text. The link is the PUBLIC add-to-calendar one — `event["htmlLink"]`
    # (stored above as `google_event_link`, and the right link for the clinic)
    # would open a permission error for the patient. See
    # services/calendar.py::build_patient_calendar_link.
    # TASK-008 §4.3: a custom "Outro" convênio carries its own payment
    # explanation, shown here right after the patient chose it. Catalog plans
    # have no patient-facing note (`mechanism`/`note` there are internal
    # metadata, never product copy - see models/insurance.py).
    plan = _selected_plan(tenant, insurance)
    payment_note = str(plan["note"]).strip() if plan and plan.get("note") else None
    # TASK-083 follow-up (owner, 2026-11-13): the event the patient adds from the link carries
    # the same details - service, doctor, date, unit and address - in its "Detalhes".
    link_details = build_patient_link_details(
        service=canonical_type or service_type,
        insurance=insurance,
        start=start,
        tz=calendar.tzinfo,
        attendee_name=attendee_name,
        professional=getattr(booking_owner, "name", None),
        **unit_fields(unit),
    )
    confirmation = (
        "Pronto! Seu agendamento está confirmado. ✅\n\n"
        f"{service_type}\n{_attendee_line(conversation)}"
        f"{start.strftime('%d/%m/%Y às %H:%M')}\n{confirmation_unit_lines(unit, booking_owner)}\n"
        "Adicionar à sua agenda:\n"
        + build_patient_calendar_link(
            start,
            end,
            summary,
            link_details,
            tz=calendar.tzinfo,
            location=getattr(unit, "location", None) if unit is not None else None,
        )
        + (f"\n\n💳 Sobre o pagamento do convênio:\n{payment_note}" if payment_note else "")
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


def _appointment_local_start(appt: dict, tenant: Tenant | None) -> datetime | None:
    """Display the persisted instant in the clinic zone; never rewrite the appointment."""
    start = appt.get("start_at")
    if not isinstance(start, datetime):
        return None
    timezone = ZoneInfo(getattr(tenant, "timezone", None) or "America/Sao_Paulo")
    return as_utc(start).astimezone(timezone)


def _appt_row_label(appt: dict, tenant: Tenant | None = None) -> str:
    """List-row title for one appointment: "Sáb, 28/11 10:00" (WhatsApp caps titles at 24).

    Only the day and time: with the weekday in front (TASK-087 D4) a service after them was cut
    mid-word (review P4) - the service rides in the row's description (`_appt_row_description`).
    """
    start = _appointment_local_start(appt, tenant)
    when = f"{short_day_label(start)} {start.strftime('%H:%M')}" if start is not None else "?"
    return truncate_list_row_title(when)


def _appt_row_description(appt: dict) -> str:
    """The pick-list row's description: the appointment's service, whole (up to the 72-char
    description cap)."""
    return truncate_plain(
        str(appt.get("appointment_type") or "Consulta"), MAX_LIST_ROW_DESCRIPTION_CHARS
    )


def _appt_summary(appt: dict, tenant: Tenant | None = None) -> str:
    """Full one-line description used in confirmation bubbles, in the clinic zone."""
    start = _appointment_local_start(appt, tenant)
    label = str(appt.get("appointment_type") or "Consulta")
    if start is not None:
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
    payload = _row_payload(body)
    if payload and payload.startswith(MANAGE_APPOINTMENT_PAYLOAD_PREFIX):
        # AI v2 pick lists use IDs to distinguish simultaneous appointments. Resolve
        # only against the caller's current patient-scoped list, never a global lookup.
        return _find_appt_by_id(appointments, payload[len(MANAGE_APPOINTMENT_PAYLOAD_PREFIX) :])
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


def _manage_pick_list_bubble(
    appointments: list[dict], body: str, *, tenant: Tenant | None = None
) -> SlotsBubble:
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
        (
            f"slot|{appt['start_at'].isoformat()}",
            _appt_row_label(appt, tenant),
            _appt_row_description(appt),
        )
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
            bubbles=_manage_action_card(appt, tenant),
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=STEP_MANAGE_ACTION,
            flow_managing_appointment_id=_appt_uuid(appt),
        )
    return FlowRouterResult(
        action="reply",
        bubbles=[
            _manage_pick_list_bubble(
                appointments, "Qual consulta você quer remarcar ou cancelar?", tenant=tenant
            )
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
            return _begin_cancel(managing_id, preselected, tenant=tenant)
    if not appointments:
        return _enter_manage(tenant, appointments, professionals)
    if len(appointments) == 1:
        appt = appointments[0]
        managing_id = _appt_uuid(appt)
        if intent == "reschedule":
            return await _begin_reschedule(managing_id, appt, tenant, calendar, professionals)
        return _begin_cancel(managing_id, appt, tenant=tenant)
    prompt = (
        "Qual consulta você quer remarcar?"
        if intent == "reschedule"
        else "Qual consulta você quer cancelar?"
    )
    step = STEP_MANAGE_PICK_RESCHEDULE if intent == "reschedule" else STEP_MANAGE_PICK_CANCEL
    return FlowRouterResult(
        action="reply",
        bubbles=[_manage_pick_list_bubble(appointments, prompt, tenant=tenant)],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=step,
    )


def _manage_action_card(appt: dict, tenant: Tenant | None = None) -> list:
    """The Remarcar / Cancelar / Voltar choices for the picked appointment."""
    return [
        MenuBubble(
            body=f"{_appt_summary(appt, tenant)}\n\nO que você gostaria de fazer?",
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
        _appointment_unit_calendar(calendar, appt, professionals, tenant),
        duration_minutes=(
            _appt_duration_minutes(appt, tenant)
            if appt is not None
            else (tenant.appointment_duration_min or 30)
        ),
        branch=MANAGE_DAY_BRANCH,
        professionals=professionals,
        # TASK-088 Task 5 (R31, R32): the appointment's own doctor's buffer; its event is the
        # booking being moved.
        buffer_rule=_appointment_buffer(appt, professionals),
    )


def _begin_cancel(
    managing_id: UUID | None, appt: dict | None, *, tenant: Tenant | None = None
) -> FlowRouterResult:
    """Ask Sim/Não to confirm cancelling `managing_id` (`appt` drives the summary text).

    Shared by STEP_MANAGE_ACTION's "Cancelar" branch and
    `enter_manage_action`'s single-appointment shortcut. `appt` may be None
    (e.g. a stale pick no longer present in the loaded snapshot) - the
    confirmation still proceeds with a generic summary, matching what
    STEP_MANAGE_ACTION always did.
    """
    summary = _appt_summary(appt, tenant) if appt else "essa consulta"
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


def _appointment_unit_calendar(
    calendar: CalendarService | None,
    appt: dict | None,
    professionals: list | None,
    tenant: Tenant | None = None,
) -> Any:
    """A reschedule keeps the appointment's unit (TASK-083 spec §8): its new day and time are
    offered within THAT unit's hours. An appointment without a unit (or whose unit the owner no
    longer attends at) of a doctor who has units is offered within the units' hours - the only
    one's, or their union (final wave I2, `flow_units.appointment_calendar`). Unchanged for a
    doctor without units.

    TASK-087: with `tenant`, also without the appointment's service's blocked weekdays there
    (`_appointment_service_days`)."""
    if not appt:
        return calendar
    parsed = _appointment_owner_and_unit_id(appt, professionals)
    if parsed is None:
        return calendar
    owner, unit_id = parsed
    scoped = flow_units.appointment_calendar(calendar, unit_id, owner)
    if tenant is None:
        return scoped
    return _appointment_service_days(scoped, appt, professionals, tenant)


async def _manage_step(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    appointments: list[dict],
    professionals: list | None = None,
    *,
    patient_name: str | None = None,
) -> FlowRouterResult:
    # TASK-088 Task 5 (R31, R32): every picker and recheck of this turn belongs to the
    # appointment being managed: its doctor's buffer, and its own event never pads itself.
    managed_now = _find_appt_by_id(appointments, _managing_appt_id_str(conversation))
    with moving_booking_scope(_appointment_buffer(managed_now, professionals)):
        return await _manage_step_inner(
            conversation,
            tenant,
            calendar,
            body,
            appointments,
            professionals,
            patient_name=patient_name,
        )


async def _manage_step_inner(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    appointments: list[dict],
    professionals: list | None = None,
    *,
    patient_name: str | None = None,
) -> FlowRouterResult:
    step = conversation.flow_step
    day_check: DayRefusal | None = None
    if step in (*_MANAGE_DAY_STEPS, STEP_MANAGE_SLOT, STEP_MANAGE_CONFIRM):
        managed = _find_appt_by_id(appointments, _managing_appt_id_str(conversation))
        calendar = _appointment_unit_calendar(calendar, managed, professionals, tenant)
        # TASK-087: a blocked day typed on the reschedule day step gets its reason.
        day_check = _appointment_day_check(managed, professionals, tenant)

    if step == STEP_MANAGE_PICK:
        appt = _find_appt_by_iso(appointments, body)
        if appt is None:
            return _preserve(conversation, "delegate_llm")
        return FlowRouterResult(
            action="reply",
            bubbles=_manage_action_card(appt, tenant),
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
        return _begin_cancel(_appt_uuid(appt), appt, tenant=tenant)

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
            return _begin_cancel(managing_id, appt, tenant=tenant)
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
            refuse_day=day_check,
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
            conversation,
            tenant,
            calendar,
            body,
            appointments,
            professionals,
            patient_name=patient_name,
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


def _manage_confirm_result(
    managing_id: UUID | None, appt: dict | None, start: datetime, selected_day: str | None
) -> FlowRouterResult:
    """The "Remarcar para:" Confirmar/Cancelar card - the ONE builder for the slot tap and
    the AI's manage hand-back (services/manage_request.py, TASK-030 P3).

    `start` is printed and stored as the same instant (naive ISO minute), and "Confirmar"
    on the card is routed by `_manage_reschedule` whichever path drew it.
    """
    appt_type = str(appt.get("appointment_type") or "Consulta") if appt else "Consulta"
    recap = f"Remarcar para:\n{appt_type}\n{start.strftime('%d/%m/%Y às %H:%M')}"
    return FlowRouterResult(
        action="reply",
        bubbles=[ButtonBubble(body=recap, confirm_label=LABEL_CONFIRM, cancel_label=LABEL_CANCEL)],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CONFIRM,
        flow_managing_appointment_id=managing_id,
        flow_selected_day=selected_day,
        flow_selected_slot=start.replace(tzinfo=None).isoformat(timespec="minutes"),
    )


def _manage_handle_slot(
    conversation: Conversation, appointments: list[dict], body: str
) -> FlowRouterResult:
    start = _slot_iso_from_body(body)
    if start is None:
        return _preserve(conversation, "delegate_llm")
    appt = _find_appt_by_id(appointments, _managing_appt_id_str(conversation))
    return _manage_confirm_result(
        _selected_managing_appointment_id(conversation),
        appt,
        start,
        conversation.flow_selected_day,
    )


async def _manage_reschedule(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    body: str,
    appointments: list[dict],
    professionals: list | None = None,
    *,
    patient_name: str | None = None,
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
    duration = _appt_duration_minutes(appt, tenant)
    end = start + timedelta(minutes=duration)

    # TASK-087: the consultation's service is not done that weekday (at its unit) - a card the AI
    # drew or a stale list. Refused before anything moves or is created (also the "Outro horário"
    # rebook below): the reason, then the reschedule day picker without that day.
    day_check = _appointment_day_check(appt, professionals, tenant)
    refusal = day_check(start.astimezone(calendar.tzinfo)) if day_check is not None else None
    if refusal:
        logger.info(
            "flow_reschedule_day_blocked_for_service",
            conversation_id=str(getattr(conversation, "id", None)),
        )
        return await enter_day_picker(
            conversation,
            tenant,
            calendar,
            duration_minutes=duration,
            branch=MANAGE_DAY_BRANCH,
            prefix=refusal,
            professionals=professionals,
        )

    rebook = _rebook_ids(appt)
    if rebook is not None:
        # TASK-085 R (D-R11): "Outro horário" - the consultation's event already left the
        # agenda; a NEW event is created instead of moving it.
        return await _manage_rebook(
            conversation,
            tenant,
            calendar,
            appt,
            start,
            end,
            duration=duration,
            rebook=rebook,
            professionals=professionals,
            patient_name=patient_name,
        )

    # Same last read as the booking "Confirmar" (`_handle_confirmation`), on the
    # appointment's OWN agenda - the one the patch below goes to. The event
    # being moved is excluded: it never takes its own new time. Fail-open.
    # TASK-088 Task 5: and the appointment's own doctor's buffer - the moved event is ignored.
    owner = _appointment_owner(appt, professionals)
    if await slot_taken_at_confirm(
        calendar,
        start,
        end,
        source="flow_reschedule",
        ignore_event_id=event_id,
        conversation_id=getattr(conversation, "id", None),
    ) or await _inside_buffer(
        calendar,
        start,
        end,
        owner,
        source="flow_reschedule",
        conversation_id=getattr(conversation, "id", None),
        professional_id=getattr(owner, "id", None),
        ignore_event_id=event_id,
    ):
        return await _reoffer_taken_slot(
            conversation,
            tenant,
            calendar,
            start,
            duration_minutes=duration,
            branch=MANAGE_DAY_BRANCH,
            back_target=None,
            professionals=professionals,
        )

    try:
        await update_event_exclusive(calendar, event_id, start=start, end=end)
    except CalendarSlotTakenError:
        # Taken between the re-check and the move (secretarIA agenda only): reoffer.
        return await _reoffer_taken_slot(
            conversation,
            tenant,
            calendar,
            start,
            duration_minutes=duration,
            branch=MANAGE_DAY_BRANCH,
            back_target=None,
            professionals=professionals,
        )
    except Exception as exc:
        # Any refusal to move the event (an outage, OR Google answering 404
        # because the event is not on this agenda / was deleted by hand) is a
        # job for a person. Letting a raw HttpError escape made the runner
        # delegate to the LLM, which re-opened the day picker: the patient
        # looped on "Confirmar" forever and nobody was told.
        if not isinstance(exc, CalendarUnavailableError):
            logger.warning(
                "flow_manage_reschedule_update_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
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
# TASK-085 R (D-R11, mapa-u §1.5): "Outro horário" - the old consultation of an abandoned change
# (its event already OFF the agenda) gets a new day and time through this same reschedule flow.
# --------------------------------------------------------------------------

# The one line when the old event's removal could not be confirmed (the agenda did not answer):
# nothing was created; the confirmation card follows so "Confirmar" can be tapped again.
REBOOK_RETRY_TEXT = "Não consegui agora, tente de novo em instantes."


# UA3 review I1: how many ids one window may use (the plain one + numbered salts).
REBOOK_ID_TRIES = 4


def patient_rebook_event_id(
    adjustment_id: UUID, *, calendar, start: datetime, end: datetime, salt: int = 0
) -> str:
    """The id of the event "Outro horário" creates for `adjustment_id` at [start, end).

    The hub's scheme (UH fix round 1, `api/hub/calendar.py::_hub_rebook_event_id`): UA1's
    `old_slot.rebook_event_id(source="patient")` (`difmove<hex>`, `loc_` on the secretarIA agenda)
    + 16 hex digits of sha256("<change>|<UTC start>|<UTC end>"); `salt` > 0 appends "|<n>" to the
    hashed text (a spent id of the same window). Only 0-9a-f are added: Google's base32hex holds;
    at most 58 characters."""
    first, last = (as_utc(value).astimezone(UTC).isoformat() for value in (start, end))
    window = f"{first}|{last}"
    text = f"{adjustment_id}|{window}" + (f"|{salt}" if salt else "")
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    from secretaria.services.payments import old_slot  # lazy: a heavy module, one branch only

    return old_slot.rebook_event_id(adjustment_id, calendar=calendar, source="patient") + digest


async def _rebook_id_spent(calendar, event_id: str) -> bool:
    """Whether `event_id` was created and then DELETED on this agenda (it reads `cancelled`).
    False when the agenda cannot say (the caller reports its own failure)."""
    try:
        existing = await calendar.get_event(event_id)
    except Exception as exc:  # noqa: BLE001 - cannot tell
        logger.warning("flow_rebook_event_unreadable", error_type=type(exc).__name__)
        return False
    return bool(existing) and str(existing.get("status") or "") == "cancelled"


def _event_instant(value: Any) -> datetime | None:
    if isinstance(value, dict):
        value = value.get("dateTime")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return as_utc(value) if isinstance(value, datetime) else None


def _event_in_window(event: dict, start: datetime, end: datetime) -> bool:
    """False only when the agenda's answer SAYS another window (Google `{"dateTime": ...}`, an
    ISO string or a datetime); an answer without a window cannot be checked and stands."""
    got_start, got_end = _event_instant(event.get("start")), _event_instant(event.get("end"))
    if got_start is None or got_end is None:
        return True
    return (got_start, got_end) == (as_utc(start), as_utc(end))


def _rebook_ids(appt: dict) -> tuple[UUID, UUID] | None:
    """(clinic id, change id) of a consultation listed for "Outro horário"
    (`patient_context.load_upcoming_appointments(rebooking_id=)`), else None."""
    try:
        adjustment_id = appt.get("rebook_adjustment_id")
        tenant_id = appt.get("rebook_tenant_id")
        if not adjustment_id or not tenant_id:
            return None
        return UUID(str(tenant_id)), UUID(str(adjustment_id))
    except (TypeError, ValueError):
        return None


async def _ensure_rebook_old_event_gone(tenant_id: UUID, adjustment_id: UUID) -> bool:
    """`old_slot.ensure_old_event_gone` on a session of its own (clean by construction: it
    commits). The ONE database step of this router, and only on the rebook branch: the old
    consultation's event must be off the agenda BEFORE its new one is created (UA1 fix round 1,
    I1). Never raises: False = "try again"."""
    from secretaria.core import database
    from secretaria.services.payments import old_slot

    try:
        async with database.async_session_factory() as session:
            return await old_slot.ensure_old_event_gone(
                session, tenant_id=tenant_id, adjustment_id=adjustment_id
            )
    except Exception as exc:  # noqa: BLE001 - the patient is asked to try again
        logger.warning("flow_rebook_ensure_failed", error_type=type(exc).__name__)
        return False


def _rebook_event_texts(
    appt: dict, professionals: list | None, patient_name: str | None
) -> tuple[str, str]:
    """The new event's title and description, as every event of a consultation reads
    (`service_change.agenda_summary` + `build_event_description` with the unit's lines)."""
    service = str(appt.get("appointment_type") or "Consulta")
    attendee = appt.get("attendee_name")
    owner = unit = None
    try:
        if appt.get("professional_id"):
            owner = _find_professional_by_id(professionals, UUID(str(appt["professional_id"])))
        if appt.get("unit_id"):
            unit = flow_units.find_unit(owner, UUID(str(appt["unit_id"])))
    except (TypeError, ValueError):
        owner = unit = None
    description = build_event_description(
        service=service,
        insurance=appt.get("insurance"),
        attendee_name=attendee,
        **event_unit_fields(unit, owner),
    )
    return f"{service} - {attendee or patient_name or 'Paciente'}", description


def _manage_confirm_unavailable(conversation: Conversation) -> FlowRouterResult:
    """The confirmation card's agenda answered with an outage: a person takes over and the
    conversation stays on the card (`_manage_reschedule`'s shape)."""
    return FlowRouterResult(
        action="calendar_unavailable",
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CONFIRM,
        flow_managing_appointment_id=_selected_managing_appointment_id(conversation),
        flow_selected_day=conversation.flow_selected_day,
        flow_selected_slot=conversation.flow_selected_slot,
    )


async def _manage_rebook(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService,
    appt: dict,
    start: datetime,
    end: datetime,
    *,
    duration: int,
    rebook: tuple[UUID, UUID],
    professionals: list | None,
    patient_name: str | None,
) -> FlowRouterResult:
    """"Confirmar" of "Outro horário" (mapa-u §1.5, UA1's order). 1) the old event is gone
    (`_ensure_rebook_old_event_gone`; False -> one line + the card again, nothing created);
    2) the new window is free on the consultation's own agenda (`slot_taken_at_confirm`, nothing
    ignored - the consultation has no event there now); 3) the event is created OUTSIDE any
    transaction under the id of THIS window (`patient_rebook_event_id`; a spent id is retried
    with a numbered salt, at most REBOOK_ID_TRIES; exclusively on the secretarIA agenda); an
    answer at another window is never bound nor deleted (retry text + the card). The row is
    written by `flow_runner` with `old_slot.finish_rebook` in one transaction
    (`appointment_reschedule["rebook"]`); a change decided meanwhile there discards this event.
    Same service, doctor, unit and convênio."""
    tenant_id, adjustment_id = rebook
    managing_id = _selected_managing_appointment_id(conversation)
    conversation_id = getattr(conversation, "id", None)
    if not await _ensure_rebook_old_event_gone(tenant_id, adjustment_id):
        retry = _manage_confirm_result(managing_id, appt, start, conversation.flow_selected_day)
        retry.bubbles = [TextBubble(body=REBOOK_RETRY_TEXT), *retry.bubbles]
        return retry

    async def _reoffer() -> FlowRouterResult:
        return await _reoffer_taken_slot(
            conversation,
            tenant,
            calendar,
            start,
            duration_minutes=duration,
            branch=MANAGE_DAY_BRANCH,
            back_target=None,
            professionals=professionals,
        )

    owner = _appointment_owner(appt, professionals)
    if await slot_taken_at_confirm(
        calendar, start, end, source="flow_rebook", conversation_id=conversation_id
    ) or await _inside_buffer(
        calendar,
        start,
        end,
        owner,
        source="flow_rebook",
        conversation_id=conversation_id,
        professional_id=getattr(owner, "id", None),
    ):
        return await _reoffer()
    summary, description = _rebook_event_texts(appt, professionals, patient_name)
    event = None
    for salt in range(REBOOK_ID_TRIES):
        # UA3 review I1: the id belongs to THIS window (a double tap is idempotent; another window
        # is a fresh id); a SPENT id (created and deleted by an attempt that did not stand -
        # agendas never re-use one) is retried with a numbered salt.
        event_id = patient_rebook_event_id(
            adjustment_id, calendar=calendar, start=start, end=end, salt=salt
        )
        try:
            event = await create_event_exclusive(
                calendar,
                start=start,
                end=end,
                summary=summary,
                description=description,
                event_id=event_id,
            )
        except CalendarSlotTakenError:
            return await _reoffer()
        except Exception as exc:
            # Google refuses a deleted id ("event id is taken"): only a confirmed-dead id is
            # retried; anything else is a real outage.
            if await _rebook_id_spent(calendar, event_id):
                event = None
                continue
            if not isinstance(exc, CalendarUnavailableError):
                logger.warning("flow_rebook_create_failed", error_type=type(exc).__name__)
            return _manage_confirm_unavailable(conversation)
        if str(event.get("status") or "") == "cancelled":
            # The secretarIA agenda hands the deleted row back: spent as well.
            event = None
            continue
        break
    if event is None:
        # Every id we may use for this window is spent: a person takes over.
        logger.error("flow_rebook_event_ids_spent", adjustment_id=str(adjustment_id))
        return _manage_confirm_unavailable(conversation)
    if not _event_in_window(event, start, end):
        # An event under our id at ANOTHER window (never ours to bind here, never ours to
        # delete - it may be a twin's): nothing is written; the patient tries again.
        logger.warning("flow_rebook_event_other_window", adjustment_id=str(adjustment_id))
        retry = _manage_confirm_result(managing_id, appt, start, conversation.flow_selected_day)
        retry.bubbles = [TextBubble(body=REBOOK_RETRY_TEXT), *retry.bubbles]
        return retry
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
            # The row is still found by the event it had (its id is never cleared).
            "google_event_id": str(appt.get("google_event_id") or ""),
            "start_at": start,
            "end_at": end,
            "rebook": {
                "adjustment_id": adjustment_id,
                "event_id": str(event.get("id") or event_id),
                "event_link": event.get("htmlLink"),
                # For `flow_runner`'s compensation (the event is discarded when the row is not
                # written), as `appointment_edit["calendar"]`.
                "calendar": calendar,
            },
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
        flow_insurance_auto=getattr(conversation, "flow_insurance_auto", None),
        flow_managing_appointment_id=_selected_managing_appointment_id(conversation),
        flow_selected_unit_id=flow_units.selected_unit_id(conversation),
    )


def _attendee_line(conversation: Conversation) -> str:
    """"Paciente: <name>" plus a newline on a booking for someone else, else ""."""
    name = _attendee_name(conversation)
    return f"Paciente: {name}\n" if name else ""


def _professional_line(professional: Any | None) -> str:
    """ "Profissional: <name>" plus a newline, or "" when there is nobody to name."""
    name = str(getattr(professional, "name", "") or "").strip() if professional is not None else ""
    return f"Profissional: {name}\n" if name else ""


def _unit_line(unit: Any | None) -> str:
    """ "Local: <unit> — <address>" plus a newline, or "" without a unit (TASK-083 spec §8)."""
    location = getattr(unit, "location", None) if unit is not None else None
    return f"Local: {location}\n" if location else ""


def _recap_text(
    conversation: Conversation,
    start: datetime,
    *,
    professional: Any | None = None,
    unit: Any | None = None,
) -> str:
    """The recap card body; unchanged (byte for byte) when there is no attendee, no
    `professional` (TASK-030 P3: whom the booking is with, see `_recap_professional`) and no
    `unit` (TASK-083: where)."""
    return (
        f"{conversation.flow_selected_type or 'Consulta'}\n"
        f"{_professional_line(professional)}"
        f"{_attendee_line(conversation)}"
        f"{_unit_line(unit)}"
        f"{start.strftime('%d/%m/%Y às %H:%M')}"
        f"{_replacement_line(conversation)}"
    )


def _confirmation_recap(
    conversation: Conversation, *, professional: Any | None = None, unit: Any | None = None
) -> str | None:
    """Rebuild the confirmation recap text from the stored slot, or None."""
    slot = conversation.flow_selected_slot
    if not slot:
        return None
    try:
        start = datetime.fromisoformat(slot)
    except ValueError:
        return None
    return _recap_text(conversation, start, professional=professional, unit=unit)


def _recap_professional(
    conversation: Conversation, tenant: Tenant, professionals: list | None
) -> Any | None:
    """Whom the confirmation card names (TASK-030 P3, spec §4.4.2: "agora com o médico").

    The booking's own professional (`_booking_professional`: the doctor the patient picked,
    or a single-professional clinic's only one) on clinics with the AI draft v2 switch;
    None elsewhere, so the card stays byte for byte today's until the clinic is switched on.
    """
    if not ai_draft_v2_enabled(tenant):
        return None
    return _booking_professional(conversation, professionals)


# TASK-088 R24 (the owner): money is said ONLY on the final confirmation. The lines (their wording
# is fixed here so the worker seam that adds the sinal's, `workers/shared/deposit.py`, finds and
# replaces the router's own):
#   - Particular (or a clinic that does not ask the convênio): "Valor: R$ 200,00";
#   - a convênio: no line (the service's price is the Particular one);
#   - a sinal charged for the booking (whatever the convênio): "Sinal (Pix): R$ 60,00" and
#     "Valor da consulta: R$ 200,00" - they replace the "Valor" line.
PRICE_LINE_PREFIX = "Valor: "
SINAL_LINE_PREFIX = "Sinal (Pix): "
TOTAL_LINE_PREFIX = "Valor da consulta: "
_MONEY_LINE_PREFIXES = (PRICE_LINE_PREFIX, SINAL_LINE_PREFIX, TOTAL_LINE_PREFIX)


def _money_lines(*lines: str | None) -> list[str]:
    return [line for line in lines if line]


def booking_price_line(
    conversation: Any, entry: dict | None, professional: Any | None, unit: Any | None
) -> str | None:
    """The final confirmation's service value line, or None (TASK-088 R24).

    The value the patient is shown is the price of `entry` for `professional` AT `unit` (TASK-085:
    `_priced_entry`), under the Particular rule (`service_price_text`: a convênio -> None). Only a
    readable, positive amount is a line ("a combinar" or 0 say nothing), spelled as the sinal's
    lines are (`format_brl`)."""
    if entry is None:
        return None
    shown = service_price_text(
        _priced_entry(entry, professional, getattr(unit, "id", None)),
        _selected_insurance(conversation),
    )
    cents = parse_brl_to_cents(shown)
    if cents is None or cents <= 0:
        return None
    return f"{PRICE_LINE_PREFIX}{format_brl(cents)}"


def with_money_lines(body: str, lines: list[str]) -> str:
    """`body` (a confirmation recap) ending with the money `lines`, within the card's cap.

    Any money lines already at the end are replaced (the worker replaces the router's "Valor"
    with the sinal's two; a second pass changes nothing). The money wins over the recap: when the
    whole does not fit `MAX_INTERACTIVE_BODY_CHARS`, the recap is the part that is cut."""
    kept = body.split("\n")
    while kept and (not kept[-1].strip() or kept[-1].startswith(_MONEY_LINE_PREFIXES)):
        kept.pop()
    recap = "\n".join(kept)
    if not lines:
        return truncate_plain(recap, MAX_INTERACTIVE_BODY_CHARS)
    block = "\n".join(lines)
    room = MAX_INTERACTIVE_BODY_CHARS - len(block) - 2
    return f"{truncate_plain(recap, room)}\n\n{block}"


def _confirmation_card(
    conversation: Conversation,
    start: datetime,
    *,
    professional: Any | None = None,
    unit: Any | None = None,
    money_lines: list[str] | None = None,
) -> ButtonBubble:
    """The Confirmar/Cancelar card - the ONE builder for the slot tap and the AI's express
    confirmation (services/booking_draft.py::_express_confirmation), so both read alike.

    `money_lines` (TASK-088): the value line the router can say by itself (`booking_price_line`);
    the sinal's lines are added later, by the worker seam, from the same functions the charge uses.

    "Confirmar" on it is routed by `_handle_confirmation`, whichever path drew it.
    """
    return ButtonBubble(
        body=with_money_lines(
            _recap_text(conversation, start, professional=professional, unit=unit),
            money_lines or [],
        ),
        confirm_label=LABEL_CONFIRM,
        cancel_label=LABEL_CANCEL,
    )


async def resume_bubbles(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    professionals: list | None = None,
) -> FlowRouterResult:
    """`_resume_bubbles` with attendee and convênio carried forward (`_carry_booking`)."""
    result = await _resume_bubbles(conversation, tenant, calendar, professionals)
    return _carry_booking(conversation, result)


async def _resume_bubbles(
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
    # TASK-083: the chosen unit's agenda; the unit list again when it was being asked.
    booking_owner = _booking_professional(conversation, professionals)
    booking_unit = _booking_unit(conversation, tenant, services, booking_owner)
    if step == STEP_AWAITING_NO_PLAN_AT_UNIT or (
        step == flow_units.STEP_AWAITING_UNIT
        and flow_units.selected_unit_id(conversation) is not None
    ):
        return await _handle_unit_insurance_choice(
            conversation, tenant, calendar, None, services, professionals
        )
    if step in UNIT_KEEP_STEPS and _automatic_insurance_unit_changed(conversation, booking_unit):
        return await _ask_day(conversation, tenant, calendar, services, professionals)
    # TASK-087: and without the service's blocked days at that unit.
    calendar = _booking_calendar(
        calendar, conversation, tenant, services, booking_owner, booking_unit
    )
    if step in (flow_units.STEP_AWAITING_UNIT, flow_units.STEP_AWAITING_UNIT_CONFIRM):
        # The unit list, or the Sim/Não on the only unit, again - whichever fits now.
        choice = _booking_unit_choice(conversation, tenant, services, booking_owner)
        if choice.kind in ("list", "confirm"):
            return _enter_unit_choice(conversation, booking_owner, choice)
        return await _ask_day(conversation, tenant, calendar, services, professionals)

    if step == STEP_AWAITING_PLAN_REFUSED:
        # The "não atende este convênio" card again, while it still applies; else the day step
        # decides afresh (the matrix may have changed back).
        if booking_owner is None or not conversation.flow_selected_type:
            return _menu_fallback()
        return await _ask_day(conversation, tenant, calendar, services, professionals)

    if step == STEP_AWAITING_PROFESSIONAL:
        if not _is_multi_professional(professionals):
            return _menu_fallback()
        return _enter_professional_list(
            tenant,
            professionals or [],
            insurance=_selected_insurance(conversation),
            service_name=conversation.flow_selected_type,
        )

    if step == STEP_AWAITING_ATTENDEE_CHOICE:
        return _attendee_question(conversation.flow_selected_type or ATTENDEE_NEXT_BOOK)
    if step == STEP_AWAITING_ATTENDEE_NAME:
        return _attendee_name_request(conversation.flow_selected_type)
    if step == STEP_AWAITING_ATTENDEE_AUTH:
        name = _attendee_name(conversation)
        if not name:
            return _attendee_question(conversation.flow_selected_type or ATTENDEE_NEXT_BOOK)
        return _attendee_authorization_card(conversation.flow_selected_type, name)

    if step == STEP_AWAITING_INSURANCE:
        if conversation.flow_selected_type:
            # TASK-088: the question again through the same decision as the first time - the
            # list, or continue to the days when the combination now accepts nothing.
            return await _ask_day(
                conversation, tenant, calendar, services, professionals, reask_insurance=True
            )
        return _enter_insurance(tenant, conversation, professionals=professionals)

    if step == STEP_AWAITING_CATALOG_SERVICE:
        return _enter_clinic_service_catalog(tenant, professionals or [])

    if step == STEP_AWAITING_SERVICE_PROFESSIONAL:
        offering = _professionals_offering(
            tenant, professionals or [], conversation.flow_selected_type or ""
        )
        if not offering:
            return _menu_fallback()
        return _enter_service_professional_list(
            tenant,
            professionals or [],
            conversation.flow_selected_type or "",
            insurance=_selected_insurance(conversation),
        )

    if step == STEP_AWAITING_SERVICE:
        if not services:
            return _menu_fallback()
        return _preserve_reply(
            conversation,
            [
                _service_list_bubble(
                    tenant,
                    services,
                    scope=service_scope(_selected_professional_id(conversation)),
                    professional=booking_owner,
                    insurance=_selected_insurance(conversation),
                )
            ],
        )

    if step == STEP_AWAITING_SERVICE_CONFIRM:
        service = _match_service(services, conversation.flow_selected_type)
        if service is None:
            return _preserve_reply(
                conversation,
                [
                    _service_list_bubble(
                        tenant,
                        services,
                        scope=service_scope(_selected_professional_id(conversation)),
                        professional=booking_owner,
                        insurance=_selected_insurance(conversation),
                    )
                ],
                step=STEP_AWAITING_SERVICE,
            )
        # TASK-088: the card is retired - a chat parked on it goes on to what comes after it.
        return await _ask_day(conversation, tenant, calendar, services, professionals)

    if step in _BOOKING_DAY_STEPS:
        # Re-render the picker from scratch: availability moves, and a resumed
        # conversation is exactly when it has had time to.
        return await _ask_day(conversation, tenant, calendar, services, professionals)

    if step == STEP_AWAITING_SLOT:
        # Re-list fresh slots for the saved day (availability may have changed).
        return await _relist_stored_day(conversation, tenant, calendar, services, professionals)

    if step == STEP_AWAITING_CONFIRMATION:
        unit = _booking_unit(conversation, tenant, services, booking_owner)
        recap = _confirmation_recap(
            conversation,
            professional=_recap_professional(conversation, tenant, professionals),
            unit=unit,
        )
        if recap is None:
            # Lost the slot somehow: re-ask the day.
            return await _ask_day(conversation, tenant, calendar, services, professionals)
        # The same card the slot tap drew, money line included (TASK-088 R24).
        price = booking_price_line(
            conversation,
            _booking_service_entry(conversation, tenant, services, booking_owner),
            booking_owner,
            unit,
        )
        return _preserve_reply(
            conversation,
            [
                ButtonBubble(
                    body=with_money_lines(recap, _money_lines(price)),
                    confirm_label=LABEL_CONFIRM,
                    cancel_label=LABEL_CANCEL,
                )
            ],
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


# --------------------------------------------------------------------------
# Rebooking after the DOCTOR cancelled (FEAT_34 §4)
# --------------------------------------------------------------------------
#
# Entered from a tap on the cancellation notice's buttons, decoded by
# schemas/webhook.py::extract_action_button and dispatched in
# workers/tasks.py::_handle_action_button. Both destinations land in the SHARED
# day/time branch (`enter_day_picker`, BOOKING_DAY_BRANCH) rather than growing
# a parallel one — the same rule that collapsed the reschedule duplicate.
#
# Nothing here reaches the LLM: every branch either renders a deterministic
# list or answers with a fixed line. The cancelled appointment is NOT reopened
# (it stays CANCELLED); confirming produces a brand-new booking, which is what
# the normal booking tail already does.

REBOOK_NO_OTHER_PROFESSIONAL = (
    "No momento não há outro profissional disponível para agendar. "
    "Se preferir, escolha um novo horário com o mesmo profissional."
)

# Shown when NOBODY else offers the cancelled service, so the full roster is
# offered instead. Deterministic and explicit: an unexplained short list reads
# as a bug, and an empty one reads as the clinic being closed.
REBOOK_SERVICE_UNAVAILABLE_PREFIX = (
    "Nenhum outro profissional atende esse serviço. "
    "Veja os profissionais disponíveis:"
)


def _rebooking_duration(tenant: Tenant, service_name: str | None) -> int:
    """Slot length for the rebooked service, falling back to the tenant default.

    Resolved through the catalog rather than by trusting the stored string, so
    a service renamed since the cancelled booking still sizes its own slot.
    """
    for service in active_appointment_types(tenant):
        if normalize(service.get("name")) == normalize(service_name):
            return _service_duration(service, tenant)
    return tenant.appointment_duration_min


def _rebooking_unit_and_duration(
    tenant: Tenant,
    owner: Any | None,
    service_name: str | None,
    cancelled_unit_id: UUID | None,
) -> tuple[Any | None, int]:
    """The unit the rebooked day list is read at, and its slot length (TASK-085 T8 (b)).

    The cancelled consultation's unit when `owner` still offers it for the service, else the
    owner's only unit for it; then `_service_duration_at` there (its own duration, else the
    base) - the very value the next step's slot list reads (`_booking_duration`). No owner, no
    unit link, no matching entry or a unit not decided (two or more, none on the booking):
    (None, `_rebooking_duration`), exactly as before.
    """
    base = _rebooking_duration(tenant, service_name)
    if owner is None or not flow_units.bookable_units(owner) or not service_name:
        return None, base
    entry = _match_service(professional_appointment_types(owner, tenant), service_name)
    if entry is None:
        return None, base
    service_id = entry_service_id(entry)
    unit = flow_units.find_unit(owner, cancelled_unit_id, service_id)
    if unit is None:
        choice = flow_units.unit_choice(owner, service_id)
        unit = choice.units[0] if choice.kind == "implied" else None
    if unit is None:
        return None, base
    return unit, _service_duration_at(entry, tenant, owner, unit.id)


def _rebooking_calendar(
    calendar: CalendarService | None,
    tenant: Tenant,
    professionals: list | None,
    professional_id: UUID | None,
    service_name: str | None,
    unit: Any | None = None,
) -> Any:
    """The rebooking doctor's agenda at `unit` without the service's blocked days (TASK-087, R1).

    `unit` is where the day list is read (TASK-085 T8 (b): the cancelled consultation's unit, or
    the doctor's only one for the service): its hours and THAT unit's blocked days (its own list,
    else the base) - the rule every other booking step uses (`_service_scoped`; owner decision
    2026-11-20, integration of TASK-085 + TASK-087). With no unit decided, a day stays while ANY
    unit where that doctor does the service allows it (`flow_units.blocked_weekdays_of(...,
    None)`); the unit step and the confirmation narrow it later. The service is the doctor's own
    entry for `service_name`; unknown doctor: only the unit's hours, unchanged (R5).
    """
    owner = _find_professional_by_id(professionals, professional_id)
    if owner is None:
        return _unit_calendar(calendar, unit)
    entry = _match_service(professional_appointment_types(owner, tenant), service_name)
    return _service_scoped(calendar, owner, entry, unit)


def rebooking_candidates(
    tenant: Tenant,
    professionals: list,
    *,
    cancelled_professional_id: UUID | None,
    service_name: str | None,
    services: list | None = None,
) -> tuple[list, str | None]:
    """Who the patient may rebook with, and the line explaining a fallback.

    Split out from `enter_rebooking` because the caller must resolve the
    CALENDAR of whoever ends up selected, and when exactly one candidate
    remains that choice is made here — the caller cannot know which agenda to
    open until it has this answer.

    * Everyone EXCEPT the one who cancelled: offering them back is the single
      option the patient just declined.
    * Narrowed to those who actually offer the same service, resolved through
      the catalog (`professionals_offering`) rather than by comparing the
      stored string — "Limpeza" and "limpeza dental" are the same service and
      a raw comparison would silently hide half the roster.
    * Nobody else offers it -> the FULL roster plus a fixed explanatory line.
      Never a silently short list, never an empty one.
    """
    others = [p for p in professionals if p.id != cancelled_professional_id]
    if not others:
        return [], None
    offering = (
        professionals_offering(service_name, others, tenant, services) if service_name else []
    )
    if offering:
        return offering, None
    return others, REBOOK_SERVICE_UNAVAILABLE_PREFIX


async def enter_rebooking(
    conversation: Conversation,
    tenant: Tenant,
    calendar: CalendarService | None,
    *,
    professionals: list,
    cancelled_professional_id: UUID | None,
    service_name: str | None,
    same_professional: bool,
    candidates: list | None = None,
    prefix: str | None = None,
    cancelled_unit_id: UUID | None = None,
) -> FlowRouterResult:
    """Deterministic destination for both rebooking buttons.

    `same_professional=True` is "Outro horário": keep the doctor and the
    service and go straight to the day picker — the patient chose both a
    moment ago, and asking again would be the flow forgetting what it just did.

    `same_professional=False` is "Outro médico", over the `candidates` the
    caller resolved with `rebooking_candidates`: exactly one skips the choice
    and opens their days, several render the tappable doctor list, none
    answers with a fixed line.

    The service rides along in EVERY branch, so the patient never re-picks it.
    Nothing here reaches the LLM.

    TASK-085 T8 (b): when the day list opens for a known doctor, it is read at the cancelled
    consultation's unit (`cancelled_unit_id`, when that doctor offers it) or the doctor's only
    unit - its hours and its own duration - and that unit is kept for the slot step, so a day
    shown free has times of the length the slot list offers. TASK-087 R1: the service's blocked
    days come off the same list - that unit's own, else the base; with no unit decided, only
    the days every unit of the service blocks (`_rebooking_calendar`).
    """

    async def _days_for(owner_id: UUID | None) -> FlowRouterResult:
        owner = _find_professional_by_id(professionals, owner_id)
        unit, duration = _rebooking_unit_and_duration(
            tenant, owner, service_name, cancelled_unit_id
        )
        result = await enter_day_picker(
            conversation,
            tenant,
            _rebooking_calendar(calendar, tenant, professionals, owner_id, service_name, unit),
            duration_minutes=duration,
            branch=BOOKING_DAY_BRANCH,
            professionals=professionals,
            # TASK-052: "Remarcar" on a released PAID slot says what happens to the deposit.
            prefix=prefix,
            back_target=None if same_professional else BACK_TARGET_DOCTOR_SERVICES,
            # TASK-088 Task 5 (R31): the doctor the list is read for, not the stale selection.
            buffer_rule=_booking_buffer(owner),
        )
        # enter_day_picker knows nothing about who or what is being booked, and
        # these fields are written unconditionally by the caller — so a result
        # that should keep them must carry them explicitly (FlowRouterResult).
        result.flow_selected_professional_id = owner_id
        result.flow_selected_type = service_name
        if unit is not None and result.flow_state is FlowState.SERVICE_CATALOG:
            result.flow_selected_unit_id = unit.id
        return result

    if same_professional:
        return await _days_for(cancelled_professional_id)

    pool = candidates or []
    if not pool:
        return FlowRouterResult(
            action="reply",
            bubbles=[TextBubble(body=REBOOK_NO_OTHER_PROFESSIONAL)],
            flow_state=FlowState.MENU,
        )

    if len(pool) == 1:
        # One candidate: a one-row list is ceremony, not a choice.
        return await _days_for(pool[0].id)

    result = _enter_professional_list(tenant, pool, pool=True)
    if prefix:
        result.bubbles.insert(0, TextBubble(body=prefix))
    # Keep the service so the doctor is the ONLY thing still to choose.
    result.flow_selected_type = service_name
    return result


# --------------------------------------------------------------------------
# "Cancelar" on the rebooking offer — and why (FEAT_34 §8)
# --------------------------------------------------------------------------

STEP_DECLINE_REASON = "decline_reason"

# (code, label). The CODE is what gets stored and grouped on; the label is
# only what the patient reads, so rewording it later never invalidates the
# history. Four rows plus nothing else, well inside the 10-row list cap.
DECLINE_REASONS: tuple[tuple[str, str], ...] = (
    ("other_clinic", "Vou procurar outra clínica"),
    ("no_longer_needed", "Não preciso mais"),
    ("reschedule_later", "Prefiro remarcar depois"),
    ("other", "Outro motivo"),
)

DECLINE_REASON_QUESTION = (
    "Tudo bem, não vamos remarcar. Só para entendermos melhor: "
    "por que você prefere não seguir agora?"
)
DECLINE_THANKS = "Obrigado por avisar! Se mudar de ideia, é só chamar por aqui. 🙏"
# Free text is a first-class answer, not a fallback for a failed match — the
# list cannot enumerate every reason and the spec asks for both.
DECLINE_FREE_TEXT_CODE = "free_text"


def enter_decline_reasons(appointment_id: UUID) -> FlowRouterResult:
    """Ask why, deterministically, after the patient declined to rebook.

    Never handed to the LLM: this is a fixed question with a fixed option list
    (the spec is explicit that the bot must not "conversar" about the reason),
    and the answer is a business record rather than a conversational turn.

    `flow_managing_appointment_id` carries the cancelled appointment through to
    the next turn — the answer arrives as a bare list tap with no id of its own.
    """
    rows = [(f"declreason|{code}", label) for code, label in DECLINE_REASONS]
    return FlowRouterResult(
        action="reply",
        bubbles=[
            SlotsBubble(
                body=DECLINE_REASON_QUESTION[:MAX_LIST_BODY_CHARS],
                rows=rows,
                button_label="Escolher motivo",
                section_title="Motivos",
            )
        ],
        flow_state=FlowState.MENU,
        flow_step=STEP_DECLINE_REASON,
        flow_managing_appointment_id=appointment_id,
    )


def _handle_decline_reason(conversation: Conversation, body: str | None) -> FlowRouterResult:
    """Record the answer and close the conversation politely.

    A tapped row matches one of `DECLINE_REASONS` by label (the row prefix is
    deliberately NOT one of the payload-carrying ones, so the tap arrives as
    the plain title — same trick the "Não sei" row uses). Anything else is
    kept verbatim as free text.

    The appointment id comes from the conversation, never from the message: a
    patient cannot address somebody else's booking by typing.
    """
    appointment_id = _selected_managing_appointment_id(conversation)
    text = (body or "").strip()

    reason_code = DECLINE_FREE_TEXT_CODE
    reason_text: str | None = text or None
    for code, label in DECLINE_REASONS:
        if _label_match(body, label):
            reason_code = code
            # A tapped option is fully described by its code; storing the label
            # too would just duplicate it, and "Outro motivo" carries no text.
            reason_text = None
            break

    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=DECLINE_THANKS)],
        flow_state=FlowState.MENU,
    )
    if appointment_id is not None:
        result.decline_reason = {
            "appointment_id": appointment_id,
            "reason_code": reason_code,
            "reason_text": reason_text,
        }
    return result
