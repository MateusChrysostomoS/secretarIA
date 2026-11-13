"""The "which unit?" step of the deterministic booking flow (TASK-083 spec §8) - pure helpers.

A professional's offered units ride on the router's roster snapshot as `units` (a list of
services/unit_booking.py::UnitOption, loaded by the worker - the router does no DB I/O). From
them this module answers, for the booking in progress:

* no unit at all (no link)         -> no step, the flow is exactly today's;
* one bookable unit                -> no step, that unit is used (its hours, its address);
* two or more bookable units       -> the patient picks one (`STEP_AWAITING_UNIT`);
* links but every unit closed      -> nobody can book this professional (the hours gap).

The step's rendering and its hooks live in services/flow_router.py (`_enter_unit_list`,
`_handle_unit_step`); this module holds what they share and never imports the router.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from secretaria.core.whatsapp_limits import (
    EMOJI_BACK,
    MAX_LIST_ROW_DESCRIPTION_CHARS,
    decorate,
    strip_decoration,
    truncate_list_row_title,
    truncate_plain,
)
from secretaria.services.unit_booking import scoped_calendar

STEP_AWAITING_UNIT = "awaiting_unit"
# The day/slot pickers' back row when the professional has a unit to choose: one step back is
# the unit list, not the service list. "⬅️ Outra unidade" is 19 characters (<= 24).
BACK_TARGET_UNIT = "unit"
LABEL_ANOTHER_UNIT = decorate(EMOJI_BACK, "Outra unidade")
UNIT_LIST_BODY = "Em qual unidade você prefere ser atendido(a)?"
# The picked unit stopped being offered mid-booking and only one is left (final wave M1).
UNIT_GONE_NOTICE = "A unidade escolhida não está mais disponível."
UNIT_LIST_BUTTON = "Ver unidades"
UNIT_LIST_SECTION = "Unidades"
# `unit|<uuid>` is a payload row (schemas/webhook.py::_PAYLOAD_ROW_PREFIXES): the tap arrives
# as "<title> (<uuid>)", so the 24-char title is never the lookup key.
ROW_UNIT_PREFIX = "unit|"
# WhatsApp lists hold 10 rows; one is the back row.
MAX_UNIT_ROWS = 9

_TRAILING_PAYLOAD = re.compile(r"\(([^)]*)\)\s*$")


def offered_units(professional: Any | None) -> list:
    """Every unit the professional attends at (bookable or not), or []."""
    if professional is None:
        return []
    return list(getattr(professional, "units", None) or [])


def bookable_units(professional: Any | None) -> list:
    """The offered units with any open weekday - the ones a patient can pick."""
    return [unit for unit in offered_units(professional) if getattr(unit, "business_hours", None)]


def unit_choice_needed(professional: Any | None) -> bool:
    """Two or more bookable units: the patient has to pick one."""
    return len(bookable_units(professional)) > 1


def find_unit(professional: Any | None, unit_id: UUID | None) -> Any | None:
    if unit_id is None:
        return None
    return next((unit for unit in bookable_units(professional) if unit.id == unit_id), None)


def selected_unit_id(conversation: Any) -> UUID | None:
    """The conversation's picked unit (getattr: older snapshots predate the field)."""
    return getattr(conversation, "flow_selected_unit_id", None)


def effective_unit(conversation: Any, professional: Any | None) -> Any | None:
    """The unit this booking happens at: the one picked, else the only bookable one, else None.

    A pick that is no longer bookable (unit deactivated, link removed) is ignored.
    """
    picked = find_unit(professional, selected_unit_id(conversation))
    if picked is not None:
        return picked
    units = bookable_units(professional)
    return units[0] if len(units) == 1 else None


def scope(calendar: Any, conversation: Any, professional: Any | None) -> Any:
    """The calendar as seen from this booking's unit (unchanged without one)."""
    return scoped_calendar(calendar, effective_unit(conversation, professional))


