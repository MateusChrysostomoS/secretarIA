"""The region the clinic's units have in common - what the greeting says after "em"/"no".

One city -> "em Campinas"; several cities in one state -> "em Minas Gerais" / "na Bahia" /
"no Paraná" (full state name + the state's own preposition); units in different states ->
"no Brasil". Only ACTIVE units that carry a city count (a legacy unit with only the
free-text address has none). A clinic with no unit
city falls back to the legacy `Tenant.address["city"]`, and with nothing at all the
greeting simply leaves the place out, as it always did.
"""

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models.tenant import Tenant
from secretaria.models.unit import Unit
from secretaria.services.greeting_template import tenant_city
from secretaria.services.unit_address import BR_STATE_NAMES, BR_STATE_PREPOSITIONS

COUNTRY_NAME = "Brasil"


@dataclass(frozen=True)
class Region:
    level: Literal["city", "state", "country"]
    name: str
    preposition: str  # "em" for a city; "em"/"no"/"na" per state; "no" for the country


def _squash(text: str) -> str:
    """Case-, accent- and whitespace-insensitive key to compare city names."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.split())


def region_of_units(units: Sequence[Unit], legacy_address: object = None) -> Region | None:
    """The common region of `units` (active + with a city only), else the legacy city, else None."""
    cities: list[tuple[str, str]] = []  # (name as typed, UF)
    for unit in units:
        if not unit.is_active:
            continue
        city = " ".join((unit.city or "").split())
        if not city:
            continue
        cities.append((city, (unit.state or "").strip().upper()))

    if not cities:
        legacy = tenant_city(legacy_address)
        return Region("city", legacy, "em") if legacy else None

    first_name = cities[0][0]
    if len({_squash(name) for name, _ in cities}) == 1:
        return Region("city", first_name, "em")

    states = {uf for _, uf in cities}
    if len(states) == 1:
        uf = next(iter(states))
        state_name = BR_STATE_NAMES.get(uf)
        if state_name:
            return Region("state", state_name, BR_STATE_PREPOSITIONS.get(uf, "em"))
    return Region("country", COUNTRY_NAME, "no")


async def load_clinic_region(session: AsyncSession, tenant: Tenant) -> Region | None:
    """`region_of_units` over this tenant's own units (always filtered by tenant_id)."""
    units = (
        await session.scalars(
            select(Unit)
            .where(Unit.tenant_id == tenant.id, Unit.is_active.is_(True))
            .order_by(Unit.created_at, Unit.id)
        )
    ).all()
    return region_of_units(units, tenant.address)
