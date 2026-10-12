"""POST /tenants/me/simulation/turn - one patient turn of the booking flow, simulated (TASK-027).

Stateless and side-effect free: the conversation state lives in the browser and comes back with
every call; the unsaved draft is validated by the same schemas the Save uses and laid over
in-memory copies. Reads the clinic, writes nothing, enqueues nothing, calls no model.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import FlowState, Tenant
from secretaria.schemas.config import TenantConfigUpdate
from secretaria.schemas.professional import ProfessionalConfigUpdate
from secretaria.services.tenant_config import list_active_professionals
from secretaria.workers.shared.flow_simulation import simulate_turn
from secretaria.workers.shared.flow_simulation_input import load_simulation_inputs

router = APIRouter(prefix="/tenants/me/simulation", tags=["hub-simulation"])


class SimulationDraft(BaseModel):
    """What the manager is editing and has NOT saved. Same shape as the Save envelope."""

    model_config = ConfigDict(extra="forbid")

    tenant: TenantConfigUpdate | None = None
    professional_id: UUID | None = None
    professional: ProfessionalConfigUpdate | None = None


class SimulationState(BaseModel):
    """The conversation position. Every field is validated: the client is not trusted."""

    model_config = ConfigDict(extra="forbid")

    flow_state: FlowState = FlowState.IDLE
    flow_step: str | None = Field(default=None, max_length=64)
    flow_selected_type: str | None = Field(default=None, max_length=200)
    flow_selected_day: str | None = Field(default=None, max_length=64)
    flow_selected_slot: str | None = Field(default=None, max_length=64)
    flow_selected_professional_id: UUID | None = None
    flow_selected_insurance: str | None = Field(default=None, max_length=200)
    flow_managing_appointment_id: UUID | None = None
    flow_attendee_name: str | None = Field(default=None, max_length=200)


class Tap(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    title: str = Field(max_length=400)


class SimulationTurnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: SimulationDraft | None = None
    state: SimulationState | None = None  # null = start the flow as if Agendar had been tapped
    tap: Tap | None = None
    text: str | None = Field(default=None, max_length=500)


class SimulationTurnOut(BaseModel):
    bubbles: list[dict[str, Any]]
    state: dict[str, Any]
    finished: bool
    note: str | None


@router.post("/turn", response_model=SimulationTurnOut)
async def simulation_turn(
    body: SimulationTurnIn,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> SimulationTurnOut:
    draft = body.draft
    professional_id = draft.professional_id if draft else None
    if professional_id is not None:
        mine = {p.id for p in await list_active_professionals(session, tenant.id)}
        if professional_id not in mine:
            raise HTTPException(404, "Profissional não encontrado.")
    draft_tenant = draft.tenant.model_dump(exclude_unset=True) if draft and draft.tenant else None
    draft_professional = (
        draft.professional.model_dump(exclude_unset=True) if draft and draft.professional else None
    )
    inputs = await load_simulation_inputs(
        session,
        tenant,
        draft_tenant=draft_tenant,
        professional_id=professional_id,
        draft_professional=draft_professional,
    )
    state = body.state.model_dump(mode="json") if body.state else None
    turn = await simulate_turn(
        tenant=inputs.tenant,
        professionals=inputs.professionals,
        calendar_for=inputs.calendar_for,
        state=state,
        tap=body.tap.model_dump() if body.tap else None,
        text=body.text,
    )
    return SimulationTurnOut(
        bubbles=turn.bubbles, state=turn.state, finished=turn.finished, note=turn.note
    )
