"""The HTTP edges of an attachment: reading a body that may carry ONE file, streaming one back.

Shared by the two routes that take a file (`api/internal.py::brain_message_inbound`,
`api/hub/conversations.py::send_message`) and the two that serve one, and written for one
rule above all: nothing is read before the caller is authenticated. A route that DECLARES
its body (a Pydantic model, an `UploadFile`) has FastAPI read and parse the whole request -
for a file, spool it to disk - before any dependency runs, the API key and the hub token
included. These routes declare no body: they call `read_json_body` / `read_attachment_form`
from inside the handler, after auth - the way brain-api's patient route does
(brain-api/docs/CHECKPOINT_brain_message_anexos.md §6.1). JSON bodies are judged exactly as
FastAPI judged them while the routes still declared them, so a text-only caller sees the
same 422s it always did.
"""

from __future__ import annotations

import json

import anyio
from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse
from starlette.types import Message as ASGIMessage, Receive, Scope, Send

from secretaria.core import attachments
from secretaria.services import media_storage

#: A JSON message body. Far above any real one (a WhatsApp text is 4096 characters); it
#: only stops an unbounded read.
JSON_BODY_LIMIT = 1024 * 1024
_MAX_FORM_FIELDS = 16
#: Each TEXT part of a multipart body (the file part is bounded by the whole-body cap).
_MAX_TEXT_PART = 32 * 1024
_BODY_UNREADABLE = "There was an error parsing the body"


class _BodyTooLarge(MultiPartException):
    """Raised from inside the body stream when a request outgrows its cap.

    A `MultiPartException` on purpose: Starlette's parser closes - and so deletes - every
    part it spooled when one escapes it, so an aborted upload leaves nothing behind.
    """


class CappedReceive:
    """An ASGI `receive` that refuses to deliver more than `limit` body bytes.

    Content-Length is checked before a byte is read; a chunked body has none, and this is
    what bounds that case, while it streams and before the parser spools any of it.
    """

    def __init__(self, receive: Receive, limit: int) -> None:
        self._receive = receive
        self._limit = limit
        self._seen = 0
        self.exceeded = False

    async def __call__(self) -> ASGIMessage:
        message = await self._receive()
        if message["type"] == "http.request":
            self._seen += len(message.get("body", b""))
            if self._seen > self._limit:
                self.exceeded = True
                raise _BodyTooLarge("request body over the cap")
        return message


def media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def is_multipart(request: Request) -> bool:
    return media_type(request) == "multipart/form-data"


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    return int(raw) if raw is not None and raw.isdigit() else None


def body_errors(exc: ValidationError) -> RequestValidationError:
    """Pydantic's errors placed under `body`, as FastAPI reports those of a declared body."""
    return RequestValidationError(
        [{**error, "loc": ("body", *error["loc"])} for error in exc.errors(include_url=False)]
    )


def _missing_body() -> RequestValidationError:
    return RequestValidationError(
        [{"type": "missing", "loc": ("body",), "msg": "Field required", "input": None}]
    )


def _not_an_object(value: object) -> RequestValidationError:
    return RequestValidationError(
        [
            {
                "type": "model_attributes_type",
                "loc": ("body",),
                "msg": "Input should be a valid dictionary or object to extract fields from",
                "input": value,
            }
        ]
    )


async def read_json_body[M: BaseModel](request: Request, model: type[M]) -> M:
    """The JSON body validated into `model`, with FastAPI's own 422s for every failure."""
    declared = _declared_length(request)
    receive = CappedReceive(request.receive, JSON_BODY_LIMIT)
    try:
        if declared is not None and declared > JSON_BODY_LIMIT:
            raise _BodyTooLarge("declared body over the cap")
        raw = await Request(request.scope, receive).body()
    except _BodyTooLarge:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "body_too_large") from None
    except ClientDisconnect:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _BODY_UNREADABLE) from None
    if not raw:
        raise _missing_body()
    media = media_type(request)
    is_json = media == "application/json" or (
        media.startswith("application/") and media.endswith("+json")
    )
    if not is_json:
        raise _not_an_object(raw.decode("utf-8", errors="replace"))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RequestValidationError(
            [
                {
                    "type": "json_invalid",
                    "loc": ("body", exc.pos),
                    "msg": "JSON decode error",
                    "input": {},
                    "ctx": {"error": exc.msg},
                }
            ]
        ) from None
    except (ValueError, RecursionError):
        # Not Unicode, a number past Python's digit limit, nesting past the recursion
        # limit: FastAPI answers each with this 400, never a 500.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _BODY_UNREADABLE) from None
    if data is None:
        raise _missing_body()
    if not isinstance(data, dict):
        raise _not_an_object(data)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise body_errors(exc) from None


