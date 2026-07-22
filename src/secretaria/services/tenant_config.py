"""Tenant configuration: runtime read-model + encrypted-credential helpers.

`load_tenant_config` is the function the multi-tenant agent calls to "dress"
itself per clinic. It is read-only and side-effect free; it is wired into the
prompt (`ai/prompts.py::secretary_system_prompt`) and Calendar tools
(`services/calendar.py`, `plugins/multi_professional.py`) — see
docs/CHECKPOINT_onboarding_multiprofessional.md for the per-professional layer.

The credential helpers wrap `tenant_credentials` / `professional_credentials`
(encrypted at rest with core.crypto). Secrets are decrypted only here, only in
memory, and are NEVER returned by an API response or logged.

`professional_completeness` / `can_activate_professional_aware` implement the
partial-activation rule (cross-service contract v1 §4/§10) for tenants with
one or more `Professional` rows (every tenant has at least one, backfilled by
migration - see migrations/versions for the revision id). `can_activate`
itself is UNCHANGED (kept for its existing caller, api/hub/config.py); the
professional-aware variant is additive.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.crypto import decrypt, encrypt
from secretaria.models import Tenant
from secretaria.models.professional import Professional
from secretaria.models.professional_credentials import ProfessionalCredentials
from secretaria.models.tenant_credentials import TenantCredentials
from secretaria.services.calendar import CalendarService

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
    # Pre-consult orientations shown to the patient, e.g. "Jejum de 8 horas".
    requirements: list[str] = field(default_factory=list)


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
    # --- Single-active-professional resolution (contract v1 §10 item D) ---
    # Populated by `load_tenant_config` ONLY when the tenant has EXACTLY ONE
    # active professional: the fields above already resolve THROUGH that
    # professional (own hours/services/calendar/credential, falling back to
    # the tenant's), and its persona context is exposed here for prompt
    # injection (ai/prompts.py). A tenant with zero or multiple active
    # professionals leaves all four None/unset — the base agent stays
    # tenant-level, and multi_professional's plugin tools resolve each
    # professional's own context individually instead.
    professional_id: UUID | None = None
    context_doctor_message: str | None = None
    specialty: str | None = None
    about: str | None = None
    # Tenant-level reference knowledge for post-consult questions (recovery
    # care, return-visit norms, exam-result delivery). Blanked by run_agent
    # on non-qualifying turns (ai/graph.py) so the prompt stays turn-
    # appropriate - see ai/prompts.py::_format_post_consult_knowledge.
    post_consult_knowledge: str | None = None
    # Per-turn rendered "consultas marcadas" block, set by the worker
    # (workers/tasks.py::_appointment_context_text) via run_agent's
    # `appointment_context` parameter for a qualifying turn (see
    # ai/graph.py::run_agent, ai/prompts.py::_format_appointment_context) —
    # NEVER loaded from DB, unlike every other field above.
    appointment_context: str | None = None


def _filter_active_types(appointment_types: list | None) -> list[dict]:
    """Shared filter behind `active_appointment_types` / `professional_appointment_types`."""
    items = [t for t in (appointment_types or []) if t.get("is_active", True)]
    return sorted(items, key=lambda t: (t.get("sort_order", 0), t.get("name", "")))


def _filter_active_hours(business_hours: dict | None) -> dict:
    """Shared filter behind `active_business_hours` / `professional_business_hours`."""
    return {day: windows for day, windows in (business_hours or {}).items() if windows}


def active_appointment_types(tenant: Tenant) -> list[dict]:
    """Active appointment-type dicts, sorted by sort_order then name."""
    return _filter_active_types(tenant.appointment_types)


def active_business_hours(tenant: Tenant) -> dict:
    """Weekday -> non-empty window list. Days with no windows are dropped."""
    return _filter_active_hours(tenant.business_hours)


def professional_appointment_types(professional: Professional, tenant: Tenant) -> list[dict]:
    """Active appointment-type dicts for ONE professional.

    The professional's own `appointment_types` JSON when set (non-empty),
    else the tenant's (the legacy single-professional column) - same
    filtering semantics as `active_appointment_types`.
    """
    return _filter_active_types(professional.appointment_types or tenant.appointment_types)


def professional_business_hours(professional: Professional, tenant: Tenant) -> dict:
    """Weekday -> non-empty window list for ONE professional.

    The professional's own `business_hours` JSON when set (non-empty), else
    the tenant's (the legacy single-professional column) - same filtering
    semantics as `active_business_hours`.
    """
    return _filter_active_hours(professional.business_hours or tenant.business_hours)


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
# Encrypted credential helpers (professional_credentials)
# --------------------------------------------------------------------------
#
# Mirror the tenant helpers above, one row per professional (professional_id
# IS the primary key - see models/professional_credentials.py), keyed by
# `professional.id`, never `tenant_id`.


async def _get_professional_credentials(
    session: AsyncSession, professional_id: UUID
) -> ProfessionalCredentials | None:
    return await session.get(ProfessionalCredentials, professional_id)


async def has_professional_google_refresh_token(
    session: AsyncSession, professional_id: UUID
) -> bool:
    """True when a (non-null) Calendar refresh token is stored for THIS professional.

    Does NOT fall back to the tenant-level token - callers that want the
    "tenant covers every professional" OR semantics compose this with
    `has_google_refresh_token` themselves (see `professional_completeness`).
    """
    cred = await _get_professional_credentials(session, professional_id)
    return bool(cred and cred.google_refresh_token_encrypted)


async def get_professional_google_refresh_token(
    session: AsyncSession, professional_id: UUID
) -> str | None:
    """Return the professional's own decrypted refresh token, or None if not connected."""
    cred = await _get_professional_credentials(session, professional_id)
    if not cred or not cred.google_refresh_token_encrypted:
        return None
    return decrypt(cred.google_refresh_token_encrypted)


