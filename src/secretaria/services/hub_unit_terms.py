"""The doctor hub's WRITE and READ side of price/duration per unit (TASK-085, T3).

The exceptions live in `professional_service_unit_terms` (models/professional_service_unit_terms.py;
the readers go through services/service_terms.py). This module is what the hub endpoints use:

* `check_unit_terms`      validates the `unit_terms` / `apply_to_all` of a professional's service
                          list BEFORE anything is mutated (raises `UnitTermsInvalid`);
* `apply_unit_terms`      writes them in the SAME transaction as the service list (D5): rule A
                          (`apply_to_all`) erases the exceptions, a `unit_terms` array replaces
                          the set, an entry that left the list takes its exceptions with it;
* `strip_unit_fields`     the two fields never reach the `appointment_types` JSON;
* `entries_with_terms`    the read model: every entry gets `unit_terms` (always present);
* `prune_after_units_save` PUT /professionals/{id}/units: an exception of a unit the doctor no
                          longer attends (or of a service no longer done there) is erased.

Nothing here commits - the caller owns the transaction (same contract as hub_configuration.py) -
and nothing here imports the HTTP layer. Every query is scoped by the tenant or by a professional
the caller already proved is the tenant's.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Professional, Tenant, Unit
from secretaria.models.professional_service_unit_terms import ProfessionalServiceUnitTerms
from secretaria.services import (
    professional_service_units as psu,
    professional_units as pu,
)
from secretaria.services.service_catalog import (
    entry_service_id,
    load_service_catalog,
    match_service,
)
from secretaria.services.service_terms import WEEKDAY_KEYS, load_terms

# Wire-only fields of a service entry: they never belong in the stored JSON.
WIRE_ONLY_FIELDS: tuple[str, ...] = ("unit_terms", "apply_to_all")

UNIT_TERMS_WITHOUT_SERVICE = "unit_terms_without_service"
UNKNOWN_UNIT_IDS = "unknown_unit_ids"
UNIT_NOT_LINKED = "unit_not_linked"
SERVICE_NOT_AT_UNIT = "service_not_at_unit"
BLOCKED_WEEKDAYS_INVALID = "blocked_weekdays_invalid"

_MESSAGES = {
    BLOCKED_WEEKDAYS_INVALID: (
        "Os dias escolhidos para este serviço não são válidos. "
        "Escolha pelo menos um dia em que pacientes podem marcar este serviço e salve de novo."
    ),
    UNIT_TERMS_WITHOUT_SERVICE: (
        "Só serviços do catálogo da clínica podem ter valor ou duração por unidade. "
        "Escolha o serviço da lista e salve de novo."
    ),
    UNKNOWN_UNIT_IDS: (
        "Uma ou mais unidades não existem mais na clínica. Recarregue a página e escolha de novo."
    ),
    UNIT_NOT_LINKED: (
        "O profissional não atende em uma das unidades escolhidas. "
        "Marque a unidade no cadastro do profissional antes de definir valores para ela."
    ),
    SERVICE_NOT_AT_UNIT: (
        "Este serviço não é feito em uma das unidades escolhidas. "
        "Ajuste as unidades do serviço antes de definir valores para ela."
    ),
}


class UnitTermsInvalid(Exception):
    """A `unit_terms` / `apply_to_all` patch the clinic cannot save; carries what to tell the UI."""

    def __init__(
        self,
        code: str,
        *,
        service_ids: Sequence[str] = (),
        unit_ids: Sequence[str] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.service_ids = list(service_ids)
        self.unit_ids = list(unit_ids)

    def detail(self) -> dict:
        """The 422 body: `code`, a plain-Portuguese `message`, and the ids that caused it."""
        body: dict = {"code": self.code, "message": _MESSAGES[self.code]}
        if self.service_ids:
            body["service_ids"] = self.service_ids
        if self.unit_ids:
            body["unit_ids"] = self.unit_ids
        return body


def check_blocked_weekdays(raw: object) -> None:
    """Refuse a `blocked_weekdays` list the hub cannot store (TASK-087); `None` is always fine.

    Only the keys of `business_hours` (`monday`..`sunday`), each at most once, and never all seven:
    a service nobody can book is "turn off Oferecer", not a day list. Raises `UnitTermsInvalid`
    with the stable code `blocked_weekdays_invalid`.
    """
    if raw is None:
        return
    keys = list(raw) if isinstance(raw, (list, tuple)) else None
    if (
        keys is None
        or len(set(keys)) != len(keys)
        or any(key not in WEEKDAY_KEYS for key in keys)
        or len(keys) >= len(WEEKDAY_KEYS)
    ):
        raise UnitTermsInvalid(BLOCKED_WEEKDAYS_INVALID)


def sorted_weekdays(raw: object) -> list[str]:
    """The canonical stored shape: known keys only, Monday first. `None` -> `[]`."""
    wanted = set(raw) if isinstance(raw, (list, tuple, set, frozenset)) else set()
    return [day for day in WEEKDAY_KEYS if day in wanted]


def strip_unit_fields(entries: Iterable) -> list:
    """The entries as they are STORED: without `unit_terms` / `apply_to_all`.

    The base `blocked_weekdays` (TASK-087), when the entry carries it, is saved as an ordered list
    (`null` -> `[]`); an entry that does not carry it keeps not having it.
    """
    out: list = []
    for entry in entries:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        stored = {key: value for key, value in entry.items() if key not in WIRE_ONLY_FIELDS}
        if "blocked_weekdays" in stored:
            stored["blocked_weekdays"] = sorted_weekdays(stored["blocked_weekdays"])
        out.append(stored)
    return out


def _entries(data: dict) -> list[dict] | None:
    """The patch's entries, or None when it does not carry a list (absent / null = untouched)."""
    entries = data.get("appointment_types")
    if not isinstance(entries, list):
        return None
    return [entry for entry in entries if isinstance(entry, dict)]


