"""At which units a professional does each service (TASK-083 follow-up, spec §3/§6.1/§9).

A `ProfessionalServiceUnit` row says "this professional does this service at this unit". The rule
that gives the rows their meaning:

* NO row for (professional, service) -> the service is done at EVERY unit the professional
  attends. This is every existing clinic, and every entry of the professional's list that has no
  `service_id` (such an entry can never be restricted);
* one or more rows                   -> the service is done ONLY at those units.

The empty set is never persisted as "none". `apply_marks` is the only writer: it validates the
whole request first (same clinic, units linked to the professional, services offered by them,
nothing restricted left without a unit) and only then touches the table, so a refusal changes
nothing. The caller owns the transaction (no commit here) and the HTTP shape; this module owns the
rules, like `professional_units.py` next to it.
"""

from collections.abc import Collection, Mapping
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Professional, Tenant
from secretaria.models.professional_service_unit import ProfessionalServiceUnit
from secretaria.services import tenant_config as cfg
from secretaria.services.service_catalog import entry_service_id, load_service_catalog

SERVICE_NOT_OFFERED = "service_not_offered"
UNIT_NOT_LINKED = "unit_not_linked"
SERVICE_WITHOUT_UNIT = "service_without_unit"


class ServiceMarkError(Exception):
    """A requested set of marks is refused; nothing was written.

    `code` is one of:

    * `service_not_offered`  - a marked service is not in the professional's active catalog (or
                               belongs to another clinic);
    * `unit_not_linked`      - a marked unit is not linked to the professional (this also covers
                               another clinic's unit; the API maps that to 404, not this layer);
    * `service_without_unit` - a restricted service would be left with no unit at all.

    `service_ids` are the services that caused it, in request order.
    """

    def __init__(self, code: str, service_ids: Collection[UUID]):
        self.code = code
        self.service_ids = list(service_ids)
        super().__init__(f"{code}: {', '.join(str(s) for s in self.service_ids)}")


def allowed_unit_ids(
    marks: Mapping[UUID, frozenset[UUID]], service_id: UUID | None
) -> frozenset[UUID] | None:
    """The units a service is restricted to, or None for "every unit of the professional".

    None also covers a service without rows and an entry without `service_id` (`service_id=None`).
    """
    if service_id is None:
        return None
    return marks.get(service_id)


def _group(rows) -> dict[UUID, set[UUID]]:
    grouped: dict[UUID, set[UUID]] = {}
    for service_id, unit_id in rows:
        grouped.setdefault(service_id, set()).add(unit_id)
    return grouped


async def read_marks(session: AsyncSession, professional_id: UUID) -> dict[UUID, frozenset[UUID]]:
    """The professional's restricted services: `{service_id: units}`. No entry = every unit."""
    rows = await session.execute(
        select(ProfessionalServiceUnit.service_id, ProfessionalServiceUnit.unit_id).where(
            ProfessionalServiceUnit.professional_id == professional_id
        )
    )
    return {service_id: frozenset(units) for service_id, units in _group(rows).items()}


async def read_marks_for(
    session: AsyncSession, professional_ids: Collection[UUID]
) -> dict[UUID, dict[UUID, frozenset[UUID]]]:
    """`read_marks` for many professionals in ONE query.

    Every requested id is present in the result (an empty dict when it has no marks).
    """
    ids = list(professional_ids)
    result: dict[UUID, dict[UUID, frozenset[UUID]]] = {pid: {} for pid in ids}
    if not ids:
        return result
    rows = await session.execute(
        select(
            ProfessionalServiceUnit.professional_id,
            ProfessionalServiceUnit.service_id,
            ProfessionalServiceUnit.unit_id,
        ).where(ProfessionalServiceUnit.professional_id.in_(ids))
    )
    grouped: dict[UUID, dict[UUID, set[UUID]]] = {}
    for professional_id, service_id, unit_id in rows:
        grouped.setdefault(professional_id, {}).setdefault(service_id, set()).add(unit_id)
    for professional_id, by_service in grouped.items():
        result[professional_id] = {s: frozenset(units) for s, units in by_service.items()}
    return result


async def _offered_service_ids(
    session: AsyncSession, tenant_id: UUID, professional: Professional
) -> set[UUID]:
    """Canonical services the professional offers right now (active entry, active service).

    Resolved through the clinic's catalog (`entry_service_id` on the resolved entries), so an
    entry matched by name counts and a `service_id` pointing at another clinic's service does not.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return set()
    catalog = await load_service_catalog(session, tenant_id)
    catalog_ids = {service.id for service in catalog if service.is_active}
    entries = cfg.professional_appointment_types(professional, tenant, catalog)
    return {sid for sid in (entry_service_id(entry) for entry in entries) if sid in catalog_ids}


async def apply_marks(
    session: AsyncSession,
    tenant_id: UUID,
    professional: Professional,
    linked_unit_ids: Collection[UUID],
    marks: Mapping[UUID, Collection[UUID]] | None,
) -> None:
    """Make the professional's marks `marks`, validated against their (new) unit links.

    `marks` is the COMPLETE desired state: a service absent from it is unrestricted ("every
    unit"), a service present must list at least one unit. `None` means "keep what is stored":
    it starts from the stored marks, which is what a save that only changes the unit links needs.

    Either way, marks on units that are not in `linked_unit_ids` are removed - for `None`
    silently (the unit was just unlinked), for an explicit `marks` as `unit_not_linked` (the
    caller asked for it). A restricted service that ends up with no linked unit is refused with
    `service_without_unit` instead of silently becoming "every unit". The professional's row is
    locked first (`FOR UPDATE`; a no-op on SQLite) so two concurrent saves run one after the
    other. The caller commits; on `ServiceMarkError` nothing was written.
    """
    if professional.tenant_id != tenant_id:
        raise ValueError("professional does not belong to this tenant")
    await session.execute(
        select(Professional.id)
        .where(Professional.id == professional.id, Professional.tenant_id == tenant_id)
        .with_for_update()
    )
    linked = frozenset(linked_unit_ids)

    if marks is None:
        wanted = {
            s: set(units) for s, units in (await read_marks(session, professional.id)).items()
        }
        wanted = {s: units & linked for s, units in wanted.items()}
        emptied = [s for s, units in wanted.items() if not units]
        if emptied:
            raise ServiceMarkError(SERVICE_WITHOUT_UNIT, emptied)
    else:
        wanted = {service_id: set(units) for service_id, units in marks.items()}
        offered = await _offered_service_ids(session, tenant_id, professional)
        not_offered = [s for s in wanted if s not in offered]
        if not_offered:
            raise ServiceMarkError(SERVICE_NOT_OFFERED, not_offered)
        not_linked = [s for s, units in wanted.items() if not units <= linked]
        if not_linked:
            raise ServiceMarkError(UNIT_NOT_LINKED, not_linked)
        emptied = [s for s, units in wanted.items() if not units]
        if emptied:
            raise ServiceMarkError(SERVICE_WITHOUT_UNIT, emptied)

    await session.execute(
        delete(ProfessionalServiceUnit).where(
            ProfessionalServiceUnit.professional_id == professional.id
        )
    )
    for service_id, units in wanted.items():
        for unit_id in sorted(units, key=str):
            session.add(
                ProfessionalServiceUnit(
                    professional_id=professional.id, service_id=service_id, unit_id=unit_id
                )
            )
    await session.flush()
