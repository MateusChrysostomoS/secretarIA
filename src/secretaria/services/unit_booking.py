"""Units in a patient's booking (TASK-083 spec §8).

services/professional_units.py owns the link rows (which units a professional attends, and the
hours there). This module turns them into what a BOOKING needs:

* `unit_options_for` - every professional's offered units with their resolved hours, in ONE read,
  so the pure flow router (services/flow_router.py) can ask "which unit?" without a query;
* `scoped_calendar` - the professional's agenda as seen from one unit: that unit's hours (the
  offered days and times), its id (stamped on a local event) and its location (written on a
  Google event). The BUSY time stays the whole agenda's: a person is in one place at a time;
* `check_unit` - the rule the hub and the AI tools apply before writing a unit onto a booking;
* `unit_location` / `unit_line` - the one spelling of "where" for every text.

A professional with no link offers no unit: every path here is then a no-op and the booking is
exactly today's.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models.professional import Professional
from secretaria.models.professional_unit import ProfessionalUnit
from secretaria.models.unit import Unit
from secretaria.services import professional_units
from secretaria.services.professional_service_units import (
    allowed_unit_ids,
    read_marks,
    read_marks_for,
)
from secretaria.services.service_terms import WEEKDAY_KEYS, load_terms_for
from secretaria.services.tenant_config import professional_business_hours

logger = get_logger(__name__)

_WEEKDAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class UnitOption:
    """One unit a professional offers, with the hours there already resolved.

    `business_hours` follows the NULL-versus-EMPTY rule of services/professional_units.py: a NULL
    link inherits the professional's resolved hours, an own `{}` is closed all week. Plain values,
    safe for the router's snapshot.
    """

    id: UUID
    name: str
    address: str | None
    business_hours: dict

    @property
    def bookable(self) -> bool:
        """Whether a patient can book here at all (any open weekday)."""
        return bool(self.business_hours)

    @property
    def location(self) -> str | None:
        return unit_location(self.name, self.address)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def unit_location(name: Any, address: Any) -> str | None:
    """ "<name> — <address>", the name alone without an address, None without a name either."""
    name_text, address_text = _clean(name), _clean(address)
    if not name_text:
        return address_text or None
    return f"{name_text} — {address_text}" if address_text else name_text


# A name that already says "Unidade"/"Unidades" (as a word) is not prefixed again.
_UNIT_WORD = re.compile(r"unidades?\b", re.IGNORECASE)


def unit_place_label(name: Any) -> str:
    """ "Unidade <name>", the patient-facing name of a place (TASK-085 T8 review M3).

    The ONE rule every text uses ("Consulta na Unidade Centro"): a name that already starts with
    the word "Unidade"/"Unidades" is kept as it is ("Unidade Centro", never "Unidade Unidade
    Centro"); any other name gets the prefix ("Sede" -> "Unidade Sede"). "" without a name.
    """
    text = _clean(name)
    if not text:
        return ""
    return text if _UNIT_WORD.match(text) else f"Unidade {text}"


def unit_line(name: Any, address: Any) -> str:
    """The e-mail lines, same spelling as the professional's edit e-mail
    (workers/shared/appointment_edit_notification.py): "Unidade: X\\nEndereço: Y\\n"."""
    name_text, address_text = _clean(name), _clean(address)
    if not name_text:
        return ""
    return f"Unidade: {name_text}\n" + (f"Endereço: {address_text}\n" if address_text else "")


def _name_of(professional: Any) -> str:
    """A professional row/snapshot's name, or the name itself when given as text."""
    if isinstance(professional, str):
        return _clean(professional)
    return _clean(getattr(professional, "name", None)) if professional is not None else ""


def unit_fields(unit: Any | None) -> dict:
    """`build_event_description`'s "Unidade"/"Endereço" keywords for `unit`, or {} without one."""
    if unit is None:
        return {}
    fields: dict = {}
    name, address = _clean(getattr(unit, "name", None)), _clean(getattr(unit, "address", None))
    if name:
        fields["unit_name"] = name
    if address:
        fields["unit_address"] = address
    return fields


def event_unit_fields(unit: Any | None, professional: Any = None) -> dict:
    """The CLINIC event's new lines for a booking AT A UNIT: the unit, its address and (when
    known) the professional. {} without a unit, so a clinic without units keeps its event
    description byte for byte (the professional line is new only alongside a unit)."""
    fields = unit_fields(unit)
    if not fields:
        return {}
    name = _name_of(professional)
    if name:
        fields["professional"] = name
    return fields


def confirmation_unit_lines(unit: Any | None, professional: Any = None) -> str:
    """The booked confirmation's "where" (TASK-083 follow-up, owner 2026-11-13): for a booking at
    a unit, "Profissional: X\\n" (when known) + "Unidade: Y\\nEndereço: Z\\n" - the unit's name and
    its address each on its own line. "" without a unit: a clinic without units keeps its text."""
    if unit is None:
        return ""
    name, address = _clean(getattr(unit, "name", None)), _clean(getattr(unit, "address", None))
    if not name:
        return f"Endereço: {address}\n" if address else ""
    doctor = _name_of(professional)
    return (f"Profissional: {doctor}\n" if doctor else "") + unit_line(name, address)


async def booking_unit_fields(
    session: AsyncSession,
    tenant_id: UUID,
    unit_id: UUID | None,
    professional_id: UUID | None = None,
) -> dict:
    """`event_unit_fields` for an EXISTING booking (edit, reschedule, service change), read here.

    Tenant-scoped (a stray foreign id names nothing). Best-effort: a failed read never blocks the
    calendar write - it degrades to no unit lines and a warning (ids and error type only).
    """
    if unit_id is None:
        return {}
    try:
        unit = await tenant_unit(session, tenant_id, unit_id)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning(
            "event_description_unit_lookup_failed",
            tenant_id=str(tenant_id),
            error_type=type(exc).__name__,
        )
        return {}
    if unit is None:
        return {}
    return event_unit_fields(unit, await professional_name(session, tenant_id, professional_id))


async def professional_name(
    session: AsyncSession, tenant_id: UUID, professional_id: UUID | None
) -> str | None:
    """THIS clinic's professional's name for a text, or None (unknown id, another clinic's, or a
    failed read - logged with the error type only: a name is never worth failing a booking)."""
    if professional_id is None:
        return None
    try:
        name = await session.scalar(
            select(Professional.name).where(
                Professional.id == professional_id, Professional.tenant_id == tenant_id
            )
        )
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning(
            "event_description_professional_lookup_failed",
            tenant_id=str(tenant_id),
            error_type=type(exc).__name__,
        )
        return None
    return _clean(name) or None


def _own_hours(raw: dict) -> dict:
    return {day: windows for day, windows in (raw or {}).items() if windows}


async def unit_options_for(
    session: AsyncSession, tenant: Any, professionals: Iterable[Any]
) -> dict[UUID, list[UnitOption]]:
    """Each professional's offered units: active link AND active unit of THIS tenant.

    One query for the whole roster. A professional without any offered unit is absent from the
    result. Ordered like `professional_units.available_unit_ids` (link sort_order, unit name).
    `professionals` may be ORM rows or the router's snapshots (`id`, `business_hours`).
    """
    by_id = {professional.id: professional for professional in professionals}
    if not by_id:
        return {}
    rows = await session.execute(
        select(ProfessionalUnit, Unit)
        .join(Unit, Unit.id == ProfessionalUnit.unit_id)
        .where(
            ProfessionalUnit.professional_id.in_(list(by_id)),
            ProfessionalUnit.is_active.is_(True),
            Unit.tenant_id == tenant.id,
            Unit.is_active.is_(True),
        )
        .order_by(ProfessionalUnit.sort_order, Unit.name, Unit.id)
    )
    options: dict[UUID, list[UnitOption]] = {}
    for link, unit in rows.all():
        professional = by_id[link.professional_id]
        # `is None`, never truthiness: an own `{}` is "closed here", not "inherit".
        hours = (
            professional_business_hours(professional, tenant)
            if link.business_hours is None
            else _own_hours(link.business_hours)
        )
        options.setdefault(link.professional_id, []).append(
            UnitOption(id=unit.id, name=unit.name, address=unit.address, business_hours=hours)
        )
    return options


async def attach_unit_options(
    session: AsyncSession, tenant: Any, professional_rows: Iterable[Any], snapshots: Iterable[Any]
) -> None:
    """Put each router snapshot's offered units on it as `units` (one read for the roster).

    `professional_rows` are the ORM rows (their hours resolve the NULL links); `snapshots` the
    router-shaped copies (workers/shared/greeting.py::_flow_professionals) that travel into the
    pure router. A snapshot without units gets `[]`: the flow is then exactly today's. Each also
    gets `service_units` (`{service_id: unit ids}`, `{}` when nothing is restricted), read for the
    whole roster in ONE query (services/professional_service_units.py::read_marks_for), and
    `service_unit_terms` (`{service_id: {unit_id: UnitTerms}}`, `{}` when none): the per-unit
    price/duration exceptions (TASK-085), also ONE query, tenant-scoped
    (services/service_terms.py::load_terms_for).
    """
    rows = list(professional_rows)
    options = await unit_options_for(session, tenant, rows)
    marks = await read_marks_for(session, [row.id for row in rows])
    terms = await load_terms_for(session, tenant.id, [row.id for row in rows])
    for snapshot in snapshots:
        snapshot.units = options.get(snapshot.id, [])
        snapshot.service_units = marks.get(snapshot.id, {})
        snapshot.service_unit_terms = terms.get(snapshot.id, {})


async def professional_terms_view(
    session: AsyncSession, tenant: Any, professional: Any
) -> SimpleNamespace:
    """A router-shaped snapshot of ONE professional for DB readers that need the D3 rule
    (flow_units.shown_terms): id, name, units, service_units, service_unit_terms - filled by
    `attach_unit_options`. `professional` is the ORM row (its hours resolve NULL links)."""
    view = SimpleNamespace(
        id=professional.id,
        name=professional.name,
        business_hours=getattr(professional, "business_hours", None),
    )
    await attach_unit_options(session, tenant, [professional], [view])
    return view


def scoped_calendar(calendar: Any, unit: Any | None, *, with_hours: bool = True) -> Any:
    """`calendar` as seen from `unit` (its hours, id and location), or `calendar` itself.

    Unchanged without a unit, and for a calendar without the seam (a test double): the unit is
    then simply not applied. `with_hours=False` keeps the agenda's hours - a write that already
    knows its window (a promotion) only needs the unit's id and location.
    """
    if unit is None or calendar is None:
        return calendar
    with_unit = getattr(calendar, "with_unit", None)
    if not callable(with_unit):
        return calendar
    hours = getattr(unit, "business_hours", None) if with_hours else None
    return with_unit(
        unit.id,
        unit_location(getattr(unit, "name", None), getattr(unit, "address", None)),
        business_hours=hours if hours else None,
    )


def service_calendar(calendar: Any, blocked: Collection[str] | None) -> Any:
    """`calendar` without the service's `blocked` weekdays (TASK-087), or `calendar` itself.

    Unchanged when nothing is blocked, and for a calendar without the seam (a test double that
    implements only `with_unit`): the block is then simply not applied.
    """
    if not blocked or calendar is None:
        return calendar
    with_blocked = getattr(calendar, "with_blocked_weekdays", None)
    if not callable(with_blocked):
        return calendar
    return with_blocked(frozenset(blocked))


def blocked_on(day: date | datetime, blocked: Collection[str] | None) -> bool:
    """Whether `day`'s weekday is one of `blocked`. A datetime must be on the clinic's clock."""
    return bool(blocked) and WEEKDAY_KEYS[day.weekday()] in blocked


# Plural weekday names for a sentence ("não está disponível às segundas"), Monday first.
_WEEKDAY_PLURAL_PT = {
    "monday": "segundas",
    "tuesday": "terças",
    "wednesday": "quartas",
    "thursday": "quintas",
    "friday": "sextas",
    "saturday": "sábados",
    "sunday": "domingos",
}
_MASCULINE_DAYS = frozenset({"saturday", "sunday"})


def _ordered_days(keys: Collection[str] | None) -> list[str]:
    return [day for day in WEEKDAY_KEYS if day in (keys or ())]


def _join_pt(parts: list[str]) -> str:
    """ "a", "a e b", "a, b e c"."""
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} e {parts[-1]}"


