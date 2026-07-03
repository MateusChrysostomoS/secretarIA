"""iClinic EHR provider — STUB.

TODO: real iClinic API integration (OAuth/API-key credential exchange, the
actual appointment create/update endpoint, error mapping onto our own
retry/alerting story). Today this only logs the push and returns a
deterministic fake external id, so the `ehr` addon's wiring, entitlement
gate, and per-tenant provider-selection logic (plugins/ehr.py) can be built
and tested end-to-end before the real integration lands.

Future providers to add alongside iClinic, all behind the same `EhrProvider`
protocol (services/ehr/base.py) and the `PROVIDERS` registry
(plugins/ehr.py): Doctoralia, Memed, Conexa.
"""

from typing import TYPE_CHECKING

from secretaria.core.logging import get_logger

if TYPE_CHECKING:
    from secretaria.models import Appointment, Patient, Tenant

logger = get_logger(__name__)


class IClinicProvider:
    """STUB `EhrProvider` for iClinic — logs and returns a fake record id."""

    async def push_appointment(
        self, tenant: "Tenant", patient: "Patient | None", appointment: "Appointment"
    ) -> str | None:
        logger.info(
            "ehr_push_stub",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
        )
        return f"iclinic-stub-{appointment.id}"
