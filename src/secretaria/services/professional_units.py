"""Which of the tenant's units a professional attends, and the hours there (TASK-083, spec §5/§8).

A `ProfessionalUnit` row links one professional to one unit of the SAME tenant. This module owns
the rules around those rows; the hub (api/hub/professional_units.py) only adds auth, entitlement
and the HTTP shape.

## Hours per unit (NULL versus EMPTY - the same three states as `Professional.business_hours`)

* link hours NULL      -> inherit the professional's RESOLVED hours
                          (`tenant_config.professional_business_hours`: their own value,
                          or, when that is NULL too, the clinic's);
* link hours `{}`      -> an OWN schedule that is empty: closed all week in this unit. It inherits
                          NOTHING. `is None` is load-bearing here, never truthiness;
* link hours non-empty -> that schedule, with closed weekdays dropped (same filtering as every other
                          resolver).

## What is offered

`available_unit_ids` = the professional's ACTIVE links to ACTIVE units of their own tenant. A
deactivated unit keeps its links and per-unit hours (reactivating brings them back untouched) but
stops being offered. No link at all = an agenda without a unit, exactly as before this feature.
`available_unit_ids` is the STAFF view (hub, staff edits); what a PATIENT is offered is further
narrowed to USABLE units (`unit_is_usable`: active + complete address, TASK-090 Adendo 1) by
services/unit_booking.py::unit_options_for and the AI unit tools.

The BUSY time is never per unit (the person is in one place at a time): that is
services/agenda.py's job, not this module's.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Professional, Tenant
from secretaria.models.professional_unit import ProfessionalUnit
from secretaria.models.unit import Unit
from secretaria.services import tenant_config as cfg
from secretaria.services.unit_address import address_complete


class UnitNotFound(Exception):
    """A unit id is malformed or does not belong to this tenant.

    The same error for "does not exist" and "belongs to another tenant", on purpose: telling them
    apart would leak the existence of another clinic's rows.
    """


@dataclass(frozen=True)
class LinkRequest:
    """One wanted link: the unit and its own hours (`None` = inherit, `{}` = closed)."""

    unit_id: str | UUID
    business_hours: dict | None = None


def _parse_unit_id(raw: str | UUID) -> UUID:
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(raw)
    except ValueError:
        raise UnitNotFound from None


async def active_units(session: AsyncSession, tenant_id: UUID) -> list[Unit]:
    """Every ACTIVE unit of the tenant, ordered by name."""
    rows = await session.scalars(
        select(Unit)
        .where(Unit.tenant_id == tenant_id, Unit.is_active.is_(True))
        .order_by(Unit.name, Unit.id)
    )
    return list(rows)


def unit_is_usable(unit: Unit) -> bool:
    """A unit a PATIENT may see or book at (TASK-090 Adendo 1 §A): active AND with a complete
    address (services/unit_address.address_complete - the same test a new unit must pass).

    THE one predicate: every patient-facing reader (flow unit options, AI unit tools, greeting
    region, the "secretária só atende com unidade completa" gate) goes through it - via
    `usable_only` / `usable_units` / `has_usable_units` below or a module-attribute call - and
    never copies it. The manager CRUD keeps showing every unit (with `address_incomplete`).
    """
    return bool(unit.is_active) and address_complete(unit)


def usable_only(units: Iterable[Unit]) -> list[Unit]:
    """`units` filtered by `unit_is_usable`, order preserved."""
    return [unit for unit in units if unit_is_usable(unit)]


async def usable_units(session: AsyncSession, tenant_id: UUID) -> list[Unit]:
    """The tenant's USABLE units (active + complete address), ordered by name."""
    return usable_only(await active_units(session, tenant_id))


async def has_usable_units(session: AsyncSession, tenant_id: UUID) -> bool:
    """Whether the tenant has at least one USABLE unit.

    Both the switch for the AI unit tools/prompt (`registry.agent_tools_for(has_active_units=)`)
    and the reply gate (workers/shared/unit_gate.py): a clinic without one is not served.
    """
    return any(unit_is_usable(unit) for unit in await active_units(session, tenant_id))


async def links_by_unit(
    session: AsyncSession, professional_id: UUID
) -> dict[UUID, ProfessionalUnit]:
    """ALL of the professional's link rows (any state), keyed by unit id."""
    rows = await session.scalars(
        select(ProfessionalUnit).where(ProfessionalUnit.professional_id == professional_id)
    )
    return {row.unit_id: row for row in rows}


async def available_unit_ids(session: AsyncSession, professional: Professional) -> list[UUID]:
    """Units this professional offers right now: active link AND active unit of their tenant.

    Ordered by the link's `sort_order`, then the unit's name. Empty without links (agenda without
    a unit) and after every linked unit was deactivated.
    """
    rows = await session.execute(
        select(ProfessionalUnit.unit_id)
        .join(Unit, Unit.id == ProfessionalUnit.unit_id)
        .where(
            ProfessionalUnit.professional_id == professional.id,
            ProfessionalUnit.is_active.is_(True),
            Unit.tenant_id == professional.tenant_id,
            Unit.is_active.is_(True),
        )
        .order_by(ProfessionalUnit.sort_order, Unit.name, Unit.id)
    )
    return [unit_id for (unit_id,) in rows]


