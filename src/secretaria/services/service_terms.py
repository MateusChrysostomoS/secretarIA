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


@dataclass(frozen=True)
class UnitTerms:
    """One exception row: either half may be None (= inherit the base)."""

    price: str | None
    duration_min: int | None


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
    return ResolvedTerms(
        own_price if own_price is not None else price,
        row.duration_min if row.duration_min is not None else duration,
        own=True,
    )


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
        ).where(
            ProfessionalServiceUnitTerms.tenant_id == tenant_id,
            ProfessionalServiceUnitTerms.professional_id.in_(ids),
        )
    )
    for professional_id, service_id, unit_id, price, duration_min in rows:
        result[professional_id].setdefault(service_id, {})[unit_id] = UnitTerms(price, duration_min)
    return result


async def load_terms(
    session: AsyncSession, tenant_id: UUID, professional_id: UUID
) -> TermsByService:
    """`load_terms_for` for one professional."""
    return (await load_terms_for(session, tenant_id, [professional_id]))[professional_id]
