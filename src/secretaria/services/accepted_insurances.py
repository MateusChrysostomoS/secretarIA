"""Which convênios the clinic accepts, by NAME, for the AI (TASK-094 D5).

Battery 2026-11-26: "Quais convênios vocês aceitam?" -> "A clínica aceita 6 convênios, mas não
tenho a lista completa aqui". The prompt carries only the COUNT on purpose (TASK-086 A16: no
plan names in the system prompt, which is not pseudonymized); the names are read on demand by
the read-only tool `ai/tools.py::list_accepted_insurances`, whose answer goes through the tool
boundary's masking like every tool result.

The list is the flow's OWN (`insurance_catalog.load_tenant_insurance`): the plans a patient is
offered in Agendar, in that order - switched-off "Outro" rows are already out, a switched-on one
is in under the name the clinic gave it. In `independent` mode ("cada médico com sua lista")
only doctors a patient may book with count (`professional_units.patient_hidden_professional_ids`):
with the conversation's doctor, HIS plans; without one, the union, flagged `per_professional`.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models.tenant import Tenant
from secretaria.services.insurance_catalog import load_tenant_insurance
from secretaria.services.service_catalog import normalize


@dataclass(frozen=True)
class AcceptedInsurances:
    names: list[str]
    # True when the list is the union of several doctors' own lists (no doctor chosen yet).
    per_professional: bool = False


def _unique(names: list[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for name in names:
        key = normalize(name)
        if key and key not in seen:
            seen.add(key)
            kept.append(name.strip())
    return kept


async def accepted_insurances(
    session: AsyncSession, tenant: Tenant, *, professional_id: UUID | None = None
) -> AcceptedInsurances:
    """The accepted convênio names for THIS tenant (see the module docstring)."""
    insurance = await load_tenant_insurance(session, tenant.id)
    plans = [plan for plan in insurance.plans if str(plan.get("name") or "").strip()]
    if tenant.insurance_mode != "independent":
        return AcceptedInsurances(_unique([str(plan["name"]) for plan in plans]))

    # Lazy: professional_units -> tenant_config -> a long import chain.
    from secretaria.services.professional_units import patient_hidden_professional_ids

    doctors = set(insurance.accepted_by)
    hidden = await patient_hidden_professional_ids(
        session, tenant.id, [UUID(doctor) for doctor in doctors]
    )
    visible = {doctor for doctor in doctors if UUID(doctor) not in hidden}
    if professional_id is not None and str(professional_id) in visible:
        taken = insurance.accepted_by.get(str(professional_id), frozenset())
        return AcceptedInsurances(
            _unique([str(plan["name"]) for plan in plans if str(plan.get("id")) in taken])
        )
    taken: set[str] = set()
    for doctor in visible:
        taken |= insurance.accepted_by[doctor]
    names = _unique([str(plan["name"]) for plan in plans if str(plan.get("id")) in taken])
    return AcceptedInsurances(names, per_professional=len(visible) > 1)


__all__ = ["AcceptedInsurances", "accepted_insurances"]
