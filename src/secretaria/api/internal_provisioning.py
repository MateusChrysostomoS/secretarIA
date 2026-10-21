"""Internal onboarding/provisioning API (`/internal/*`) — INTERNAL ONLY.

Six endpoints (cross-service contract v1 §4) that let brain-api (the
onboarding orchestrator) provision a secretaria tenant ahead of its WhatsApp
connection, wire that connection once it happens, read onboarding-completeness,
attach professionals by name, activate the bot, and enqueue transactional
email. Same trust boundary as api/internal.py (see its module docstring):
gated by `require_internal_api_key` at the router level, imported (not
redefined) from there — this is a SEPARATE module (not added to
api/internal.py itself) purely because that cluster has grown large enough to
warrant splitting by concern, mirroring how api/internal_privacy.py already
split off the LGPD endpoints.

Business logic lives in services/provisioning.py; this module only
parses/validates input, calls the service, and shapes the response.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.internal import require_internal_api_key
from secretaria.core.crypto import EncryptionError
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Professional, Tenant
from secretaria.schemas.internal_provisioning import (
    ActivateOut,
    AsaasConnectionIn,
    AsaasConnectionOut,
    ConfigStatusOut,
    NotificationEmailIn,
    NotificationEmailOut,
    ProfessionalAttachIn,
    ProfessionalAttachOut,
    ProfessionalStatusOut,
    ProvisionTenantIn,
    ProvisionTenantOut,
    WhatsappConnectionIn,
    WhatsappConnectionOut,
)
from secretaria.schemas.professional import ProfessionalListItem
from secretaria.services import provisioning
from secretaria.services.email import is_known_template
from secretaria.services.hub_configuration import professional_list_item
from secretaria.services.professional_calendar_setup import prepare_shared_professional_calendar
from secretaria.services.professional_capacity import ProfessionalCapacityError
from secretaria.services.staff_welcome import seal_staff_welcome

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal",
    tags=["internal", "internal-provisioning"],
    dependencies=[Depends(require_internal_api_key)],
)

_TENANT_PATH = Annotated[UUID, Path(description="Tenant UUID — the only scope.")]

_INTERNAL_RESPONSES = {
    401: {"description": "Missing or invalid X-Internal-Api-Key."},
    403: {"description": "INTERNAL_API_KEY not configured on the server."},
}
_NOT_FOUND_RESPONSES = {**_INTERNAL_RESPONSES, 404: {"description": "Unknown tenant."}}


@router.get("/tenants/{tenant_id}/professionals", response_model=list[ProfessionalListItem])
async def internal_professional_roster(
    tenant_id: _TENANT_PATH,
    session: AsyncSession = Depends(get_session),
) -> list[ProfessionalListItem]:
    """The complete roster, including inactive linked professionals; internal only."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "Unknown tenant")
    rows = await session.scalars(
        select(Professional)
        .where(Professional.tenant_id == tenant.id)
        .order_by(Professional.created_at, Professional.id)
    )
    return [await professional_list_item(session, row, tenant) for row in rows]


@router.post(
    "/tenants",
    response_model=ProvisionTenantOut,
    summary="Provision a secretaria tenant ahead of its WhatsApp connection (internal)",
    description=(
        "Creates the tenant row with the CALLER-SUPPLIED tenant_id as primary key "
        "(the brain-api tenant id — same UUID everywhere, contract v1 §0). Idempotent: "
        "an id that already exists is returned unmodified (created=false)."
    ),
    responses=_INTERNAL_RESPONSES,
)
async def provision_tenant(
    body: ProvisionTenantIn,
    session: AsyncSession = Depends(get_session),
) -> ProvisionTenantOut:
    tenant, created = await provisioning.provision_tenant(
        session,
        tenant_id=body.tenant_id,
        clinic_name=body.clinic_name,
        contact_email=body.contact_email,
        timezone=body.timezone,
    )
    await session.commit()
    return ProvisionTenantOut(tenant_id=tenant.id, created=created)