async def set_professional_google_refresh_token(
    session: AsyncSession, professional_id: UUID, refresh_token: str
) -> None:
    """Encrypt and upsert the professional's own Calendar refresh token. Caller commits."""
    encrypted = encrypt(refresh_token)
    cred = await _get_professional_credentials(session, professional_id)
    if cred is None:
        session.add(
            ProfessionalCredentials(
                professional_id=professional_id, google_refresh_token_encrypted=encrypted
            )
        )
    else:
        cred.google_refresh_token_encrypted = encrypted


async def clear_professional_google_refresh_token(
    session: AsyncSession, professional_id: UUID
) -> None:
    """Forget the professional's own Calendar refresh token. Caller commits."""
    cred = await _get_professional_credentials(session, professional_id)
    if cred is not None:
        cred.google_refresh_token_encrypted = None


# --------------------------------------------------------------------------
# Shared professional resolution (both brains: flow router + LLM tools)
# --------------------------------------------------------------------------


async def list_active_professionals(session: AsyncSession, tenant_id: UUID) -> list[Professional]:
    """Active professionals for `tenant_id`, ordered by name.

    THE shared query behind every "which professionals can a patient book
    with?" surface: plugins/multi_professional.py's agent tools and the
    deterministic-flow snapshot in workers/tasks.py both resolve through
    here, so the two brains can never disagree on the roster.
    """
    rows = await session.scalars(
        select(Professional)
        .where(Professional.tenant_id == tenant_id, Professional.is_active.is_(True))
        .order_by(Professional.name)
    )
    return list(rows)