def _wants_terms(entry: dict) -> bool:
    return bool(entry.get("unit_terms")) or bool(entry.get("apply_to_all"))


def _parse_uuid(raw: object) -> UUID | None:
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


async def check_unit_terms(
    session: AsyncSession, tenant: Tenant, professional: Professional, data: dict
) -> None:
    """Refuse (before any mutation) a service list whose per-unit exceptions cannot be saved.

    Shape errors (both fields empty, repeated unit, `apply_to_all` with terms, range/length) are
    already 422s from the schema. Here: the entry must be a catalog service; every unit must be
    THIS clinic's (a malformed id, an unknown one and another clinic's are the same answer - no
    oracle); an ACTIVE unit must be one the professional attends and one where the service is
    done (`professional_service_units`). A DEACTIVATED unit of the clinic is ignored, like in
    PUT .../units. Reads only.
    """
    entries = _entries(data)
    if not entries:
        return

    for entry in entries:
        check_blocked_weekdays(entry.get("blocked_weekdays"))
        for item in entry.get("unit_terms") or []:
            check_blocked_weekdays(item.get("blocked_weekdays"))

    for entry in entries:
        if _wants_terms(entry) and entry_service_id(entry) is None:
            raise UnitTermsInvalid(UNIT_TERMS_WITHOUT_SERVICE)

    asked: list[str] = []
    for entry in entries:
        for item in entry.get("unit_terms") or []:
            unit_id = str(item["unit_id"])
            if unit_id not in asked:
                asked.append(unit_id)
    if not asked:
        return

    parsed = {raw: _parse_uuid(raw) for raw in asked}
    found = {}
    wanted_ids = [uid for uid in parsed.values() if uid is not None]
    if wanted_ids:
        rows = await session.execute(
            select(Unit.id, Unit.is_active).where(
                Unit.id.in_(wanted_ids), Unit.tenant_id == tenant.id
            )
        )
        found = {unit_id: is_active for unit_id, is_active in rows}
    unknown = [raw for raw, uid in parsed.items() if uid is None or uid not in found]
    if unknown:
        raise UnitTermsInvalid(UNKNOWN_UNIT_IDS, unit_ids=unknown)

    attended = set(await pu.available_unit_ids(session, professional))
    marks = await psu.read_marks(session, professional.id)
    not_linked_services: list[str] = []
    not_linked_units: list[str] = []
    not_at_services: list[str] = []
    not_at_units: list[str] = []
    for entry in entries:
        service_id = entry_service_id(entry)
        for item in entry.get("unit_terms") or []:
            unit_id = parsed[str(item["unit_id"])]
            if not found[unit_id]:
                continue  # a deactivated unit: ignored, nothing is written for it
            if unit_id not in attended:
                _add(not_linked_services, str(service_id))
                _add(not_linked_units, str(unit_id))
                continue
            allowed = psu.allowed_unit_ids(marks, service_id)
            if allowed is not None and unit_id not in allowed:
                _add(not_at_services, str(service_id))
                _add(not_at_units, str(unit_id))
    if not_linked_units:
        raise UnitTermsInvalid(
            UNIT_NOT_LINKED, service_ids=not_linked_services, unit_ids=not_linked_units
        )
    if not_at_units:
        raise UnitTermsInvalid(
            SERVICE_NOT_AT_UNIT, service_ids=not_at_services, unit_ids=not_at_units
        )


