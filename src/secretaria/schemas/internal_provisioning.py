"""Request/response schemas for the internal provisioning endpoints (contract v1 §4).

Mirrors schemas/internal.py's style: lean, purpose-built shapes (never
`model_validate(orm)`) so only the fields the contract lists ever cross the
service boundary.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ProvisionTenantIn(BaseModel):
    """POST /internal/tenants body."""

    tenant_id: UUID
    clinic_name: str = Field(min_length=1, max_length=255)
    contact_email: str | None = None
    timezone: str | None = None


class ProvisionTenantOut(BaseModel):
    tenant_id: UUID
    created: bool


class WhatsappConnectionIn(BaseModel):
    """POST /internal/tenants/{tenant_id}/whatsapp-connection body."""

    phone_number_id: str = Field(min_length=1, max_length=64)
    waba_id: str | None = None
    access_token: str | None = None


class WhatsappConnectionOut(BaseModel):
    connected: bool


class AsaasConnectionIn(BaseModel):
    """POST /internal/tenants/{tenant_id}/asaas-connection body."""

    api_key: str = Field(min_length=10)
    webhook_token: str = Field(min_length=10)


class AsaasConnectionOut(BaseModel):
    status: str
    asaas_connected: bool


class ProfessionalStatusOut(BaseModel):
    """One professional entry in `GET .../config-status`'s `professionals` list."""

    id: UUID
    name: str
    is_active: bool
    has_calendar: bool
    has_hours: bool
    has_services: bool
    complete: bool


class ConfigStatusOut(BaseModel):
    """GET /internal/tenants/{tenant_id}/config-status response."""

    connected: bool
    connected_at: datetime | None
    mode_resolved: bool
    mode_resolved_at: datetime | None
    history_sync_status: str
    config_complete: bool
    reasons: list[str]
    complete_count: int
    total_active: int
    professionals: list[ProfessionalStatusOut]


class ProfessionalAttachIn(BaseModel):
    """POST /internal/tenants/{tenant_id}/professionals body."""

    name: str = Field(min_length=1, max_length=255)
    specialty: str | None = Field(default=None, max_length=255)
    about: str | None = None


class ProfessionalAttachOut(BaseModel):
    professional_id: UUID
    created: bool


class ActivateOut(BaseModel):
    """POST /internal/tenants/{tenant_id}/activate response."""

    activated: bool
    reasons: list[str]


class NotificationEmailIn(BaseModel):
    """POST /internal/notifications/email body."""

    to: str = Field(min_length=1, max_length=254)
    template: str = Field(min_length=1, max_length=80)
    variables: dict = Field(default_factory=dict)


class NotificationEmailOut(BaseModel):
    queued: bool
