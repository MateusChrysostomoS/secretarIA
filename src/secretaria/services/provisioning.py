"""Onboarding provisioning — business logic for the internal endpoints (contract v1 §4).

Called by `api/internal_provisioning.py` (thin HTTP layer, per this repo's
layering rule — see CLAUDE.md). Every function here takes an already-open
`AsyncSession` and does NOT commit; the API layer commits once, after
translating any sentinel failure (tenant not found, phone conflict) into the
right HTTP status. "Not found" is signalled by returning `None` rather than
raising — services never raise `HTTPException` (that is an `api/` concern).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Professional, Tenant
from secretaria.services.tenant_config import (
    ProfessionalCompletenessItem,
    can_activate_professional_aware,
    professional_completeness,
    set_asaas_api_key,
    set_asaas_webhook_token,
    set_waba_token,
)

logger = get_logger(__name__)

__all__ = [
    "ConfigStatus",
    "ConnectOutcome",
    "activate_tenant",
    "connect_asaas",
    "connect_whatsapp",
    "create_or_attach_professional",
    "get_config_status",
    "provision_tenant",
]


async def provision_tenant(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    clinic_name: str,
    contact_email: str | None,
    timezone: str | None,
) -> tuple[Tenant, bool]:
    """Create the tenant row with the CALLER-SUPPLIED id (contract endpoint 1).

    Idempotent: an id that already exists is returned UNMODIFIED
    (`created=False`) — this never overwrites an existing tenant's config,
    even if the caller resends a different clinic_name/timezone on retry.
    """
    existing = await session.get(Tenant, tenant_id)
    if existing is not None:
        return existing, False

    tenant = Tenant(
        id=tenant_id,
        clinic_name=clinic_name,
        phone_number_id=None,
        is_active=False,
        contact_email=contact_email,
    )
    if timezone:
        tenant.timezone = timezone
    session.add(tenant)
    await session.flush()
    logger.info("provisioning_tenant_created", tenant_id=str(tenant_id))
    return tenant, True


class ConnectOutcome(Enum):
    """Result of `connect_whatsapp` — the API layer maps this to a status code."""

    CONNECTED = "connected"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"


async def connect_whatsapp(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    phone_number_id: str,
    waba_id: str | None,
    access_token: str | None,
) -> ConnectOutcome:
    """Wire a tenant's WhatsApp connection (contract endpoint 2). Idempotent.

    `waba_id`/`access_token` are only WRITTEN when truthy — a retry that
    omits them must never wipe a previously-stored value. `connected_at` is
    set only when currently NULL, so a resend preserves the original
    connection timestamp.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return ConnectOutcome.NOT_FOUND

    conflict = await session.scalar(
        select(Tenant.id).where(
            Tenant.phone_number_id == phone_number_id, Tenant.id != tenant_id
        )
    )
    if conflict is not None:
        return ConnectOutcome.CONFLICT

    tenant.phone_number_id = phone_number_id
    if waba_id:
        tenant.waba_id = waba_id
    if tenant.connected_at is None:
        tenant.connected_at = datetime.now(UTC)
    if access_token:
        await set_waba_token(session, tenant_id, access_token)
    logger.info("provisioning_whatsapp_connected", tenant_id=str(tenant_id))
    return ConnectOutcome.CONNECTED


async def connect_asaas(
    session: AsyncSession, *, tenant_id: UUID, api_key: str, webhook_token: str
) -> bool:
    """Store a tenant's Asaas credentials, encrypted (Pix deposit wave,
    adjacent to connect_whatsapp above). Returns False when the tenant does
    not exist (the API layer 404s), True once both secrets are stored.
    Values are never logged. Caller commits.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return False
    await set_asaas_api_key(session, tenant_id, api_key)
    await set_asaas_webhook_token(session, tenant_id, webhook_token)
    logger.info("provisioning_asaas_connected", tenant_id=str(tenant_id))
    return True


@dataclass(frozen=True)
class ConfigStatus:
    """Contract endpoint 3's response, pre-projection (api/internal_provisioning.py
    shapes this into `ConfigStatusOut`)."""

    connected: bool
    connected_at: datetime | None
    mode_resolved: bool
    mode_resolved_at: datetime | None
    history_sync_status: str
    config_complete: bool
    reasons: list[str]
    complete_count: int
    total_active: int
    professionals: list[ProfessionalCompletenessItem]


async def get_config_status(session: AsyncSession, tenant: Tenant) -> ConfigStatus:
    """Contract endpoint 3: connection/mode/history-sync facts + completeness."""
    completeness = await professional_completeness(session, tenant)
    return ConfigStatus(
        connected=tenant.phone_number_id is not None,
        connected_at=tenant.connected_at,
        mode_resolved=tenant.mode_resolved_at is not None,
        mode_resolved_at=tenant.mode_resolved_at,
        history_sync_status=tenant.history_sync_status,
        config_complete=completeness.config_complete,
        reasons=completeness.reasons,
        complete_count=completeness.complete_count,
        total_active=completeness.total_active,
        professionals=completeness.professionals,
    )


async def create_or_attach_professional(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    name: str,
    specialty: str | None,
    about: str | None,
) -> tuple[Professional, bool] | None:
    """Case-insensitive attach-by-name, else create (contract endpoint 4).

    `None` means the tenant does not exist (the API layer 404s). Attaching to
    an EXISTING professional is a pure lookup — its specialty/about are never
    overwritten by this call, only set on a freshly created row.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return None

    existing = await session.scalar(
        select(Professional).where(
            Professional.tenant_id == tenant_id,
            Professional.is_active.is_(True),
            func.lower(Professional.name) == name.strip().lower(),
        )
    )
    if existing is not None:
        return existing, False

    professional = Professional(tenant_id=tenant_id, name=name, specialty=specialty, about=about)
    session.add(professional)
    await session.flush()
    logger.info(
        "provisioning_professional_created",
        tenant_id=str(tenant_id),
        professional_id=str(professional.id),
    )
    return professional, True


async def activate_tenant(session: AsyncSession, tenant: Tenant) -> tuple[bool, list[str]]:
    """Professional-aware + connected activation gate (contract endpoint 5). Idempotent.

    Already-active tenants short-circuit to `(True, [])` without
    re-validating — this endpoint only ever turns activation ON, never off,
    so a later config regression must never flip an already-live tenant back
    to inactive through this path.
    """
    if tenant.is_active:
        return True, []

    ok, reasons = await can_activate_professional_aware(session, tenant)
    if tenant.phone_number_id is None:
        ok = False
        reasons = [*reasons, "WhatsApp number must be connected before going live."]

    if ok:
        tenant.is_active = True
        logger.info("provisioning_tenant_activated", tenant_id=str(tenant.id))
    return ok, reasons
