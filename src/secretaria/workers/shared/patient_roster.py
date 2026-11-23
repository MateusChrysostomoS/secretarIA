"""The router-shaped roster of a patient turn (TASK-090 Adendo 1, owner rulings on I1 and C1).

One builder for every patient path that hands `route()` / the draft resolver a roster:

* the LIST holds the doctors a patient may be OFFERED for a NEW booking - every active doctor
  minus the ones `services/professional_units.patient_hidden_professional_ids` hides (unit links,
  none usable); their units are the usable ones only;
* `everyone` (`services/booking_scope.PatientRoster`) holds every active doctor, hidden ones
  included, with ALL their active units: it decides the topology (a clinic with 2+ active
  doctors stays multi-doctor even with one visible) and resolves the doctor of an EXISTING
  consultation (manage, edit, reschedule keep his services, hours, blocked days and unit lines).
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.services.booking_scope import PatientRoster
from secretaria.services.professional_units import patient_hidden_professional_ids
from secretaria.services.unit_booking import attach_unit_options
from secretaria.workers.shared.greeting import _flow_professionals


async def build_patient_roster(
    session: AsyncSession, tenant: Any, professional_rows: list, service_catalog: list | None
) -> PatientRoster:
    """`professional_rows`: every ACTIVE doctor of `tenant` (ORM rows, as
    `list_active_professionals` returns them). One hidden-ids query plus the unit reads."""
    hidden = await patient_hidden_professional_ids(
        session, tenant.id, [row.id for row in professional_rows]
    )
    everyone = _flow_professionals(professional_rows, service_catalog)
    offered = [snapshot for snapshot in everyone if snapshot.id not in hidden]
    await attach_unit_options(
        session, tenant, [row for row in professional_rows if row.id not in hidden], offered
    )
    if hidden:
        await attach_unit_options(
            session,
            tenant,
            [row for row in professional_rows if row.id in hidden],
            [snapshot for snapshot in everyone if snapshot.id in hidden],
            usable_only=False,
        )
    return PatientRoster(offered, everyone=everyone)


def hidden_ids(roster: Any) -> set:
    """The ids `everyone` has and the offered list does not (the hidden doctors)."""
    offered = {getattr(p, "id", None) for p in roster or []}
    return {p.id for p in getattr(roster, "everyone", []) if p.id not in offered}


__all__ = ["build_patient_roster", "hidden_ids"]
