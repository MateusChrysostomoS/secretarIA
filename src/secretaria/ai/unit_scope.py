"""Which unit an agent tool reads or books at (TASK-083 spec §8).

The deterministic flow asks the patient (services/flow_router.py "awaiting_unit"); the agent's
tools apply the same rule to the unit the model names:

* the professional attends at units (`professional_units`): the unit must be one of THEIR
  bookable units - named, or implied when there is only one; two or more and none named is a
  recoverable error that lists them, so the model asks the patient;
* no link at all: today's rule - any active unit of the clinic may be named (a label only), and
  none is required.

The unit then scopes the agenda (`unit_booking.scoped_calendar`): its hours for the free times,
its id / location on the event the booking writes. Database reads open their own session from
`secretaria.core.database` at call time (the tools' tests substitute it there).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from secretaria.services.booking_scope import service_entry_name
from secretaria.services.professional_service_units import allowed_unit_ids, read_marks
from secretaria.services.service_catalog import entry_service_id
from secretaria.services.unit_booking import (
    UnitOption,
    UnitRefused,
    check_unit,
    unit_options_for,
    within_hours,
)

OUTSIDE_UNIT_HOURS_ERROR = (
    "Esse horário está fora do atendimento dessa unidade. Consulte os horários livres de novo "
    "e ofereça um deles."
)


SERVICE_NOT_AT_UNIT_ERROR = "Esse serviço não é feito nessa unidade."


def _names(units: list) -> str:
    return ", ".join(str(unit.name) for unit in units) or "nenhuma unidade"


def _match(units: list, name: str) -> Any | None:
    target = name.strip().casefold()
    return next((unit for unit in units if str(unit.name).strip().casefold() == target), None)


async def professional_unit_options(tenant_id: UUID, professional: Any | None) -> list[UnitOption]:
    """The professional's offered units (hours resolved), or [] - also without a professional."""
    if professional is None:
        return []
    from secretaria.core.database import async_session_factory
    from secretaria.models import Tenant

    async with async_session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return []
        options = await unit_options_for(session, tenant, [professional])
    return options.get(professional.id, [])


async def resolve_tool_unit(
    tenant_id: UUID, professional: Any | None, unit_name: str = ""
) -> tuple[UnitOption | None, dict | None]:
    """(the unit to use or None, None), or (None, a tool-error payload the model recovers from)."""
    options = await professional_unit_options(tenant_id, professional)
    name = (unit_name or "").strip()
    if options:
        bookable = [unit for unit in options if unit.bookable]
        if name:
            unit = _match(bookable, name)
            if unit is None:
                return None, {
                    "error": (
                        f"{getattr(professional, 'name', 'Este profissional')} não atende na "
                        f"unidade '{name}'. Unidades em que atende: {_names(bookable)}."
                    )
                }
            return unit, None
        if len(bookable) == 1:
            return bookable[0], None
        if not bookable:
            return None, {
                "error": "Este profissional não tem horários de atendimento nas suas unidades."
            }
        return None, {
            "error": (
                f"Este profissional atende em mais de uma unidade: {_names(bookable)}. Pergunte "
                "ao paciente em qual ele prefere e informe a unidade."
            )
        }
    if not name:
        return None, None
    # No link: today's rule - any active unit of the clinic, as a label (the agenda's hours).
    from secretaria.plugins.multi_unit import _resolve_unit_or_error

    unit, error = await _resolve_unit_or_error(tenant_id, name)
    if error is not None:
        return None, error
    return (
        UnitOption(
            id=unit.id,
            name=str(getattr(unit, "name", "") or ""),
            address=getattr(unit, "address", None),
            business_hours={},
        ),
        None,
    )


def service_id_of(catalog: list | None, service_name: str | None) -> UUID | None:
    """The `service_id` of the catalog entry named `service_name` (its canonical spelling), or
    None - no such entry, or an entry without one: unrestricted by definition (spec §9.11)."""
    target = (service_name or "").strip().casefold()
    if not target:
        return None
    for entry in catalog or []:
        if str(service_entry_name(entry)).strip().casefold() == target:
            return entry_service_id(entry)
    return None


