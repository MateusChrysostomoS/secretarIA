"""LangChain tools for the LangGraph agent.

Each tool wraps a CalendarService method. The active CalendarService is stored
in a ContextVar so the process-wide cached agent can serve different tenants
concurrently without interference. graph.py sets the ContextVars before invoking
the agent and resets them afterwards.

Side effect of a successful booking/cancellation: a row in the `appointments`
table is created/updated so the platform has a record independent of Google
Calendar. This is wrapped so a DB hiccup never fails the calendar operation.
"""

import re
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import UUID

from langchain_core.tools import tool
from pseudonymize_core import has_unresolved_tokens

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.services.booking_dates import resolve_booking_day
from secretaria.services.booking_draft import BookingDraft
from secretaria.services.booking_scope import (
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
    canonical_service_name,
    service_names,
)
from secretaria.services.calendar import (
    CalendarService,
    build_event_description,
    build_patient_calendar_link,
)
from secretaria.services.manage_request import ManageRequest, parse_appointment_ref
from secretaria.services.precheck import HandoffOutcome, request_precheck_handoff
from secretaria.services.service_catalog import find_by_name, missing_from

if TYPE_CHECKING:
    from secretaria.services.tenant_config import TenantRuntimeConfig

logger = get_logger(__name__)

# Per-async-task CalendarService. Set by graph.run_agent before invoking the
# agent; each concurrent worker task has its own slot.
_calendar_ctx: ContextVar[CalendarService | None] = ContextVar("_calendar", default=None)

# Tenant + conversation the current agent turn belongs to, used to persist
# appointment rows. None in dev scripts that invoke the agent directly.
_tenant_id_ctx: ContextVar[UUID | None] = ContextVar("_tenant_id", default=None)
_conversation_id_ctx: ContextVar[UUID | None] = ContextVar("_conversation_id", default=None)

# Per-async-task TenantRuntimeConfig. Set by graph.run_agent; read here (and by
# plugin tools, e.g. plugins/multi_professional.py) to build a CalendarService
# scoped to something other than the tenant's own calendar_id (see
# `_calendar_for_calendar_id` below). Lives in this module (not graph.py) so a
# plugin tool module can import it without reaching into graph.py.
_tenant_config_ctx: ContextVar["TenantRuntimeConfig | None"] = ContextVar(
    "_tenant_config", default=None
)

# Per-async-task arq Redis pool, used ONLY to fire-and-forget enqueue
# `run_post_booking_hooks` after a successful booking (see
# `_persist_appointment` below and plugins/post_booking.py). Set by
# graph.run_agent from the `redis` it receives from workers/tasks.py;
# `None` (dev scripts, or no arq pool reachable) makes the enqueue a silent
# no-op rather than an error.
_redis_ctx: ContextVar[Any | None] = ContextVar("_redis", default=None)

# Per-async-task booking topology (services/booking_scope.py): how many ACTIVE
# professionals the tenant of the current turn books with. Set by
# graph.run_agent from the roster the worker already loaded, and read here for
# two decisions no prompt may be trusted with:
#
#   - a MULTI turn must not reach a tenant-level calendar tool at all. The
#     tool set the agent is built with already excludes them
#     (graph.base_tools_for), and the guards below are the second lock: an
#     invocation that arrives anyway fails closed WITHOUT calling Google.
#   - a SOLE turn resolves its booking owner from `TenantRuntimeConfig.
#     professional_id`, which `load_tenant_config` populates exactly when the
#     tenant has one active professional. On any other topology that field may
#     have been overlaid with a flow-selected doctor (graph.
#     _config_with_selected_professional), so it is NOT an owner for a
#     tenant-level tool and is ignored.
#
# UNKNOWN (the default: dev scripts, direct tests, a roster load that failed)
# keeps the pre-existing tenant-level behaviour and claims no owner.
_booking_topology_ctx: ContextVar[str] = ContextVar(
    "_booking_topology", default=BOOKING_TOPOLOGY_UNKNOWN
)

# Note on calendar outages: a tool that hits CalendarUnavailableError simply
# lets it propagate. LangGraph's ToolNode re-raises it out of the agent's
# ainvoke (it is not a ToolInvocationError), and graph.run_agent catches it by
# type to return the CALENDAR_UNAVAILABLE sentinel. We deliberately do NOT use
# a ContextVar flag for this: LangGraph runs each node in a COPIED context
# (contextvars.copy_context()), so a flag set inside the tool would be invisible
# to run_agent.


# TASK-038 (owner, 2026-10-07): a hand-back used to drop whatever the agent meant to
# say, so the patient only saw the same list again, as if unheard. Every hand-back
# tool now takes the agent's own short words (`message`); the exception carries them
# as `intro`, graph.run_agent puts them in front of the sentinel
# (`HANDBACK_INTRO_PREFIX`) and the worker sends them BEFORE the flow's card.
HANDBACK_MESSAGE_MAX = 600


def handback_message(message: str | None) -> str | None:
    """The patient-facing words a hand-back tool was given, trimmed and capped (or None).

    The tool boundary already re-hydrated them (ai/pii.py); a token the map could not
    resolve (the model invented or mangled one) would be printed to the patient as
    "[PACIENTE_...]", so such words are dropped - the card alone still answers.
    """
    text = (message or "").strip()[:HANDBACK_MESSAGE_MAX].rstrip()
    if text and has_unresolved_tokens(text):
        logger.warning("handback_message_unresolved_tokens", message_len=len(text))
        return None
    return text or None


class ShowMainMenuRequested(Exception):
    """Raised by the `show_main_menu` tool: the patient wants the button menu back.

    Same exception->sentinel mechanism as CalendarUnavailableError (see the
    note above): it propagates out of the LangGraph ToolNode and
    graph.run_agent maps it to SHOW_MAIN_MENU_SENTINEL for workers/tasks.py
    to act on. NOTHING is deleted — the worker only resets the conversation's
    flow fields to the menu; history and the patient row stay untouched.

    Since PROMPT_FIX_18 this is the SAME seam the `/menu` command uses
    (`workers/tasks.py::_handle_show_main_menu`, `source="agent_tool"` here vs
    `source="command"` there), so both surfaces render the identical effective
    menu and perform the identical state write. The destructive reset `/menu`
    used to trigger now lives behind the exact literal
    `/dangerously-remove-context` and is unreachable from this tool.
    """

    def __init__(self, *, intro: str | None = None) -> None:
        super().__init__("show main menu")
        self.intro = intro


class ManageAppointmentRequested(Exception):
    """Raised by `manage_existing_appointment`: the patient wants to reschedule
    or cancel an EXISTING appointment.

    Same exception->sentinel mechanism as ShowMainMenuRequested above: it
    propagates out of the LangGraph ToolNode and graph.run_agent maps it to
    MANAGE_APPOINTMENT_SENTINEL_PREFIX + `action` for workers/tasks.py to act
    on (`_handle_manage_appointment`), re-entering the deterministic manage
    (cancel/reschedule) flow via services/flow_router.py::enter_manage_action.
    The LLM itself NEVER performs the reschedule/cancel — it only requests it.
    """

    def __init__(
        self,
        action: str,
        *,
        appointment: datetime | None = None,
        day: date | None = None,
        time: Any = None,
        intro: str | None = None,
    ) -> None:
        super().__init__(f"manage appointment: {action}")
        self.action = action
        self.intro = intro
        # TASK-030 P3 (v2 tool only): WHICH appointment, by its clinic-local start, and for
        # a reschedule the new day and time. Formats only - the worker validates the rest.
        self.appointment = appointment
        self.day = day
        self.time = time

    @property
    def request(self) -> ManageRequest:
        """What graph.run_agent serializes (services/manage_request.py)."""
        return ManageRequest(
            action=self.action, appointment=self.appointment, day=self.day, time=self.time
        )


class GuidedBookingRequested(Exception):
    """Raised by `start_guided_booking`: hand the BOOKING to the button flow.

    Same exception->sentinel mechanism as the two above: it propagates out of
    the LangGraph ToolNode and graph.run_agent maps it to
    START_GUIDED_BOOKING_SENTINEL_PREFIX + the canonical service name, which
    workers/tasks.py turns into a re-entry through
    services/flow_router.py::enter_guided_booking.

    Carries the service because that is the ONLY thing the free-text
    conversation resolved that the deterministic flow would otherwise have to
    ask for again — the same reason SelectProfessionalRequested carries a
    professional. It is already canonical (the tool validates it against the
    tenant's catalog BEFORE raising); None means the clinic has no catalog to
    prove one against, which the flow handles exactly as it does elsewhere.
    """

    def __init__(self, appointment_type: str | None, *, intro: str | None = None) -> None:
        super().__init__("start guided booking")
        self.appointment_type = appointment_type
        self.intro = intro


