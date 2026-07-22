"""Asaas payment webhook endpoint.

POST /webhooks/asaas - receives Pix payment lifecycle events (PAYMENT_RECEIVED,
PAYMENT_CONFIRMED, PAYMENT_OVERDUE, PAYMENT_DELETED, PAYMENT_REFUNDED, ...).

Unlike the Meta webhook (api/webhook.py), authenticity here is verified in the
WORKER, not in this handler: Asaas signs each tenant's webhook calls with a
PER-TENANT shared token (the `asaas-access-token` header, checked against
that tenant's own stored `asaas_webhook_token_encrypted`), and resolving
"which tenant" requires a DB round-trip (payment id -> PixDeposit ->
tenant_id) that this handler must never make — the golden webhook rule
(whatsapp-webhook-arq skill) is fast-ACK, no heavy work, and "heavy" here
includes any DB read. So this handler stays dumb+fast: light parse, enqueue,
200. `services/payments/deposit_lifecycle.py::apply_asaas_event` (run by the
`process_asaas_event` arq job — workers/payments_tasks.py) does the real
auth check + dedupe + state mutation, all in one place.
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from secretaria.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _extract_event_fields(payload: dict) -> tuple[str, str, str | None]:
    """Tolerant extraction of (event_id, event_type, payment_id) from an Asaas
    webhook payload — plain dict .get chains, no pydantic model (the shape is
    Asaas', free to grow new fields at any time). Asaas' own event id lives at
    `payload["id"]`; when absent, fall back to a synthetic id derived from
    (event, payment.id) so SOME idempotency key always exists rather than the
    ledger being skipped entirely."""
    event_type = str(payload.get("event") or "")
    payment = payload.get("payment")
    payment_id: str | None = None
    if isinstance(payment, dict):
        raw_id = payment.get("id")
        payment_id = str(raw_id) if raw_id is not None else None
    event_id = payload.get("id") or f"{event_type}:{payment_id}"
    return str(event_id), event_type, payment_id


@router.post("/webhooks/asaas")
async def receive_asaas_webhook(request: Request) -> JSONResponse:
    """Fast-ACK Asaas webhook receiver. Auth + processing happen in the worker."""
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("asaas_webhook_invalid_json")
        # Ack with 200 so Asaas does not retry a permanently broken payload.
        return JSONResponse({"status": "ignored"}, status_code=200)

    if not isinstance(payload, dict):
        logger.warning("asaas_webhook_unexpected_shape")
        return JSONResponse({"status": "ignored"}, status_code=200)

    event_id, event_type, payment_id = _extract_event_fields(payload)

    # Never logged: the shared per-tenant webhook secret.
    access_token = request.headers.get("asaas-access-token")

    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        logger.error("asaas_webhook_arq_pool_unavailable")
        # 503 -> Asaas retries once the queue is reachable again.
        return JSONResponse({"status": "unavailable"}, status_code=503)

    await arq_pool.enqueue_job(
        "process_asaas_event",
        event_id=event_id,
        event_type=event_type,
        payment_id=payment_id,
        access_token=access_token,
    )
    logger.info("asaas_webhook_enqueued", event_type=event_type)
    return JSONResponse({"status": "ok"}, status_code=200)
