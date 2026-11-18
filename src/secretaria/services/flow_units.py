"""The "which unit?" step of the deterministic booking flow (TASK-083 spec §8) - pure helpers.

A professional's offered units ride on the router's roster snapshot as `units` (a list of
services/unit_booking.py::UnitOption, loaded by the worker - the router does no DB I/O). From
them this module answers, for the booking in progress:

* no unit at all (no link)         -> no step, the flow is exactly today's;
* one bookable unit                -> no step, that unit is used (its hours, its address);
* two or more bookable units       -> the patient picks one (`STEP_AWAITING_UNIT`);
* the service done at one of 2+    -> Sim/Não on that unit (`STEP_AWAITING_UNIT_CONFIRM`);
* links but every unit closed      -> nobody can book this professional (the hours gap).

The step's rendering and its hooks live in services/flow_router.py (`_enter_unit_list`,
`_handle_unit_step`); this module holds what they share and never imports the router.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from secretaria.core.whatsapp_limits import (
    EMOJI_BACK,
    MAX_LIST_ROW_DESCRIPTION_CHARS,
    decorate,
    strip_decoration,
    truncate_list_row_title,
    truncate_plain,
)
from secretaria.services.professional_service_units import allowed_unit_ids
from secretaria.services.service_catalog import entry_service_id
from secretaria.services.service_terms import (
    ResolvedTerms,
    TermsByService,
    price_differs_across_units,
    resolve_terms,
    terms_for_entry,
)
from secretaria.services.unit_booking import scoped_calendar

if TYPE_CHECKING:
    from secretaria.services.insurance_acceptance import AcceptanceExceptions

STEP_AWAITING_UNIT = "awaiting_unit"
# The day/slot pickers' back row when the professional has a unit to choose: one step back is
# the unit list, not the service list. "⬅️ Outra unidade" is 19 characters (<= 24).
BACK_TARGET_UNIT = "unit"
LABEL_ANOTHER_UNIT = decorate(EMOJI_BACK, "Outra unidade")
UNIT_LIST_BODY = "Em qual unidade você prefere ser atendido(a)?"
# The picked unit stopped being offered mid-booking and only one is left (final wave M1).
UNIT_GONE_NOTICE = "A unidade escolhida não está mais disponível."
# TASK-083 follow-up (spec §5.2-3, §9.1): the service is done at ONE of the doctor's two or more
# bookable units - ask Sim/Não before opening its days. The unit is saved only on "Sim".
STEP_AWAITING_UNIT_CONFIRM = "awaiting_unit_confirm"
# The owner's text, verbatim. A unit without an address drops ", em {endereco}" (never "None").
UNIT_CONFIRM_BODY = (
    "O serviço {servico} feito por {medico} é feito apenas na unidade {unidade}, em {endereco}. "
    "Você confirma essa unidade?"
)
UNIT_CONFIRM_BODY_NO_ADDRESS = (
    "O serviço {servico} feito por {medico} é feito apenas na unidade {unidade}. "
    "Você confirma essa unidade?"
)
# TASK-086 (inventory A5/R5): the same Sim/Não when it is the CONVÊNIO that leaves one unit
# (the service itself is done at two or more) - never "o serviço é feito apenas", which would
# blame the clinic's unit setup for a convênio rule.
UNIT_CONFIRM_PLAN_BODY = (
    "Pelo convênio {convenio}, o serviço {servico} com {medico} é atendido apenas na unidade "
    "{unidade}, em {endereco}. Você confirma essa unidade?"
)
UNIT_CONFIRM_PLAN_BODY_NO_ADDRESS = (
    "Pelo convênio {convenio}, o serviço {servico} com {medico} é atendido apenas na unidade "
    "{unidade}. Você confirma essa unidade?"
)
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


def service_units_of(professional: Any | None) -> dict[UUID, frozenset[UUID]]:
    """The professional's restricted services `{service_id: unit ids}` (getattr: older snapshots
    predate the field). A service absent from it is done at every unit they attend."""
    if professional is None:
        return {}
    return getattr(professional, "service_units", None) or {}


def _plan_filter_active(
    service_id: UUID | None, plan_id: Any, refused: AcceptanceExceptions | None
) -> bool:
    """Whether the convênio matrix (TASK-086) can narrow anything for this question.

    Needs a service, a plan with an id and at least one refused cell; otherwise every helper
    below answers exactly what it answered before the matrix existed (no kwargs = today's)."""
    if service_id is None or plan_id is None or refused is None:
        return False
    return bool(refused.clinic or refused.by_professional)


def _accepted_at(
    professional: Any | None,
    service_id: UUID | None,
    unit_id: UUID | None,
    plan_id: Any,
    refused: AcceptanceExceptions | None,
) -> bool:
    if not _plan_filter_active(service_id, plan_id, refused):
        return True
    # Imported here: services/insurance_acceptance.py builds on this module.
    from secretaria.services.insurance_acceptance import is_accepted

    return is_accepted(refused, getattr(professional, "id", None), service_id, unit_id, plan_id)


def units_for_service(
    professional: Any | None,
    service_id: UUID | None,
    *,
    plan_id: Any = None,
    refused: AcceptanceExceptions | None = None,
) -> list:
    """The bookable units where `service_id` is done: bookable AND allowed for the service.

    `service_id=None` (no service picked yet, or an entry without one) is no filter: exactly
    `bookable_units`. A restriction whose units are all closed/inactive yields [] - never "all".

    TASK-086 (the single unit x convênio filter): with `plan_id` and the clinic's `refused`
    matrix, only the units where that convênio is accepted for the service remain. Without them
    (or with an empty matrix) nothing changes.
    """
    bookable = bookable_units(professional)
    allowed = allowed_unit_ids(service_units_of(professional), service_id)
    units = bookable if allowed is None else [unit for unit in bookable if unit.id in allowed]
    if not _plan_filter_active(service_id, plan_id, refused):
        return units
    return [
        unit for unit in units if _accepted_at(professional, service_id, unit.id, plan_id, refused)
    ]


def service_unit_terms_of(professional: Any | None) -> TermsByService:
    """The professional's per-unit price/duration exceptions (getattr: older snapshots predate
    the field)."""
    if professional is None:
        return {}
    return getattr(professional, "service_unit_terms", None) or {}


def terms_at(professional: Any | None, entry: Mapping, unit_id: UUID | None) -> ResolvedTerms:
    """`entry`'s effective price/duration for this professional at `unit_id` (None = base)."""
    terms = terms_for_entry(service_unit_terms_of(professional), entry)
    return resolve_terms(entry, terms, unit_id)


@dataclass(frozen=True)
class ShownTerms:
    """What a text may show about a service's price/duration (D3/D7)."""

    price: str | None  # None = nothing to show (no price set, or hidden by D3)
    duration_min: int | None
    price_hidden: bool  # True ONLY when D3 hid a price (differs across 2+ units, unit unknown)
    unit_id: UUID | None  # the unit the values were resolved at (given, or the only one)


def shown_terms(
    professional: Any | None, entry: Mapping, unit_id: UUID | None = None
) -> ShownTerms:
    """D3/D7/R2 in one place, for every text that shows a service's price/duration.

    Unit given: resolved there. Else one bookable unit for the service: resolved there. Else none:
    the base. Else (2+): the price is hidden when it differs across them (never a range), or the
    one shared price is shown; the duration is the BASE either way (no unit known yet).
    """
    if unit_id is not None:
        resolved = terms_at(professional, entry, unit_id)
        return ShownTerms(resolved.price, resolved.duration_min, False, unit_id)
    units = units_for_service(professional, entry_service_id(entry))
    if len(units) == 1:
        only = units[0].id
        resolved = terms_at(professional, entry, only)
        return ShownTerms(resolved.price, resolved.duration_min, False, only)
    base = resolve_terms(entry, None, None)
    if not units:
        return ShownTerms(base.price, base.duration_min, False, None)
    terms = terms_for_entry(service_unit_terms_of(professional), entry)
    if price_differs_across_units(entry, terms, [unit.id for unit in units]):
        return ShownTerms(None, base.duration_min, True, None)
    first = resolve_terms(entry, terms, units[0].id)
    return ShownTerms(first.price, base.duration_min, False, None)


def unit_choice_needed(professional: Any | None, service_id: UUID | None = None) -> bool:
    """Two or more bookable units (for the service, when given): the patient has to pick one."""
    return len(units_for_service(professional, service_id)) > 1


@dataclass(frozen=True)
class UnitChoice:
    """What the booking flow does about the unit for (professional, service).

    * `free`    - the professional has no unit link: no step, today's flow;
    * `none`    - links, but no bookable unit does this service: the "0 unidades" case;
    * `implied` - one unit and the doctor attends at no other bookable one: used silently;
    * `confirm` - the service is restricted to one of two or more bookable units: ask Sim/Não;
    * `list`    - two or more units: the patient picks.

    `units` are the `UnitOption`s on offer ([] for `free` and `none`).

    `by_plan` (TASK-086): the convênio matrix, not the clinic's unit setup, is why the choice is
    what it is - `none` because the convênio is refused at every unit doing the service (or, for
    a doctor without units, in the "no unit" slice), or `confirm`/`list` because the convênio
    narrowed the units the service itself is done at. A `by_plan` `none` must never alert the
    clinic about a missing unit (inventory R4) and a `by_plan` `confirm` names the convênio (R5).
    """

    kind: Literal["free", "none", "implied", "confirm", "list"]
    units: list
    by_plan: bool = False


def unit_choice(
    professional: Any | None,
    service_id: UUID | None,
    *,
    plan_id: Any = None,
    refused: AcceptanceExceptions | None = None,
) -> UnitChoice:
    """Pure decision for the unit step (no I/O); see `UnitChoice` for the kinds.

    `plan_id`/`refused` (TASK-086): the conversation's convênio and the clinic's matrix; left
    out (or an empty matrix), the decision is exactly today's."""
    if not offered_units(professional):
        if not _accepted_at(professional, service_id, None, plan_id, refused):
            return UnitChoice("none", [], by_plan=True)
        return UnitChoice("free", [])
    units = units_for_service(professional, service_id)
    if not units:
        return UnitChoice("none", [])
    accepted = units_for_service(professional, service_id, plan_id=plan_id, refused=refused)
    if not accepted:
        return UnitChoice("none", [], by_plan=True)
    if len(accepted) > 1:
        return UnitChoice("list", accepted, by_plan=len(accepted) < len(units))
    if len(bookable_units(professional)) >= 2:
        return UnitChoice("confirm", accepted, by_plan=len(units) > 1)
    return UnitChoice("implied", accepted)


def unit_confirm_body(
    service_name: str, professional: Any | None, unit: Any, *, convenio: str | None = None
) -> str:
    """The Sim/Não question for `unit`, the only one of the doctor's units doing the service.

    The unit's address is the one the unit list shows; without one the sentence ends at the
    unit's name (`UNIT_CONFIRM_BODY_NO_ADDRESS`). A trailing period on the address is dropped so
    the sentence never gets "..". `convenio` (TASK-086): the convênio is what leaves this one
    unit, and the sentence says so (`UNIT_CONFIRM_PLAN_BODY`).
    """
    address = str(getattr(unit, "address", None) or "").strip().rstrip(".").strip()
    values = {
        "servico": str(service_name or "").strip(),
        "medico": str(getattr(professional, "name", None) or "").strip(),
        "unidade": str(getattr(unit, "name", None) or "").strip(),
    }
    plan = str(convenio or "").strip()
    if plan:
        if address:
            return UNIT_CONFIRM_PLAN_BODY.format(convenio=plan, endereco=address, **values)
        return UNIT_CONFIRM_PLAN_BODY_NO_ADDRESS.format(convenio=plan, **values)
    if address:
        return UNIT_CONFIRM_BODY.format(endereco=address, **values)
    return UNIT_CONFIRM_BODY_NO_ADDRESS.format(**values)


def find_unit(
    professional: Any | None,
    unit_id: UUID | None,
    service_id: UUID | None = None,
    *,
    plan_id: Any = None,
    refused: AcceptanceExceptions | None = None,
) -> Any | None:
    if unit_id is None:
        return None
    units = units_for_service(professional, service_id, plan_id=plan_id, refused=refused)
    return next((unit for unit in units if unit.id == unit_id), None)


def selected_unit_id(conversation: Any) -> UUID | None:
    """The conversation's picked unit (getattr: older snapshots predate the field)."""
    return getattr(conversation, "flow_selected_unit_id", None)


def effective_unit(
    conversation: Any,
    professional: Any | None,
    service_id: UUID | None = None,
    *,
    plan_id: Any = None,
    refused: AcceptanceExceptions | None = None,
) -> Any | None:
    """The unit this booking happens at: the one picked, else the only one on offer, else None.

    With `service_id`, only units where the service is done count: a pick outside that filter is
    ignored, and the "only one" is the single filtered unit when the choice is `implied` (a
    `confirm` is not decided until the patient says yes, which stores it as the pick). A pick that
    is no longer bookable (unit deactivated, link removed) is ignored. `plan_id`/`refused`
    (TASK-086): a unit where the convênio is refused counts as not doing the service.
    """
    plan = {"plan_id": plan_id, "refused": refused}
    picked = find_unit(professional, selected_unit_id(conversation), service_id, **plan)
    if picked is not None:
        return picked
    choice = unit_choice(professional, service_id, **plan)
    if choice.kind == "implied":
        return choice.units[0]
    return None


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
    professional: Any | None,
    current_unit_id: UUID | None,
    original_unit_id: UUID | None,
    service_id: UUID | None = None,
) -> UUID | None:
    """The unit a booking keeps when its doctor changes to `professional`.

    The current (then the original) unit if the new doctor attends it (an active link) AND does
    `service_id` there; else the new doctor's only bookable unit for the service
    (`units_for_service`); else none - with several units nobody chose one, with zero the caller
    refuses (`unit_refused`). `service_id=None` (an entry without one) is no filter.
    """
    allowed = allowed_unit_ids(service_units_of(professional), service_id)
    offered = {
        unit.id for unit in offered_units(professional) if allowed is None or unit.id in allowed
    }
    for candidate in (current_unit_id, original_unit_id):
        if candidate is not None and candidate in offered:
            return candidate
    units = units_for_service(professional, service_id)
    return units[0].id if len(units) == 1 else None


