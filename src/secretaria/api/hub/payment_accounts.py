"""Doctor-hub endpoints to connect a clinic's payment provider (spec sections 12.3, 12.3A).

    GET    /tenants/me/payment-accounts                       status of each connection
    PUT    /tenants/me/payment-accounts/asaas/connection      paste the API key (write-only)
    POST   /tenants/me/payment-accounts/asaas/retry-webhook   "Tentar de novo" (no key re-sent)
    DELETE /tenants/me/payment-accounts/asaas                 disconnect

The PUT is the ONE deliberate exception to "a credential never enters through a form"
(`PixSection.tsx`, the tenant-secrets-encryption skill): exactly one write-only field, over the
authenticated hub channel and the existing `/api/secretaria/` proxy, SEPARATE from
`PUT /configuration` (the key never enters the configuration envelope or the module snapshot).
The rules that make it safe:

* the body is parsed BY HAND (`_read_api_key`): a FastAPI/Pydantic validation error echoes the
  offending input, which for this field would be the key itself. Every malformed body is a
  fixed `422 {"detail": {"code": "invalid_body"}}`;
* the response is a whitelist (`schemas/payment_accounts.py`): status, reason, capability
  booleans, group CODES and pt-BR labels - never the key, a token or provider content;
* the key is validated, probed and only then encrypted (`services/payments/asaas_connection.py`);
  a refused or inconclusive key is dropped and NOTHING is stored;
* attempts are rate-limited per tenant (10 per 10 minutes, fail-closed without Redis), so the
  screen cannot be used as an oracle for guessed keys.

Who may call (D10, owner decision 2026-09-29, enforced HERE and not only in the screen): the
mutating endpoints require `require_clinic_manager` (manager only) and the status listing
`require_clinic_clinician` (manager or doctor). Both come from the TASK-020 role claim in the hub
token: a token without the claim is `403 role_claim_missing`, a secretary is `403 forbidden_role`.
The role gate is a route dependency, so it answers BEFORE the rate limiter and before any provider
call: a refused role never spends an attempt and never touches the key.
"""

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# The role dependencies are defined by the TASK-020 plan; if it chose a module other than
# `hub.deps`, change ONLY this import.
from secretaria.api.hub.deps import (
    get_current_tenant,
    require_clinic_clinician,
    require_clinic_manager,
)
from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import ClinicPaymentAccount, Tenant
from secretaria.models.clinic_payment_account import ACCOUNT_REVOKED, PROVIDER_ASAAS
from secretaria.schemas.payment_accounts import AsaasConnectionResultOut, PaymentAccountStatusOut
from secretaria.services.payments import accounts, asaas_connection
from secretaria.services.payments.asaas_connection import ConnectionResult
from secretaria.services.payments.asaas_probes import GROUP_LABELS
from secretaria.services.payments.rate_limit import RateLimiterUnavailable, allow

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/payment-accounts", tags=["hub-payment-accounts"])

RATE_LIMIT_ATTEMPTS = 10
RATE_LIMIT_WINDOW_SECONDS = 600
MAX_BODY_BYTES = 4096


def _detail(code: str, **extra) -> dict:
    return {"detail": {"code": code, **extra}}


def _labels(groups: tuple[str, ...]) -> list[str]:
    return [GROUP_LABELS.get(group, group) for group in groups]


def _result_response(result: ConnectionResult) -> JSONResponse:
    """Map a service result to the HTTP contract (F4 renders M1-M8 from these)."""
    if result.outcome == "refused":
        return JSONResponse(
            _detail(
                result.code or "key_invalid",
                missing_groups=list(result.missing_groups),
                excess_groups=list(result.excess_groups),
                missing_labels=_labels(result.missing_groups),
                excess_labels=_labels(result.excess_groups),
            ),
            status_code=422,
        )
    if result.outcome == "inconclusive":
        return JSONResponse(_detail(result.code or "verification_unavailable"), status_code=503)
    body = AsaasConnectionResultOut(
        status=result.status or "incomplete",
        incomplete_reason=result.incomplete_reason,
        capabilities=result.capabilities,
    )
    return JSONResponse(body.model_dump(), status_code=200)