class BookingDraftRequested(Exception):
    """Raised by `set_booking_draft` (v1 and v2): the agent resolved what the patient wants.

    Same exception->sentinel mechanism as `GuidedBookingRequested`. graph.run_agent
    serializes `draft` (services/booking_draft.py) after BOOKING_DRAFT_SENTINEL_PREFIX;
    workers/shared/sentinels.py::_handle_set_booking_draft lands it. The LLM never writes
    `flow_selected_*` itself, and never a third party's name: `attendee` is only
    "self"/"other" (TASK-030 P2).
    """

    def __init__(
        self,
        appointment_type: str | None,
        professional_id: UUID | None,
        insurance: str | None,
        *,
        attendee: str | None = None,
        day: date | None = None,
        time: Any = None,
        professional_unresolved: bool = False,
        intro: str | None = None,
    ) -> None:
        super().__init__("set booking draft")
        self.intro = intro
        self.appointment_type = appointment_type
        self.professional_id = professional_id
        self.insurance = insurance
        self.attendee = attendee
        self.day = day
        self.time = time
        self.professional_unresolved = professional_unresolved

    @property
    def draft(self) -> BookingDraft:
        return BookingDraft(
            service=self.appointment_type,
            professional_id=self.professional_id,
            insurance=self.insurance,
            attendee=self.attendee,  # type: ignore[arg-type]
            day=self.day,
            time=self.time,
            professional_unresolved=self.professional_unresolved,
        )


class SelectProfessionalRequested(Exception):
    """Raised by `select_professional_and_continue` (plugins/multi_professional.py).

    Carries the ACTIVE professional the tool already resolved by name;
    graph.run_agent maps it to the SELECT_PROFESSIONAL sentinel so
    workers/tasks.py re-enters the deterministic flow at that doctor's
    greeting + services (flow_router._enter_professional_services).
    """

    def __init__(
        self, professional_id: UUID, professional_name: str, *, intro: str | None = None
    ) -> None:
        super().__init__(f"select professional {professional_name}")
        self.professional_id = professional_id
        self.professional_name = professional_name
        self.intro = intro


def _get_calendar() -> CalendarService:
    cal = _calendar_ctx.get()
    if cal is None:
        # Fallback for dev scripts that call invoke_agent directly without
        # setting a tenant context (Fase A single-tenant convenience).
        return CalendarService()
    return cal


def _calendar_for_calendar_id(google_calendar_id: str | None) -> CalendarService:
    """Build a CalendarService scoped to a specific Google Calendar id.

    Used by plugin tools that book against something other than the tenant's
    own calendar (e.g. plugins/multi_professional.py's per-professional
    calendars). `google_calendar_id=None` (or falsy) is a deliberate no-op —
    the tenant's own calendar_id is kept, which is exactly the "falls back to
    the tenant's calendar" behavior the multi_professional addon wants when a
    professional has no calendar of their own configured.

    Falls back to `_get_calendar()` (the base ContextVar) when no
    TenantRuntimeConfig is set — same dev-script convenience as `_get_calendar`.
    """
    config = _tenant_config_ctx.get()
    if config is None:
        return _get_calendar()
    if google_calendar_id:
        config = replace(config, google_calendar_id=google_calendar_id)
    return CalendarService.from_tenant_config(config)


def _calendar_for_professional(
    *,
    google_calendar_id: str | None,
    google_refresh_token: str | None,
    business_hours: dict | None,
    appointment_duration_min: int | None = None,
) -> CalendarService:
    """Build a CalendarService for ONE professional's own config (contract v1 §10 item C).

    `google_calendar_id`/`google_refresh_token`/`business_hours`/
    `appointment_duration_min` are already resolved by the caller
    (plugins/multi_professional.py) through the professional -> tenant
    fallback chain (services/tenant_config.py); this only substitutes them
    onto the CURRENT TenantRuntimeConfig via `CalendarService.for_professional`.
    Falls back to `_calendar_for_calendar_id` (which itself falls back to
    `_get_calendar()`) when no TenantRuntimeConfig is set — same dev-script
    convenience as the rest of this module.
    """
    config = _tenant_config_ctx.get()
    if config is None:
        return _calendar_for_calendar_id(google_calendar_id)
    return CalendarService.for_professional(
        config,
        google_calendar_id=google_calendar_id,
        google_refresh_token=google_refresh_token,
        business_hours=business_hours,
        appointment_duration_min=appointment_duration_min,
    )


# Structured reasons for `agent_tool_blocked` (see `_blocked_tenant_level` and
# `_canonical_appointment_type`). Enums only — never the tool arguments, which
# carry the patient's own words.
TOOL_BLOCK_WRONG_TOPOLOGY = "wrong_topology"
TOOL_BLOCK_UNKNOWN_SERVICE = "unknown_service"
TOOL_BLOCK_AMBIGUOUS_SERVICE = "ambiguous_service"

_MULTI_PROFESSIONAL_TOOL_ERROR = (
    "Esta clínica tem vários profissionais, então esta ferramenta não pode ser "
    "usada: toda consulta pertence à agenda de UM profissional. Use "
    "list_professionals e as ferramentas *_for_professional, ou chame "
    "show_main_menu para o paciente escolher pelos botões."
)


def _blocked_tenant_level(tool_name: str) -> dict | None:
    """Fail-closed guard for the tenant-level calendar tools. None = allowed.

    Defence in depth, not the primary control: on a multi-professional tenant
    these tools are never handed to the agent in the first place
    (ai/graph.py::base_tools_for, and workers/tasks.py::_flow_handback_tools
    for `start_guided_booking`). This catches the paths a tool set cannot —
    a stale cached graph, a hand-rolled invocation, a future caller — and it
    returns BEFORE any Google call or DB write, so a blocked attempt can never
    land an event on the clinic-level calendar or an ownerless Appointment.

    `start_guided_booking` is guarded by this too even though it books
    nothing: what it OPENS is the tenant-level day picker, whose availability
    would be read off the clinic-level agenda rather than the chosen doctor's.
    Offering a multi-doctor clinic's patient days that no doctor actually has
    free is the same class of wrong, one step earlier.
    """
    if _booking_topology_ctx.get() != BOOKING_TOPOLOGY_MULTI:
        return None
    tenant_id = _tenant_id_ctx.get()
    logger.warning(
        "agent_tool_blocked",
        tool=tool_name,
        reason=TOOL_BLOCK_WRONG_TOPOLOGY,
        topology=BOOKING_TOPOLOGY_MULTI,
        tenant_id=str(tenant_id) if tenant_id else None,
    )
    return {"error": _MULTI_PROFESSIONAL_TOOL_ERROR}


def _sole_professional_id() -> UUID | None:
    """The single active professional owning THIS turn's bookings, or None.

    Non-None only on a `sole` turn — see `_booking_topology_ctx`'s note for
    why `TenantRuntimeConfig.professional_id` alone is not enough.
    """
    if _booking_topology_ctx.get() != BOOKING_TOPOLOGY_SOLE:
        return None
    config = _tenant_config_ctx.get()
    return getattr(config, "professional_id", None)


def _effective_service_catalog() -> list:
    """The catalog THIS turn books from: the resolved tenant/professional one.

    `TenantRuntimeConfig.appointment_types` is already the EFFECTIVE catalog —
    `load_tenant_config` resolves it through the single active professional's
    own services when there is one (contract v1 §10 item D), falling back to
    the tenant's. Empty for dev scripts with no config in context.
    """
    config = _tenant_config_ctx.get()
    return list(getattr(config, "appointment_types", None) or [])


def _service_guides() -> list:
    """The clinic-wide service orientations of THIS turn's tenant (read-only info).

    Unlike `_effective_service_catalog` this is NOT what a booking is validated against: it
    holds every active catalog service that has orientations AND that somebody offers,
    including ones only reachable through a professional's own list (a 2+ professional clinic's
    effective catalog is the tenant's). Empty for dev scripts with no config in context.
    """
    config = _tenant_config_ctx.get()
    return list(getattr(config, "service_guides", None) or [])