async def resolve_professional_calendar(
    session: AsyncSession,
    tenant: Tenant | None,
    professional: Professional,
    *,
    tenant_config: TenantRuntimeConfig | None = None,
    calendar_factory: Callable[..., CalendarService] | None = None,
) -> CalendarService:
    """Resolve ONE professional's own config into a ready CalendarService.

    THE single implementation of the professional -> tenant resolution chain
    (contract v1 §10 item C), shared by both brains:

      - refresh token: the professional's own encrypted credential
        (`professional_credentials`) when connected, else the tenant's
        (`tenant_config.google_refresh_token`; env-last inside CalendarService).
      - hours/services: the professional's own JSON when set, else the
        tenant's (`professional_business_hours` / `professional_appointment_types`).
      - default slot duration: the first active RESOLVED service's
        `duration_min`; when nothing resolves, None keeps the tenant default.
      - calendar id: `professional.google_calendar_id`, else the tenant's
        (CalendarService.for_professional's substitution semantics).

    `tenant_config` is loaded via `load_tenant_config` when not passed —
    callers that already hold one (workers/tasks.py's flow path, the plugin's
    ContextVar) should pass it in to avoid a second decrypt.

    `calendar_factory` is the construction seam, called with the resolved
    keyword overrides (google_calendar_id / google_refresh_token /
    business_hours / appointment_duration_min). None builds directly via
    `CalendarService.for_professional(tenant_config, ...)` — the plain
    flow-router path, no ContextVar involved. plugins/multi_professional.py
    passes `ai.tools._calendar_for_professional` instead, keeping the LLM
    tool path's ContextVar-scoped construction while sharing this resolution.
    """
    own_token = await get_professional_google_refresh_token(session, professional.id)
    hours = professional_business_hours(professional, tenant) if tenant is not None else {}
    resolved_types = (
        professional_appointment_types(professional, tenant) if tenant is not None else []
    )
    duration = int(resolved_types[0]["duration_min"]) if resolved_types else None

    if tenant_config is None and tenant is not None:
        tenant_config = await load_tenant_config(session, tenant)
    fallback_token = tenant_config.google_refresh_token if tenant_config is not None else None

    overrides = dict(
        google_calendar_id=professional.google_calendar_id,
        google_refresh_token=own_token or fallback_token,
        business_hours=hours,
        appointment_duration_min=duration,
    )
    if calendar_factory is not None:
        return calendar_factory(**overrides)
    if tenant_config is None:
        raise ValueError(
            "resolve_professional_calendar needs a tenant_config (or a tenant row to load one)"
        )
    return CalendarService.for_professional(tenant_config, **overrides)


# --------------------------------------------------------------------------
# Professional completeness / partial-activation (onboarding contract v1)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfessionalCompletenessItem:
    """One active professional's onboarding-completeness snapshot (contract §4 endpoint 3)."""

    id: UUID
    name: str
    is_active: bool
    has_calendar: bool
    has_hours: bool
    has_services: bool
    complete: bool


@dataclass(frozen=True)
class TenantCompleteness:
    """Aggregate, partial-activation-aware completeness for a tenant's active professionals."""

    professionals: list[ProfessionalCompletenessItem]
    complete_count: int
    total_active: int
    config_complete: bool
    reasons: list[str]


def _professional_missing_reasons(item: ProfessionalCompletenessItem) -> list[str]:
    """Per-professional missing-config reasons, same tone/wording as `can_activate`."""
    reasons: list[str] = []
    if not item.has_calendar:
        reasons.append(f"{item.name}: Google Calendar must be connected before going live.")
    if not item.has_services:
        reasons.append(f"{item.name}: at least one active appointment type is required.")
    if not item.has_hours:
        reasons.append(f"{item.name}: at least one availability window is required.")
    return reasons


