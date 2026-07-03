"""Request/response schemas for the doctor-hub conversations endpoints."""

from datetime import datetime

from pydantic import BaseModel

from secretaria.models.conversation import HandoverState


class ConversationRead(BaseModel):
    """Response — whitelisted fields only (no message bodies ride along)."""

    id: str
    patient_wa_id: str
    patient_name: str | None
    handover_state: str
    last_message_at: datetime | None


class HandoverUpdate(BaseModel):
    """POST /tenants/me/conversations/{id}/handover."""

    state: HandoverState
