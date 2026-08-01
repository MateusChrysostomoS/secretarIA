"""Doctor hub — professionals CRUD (multi_professional addon).

GET   /tenants/me/professionals        - list (always allowed, even when the
                                          addon is disabled — a tenant that lost
                                          the addon can still see what it had).
                                          Rows include onboarding completeness
                                          (contract v1 §10 item E).
POST  /tenants/me/professionals        - create.
PATCH /tenants/me/professionals/{id}   - update (name, google_calendar_id, is_active).
PUT   /tenants/me/professionals/{id}/config
                                        - per-professional config (business_hours,
                                          appointment_types, specialty, about,
                                          context_doctor_message, google_calendar_id).
POST  /tenants/me/professionals/{id}/calendar
                                        - shared_account mode only (contract in
                                          docs/CHECKPOINT_google_calendar_modes.md):
                                          idempotently create a secondary Google
                                          Calendar for this professional under the
                                          clinic's connected account and persist its
                                          id onto google_calendar_id. Never gated by
                                          entitlements (core platform wiring, not an
                                          addon) — 422 `clinic_calendar_not_connected`
                                          when the clinic has no Calendar connected,
                                          409 `google_reconnect_required` when the
                                          stored clinic token predates the
                                          `calendar.app.created` scope.

Entitlement + limit enforcement (brain-api is the source of truth, fetched
fresh — `redis=None` — since this path is not hot):
  - Addon disabled -> creating, or activating (is_active: false -> true), a
    professional is rejected with 403 {"detail": "multi_professional_not_entitled"}.
  - Creating/activating an ACTIVE professional beyond `limits["professionals"]`
    is rejected with 409 {"detail": "professional_limit_reached"}.
  - A failed entitlement fetch (None) fails CLOSED: 503, never silently allowed.
  - Renaming, changing the calendar id, DEACTIVATING, or saving the `/config`
    body never touch entitlements/limits at all — those are always allowed
    regardless of addon state, so a downgraded tenant can still tidy up its
    existing rows (same "config save is never gated" principle as the
    tenant-level PUT /tenants/me/config — see api/hub/config.py).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.professional import Professional
from secretaria.schemas.professional import (
    ProfessionalCalendarConnect,
    ProfessionalConfigUpdate,
    ProfessionalCreate,
    ProfessionalListItem,
    ProfessionalRead,
    ProfessionalUpdate,
)
from secretaria.services.calendar import GoogleScopeInsufficientError
from secretaria.services.entitlements_client import get_entitlements, is_entitled
from secretaria.services.tenant_config import (
    ClinicCalendarNotConnectedError,
    ensure_professional_secondary_calendar,
    professional_completeness_item,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/professionals", tags=["hub-professionals"])

ADDON_KEY = "multi_professional"
LIMIT_KEY = "professionals"


def _read_model(professional: Professional) -> ProfessionalRead:
    return ProfessionalRead(
        id=str(professional.id),
        name=professional.name,
        google_calendar_id=professional.google_calendar_id,
        is_active=professional.is_active,
        created_at=professional.created_at,
    )


async def _list_item(
    session: AsyncSession, professional: Professional, tenant: Tenant
) -> ProfessionalListItem:
    completeness = await professional_completeness_item(session, professional, tenant)
    return ProfessionalListItem(
        id=str(professional.id),
        name=professional.name,
        google_calendar_id=professional.google_calendar_id,
        is_active=professional.is_active,
        created_at=professional.created_at,
        specialty=professional.specialty,
        about=professional.about,
        context_doctor_message=professional.context_doctor_message,
        business_hours=professional.business_hours or {},
        appointment_types=professional.appointment_types or [],
        has_calendar=completeness.has_calendar,
        has_hours=completeness.has_hours,
        has_services=completeness.has_services,
        complete=completeness.complete,
    )


async def _get_professional(
    session: AsyncSession, tenant: Tenant, professional_id: str
) -> Professional:
    try:
        prof_uuid = UUID(professional_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional not found") from None
    professional = await session.scalar(
        select(Professional).where(
            Professional.id == prof_uuid, Professional.tenant_id == tenant.id
        )
    )
    if professional is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional not found")
    return professional


async def _count_active(session: AsyncSession, tenant_id: UUID) -> int:
    result = await session.scalar(
        select(func.count())
        .select_from(Professional)
        .where(Professional.tenant_id == tenant_id, Professional.is_active.is_(True))
    )
    return int(result or 0)


async def _require_entitled_within_limit(session: AsyncSession, tenant: Tenant) -> None:
    """Gate an activate/create-active mutation. Raises HTTPException on failure.

    Fetches entitlements fresh (redis=None — this path is not hot). None
    (fetch failure) fails CLOSED: 503, never treated as "allowed".
    """
    summary = await get_entitlements(tenant.id, redis=None)
    if summary is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not verify entitlements right now"
        )
    if not is_entitled(summary, ADDON_KEY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "multi_professional_not_entitled")

    limit = summary.limits.get(LIMIT_KEY)
    if limit is not None:
        active_count = await _count_active(session, tenant.id)
        if active_count >= limit:
            raise HTTPException(status.HTTP_409_CONFLICT, "professional_limit_reached")


@router.get("", response_model=list[ProfessionalListItem])
async def list_professionals(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[ProfessionalListItem]:
    rows = await session.scalars(
        select(Professional).where(Professional.tenant_id == tenant.id).order_by(Professional.name)
    )
    return [await _list_item(session, p, tenant) for p in rows]


@router.post("", response_model=ProfessionalRead, status_code=status.HTTP_201_CREATED)
async def create_professional(
    body: ProfessionalCreate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalRead:
    if body.is_active:
        await _require_entitled_within_limit(session, tenant)

    professional = Professional(
        tenant_id=tenant.id,
        name=body.name,
        google_calendar_id=body.google_calendar_id,
        is_active=body.is_active,
    )
    session.add(professional)
    await session.commit()
    await session.refresh(professional)
    logger.info(
        "hub_professional_created", tenant_id=str(tenant.id), professional_id=str(professional.id)
    )
    return _read_model(professional)


@router.patch("/{professional_id}", response_model=ProfessionalRead)
async def update_professional(
    professional_id: str,
    body: ProfessionalUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalRead:
    professional = await _get_professional(session, tenant, professional_id)
    data = body.model_dump(exclude_unset=True)

    activating = data.get("is_active") is True and not professional.is_active
    if activating:
        await _require_entitled_within_limit(session, tenant)

    if "name" in data:
        professional.name = data["name"]
    if "google_calendar_id" in data:
        professional.google_calendar_id = data["google_calendar_id"]
    if "is_active" in data:
        professional.is_active = data["is_active"]

    await session.commit()
    await session.refresh(professional)
    logger.info(
        "hub_professional_updated", tenant_id=str(tenant.id), professional_id=str(professional.id)
    )
    return _read_model(professional)


@router.put("/{professional_id}/config", response_model=ProfessionalListItem)
async def update_professional_config(
    professional_id: str,
    body: ProfessionalConfigUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalListItem:
    """Per-professional config save — NEVER gated by entitlements/limits/activation
    (contract v1 §10 item E, same "config save is always allowed" principle as
    the tenant-level PUT /tenants/me/config)."""
    professional = await _get_professional(session, tenant, professional_id)
    data = body.model_dump(exclude_unset=True)

    for field_name in ("specialty", "about", "context_doctor_message", "google_calendar_id"):
        if field_name in data:
            setattr(professional, field_name, data[field_name])
    if "business_hours" in data:
        professional.business_hours = data["business_hours"]
    if "appointment_types" in data:
        professional.appointment_types = data["appointment_types"]

    await session.commit()
    await session.refresh(professional)
    logger.info(
        "hub_professional_config_updated",
        tenant_id=str(tenant.id),
        professional_id=str(professional.id),
        fields=sorted(data.keys()),
    )
    return await _list_item(session, professional, tenant)


@router.post("/{professional_id}/calendar", response_model=ProfessionalCalendarConnect)
async def create_professional_calendar(
    professional_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalCalendarConnect:
    """shared_account mode: idempotently create this professional's secondary
    Google Calendar under the clinic's connected account (item 3 of
    docs/CHECKPOINT_google_calendar_modes.md).

    Never gated by entitlements/limits — same "config save is always
    allowed" principle as `update_professional_config` above (this is core
    platform wiring, not an addon). Ownership is enforced by `_get_professional`
    (404 for an unknown id or one belonging to another tenant), exactly like
    every other professional-scoped hub endpoint.
    """
    professional = await _get_professional(session, tenant, professional_id)
    try:
        result = await ensure_professional_secondary_calendar(session, tenant, professional)
    except ClinicCalendarNotConnectedError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "clinic_calendar_not_connected",
                "message": (
                    "A clínica ainda não conectou uma conta do Google Calendar. "
                    "Conecte a agenda da clínica antes de criar agendas por profissional."
                ),
            },
        ) from None
    except GoogleScopeInsufficientError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "google_reconnect_required",
                "message": (
                    "A conexão da clínica com o Google Calendar não tem mais a "
                    "permissão necessária para criar agendas. Reconecte a conta "
                    "Google da clínica para continuar."
                ),
            },
        ) from None

    await session.commit()
    logger.info(
        "hub_professional_secondary_calendar_ensured",
        tenant_id=str(tenant.id),
        professional_id=str(professional.id),
        created=result.created,
    )
    return ProfessionalCalendarConnect(
        professional_id=str(result.professional_id),
        google_calendar_id=result.google_calendar_id,
        created=result.created,
    )
