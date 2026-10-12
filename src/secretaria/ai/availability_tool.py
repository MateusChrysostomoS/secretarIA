"""`get_availability`: the AI's one agenda read - free WINDOWS, never events (TASK-030 P4).

Spec §4.6. On the v2 toolset (`flow_router.ai_draft_v2_enabled`) the AI loses
`check_availability`, `list_free_slots` and every tool that writes to the agenda
(ai/tools.py `AI_TOOLSET_V2_WITHHELD`, `AI_TOOLSET_V2_STAGING`; its `create_event` /
`cancel_event` there only stage the confirmation card, ai/staging_tools.py). What it keeps,
to answer "tem horário semana que vem?", is this tool, and the answer has exactly one shape:

    {"windows": [{"day": "2026-10-08", "start": "08:00", "end": "11:20"}, ...],
     "timezone": "America/Sao_Paulo", "slot_minutes": 40,
     "day_from": "2026-10-05", "day_to": "2026-10-18"}      (+ `professional`, `clamped`, `note`)

The calendar's events never get this far: the free windows are computed by
`services/availability_windows.py` from `services/availability.py` (the same "free" the day
and slot pickers and the draft resolver use: business hours, busy events and held slots
already subtracted), and neither of them ever returns an event - no title, attendee, id,
description, link or busy block exists in this module's data. tests/test_get_availability_tool.py
serializes the answer and fails on any key outside the documented set.

Tenant isolation: the roster, the catalog, the agenda and the holds are all read for
`_tenant_id_ctx` - the tenant of THIS turn - and the agenda is built by the very resolution
the workflow uses (`plugins.multi_professional._professional_calendar` ->
`services/tenant_config.py::resolve_professional_calendar`, the only place a Google refresh
token is decrypted; a single-professional clinic reads the agenda `run_agent` built from the
same config). The environment-variable calendar `ai/tools.py::_get_calendar` falls back to
for dev scripts is deliberately NOT used here.

Failure policy (decided, tested): a calendar outage (`CalendarUnavailableError`, a revoked
token included) propagates, exactly as `check_availability` and `list_free_slots` always
did - graph.run_agent maps it to the CALENDAR_UNAVAILABLE sentinel, the worker hands the
conversation to a person and alerts the clinic owner, and a revoked token must keep
reaching that owner. Anything else that goes wrong is an error dict the model can read
(and a count-only log line), never a crash of the turn.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from langchain_core.tools import tool

from secretaria.ai.tools import (
    _calendar_ctx,
    _conversation_id_ctx,
    _effective_service_catalog,
    _tenant_config_ctx,
    _tenant_id_ctx,
)
from secretaria.core.logging import get_logger
from secretaria.services.availability_windows import (
    CLAMP_BOOKING_WINDOW,
    CLAMP_MAX_DAYS,
    CLAMP_MAX_WINDOWS,
    MAX_DAYS,
    MAX_WINDOWS,
    DayRange,
    RangeError,
    WindowScan,
    resolve_range,
    scan_free_windows,
)
from secretaria.services.booking_hold import BookingGate
from secretaria.services.booking_scope import (
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_SOLE,
    booking_topology,
    canonical_service_name,
    resolve_booking_owner_id,
    service_entry_name,
    service_names,
)
from secretaria.services.calendar import CalendarService, CalendarUnavailableError
from secretaria.services.flow_router import DAY_PICKER_WINDOW_DAYS

logger = get_logger(__name__)

TOOL_NAME = "get_availability"

# Why the tool refused (stable enums for `agent_tool_blocked`; never the arguments, which
# carry the patient's own words). The date reasons are availability_windows.RANGE_*.
BLOCK_NO_CLINIC = "no_clinic"
BLOCK_TENANT_MISMATCH = "tenant_mismatch"
BLOCK_NO_CALENDAR = "no_calendar"
BLOCK_NO_PROFESSIONALS = "no_professionals"
BLOCK_PROFESSIONAL_REQUIRED = "professional_required"
BLOCK_PROFESSIONAL_UNKNOWN = "professional_unknown"
BLOCK_PROFESSIONAL_AMBIGUOUS = "professional_ambiguous"
BLOCK_SERVICE_UNKNOWN = "service_unknown"
BLOCK_NO_PROFESSIONAL_OFFERS_SERVICE = "no_professional_offers_service"
BLOCK_SEVERAL_PROFESSIONALS_OFFER_SERVICE = "several_professionals_offer_service"

_TEMPORARY_ERROR = (
    "Não consegui consultar a agenda agora. Diga ao paciente que vai olhar de novo em instantes "
    "e NUNCA invente horários."
)


class _Refusal(Exception):
    """A request the tool cannot serve. `message` is what the model reads."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


