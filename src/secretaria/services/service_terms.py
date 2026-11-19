"""Price/duration of a service for a (professional, unit): the single resolver (TASK-085).

Decisions D2/D3/D7 of the SPEC.

The professional's entry in `appointment_types` carries the BASE `price` / `duration_min`. A row
of `professional_service_unit_terms` is an EXCEPTION for one unit. This module is the only place
that turns "base + exceptions + unit" into the value a reader uses:

* `resolve_terms`            unit exception > base; no row = base; a row with only a price (or only
                             a duration) inherits the other half from the base; no unit known = base
                             (D7);
* `price_differs_across_units`  D3 - True when the effective price is not the same in every unit
                             where the service is done, so the caller HIDES the price until the unit
                             is chosen. Deliberately no "price range": hide, never "from X to Y";
* `blocked_weekdays_for` / `blocked_weekdays_everywhere`  TASK-087 - weekdays a service cannot be
                             booked by patients: unit exception (NULL = inherit) > base list on the
                             entry; with no unit chosen, only the days blocked in EVERY unit;
* `load_terms` / `load_terms_for`  the rows, tenant-scoped:
                             `{professional: {service: {unit: UnitTerms}}}`.

The two resolver functions are pure (rows already loaded, no I/O) so the flow router can use them
on a snapshot. Money equality goes through `parse_brl_to_cents` ("250" == "R$ 250,00"); free text
that is not money ("a combinar") compares casefolded.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models.professional_service_unit_terms import ProfessionalServiceUnitTerms
from secretaria.services.payments.money import parse_brl_to_cents
from secretaria.services.service_catalog import entry_service_id

# Keys of the weekdays in `business_hours` and in `blocked_weekdays` (TASK-087), Monday first.
WEEKDAY_KEYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass(frozen=True)
class UnitTerms:
    """One exception row: every part may be None (= inherit the base).

    `blocked_weekdays` (TASK-087): None = inherit the base list, an empty set = own (nothing
    blocked at this unit), a non-empty set = own.
    """

    price: str | None
    duration_min: int | None
    blocked_weekdays: frozenset[str] | None = None


@dataclass(frozen=True)
class ResolvedTerms:
    """The effective price/duration; `own` is True when a unit row contributed."""

    price: str | None
    duration_min: int | None
    own: bool


# {service_id: {unit_id: UnitTerms}} for one professional.
TermsByService = dict[UUID, dict[UUID, UnitTerms]]


def _blank_to_none(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _base_duration(entry: Mapping) -> int | None:
    value = entry.get("duration_min")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def resolve_terms(
    entry: Mapping,
    terms_by_unit: Mapping[UUID, UnitTerms] | None,
    unit_id: UUID | None,
) -> ResolvedTerms:
    """Effective price/duration of `entry` (the professional's base) at `unit_id`.

    `terms_by_unit` is the exceptions of THIS (professional, service): `{unit_id: UnitTerms}`.
    """
    price = _blank_to_none(entry.get("price"))
    duration = _base_duration(entry)
    row = terms_by_unit.get(unit_id) if terms_by_unit and unit_id is not None else None
    if row is None:
        return ResolvedTerms(price, duration, own=False)
    own_price = _blank_to_none(row.price)
    if own_price is None and row.duration_min is None:
        # TASK-087: a row that carries only weekdays contributes no price/duration of its own.
        return ResolvedTerms(price, duration, own=False)
    return ResolvedTerms(
        own_price if own_price is not None else price,
        row.duration_min if row.duration_min is not None else duration,
        own=True,
    )


def _weekday_set(value) -> frozenset[str]:
    """A stored `blocked_weekdays` list as a set of known keys (anything else is ignored)."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(day for day in value if isinstance(day, str) and day in WEEKDAY_KEYS)


def blocked_weekdays_for(
    entry: Mapping,
    terms_by_unit: Mapping[UUID, UnitTerms] | None,
    unit_id: UUID | None,
) -> frozenset[str]:
    """Weekdays on which patients cannot book `entry`'s service at `unit_id` (TASK-087).

    The unit's own list (when the row carries one, even an empty one) beats the base list on the
    professional's entry; no unit known (`unit_id` None) = the base. Absent/None base = nothing.
    """
    row = terms_by_unit.get(unit_id) if terms_by_unit and unit_id is not None else None
    if row is not None and row.blocked_weekdays is not None:
        return _weekday_set(row.blocked_weekdays)
    return _weekday_set(entry.get("blocked_weekdays"))


def blocked_weekdays_everywhere(
    entry: Mapping,
    terms_by_unit: Mapping[UUID, UnitTerms] | None,
    unit_ids: Collection[UUID],
) -> frozenset[str]:
    """Weekdays blocked in EVERY one of `unit_ids` (R1: unit not chosen yet = union of openings).

    A day stays bookable when at least one unit allows it, so only the intersection is blocked.
    No units = the base list.
    """
    if not unit_ids:
        return blocked_weekdays_for(entry, terms_by_unit, None)
    per_unit = [blocked_weekdays_for(entry, terms_by_unit, unit_id) for unit_id in set(unit_ids)]
    return frozenset.intersection(*per_unit)


def _price_key(price: str | None) -> tuple[str, int | str | None]:
    if price is None:
        return ("none", None)
    cents = parse_brl_to_cents(price)
    if cents is not None:
        return ("money", cents)
    return ("text", price.casefold())


def price_differs_across_units(
    entry: Mapping,
    terms_by_unit: Mapping[UUID, UnitTerms] | None,
    unit_ids: Iterable[UUID],
) -> bool:
    """True when the effective price is not identical in every one of `unit_ids` (D3).

    `unit_ids` are the units where the professional does the service. Zero or one unit never
    differs (nothing to choose between); a unit without a row counts with the base price.
    """
    keys = {
        _price_key(resolve_terms(entry, terms_by_unit, unit_id).price) for unit_id in set(unit_ids)
    }
    return len(keys) > 1


async def load_terms_for(
    session: AsyncSession, tenant_id: UUID, professional_ids: Collection[UUID]
) -> dict[UUID, TermsByService]:
    """The exceptions of many professionals in ONE query, scoped to `tenant_id`.

    A professional that has none (or is not of this tenant) maps to `{}`; an empty request
    returns `{}`.
    """
    ids = list(professional_ids)
    result: dict[UUID, TermsByService] = {pid: {} for pid in ids}
    if not ids:
        return result
    rows = await session.execute(
        select(
            ProfessionalServiceUnitTerms.professional_id,
            ProfessionalServiceUnitTerms.service_id,
            ProfessionalServiceUnitTerms.unit_id,
            ProfessionalServiceUnitTerms.price,
            ProfessionalServiceUnitTerms.duration_min,
            ProfessionalServiceUnitTerms.blocked_weekdays,
        ).where(
            ProfessionalServiceUnitTerms.tenant_id == tenant_id,
            ProfessionalServiceUnitTerms.professional_id.in_(ids),
        )
    )
    for professional_id, service_id, unit_id, price, duration_min, blocked in rows:
        result[professional_id].setdefault(service_id, {})[unit_id] = UnitTerms(
            price, duration_min, None if blocked is None else _weekday_set(blocked)
        )
    return result


async def load_terms(
    session: AsyncSession, tenant_id: UUID, professional_id: UUID
) -> TermsByService:
    """`load_terms_for` for one professional."""
    return (await load_terms_for(session, tenant_id, [professional_id]))[professional_id]


def terms_for_entry(
    terms: Mapping[UUID, Mapping[UUID, UnitTerms]] | None, entry: Mapping
) -> Mapping[UUID, UnitTerms] | None:
    """The `{unit_id: UnitTerms}` of `entry`'s service out of ONE professional's TermsByService.

    None when `terms` is empty/None or the entry has no (valid) service_id.
    """
    if not terms:
        return None
    service_id = entry_service_id(entry)
    if service_id is None:
        return None
    return terms.get(service_id)


async def resolve_entry_at_unit(
    session: AsyncSession,
    tenant_id: UUID,
    professional_id: UUID | None,
    entry: Mapping,
    unit_id: UUID | None,
) -> ResolvedTerms:
    """For DB readers that know the unit (a booked appointment, a hold, an edit draft).

    The BASE (`resolve_terms(entry, None, None)`) WITHOUT any query when `unit_id` or
    `professional_id` is None or the entry has no service_id; otherwise that professional's rows
    are loaded tenant-scoped and resolved (another tenant's professional has none = base).
    """
    if unit_id is None or professional_id is None or entry_service_id(entry) is None:
        return resolve_terms(entry, None, None)
    terms = await load_terms(session, tenant_id, professional_id)
    return resolve_terms(entry, terms_for_entry(terms, entry), unit_id)
