"""LangChain tools for the LangGraph agent.

Each tool wraps a CalendarService method. The active CalendarService is stored
in a ContextVar so the process-wide cached agent can serve different tenants
concurrently without interference. graph.py sets the ContextVars before invoking
the agent and resets them afterwards.

Side effect of a successful booking/cancellation: a row in the `appointments`
table is created/updated so the platform has a record independent of Google
Calendar. This is wrapped so a DB hiccup never fails the calendar operation.
"""

from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import UUID

from langchain_core.tools import tool

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.services.booking_scope import (
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
    canonical_service_name,
    service_names,
)
from secretaria.services.calendar import CalendarService
from secretaria.services.precheck import HandoffOutcome, request_precheck_handoff

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

    def __init__(self, action: str) -> None:
        super().__init__(f"manage appointment: {action}")
        self.action = action


class SelectProfessionalRequested(Exception):
    """Raised by `select_professional_and_continue` (plugins/multi_professional.py).

    Carries the ACTIVE professional the tool already resolved by name;
    graph.run_agent maps it to the SELECT_PROFESSIONAL sentinel so
    workers/tasks.py re-enters the deterministic flow at that doctor's
    greeting + services (flow_router._enter_professional_services).
    """

    def __init__(self, professional_id: UUID, professional_name: str) -> None:
        super().__init__(f"select professional {professional_name}")
        self.professional_id = professional_id
        self.professional_name = professional_name


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
    (ai/graph.py::base_tools_for). This catches the paths a tool set cannot —
    a stale cached graph, a hand-rolled invocation, a future caller — and it
    returns BEFORE any Google call or DB write, so a blocked attempt can never
    land an event on the clinic-level calendar or an ownerless Appointment.
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
    from secretaria.models import Appointment, AppointmentStatus, Conversation, Patient

    appointment: Any | None = None
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
                )
                session.add(appointment)
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
    from secretaria.services.payments import deposit_lifecycle

    notice: str | None = None
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
        logger.info("tool_appointment_cancelled", event_id=event_id, rows=result.rowcount)
    except Exception as exc:
        logger.warning("tool_appointment_cancel_persist_failed", error=str(exc), event_id=event_id)
    return notice


@tool
async def check_availability(start: str, end: str) -> dict:
    """Lista eventos que conflitam com o intervalo [start, end) no calendário
    da clínica. Cada item tem id, summary, start, end (ISO 8601). Lista vazia
    significa que a janela está totalmente livre. Eventos de dia inteiro e
    eventos marcados como 'livre' são ignorados.

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
    return {"busy": busy}


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


@tool
async def create_event(
    start: str,
    end: str,
    summary: str,
    description: str = "",
    appointment_type: str = "",
) -> dict:
    """Cria um evento (consulta) no calendário da clínica. Use SOMENTE depois
    de check_availability E confirmação explícita do paciente.

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
    )

    return {
        "id": event.get("id"),
        "status": event.get("status"),
        "htmlLink": event.get("htmlLink"),
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
async def show_main_menu() -> str:
    """Volta a conversa para o menu inicial de botões da clínica. Use quando o
    paciente quiser recomeçar, "voltar ao início", trocar de profissional ou
    ver as opções de novo. Não apaga nada da conversa — apenas reabre o menu.
    """
    raise ShowMainMenuRequested()


@tool
async def manage_existing_appointment(action: str) -> dict:
    """Aciona o fluxo de remarcação/cancelamento de uma consulta JÁ MARCADA
    deste paciente. Chame SEMPRE que o paciente quiser remarcar ou cancelar
    uma consulta existente — NUNCA remarque ou cancele você mesma pelo chat
    (não use check_availability/create_event/cancel_event para isso). Esta
    ferramenta devolve o paciente ao fluxo guiado de botões, que identifica
    qual consulta (quando houver mais de uma) e executa a ação com segurança.

    Args:
        action: "reschedule" (ou "remarcar") para remarcar, "cancel" (ou
            "cancelar") para cancelar.
    """
    normalized = (action or "").strip().casefold()
    if normalized in {"reschedule", "remarcar"}:
        raise ManageAppointmentRequested("reschedule")
    if normalized in {"cancel", "cancelar"}:
        raise ManageAppointmentRequested("cancel")
    return {
        "error": (
            f"Ação '{action}' não reconhecida. Use 'reschedule' para remarcar "
            "ou 'cancel' para cancelar."
        )
    }


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
