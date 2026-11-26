"""Doctor hub - the shared per-person agenda (TASK-083, spec §6 and §9).

POST /tenants/me/agendas/sync
    Re-link this clinic's professionals to their agendas through brain-api (manager only). Body is
    EMPTY: the identity (`agenda_id`/`person_key`) is decided server side from brain-api's
    `professional-accounts` answer and is never accepted from the client - a body carrying any
    field is a 422. Answers `{"linked", "shared"}` (counts); a brain-api failure is not an error,
    it just changes nothing.

GET  /tenants/me/professionals/{professional_id}/agenda
    `{"shared", "other_tenants_count"}`: whether this professional's agenda is shared with other
    clinics and with how many. Counts only: never a clinic name, id, patient or window.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import (
    get_current_tenant,
    professional_reader,
    require_clinic_manager,
)
from secretaria.core.database import get_session
from secretaria.models import Tenant
from secretaria.models.professional import Professional
from secretaria.services import hub_configuration as hubcfg
from secretaria.services.agenda_linking import sync_tenant_agendas

router = APIRouter(prefix="/tenants/me", tags=["hub-agendas"])


class AgendaSyncBody(BaseModel):
    """No fields on purpose: any key (`agenda_id`, `person_key`, ...) is rejected."""

    model_config = ConfigDict(extra="forbid")


class AgendaSyncOut(BaseModel):
    linked: int
    shared: int


class ProfessionalAgendaOut(BaseModel):
    shared: bool
    other_tenants_count: int


@router.post("/agendas/sync", response_model=AgendaSyncOut)
async def sync_agendas(
    body: AgendaSyncBody | None = None,
    tenant: Tenant = Depends(require_clinic_manager),
    session: AsyncSession = Depends(get_session),
) -> AgendaSyncOut:
    result = await sync_tenant_agendas(session, tenant.id)
    await session.commit()
    return AgendaSyncOut(linked=result.linked, shared=result.shared)


@router.get(
    "/professionals/{professional_id}/agenda",
    response_model=ProfessionalAgendaOut,
    dependencies=[Depends(professional_reader)],
)
async def professional_agenda(
    professional_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalAgendaOut:
    try:
        professional = await hubcfg.resolve_professional(session, tenant, professional_id)
    except hubcfg.ProfessionalNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional not found") from None
    other_tenants = 0
    if professional.agenda_id is not None:
        other_tenants = int(
            await session.scalar(
                select(func.count(func.distinct(Professional.tenant_id))).where(
                    Professional.agenda_id == professional.agenda_id,
                    Professional.tenant_id != tenant.id,
                )
            )
            or 0
        )
    return ProfessionalAgendaOut(shared=other_tenants > 0, other_tenants_count=other_tenants)