@dataclass
class _Target:
    """Whose agenda is read, with what, for how long."""

    calendar: CalendarService
    professional: Any | None  # the roster row; None on a clinic without professionals
    owner_id: UUID | None  # whose holds hide slots: the agenda a booking would land on
    slot_minutes: int


def _clinic_today(tz) -> date:
    """The clinic's date. Its own function so a test can pin the clock.

    The CLINIC's, not the server's: at 22:30 in America/Sao_Paulo the UTC date is already
    tomorrow.
    """
    return datetime.now(tz).date()


def _names(items: list) -> str:
    return ", ".join(str(getattr(item, "name", item)) for item in items)


def _entry_minutes(entry: Any) -> int | None:
    """A catalog entry's duration, whichever shape it travels in; None when unusable."""
    raw = (
        entry.get("duration_min")
        if isinstance(entry, dict)
        else getattr(entry, "duration_min", None)
    )
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return None
    return minutes if minutes > 0 else None


async def _pick_professional(tenant_id: UUID, roster: list, name: str, service: str):
    """The roster row on a multi-professional clinic, plus THEIR catalog.

    Resolved like the draft does (`set_booking_draft`): by exact name, case-insensitive; or,
    with no name, the one professional who offers the service. Several, none, or no hint at
    all is a refusal listing the valid names - never a guess.
    """
    # Lazy: plugins import ai.tools, so the reverse import cannot be top-level.
    from secretaria.plugins.multi_professional import _professional_services

    if name:
        matches = [p for p in roster if p.name.strip().casefold() == name.casefold()]
        if not matches:
            raise _Refusal(
                BLOCK_PROFESSIONAL_UNKNOWN,
                f"Profissional não encontrado. Profissionais disponíveis: {_names(roster)}.",
            )
        if len(matches) > 1:
            raise _Refusal(
                BLOCK_PROFESSIONAL_AMBIGUOUS,
                "Mais de um profissional tem esse nome. Peça uma escolha inequívoca.",
            )
        return matches[0], await _professional_services(tenant_id, matches[0])
    if not service:
        raise _Refusal(
            BLOCK_PROFESSIONAL_REQUIRED,
            f"Esta clínica tem vários profissionais ({_names(roster)}). "
            "Informe `professional` ou `service`.",
        )
    offering = []
    for candidate in roster:
        offered = await _professional_services(tenant_id, candidate)
        if canonical_service_name(offered, service) is not None:
            offering.append((candidate, offered))
    if not offering:
        raise _Refusal(
            BLOCK_NO_PROFESSIONAL_OFFERS_SERVICE,
            "Nenhum profissional da clínica oferece esse serviço.",
        )
    if len(offering) > 1:
        raise _Refusal(
            BLOCK_SEVERAL_PROFESSIONALS_OFFER_SERVICE,
            f"Mais de um profissional atende esse serviço: {_names([p for p, _ in offering])}. "
            "Pergunte com quem o paciente prefere e chame de novo com `professional`.",
        )
    return offering[0]


