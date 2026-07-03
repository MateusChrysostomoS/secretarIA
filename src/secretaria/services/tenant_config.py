"""Tenant configuration: runtime read-model + encrypted-credential helpers.

`load_tenant_config` is the function the multi-tenant agent will call to "dress"
itself per clinic. It is read-only and side-effect free; wiring it into the
prompt and Calendar tools is the next backend step (see docs/doctor-hub-backend.md).

The credential helpers wrap `tenant_credentials` (encrypted at rest with
core.crypto). Secrets are decrypted only here, only in memory, and are NEVER
returned by an API response or logged.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.crypto import decrypt, encrypt
from secretaria.models import Tenant
from secretaria.models.tenant_credentials import TenantCredentials

# --------------------------------------------------------------------------
# Runtime read-model (consumed by the agent, not by the API)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeAppointmentType:
    """An active appointment type as the LLM should see it."""

    name: str
    description: str | None
    duration_min: int
    price: str | None = None
    long_description: str | None = None


@dataclass(frozen=True)
class TenantRuntimeConfig:
    """Everything needed to run the agent for one tenant, resolved + decrypted."""

    tenant_id: UUID
    clinic_name: str
    greeting_message: str | None
    persona_notes: str | None
    language: str
    timezone: str
    appointment_duration_min: int
    appointment_types: list[RuntimeAppointmentType]
    business_hours: dict
    google_calendar_id: str
    google_refresh_token: str | None


def active_appointment_types(tenant: Tenant) -> list[dict]:
    """Active appointment-type dicts, sorted by sort_order then name."""
    items = [t for t in (tenant.appointment_types or []) if t.get("is_active", True)]
    return sorted(items, key=lambda t: (t.get("sort_order", 0), t.get("name", "")))


def active_business_hours(tenant: Tenant) -> dict:
    """Weekday -> non-empty window list. Days with no windows are dropped."""
    return {day: windows for day, windows in (tenant.business_hours or {}).items() if windows}


def can_activate(tenant: Tenant, calendar_connected: bool) -> tuple[bool, str | None]:
    """Whether the tenant may be set `is_active=True`.

    Returns (ok, reason). The bot cannot operate without a Calendar, at least
    one active appointment type, and at least one availability window.
    """
    if not calendar_connected:
        return False, "Google Calendar must be connected before going live."
    if not active_appointment_types(tenant):
        return False, "At least one active appointment type is required."
    if not active_business_hours(tenant):
        return False, "At least one availability window is required."
    return True, None


# --------------------------------------------------------------------------
# Encrypted credential helpers (tenant_credentials)
# --------------------------------------------------------------------------


async def _get_credentials(session: AsyncSession, tenant_id: UUID) -> TenantCredentials | None:
    return await session.scalar(
        select(TenantCredentials).where(TenantCredentials.tenant_id == tenant_id)
    )


async def has_google_refresh_token(session: AsyncSession, tenant_id: UUID) -> bool:
    """True when a (non-null) Calendar refresh token is stored for this tenant."""
    cred = await _get_credentials(session, tenant_id)
    return bool(cred and cred.google_refresh_token_encrypted)


async def get_google_refresh_token(session: AsyncSession, tenant_id: UUID) -> str | None:
    """Return the decrypted refresh token, or None if not connected."""
    cred = await _get_credentials(session, tenant_id)
    if not cred or not cred.google_refresh_token_encrypted:
        return None
    return decrypt(cred.google_refresh_token_encrypted)


async def set_google_refresh_token(
    session: AsyncSession, tenant_id: UUID, refresh_token: str
) -> None:
    """Encrypt and upsert the Calendar refresh token. Caller commits."""
    encrypted = encrypt(refresh_token)
    cred = await _get_credentials(session, tenant_id)
    if cred is None:
        session.add(
            TenantCredentials(tenant_id=tenant_id, google_refresh_token_encrypted=encrypted)
        )
    else:
        cred.google_refresh_token_encrypted = encrypted


async def clear_google_refresh_token(session: AsyncSession, tenant_id: UUID) -> None:
    """Forget the Calendar refresh token (disconnect). Caller commits."""
    cred = await _get_credentials(session, tenant_id)
    if cred is not None:
        cred.google_refresh_token_encrypted = None


async def has_waba_token(session: AsyncSession, tenant_id: UUID) -> bool:
    """True when a (non-null) per-tenant WhatsApp token is stored for this tenant."""
    cred = await _get_credentials(session, tenant_id)
    return bool(cred and cred.waba_token_encrypted)


async def get_waba_token(session: AsyncSession, tenant_id: UUID) -> str | None:
    """Return the decrypted WhatsApp access token, or None when not provisioned.

    THE single decrypt point for the WABA token (tenant-secrets-encryption). A None
    means "fall back to the single-tenant META_ACCESS_TOKEN env scaffold" — callers
    hand the value straight to WhatsAppClient, never to a response or a log.
    """
    cred = await _get_credentials(session, tenant_id)
    if not cred or not cred.waba_token_encrypted:
        return None
    return decrypt(cred.waba_token_encrypted)


async def set_waba_token(session: AsyncSession, tenant_id: UUID, token: str) -> None:
    """Encrypt and upsert the WhatsApp access token. Caller commits."""
    encrypted = encrypt(token)
    cred = await _get_credentials(session, tenant_id)
    if cred is None:
        session.add(TenantCredentials(tenant_id=tenant_id, waba_token_encrypted=encrypted))
    else:
        cred.waba_token_encrypted = encrypted


async def clear_waba_token(session: AsyncSession, tenant_id: UUID) -> None:
    """Forget the WhatsApp access token (the env scaffold takes over). Caller commits."""
    cred = await _get_credentials(session, tenant_id)
    if cred is not None:
        cred.waba_token_encrypted = None


# --------------------------------------------------------------------------
# Runtime config loader (for the agent — next backend step)
# --------------------------------------------------------------------------


async def load_tenant_config(session: AsyncSession, tenant: Tenant) -> TenantRuntimeConfig:
    """Resolve a tenant row into the fully-decrypted runtime config.

    The agent will call this to render the dynamic system prompt and to build a
    per-tenant CalendarService with `google_refresh_token` + `google_calendar_id`.
    """
    refresh_token = await get_google_refresh_token(session, tenant.id)
    types = [
        RuntimeAppointmentType(
            name=t["name"],
            description=t.get("description"),
            duration_min=int(t.get("duration_min", tenant.appointment_duration_min)),
            price=t.get("price"),
            long_description=t.get("long_description"),
        )
        for t in active_appointment_types(tenant)
    ]
    return TenantRuntimeConfig(
        tenant_id=tenant.id,
        clinic_name=tenant.clinic_name,
        greeting_message=tenant.greeting_message,
        persona_notes=tenant.persona_notes,
        language=tenant.language,
        timezone=tenant.timezone,
        appointment_duration_min=tenant.appointment_duration_min,
        appointment_types=types,
        business_hours=active_business_hours(tenant),
        google_calendar_id=tenant.google_calendar_id,
        google_refresh_token=refresh_token,
    )
