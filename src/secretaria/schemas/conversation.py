"""Request/response schemas for the doctor-hub conversations endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field

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


class MessageRead(BaseModel):
    """One message in a conversation thread — GET .../conversations/{id}/messages."""

    id: str
    direction: str
    sender: str
    body: str | None
    created_at: datetime


class MessageSend(BaseModel):
    """POST /tenants/me/conversations/{id}/messages — staff sends a message."""

    body: str = Field(min_length=1)