def weekday_plural_pt(keys: Collection[str] | None) -> str:
    """ "segundas e sextas" - known keys only, Monday first, commas then " e ". "" for none."""
    return _join_pt([_WEEKDAY_PLURAL_PT[day] for day in _ordered_days(keys)])


def weekday_list_pt(keys: Collection[str] | None) -> str:
    """The days with their article, for a sentence - Monday first, known keys only, "" for none.

    "às segundas e sextas", "aos sábados e domingos"; a list mixing both genders gives every day
    its own article: "às sextas e aos sábados", "às segundas, às sextas e aos sábados" (TASK-087
    review P6 - never "às sextas e sábados").
    """
    days = _ordered_days(keys)
    if not days:
        return ""
    masculine = [day in _MASCULINE_DAYS for day in days]
    if all(masculine) or not any(masculine):
        return f"{'aos' if masculine[0] else 'às'} {weekday_plural_pt(days)}"
    return _join_pt(
        [
            f"{'aos' if is_masc else 'às'} {_WEEKDAY_PLURAL_PT[day]}"
            for day, is_masc in zip(days, masculine, strict=True)
        ]
    )


def service_day_refusal(
    service_name: str, professional_name: str | None, blocked: Collection[str] | None
) -> str:
    """The patient's sentence for a service asked on a day it is not done (TASK-087).

    "Cirurgia com Dra. Ana não está disponível às segundas. Posso ver outro dia?" - " com
    <médico>" only when known; the days through `weekday_list_pt` ("às sextas e aos sábados");
    "nesse dia" when no day is known. "não está disponível" agrees with any service name
    ("Consulta", "Retorno") - review P6.
    """
    service = _clean(service_name)
    doctor = _clean(professional_name)
    who = f"{service} com {doctor}" if doctor else service
    when = weekday_list_pt(blocked) or "nesse dia"
    return f"{who} não está disponível {when}. Posso ver outro dia?"