def _canonical_appointment_type(
    candidate: str, tool_name: str, catalog: list | None = None
) -> tuple[str | None, dict | None]:
    """Resolve `candidate` to a catalog service name. Returns (name, error).

    `Appointment.appointment_type` is read downstream as a CATALOG KEY (the
    Pix deposit prices a booking by exact name — see
    services/payments/deposit_lifecycle.py::_price_text_for_appointment), so a
    free-text title must never be stored there:

      - a name that matches an active service resolves to the catalog's own
        spelling;
      - an unmatched name is refused (the tool errors, listing the valid
        options, and the model can correct itself inside the same turn)
        rather than booking something unpriceable;
      - an OMITTED name is derived only when the catalog leaves no room for
        doubt — exactly one active service. With several, the model must say
        which one;
      - a clinic with NO active services has nothing to prove a type against,
        so the booking proceeds with no type at all. NULL is honest; the free
        Calendar title would not be.

    `catalog` defaults to the current turn's effective catalog; the
    professional-aware tools pass the resolved professional's own instead
    (plugins/multi_professional.py), so each tool validates against exactly
    the catalog it books from.
    """
    catalog = _effective_service_catalog() if catalog is None else catalog
    names = service_names(catalog)
    text = (candidate or "").strip()
    if not names:
        return None, None
    if not text:
        if len(names) == 1:
            return names[0], None
        logger.info(
            "agent_tool_blocked", tool=tool_name, reason=TOOL_BLOCK_AMBIGUOUS_SERVICE
        )
        return None, {
            "error": (
                "Informe appointment_type com o nome EXATO de um dos serviços da "
                f"clínica: {', '.join(names)}."
            )
        }
    canonical = canonical_service_name(catalog, text)
    if canonical is None:
        logger.info("agent_tool_blocked", tool=tool_name, reason=TOOL_BLOCK_UNKNOWN_SERVICE)
        return None, {
            "error": (
                f"Serviço '{text}' não existe nesta clínica. Serviços "
                f"disponíveis: {', '.join(names)}."
            )
        }
    return canonical, None


def _match_by_name(items: Sequence[Any], name: str) -> Any | None:
    """Case-insensitive exact match of `name` against `item.name` in `items`.

    Shared by plugin tools that resolve a professional/unit by the name the
    LLM heard from the patient (plugins/multi_professional.py,
    plugins/multi_unit.py). Returns None when nothing matches — callers build
    their own "valid options" error message from the full `items` list.
    """
    target = name.strip().casefold()
    for item in items:
        if item.name.strip().casefold() == target:
            return item
    return None


def _lookup_service(items: Sequence[Any], name: str) -> Any | None:
    """A service of `items` by the name the patient or the LLM used.

    `_match_by_name` first — the exact, case-insensitive rule `get_service_info` has always
    used, so anything it matched still resolves to the very same entry — then the catalog's own
    identity rule (`service_catalog.find_by_name`), which also ignores accents and inner
    spacing, so "ressonancia magnetica" finds "Ressonância Magnética".
    """
    found = _match_by_name(items, name)
    return found if found is not None else find_by_name(items, name)


def _event_window(
    event: dict, fallback_start: datetime, fallback_end: datetime
) -> tuple[datetime, datetime]:
    """Authoritative booked window from Google's response, else the inputs.

    Google's event payload carries RFC3339 datetimes with an offset, so they
    are timezone-aware (safe for a TIMESTAMPTZ column). The fallbacks must be
    made tz-aware by the caller.
    """
    try:
        start = datetime.fromisoformat(event["start"]["dateTime"])
        end = datetime.fromisoformat(event["end"]["dateTime"])
        return start, end
    except (KeyError, TypeError, ValueError):
        return fallback_start, fallback_end


def _localize_window(start: str, end: str, cal: CalendarService) -> tuple[datetime, datetime]:
    """Parse LLM-supplied ISO 8601 start/end and localize naive values to `cal`'s tz.

    Shared by every create_event*-shaped tool (base `create_event` here, plus
    plugins/multi_professional.py's `create_event_for_professional` and
    plugins/multi_unit.py's `create_event_at_unit`) so a fallback persist still
    gets tz-aware values for the TIMESTAMPTZ columns even if Google's response
    is unexpectedly missing the authoritative window.
    """
    parsed_start = datetime.fromisoformat(start)
    parsed_end = datetime.fromisoformat(end)
    if parsed_start.tzinfo is None:
        parsed_start = parsed_start.replace(tzinfo=cal.tzinfo)
    if parsed_end.tzinfo is None:
        parsed_end = parsed_end.replace(tzinfo=cal.tzinfo)
    return parsed_start, parsed_end


async def _persist_appointment(
    event: dict,
    fallback_start: datetime,
    fallback_end: datetime,
    appointment_type: str | None,
    professional_id: UUID | None = None,
    unit_id: UUID | None = None,
    source: str = "agent",
    attendee_name: str | None = None,
) -> None:
    """Record a bot-created appointment. Best-effort: never raises.

    Skipped silently when no tenant context is set (dev scripts). A DB failure
    is logged but does not undo the (already successful) Google Calendar event.

    `appointment_type` must ALREADY be a canonical catalog name (or None when
    the clinic has no catalog to prove one against) — callers resolve it via
    `_canonical_appointment_type` / `services/booking_scope.canonical_service_name`.
    It is never the Google event title: that is `summary`, a different
    concept, and everything downstream reads this column as a catalog key.

    `professional_id` is the booking's owner: the resolved single active
    professional for the base tools (`_sole_professional_id`), the named one
    for the multi_professional plugin tools. `unit_id` is set only by the
    multi_unit plugin tool. `source` names the surface for the structured
    logs below and never reaches the DB.
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return
    conversation_id = _conversation_id_ctx.get()
    # Imported lazily to keep this module importable without a DB/ORM in the
    # dev terminal, and to avoid an import cycle through models -> services.
    from secretaria.core.database import async_session_factory
    from secretaria.models import Appointment, AppointmentStatus, Conversation, Patient, Tenant
    from secretaria.services import reminder_hooks

    appointment: Any | None = None
    plan_reminders = False
    try:
        async with async_session_factory() as session:
            async with session.begin():
                patient_id: UUID | None = None
                phone: str | None = None
                if conversation_id is not None:
                    conversation = await session.get(Conversation, conversation_id)
                    if conversation is not None:
                        patient_id = conversation.patient_id
                        if patient_id is not None:
                            patient = await session.get(Patient, patient_id)
                            if patient is not None:
                                phone = patient.wa_id
                start_at, end_at = _event_window(event, fallback_start, fallback_end)
                appointment = Appointment(
                    tenant_id=tenant_id,
                    patient_id=patient_id,
                    conversation_id=conversation_id,
                    google_event_id=event.get("id") or "",
                    google_event_link=event.get("htmlLink"),
                    appointment_type=(appointment_type or "").strip()[:120] or None,
                    start_at=start_at,
                    end_at=end_at,
                    phone=phone,
                    status=AppointmentStatus.SCHEDULED,
                    professional_id=professional_id,
                    unit_id=unit_id,
                    attendee_name=attendee_name or None,
                )
                session.add(appointment)
                # TASK-032 R2: read the clinic's reminder switch in this same
                # transaction; the hook itself runs after the commit, below.
                plan_reminders = reminder_hooks.enabled_for(await session.get(Tenant, tenant_id))
                # The attendee belonged to THIS booking: consumed here, so the
                # patient's next chat booking ("agora uma pra mim") is theirs.
                # Also consumes the "pra mim" marker (""), not only a third party's
                # name, so the next chat booking is asked pra-quem again.
                if conversation_id is not None:
                    booked_conversation = await session.get(Conversation, conversation_id)
                    if booked_conversation is not None:
                        booked_conversation.flow_attendee_name = None
        logger.info("tool_appointment_persisted", event_id=event.get("id"))
    except Exception as exc:
        # The calendar event already exists; a missing DB row is recoverable
        # and must not break the booking the patient just made.
        logger.warning("tool_appointment_persist_failed", error=str(exc), event_id=event.get("id"))
        return

    # Fire-and-forget: enqueue post_booking plugin hooks (EHR push, Pix
    # deposit ask, analytics event, ...) off the hot path. Never awaited
    # inline and never allowed to affect the booking reply — see
    # plugins/post_booking.py.
    if appointment is not None:
        from secretaria.plugins.post_booking import enqueue_post_booking_hooks

        # Ids and enums only (mirrors workers/tasks.py::_log_booking_scope for
        # the deterministic flow, so both surfaces answer "did this booking
        # get an owner and a real service?" with one query).
        logger.info(
            "booking_owner_resolved",
            tenant_id=str(tenant_id),
            appointment_id=str(appointment.id),
            professional_id=str(professional_id) if professional_id else None,
            has_owner=professional_id is not None,
            source=source,
        )
        logger.info(
            "booking_service_resolved",
            tenant_id=str(tenant_id),
            appointment_id=str(appointment.id),
            professional_id=str(professional_id) if professional_id else None,
            has_type=bool(appointment.appointment_type),
            source=source,
        )
        await enqueue_post_booking_hooks(
            _redis_ctx.get(), tenant_id, appointment.id, source="agent"
        )
        if plan_reminders:
            await reminder_hooks.after_appointment_booked(appointment.id)


async def _mark_appointment_cancelled(event_id: str) -> str | None:
    """Flip the matching appointment row(s) to CANCELLED. Best-effort.

    Returns the Pix deposit `cancellation_notice`
    (services/payments/deposit_lifecycle.py) when a deposit existed and was
    resolved by this cancellation, so `cancel_event` can relay honest
    refund/retention info back to the patient through the agent; None when
    there is nothing deposit-related to say (no deposit, or the lookup/hook
    itself failed — never blocks the cancellation, which has already
    happened by the time any of this runs).
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        # No tenant context (dev scripts): skip rather than issue a
        # tenant-unscoped UPDATE that could touch other tenants' rows
        # (google_event_id is indexed but not globally unique).
        return None
    if not event_id:
        return None
    from sqlalchemy import select, update

    from secretaria.core.database import async_session_factory
    from secretaria.models import Appointment, AppointmentStatus, Tenant
    from secretaria.services import reminder_hooks
    from secretaria.services.payments import deposit_lifecycle

    notice: str | None = None
    closed_id = None
    try:
        async with async_session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(Appointment)
                    .where(
                        Appointment.google_event_id == event_id,
                        Appointment.tenant_id == tenant_id,
                    )
                    .values(status=AppointmentStatus.CANCELLED)
                )
                appointment = await session.scalar(
                    select(Appointment).where(
                        Appointment.google_event_id == event_id,
                        Appointment.tenant_id == tenant_id,
                    )
                )
                if appointment is not None:
                    tenant = await session.get(Tenant, tenant_id)
                    if tenant is not None and reminder_hooks.enabled_for(tenant):
                        closed_id = appointment.id
                    if tenant is not None:
                        outcome = await deposit_lifecycle.on_appointment_cancelled(
                            session, tenant=tenant, appointment=appointment
                        )
                        if outcome is not None:
                            deposit = await deposit_lifecycle.get_deposit_for_appointment(
                                session, appointment.id
                            )
                            if deposit is not None:
                                notice = deposit_lifecycle.cancellation_notice(
                                    outcome, tenant, deposit
                                )
        if closed_id is not None:
            await reminder_hooks.after_appointment_closed(closed_id, reason="cancelled")
        logger.info("tool_appointment_cancelled", event_id=event_id, rows=result.rowcount)
    except Exception as exc:
        logger.warning("tool_appointment_cancel_persist_failed", error=str(exc), event_id=event_id)
    return notice