async def _resolve_target(tenant_id: UUID, professional: str, service: str) -> _Target:
    """Roster -> professional -> catalog -> service duration -> agenda, all for `tenant_id`."""
    # Lazy, same reason as above.
    from secretaria.plugins import multi_professional as mp

    config = _tenant_config_ctx.get()
    if config is not None and config.tenant_id != tenant_id:
        raise _Refusal(BLOCK_TENANT_MISMATCH, _TEMPORARY_ERROR)

    roster = await mp._active_professionals(tenant_id)
    topology = booking_topology(roster)
    chosen: Any | None = None
    if topology == BOOKING_TOPOLOGY_MULTI:
        chosen, catalog = await _pick_professional(tenant_id, roster, professional, service)
    elif topology == BOOKING_TOPOLOGY_SOLE:
        chosen = roster[0]
        if professional and chosen.name.strip().casefold() != professional.casefold():
            raise _Refusal(
                BLOCK_PROFESSIONAL_UNKNOWN,
                f"Profissional não encontrado. Esta clínica atende com: {chosen.name}.",
            )
        catalog = await mp._professional_services(tenant_id, chosen)
    else:
        if professional:
            raise _Refusal(
                BLOCK_NO_PROFESSIONALS,
                "Esta clínica não tem profissionais cadastrados: não informe `professional`.",
            )
        catalog = _effective_service_catalog()

    minutes: int | None = None
    if service:
        canonical = canonical_service_name(catalog, service)
        if canonical is None:
            raise _Refusal(
                BLOCK_SERVICE_UNKNOWN,
                "Esse serviço não existe nesta agenda. Serviços disponíveis: "
                f"{', '.join(service_names(catalog)) or 'nenhum serviço cadastrado'}.",
            )
        entry = next(e for e in catalog if service_entry_name(e) == canonical)
        minutes = _entry_minutes(entry)

    if topology == BOOKING_TOPOLOGY_MULTI:
        if config is None:
            # Never the environment calendar: a multi-professional agenda is only ever built
            # from this turn's own tenant config.
            raise _Refusal(BLOCK_NO_CALENDAR, _TEMPORARY_ERROR)
        calendar = await mp._professional_calendar(tenant_id, chosen)
    else:
        calendar = _calendar_ctx.get()
        if calendar is None:
            raise _Refusal(BLOCK_NO_CALENDAR, _TEMPORARY_ERROR)
    return _Target(
        calendar=calendar,
        professional=chosen,
        # The owner a booking would be PLACED with - the same rule every booking surface
        # uses - so the holds hidden here are the ones the confirmation would collide with.
        owner_id=resolve_booking_owner_id(
            roster, chosen.id if topology == BOOKING_TOPOLOGY_MULTI else None
        ),
        slot_minutes=minutes or calendar.default_slot_minutes,
    )


def _fmt(day: date) -> str:
    return day.strftime("%d/%m")


def _note(span: DayRange, scan: WindowScan) -> str | None:
    """The sentences that tell the model what the list does NOT cover. None = nothing to say."""
    parts: list[str] = []
    if CLAMP_MAX_DAYS in span.clamped:
        parts.append(
            f"Consulto no máximo {MAX_DAYS} dias por vez: mostrei de {_fmt(span.first)} a "
            f"{_fmt(span.last)}. Para ver depois disso, chame de novo a partir do dia seguinte."
        )
    elif CLAMP_BOOKING_WINDOW in span.clamped:
        parts.append(
            f"A agenda só abre até {_fmt(span.last)}: mostrei até esse dia, que é o limite para "
            "marcar."
        )
    if scan.truncated:
        covered = _fmt(scan.windows[-1].day)
        parts.append(
            f"Mostrei só os primeiros {MAX_WINDOWS} horários livres (até {covered}); há mais. "
            "Peça ao paciente um dia ou período mais curto para ver o resto."
        )
    if not scan.windows:
        parts.append(f"Nenhum horário livre de {_fmt(span.first)} a {_fmt(span.last)}.")
    return " ".join(parts) or None