async def tenant_unit(session: AsyncSession, tenant_id: UUID, unit_id: UUID | None) -> Unit | None:
    """The tenant's own unit row (any state), or None - never another clinic's."""
    if unit_id is None:
        return None
    return await session.scalar(select(Unit).where(Unit.id == unit_id, Unit.tenant_id == tenant_id))


async def calendar_at_unit(
    session: AsyncSession, calendar: Any, tenant_id: UUID, unit_id: UUID | None
) -> Any:
    """For a write whose window is already chosen (a hold or Pix promotion): stamp the unit's id
    and location, keep the agenda's hours. A unit since removed answers the plain calendar."""
    unit = await tenant_unit(session, tenant_id, unit_id)
    return scoped_calendar(calendar, unit, with_hours=False)


def within_hours(hours: dict, start: datetime, end: datetime) -> bool:
    """Whether [start, end) fits one window of `hours` on its own weekday (clinic clock).

    `start`/`end` must be on the clinic's clock already (aware in its zone, or naive local).
    """
    windows = (hours or {}).get(_WEEKDAY_KEYS[start.weekday()]) or []
    begin, finish = start.strftime("%H:%M"), end.strftime("%H:%M")
    if end.date() != start.date():
        return False
    return any(
        str(window.get("start")) <= begin and finish <= str(window.get("end"))
        for window in windows
        if isinstance(window, dict)
    )