def union_hours(units: list) -> dict:
    """Every unit's open windows merged per weekday (overlapping/touching windows joined)."""
    spans: dict[str, list[tuple[str, str]]] = {}
    for unit in units:
        for day, windows in (getattr(unit, "business_hours", None) or {}).items():
            for window in windows or []:
                if isinstance(window, dict) and window.get("start") and window.get("end"):
                    spans.setdefault(day, []).append((str(window["start"]), str(window["end"])))
    merged: dict[str, list[dict]] = {}
    for day, pairs in spans.items():
        joined: list[list[str]] = []
        for start, end in sorted(pairs):
            if joined and start <= joined[-1][1]:
                joined[-1][1] = max(joined[-1][1], end)
            else:
                joined.append([start, end])
        merged[day] = [{"start": start, "end": end} for start, end in joined]
    return merged


def appointment_calendar(calendar: Any, unit_id: UUID | None, owner: Any | None) -> Any:
    """An EXISTING booking's agenda for a new day/time (reschedule, "Alterar Dados").

    * the owner offers no bookable unit -> `calendar` itself (today's behaviour, byte for byte);
    * the booking's unit is one the owner still offers -> that unit's hours (and its id/location);
    * otherwise (no unit - a booking from before the units, a hub "encaixe" - or a unit the
      owner no longer offers): the owner's only bookable unit, or with several the UNION of their
      hours with NO unit stamped (none was chosen). Never the agenda's own hours / the 08-18
      fallback when the doctor's real hours live on the units (final wave I1/I2).
    """
    units = bookable_units(owner)
    if not units:
        return calendar
    unit = find_unit(owner, unit_id)
    if unit is None and len(units) == 1:
        unit = units[0]
    if unit is not None:
        return scoped_calendar(calendar, unit)
    with_unit = getattr(calendar, "with_unit", None)
    if calendar is None or not callable(with_unit):
        return calendar
    return with_unit(None, None, business_hours=union_hours(units))


def unit_after_doctor_change(
    professional: Any | None, current_unit_id: UUID | None, original_unit_id: UUID | None
) -> UUID | None:
    """The unit a booking keeps when its doctor changes to `professional`.

    The current (then the original) unit if the new doctor attends it (an active link); else the
    new doctor's only bookable unit; else none - with several units nobody chose one.
    """
    offered = {unit.id for unit in offered_units(professional)}
    for candidate in (current_unit_id, original_unit_id):
        if candidate is not None and candidate in offered:
            return candidate
    units = bookable_units(professional)
    return units[0].id if len(units) == 1 else None


def unit_rows(units: list) -> list[tuple[str, str, str | None]]:
    """One payload row per unit: the name as title, the address as description."""
    rows: list[tuple[str, str, str | None]] = []
    for unit in units[:MAX_UNIT_ROWS]:
        address = str(getattr(unit, "address", None) or "").strip()
        rows.append(
            (
                f"{ROW_UNIT_PREFIX}{unit.id}",
                truncate_list_row_title(str(unit.name)),
                truncate_plain(address, MAX_LIST_ROW_DESCRIPTION_CHARS) if address else None,
            )
        )
    return rows


def _key(text: str | None) -> str:
    return strip_decoration(text).casefold()


def match_unit(units: list, body: str | None) -> Any | None:
    """A tapped (embedded uuid) or typed (full or 24-char name) unit, or None."""
    if not body:
        return None
    payload = _TRAILING_PAYLOAD.search(body)
    raw = payload.group(1).strip() if payload else body.strip()
    try:
        tapped = UUID(raw)
    except ValueError:
        tapped = None
    if tapped is not None:
        found = next((unit for unit in units if unit.id == tapped), None)
        if found is not None:
            return found
    target = _key(_TRAILING_PAYLOAD.sub("", body))
    if not target:
        return None
    for unit in units:
        name = str(unit.name)
        if target in (_key(name), _key(truncate_list_row_title(name))):
            return unit
    return None


__all__ = [
    "BACK_TARGET_UNIT",
    "LABEL_ANOTHER_UNIT",
    "MAX_UNIT_ROWS",
    "ROW_UNIT_PREFIX",
    "STEP_AWAITING_UNIT",
    "UNIT_LIST_BODY",
    "appointment_calendar",
    "bookable_units",
    "effective_unit",
    "find_unit",
    "match_unit",
    "offered_units",
    "scope",
    "selected_unit_id",
    "union_hours",
    "unit_after_doctor_change",
    "unit_choice_needed",
    "unit_rows",
]