async def _own_google_event_ids(event_ids: list[str]) -> set[str]:
    """Quais destes eventos do Google pertencem ao paciente DESTA conversa.

    O vínculo é a tabela `appointments`, que guarda `google_event_id` nos DOIS
    caminhos de agendamento (o do agente, `_persist_appointment`, e o do fluxo
    de botões, `workers/tasks.py`) — nunca o título do evento, que é texto
    livre digitado por qualquer pessoa com acesso à agenda.

    FALHA FECHADA: sem contexto de paciente, ou com o banco fora do ar, devolve
    conjunto VAZIO — ou seja, trata todo evento como de terceiro e esconde
    tudo. O erro barato é a agente dizer "ocupado" sem detalhe; o caro é contar
    a um paciente o nome de outro.
    """
    tenant_id = _tenant_id_ctx.get()
    conversation_id = _conversation_id_ctx.get()
    if not event_ids or tenant_id is None or conversation_id is None:
        return set()
    # Imported lazily, same reason as _persist_appointment above.
    from sqlalchemy import select

    from secretaria.core.database import async_session_factory
    from secretaria.models import Appointment, Conversation

    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, conversation_id)
            patient_id = conversation.patient_id if conversation is not None else None
            if patient_id is None:
                return set()
            rows = await session.scalars(
                select(Appointment.google_event_id).where(
                    Appointment.tenant_id == tenant_id,
                    Appointment.patient_id == patient_id,
                    Appointment.google_event_id.in_(event_ids),
                )
            )
            return {row for row in rows if row}
    except Exception as exc:
        logger.warning(
            "tool_own_event_ids_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return set()


async def _busy_without_foreign_details(busy: list[dict]) -> list[dict]:
    """Deixa passar o evento do próprio paciente; o resto vira só o intervalo.

    A agenda da clínica é compartilhada: o título de um evento que não é deste
    paciente é, quase sempre, o NOME DE OUTRO PACIENTE
    ("Consulta - Maria Silva"). Devolvê-lo à LLM é entregar dado de saúde de
    terceiro a quem não tem nada com isso — e o prompt pedia justamente que a
    agente comentasse o conflito, então vazava por desenho, não por acidente.

    O `id` do Google também some junto: além de não servir para nada aqui, é o
    argumento que `cancel_event` aceita, e um id de terceiro na mão do modelo é
    um cancelamento alheio a uma alucinação de distância.
    """
    if not busy:
        return []
    own = await _own_google_event_ids([str(e.get("id") or "") for e in busy])
    out: list[dict] = []
    for event in busy:
        if str(event.get("id") or "") in own:
            out.append({**event, "do_paciente": True})
        else:
            out.append({"start": event.get("start"), "end": event.get("end"), "do_paciente": False})
    return out


@tool
async def check_availability(start: str, end: str) -> dict:
    """Lista os intervalos ocupados dentro de [start, end) na agenda da clínica.
    Lista vazia significa que a janela está totalmente livre. Eventos de dia
    inteiro e eventos marcados como 'livre' são ignorados.

    Cada item tem start, end (ISO 8601) e `do_paciente`:

    - `do_paciente: false` — compromisso de OUTRA pessoa. Você recebe SOMENTE o
      intervalo, de propósito. Diga apenas que o horário está ocupado e ofereça
      alternativas. NUNCA especule de quem é, do que se trata, nem invente
      qualquer detalhe sobre ele.
    - `do_paciente: true` — é uma consulta DESTE paciente. Aí sim vêm id e
      summary, e você pode citá-los ("você já tem consulta nesse horário").

    Args:
        start: Início da janela em ISO 8601 (ex: 2026-05-27T14:00:00).
        end: Fim da janela em ISO 8601.
    """
    blocked = _blocked_tenant_level("check_availability")
    if blocked is not None:
        return blocked
    busy = await _get_calendar().check_availability(
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
    )
    return {"busy": await _busy_without_foreign_details(busy)}


@tool
async def list_free_slots(day: str, max_slots: int = 6) -> dict:
    """Lista até `max_slots` horários livres dentro do horário comercial da
    clínica para o dia especificado. Use quando o paciente pedir um dia inteiro
    (\"tem horário sexta?\") ou quando quiser oferecer alternativas. Renderize
    a resposta usando o marcador [SLOTS] do sistema.

    Args:
        day: Dia no formato YYYY-MM-DD (ex: 2026-05-29).
        max_slots: Quantidade máxima de slots a retornar (default 6, máx 10).
    """
    blocked = _blocked_tenant_level("list_free_slots")
    if blocked is not None:
        return blocked
    target_day = date.fromisoformat(day)
    day_dt = datetime.combine(target_day, datetime.min.time())
    slots = await _get_calendar().list_free_slots(
        day=day_dt,
        max_slots=min(max(max_slots, 1), 10),
    )
    return {"slots": slots}


async def _conversation_attendee_name() -> str | None:
    """This conversation's authorized attendee (flow_attendee_name), or None.

    Best-effort like `_persist_appointment`: a failed read books for the
    patient themself, exactly as before the attendee existed.
    """
    conversation_id = _conversation_id_ctx.get()
    if conversation_id is None or _tenant_id_ctx.get() is None:
        return None
    from secretaria.core.database import async_session_factory
    from secretaria.models import Conversation

    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, conversation_id)
            return conversation.flow_attendee_name if conversation is not None else None
    except Exception as exc:
        logger.warning("tool_attendee_lookup_failed", error_type=type(exc).__name__)
        return None


async def _conversation_insurance() -> str | None:
    """Read the current conversation's selected insurance, best effort."""
    conversation_id = _conversation_id_ctx.get()
    if conversation_id is None or _tenant_id_ctx.get() is None:
        return None
    from secretaria.core.database import async_session_factory
    from secretaria.models import Conversation

    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, conversation_id)
            return conversation.flow_selected_insurance if conversation is not None else None
    except Exception as exc:
        logger.warning("tool_insurance_lookup_failed", error_type=type(exc).__name__)
        return None


