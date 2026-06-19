"""Doctor hub — tenant configuration endpoints (authenticated).

GET  /tenants/me/config  - current config (never includes secrets).
PUT  /tenants/me/config  - update non-sensitive config; enforces the activation
                           rule (no going live without a connected Calendar).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.schemas.config import TenantConfigRead, TenantConfigUpdate
from secretaria.services import tenant_config as cfg

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me", tags=["hub-config"])

# Scalar config fields that PUT can set directly (is_active is handled separately
# because it is gated by the activation rule).
_SCALAR_FIELDS = (
    "greeting_message",
    "returning_greeting_message",
    "greeting_buttons",
    "persona_notes",
    "language",
    "timezone",
    "google_calendar_id",
    "appointment_duration_min",
    "business_hours",
    "appointment_types",
    "initial_flows",
)


async def _read_model(session: AsyncSession, tenant: Tenant) -> TenantConfigRead:
    connected = await cfg.has_google_refresh_token(session, tenant.id)
    return TenantConfigRead(
        clinic_name=tenant.clinic_name,
        greeting_message=tenant.greeting_message,
        returning_greeting_message=tenant.returning_greeting_message,
        greeting_buttons=tenant.greeting_buttons or [],
        persona_notes=tenant.persona_notes,
        language=tenant.language,
        timezone=tenant.timezone,
        google_calendar_id=tenant.google_calendar_id,
        appointment_duration_min=tenant.appointment_duration_min,
        business_hours=tenant.business_hours or {},
        appointment_types=tenant.appointment_types or [],
        initial_flows=tenant.initial_flows or {},
        is_active=tenant.is_active,
        calendar_connected=connected,
    )


@router.get("/config", response_model=TenantConfigRead)
async def get_config(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TenantConfigRead:
    return await _read_model(session, tenant)


@router.put("/config", response_model=TenantConfigRead)
async def update_config(
    body: TenantConfigUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TenantConfigRead:
    # `exclude_unset` so absent fields are left untouched (partial update). The
    # dumped values are already plain dicts/lists, ready to store as JSON.
    data = body.model_dump(exclude_unset=True)

    for field_name in _SCALAR_FIELDS:
        if field_name in data:
            setattr(tenant, field_name, data[field_name])

    # Activation is gated: only let the tenant go live once the prerequisites
    # (Calendar connected + active type + active window) hold.
    if data.get("is_active") is True:
        connected = await cfg.has_google_refresh_token(session, tenant.id)
        ok, reason = cfg.can_activate(tenant, connected)
        if not ok:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, reason)
        tenant.is_active = True
    elif "is_active" in data:
        tenant.is_active = False

    await session.commit()
    await session.refresh(tenant)
    logger.info("hub_config_updated", tenant_id=str(tenant.id), fields=sorted(data.keys()))
    return await _read_model(session, tenant)
