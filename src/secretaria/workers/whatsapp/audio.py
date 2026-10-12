"""audio - split out of workers/tasks.py (TASK-023)."""

import httpx
from sqlalchemy.exc import IntegrityError
from transcription_core import (
    MediaTooLarge,
    NotAudio,
    TranscriptionConfig,
    TranscriptionError,
    TranscriptionResult,
    transcribe_whatsapp_media,
)

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger, wa_suffix
from secretaria.models import (
    ProcessedEvent,
)
from secretaria.services.tenant_config import (
    get_waba_token,
)
from secretaria.workers.orchestrator import (
    _send_bot_reply,
)
from secretaria.workers.shared.db import (
    _event_already_processed,
)
from secretaria.workers.shared.sender import (
    _send_simple_text,
    _tenant_client,
)
from secretaria.workers.shared.text import (
    AUDIO_UNINTELLIGIBLE_MESSAGE,
)
from secretaria.workers.whatsapp.db import (
    _resolve_tenant,
)
from secretaria.workers.whatsapp.inbound import (
    _persist_inbound_message,
)
from secretaria.workers.whatsapp.rate_limit import (
    _is_rate_limited,
)

logger = get_logger(__name__)


def _transcription_config() -> TranscriptionConfig:
    """Build the transcription-core config from env settings.

    Deliberately reads OPENAI_TRANSCRIPT_MODEL, never OPENAI_SECRETARIA_MODEL —
    the chat/agent model and the STT model must not cross-contaminate.
    """
    settings = get_settings()
    return TranscriptionConfig(
        openai_api_key=settings.OPENAI_API_KEY,
        openai_model=settings.OPENAI_TRANSCRIPT_MODEL,
        groq_api_key=settings.GROQ_API_KEY or None,
        primary=settings.AUDIO_TRANSCRIPTION_PRIMARY,
        domain_prompt=settings.AUDIO_DOMAIN_PROMPT or None,
        language="pt",
    )

async def _mark_audio_event_processed(message_id: str) -> None:
    """Claim `message_id` in the ProcessedEvent ledger without a full inbound persist.

    Used when a voice note fails permanently (rejected media) or transcribes
    with low confidence: the patient gets a clarification instead of a
    transcript, but the event must still be marked processed so a Meta
    redelivery never pays for STT again on the same wamid.
    """
    async with async_session_factory() as session:
        try:
            async with session.begin():
                if not await _event_already_processed(session, message_id):
                    session.add(ProcessedEvent(event_id=message_id))
        except IntegrityError:
            # A concurrent worker already claimed this event id.
            logger.info("worker_audio_duplicate_race", wam_id=message_id)