async def _read_api_key(request: Request) -> str | None:
    """The `api_key` string of a body that is exactly `{"api_key": "<string>"}`, else None."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != {"api_key"}:
        return None
    key = data["api_key"]
    return key if isinstance(key, str) else None


async def _guard(request: Request, tenant: Tenant) -> JSONResponse | None:
    """Rate-limit a mutating attempt. None = allowed."""
    try:
        allowed = await allow(
            getattr(request.app.state, "arq_pool", None),
            key=f"payacct:{tenant.id}",
            limit=RATE_LIMIT_ATTEMPTS,
            window_seconds=RATE_LIMIT_WINDOW_SECONDS,
        )
    except RateLimiterUnavailable:
        return JSONResponse(_detail("rate_limiter_unavailable"), status_code=503)
    return None if allowed else JSONResponse(_detail("rate_limited"), status_code=429)


def _status_out(row: ClinicPaymentAccount) -> PaymentAccountStatusOut:
    excess = tuple(row.excess_groups or [])
    return PaymentAccountStatusOut(
        provider=row.provider,
        status=row.status,
        incomplete_reason=row.incomplete_reason,
        capabilities={k: bool(v) for k, v in (row.capabilities or {}).items()},
        excess_groups=list(excess),
        excess_labels=_labels(excess),
        connected_at=row.connected_at,
        last_verified_at=row.last_verified_at,
        ready_for_before=(row.status == "connected" and row.last_verified_at is not None),
    )


@router.get(
    "",
    response_model=list[PaymentAccountStatusOut],
    dependencies=[Depends(require_clinic_clinician)],
)
async def list_payment_accounts(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[PaymentAccountStatusOut]:
    """The connections of THIS clinic (a clinic with none gets an empty list)."""
    rows = (
        await session.scalars(
            select(ClinicPaymentAccount)
            .where(ClinicPaymentAccount.tenant_id == tenant.id)
            .order_by(ClinicPaymentAccount.provider)
        )
    ).all()
    return [_status_out(row) for row in rows]


@router.put(
    "/asaas/connection",
    response_model=AsaasConnectionResultOut,
    dependencies=[Depends(require_clinic_manager)],
)
async def put_asaas_connection(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Connect (or replace) the clinic's Asaas key. Body: `{"api_key": "<string>"}` only."""
    blocked = await _guard(request, tenant)
    if blocked is not None:
        return blocked
    api_key = await _read_api_key(request)
    if api_key is None:
        return JSONResponse(_detail("invalid_body"), status_code=422)

    result = await asaas_connection.connect_asaas_account(
        session,
        tenant_id=tenant.id,
        api_key=api_key,
        webhook_base_url=get_settings().payments_webhook_base_url or None,
    )
    del api_key  # the value is never needed again
    if result.outcome in ("connected", "incomplete"):
        await session.commit()
    return _result_response(result)


@router.post(
    "/asaas/retry-webhook",
    response_model=AsaasConnectionResultOut,
    dependencies=[Depends(require_clinic_manager)],
)
async def retry_asaas_webhook(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """"Tentar de novo": redo the webhook step with the key already approved and stored."""
    blocked = await _guard(request, tenant)
    if blocked is not None:
        return blocked
    result = await asaas_connection.retry_webhook_setup(
        session,
        tenant_id=tenant.id,
        webhook_base_url=get_settings().payments_webhook_base_url or None,
    )
    if result.outcome in ("connected", "incomplete"):
        await session.commit()
    return _result_response(result)


@router.delete(
    "/asaas",
    response_model=AsaasConnectionResultOut,
    dependencies=[Depends(require_clinic_manager)],
)
async def disconnect_asaas(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Forget the key (and try to remove the webhook at Asaas). 404 when nothing is connected."""
    blocked = await _guard(request, tenant)
    if blocked is not None:
        return blocked
    row = await accounts.get_account_row(session, tenant_id=tenant.id, provider=PROVIDER_ASAAS)
    if row is None or row.status == ACCOUNT_REVOKED:
        return JSONResponse(_detail("not_connected"), status_code=404)
    await asaas_connection.disconnect_asaas_account(session, tenant_id=tenant.id)
    await session.commit()
    return JSONResponse(AsaasConnectionResultOut(status="revoked").model_dump(), status_code=200)