@tool
async def create_event(
    start: str,
    end: str,
    summary: str,
    description: str = "",
    appointment_type: str = "",
) -> dict:
    """Cria um evento (consulta) no calendário da clínica. Use SOMENTE depois
    de check_availability E confirmação explícita do paciente. Agenda sempre
    para QUEM ESTÁ CONVERSANDO: se a consulta for para outra pessoa, não use
    esta ferramenta — chame show_main_menu (o fluxo de botões pede o nome do
    atendido e a autorização para compartilhar os dados).

    Args:
        start: Início em ISO 8601 (ex: 2026-05-27T14:00:00).
        end: Fim em ISO 8601.
        summary: Título do evento no Google Agenda, ex: 'Consulta - João
            Silva'. É só o título da agenda — NÃO use este campo para dizer
            qual é o serviço.
        description: Notas adicionais (opcional).
        appointment_type: Nome EXATO do serviço da clínica que está sendo
            agendado (um dos "Tipos de consulta disponíveis" do seu contexto,
            ex: 'Primeira Consulta'). Não invente, não traduza e não misture
            com o nome do paciente. Se a clínica tiver só um serviço, pode
            deixar em branco.
    """
    blocked = _blocked_tenant_level("create_event")
    if blocked is not None:
        return blocked
    # Resolved BEFORE the calendar call: an unprovable service must not leave
    # an orphan Google event behind.
    canonical_type, error = _canonical_appointment_type(appointment_type, "create_event")
    if error is not None:
        return error

    cal = _get_calendar()
    fallback_start, fallback_end = _localize_window(start, end, cal)
    # A pra-quem answer the patient gave in the button flow before drifting
    # here (services/attendee.py). The model never books for a third party by
    # itself (ai/prompts.py sends it to show_main_menu), but when the flow has
    # already recorded an AUTHORIZED attendee for this booking, the event is
    # theirs: titled deterministically, never left to the model's wording.
    attendee_name = await _conversation_attendee_name()
    if attendee_name:
        summary = f"{canonical_type or 'Consulta'} - {attendee_name}"
    insurance = await _conversation_insurance()
    auto_description = build_event_description(
        service=canonical_type, insurance=insurance, attendee_name=attendee_name
    )
    description = (
        f"{auto_description}\n\n{description}".strip() if description else auto_description
    )
    event = await cal.create_event(
        start=fallback_start,
        end=fallback_end,
        summary=summary,
        description=description,
    )
    await _persist_appointment(
        event,
        fallback_start,
        fallback_end,
        canonical_type,
        professional_id=_sole_professional_id(),
        attendee_name=attendee_name,
    )

    return {
        "id": event.get("id"),
        "status": event.get("status"),
        # The event on the CLINIC's calendar. Kept because it is what
        # `_persist_appointment` stores on `Appointment.google_event_link`,
        # which the doctor's hub and the booking email open — and it is
        # exactly the WRONG link to hand a patient, who has no access to that
        # agenda. The prompt is explicit about which of the two to send.
        "htmlLink": event.get("htmlLink"),
        # The public "add to my calendar" link, the one that goes to the
        # PATIENT — see services/calendar.py::build_patient_calendar_link.
        "patient_calendar_link": build_patient_calendar_link(
            fallback_start,
            fallback_end,
            summary,
            description,
            tz=cal.tzinfo,
        ),
    }


@tool
async def cancel_event(event_id: str) -> dict:
    """Cancela (deleta) um evento existente pelo seu id. Se o resultado trouxer
    um campo "note", repasse essa frase ao paciente literalmente — é a
    informação honesta sobre reembolso/retenção do sinal (Pix), quando houver.

    Args:
        event_id: ID do evento no Google Calendar.
    """
    blocked = _blocked_tenant_level("cancel_event")
    if blocked is not None:
        return blocked
    await _get_calendar().cancel_event(event_id)
    notice = await _mark_appointment_cancelled(event_id)
    result: dict = {"status": "cancelled"}
    if notice:
        # Honest refund/retention info (Pix deposit) for the agent to relay
        # verbatim to the patient — see _mark_appointment_cancelled.
        result["note"] = notice
    return result


@tool
async def show_main_menu(message: str = "") -> str:
    """Volta a conversa para o menu inicial de botões da clínica. Use quando o
    paciente quiser recomeçar, "voltar ao início", trocar de profissional ou
    ver as opções de novo. Não apaga nada da conversa — apenas reabre o menu.

    Args:
        message: UMA ou duas frases curtas para o paciente, enviadas ANTES dos botões:
            responda o que ele perguntou ou contou, sem anunciar qual lista vem a
            seguir (o fluxo decide e mostra logo abaixo). Deixe vazio só se não
            houver nada a dizer.
    """
    raise ShowMainMenuRequested(intro=handback_message(message))


@tool
async def manage_existing_appointment(action: str, message: str = "") -> dict:
    """Aciona o fluxo de remarcação/cancelamento de uma consulta JÁ MARCADA
    deste paciente. Chame SEMPRE que o paciente quiser remarcar ou cancelar
    uma consulta existente — NUNCA remarque ou cancele você mesma pelo chat
    (não use check_availability/create_event/cancel_event para isso). Esta
    ferramenta devolve o paciente ao fluxo guiado de botões, que identifica
    qual consulta (quando houver mais de uma) e executa a ação com segurança.

    Args:
        action: "reschedule" (ou "remarcar") para remarcar, "cancel" (ou
            "cancelar") para cancelar.
        message: UMA ou duas frases curtas para o paciente, enviadas ANTES dos botões:
            responda o que ele perguntou ou contou, sem anunciar qual lista vem a
            seguir (o fluxo decide e mostra logo abaixo). Deixe vazio só se não
            houver nada a dizer.
    """
    normalized = (action or "").strip().casefold()
    if normalized in {"reschedule", "remarcar"}:
        raise ManageAppointmentRequested("reschedule", intro=handback_message(message))
    if normalized in {"cancel", "cancelar"}:
        raise ManageAppointmentRequested("cancel", intro=handback_message(message))
    return {
        "error": (
            f"Ação '{action}' não reconhecida. Use 'reschedule' para remarcar "
            "ou 'cancel' para cancelar."
        )
    }


@tool
async def start_guided_booking(appointment_type: str, message: str = "") -> dict:
    """Entrega o agendamento ao fluxo guiado de botões, abrindo direto a lista
    de dias disponíveis (ou a pergunta de convênio, quando a clínica pede).
    A partir daí o fluxo de botões conduz dia, horário e confirmação — você
    para de marcar pelo chat.

    OPCIONAL, por sua iniciativa: NÃO é um passo obrigatório do agendamento.
    Continuar perguntando dia e horário em texto e chamar create_event no fim
    também é válido. Use esta ferramenta quando o paciente já disse QUAL
    serviço quer e quer ver horários — os botões são mais rápidos e não erram
    horário. Se o paciente já pediu um dia específico ("tem horário sexta?"),
    prefira list_free_slots; se ele estiver aberto ("quando tem vaga?"),
    prefira esta.

    Args:
        appointment_type: Nome EXATO do serviço da clínica que será agendado
            (um dos "Tipos de consulta disponíveis" do seu contexto, ex:
            'Primeira Consulta'). Não invente, não traduza e não misture com
            o nome do paciente.
        message: UMA ou duas frases curtas para o paciente, enviadas ANTES dos botões:
            responda o que ele perguntou ou contou, sem anunciar qual lista vem a
            seguir (o fluxo decide e mostra logo abaixo). Deixe vazio só se não
            houver nada a dizer.
    """
    blocked = _blocked_tenant_level("start_guided_booking")
    if blocked is not None:
        return blocked
    # Validated BEFORE the hand-back for the same reason create_event does it:
    # the deterministic flow would carry this straight into
    # `Appointment.appointment_type`, which is read downstream as a CATALOG
    # KEY. An unmatched name errors recoverably here (the model can correct
    # itself inside the same turn) instead of parking the patient in a button
    # flow scoped to a service the clinic does not sell.
    canonical_type, error = _canonical_appointment_type(
        appointment_type, "start_guided_booking"
    )
    if error is not None:
        return error
    raise GuidedBookingRequested(canonical_type, intro=handback_message(message))


_PATIENT_UNRESOLVED_TEXT = (
    "Não consegui identificar o cadastro deste paciente nesta conversa. "
    "NUNCA afirme que ele não tem consulta marcada — ofereça o menu "
    "(show_main_menu) para ele verificar pelos botões."
)


