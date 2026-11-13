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

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models.professional_unit import ProfessionalUnit
from secretaria.models.unit import Unit
from secretaria.services import professional_units
from secretaria.services.tenant_config import professional_business_hours

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
    """"<name> — <address>", the name alone without an address, None without a name either."""
    name_text, address_text = _clean(name), _clean(address)
    if not name_text:
        return address_text or None
    return f"{name_text} — {address_text}" if address_text else name_text


def unit_line(name: Any, address: Any) -> str:
    """The e-mail lines, same spelling as the professional's edit e-mail
    (workers/shared/appointment_edit_notification.py): "Unidade: X\\nEndereço: Y\\n"."""
    name_text, address_text = _clean(name), _clean(address)
    if not name_text:
        return ""
    return f"Unidade: {name_text}\n" + (f"Endereço: {address_text}\n" if address_text else "")


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


async def check_unit(
    session: AsyncSession, tenant_id: UUID, professional: Any | None, unit_id: UUID
) -> Unit:
    """The unit, when it may be written onto a booking of `professional` at this clinic.

    Refused (`UnitRefused`): another clinic's or an unknown unit (404, the same answer for both:
    telling them apart would leak another clinic's rows), a deactivated unit, or - when the
    professional attends at units - one they do not attend at (422). A professional without any
    offered unit may be booked at any active unit of the clinic.
    """
    unit = await tenant_unit(session, tenant_id, unit_id)
    if unit is None:
        raise UnitRefused("unit_not_found", "Unidade não encontrada.", 404)
    if not unit.is_active:
        raise UnitRefused("unit_inactive", "Essa unidade está desativada.", 422)
    if professional is not None:
        offered = await professional_units.available_unit_ids(session, professional)
        if offered and unit.id not in offered:
            raise UnitRefused(
                "unit_not_linked", "Esse profissional não atende nessa unidade.", 422
            )
    return unit


__all__ = [
    "UnitOption",
    "UnitRefused",
    "calendar_at_unit",
    "check_unit",
    "scoped_calendar",
    "tenant_unit",
    "unit_line",
    "unit_location",
    "unit_options_for",
    "within_hours",
]