async def _read_availability(
    tenant_id: UUID, professional: str, service: str, day_from: str, day_to: str
) -> dict:
    target = await _resolve_target(tenant_id, professional.strip(), service.strip())
    calendar = target.calendar
    tz = calendar.tzinfo
    try:
        span = resolve_range(
            day_from, day_to, today=_clinic_today(tz), booking_window_days=DAY_PICKER_WINDOW_DAYS
        )
    except RangeError as error:
        raise _Refusal(error.code, str(error)) from None

    # Held slots (a Portal visitor between "Confirmar" and the code) are not free: the same
    # lookup the slot picker makes, through an UNARMED gate - this tool never reserves.
    gate = BookingGate(
        tenant_id=tenant_id,
        conversation_id=_conversation_id_ctx.get() or uuid4(),
        patient_id=None,
        external_id=None,
        armed=False,
    )
    holds = await gate.busy_windows(target.owner_id)
    scan = await scan_free_windows(
        calendar, span=span, duration_minutes=target.slot_minutes, holds=holds
    )

    clamped = [*span.clamped, *([CLAMP_MAX_WINDOWS] if scan.truncated else [])]
    last = scan.windows[-1].day if scan.truncated else span.last
    result: dict[str, Any] = {
        "windows": [window.payload() for window in scan.windows],
        "timezone": getattr(tz, "key", None) or str(tz),
        "slot_minutes": target.slot_minutes,
        "day_from": span.first.isoformat(),
        "day_to": last.isoformat(),
    }
    if target.professional is not None:
        result["professional"] = target.professional.name
    if clamped:
        result["clamped"] = clamped
    note = _note(span, scan)
    if note:
        result["note"] = note
    logger.info(
        "ai_availability_read",
        tenant_id=str(tenant_id),
        conversation_id=str(_conversation_id_ctx.get() or ""),
        windows=len(scan.windows),
        days_scanned=span.days,
        clamped=bool(clamped),
        professional_resolved=target.professional is not None,
    )
    return result


@tool
async def get_availability(
    professional: str = "",
    service: str = "",
    day_from: str = "",
    day_to: str = "",
) -> dict:
    """Consulta os HORÁRIOS LIVRES da agenda de um profissional - só isso. Devolve janelas
    {day, start, end} no fuso da clínica (campo `timezone`), já sem os horários que outros
    pacientes estão reservando. Nunca devolve compromissos, nomes nem horários ocupados: o
    que não está na lista não está livre. Use para responder "tem horário?" ou "tem vaga
    semana que vem?" e para sugerir opções. Esta ferramenta só lê: se o paciente quer marcar
    um horário da lista, chame create_event com esse início (ou set_booking_draft com `day`
    e `time`) - elas só preparam o cartão de confirmação, e o fluxo confere e conduz.

    Cada janela é um trecho contínuo em que cabe uma consulta; `slot_minutes` é a duração
    usada. Mostra no máximo 14 dias e 30 janelas por chamada. Se a resposta trouxer
    `clamped`, a lista NÃO cobre tudo o que foi pedido: diga isso ao paciente e peça um dia
    ou período menor. `note` resume em uma frase o que a lista cobre: respeite-a. Sem
    janelas, diga que não há horário livre nesse período - NUNCA invente um horário nem
    prometa um que não esteja na lista.

    Args:
        professional: Nome do profissional (ou vazio). Em clínica com vários profissionais,
            informe o profissional OU o serviço (se só um profissional oferece o serviço, ele
            é usado).
        service: Nome EXATO de um serviço da clínica (ou vazio: usa a duração padrão do
            profissional, veja `slot_minutes`).
        day_from: Primeiro dia, no formato AAAA-MM-DD, no fuso da clínica (vazio = hoje).
        day_to: Último dia, no formato AAAA-MM-DD (vazio = 14 dias a partir de day_from).
    """
    tenant_id = _tenant_id_ctx.get()
    try:
        if tenant_id is None:
            raise _Refusal(BLOCK_NO_CLINIC, "Nenhuma clínica configurada para esta conversa.")
        return await _read_availability(
            tenant_id, professional or "", service or "", day_from or "", day_to or ""
        )
    except _Refusal as refusal:
        logger.info("agent_tool_blocked", tool=TOOL_NAME, reason=refusal.reason)
        return {"error": refusal.message}
    except CalendarUnavailableError:
        # Today's behaviour for every calendar read: run_agent maps it to the sentinel that
        # hands the conversation to a person and alerts the owner (module docstring).
        raise
    except Exception as exc:
        logger.warning("agent_availability_failed", error_type=type(exc).__name__)
        return {"error": _TEMPORARY_ERROR}
