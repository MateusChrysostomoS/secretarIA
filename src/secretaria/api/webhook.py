"""WhatsApp webhook endpoints.

GET  /webhook  - Meta verification handshake (echoes hub.challenge).
POST /webhook  - receives events: validates HMAC, dedupes, enqueues to arq.

WEBHOOK FIELDS TO SUBSCRIBE in Meta for Developers
(App -> WhatsApp -> Configuration -> Webhook fields):
  * messages            - inbound patient messages.
  * smb_message_echoes  - echoes of messages sent by the human secretary
                          from the WhatsApp mobile app. Required for the
                          Coexistence handover logic.
"""

import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.core.security import verify_meta_signature
from secretaria.models.processed_event import ProcessedEvent
from secretaria.schemas.webhook import iter_audio_messages, iter_event_ids

logger = get_logger(__name__)
router = APIRouter()


@router.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    """Meta verification handshake.

    Meta calls this once when the webhook is configured. When the verify
    token matches, echo back `hub.challenge` as PLAIN TEXT (not JSON).
    """
    settings = get_settings()
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token and token == settings.META_VERIFY_TOKEN:
        logger.info("webhook_verification_ok")
        return PlainTextResponse(content=challenge or "", status_code=200)

    logger.warning("webhook_verification_failed", mode=mode)
    return PlainTextResponse(content="Forbidden", status_code=403)


async def _filter_new_event_ids(event_ids: list[str]) -> list[str]:
    """Return the subset of `event_ids` not yet in `processed_events`.

    Fail-open: on any DB error return all ids so the event is still
    enqueued - the worker re-checks idempotency before processing.
    """
    try:
        async with async_session_factory() as session:
            rows = await session.scalars(
                select(ProcessedEvent.event_id).where(ProcessedEvent.event_id.in_(event_ids))
            )
            already_seen = set(rows.all())
        return [eid for eid in event_ids if eid not in already_seen]
    except Exception as exc:
        logger.warning("idempotency_check_failed", error=str(exc))
        return event_ids


@router.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    """Receive a webhook event.

    GOLDEN RULE: return 200 in well under 5 seconds. Only minimal synchronous
    work happens here (signature check, light parsing, idempotency fast-path,
    enqueue). Everything heavy is handed to the arq worker.
    """
    settings = get_settings()

    # 1. Raw body BEFORE any parsing - required for a correct HMAC check.
    raw_body = await request.body()

    # 2. Validate the HMAC-SHA256 signature.
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(raw_body, signature, settings.META_APP_SECRET):
        logger.warning("webhook_rejected_invalid_signature")
        return JSONResponse({"error": "invalid signature"}, status_code=403)

    # 3. Light parsing.
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("webhook_invalid_json")
        # Ack with 200 so Meta does not retry a permanently broken payload.
        return JSONResponse({"status": "ignored"}, status_code=200)

    # 4. Idempotency fast-path: skip if every event id was already processed.
    event_ids = list(iter_event_ids(payload))
    if event_ids:
        new_ids = await _filter_new_event_ids(event_ids)
        if not new_ids:
            logger.info("webhook_duplicate_skipped", event_ids=event_ids)
            return JSONResponse({"status": "duplicate"}, status_code=200)

    # 5. Enqueue for async processing on the arq worker.
    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        logger.error("webhook_arq_pool_unavailable")
        # 503 -> Meta retries once the queue is reachable again.
        return JSONResponse({"status": "unavailable"}, status_code=503)

    await arq_pool.enqueue_job("process_webhook_event", payload)

    # Voice notes get their own dedicated job with a minimal payload (never
    # the full body) - the download + STT work happens there, off this
    # request/response cycle. See workers/tasks.py:transcribe_audio_message.
    audio_count = 0
    for audio_ref in iter_audio_messages(payload):
        await arq_pool.enqueue_job("transcribe_audio_message", **audio_ref)
        audio_count += 1
    if audio_count:
        logger.info("webhook_audio_enqueued", count=audio_count)

    logger.info("webhook_enqueued", event_count=len(event_ids))

    # 6. Fast 200 ack.
    return JSONResponse({"status": "ok"}, status_code=200)