async def read_attachment_form[M: BaseModel](
    request: Request, fields_model: type[M]
) -> tuple[M, UploadFile, FormData]:
    """The ONE file part (`file`) and the text fields, the latter validated into `fields_model`.

    The caller owns the returned `FormData` and must `await form.close()` in a `finally`
    (that deletes the spooled file). Raises `AttachmentRefused` for anything wrong with the
    body as a whole, and FastAPI's 422 for a bad text field.
    """
    cap = attachments.MAX_ATTACHMENT_BYTES + attachments.MULTIPART_OVERHEAD_BYTES
    declared = _declared_length(request)
    if declared is not None and declared > cap:
        raise attachments.AttachmentRefused(attachments.ATTACHMENT_TOO_LARGE)
    receive = CappedReceive(request.receive, cap)
    try:
        form = await Request(request.scope, receive).form(
            max_files=1, max_fields=_MAX_FORM_FIELDS, max_part_size=_MAX_TEXT_PART
        )
    except StarletteHTTPException:
        # Starlette answers every parser refusal with a 400 naming its own limits; the
        # caller gets ours - 413 for the size cap, one "malformed" for the rest.
        code = (
            attachments.ATTACHMENT_TOO_LARGE
            if receive.exceeded
            else attachments.ATTACHMENT_MALFORMED
        )
        raise attachments.AttachmentRefused(code) from None
    except (ValueError, ClientDisconnect):
        raise attachments.AttachmentRefused(attachments.ATTACHMENT_MALFORMED) from None
    try:
        items = form.multi_items()
        uploads = [(key, value) for key, value in items if isinstance(value, UploadFile)]
        if len(uploads) != 1 or uploads[0][0] != "file":
            raise attachments.AttachmentRefused(attachments.ATTACHMENT_MALFORMED)
        texts = [(key, value) for key, value in items if isinstance(value, str)]
        if len({key for key, _ in texts}) != len(texts):  # the same field twice
            raise attachments.AttachmentRefused(attachments.ATTACHMENT_MALFORMED)
        try:
            fields = fields_model.model_validate({k: v for k, v in texts if v != ""})
        except ValidationError as exc:
            raise body_errors(exc) from None
    except BaseException:
        await form.close()
        raise
    return fields, uploads[0][1], form


async def checked_upload(upload: UploadFile) -> attachments.CheckedAttachment:
    """The upload's REAL kind (first bytes), real size and a safe name - or a refusal."""
    head = await upload.read(attachments.SNIFF_BYTES)
    await upload.seek(0)
    return attachments.check_attachment(head, upload.size or 0, upload.filename)


def refusal(code: str) -> HTTPException:
    refused = attachments.AttachmentRefused(code)
    return HTTPException(refused.status_code, refused.detail)


class _MediaResponse(StreamingResponse):
    """A StreamingResponse that ALWAYS releases the storage connection, however it ends.

    Starlette runs `background` only after a clean finish: when the client drops mid-
    download under ASGI spec >= 2.4, `StreamingResponse.__call__` raises
    `ClientDisconnect` before reaching it, and the abandoned body generator would leave
    the upstream connection to the garbage collector. The close is idempotent (the
    generator's own `finally` also calls it after the last chunk) and shielded, so a
    cancelled request still runs it to the end.
    """

    def __init__(
        self, media: media_storage.MediaStream, *, media_type: str, headers: dict[str, str]
    ) -> None:
        super().__init__(media.chunks, media_type=media_type, headers=headers)
        self._release = media.aclose

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._release()


async def stream_attachment(record: attachments.StoredAttachment | None) -> StreamingResponse:
    """The file's bytes, typed by what the ROW says (one of the five), hardened headers.

    404 for no record and for an object the bucket lost - one answer for every "no"; 503
    for storage trouble. Never a redirect, never a URL.
    """
    if record is None:
        raise refusal(attachments.ATTACHMENT_NOT_FOUND)
    try:
        media = await media_storage.open_object(record.r2_object_key)
    except media_storage.MediaObjectMissing:
        raise refusal(attachments.ATTACHMENT_NOT_FOUND) from None
    except media_storage.MediaStorageUnavailable:
        raise refusal(attachments.ATTACHMENT_STORAGE_UNAVAILABLE) from None
    headers = {
        **attachments.MEDIA_RESPONSE_HEADERS,
        "Content-Disposition": attachments.content_disposition(record.content_type),
    }
    if media.content_length is not None:
        headers["Content-Length"] = str(media.content_length)
    return _MediaResponse(media, media_type=record.content_type, headers=headers)