async def _completeness_item(
    session: AsyncSession, professional: Professional, tenant: Tenant, tenant_calendar: bool
) -> ProfessionalCompletenessItem:
    """Shared computation behind `professional_completeness` (per active row)
    and `professional_completeness_item` (any single row, any is_active state).
    """
    has_hours = bool(professional_business_hours(professional, tenant))
    has_services = bool(professional_appointment_types(professional, tenant))
    has_calendar = tenant_calendar or await has_professional_google_refresh_token(
        session, professional.id
    )
    return ProfessionalCompletenessItem(
        id=professional.id,
        name=professional.name,
        is_active=professional.is_active,
        has_calendar=has_calendar,
        has_hours=has_hours,
        has_services=has_services,
        complete=has_calendar and has_hours and has_services,
    )


async def professional_completeness_item(
    session: AsyncSession, professional: Professional, tenant: Tenant
) -> ProfessionalCompletenessItem:
    """Completeness snapshot for ONE professional, regardless of is_active.

    Used by the hub professionals endpoints (api/hub/professionals.py) to
    surface has_calendar/has_hours/has_services/complete on a single row
    right after creating/updating it - independent of the tenant-wide
    partial-activation aggregate `professional_completeness` computes across
    every ACTIVE professional (an inactive row is never counted there, but
    the hub's list view still wants to show its completeness).
    """
    tenant_calendar = await has_google_refresh_token(session, tenant.id)
    return await _completeness_item(session, professional, tenant, tenant_calendar)


async def professional_completeness(session: AsyncSession, tenant: Tenant) -> TenantCompleteness:
    """Per-active-professional completeness + the partial-activation verdict.

    `has_calendar` is the professional's OWN `professional_credentials` token
    OR the tenant-level token (`has_google_refresh_token`) - a tenant-wide
    Calendar connection covers every professional that hasn't connected its
    own. `has_hours` / `has_services` reuse the same "non-empty after
    active-filtering" semantics as `active_business_hours` /
    `active_appointment_types`, resolved per-professional via
    `professional_business_hours` / `professional_appointment_types` (own
    JSON when set, else the tenant's legacy columns). `complete` requires all
    three.

    Threshold rule (`settings.PARTIAL_ACTIVATION_THRESHOLD`, default 10):
    `total_active <= threshold` -> EVERY active professional must be complete
    (and there must be at least one - `all()` over an empty list is
    vacuously True, so a zero-professional tenant is explicitly never
    complete here); `total_active > threshold` -> at least one complete
    professional is enough (a large roster should not block go-live on its
    slowest-to-configure member).
    """
    tenant_calendar = await has_google_refresh_token(session, tenant.id)
    rows = (
        await session.scalars(
            select(Professional)
            .where(Professional.tenant_id == tenant.id, Professional.is_active.is_(True))
            .order_by(Professional.name)
        )
    ).all()

    items: list[ProfessionalCompletenessItem] = [
        await _completeness_item(session, professional, tenant, tenant_calendar)
        for professional in rows
    ]

    total_active = len(items)
    complete_count = sum(1 for item in items if item.complete)
    threshold = get_settings().PARTIAL_ACTIVATION_THRESHOLD

    reasons: list[str] = []
    if total_active == 0:
        config_complete = False
        reasons.append("At least one active professional is required.")
    elif total_active <= threshold:
        incomplete = [item for item in items if not item.complete]
        config_complete = not incomplete
        for item in incomplete:
            reasons.extend(_professional_missing_reasons(item))
    else:
        config_complete = complete_count >= 1
        if not config_complete:
            reasons.append(
                "At least one professional must have a connected Google Calendar, "
                "an active appointment type, and an availability window."
            )

    return TenantCompleteness(
        professionals=items,
        complete_count=complete_count,
        total_active=total_active,
        config_complete=config_complete,
        reasons=reasons,
    )