@router.post(
    "/tenants/{tenant_id}/whatsapp-connection",
    response_model=WhatsappConnectionOut,
    summary="Wire a tenant's WhatsApp connection (internal)",
    description=(
        "Sets phone_number_id/waba_id/connected_at; stores the access token via "
        "set_waba_token when provided. 404 unknown tenant; 409 phone_number_conflict "
        "when another tenant already holds that phone_number_id. Idempotent for the "
        "same values."
    ),
    responses={
        **_NOT_FOUND_RESPONSES,
        409: {"description": "phone_number_id already used by another tenant."},
    },
)
async def whatsapp_connection(
    tenant_id: _TENANT_PATH,
    body: WhatsappConnectionIn,
    session: AsyncSession = Depends(get_session),
) -> WhatsappConnectionOut:
    outcome = await provisioning.connect_whatsapp(
        session,
        tenant_id=tenant_id,
        phone_number_id=body.phone_number_id,
        waba_id=body.waba_id,
        access_token=body.access_token,
    )
    if outcome is provisioning.ConnectOutcome.NOT_FOUND:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tenant")
    if outcome is provisioning.ConnectOutcome.CONFLICT:
        raise HTTPException(status.HTTP_409_CONFLICT, "phone_number_conflict")
    await session.commit()
    return WhatsappConnectionOut(connected=True)


@router.post(
    "/tenants/{tenant_id}/asaas-connection",
    response_model=AsaasConnectionOut,
    summary="Connect a tenant's Asaas account from an API key (internal)",
    description=(
        "Runs the same connection service as the clinic's own screen: format check, permission "
        "probes (a key with permissions beyond Clientes/Cobranças/Webhooks is REFUSED and never "
        "stored), then stores the key encrypted and creates + reads back the clinic's webhook. "
        "`webhook_token` is optional (generated when absent). Values are never echoed back or "
        "logged. 404 unknown tenant; 422 refused key (`detail.code`); 503 could not verify. "
        "Same internal auth as whatsapp-connection."
    ),
    responses={
        **_NOT_FOUND_RESPONSES,
        422: {"description": "Key refused (invalid, missing or excess permissions)."},
        503: {"description": "Permissions could not be verified; nothing was stored."},
    },
)
async def asaas_connection(
    tenant_id: _TENANT_PATH,
    body: AsaasConnectionIn,
    session: AsyncSession = Depends(get_session),
) -> AsaasConnectionOut:
    result = await provisioning.connect_asaas(
        session, tenant_id=tenant_id, api_key=body.api_key, webhook_token=body.webhook_token
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tenant")
    if result.outcome == "refused":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": result.code,
                "missing_groups": list(result.missing_groups),
                "excess_groups": list(result.excess_groups),
            },
        )
    if result.outcome == "inconclusive":
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"code": result.code})
    await session.commit()
    return AsaasConnectionOut(
        status="ok" if result.outcome == "connected" else "incomplete", asaas_connected=True
    )


@router.get(
    "/tenants/{tenant_id}/config-status",
    response_model=ConfigStatusOut,
    summary="Onboarding config-completeness status for a tenant (internal)",
    description=(
        "Connected/mode-resolved/history-sync facts plus the partial-activation-aware "
        "config completeness (services/tenant_config.professional_completeness)."
    ),
    responses=_NOT_FOUND_RESPONSES,
)
async def config_status(
    tenant_id: _TENANT_PATH,
    session: AsyncSession = Depends(get_session),
) -> ConfigStatusOut:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tenant")
    result = await provisioning.get_config_status(session, tenant)
    return ConfigStatusOut(
        connected=result.connected,
        connected_at=result.connected_at,
        mode_resolved=result.mode_resolved,
        mode_resolved_at=result.mode_resolved_at,
        history_sync_status=result.history_sync_status,
        config_complete=result.config_complete,
        reasons=result.reasons,
        complete_count=result.complete_count,
        total_active=result.total_active,
        professionals=[
            ProfessionalStatusOut(
                id=p.id,
                name=p.name,
                is_active=p.is_active,
                has_calendar=p.has_calendar,
                has_hours=p.has_hours,
                has_services=p.has_services,
                complete=p.complete,
            )
            for p in result.professionals
        ],
    )


