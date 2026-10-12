"""GET /tenants/me/context-completeness - what is still missing in the clinic's context (TASK-025).

Read-only and deterministic (services/context_completeness.py); no LLM, no entitlement gate
(it is core wiring, like the service catalog).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Tenant
from secretaria.services.context_completeness import compute_completeness
from secretaria.services.service_catalog import load_service_catalog

router = APIRouter(prefix="/tenants/me", tags=["hub-context"])


Section = Literal[
    "address", "facts", "services", "hours", "insurance", "messages", "post_consult"
]


class CompletenessItemRead(BaseModel):
    key: str
    label: str
    status: Literal["done", "missing", "optional"]
    hint: str | None
    section: Section


class CompletenessRead(BaseModel):
    score: int
    items: list[CompletenessItemRead]


@router.get("/context-completeness", response_model=CompletenessRead)
async def get_context_completeness(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> CompletenessRead:
    services = await load_service_catalog(session, tenant.id)
    result = compute_completeness(tenant, services)
    return CompletenessRead(
        score=result.score,
        items=[CompletenessItemRead(**vars(item)) for item in result.items],
    )
