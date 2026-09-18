"""Object storage for Brain-Message attachments: Cloudflare R2, through its S3 API.

secretarIA's OWN copy of the pattern PreCheck runs in production
(`PreCheck/app/services/r2.py`): a boto3 client built lazily on first use against
`https://{account}.r2.cloudflarestorage.com`, SigV4, path-style addressing, and
short-lived presigned GETs. A copy, never a call into PreCheck or its bucket:
PreCheck's storage sits on its WhatsApp production path (PreCheck/CLAUDE.md), and a
clinic can buy secretarIA without PreCheck - one product's files must not live or
die with the other's storage. Own bucket, own credentials, own variable names
(`ATTACHMENTS_R2_*` in config.py), even if one provider account ever backs both.

When PreCheck's adaptation lands (a second consumer), this client and the type
sniffing in core/attachments.py are the candidates for a shared package - the
`brain-shared-python-library` pattern. Not before: one consumer is not a library.

Only the API process calls this. Uploads (a patient's, through
`POST /internal/brain-message/inbound`; staff's, through the console) and downloads
(the two media routes) all happen in `secretaria_api`. The arq worker only writes the
`messages` row it is handed and never needs these credentials.

Two deliberate departures from the PreCheck original:
  * boto3 is synchronous, so every call runs in a worker thread - on the event loop,
    one slow upload would stall every other request of the process.
  * a presigned URL never leaves this process. A download is presigned locally (no
    network), fetched here with httpx and streamed to the caller as bytes; its TTL only
    has to outlive the start of that one fetch. Callers get bytes, never a storage URL.

Nothing here logs a file name, a byte or a credential.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, BinaryIO
from uuid import UUID, uuid4

import anyio
import anyio.to_thread
import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)

#: Every object lives under this prefix, then the clinic, then the patient, so an erasure
#: or a lifecycle rule can target one patient's files by prefix.
KEY_PREFIX = "brain-message"
_STREAM_CHUNK_BYTES = 64 * 1024
# Connect fast, read patiently: one object of at most 20 MiB.
_FETCH_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class MediaStorageUnavailable(Exception):
    """Storage is unconfigured, unreachable or refused - transient from the caller's side."""


class MediaObjectMissing(Exception):
    """A row points at an object the bucket does not have: answered like any unknown file."""


_client: Any = None
_client_lock = threading.Lock()


def is_configured() -> bool:
    settings = get_settings()
    return all(
        (
            settings.ATTACHMENTS_R2_ACCOUNT_ID,
            settings.ATTACHMENTS_R2_ACCESS_KEY_ID,
            settings.ATTACHMENTS_R2_SECRET_ACCESS_KEY,
            settings.ATTACHMENTS_R2_BUCKET,
        )
    )