def _add(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


async def apply_unit_terms(
    session: AsyncSession, tenant: Tenant, professional: Professional, data: dict
) -> None:
    """Write the exceptions of a VALIDATED patch (`check_unit_terms` ran). No commit.

    * a service that is no longer in the new list loses its exceptions (switching an entry OFF
      keeps them; so does an entry the catalog matches by name without a `service_id`);
    * `apply_to_all` erases every unit's own price/duration of the service (deactivated units'
      included: "Todas" is all) but keeps the units' own weekdays (TASK-087: it is per subject);
    * a `unit_terms` array then replaces the set on the clinic's ACTIVE units (a deactivated unit's
      exception is preserved, like its link hours); absent / null leaves the service alone.
      `apply_to_all` and `unit_terms` may travel together: the erase runs first.
    """
    entries = _entries(data)
    if entries is None:
        return
    tenant_id, professional_id = tenant.id, professional.id
    scope = (
        ProfessionalServiceUnitTerms.tenant_id == tenant_id,
        ProfessionalServiceUnitTerms.professional_id == professional_id,
    )

    catalog = await load_service_catalog(session, tenant_id)
    kept: set[UUID] = set()
    plan: dict[UUID, dict] = {}  # last entry of a service wins
    for entry in entries:
        service_id = entry_service_id(entry)
        matched = match_service(catalog, entry)
        for sid in (service_id, matched.id if matched is not None else None):
            if sid is not None:
                kept.add(sid)
        if service_id is not None:
            plan[service_id] = entry

    stored = set(
        await session.scalars(select(ProfessionalServiceUnitTerms.service_id).where(*scope))
    )
    gone = stored - kept
    if gone:
        await session.execute(
            delete(ProfessionalServiceUnitTerms).where(
                *scope, ProfessionalServiceUnitTerms.service_id.in_(gone)
            )
        )

    active_ids = {unit.id for unit in await pu.active_units(session, tenant_id)}
    for service_id, entry in plan.items():
        if entry.get("apply_to_all"):
            # "Todas as unidades" is PER SUBJECT (TASK-087): price/duration go back to the base on
            # every unit (deactivated ones included); a unit's own weekdays are another subject
            # and stay. A row left with nothing of its own is erased. Decided in Python: a JSON
            # `null` and an SQL NULL both read back as None here.
            rows = await session.scalars(
                select(ProfessionalServiceUnitTerms).where(
                    *scope, ProfessionalServiceUnitTerms.service_id == service_id
                )
            )
            for row in rows.all():
                if row.blocked_weekdays is None:
                    await session.delete(row)
                else:
                    row.price = None
                    row.duration_min = None
            await session.flush()
        terms = entry.get("unit_terms")
        if terms is None:
            continue
        if active_ids:
            await session.execute(
                delete(ProfessionalServiceUnitTerms).where(
                    *scope,
                    ProfessionalServiceUnitTerms.service_id == service_id,
                    ProfessionalServiceUnitTerms.unit_id.in_(active_ids),
                )
            )
        for item in terms:
            unit_id = UUID(str(item["unit_id"]))
            if unit_id not in active_ids:
                continue
            fields: dict = {}
            if item.get("blocked_weekdays") is not None:
                # Only when set: an explicit None on a JSON column is stored as JSON `null`,
                # which is not the SQL NULL the CHECK and "inherit the base" rely on.
                fields["blocked_weekdays"] = sorted_weekdays(item["blocked_weekdays"])
            session.add(
                ProfessionalServiceUnitTerms(
                    tenant_id=tenant_id,
                    professional_id=professional_id,
                    service_id=service_id,
                    unit_id=unit_id,
                    price=item.get("price"),
                    duration_min=item.get("duration_min"),
                    **fields,
                )
            )
    await session.flush()


def _wire_days(days: frozenset[str] | None) -> list[str] | None:
    """A unit's own weekdays on the wire: `None` = inherits the base, a list otherwise."""
    return None if days is None else sorted_weekdays(days)


async def entries_with_terms(
    session: AsyncSession, professional: Professional, entries: Sequence
) -> list:
    """`entries` for the wire: each dict gets `unit_terms` (always present, `[]` when none).

    Only exceptions of units the professional attends right now (active link AND active unit),
    in the order of `available_unit_ids`; the rest stay stored but hidden. An entry without a
    catalog `service_id` has none.
    """
    terms = await load_terms(session, professional.tenant_id, professional.id)
    order = await pu.available_unit_ids(session, professional) if terms else []
    out: list = []
    for entry in entries:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        service_id = entry_service_id(entry)
        by_unit = terms.get(service_id, {}) if service_id is not None else {}
        out.append(
            {
                **entry,
                "unit_terms": [
                    {
                        "unit_id": str(unit_id),
                        "price": by_unit[unit_id].price,
                        "duration_min": by_unit[unit_id].duration_min,
                        "blocked_weekdays": _wire_days(by_unit[unit_id].blocked_weekdays),
                    }
                    for unit_id in order
                    if unit_id in by_unit
                ],
            }
        )
    return out


async def prune_after_units_save(
    session: AsyncSession, tenant_id: UUID, professional: Professional
) -> None:
    """Erase the exceptions a units save just made meaningless. No commit; run after the links and
    the marks were flushed.

    An ACTIVE unit of the clinic the professional no longer attends: all its exceptions go. A
    restricted service (`professional_service_units`): the exceptions on the units outside its
    marks go. Deactivated units are left alone (their data is preserved, as everywhere else).
    """
    if professional.tenant_id != tenant_id:
        raise ValueError("professional does not belong to this tenant")
    scope = (
        ProfessionalServiceUnitTerms.tenant_id == tenant_id,
        ProfessionalServiceUnitTerms.professional_id == professional.id,
    )
    active_ids = {unit.id for unit in await pu.active_units(session, tenant_id)}
    attended = set(await pu.available_unit_ids(session, professional))
    dropped = active_ids - attended
    if dropped:
        await session.execute(
            delete(ProfessionalServiceUnitTerms).where(
                *scope, ProfessionalServiceUnitTerms.unit_id.in_(dropped)
            )
        )
    for service_id, allowed in (await psu.read_marks(session, professional.id)).items():
        outside = active_ids - set(allowed)
        if outside:
            await session.execute(
                delete(ProfessionalServiceUnitTerms).where(
                    *scope,
                    ProfessionalServiceUnitTerms.service_id == service_id,
                    ProfessionalServiceUnitTerms.unit_id.in_(outside),
                )
            )
    await session.flush()
