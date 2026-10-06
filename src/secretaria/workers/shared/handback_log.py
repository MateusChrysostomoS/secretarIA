"""One structured event per AI hand-back: where did the patient land?

The agent hands a conversation back to the guided workflow through a few tools
(`set_booking_draft`, `show_main_menu`, `manage_existing_appointment`,
`select_professional_and_continue`, `start_guided_booking`, `request_human_handoff`). Each
one ends in a worker handler (`workers/shared/sentinels.py`, `workers/shared/handover.py`)
that either lands the patient on a flow step or falls back - to the main menu, to a human,
or to nothing at all. This module is the one place that records the outcome, as
`conversation_handback_entered`, so the share of patients landing on each step (and the
share bouncing to the menu) can be counted from the logs alone.

Privacy: the event carries ids, field NAMES and reason CODES. Never a value the patient or
the model wrote - a service, a convênio, a name, a time, a doctor's or a patient's UUID.
Every free-form argument is checked against the closed vocabularies below and anything
outside them is logged as "other", so a call site that passes the wrong thing cannot leak it.

Other TASK-030 plans add their codes here, in the same commit as the first call site that
uses them; tests/test_handback_log.py keeps each list in sync with its constants.
"""

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from secretaria.core.logging import get_logger
from secretaria.services.booking_scope import (
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, CHANNEL_WHATSAPP

if TYPE_CHECKING:
    from secretaria.services.flow_router import FlowRouterResult

logger = get_logger(__name__)

EVENT_NAME = "conversation_handback_entered"
# What any value outside a closed vocabulary is logged as.
OTHER = "other"

# --- source_tool: which agent tool asked for the hand-back -------------------------------
SOURCE_SET_BOOKING_DRAFT = "set_booking_draft"
SOURCE_SHOW_MAIN_MENU = "show_main_menu"
SOURCE_MANAGE_EXISTING_APPOINTMENT = "manage_existing_appointment"
SOURCE_SELECT_PROFESSIONAL = "select_professional"
SOURCE_START_GUIDED_BOOKING = "start_guided_booking"
SOURCE_REQUEST_HUMAN_HANDOFF = "request_human_handoff"
SOURCE_TOOLS = frozenset(
    {
        SOURCE_SET_BOOKING_DRAFT,
        SOURCE_SHOW_MAIN_MENU,
        SOURCE_MANAGE_EXISTING_APPOINTMENT,
        SOURCE_SELECT_PROFESSIONAL,
        SOURCE_START_GUIDED_BOOKING,
        SOURCE_REQUEST_HUMAN_HANDOFF,
    }
)

# --- landing_step -------------------------------------------------------------------------
# Normally the result's own `flow_step` (the `STEP_*` values of services/flow_router.py) or,
# when it has none, the lower-cased flow state. These pseudo steps cover landings that are
# not a place in the flow. The shape check below is all that guards the open part.
LANDING_MENU = "menu"
LANDING_HUMAN_HANDOVER = "human_handover"
LANDING_CONFIG_INCOMPLETE = "config_incomplete"
_STEP_SHAPE = re.compile(r"[a-z][a-z0-9_]{0,47}")

# --- fallback: why the hand-back did not land where the tool asked ------------------------
FALLBACK_BAD_SENTINEL = "bad_sentinel"
FALLBACK_NO_TENANT = "no_tenant"
FALLBACK_WITHOUT_FLOWS = "without_flows"
FALLBACK_NO_PATIENT = "no_patient"
FALLBACK_INVALID_SELECTION = "invalid_selection"
FALLBACK_MISSING_PROFESSIONAL = "missing_professional"
FALLBACK_UNKNOWN_PROFESSIONAL = "unknown_professional"
FALLBACK_NO_BOOKABLE_CATALOG = "no_bookable_catalog"
FALLBACK_MULTI_PROFESSIONAL = "multi_professional"
FALLBACK_NO_APPOINTMENTS = "no_appointments"
FALLBACK_CALENDAR_UNAVAILABLE = "calendar_unavailable"
FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE = "professional_config_incomplete"
FALLBACK_NO_FREE_DAYS = "no_free_days"
# TASK-030 P3: a reschedule the deposit's reschedule limit refuses (keep-or-cancel card).
FALLBACK_RESCHEDULE_LIMIT = "reschedule_limit"
FALLBACK_AMBIGUOUS_APPOINTMENT = "ambiguous_appointment"
FALLBACK_REASONS = frozenset(
    {
        FALLBACK_BAD_SENTINEL,
        FALLBACK_NO_TENANT,
        FALLBACK_WITHOUT_FLOWS,
        FALLBACK_NO_PATIENT,
        FALLBACK_INVALID_SELECTION,
        FALLBACK_MISSING_PROFESSIONAL,
        FALLBACK_UNKNOWN_PROFESSIONAL,
        FALLBACK_NO_BOOKABLE_CATALOG,
        FALLBACK_MULTI_PROFESSIONAL,
        FALLBACK_NO_APPOINTMENTS,
        FALLBACK_CALENDAR_UNAVAILABLE,
        FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE,
        FALLBACK_NO_FREE_DAYS,
        FALLBACK_RESCHEDULE_LIMIT,
        FALLBACK_AMBIGUOUS_APPOINTMENT,
    }
)

# --- field names (supplied / accepted / dropped keys) -------------------------------------
# `for_whom`, `day` and `time` are carried by the draft v2 (TASK-030 P2);
# `appointment`, `day` and `time` by the manage request v2 (P3).
FIELD_SERVICE = "service"
FIELD_PROFESSIONAL = "professional"
FIELD_INSURANCE = "insurance"
FIELD_FOR_WHOM = "for_whom"
FIELD_DAY = "day"
FIELD_TIME = "time"
FIELD_ACTION = "action"
FIELD_APPOINTMENT = "appointment"
FIELD_REASON = "reason"
FIELD_NAMES = frozenset(
    {
        FIELD_SERVICE,
        FIELD_PROFESSIONAL,
        FIELD_INSURANCE,
        FIELD_FOR_WHOM,
        FIELD_DAY,
        FIELD_TIME,
        FIELD_ACTION,
        FIELD_APPOINTMENT,
        FIELD_REASON,
    }
)

# --- dropped: why a supplied field did not survive ----------------------------------------
DROP_NOT_IN_CATALOG = "not_in_catalog"
DROP_UNKNOWN_PROFESSIONAL = "unknown_professional"
DROP_UNMATCHED_PLAN = "unmatched_plan"
# TASK-030 P2: the resolver's own reasons (services/booking_draft.py DROP_*, same strings).
DROP_NOT_OFFERED_BY_PROFESSIONAL = "not_offered_by_professional"
DROP_OUT_OF_WINDOW = "out_of_window"
DROP_DAY_UNAVAILABLE = "day_unavailable"
DROP_NO_FREE_SLOT = "no_free_slot"
DROP_MISSING_DAY = "missing_day"
# TASK-030 P3: the manage request's own (services/manage_request.py DROP_*, same strings).
DROP_UNKNOWN_APPOINTMENT = "unknown_appointment"
DROP_APPOINTMENT_NOT_CHOSEN = "appointment_not_chosen"
DROP_REASONS = frozenset(
    {
        DROP_NOT_IN_CATALOG,
        DROP_UNKNOWN_PROFESSIONAL,
        DROP_UNMATCHED_PLAN,
        DROP_NOT_OFFERED_BY_PROFESSIONAL,
        DROP_OUT_OF_WINDOW,
        DROP_DAY_UNAVAILABLE,
        DROP_NO_FREE_SLOT,
        DROP_MISSING_DAY,
        DROP_UNKNOWN_APPOINTMENT,
        DROP_APPOINTMENT_NOT_CHOSEN,
    }
)

TOPOLOGIES = frozenset(
    {
        BOOKING_TOPOLOGY_UNKNOWN,
        BOOKING_TOPOLOGY_NONE,
        BOOKING_TOPOLOGY_SOLE,
        BOOKING_TOPOLOGY_MULTI,
    }
)
CHANNELS = frozenset({CHANNEL_WHATSAPP, CHANNEL_BRAIN_MESSAGE})


def _id(value: object) -> str | None:
    return None if value is None else str(value)


def _vocab(value: object, allowed: frozenset[str]) -> str | None:
    """`value` when it is in the closed vocabulary, "other" when it is not, None for None."""
    if value is None:
        return None
    text = str(value)
    return text if text in allowed else OTHER


def _names(values: Sequence[str]) -> list[str]:
    return [name if name in FIELD_NAMES else OTHER for name in map(str, values)]


def _dropped(dropped: Mapping[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for field, reason in (dropped or {}).items():
        field_name, reason_code = str(field), str(reason)
        out[field_name if field_name in FIELD_NAMES else OTHER] = (
            reason_code if reason_code in DROP_REASONS else OTHER
        )
    return out


def _step(landing_step: str | None) -> str | None:
    if landing_step is None:
        return None
    text = str(landing_step)
    return text if _STEP_SHAPE.fullmatch(text) else OTHER


def log_handback_entered(
    *,
    conversation_id: UUID | str | None,
    tenant_id: UUID | str | None,
    source_tool: str,
    landing_step: str | None,
    supplied: Sequence[str] = (),
    accepted: Sequence[str] = (),
    dropped: Mapping[str, str] | None = None,
    fallback: str | None = None,
    topology: str | None = None,
    channel: str | None = None,
) -> None:
    """Emit the ONE `conversation_handback_entered` event for this hand-back.

    `landing_step` is None when nothing landed (no menu, no flow step). `fallback` is None on
    a clean landing and a `FALLBACK_*` code when the hand-back bounced. Never raises:
    observability must not be able to fail a turn.
    """
    try:
        logger.info(
            EVENT_NAME,
            conversation_id=_id(conversation_id),
            tenant_id=_id(tenant_id),
            source_tool=_vocab(source_tool, SOURCE_TOOLS),
            landing_step=_step(landing_step),
            supplied=_names(supplied),
            accepted=_names(accepted),
            dropped=_dropped(dropped),
            fallback=_vocab(fallback, FALLBACK_REASONS),
            topology=_vocab(topology, TOPOLOGIES),
            channel=_vocab(channel, CHANNELS),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("conversation_handback_log_failed", error_type=type(exc).__name__)


def landing_of(result: "FlowRouterResult") -> tuple[str, str | None]:
    """The `(landing_step, fallback)` a flow result implies.

    `landing_step` is the result's own `flow_step` when it has one, else the lower-cased flow
    state (`menu`, `idle`, ...). Two results are not a place in the flow at all and get a
    pseudo step plus a fallback code - the patient did not land where the tool wanted: a
    calendar outage hands them to a person, and a doctor with no configured services or
    hours ends in an alert.
    """
    if result.action == "handover":
        return LANDING_HUMAN_HANDOVER, None
    if result.action == "calendar_unavailable":
        return LANDING_HUMAN_HANDOVER, FALLBACK_CALENDAR_UNAVAILABLE
    if result.action == "professional_config_incomplete":
        return LANDING_CONFIG_INCOMPLETE, FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE
    return result.flow_step or result.flow_state.value.lower(), None
