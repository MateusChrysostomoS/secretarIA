"""`get_availability`: the AI's one agenda read - free WINDOWS, never events (TASK-030 P4).

Spec §4.6. On the v2 toolset (`flow_router.ai_draft_v2_enabled`) the AI loses
`check_availability`, `list_free_slots` and every tool that writes to the agenda
(ai/tools.py `AI_TOOLSET_V2_WITHHELD`, `AI_TOOLSET_V2_STAGING`; its `create_event` /
`cancel_event` there only stage the confirmation card, ai/staging_tools.py). What it keeps,
to answer "tem horário semana que vem?", is this tool, and the answer has exactly one shape:

    {"windows": [{"day": "2026-10-08", "weekday": "qui", "start": "08:00", "end": "11:20"}, ...],
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
from secretaria.services.availability import booking_notice_floor, booking_notice_hours
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
from secretaria.services.booking_dates import short_day_phrase
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
from secretaria.services.service_catalog import entry_service_id
from secretaria.services.unit_booking import scoped_calendar, service_calendar

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
    # The professional's catalog entry of the named service (None without one): its per-unit
    # duration, when it has one, sizes that unit's windows (TASK-085 D7).
    entry: Any | None = None


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
    entry: Any | None = None
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
        entry=entry,
    )


async def _service_unit_scope(
    tenant_id: UUID, target: _Target, units: list
) -> tuple[list, bool, Any | None]:
    """The units the named service is done at, plus the professional's terms - ONE session.

    Returns `(units, restricted_to_none, terms)`:
    * `units`: the bookable units narrowed to the ones the doctor marked for the service
      (TASK-083 `professional_service_units`; no marks = all of them, as `units_for_service`
      does). A restriction whose units are all closed yields `[]` with the flag set - never
      "all". A failed marks read keeps every unit (a read tool; the booking re-checks).
    * `terms`: the exception rows of THIS service (`terms_for_entry`), read once for both the
      per-unit durations (D7) and the per-unit blocked days (TASK-087). None when there is
      nothing to read (no units, professional, or linked service) or the read FAILED - the
      callers then fall back to the base for every unit (R8), like the units read above.

    Everything is scoped to `tenant_id` (services/service_terms.py).
    """
    entry, professional = target.entry, target.professional
    if not units or professional is None or not isinstance(entry, dict):
        return units, False, None
    service_id = entry_service_id(entry)
    if service_id is None:
        return units, False, None
    from secretaria.core.database import async_session_factory
    from secretaria.services.professional_service_units import allowed_unit_ids, read_marks
    from secretaria.services.service_terms import load_terms, terms_for_entry

    restricted_to_none = False
    terms = None
    async with async_session_factory() as session:
        try:
            allowed = allowed_unit_ids(await read_marks(session, professional.id), service_id)
            if allowed is not None:
                units = [unit for unit in units if unit.id in allowed]
                restricted_to_none = not units
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("agent_availability_marks_failed", error_type=type(exc).__name__)
        try:
            terms = terms_for_entry(await load_terms(session, tenant_id, professional.id), entry)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("agent_availability_unit_terms_failed", error_type=type(exc).__name__)
    return units, restricted_to_none, terms


def _unit_minutes(target: _Target, units: list, terms: Any | None) -> dict[UUID, int]:
    """{unit id: minutes} for the units where the named service has its OWN duration (D7).

    A unit absent from the result keeps `target.slot_minutes` - today's duration - and so does
    everything without units, a named service or a professional, and a failed terms read
    (`terms` None): the base, like the units read (a read tool; the flow re-checks the slot).
    """
    entry = target.entry
    if not units or terms is None or not isinstance(entry, dict):
        return {}
    from secretaria.services.service_terms import resolve_terms

    out: dict[UUID, int] = {}
    for unit in units:
        resolved = resolve_terms(entry, terms, unit.id)
        if resolved.own and resolved.duration_min is not None:
            out[unit.id] = resolved.duration_min
    return out


def _unit_blocked_days(
    target: _Target, units: list, terms: Any | None
) -> dict[UUID | None, frozenset[str]]:
    """{unit id (None = no unit): weekdays the named service is NOT done on} (TASK-087).

    Only with a named service of a professional (R5: no service, no filter). A unit without its
    own list gets the base. `terms` None (nothing to read, or the read failed) = the BASE list
    for every unit (R8) - the booking tools still refuse a blocked day at confirmation (G3), so
    this read never lets one through.
    """
    entry, professional = target.entry, target.professional
    if professional is None or not isinstance(entry, dict):
        return {}
    from secretaria.services.service_terms import blocked_weekdays_for

    return {
        unit.id if unit else None: blocked_weekdays_for(entry, terms, unit.id if unit else None)
        for unit in units or [None]
    }


def _fmt(day: date) -> str:
    return short_day_phrase(day)


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
    # The doctor's minimum notice for a new booking: times inside it are not free to book.
    floor = booking_notice_floor(target.professional, datetime.now(tz))
    # TASK-083 spec §8: a professional who attends at units is free only within a unit's hours
    # (the busy time stays the whole agenda's). One unit: its hours. Several: each unit read
    # within its own hours and every window says which unit it is at.
    from secretaria.ai.unit_scope import professional_unit_options

    try:
        offered = await professional_unit_options(tenant_id, target.professional)
    except Exception as exc:  # noqa: BLE001 - a read tool: the agenda's own hours then
        logger.warning("agent_availability_units_failed", error_type=type(exc).__name__)
        offered = []
    units = [unit for unit in offered if unit.bookable]
    # TASK-085 D7: each unit's windows are sized with the service's duration AT that unit.
    # Only the units the doctor does the named service at (marks), read together with the terms.
    units, no_unit_does_it, terms = await _service_unit_scope(tenant_id, target, units)
    own_minutes = _unit_minutes(target, units, terms)
    # TASK-087: the weekdays the named service is not done on, per unit (none without a service).
    blocked_by_unit = _unit_blocked_days(target, units, terms)
    scanned: set[int] = set()
    labelled: list[tuple[Any, str | None]] = []
    truncated = False
    for unit in [] if no_unit_does_it else units or [None]:
        minutes = own_minutes.get(unit.id, target.slot_minutes) if unit else target.slot_minutes
        scanned.add(minutes)
        unit_scan = await scan_free_windows(
            service_calendar(
                scoped_calendar(calendar, unit), blocked_by_unit.get(unit.id if unit else None)
            ),
            span=span,
            duration_minutes=minutes,
            holds=holds,
            not_before=floor,
        )
        truncated = truncated or unit_scan.truncated
        name = unit.name if unit is not None and len(units) > 1 else None
        labelled.extend((window, name) for window in unit_scan.windows)
    labelled.sort(key=lambda item: (item[0].day, item[0].start))
    if len(labelled) > MAX_WINDOWS:
        labelled, truncated = labelled[:MAX_WINDOWS], True
    scan = WindowScan(windows=[window for window, _ in labelled], truncated=truncated)

    clamped = [*span.clamped, *([CLAMP_MAX_WINDOWS] if scan.truncated else [])]
    last = scan.windows[-1].day if scan.truncated else span.last
    result: dict[str, Any] = {
        "windows": [
            {**window.payload(), **({"unit": name} if name else {})} for window, name in labelled
        ],
        "timezone": getattr(tz, "key", None) or str(tz),
        # One duration for every unit read (one unit, or the same everywhere): that one. Units
        # that differ: the base (D7 - no unit chosen); each window is sized for its own unit.
        "slot_minutes": scanned.pop() if len(scanned) == 1 else target.slot_minutes,
        "day_from": span.first.isoformat(),
        "day_to": last.isoformat(),
    }
    if target.professional is not None:
        result["professional"] = target.professional.name
    if len(units) == 1:
        result["unit"] = units[0].name
    if floor is not None:
        result["min_booking_notice_hours"] = booking_notice_hours(target.professional)
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
    {day, weekday, start, end} no fuso da clínica (campo `timezone`), já sem os horários
    indisponíveis (nunca diga que um horário está "reservado" para alguém).
    Nunca devolve compromissos, nomes nem horários ocupados: o
    que não está na lista não está livre. Use para responder "tem horário?" ou "tem vaga
    semana que vem?" e para sugerir opções. Esta ferramenta só lê: se o paciente quer marcar
    um horário da lista, chame create_event com esse início (ou set_booking_draft com `day`
    e `time`) - elas só preparam o cartão de confirmação, e o fluxo confere e conduz.

    Cada janela é um trecho contínuo em que cabe uma consulta; `slot_minutes` é a duração
    usada (se a duração muda entre as unidades do profissional, cada janela já usa a da sua
    unidade). Mostra no máximo 14 dias e 30 janelas por chamada. Se a resposta trouxer
    `clamped`, a lista NÃO cobre tudo o que foi pedido: diga isso ao paciente e peça um dia
    ou período menor. `note` resume em uma frase o que a lista cobre: respeite-a. Sem
    janelas, diga que não há horário livre nesse período - NUNCA invente um horário nem
    prometa um que não esteja na lista. Cada janela traz `weekday` (seg, ter...): ao listar
    datas ao paciente, escreva como "seg, 24/11".

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