class UnitRefused(Exception):
    """A unit that may not be written onto this booking. `code` is stable, `message` plain."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


async def service_offered_at_unit(
    session: AsyncSession, professional_id: UUID, service_id: UUID | None, unit_id: UUID
) -> bool:
    """Whether the professional does `service_id` at `unit_id` (TASK-083 follow-up).

    No `service_id` (an entry without one is unrestricted by definition) or a service without
    marks: every unit of the professional, so True. Otherwise only the marked units.
    """
    if service_id is None:
        return True
    allowed = allowed_unit_ids(await read_marks(session, professional_id), service_id)
    return allowed is None or unit_id in allowed


async def check_unit(
    session: AsyncSession,
    tenant_id: UUID,
    professional: Any | None,
    unit_id: UUID,
    service_id: UUID | None = None,
) -> Unit:
    """The unit, when it may be written onto a booking of `professional` at this clinic.

    Refused (`UnitRefused`): another clinic's or an unknown unit (404, the same answer for both:
    telling them apart would leak another clinic's rows), a deactivated unit, or - when the
    professional attends at units - one they do not attend at (422). A professional without any
    offered unit may be booked at any active unit of the clinic.

    With `service_id` (the professional's catalog entry the booking is for), also refused when
    the professional does that service only at OTHER units (422 `unit_service_not_offered`).
    `service_id=None` is exactly the rule above. Callers that write for the PATIENT pass it; the
    staff hub only flags the combination (`service_offered_at_unit`).
    """
    unit = await tenant_unit(session, tenant_id, unit_id)
    if unit is None:
        raise UnitRefused("unit_not_found", "Unidade não encontrada.", 404)
    if not unit.is_active:
        raise UnitRefused("unit_inactive", "Essa unidade está desativada.", 422)
    if professional is not None:
        offered = await professional_units.available_unit_ids(session, professional)
        if offered and unit.id not in offered:
            raise UnitRefused("unit_not_linked", "Esse profissional não atende nessa unidade.", 422)
        if not await service_offered_at_unit(session, professional.id, service_id, unit.id):
            raise UnitRefused(
                "unit_service_not_offered", "Esse serviço não é feito nessa unidade.", 422
            )
    return unit


__all__ = [
    "UnitOption",
    "UnitRefused",
    "attach_unit_options",
    "blocked_on",
    "booking_unit_fields",
    "calendar_at_unit",
    "check_unit",
    "confirmation_unit_lines",
    "event_unit_fields",
    "professional_name",
    "professional_terms_view",
    "scoped_calendar",
    "service_calendar",
    "service_day_refusal",
    "service_offered_at_unit",
    "tenant_unit",
    "unit_fields",
    "unit_line",
    "unit_location",
    "unit_options_for",
    "weekday_list_pt",
    "weekday_plural_pt",
    "within_hours",
]
