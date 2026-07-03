"""ehr plugin: push a freshly booked appointment to the tenant's EHR (post_booking).

Entitlement-gated addon (`ehr`). Provider selection is per-tenant
(`Tenant.ehr_provider`, nullable) — the doctor hub will eventually let a
clinic pick + configure its EHR; until then this is a straight lookup keyed
off that column into the `PROVIDERS` registry below.

Only ONE provider ships today (iClinic, and even that is a STUB — see
services/ehr/iclinic.py). Doctoralia, Memed and Conexa are listed there as
future providers behind the same `EhrProvider` protocol
(services/ehr/base.py); adding one is: implement the protocol, register it
here.

The hook is a silent no-op (logged at DEBUG) when `ehr_provider` is unset or
names a provider not in `PROVIDERS` — a tenant with the addon on but no
provider configured yet, or a stale/typo'd value, never breaks anything.
"""

from secretaria.core.logging import get_logger
from secretaria.plugins.base import PluginSpec, PostBookingContext
from secretaria.plugins.registry import register
from secretaria.services.ehr.base import EhrProvider
from secretaria.services.ehr.iclinic import IClinicProvider

logger = get_logger(__name__)

# Keyed by `Tenant.ehr_provider`. Add a new provider here once it implements
# `EhrProvider` (services/ehr/base.py).
PROVIDERS: dict[str, EhrProvider] = {"iclinic": IClinicProvider()}


async def _post_booking(ctx: PostBookingContext) -> None:
    provider_key = ctx.tenant.ehr_provider
    if not provider_key:
        logger.debug("ehr_push_skipped_no_provider", tenant_id=str(ctx.tenant.id))
        return
    provider = PROVIDERS.get(provider_key)
    if provider is None:
        logger.debug(
            "ehr_push_skipped_unknown_provider",
            tenant_id=str(ctx.tenant.id),
            provider=provider_key,
        )
        return
    external_id = await provider.push_appointment(ctx.tenant, ctx.patient, ctx.appointment)
    logger.info(
        "ehr_push_completed",
        tenant_id=str(ctx.tenant.id),
        appointment_id=str(ctx.appointment.id),
        provider=provider_key,
        external_id=external_id,
    )


EHR_SPEC = PluginSpec(id="ehr", entitlement_keys=("ehr",), post_booking=_post_booking)
register(EHR_SPEC)