async def transcribe_audio_message(
    ctx: dict,
    *,
    media_id: str,
    phone_number_id: str | None,
    wa_id: str,
    message_id: str,
    patient_name: str | None = None,
) -> None:
    """arq job: transcribe one inbound WhatsApp voice note, then reply like text.

    Enqueued by the webhook POST handler (api/webhook.py) right alongside
    `process_webhook_event`, with a deliberately minimal payload —
    media_id/phone_number_id/wa_id/message_id/patient_name only, never the
    full webhook body. All download + STT + reply work happens here, off the
    request/response cycle (see `iter_audio_messages`).

    Error policy:
    - Permanent failures (`MediaTooLarge`, `NotAudio`) get the ProcessedEvent
      id claimed explicitly (`_mark_audio_event_processed`) and the patient
      receives `AUDIO_UNINTELLIGIBLE_MESSAGE`. Retrying a file that will
      never transcribe usefully would just burn another Graph API call.
    - A low-confidence (or empty) transcript is treated the same way: marked
      processed + the same clarification, WITHOUT ever reaching the LLM.
    - Any other `TranscriptionError` (e.g. `MediaFetchError`,
      `AllProvidersFailed`) is logged and re-raised so arq retries the job.
      The ProcessedEvent id is deliberately NOT claimed on this path, so a
      retry is safe. The media URL is re-fetched fresh from Meta on every
      attempt (short-lived Graph API URLs), so nothing is stale.
    - On success, the ProcessedEvent id is claimed by `_persist_inbound_message`
      itself — the exact same seam the text path uses — so it is claimed
      exactly once and a retried job never pays for STT twice.
    """
    # Single rate-limit increment for audio: _handle_patient_messages skips
    # audio messages entirely (see the guard added there), so this is the
    # only place an audio message's inbound rate limit is counted/checked.
    if await _is_rate_limited(ctx.get("redis"), phone_number_id, wa_id):
        logger.info("worker_audio_rate_limited", wa_id_suffix=wa_suffix(wa_id))
        return

    async with async_session_factory() as session:
        async with session.begin():
            if await _event_already_processed(session, message_id):
                logger.info("worker_audio_duplicate", wam_id=message_id)
                return

            tenant = await _resolve_tenant(session, phone_number_id)
            if tenant is None:
                logger.error("worker_audio_tenant_unresolved", phone_number_id=phone_number_id)
                return

            # Snapshot plain values so they survive past this session's close
            # (expire_on_commit=False makes this safe for already-loaded
            # attributes too, but explicit snapshots keep intent obvious).
            tenant_is_active = tenant.is_active
            # Decrypt inside the session (single seam); only the in-memory
            # value travels from here on - never logged, never returned.
            waba_token = await get_waba_token(session, tenant.id)

    # Tenant not activated yet: replicate today's zero-STT-spend behavior
    # exactly (single polite fallback, no conversation/message/LLM touched).
    if not tenant_is_active:
        reply = await _persist_inbound_message(
            phone_number_id=phone_number_id,
            wa_id=wa_id,
            patient_name=patient_name,
            wam_id=message_id,
            body=None,
        )
        if reply is not None:
            await _send_bot_reply(reply, redis=ctx.get("redis"))
        return

    # Fail closed (PROMPT_FIX_21): the tenant's OWN decrypted token, never the
    # global META_ACCESS_TOKEN. It authenticates BOTH the Graph media download
    # and the clarification reply, so a missing credential must stop the job
    # before either - which also means zero STT spend on an unusable turn.
    reply_client = _tenant_client(tenant, waba_token)
    if reply_client is None:
        logger.error("worker_audio_no_token", tenant_id=str(tenant.id), wam_id=message_id)
        return
    token = waba_token

    # A ValueError here means TranscriptionConfig rejected the configured STT
    # model/provider combo - a misconfiguration, not a per-message failure.
    # Deliberately left to propagate loudly into the arq error logs.
    config = _transcription_config()

    client = ctx.get("http_client")
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient()
    try:
        try:
            result: TranscriptionResult = await transcribe_whatsapp_media(
                media_id,
                token,
                api_version=get_settings().META_GRAPH_API_VERSION,
                config=config,
                http_client=client,
            )
        except (MediaTooLarge, NotAudio) as exc:
            await _mark_audio_event_processed(message_id)
            await _send_simple_text(wa_id, AUDIO_UNINTELLIGIBLE_MESSAGE, client=reply_client)
            logger.info("worker_audio_rejected", wam_id=message_id, reason=type(exc).__name__)
            return
        except TranscriptionError:
            # MediaFetchError / AllProvidersFailed are usually transient (a
            # blip fetching from Meta, or every STT provider briefly down);
            # let arq retry. The media URL is re-fetched fresh next attempt.
            logger.warning("worker_audio_transcription_failed", wam_id=message_id)
            raise
    finally:
        if owns_client:
            await client.aclose()

    if result.is_low_confidence:
        await _mark_audio_event_processed(message_id)
        await _send_simple_text(wa_id, AUDIO_UNINTELLIGIBLE_MESSAGE, client=reply_client)
        logger.info(
            "worker_audio_low_confidence",
            wam_id=message_id,
            provider=result.provider_used,
            chars=result.char_count,
        )
        return

    # NEVER log result.text (the transcript itself) - char_count/provider only.
    logger.info(
        "worker_audio_transcribed",
        wam_id=message_id,
        provider=result.provider_used,
        chars=result.char_count,
    )
    reply = await _persist_inbound_message(
        phone_number_id=phone_number_id,
        wa_id=wa_id,
        patient_name=patient_name,
        wam_id=message_id,
        body=result.text,
    )
    if reply is not None:
        await _send_bot_reply(reply, redis=ctx.get("redis"))
