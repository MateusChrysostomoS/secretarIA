"""Doctor hub - "Pagamentos que precisam de decisão" (spec sections 6.5 and 10A.2).

    GET  /tenants/me/payment-attention-items?status=open|resolved&limit=50&cursor=
    POST /tenants/me/payment-attention-items/{id}/resolve

A payment that is PAID while the consultation is not (or stopped being) confirmed never triggers a
refund (decision D15): the system writes an attention item and the clinic decides - refund in the
provider's own panel, rebook, keep as credit - and resolves it here with a mandatory note.

Rules that make it safe:

* everything is filtered by the tenant of the hub token; an id of another clinic is a 404 (never
  a 403, which would confirm it exists);
* the output is a whitelist (`schemas/payment_attention.py`): the patient is a display name read
  when shown, never a document, a phone, a charge id or a payer;
* `resolve` parses its body BY HAND: a validation error would echo the note, and a note is where a
  clinic may type a CPF by mistake. Every refusal is `422 {"detail": {"code"}}` and the code never
  contains what was typed. A second resolve is `409 already_resolved` with the current state: the
  first decision is never overwritten;
* resolving records the clinic's decision only. It moves no money and calls no provider; the one
  money-row effect is `cancelado_reembolsado`, written when the clinic says it refunded in the
  provider's panel (`services/payments/attention_view.py`).

Who may resolve (owner answer 12, TASK-020): the hub token now carries the acting user's role, so
the rule is enforced HERE and not only in the UI. `resolve` depends on `require_clinic_clinician`
(a manager or a doctor; a secretary and a token without a role get `403 forbidden_role` /
`role_claim_missing`, before the body is read and before the rate limiter). Listing stays on
`get_current_tenant`: every Chat role sees the notice.
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant, require_clinic_clinician
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.payment_attention_item import ATTENTION_OPEN, ATTENTION_RESOLVED
from secretaria.schemas.payment_attention import (
    AttentionItemOut,
    AttentionListOut,
    AttentionResolutionOut,
)
from secretaria.services.payments import attention_view
from secretaria.services.payments.attention_view import ItemRow, ResolveRefused
from secretaria.services.payments.rate_limit import RateLimiterUnavailable, allow

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/payment-attention-items", tags=["hub-payment-attention"])

RESOLVE_LIMIT = 30
RESOLVE_WINDOW_SECONDS = 60
MAX_BODY_BYTES = 4096


def _item_out(row: ItemRow) -> AttentionItemOut:
    item = row.item
    resolution = None
    if item.status == ATTENTION_RESOLVED and item.resolved_at is not None:
        resolution = AttentionResolutionOut(
            action=item.resolution_action or "other",
            note=item.resolution_note or "",
            resolved_by_name=item.resolved_by_name or "",
            resolved_at=item.resolved_at,
        )
    return AttentionItemOut(
        id=str(item.id),
        reason=item.reason,
        reason_text=attention_view.reason_text(
            item.reason, item.suggested_refund_cents, item.amount_cents
        ),
        patient_display_name=row.patient_name,
        service_label=item.service_label,
        slot_start=item.slot_start,
        slot_end=item.slot_end,
        amount_cents=item.amount_cents,
        paid_amount_cents=item.paid_amount_cents,
        paid_at=item.paid_at,
        provider=item.provider,
        suggested_refund_cents=item.suggested_refund_cents,
        provider_refund_seen_at=item.provider_refund_seen_at,
        detected_at=item.detected_at,
        status=item.status,
        resolution=resolution,
    )


def _detail(code: str, status_code: int) -> JSONResponse:
    return JSONResponse({"detail": {"code": code}}, status_code=status_code)


@router.get("", response_model=AttentionListOut)
async def list_attention_items(
    status: str = Query(default=ATTENTION_OPEN, pattern="^(open|resolved)$"),
    limit: int = Query(default=attention_view.DEFAULT_LIMIT, ge=1, le=attention_view.MAX_LIMIT),
    cursor: str | None = Query(default=None, max_length=200),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> AttentionListOut:
    page = await attention_view.list_items(
        session, tenant.id, status=status, limit=limit, cursor=cursor
    )
    return AttentionListOut(
        open_count=page.open_count,
        items=[_item_out(row) for row in page.rows],
        next_cursor=page.next_cursor,
    )


async def _read_body(request: Request) -> dict | None:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@router.post("/{item_id}/resolve")
async def resolve_attention_item(
    item_id: str,
    request: Request,
    # A manager or a doctor (TASK-020): the same `Tenant` `get_current_tenant` returns, with the
    # role checked first. A secretary never reaches the body, the limiter or the item.
    tenant: Tenant = Depends(require_clinic_clinician),
    session: AsyncSession = Depends(get_session),
):
    try:
        item_uuid = UUID(item_id)
    except ValueError:
        return _detail("not_found", 404)

    # Rate limit per clinic. Fail-OPEN, unlike the credential endpoint: resolving is idempotent
    # and moves no money, so a Redis outage must not stop a clinic from closing an item.
    try:
        if not await allow(
            getattr(request.app.state, "arq_pool", None),
            key=f"payattn-resolve:{tenant.id}",
            limit=RESOLVE_LIMIT,
            window_seconds=RESOLVE_WINDOW_SECONDS,
        ):
            return _detail("rate_limited", 429)
    except RateLimiterUnavailable:
        logger.warning("attention_resolve_rate_limiter_unavailable")

    data = await _read_body(request)
    if data is None:
        return _detail("invalid_body", 422)
    try:
        action, note, name = attention_view.check_resolution_input(
            data.get("action"), data.get("note"), data.get("resolved_by_name")
        )
    except ResolveRefused as refused:
        return _detail(refused.code, 422)

    outcome = await attention_view.resolve_item(
        session, tenant.id, item_uuid, action=action, note=note, resolved_by_name=name
    )
    if outcome == "not_found":
        return _detail("not_found", 404)
    row = await attention_view.get_item(session, tenant.id, item_uuid)
    if outcome == "already_resolved":
        body = _item_out(row).model_dump(mode="json")
        return JSONResponse({"detail": {"code": "already_resolved", "item": body}}, status_code=409)
    logger.info("attention_item_resolved", action=action)
    return JSONResponse(_item_out(row).model_dump(mode="json"), status_code=200)