def unit_refused(professional: Any | None, unit_id: UUID | None, service_id: UUID | None) -> bool:
    """Whether a booking of `service_id` with `professional` at `unit_id` must be refused.

    Only a restricted service is ever refused (no marks, no links or an entry without
    `service_id` is today's behaviour): at a unit outside its set, or - with no unit on the
    booking - when no bookable unit of the doctor does it (the "0 unidades" case).
    """
    allowed = allowed_unit_ids(service_units_of(professional), service_id)
    if allowed is None:
        return False
    if unit_id is not None:
        return unit_id not in allowed
    return not units_for_service(professional, service_id)


def services_at_unit(professional: Any | None, entries: list[dict], unit_id: UUID | None) -> list:
    """The professional's `entries` done at `unit_id` - a unit they can be booked at.

    Same data rule as everywhere (follow-up E1): an entry without `service_id`, or a service
    without marks, is done at every unit of the professional; a marked one only at its marks.
    A unit the professional is not bookable at (no link, inactive, closed) has no services.
    """
    if unit_id is None or unit_id not in {unit.id for unit in bookable_units(professional)}:
        return []
    marks = service_units_of(professional)
    out = []
    for entry in entries:
        allowed = allowed_unit_ids(marks, entry_service_id(entry))
        if allowed is None or unit_id in allowed:
            out.append(entry)
    return out


def units_with_services(professional: Any | None, entries: list[dict]) -> list:
    """The professional's bookable units where at least one of `entries` is done - the units a
    patient may pick for them without landing on an empty service list."""
    return [
        unit
        for unit in bookable_units(professional)
        if services_at_unit(professional, entries, unit.id)
    ]


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
    "STEP_AWAITING_UNIT_CONFIRM",
    "UNIT_CONFIRM_BODY",
    "UNIT_CONFIRM_BODY_NO_ADDRESS",
    "UNIT_CONFIRM_PLAN_BODY",
    "UNIT_CONFIRM_PLAN_BODY_NO_ADDRESS",
    "UNIT_LIST_BODY",
    "UnitChoice",
    "appointment_calendar",
    "bookable_units",
    "effective_unit",
    "find_unit",
    "match_unit",
    "offered_units",
    "scope",
    "selected_unit_id",
    "service_units_of",
    "services_at_unit",
    "union_hours",
    "unit_after_doctor_change",
    "unit_choice",
    "unit_refused",
    "unit_choice_needed",
    "unit_confirm_body",
    "unit_rows",
    "units_for_service",
    "units_with_services",
]