@router.post(
    "/tenants/{tenant_id}/professionals",
    response_model=ProfessionalAttachOut,
    summary="Create-or-attach a professional by name (internal)",
    description=(
        "Case-insensitive match on an existing ACTIVE professional of this tenant -> "
        "returns it (created=false). Otherwise creates a new one. After registration, "
        "shared-account clinics prepare a missing secondary calendar without overwriting "
        "existing professional settings."
    ),
    responses=_NOT_FOUND_RESPONSES,
)
async def attach_professional(
    tenant_id: _TENANT_PATH,
    body: ProfessionalAttachIn,
    session: AsyncSession = Depends(get_session),
) -> ProfessionalAttachOut:
    try:
        result = await provisioning.create_or_attach_professional(
            session,
            tenant_id=tenant_id,
            name=body.name,
            specialty=body.specialty,
            about=body.about,
            interactive=body.interactive,
        )
    except ProfessionalCapacityError as exc:
        raise HTTPException(exc.status, exc.code) from None
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tenant")
    professional, created = result
    await session.commit()
    tenant = await session.get(Tenant, professional.tenant_id)
    await prepare_shared_professional_calendar(session, tenant, professional)
    return ProfessionalAttachOut(professional_id=professional.id, created=created)


@router.post(
    "/tenants/{tenant_id}/activate",
    response_model=ActivateOut,
    summary="Activate the bot for a tenant, professional-aware (internal)",
    description=(
        "Validates the professional-aware can_activate gate AND that the WhatsApp "
        "number is connected; sets is_active=True on success. Idempotent — always "
        "200, `activated` reflects whether the gate passed."
    ),
    responses=_NOT_FOUND_RESPONSES,
)
async def activate(
    tenant_id: _TENANT_PATH,
    session: AsyncSession = Depends(get_session),
) -> ActivateOut:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tenant")
    activated, reasons = await provisioning.activate_tenant(session, tenant)
    await session.commit()
    return ActivateOut(activated=activated, reasons=reasons)


@router.post(
    "/notifications/email",
    response_model=NotificationEmailOut,
    summary="Enqueue a transactional onboarding email (internal)",
    description=(
        "Enqueues the `send_transactional_email` arq job (workers/tasks.py). "
        "`queued=false` (never an error) when the arq pool is unreachable — the same "
        "fail-soft shape as every other best-effort queue write in this service. An "
        "unknown `template`, by contrast, IS an error (422): it is a caller defect no "
        "retry can fix, and answering 200 for it would promise an e-mail nobody sends."
    ),
    responses={
        **_INTERNAL_RESPONSES,
        422: {"description": "Unknown template id — this service cannot render it."},
    },
)
async def notifications_email(
    body: NotificationEmailIn,
    request: Request,
) -> NotificationEmailOut:
    # Resolve the template HERE, not only in the worker. The queue accepts any string,
    # and an id this service cannot render dies as a WARNING inside the arq process —
    # invisible to the caller, who already got its 200. That gap is what swallowed every
    # patient login code while brain-api's `patient_access_otp` went unregistered, so the
    # enqueue side now refuses what the render side could never have delivered.
    if not is_known_template(body.template):
        logger.error("provisioning_email_unknown_template", template=body.template)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown email template: {body.template}",
        )

    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        logger.error("provisioning_email_arq_pool_unavailable", template=body.template)
        return NotificationEmailOut(queued=False)
    if body.template == "staff_temporary_password":
        try:
            sealed, remaining = seal_staff_welcome({**body.variables, "to": body.to})
            await arq_pool.enqueue_job(
                "send_staff_welcome_email", payload_encrypted=sealed, _expires=remaining
            )
        except EncryptionError:
            logger.error("staff_welcome_encryption_unavailable")
            return NotificationEmailOut(queued=False)
        except ValueError:
            raise HTTPException(422, "Invalid staff welcome request") from None
        except Exception:
            logger.warning("staff_welcome_enqueue_failed")
            return NotificationEmailOut(queued=False)
        return NotificationEmailOut(queued=True)
    await arq_pool.enqueue_job("send_transactional_email", body.template, body.to, body.variables)
    logger.info("provisioning_email_enqueued", template=body.template)
    return NotificationEmailOut(queued=True)
