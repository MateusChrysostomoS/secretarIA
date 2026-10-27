"""Doctor hub — tenant configuration endpoints (authenticated).

GET  /tenants/me/config        - current config (never includes secrets).
PUT  /tenants/me/config        - update non-sensitive config; enforces the
                                 activation rule (no going live without a
                                 connected Calendar). LEGACY: kept for
                                 compatibility during the rollout of the
                                 aggregate endpoint below.
PUT  /tenants/me/configuration - TRANSACTIONAL save of the tenant config AND
                                 (optionally) one professional's config, in a
                                 single commit.

WHY THE AGGREGATE ENDPOINT EXISTS
---------------------------------
The Configuração screen edits two scopes at once, and used to persist them with
two independent requests that each committed. When the second failed, the first
was already live: the clinic's greeting and Pix policy had changed, the
professional's hours and services had not, and the UI could only report a
generic failure. Worse, retrying re-sent a snapshot that no longer matched the
database.

`update_configuration` validates both patches, applies both, and commits once.
Any failure — a bad professional id, a blocked activation, a database error —
leaves BOTH scopes exactly as they were.

Every rule is shared with the two legacy endpoints through
services/hub_configuration.py, so the atomic path cannot drift into a second,
subtly different contract. The legacy endpoints stay green and stay routable
until no deployed frontend depends on them.
"""

import time

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import ensure_clinic_manager, get_current_tenant, get_hub_claim
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.core.subscription import SubscriptionClaim
from secretaria.models import Tenant
from secretaria.schemas.config import TenantConfigRead, TenantConfigUpdate
from secretaria.schemas.hub_configuration import HubConfigurationRead, HubConfigurationUpdate
from secretaria.services import hub_configuration as hubcfg

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me", tags=["hub-config"])

# The fields only a clinic MANAGER may write (owner answer 12 / spec 12.2 R12, TASK-020): which Pix
# provider the clinic uses, when the deposit is asked for, how long a slot is held for it, whether
# a deposit is charged at all and the deposit POLICY (amount, refund window, retention,
# rescheduling). A save that touches none of them (a greeting, the hours) is open to whoever could
# save it before.
PAYMENT_WRITE_FIELDS = frozenset(
    {
        "payment_provider",
        "deposit_timing",
        "deposit_hold_ttl_minutes",
        "pix_deposit_enabled",
        "pix_deposit_percent",
        "pix_refund_window_hours",
        "pix_difference_refund_min_days",
        "pix_retention_policy",
        "pix_partial_refund_percent",
        "pix_reschedule_limit",
    }
)


async def _claim_if_authenticated(
    authorization: str | None = Header(default=None),
) -> SubscriptionClaim | None:
    """The verified claim of a caller that sent credentials, else None.

    Deliberately NOT a bare `Depends(get_hub_claim)`: a save that touches no payment field never
    needed the claim, and `get_current_tenant` has already refused an unauthenticated caller by
    the time a handler runs, so the claim is only ever read for the three payment fields.
    """
    if authorization is None:
        return None
    return await get_hub_claim(authorization)


def _require_manager_for_payment_fields(claim: SubscriptionClaim | None, patch: dict) -> None:
    """403 `forbidden_role` / `role_claim_missing` when `patch` touches a payment field and the
    caller is not a manager. It runs FIRST: before any validation, read or mutation."""
    if PAYMENT_WRITE_FIELDS & patch.keys():
        if claim is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
        ensure_clinic_manager(claim)


@router.get("/config", response_model=TenantConfigRead)
async def get_config(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TenantConfigRead:
    return await hubcfg.tenant_read_model(session, tenant)


@router.put("/config", response_model=TenantConfigRead)
async def update_config(
    body: TenantConfigUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    claim: SubscriptionClaim | None = Depends(_claim_if_authenticated),
    session: AsyncSession = Depends(get_session),
) -> TenantConfigRead:
    """LEGACY single-scope save. Superseded by PUT /tenants/me/configuration.

    Behaviour is unchanged and must stay unchanged: it now delegates to the
    same helpers the aggregate endpoint uses, which is precisely what keeps the
    two from drifting apart.
    """
    # `exclude_unset` so absent fields are left untouched (partial update). The
    # dumped values are already plain dicts/lists, ready to store as JSON.
    data = body.model_dump(exclude_unset=True)
    _require_manager_for_payment_fields(claim, data)

    # Before any mutation: a `service_id` this clinic does not own would not
    # fail loudly later, it would silently fall back to name matching — see
    # hub_configuration.check_appointment_type_service_ids.
    try:
        await hubcfg.check_appointment_type_service_ids(session, tenant, data)
    except hubcfg.UnknownServiceIds as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unknown_service_ids",
                "message": (
                    "Um ou mais serviços selecionados não existem mais no catálogo "
                    "da clínica. Recarregue a página e escolha de novo."
                ),
                "service_ids": exc.service_ids,
            },
        ) from None

    try:
        await hubcfg.check_payment_settings(session, tenant, data)
    except hubcfg.PaymentSettingsInvalid as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": exc.message},
        ) from None

    try:
        await hubcfg.apply_tenant_config(session, tenant, data)
    except hubcfg.ActivationBlocked as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.reason) from None

    await session.commit()
    await session.refresh(tenant)
    logger.info("hub_config_updated", tenant_id=str(tenant.id), fields=sorted(data.keys()))
    return await hubcfg.tenant_read_model(session, tenant)