@tool
async def list_patient_appointments() -> dict:
    """Lista as consultas FUTURAS já marcadas DESTE paciente nesta clínica.
    Use para responder com dados reais a perguntas como "tenho consulta
    marcada?" ou "quando é minha consulta?". Ferramenta SOMENTE-LEITURA: ela
    informa, não marca, não cancela e não remarca. Quando o paciente quiser
    remarcar ou cancelar uma consulta listada, chame show_main_menu para ele
    seguir pelo fluxo de botões do menu.

    Não recebe argumentos: paciente e clínica são resolvidos automaticamente
    a partir da conversa atual.
    """
    tenant_id = _tenant_id_ctx.get()
    conversation_id = _conversation_id_ctx.get()
    if tenant_id is None or conversation_id is None:
        # Dev scripts / missing context: same rule as the opening gate — an
        # unresolved patient must NEVER read as "no appointments".
        logger.warning("tool_list_patient_appointments_no_context")
        return {"error": _PATIENT_UNRESOLVED_TEXT}
    # Imported lazily, same reason as _persist_appointment above.
    from secretaria.core.database import async_session_factory
    from secretaria.models import Conversation
    from secretaria.services.patient_context import as_utc, load_upcoming_appointments

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        patient_id = conversation.patient_id if conversation is not None else None
        if patient_id is None:
            logger.warning(
                "tool_list_patient_appointments_no_patient", tenant_id=str(tenant_id)
            )
            return {"error": _PATIENT_UNRESOLVED_TEXT}
        rows = await load_upcoming_appointments(session, tenant_id, patient_id)

    # Deliberately NO ids in the payload (not even google_event_id): this tool
    # informs; acting on an appointment routes through show_main_menu, never
    # through cancel_event on an id the model saw here.
    tz = _get_calendar().tzinfo
    appointments = [
        {
            "quando": as_utc(row["start_at"]).astimezone(tz).strftime("%d/%m/%Y às %H:%M"),
            "tipo": row["appointment_type"] or "Consulta",
        }
        for row in rows
    ]
    logger.info(
        "tool_list_patient_appointments",
        tenant_id=str(tenant_id),
        count=len(appointments),
    )
    return {
        "appointments": appointments,
        "count": len(appointments),
        "nota": "Somente leitura — para remarcar ou cancelar, chame show_main_menu.",
    }


async def _resolve_patient_phone() -> str | None:
    """Best-effort patient WhatsApp id (phone) for the current turn.

    Mirrors `_persist_appointment`'s phone resolution: `_conversation_id_ctx`
    (set by graph.run_agent) -> Conversation.patient_id -> Patient.wa_id.
    Returns None when there is no conversation context (dev scripts) or
    nothing resolves — callers must treat that as "can't proceed", not crash.
    """
    conversation_id = _conversation_id_ctx.get()
    if conversation_id is None:
        return None
    # Imported lazily, same reason as _persist_appointment above.
    from secretaria.core.database import async_session_factory
    from secretaria.models import Conversation, Patient

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.patient_id is None:
            return None
        patient = await session.get(Patient, conversation.patient_id)
        return patient.wa_id if patient is not None else None


# Precheck hand-off tool guidance text (PT-BR), one per `HandoffOutcome` plus
# the "feature not configured for this clinic" pre-check. Kept as module
# constants so the outcome->text mapping is easy to audit/test in one place.
_PRECHECK_UNAVAILABLE_TEXT = (
    "A pré-consulta ainda não está disponível para esta clínica no momento. "
    "Você pode seguir normalmente com o agendamento."
)
_PRECHECK_NOT_ENTITLED_TEXT = "Esse recurso de pré-consulta não está disponível para esta clínica."
_PRECHECK_CONFLICT_TEXT = (
    "O paciente já tem uma pré-consulta em andamento no número da PreCheck. "
    "Oriente-o a continuar por lá em vez de abrir uma nova conversa."
)
_PRECHECK_TEMPORARY_FAILURE_TEXT = (
    "Não foi possível iniciar a pré-consulta agora (instabilidade temporária). "
    "Tente novamente em alguns instantes."
)


@tool
async def iniciar_pre_consulta() -> str:
    """Inicia a pré-consulta (anamnese) do paciente desta clínica, encaminhando-o
    por WhatsApp para o número da PreCheck. Use SOMENTE quando o paciente
    precisar preencher o questionário de pré-consulta desta clínica antes da
    consulta marcada — este recurso é restrito a essa tarefa específica desta
    clínica (política da Meta), não é uma ferramenta de mensageria genérica.

    Não recebe argumentos: o telefone do paciente e a clínica são resolvidos
    automaticamente a partir da conversa atual.
    """
    settings = get_settings()
    if not settings.PRECHECK_WHATSAPP_NUMBER:
        return _PRECHECK_UNAVAILABLE_TEXT

    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        # Dev scripts / no tenant context: nothing to check entitlement
        # against, so behave exactly like "not configured for this clinic".
        logger.warning("tool_precheck_handoff_no_tenant_context")
        return _PRECHECK_UNAVAILABLE_TEXT

    phone = await _resolve_patient_phone()
    if not phone:
        logger.warning("tool_precheck_handoff_no_patient_phone", tenant_id=str(tenant_id))
        return _PRECHECK_TEMPORARY_FAILURE_TEXT

    result = await request_precheck_handoff(tenant_id, phone)

    if result.outcome in (HandoffOutcome.SEEDED, HandoffOutcome.ALREADY_ACTIVE):
        link = (
            f"https://wa.me/{settings.PRECHECK_WHATSAPP_NUMBER}"
            f"?text={quote(settings.PRECHECK_HANDOFF_PREFILL)}"
        )
        return (
            "Pré-consulta liberada para o paciente. Envie este link para que ele "
            f"continue no WhatsApp da pré-consulta: {link}"
        )
    if result.outcome in (HandoffOutcome.NOT_ENTITLED, HandoffOutcome.NO_CLINIC):
        return _PRECHECK_NOT_ENTITLED_TEXT
    if result.outcome is HandoffOutcome.CONFLICT:
        return _PRECHECK_CONFLICT_TEXT
    return _PRECHECK_TEMPORARY_FAILURE_TEXT


@tool
async def set_booking_draft(
    service: str = "", professional: str = "", insurance: str = "",
    for_whom: str = "", day: str = "", time: str = "", message: str = "",
) -> dict:
    """Registra o que o paciente já disse (serviço, profissional, convênio) e entrega
    o agendamento ao fluxo guiado, que PULA as etapas já respondidas e abre a próxima
    que falta. Se o paciente quer uma avaliação/consulta, mas não escolheu um serviço
    ou médico, chame com ambos vazios: o fluxo mostra as escolhas administrativas.
    Nunca deduza um procedimento a partir de sintomas.

    Args:
        service: Nome EXATO de um serviço escolhido pelo paciente (ou vazio).
        professional: Nome do profissional, quando o paciente já escolheu um (ou vazio).
        insurance: Convênio que o paciente citou, se citou (ou vazio).
            Sem convênio / particular / não tenho convênio: use "Particular".
        for_whom: "me" se disse que é para si; "other" se é para outra pessoa;
            vazio se não informou. Nunca envie nome de paciente aqui.
        day: Dia explicitamente informado, AAAA-MM-DD, ou expressão como 07/10,
            amanhã, quinta da semana que vem. Use a data e o calendário da clínica
            no prompt. Não invente nem perca a data já escolhida.
        time: HH:MM, apenas quando informado. Ao receber só o horário, envie
            também o day já escolhido no ESTADO DA CONVERSA.
        message: UMA ou duas frases curtas para o paciente, enviadas ANTES dos botões:
            responda o que ele perguntou ou contou, sem anunciar qual lista vem a
            seguir (o fluxo decide e mostra logo abaixo); se nenhuma opção da
            clínica corresponde exatamente ao que ele descreveu, diga isso.
            Deixe vazio só se não houver nada a dizer.
    """
    # Existing three-field calls retain their old validation/landing. Only an
    # explicit date/attendee preference opts into the complete capture request.
    if any((value or "").strip() for value in (for_whom, day, time)):
        normalized_day = (day or "").strip()
        if normalized_day:
            config = _tenant_config_ctx.get()
            timezone = config.timezone if config is not None else get_settings().CLINIC_TIMEZONE
            try:
                normalized_day = resolve_booking_day(normalized_day, timezone=timezone).isoformat()
            except ValueError:
                logger.info("agent_tool_blocked", tool="set_booking_draft", reason="bad_day")
                return {"error": "Não consegui identificar a data. Peça o dia e mês sem adivinhar."}
        return await set_booking_draft_v2.coroutine(
            service=service, professional=professional, insurance=insurance,
            for_whom=for_whom, day=normalized_day, time=time, message=message,
        )
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return {"error": "Nenhuma clínica configurada para esta conversa."}
    service = (service or "").strip()
    professional = (professional or "").strip()
    insurance = (insurance or "").strip()

    intro = handback_message(message)
    if not service and not professional:
        raise BookingDraftRequested(None, None, insurance or None, intro=intro)

    professional_id: UUID | None = None
    catalog = _effective_service_catalog()
    if _booking_topology_ctx.get() == BOOKING_TOPOLOGY_MULTI:
        # Lazy: plugins import this module, so the reverse import must not be top-level.
        from secretaria.plugins.multi_professional import (
            _active_professionals,
            _professional_services,
            _unknown_professional_error,
        )

        professionals = await _active_professionals(tenant_id)
        chosen = None
        if professional:
            matches = [
                p for p in professionals
                if p.name.strip().casefold() == professional.casefold()
            ]
            if len(matches) > 1:
                return {
                    "error": "Mais de um profissional tem esse nome. Peça uma escolha inequívoca."
                }
            chosen = _match_by_name(professionals, professional)
            if chosen is None:
                return _unknown_professional_error(professional, professionals)
        elif service:
            offering = []
            for candidate in professionals:
                offered = await _professional_services(tenant_id, candidate)
                if canonical_service_name(offered, service) is not None:
                    offering.append(candidate)
            if not offering:
                return {"error": f"Nenhum profissional da clínica oferece '{service}'."}
            if len(offering) > 1:
                names = ", ".join(p.name for p in offering)
                return {
                    "error": (
                        f"Mais de um profissional atende '{service}': {names}. "
                        "Pergunte com quem o paciente prefere e chame de novo."
                    )
                }
            chosen = offering[0]
        else:
            return {"error": "Informe ao menos o serviço ou o profissional."}
        professional_id = chosen.id
        catalog = await _professional_services(tenant_id, chosen)
    elif not service:
        return {"error": "Informe o serviço (nome exato de um dos serviços da clínica)."}

    canonical_type: str | None = None
    if service:
        canonical_type, error = _canonical_appointment_type(service, "set_booking_draft", catalog)
        if error is not None:
            return error
    raise BookingDraftRequested(canonical_type, professional_id, insurance or None, intro=intro)