def _r2() -> Any:
    """The boto3 client, built on first use: a missing credential fails the feature, not
    the import - and fails it CLOSED (nothing stored, 503 to the caller)."""
    global _client
    if not is_configured():
        logger.error("media_storage_unconfigured")
        raise MediaStorageUnavailable("attachment storage is not configured")
    with _client_lock:
        if _client is None:
            settings = get_settings()
            _client = boto3.client(
                "s3",
                endpoint_url=(
                    f"https://{settings.ATTACHMENTS_R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
                ),
                aws_access_key_id=settings.ATTACHMENTS_R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.ATTACHMENTS_R2_SECRET_ACCESS_KEY,
                config=Config(
                    signature_version="s3v4",
                    region_name="auto",
                    s3={"addressing_style": "path"},
                    # botocore >= 1.36 adds CRC checksums (and aws-chunked trailers for
                    # streamed bodies) to every upload by default; S3-compatible stores are
                    # the documented casualty. Only when an operation REQUIRES one.
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                    connect_timeout=5,
                    read_timeout=60,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        return _client


def _bucket() -> str:
    return get_settings().ATTACHMENTS_R2_BUCKET


def object_key_prefix(tenant_id: UUID, patient_id: UUID) -> str:
    """Where one patient's files live: `brain-message/<tenant>/<patient>/`."""
    return f"{KEY_PREFIX}/{tenant_id}/{patient_id}/"


def new_object_key(prefix: str) -> str:
    """A fresh random key under `prefix` - never the file's name, which may carry PII."""
    return f"{prefix}{uuid4().hex}"


async def put_object(key: str, body: BinaryIO, content_type: str) -> None:
    """Upload `body` (a seekable file, read from the start) under `key`.

    Raises `MediaStorageUnavailable` on any failure; nothing half-written is referenced,
    because the caller writes the `messages` row only after this returns.
    """

    def _put() -> None:
        body.seek(0)
        _r2().put_object(Bucket=_bucket(), Key=key, Body=body, ContentType=content_type)

    try:
        await anyio.to_thread.run_sync(_put)
    except (BotoCoreError, ClientError) as exc:
        logger.error("media_storage_put_failed", error=type(exc).__name__)
        raise MediaStorageUnavailable("upload failed") from exc


async def delete_object(key: str) -> None:
    """Best effort, never raises: the cleanup of an upload whose message was never written."""

    def _delete() -> None:
        _r2().delete_object(Bucket=_bucket(), Key=key)

    try:
        await anyio.to_thread.run_sync(_delete)
    except (MediaStorageUnavailable, BotoCoreError, ClientError) as exc:
        logger.warning("media_storage_orphan_left", error=type(exc).__name__)


def _presigned_get(key: str) -> str:
    """Signed locally - no network call. Used by `open_object` only, never returned."""
    return _r2().generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": key},
        ExpiresIn=get_settings().ATTACHMENTS_R2_SIGNED_URL_TTL_SECONDS,
    )


@dataclass
class MediaStream:
    """An object being fetched: its bytes as they arrive, its length if known, a closer."""

    chunks: AsyncIterator[bytes]
    content_length: int | None
    aclose: Callable[[], Awaitable[None]]


async def open_object(
    key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> MediaStream:
    """Start fetching `key`. Raises `MediaObjectMissing` (404) or `MediaStorageUnavailable`.

    The stream closes itself when fully read; `aclose` is for a consumer that stops early
    (the media routes call it on every exit - `api/attachment_http.py::_MediaResponse` -
    so a dropped download frees the connection too). `transport` exists for tests only.
    """
    try:
        url = await anyio.to_thread.run_sync(_presigned_get, key)
    except (BotoCoreError, ClientError) as exc:
        logger.error("media_storage_presign_failed", error=type(exc).__name__)
        raise MediaStorageUnavailable("presign failed") from exc

    client = httpx.AsyncClient(timeout=_FETCH_TIMEOUT, transport=transport)
    try:
        response = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.error("media_storage_get_failed", error=type(exc).__name__)
        raise MediaStorageUnavailable("fetch failed") from exc
    except BaseException:
        # Cancelled (or anything else) while the GET was in flight: no stream is handed
        # back, so nothing else could ever close this client. Shielded, because inside a
        # cancelled anyio scope the close itself would be cancelled.
        with anyio.CancelScope(shield=True):
            await client.aclose()
        raise

    closed = False

    async def aclose() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        with anyio.CancelScope(shield=True):
            await response.aclose()
            await client.aclose()

    if response.status_code == 404:
        await aclose()
        raise MediaObjectMissing("object not in bucket")
    if response.status_code != 200:
        await aclose()
        logger.error("media_storage_get_refused", status=response.status_code)
        raise MediaStorageUnavailable("fetch refused")

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes(_STREAM_CHUNK_BYTES):
                yield chunk
        finally:
            await aclose()

    raw_length = response.headers.get("content-length")
    return MediaStream(
        chunks=chunks(),
        content_length=int(raw_length) if raw_length and raw_length.isdigit() else None,
        aclose=aclose,
    )
