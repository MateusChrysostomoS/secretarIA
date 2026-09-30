"""Per-clinic payment webhook: POST /webhooks/{provider}/{webhook_slug}.

The clinic is identified by the URL (an opaque, unguessable slug), the request is
authenticated BEFORE anything is processed, and the event is made DURABLE before the 200:
one indexed SELECT (slug -> account), the provider's cheap `verify_webhook`, one
`INSERT ... ON CONFLICT DO NOTHING` into `payment_webhook_events`, then only the row id goes
to arq. Nothing else runs in the handler (the event processor calls the provider, moves money
state and talks to the patient - all in the worker).

WHY THIS BREAKS THE "FAST-ACK, NO DB" RULE OF THE META HANDLER (whatsapp-webhook-arq skill),
deliberately and ONLY here: that rule protects a 5-second ACK budget (Meta disables the
webhook past it). Payment providers give far more room (Mercado Pago: 22 s; Asaas retries and
pauses its queue only after 15 consecutive failures), and the checklist for money requires
authenticating and deduplicating BEFORE processing, which needs to know the clinic. The old
handler (`api/webhook_asaas.py`) answered 200 to any JSON and enqueued it unauthenticated, so
anyone could fill the queue. `api/webhook.py` (Meta) is unchanged.

Status contract: 404 (no body) unknown provider/slug; 413 body over 64 KB; 415 not JSON;
401 (no body) failed authenticity; 200 for everything accepted - `ok` (new), `duplicate`
(replay) and `ignored` (well-formed but not a payment event, or not JSON at all: a permanently
broken payload must not be retried); 503 only when the DB write fails, so the provider retries.
A queue outage is NOT a 503: the event is already durable and the sweep re-enqueues it.
"""

import json

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models.payment_webhook_event import EVENT_RECEIVED, PaymentWebhookEvent
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, registry
from secretaria.services.payments.db_utils import insert_ignore
from secretaria.services.payments.webhook_payload import event_to_dict, minimize_payload

logger = get_logger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 64 * 1024

# Asaas' official production webhook IPs (docs.asaas.com/docs/ips-oficiais-do-asaas, read
# 2026-09-29). Sandbox may use others (not verified), so the default mode only ALERTS.
ASAAS_WEBHOOK_IPS = frozenset(
    {"52.67.12.206", "18.230.8.159", "54.94.136.112", "54.94.183.101"}
)


def _ip_mode() -> str:
    return get_settings().ASAAS_WEBHOOK_IP_MODE.strip().lower()


def _client_ip(request: Request) -> str | None:
    """The address our proxy saw: the LAST `X-Forwarded-For` hop (the proxy appends the peer
    it received the connection from; earlier entries are client-supplied and untrusted)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.client.host if request.client else None


@router.post("/webhooks/{provider}/{webhook_slug}")
async def receive_payment_webhook(
    provider: str,
    webhook_slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    impl = registry.find_provider(provider)
    if impl is None:
        return Response(status_code=404)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return Response(status_code=413)
    if "json" not in (request.headers.get("content-type") or "").lower():
        return Response(status_code=415)
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        return Response(status_code=413)

    row = await accounts.get_account_by_slug(session, provider=provider, webhook_slug=webhook_slug)
    if row is None:
        return Response(status_code=404)
    account = cfg.payment_account_from_row(row)

    if provider == "asaas":
        mode = _ip_mode()
        ip = _client_ip(request)
        if mode != "off" and ip not in ASAAS_WEBHOOK_IPS:
            logger.warning("asaas_webhook_ip_not_allowlisted", mode=mode, ip=ip)
            if mode == "enforce":
                return Response(status_code=403)

    headers, query = dict(request.headers), dict(request.query_params)
    if not impl.verify_webhook(account, headers, raw_body, query):
        logger.warning("payment_webhook_auth_failed", provider=provider)
        return Response(status_code=401)

    try:
        events = impl.parse_webhook(raw_body, headers)
        body = json.loads(raw_body)
    except ValueError:
        logger.warning("payment_webhook_unparseable", provider=provider)
        return JSONResponse({"status": "ignored"}, status_code=200)
    if not events:
        return JSONResponse({"status": "ignored"}, status_code=200)

    raw = minimize_payload(body)
    new_ids = []
    try:
        for event in events:
            row_id = await insert_ignore(
                session,
                PaymentWebhookEvent,
                {
                    "tenant_id": row.tenant_id,
                    "provider": provider,
                    "event_id": event.event_id,
                    "provider_charge_id": event.provider_charge_id,
                    "event_type": event.event_type[:64],
                    "status": EVENT_RECEIVED,
                    "attempts": 0,
                    "payload": {"normalized": event_to_dict(event), "raw": raw},
                },
                index_elements=["tenant_id", "provider", "event_id"],
            )
            if row_id is not None:
                new_ids.append(row_id)
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        logger.error("payment_webhook_persist_failed", error_type=type(exc).__name__)
        return JSONResponse({"status": "unavailable"}, status_code=503)

    pool = getattr(request.app.state, "arq_pool", None)
    for row_id in new_ids:
        try:
            if pool is None:
                raise RuntimeError("arq pool unavailable")
            await pool.enqueue_job("process_payment_event", str(row_id))
        except Exception as exc:  # noqa: BLE001 - durable already; the sweep re-enqueues
            logger.warning("payment_webhook_enqueue_failed", error_type=type(exc).__name__)

    logger.info("payment_webhook_received", provider=provider, new=len(new_ids))
    return JSONResponse({"status": "ok" if new_ids else "duplicate"}, status_code=200)