@router.put("/configuration", response_model=HubConfigurationRead)
async def update_configuration(
    body: HubConfigurationUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    claim: SubscriptionClaim | None = Depends(_claim_if_authenticated),
    session: AsyncSession = Depends(get_session),
) -> HubConfigurationRead:
    """Save tenant config and one professional's config in ONE transaction.

    Ordering is the whole contract, so it is spelled out rather than implied:

      1. Resolve the professional (ownership check) — a pure read, so an
         unknown or foreign id is rejected before anything is touched at all.
      2. Mutate the tenant. This is also where the activation gate runs: it
         has to judge the tenant AS PATCHED, because a clinic legitimately
         sends its hours, its services and `is_active: true` in one request
         (see services/hub_configuration.py::check_tenant_activation).
      3. Mutate the professional.
      4. One commit.

    Anything that goes wrong from step 2 onwards — a blocked activation, a
    database error, a bug — rolls the whole transaction back, so the tenant
    half can never survive on its own. That is the failure the two-PUT flow
    could not handle: by the time its second request failed, its first had
    already committed.

    No external call, OAuth handoff or background job runs inside this
    transaction — it touches the database and nothing else, so it cannot be
    left half-applied by a slow third party.
    """
    started = time.perf_counter()

    tenant_data = body.tenant.model_dump(exclude_unset=True) if body.tenant else {}
    _require_manager_for_payment_fields(claim, tenant_data)
    professional_data = (
        body.professional.model_dump(exclude_unset=True) if body.professional else {}
    )

    # Ids are captured as strings up front, and every log line below uses these
    # rather than reading the ORM objects. `session.rollback()` expires every
    # instance in the session, so touching `tenant.id` afterwards would trigger
    # a lazy refresh — i.e. database IO from inside an exception handler, which
    # blows up the error path instead of reporting it.
    tenant_id = str(tenant.id)

    # --- 1. Ownership check: a read, before any mutation ------------------
    professional = None
    professional_id: str | None = None
    if body.professional_id is not None:
        try:
            professional = await hubcfg.resolve_professional(session, tenant, body.professional_id)
        except hubcfg.ProfessionalNotFound:
            logger.info(
                "hub_configuration_rejected",
                tenant_id=tenant_id,
                reason="professional_not_found",
                stage="validate",
            )
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional not found") from None
        professional_id = str(professional.id)

    # --- 1b. Catalog ownership: still a read, still before any mutation ----
    try:
        await hubcfg.check_appointment_type_service_ids(
            session, tenant, tenant_data, professional_data
        )
    except hubcfg.UnknownServiceIds as exc:
        logger.info(
            "hub_configuration_rejected",
            tenant_id=tenant_id,
            reason="unknown_service_ids",
            stage="validate",
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unknown_service_ids",
                "message": (
                    "Um ou mais serviços selecionados não existem mais no catálogo "
                    "da clínica. Recarregue a página e escolha de novo."
                ),
                "service_ids": exc.service_ids,
            },
        ) from None

    # --- 1c. Payment settings (TASK-017): a read too (it may ask brain-api whether the
    # add-on is active), so it runs before the transaction mutates anything.
    try:
        await hubcfg.check_payment_settings(session, tenant, tenant_data)
    except hubcfg.PaymentSettingsInvalid as exc:
        logger.info(
            "hub_configuration_rejected",
            tenant_id=tenant_id,
            reason=exc.code,
            stage="validate",
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": exc.message},
        ) from None

    # --- 2. Mutate both scopes, then commit exactly once -------------------
    try:
        if tenant_data:
            await hubcfg.apply_tenant_config(session, tenant, tenant_data)
        if professional is not None and professional_data:
            hubcfg.apply_professional_config(professional, professional_data)
        await session.commit()
    except hubcfg.ActivationBlocked as exc:
        # The scalar fields of this patch are already assigned on the ORM
        # object at this point — rolling back is what keeps them out of the
        # database, and keeps the professional patch out with them.
        await session.rollback()
        logger.info(
            "hub_configuration_rolled_back",
            tenant_id=tenant_id,
            professional_id=professional_id,
            reason="activation_blocked",
            stage="apply",
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.reason) from None
    except Exception:
        await session.rollback()
        logger.warning(
            "hub_configuration_rolled_back",
            tenant_id=tenant_id,
            professional_id=professional_id,
            reason="apply_or_commit_failed",
            stage="apply_or_commit",
        )
        raise

    await session.refresh(tenant)
    if professional is not None:
        await session.refresh(professional)

    logger.info(
        "hub_configuration_updated",
        tenant_id=tenant_id,
        professional_id=professional_id,
        # Field NAMES only. The values are the clinic's configuration and never
        # belong in a log line.
        tenant_fields=sorted(tenant_data.keys()),
        professional_fields=sorted(professional_data.keys()),
        # Categorical: which side the professional's hours/services resolve
        # from AFTER the commit, so a save that replaced inheritance with an
        # empty override is legible without logging any config value. Omitted
        # entirely for a tenant-only save; never computed on a rollback path,
        # where the ORM instance is expired.
        **(hubcfg.config_source_fields(professional) if professional is not None else {}),
        outcome="committed",
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )

    return HubConfigurationRead(
        tenant=await hubcfg.tenant_read_model(session, tenant),
        professional=(
            await hubcfg.professional_list_item(session, professional, tenant)
            if professional is not None
            else None
        ),
    )