# --- set_booking_draft v2 (TASK-030 P2) ----------------------------------------------------
# Same model-facing NAME as the v1 tool above; the clinic's switch picks one
# (`flow_router.ai_draft_v2_enabled`, workers/shared/llm_context.py::_flow_handback_tools).
# It checks FORMATS only. Whether a service, convênio or doctor exists is the worker-side
# resolver's call (services/booking_draft.py), which drops just the invalid item and asks
# from that step - so "not in the catalog" is never an error the model would answer in text.
TOOL_BLOCK_BAD_FOR_WHOM = "bad_for_whom"
TOOL_BLOCK_BAD_DAY = "bad_day"
TOOL_BLOCK_BAD_TIME = "bad_time"
_FOR_WHOM_VALUES: dict[str, str | None] = {"": None, "me": "self", "other": "other"}
_FOR_WHOM_ERROR = (
    'for_whom aceita só "me" (a consulta é para o próprio paciente), "other" (é para outra '
    "pessoa) ou vazio (ele não disse). Nunca escreva um nome nesse campo."
)
_DAY_FORMAT_ERROR = "day precisa estar no formato AAAA-MM-DD (ex.: 2026-10-08), no fuso da clínica."
_TIME_FORMAT_ERROR = "time precisa estar no formato HH:MM (ex.: 10:00)."
_ISO_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HHMM_RE = re.compile(r"\d{2}:\d{2}")
_DRAFT_TEXT_MAX = 120


async def _draft_professional_id(tenant_id: UUID, name: str) -> UUID | None:
    """The ACTIVE professional with exactly this name, or None (counted, never an error)."""
    if not name:
        return None
    # Lazy: plugins import this module, so the reverse import must not be top-level.
    from secretaria.plugins.multi_professional import _active_professionals

    roster = await _active_professionals(tenant_id)
    matches = [p for p in roster if p.name.strip().casefold() == name.casefold()]
    if not matches:
        # The patient may say only a first name. A subset of complete name tokens
        # is safe only when it identifies exactly one ACTIVE professional.
        def tokens(value: str) -> set[str]:
            return set(re.findall(r"[^\W\d_]+", value.casefold())) - {"dr", "dra"}

        requested = tokens(name)
        if requested:
            matches = [p for p in roster if requested <= tokens(p.name)]
    if len(matches) == 1:
        return matches[0].id
    logger.info(
        "booking_draft_tool_item_dropped",
        field="professional",
        reason="ambiguous_professional" if matches else "unknown_professional",
    )
    return None


@tool("set_booking_draft")
async def set_booking_draft_v2(
    service: str = "",
    professional: str = "",
    insurance: str = "",
    for_whom: str = "",
    day: str = "",
    time: str = "",
    message: str = "",
) -> dict:
    """Entrega o agendamento ao fluxo guiado com TUDO o que o paciente já disse. O fluxo
    confere cada item com os dados reais da clínica e abre a próxima etapa que falta - pode
    ir até a lista de horários do dia pedido. Preencha só o que o paciente disse e deixe o
    resto vazio; não repita uma pergunta que ele já respondeu. Nunca deduza um procedimento
    a partir de sintomas e nunca invente dia ou horário.

    Args:
        service: Nome EXATO de um serviço da clínica escolhido pelo paciente (ou vazio).
        professional: Nome do profissional que o paciente escolheu (ou vazio).
        insurance: Convênio que o paciente citou (ou vazio).
        for_whom: "me" se a consulta é para o próprio paciente, "other" se é para outra
            pessoa, vazio se ele não disse. NUNCA escreva um nome aqui.
        day: Dia pedido no formato AAAA-MM-DD, no fuso da clínica (ou vazio).
        time: Horário pedido no formato HH:MM (ou vazio). Sem `day`, é ignorado.
        message: UMA ou duas frases curtas para o paciente, enviadas ANTES dos botões:
            responda o que ele perguntou ou contou, sem anunciar qual lista vem a
            seguir (o fluxo decide e mostra logo abaixo); se nenhuma opção da
            clínica corresponde exatamente ao que ele descreveu, diga isso.
            Deixe vazio só se não houver nada a dizer.
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return {"error": "Nenhuma clínica configurada para esta conversa."}
    who = (for_whom or "").strip().casefold()
    if who not in _FOR_WHOM_VALUES:
        # The value itself is never logged nor echoed: it may be a third party's name.
        logger.info("agent_tool_blocked", tool="set_booking_draft", reason=TOOL_BLOCK_BAD_FOR_WHOM)
        return {"error": _FOR_WHOM_ERROR}
    day_text = (day or "").strip()
    parsed_day: date | None = None
    if day_text:
        try:
            if not _ISO_DAY_RE.fullmatch(day_text):
                raise ValueError(day_text)
            parsed_day = date.fromisoformat(day_text)
        except ValueError:
            logger.info("agent_tool_blocked", tool="set_booking_draft", reason=TOOL_BLOCK_BAD_DAY)
            return {"error": _DAY_FORMAT_ERROR}
    time_text = (time or "").strip()
    parsed_time = None
    if time_text:
        try:
            if not _HHMM_RE.fullmatch(time_text):
                raise ValueError(time_text)
            parsed_time = datetime.strptime(time_text, "%H:%M").time()
        except ValueError:
            logger.info("agent_tool_blocked", tool="set_booking_draft", reason=TOOL_BLOCK_BAD_TIME)
            return {"error": _TIME_FORMAT_ERROR}
    professional_id = await _draft_professional_id(tenant_id, (professional or "").strip())
    raise BookingDraftRequested(
        (service or "").strip()[:_DRAFT_TEXT_MAX] or None,
        professional_id,
        (insurance or "").strip()[:_DRAFT_TEXT_MAX] or None,
        attendee=_FOR_WHOM_VALUES[who],
        day=parsed_day,
        time=parsed_time,
        professional_unresolved=bool((professional or "").strip()) and professional_id is None,
        intro=handback_message(message),
    )


# Read by ai/graph.py::_tool_cache_key: the v1 and v2 tools share the name
# "set_booking_draft" and must never share a compiled agent.
set_booking_draft_v2.metadata = {"cache_variant": "draft_v2"}


# --- manage_existing_appointment v2 (TASK-030 P3) ---------------------------------------------
# Same model-facing NAME as the v1 tool above; the clinic's switch picks one
# (`flow_router.ai_draft_v2_enabled`, workers/shared/llm_context.py::_flow_handback_tools).
# Formats only, like set_booking_draft v2: whether the appointment is THIS patient's and
# whether the new time is free is the worker's call (services/manage_request.py), which
# never goes further than the confirmation card the buttons show.
TOOL_BLOCK_BAD_ACTION = "bad_action"
TOOL_BLOCK_BAD_APPOINTMENT = "bad_appointment"
_MANAGE_ACTIONS: dict[str, str] = {
    "reschedule": "reschedule",
    "remarcar": "reschedule",
    "cancel": "cancel",
    "cancelar": "cancel",
}
_MANAGE_ACTION_ERROR = (
    "Ação não reconhecida. Use 'reschedule' para remarcar ou 'cancel' para cancelar."
)
_APPOINTMENT_REF_ERROR = (
    'appointment precisa ser a referência da consulta que aparece em "consultas marcadas" '
    "(ref AAAA-MM-DD HH:MM), ou vazio."
)


def _tool_day(text: str) -> date | None:
    """`day` as the hand-back tools accept it (AAAA-MM-DD); None when empty, else ValueError."""
    value = (text or "").strip()
    if not value:
        return None
    if not _ISO_DAY_RE.fullmatch(value):
        raise ValueError("day must be YYYY-MM-DD")
    return date.fromisoformat(value)


def _tool_time(text: str) -> Any:
    """`time` as the hand-back tools accept it (HH:MM); None when empty, else ValueError."""
    value = (text or "").strip()
    if not value:
        return None
    if not _HHMM_RE.fullmatch(value):
        raise ValueError("time must be HH:MM")
    return datetime.strptime(value, "%H:%M").time()


@tool("manage_existing_appointment")
async def manage_existing_appointment_v2(
    action: str, appointment: str = "", day: str = "", time: str = "", message: str = ""
) -> dict:
    """Leva o paciente ao fluxo de remarcar ou cancelar uma consulta JÁ MARCADA dele. O
    fluxo confere tudo e para no cartão de confirmação: quem confirma é o paciente, tocando
    no botão. NUNCA remarque nem cancele você mesma pelo chat.

    Args:
        action: "reschedule" (ou "remarcar") ou "cancel" (ou "cancelar").
        appointment: QUAL consulta, pela referência "(ref AAAA-MM-DD HH:MM)" mostrada em
            "consultas marcadas". Vazio se o paciente não disse qual.
        day: Só para remarcar: o novo dia, AAAA-MM-DD, no fuso da clínica (ou vazio).
        time: Só para remarcar: o novo horário, HH:MM (ou vazio). Sem `day`, é ignorado.
        message: UMA ou duas frases curtas para o paciente, enviadas ANTES dos botões:
            responda o que ele perguntou ou contou, sem anunciar qual lista vem a
            seguir (o fluxo decide e mostra logo abaixo). Deixe vazio só se não
            houver nada a dizer.
    """
    canonical = _MANAGE_ACTIONS.get((action or "").strip().casefold())
    if canonical is None:
        # The value is never echoed nor logged: it may carry the patient's own words.
        logger.info(
            "agent_tool_blocked", tool="manage_existing_appointment", reason=TOOL_BLOCK_BAD_ACTION
        )
        return {"error": _MANAGE_ACTION_ERROR}
    reference_text = (appointment or "").strip()
    try:
        reference = parse_appointment_ref(reference_text) if reference_text else None
    except ValueError:
        logger.info(
            "agent_tool_blocked",
            tool="manage_existing_appointment",
            reason=TOOL_BLOCK_BAD_APPOINTMENT,
        )
        return {"error": _APPOINTMENT_REF_ERROR}
    new_day = new_time = None
    if canonical == "reschedule":
        try:
            new_day = _tool_day(day)
        except ValueError:
            logger.info(
                "agent_tool_blocked", tool="manage_existing_appointment", reason=TOOL_BLOCK_BAD_DAY
            )
            return {"error": _DAY_FORMAT_ERROR}
        try:
            new_time = _tool_time(time)
        except ValueError:
            logger.info(
                "agent_tool_blocked", tool="manage_existing_appointment", reason=TOOL_BLOCK_BAD_TIME
            )
            return {"error": _TIME_FORMAT_ERROR}
    raise ManageAppointmentRequested(
        canonical,
        appointment=reference,
        day=new_day,
        time=new_time,
        intro=handback_message(message),
    )


# Read by ai/graph.py::_tool_cache_key: the v1 and v2 tools share the name
# "manage_existing_appointment" and must never share a compiled agent.
manage_existing_appointment_v2.metadata = {"cache_variant": "manage_v2"}


HANDOFF_REASONS = ("patient_requested_human", "clinical_sensitive", "could_not_help")


class HumanHandoffRequested(Exception):
    """Raised by `request_human_handoff`: the agent asks for a person.

    Same exception->sentinel mechanism as the other hand-backs. Carries only a
    reason CODE (an enum) — never patient text — so nothing sensitive can ride
    into the operational e-mail that follows.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"human handoff: {reason}")
        self.reason = reason


