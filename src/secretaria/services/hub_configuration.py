"""Shared apply/validate/read helpers for the doctor hub's configuration saves.

WHY THIS MODULE EXISTS
----------------------
Saving the Configuração screen used to mean two independent HTTP calls, each
with its own commit: PUT /tenants/me/config then PUT
/tenants/me/professionals/{id}/config. When the second one failed, the first
had already been committed — the clinic's greeting, Pix policy and address were
live, the professional's hours and services were not, and the UI could only say
"não foi possível salvar". Retrying then re-sent a snapshot that no longer
matched the database.

The fix is a single endpoint that validates everything, mutates everything, and
commits ONCE (see api/hub/config.py::update_configuration). For that to be
trustworthy it must apply byte-for-byte the same rules as the two endpoints it
supersedes — otherwise the "atomic" path would quietly drift into a second,
subtly different contract. So the rules live here, once, and all three
endpoints call them.

THE CONTRACT EVERY HELPER IN THIS MODULE KEEPS
----------------------------------------------
Nothing here commits or rolls back. Each `apply_*` function mutates the ORM
objects on the session it was handed and returns; the CALLER owns the
transaction boundary. That is the whole point — a helper that committed on its
own would make the aggregate endpoint non-atomic again, and the bug would come
back wearing a different shape.

Validation likewise happens before any mutation, and raises domain errors
(never HTTPException) so this module stays free of the HTTP layer, per the
layering rule in CLAUDE.md. The routers map those errors to status codes.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Tenant
from secretaria.models.professional import Professional
from secretaria.schemas.config import TenantConfigRead
from secretaria.schemas.professional import ProfessionalListItem
from secretaria.services import tenant_config as cfg

# ---------------------------------------------------------------------------
# Domain errors — routers translate these to HTTP, this module never imports
# fastapi (services/ must stay framework-agnostic; see CLAUDE.md layering).
# ---------------------------------------------------------------------------


class ProfessionalNotFound(Exception):
    """No professional with that id belongs to this tenant.

    Deliberately the same error for "does not exist" and "belongs to another
    tenant": distinguishing them would leak the existence of another clinic's
    rows to whoever is probing.
    """


class ActivationBlocked(Exception):
    """`is_active: true` was requested while the activation prerequisites fail.

    Carries the human-readable reason produced by
    services/tenant_config.py::can_activate, which the routers surface verbatim
    as the 422 detail — exactly as the pre-existing PUT /config always has.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Field sets
# ---------------------------------------------------------------------------

# Tenant config fields a PUT can set directly. `is_active` is NOT here: it is
# gated by the activation rule and handled separately in apply_tenant_config.
TENANT_SCALAR_FIELDS: tuple[str, ...] = (
    "greeting_message",
    "returning_greeting_message",
    "persona_notes",
    "post_consult_message",
    "post_consult_knowledge",
    "language",
    "timezone",
    "google_calendar_id",
    "google_calendar_mode",
    "appointment_duration_min",
    "business_hours",
    "appointment_types",
    "initial_flows",
    "address",
    "insurances",
    "collect_insurance",
    "pix_deposit_enabled",
    "pix_deposit_percent",
    "pix_refund_window_hours",
    "pix_retention_policy",
    "pix_partial_refund_percent",
    "pix_reschedule_limit",
)

PROFESSIONAL_CONFIG_FIELDS: tuple[str, ...] = (
    "specialty",
    "about",
    "context_doctor_message",
    "google_calendar_id",
    "business_hours",
    "appointment_types",
)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


async def resolve_professional(
    session: AsyncSession, tenant: Tenant, professional_id: str
) -> Professional:
    """Fetch a professional, scoped to this tenant. Raises ProfessionalNotFound.

    The tenant comes from the hub token, never from the request body, and the
    ownership check compares tenant_id — so a caller cannot reach another
    clinic's professional by guessing an id.
    """
    try:
        prof_uuid = UUID(professional_id)
    except ValueError:
        # A malformed id is indistinguishable from an unknown one, on purpose.
        raise ProfessionalNotFound from None

    professional = await session.get(Professional, prof_uuid)
    if professional is None or professional.tenant_id != tenant.id:
        raise ProfessionalNotFound
    return professional


# ---------------------------------------------------------------------------
# Validation (read-only — safe to run before touching anything)
# ---------------------------------------------------------------------------


