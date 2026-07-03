"""EhrProvider protocol: how post_booking pushes a booked appointment to a
tenant's external EHR / practice-management system (the `ehr` addon).

Every concrete provider (services/ehr/iclinic.py, and whatever joins it
later) implements `push_appointment`. `plugins/ehr.py` is the only caller —
it selects the provider for a tenant (`Tenant.ehr_provider`) and invokes it
from its `post_booking` hook, entirely off the hot path (see
plugins/post_booking.py).
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from secretaria.models import Appointment, Patient, Tenant


class EhrProvider(Protocol):
    """One external EHR/practice-management system's appointment push."""

    async def push_appointment(
        self, tenant: "Tenant", patient: "Patient | None", appointment: "Appointment"
    ) -> str | None:
        """Push a booked appointment to the external EHR.

        Returns the external system's record id when the push created/
        matched one, or None when there is nothing to reference. MUST NOT
        raise for a routine failure (the caller — plugins/ehr.py — treats
        this as best-effort and fail-open regardless, but a provider that
        raises only for truly exceptional cases keeps logs meaningful).
        """
        ...