class HumanHandoffOfferRequested(Exception):
    """Raised by `offer_human_handoff` (and by `request_human_handoff` for "could_not_help").

    Owner, 2026-10-07 (TASK-038): when the agent lacks what it needs to answer, it does
    not hand the patient to a person behind their back - the flow ASKS, with the fixed
    card `services/flow_router.py::HUMAN_OFFER_BODY` and ✅ Sim / ❌ Não. Same
    exception->sentinel path as the other hand-backs; `intro` is the agent's one-line
    "what I don't have", sent in front of the card.
    """

    def __init__(self, *, intro: str | None = None) -> None:
        super().__init__("offer human handoff")
        self.intro = intro


@tool
async def get_service_info(service_name: str) -> dict:
    """Consulta as orientações de UM serviço da clínica: o que o paciente precisa saber antes
    (jejum, exames, documentos, preparo), a descrição completa, a duração e o preço.

    Use quando o paciente perguntar o que precisa fazer ou levar para um serviço, quanto dura
    ou o que inclui. Ferramenta SOMENTE-LEITURA: não agenda nada. Se duracao_min ou preco vierem
    vazios, não invente: diga que não tem essa informação e chame offer_human_handoff.

    Args:
        service_name: Nome do serviço como aparece na lista de serviços da clínica.
    """
    asked = service_name or ""
    catalog = _effective_service_catalog()
    # Two sources, because the effective catalog only lists what THIS turn books from (the
    # tenant's list, or the one professional's), while the orientations are clinic-wide.
    match = _lookup_service(catalog, asked)
    if match is not None:
        return {
            "servico": match.name,
            "duracao_min": match.duration_min,
            "preco": match.price,
            "descricao": match.description,
            "descricao_completa": getattr(match, "long_description", None),
            "orientacoes": list(getattr(match, "requirements", None) or []),
        }
    guides = _service_guides()
    guide = _lookup_service(guides, asked)
    if guide is None:
        listed = [t.name for t in catalog] + [g.name for g in missing_from(guides, catalog)]
        return {
            "error": (
                f"Serviço '{asked.strip()}' não existe nesta clínica. "
                f"Serviços disponíveis: {', '.join(listed) or 'nenhum'}."
            )
        }
    # Known to the clinic but not in this turn's catalog: duration and price vary per
    # professional, so they are not guessed here.
    return {
        "servico": guide.name,
        "duracao_min": None,
        "preco": None,
        "descricao": guide.description,
        "descricao_completa": guide.long_description,
        "orientacoes": list(guide.requirements or []),
    }


@tool
async def request_human_handoff(reason: str) -> dict:
    """ÚLTIMO RECURSO: passa a conversa DIRETO para uma pessoa da equipe. Use SOMENTE
    quando o paciente pediu explicitamente para falar com uma pessoa, ou quando o assunto
    exige avaliação humana. Se você apenas não tem a informação ou não consegue fazer o
    que ele pediu, use offer_human_handoff (pergunta ao paciente antes). NUNCA por uma
    dúvida comum sobre a clínica, serviços, horários ou convênios.

    Args:
        reason: Exatamente um de: "patient_requested_human" (o paciente pediu uma
            pessoa), "clinical_sensitive" (assunto que exige avaliação humana).
    """
    normalized = (reason or "").strip()
    if normalized not in HANDOFF_REASONS:
        return {"error": f"Motivo inválido. Use um de: {', '.join(HANDOFF_REASONS)}."}
    if normalized == "could_not_help":
        # TASK-038: "I could not help" never transfers behind the patient's back -
        # the flow asks first (offer_human_handoff's card).
        raise HumanHandoffOfferRequested()
    raise HumanHandoffRequested(normalized)


@tool
async def offer_human_handoff(message: str = "") -> dict:
    """Use quando você NÃO tem a informação ou NÃO consegue fazer o que o paciente pediu
    (ex.: um preço ou dado que não está no seu contexto, um pedido fora do que você
    resolve por aqui). O sistema mostra ao paciente a pergunta padrão "Não sou capaz de
    atender essa sua necessidade por aqui. Quer que eu chame nosso atendente humano?"
    com os botões Sim/Não; se ele tocar Sim, a equipe assume a conversa. NUNCA diga
    "vou confirmar com a equipe" ou "vou verificar" sem chamar esta ferramenta. Antes,
    tente de verdade com o que você sabe.

    Args:
        message: UMA frase curta dizendo o que você não tem ou não consegue (ex.: "Não
            tenho o valor dessa cirurgia aqui."), enviada antes da pergunta. Deixe vazio
            se não houver nada a dizer.
    """
    raise HumanHandoffOfferRequested(intro=handback_message(message))