def _own_hours(raw: dict) -> dict:
    """Weekday -> non-empty windows; same filtering as `tenant_config._filter_active_hours`."""
    return {day: windows for day, windows in raw.items() if windows}


async def unit_hours(
    session: AsyncSession,
    professional: Professional,
    unit: Unit,
    tenant: Tenant | None = None,
) -> dict | None:
    """The professional's business hours in `unit`, or None when the unit is not offered.

    None means "not offered here" (no active link, a deactivated unit, another tenant's unit);
    `{}` means "offered, but closed all week". See the module docstring for the NULL/`{}` rule.
    """
    if unit.tenant_id != professional.tenant_id or not unit.is_active:
        return None
    link = await session.get(ProfessionalUnit, (professional.id, unit.id))
    if link is None or not link.is_active:
        return None
    if link.business_hours is not None:
        return _own_hours(link.business_hours)
    if tenant is None:
        tenant = await session.get(Tenant, professional.tenant_id)
    if tenant is None:
        return {}
    return cfg.professional_business_hours(professional, tenant)


async def resolve_active_target_units(
    session: AsyncSession, tenant_id: UUID, wanted: Sequence[LinkRequest]
) -> list[LinkRequest]:
    """Validate the wanted unit ids against the tenant and keep only the ACTIVE ones.

    A malformed id, an unknown id or another tenant's unit raises `UnitNotFound` (the whole
    request is refused, nothing is written). An id of a DEACTIVATED unit of this tenant is simply
    dropped: its links are preserved untouched by `replace_links`, not rewritten from the body.
    """
    if not wanted:
        return []
    parsed = [(_parse_unit_id(item.unit_id), item.business_hours) for item in wanted]
    unit_ids = {unit_id for unit_id, _ in parsed}
    rows = await session.execute(
        select(Unit.id, Unit.is_active).where(Unit.id.in_(unit_ids), Unit.tenant_id == tenant_id)
    )
    state = {unit_id: is_active for unit_id, is_active in rows}
    if set(state) != unit_ids:
        raise UnitNotFound
    return [LinkRequest(unit_id, hours) for unit_id, hours in parsed if state[unit_id]]


async def replace_links(
    session: AsyncSession,
    professional: Professional,
    wanted: Sequence[LinkRequest],
) -> None:
    """Replace the professional's links to ACTIVE units of their tenant with `wanted`.

    `wanted` must already be validated and filtered by `resolve_active_target_units`. Links to
    DEACTIVATED units (and their hours) are never read or written here. `sort_order` is the
    position in `wanted`. The caller commits.

    Starts by locking the professional's row (`SELECT ... FOR UPDATE`; a no-op on SQLite) so
    two concurrent saves of one professional run one after the other instead of colliding on
    the (professional, unit) key (final wave T6).
    """
    await session.execute(
        select(Professional.id)
        .where(Professional.id == professional.id, Professional.tenant_id == professional.tenant_id)
        .with_for_update()
    )
    active_ids = [unit.id for unit in await active_units(session, professional.tenant_id)]
    if active_ids:
        await session.execute(
            delete(ProfessionalUnit).where(
                ProfessionalUnit.professional_id == professional.id,
                ProfessionalUnit.unit_id.in_(active_ids),
            )
        )
    for position, item in enumerate(wanted):
        session.add(
            ProfessionalUnit(
                professional_id=professional.id,
                unit_id=item.unit_id,
                business_hours=item.business_hours,
                is_active=True,
                sort_order=position,
            )
        )
    await session.flush()


async def count_professionals_by_unit(
    session: AsyncSession, tenant_id: UUID, unit_ids: Iterable[UUID] | None = None
) -> dict[UUID, int]:
    """ACTIVE professionals of the tenant linked through an ACTIVE link, per unit id.

    Units without such a professional are absent from the result (callers default to 0).
    """
    query = (
        select(
            ProfessionalUnit.unit_id, func.count(func.distinct(ProfessionalUnit.professional_id))
        )
        .join(Professional, Professional.id == ProfessionalUnit.professional_id)
        .join(Unit, Unit.id == ProfessionalUnit.unit_id)
        .where(
            Professional.tenant_id == tenant_id,
            Professional.is_active.is_(True),
            Unit.tenant_id == tenant_id,
            ProfessionalUnit.is_active.is_(True),
        )
        .group_by(ProfessionalUnit.unit_id)
    )
    if unit_ids is not None:
        query = query.where(ProfessionalUnit.unit_id.in_(list(unit_ids)))
    rows = await session.execute(query)
    return {unit_id: int(count) for unit_id, count in rows}