async def check_tenant_activation(session: AsyncSession, tenant: Tenant, data: dict) -> None:
    """Run the activation gate against the tenant AS PATCHED.

    Only fires when `is_active` is explicitly `true` in the request body. A
    plain config save that omits `is_active` is always allowed, connected
    WhatsApp number or not — that invariant is contract v1 §10 and is what lets
    a clinic prepare its configuration before going live.

    ORDERING, which is load-bearing: this must run AFTER the patch's scalar
    fields have been applied to `tenant`, never before. A clinic legitimately
    sends its business_hours, its appointment_types and `is_active: true` in
    ONE request, and `can_activate` has to see the hours and types that request
    is establishing — judging the pre-patch row would reject a request that is
    in fact valid. (Checking first was tried and broke exactly that case; see
    tests/test_hub_config.py::test_put_activate_succeeds_once_prerequisites_met.)

    Rejecting after mutation costs nothing, because the caller owns the
    transaction and rolls back — the pending scalar changes are discarded
    before anything reaches the database.
    """
    if data.get("is_active") is not True:
        return
    connected = await cfg.has_google_refresh_token(session, tenant.id)
    ok, reason = cfg.can_activate(tenant, connected)
    if not ok:
        raise ActivationBlocked(reason)


# ---------------------------------------------------------------------------
# Apply — mutate only, never commit
# ---------------------------------------------------------------------------


async def apply_tenant_config(session: AsyncSession, tenant: Tenant, data: dict) -> None:
    """Apply a validated TenantConfigUpdate dump onto `tenant`. No commit.

    `data` must come from `model_dump(exclude_unset=True)` so an absent key
    means "leave untouched" rather than "set to null" — that is what makes a
    partial PUT partial.

    Raises ActivationBlocked when the request asks to go live and the
    prerequisites do not hold. By then the scalar fields have already been
    assigned on the ORM object; the caller MUST roll back (every caller does)
    so nothing reaches the database. See check_tenant_activation for why the
    gate cannot run any earlier.
    """
    for field_name in TENANT_SCALAR_FIELDS:
        if field_name in data:
            setattr(tenant, field_name, data[field_name])

    # Gate reads the patched tenant. Deactivation is never gated.
    await check_tenant_activation(session, tenant, data)

    if data.get("is_active") is True:
        tenant.is_active = True
    elif "is_active" in data:
        tenant.is_active = False


def apply_professional_config(professional: Professional, data: dict) -> None:
    """Apply a validated ProfessionalConfigUpdate dump. No commit.

    Never gated by entitlements, limits or activation — the same "a config save
    is always allowed" principle the tenant-level save follows, so a tenant
    that lost the multi_professional addon can still tidy up the rows it has.
    """
    for field_name in PROFESSIONAL_CONFIG_FIELDS:
        if field_name in data:
            setattr(professional, field_name, data[field_name])


# ---------------------------------------------------------------------------
# Read models — the single source both the GETs and the PUT responses use, so
# "what a save returned" and "what a later GET returns" cannot disagree.
# ---------------------------------------------------------------------------


async def tenant_read_model(session: AsyncSession, tenant: Tenant) -> TenantConfigRead:
    connected = await cfg.has_google_refresh_token(session, tenant.id)
    asaas_connected = await cfg.has_asaas_api_key(session, tenant.id)
    return TenantConfigRead(
        clinic_name=tenant.clinic_name,
        greeting_message=tenant.greeting_message,
        returning_greeting_message=tenant.returning_greeting_message,
        persona_notes=tenant.persona_notes,
        post_consult_message=tenant.post_consult_message,
        post_consult_knowledge=tenant.post_consult_knowledge,
        language=tenant.language,
        timezone=tenant.timezone,
        google_calendar_id=tenant.google_calendar_id,
        google_calendar_mode=tenant.google_calendar_mode,
        appointment_duration_min=tenant.appointment_duration_min,
        business_hours=tenant.business_hours or {},
        appointment_types=tenant.appointment_types or [],
        initial_flows=tenant.initial_flows or {},
        address=tenant.address,
        insurances=tenant.insurances or [],
        collect_insurance=tenant.collect_insurance,
        is_active=tenant.is_active,
        calendar_connected=connected,
        pix_deposit_enabled=tenant.pix_deposit_enabled,
        pix_deposit_percent=tenant.pix_deposit_percent,
        pix_refund_window_hours=tenant.pix_refund_window_hours,
        pix_retention_policy=tenant.pix_retention_policy,
        pix_partial_refund_percent=tenant.pix_partial_refund_percent,
        pix_reschedule_limit=tenant.pix_reschedule_limit,
        asaas_connected=asaas_connected,
    )


async def professional_list_item(
    session: AsyncSession, professional: Professional, tenant: Tenant
) -> ProfessionalListItem:
    completeness = await cfg.professional_completeness_item(session, professional, tenant)
    return ProfessionalListItem(
        id=str(professional.id),
        name=professional.name,
        google_calendar_id=professional.google_calendar_id,
        is_active=professional.is_active,
        created_at=professional.created_at,
        specialty=professional.specialty,
        about=professional.about,
        context_doctor_message=professional.context_doctor_message,
        business_hours=professional.business_hours or {},
        appointment_types=professional.appointment_types or [],
        has_calendar=completeness.has_calendar,
        has_hours=completeness.has_hours,
        has_services=completeness.has_services,
        complete=completeness.complete,
    )
