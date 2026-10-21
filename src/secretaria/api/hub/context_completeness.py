"""GET /tenants/me/context-completeness - what is still missing in the clinic's context (TASK-025).

Read-only and deterministic (services/context_completeness.py); no LLM, no entitlement gate
(it is core wiring, like the service catalog).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Professional, Tenant
from secretaria.services.context_completeness import compute_completeness
from secretaria.services.service_catalog import load_service_catalog

router = APIRouter(prefix="/tenants/me", tags=["hub-context"])


# "professionals" is newer than the console's first build of this card: a console that does not
# know it still renders the item and its hint, and only loses the jump-to-section on click.
Section = Literal[
    "address",
    "facts",
    "services",
    "hours",
    "insurance",
    "messages",
    "post_consult",
    "professionals",
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
    # Roster order (the clinic's own), so the hints name people the way the console lists them.
    professionals = list(
        await session.scalars(
            select(Professional)
            .where(Professional.tenant_id == tenant.id, Professional.is_active.is_(True))
            .order_by(Professional.created_at)
        )
    )
    result = compute_completeness(tenant, services, professionals)
    return CompletenessRead(
        score=result.score,
        items=[CompletenessItemRead(**vars(item)) for item in result.items],
    )