async def valid_unit_options(
    tenant_id: UUID, professional: Any | None, service_id: UUID | None
) -> list[UnitOption]:
    """The professional's bookable units where `service_id` is done (all of them when it is not
    restricted, or no service is given). [] without a professional or links."""
    options = await professional_unit_options(tenant_id, professional)
    bookable = [unit for unit in options if unit.bookable]
    if service_id is None or professional is None or not bookable:
        return bookable
    from secretaria.core.database import async_session_factory

    async with async_session_factory() as session:
        allowed = allowed_unit_ids(await read_marks(session, professional.id), service_id)
    return bookable if allowed is None else [unit for unit in bookable if unit.id in allowed]


async def service_unit_refusal(
    tenant_id: UUID,
    professional: Any | None,
    unit: UnitOption | None,
    catalog: list | None,
    service_name: str | None,
) -> dict | None:
    """A tool-error when the chosen `unit` is not one where `service_name` is done, else None.

    The AI / patient write rule (spec §6.1, §9.13) through the same `check_unit` every patient
    write path uses. The error is plain and carries `valid_units` (names) so the model re-offers
    only those. A service without `service_id` (or without marks) never refuses.
    """
    service_id = service_id_of(catalog, service_name)
    if professional is None or unit is None or service_id is None:
        return None
    from secretaria.core.database import async_session_factory

    async with async_session_factory() as session:
        try:
            await check_unit(session, tenant_id, professional, unit.id, service_id)
        except UnitRefused as refused:
            if refused.code != "unit_service_not_offered":
                return {"error": refused.message}
        else:
            return None
    # Refused: list where the service IS done.
    return await _service_refusal_payload(tenant_id, professional, service_id, service_name)


async def _service_refusal_payload(
    tenant_id: UUID, professional: Any, service_id: UUID, service_name: str | None
) -> dict:
    valid = [unit.name for unit in await valid_unit_options(tenant_id, professional, service_id)]
    head = f"{SERVICE_NOT_AT_UNIT_ERROR} O serviço '{service_name}'"
    if valid:
        error = f"{head} é feito em: {', '.join(valid)}. Ofereça só essas ao paciente."
    else:
        error = (
            f"{head} não tem nenhuma unidade com atendimento agora. Não marque; avise o "
            "paciente e ofereça outro serviço."
        )
    return {"error": error, "valid_units": valid}


def outside_unit_hours(unit: UnitOption | None, start: datetime, end: datetime, tz) -> dict | None:
    """A tool-error when [start, end) is outside the unit's own hours, else None."""
    hours = getattr(unit, "business_hours", None) if unit is not None else None
    if not hours:
        return None
    local_start = start.astimezone(tz) if start.tzinfo else start
    local_end = end.astimezone(tz) if end.tzinfo else end
    if within_hours(hours, local_start, local_end):
        return None
    return {"error": OUTSIDE_UNIT_HOURS_ERROR}


async def sole_professional_row(tenant_id: UUID, professional_id: UUID | None) -> Any | None:
    """The clinic's single active professional's row (the base tools' booking owner), or None."""
    if professional_id is None:
        return None
    from secretaria.core.database import async_session_factory
    from secretaria.models import Professional

    async with async_session_factory() as session:
        row = await session.get(Professional, professional_id)
    return row if row is not None and row.tenant_id == tenant_id else None


__all__ = [
    "OUTSIDE_UNIT_HOURS_ERROR",
    "SERVICE_NOT_AT_UNIT_ERROR",
    "outside_unit_hours",
    "professional_unit_options",
    "resolve_tool_unit",
    "service_id_of",
    "service_unit_refusal",
    "sole_professional_row",
    "valid_unit_options",
]