async def can_activate_professional_aware(
    session: AsyncSession, tenant: Tenant
) -> tuple[bool, list[str]]:
    """Professional-aware activation gate (contract endpoints 3/5).

    Requires `professional_completeness(...).config_complete` (the partial-
    activation threshold rule) AND a connected Google Calendar, tenant-level
    OR on any active professional. The calendar half is already implied by
    `config_complete` in practice (every counted-complete professional's own
    `has_calendar` already ORs in the tenant-level token), but is checked
    again explicitly here so this gate never depends on that coupling holding.

    Unlike `can_activate` (kept unchanged for its existing caller,
    api/hub/config.py), this resolves everything itself from the DB - no
    `calendar_connected` argument - and does NOT check `phone_number_id`
    (endpoint 5 checks "connected" separately: that is a WhatsApp fact, not a
    config-completeness one).
    """
    completeness = await professional_completeness(session, tenant)
    tenant_calendar = await has_google_refresh_token(session, tenant.id)
    any_calendar = tenant_calendar or any(p.has_calendar for p in completeness.professionals)

    reasons = list(completeness.reasons)
    ok = completeness.config_complete and any_calendar
    if not any_calendar and completeness.config_complete:
        # Defensive only: config_complete should already guarantee this via
        # has_calendar, but never claim activation is possible without one.
        reasons.append(
            "Google Calendar must be connected (clinic-level or for at least "
            "one professional) before going live."
        )
    return ok, reasons


# --------------------------------------------------------------------------
# Runtime config loader (for the agent — next backend step)
# --------------------------------------------------------------------------


async def load_tenant_config(session: AsyncSession, tenant: Tenant) -> TenantRuntimeConfig:
    """Resolve a tenant row into the fully-decrypted runtime config.

    The agent will call this to render the dynamic system prompt and to build a
    per-tenant CalendarService with `google_refresh_token` + `google_calendar_id`.

    Single-active-professional resolution (contract v1 §10 item D): when the
    tenant has EXACTLY ONE active `Professional` row, business_hours /
    appointment_types / google_calendar_id / google_refresh_token resolve
    THROUGH that professional (its own JSON/credential, falling back to the
    tenant's own via the same S1 helpers `professional_completeness` uses),
    and its `context_doctor_message`/`specialty`/`about` are exposed on the
    returned config for prompt injection (ai/prompts.py). A tenant with zero
    or more-than-one active professional keeps the tenant-level resolution
    unchanged (multi-professional tenants are served by the
    `multi_professional` plugin tools instead, which resolve each
    professional's own config individually per tool call).
    """
    refresh_token = await get_google_refresh_token(session, tenant.id)
    business_hours = active_business_hours(tenant)
    google_calendar_id = tenant.google_calendar_id
    active_types = active_appointment_types(tenant)

    professional_id: UUID | None = None
    context_doctor_message: str | None = None
    specialty: str | None = None
    about: str | None = None

    active_professionals = (
        await session.scalars(
            select(Professional).where(
                Professional.tenant_id == tenant.id, Professional.is_active.is_(True)
            )
        )
    ).all()
    if len(active_professionals) == 1:
        professional = active_professionals[0]
        professional_id = professional.id
        business_hours = professional_business_hours(professional, tenant)
        active_types = professional_appointment_types(professional, tenant)
        google_calendar_id = professional.google_calendar_id or tenant.google_calendar_id
        own_token = await get_professional_google_refresh_token(session, professional.id)
        refresh_token = own_token or refresh_token
        context_doctor_message = professional.context_doctor_message
        specialty = professional.specialty
        about = professional.about

    types = [
        RuntimeAppointmentType(
            name=t["name"],
            description=t.get("description"),
            duration_min=int(t.get("duration_min", tenant.appointment_duration_min)),
            price=t.get("price"),
            long_description=t.get("long_description"),
            # Old stored dicts predate this field, so it may be absent entirely.
            requirements=list(t.get("requirements") or []),
        )
        for t in active_types
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
        business_hours=business_hours,
        google_calendar_id=google_calendar_id,
        google_refresh_token=refresh_token,
        professional_id=professional_id,
        context_doctor_message=context_doctor_message,
        specialty=specialty,
        about=about,
        post_consult_knowledge=tenant.post_consult_knowledge,
    )
