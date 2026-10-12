"""Brain orchestrated cleanup, authenticated with the existing mesh key."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.internal import require_internal_api_key
from secretaria.core.database import get_session
from secretaria.schemas.tenant_patient_cleanup import CleanupRequest, CleanupResult
from secretaria.services.tenant_patient_cleanup import cleanup_patients

router = APIRouter(
    prefix="/internal/tenants/{tenant_id}/patient-cleanup",
    tags=["internal-patient-cleanup"],
    dependencies=[Depends(require_internal_api_key)],
)


@router.get("/preview", response_model=CleanupResult)
async def preview(
    tenant_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> CleanupResult:
    return await cleanup_patients(session, tenant_id)


@router.post("", response_model=CleanupResult)
async def execute(
    tenant_id: UUID,
    payload: CleanupRequest,
    session: AsyncSession = Depends(get_session),
) -> CleanupResult:
    return await cleanup_patients(session, tenant_id, payload=payload)
