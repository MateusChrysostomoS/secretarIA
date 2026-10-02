"""db - split out of workers/tasks.py (TASK-023)."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.models import (
    Patient,
    Tenant,
)
from secretaria.services.tenant_config import (
    set_waba_token,
)

logger = get_logger(__name__)


def _mark_connected(tenant: Tenant) -> Tenant:
    """Best-effort `connected_at` backstop: any webhook resolving to a known
    tenant implies its WhatsApp number is receiving live traffic.

    The primary setter is the internal whatsapp-connection endpoint
    (contract v1 §4 endpoint 2, api/internal_provisioning.py) - this only
    fires when that path was somehow skipped (e.g. a dev-seeded tenant, or a
    number reconnected by hand), so `connected_at` is never left NULL once
    real traffic exists. A no-op once already set.
    """
    if tenant.connected_at is None:
        tenant.connected_at = datetime.now(UTC)
    return tenant

async def _resolve_tenant(session: AsyncSession, phone_number_id: str | None) -> Tenant | None:
    """Find the tenant for an inbound event.

    Primary path (always on): exact match on `phone_number_id`. This is
    null-safe by construction against the now-NULLABLE `tenants.phone_number_id`
    (onboarding creates a tenant row before its WhatsApp number is connected,
    so several tenants may have a NULL phone_number_id at once) - the lookup
    only runs when `phone_number_id` is truthy, and `Tenant.phone_number_id ==
    <a non-empty string>` never matches a NULL column value in SQL, so a
    NULL-phone tenant can never be adopted here.

    MVP single-tenant scaffold (`settings.ALLOW_WEBHOOK_AUTOPROVISION`,
    default False): when no tenant matches and the flag is on, fall back to
    the configured META_PHONE_NUMBER_ID / auto-provision a tenant from env,
    exactly as the single-tenant dev flow always has. Production/multi-tenant
    deployments must leave this OFF - an unrecognized phone_number_id is then
    simply dropped (returns None), never adopted or fabricated.

    Every resolved tenant is passed through `_mark_connected` before it is
    returned (see that function's docstring).
    """
    if phone_number_id:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.phone_number_id == phone_number_id)
        )
        if tenant is not None:
            return _mark_connected(tenant)

    settings = get_settings()
    if not settings.ALLOW_WEBHOOK_AUTOPROVISION:
        return None

    configured = settings.META_PHONE_NUMBER_ID
    if phone_number_id and configured and phone_number_id != configured:
        # Unknown number - never auto-provision a foreign tenant.
        return None

    target = phone_number_id or configured
    if not target:
        return None

    tenant = await session.scalar(select(Tenant).where(Tenant.phone_number_id == target))
    if tenant is not None:
        return _mark_connected(tenant)

    tenant = Tenant(
        clinic_name="MVP Clinic",
        phone_number_id=target,
        # The single-tenant MVP scaffold is live by definition; without this the
        # new is_active gate would silently block the validated dev flow.
        is_active=True,
    )
    session.add(tenant)
    await session.flush()
    if settings.META_ACCESS_TOKEN:
        # Encrypted at rest from the very first write — never a plaintext column.
        await set_waba_token(session, tenant.id, settings.META_ACCESS_TOKEN)
    logger.info("worker_tenant_auto_provisioned", tenant_id=str(tenant.id))
    return _mark_connected(tenant)

async def _get_or_create_patient(
    session: AsyncSession,
    tenant: Tenant,
    wa_id: str,
    name: str | None,
) -> Patient:
    """Return the patient for (tenant, wa_id), creating it if needed."""
    patient = await session.scalar(
        select(Patient).where(
            Patient.tenant_id == tenant.id,
            Patient.wa_id == wa_id,
        )
    )
    if patient is None:
        patient = Patient(tenant_id=tenant.id, wa_id=wa_id, name=name)
        session.add(patient)
        await session.flush()
    elif name and not patient.name:
        patient.name = name
    return patient
