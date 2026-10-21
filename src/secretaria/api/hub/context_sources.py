"""Hub routes for context sources (TASK-026): material the manager gives the secretary to learn.

POST   /tenants/me/context-sources        - a URL or pasted text (202; a job reads the rest)
POST   /tenants/me/context-sources/file   - a PDF / DOCX / TXT (text read here, file dropped)
GET    /tenants/me/context-sources        - the 20 most recent
GET    /tenants/me/context-sources/{id}   - one, with the proposal once it is done
DELETE /tenants/me/context-sources/{id}

There is deliberately NO "apply" route (SPEC G1): the model only proposes, and nothing reaches the
clinic's data except the Save the form already has. Every access is filtered by the token's tenant.
Nothing here fetches a URL: `validate_url` only judges it; the worker fetches through `safe_fetch`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from secretaria.api.attachment_http import CappedReceive, read_json_body
from secretaria.api.hub.deps import get_current_tenant
from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.context_source import (
    IN_FLIGHT,
    KIND_FILE,
    KIND_TEXT,
    KIND_URL,
    ContextSource,
)
from secretaria.services import text_extract
from secretaria.services.safe_fetch import UnsafeUrlError, validate_url
from secretaria.services.text_extract import MIN_USEFUL_CHARS, ExtractionError

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/context-sources", tags=["hub-context-sources"])

# Parsing is CPU shared with every other route (the WhatsApp webhook among them): a burst of
# uploads queues here instead of starving the API.
_PARSE_SLOTS = asyncio.Semaphore(2)
# One clinic's count-then-insert is a single critical section. The in-process lock covers this
# process; the row lock below covers a second API replica.
_tenant_locks: dict[UUID, asyncio.Lock] = {}

_DAY = timedelta(hours=24)
# A row still "in flight" after this long belongs to a worker that died: it stops counting.
_IN_FLIGHT_STALE = timedelta(minutes=15)
_LIST_LIMIT = 20
_FORM_OVERHEAD_BYTES = 64 * 1024
_MAX_TEXT_PART = 32 * 1024
_BAD_URL = "Esse endereço não pode ser lido. Use o endereço público do site, começando com https://"
_URL_MESSAGES = {
    "private_address": _BAD_URL,
    "no_dot": "Esse endereço não parece um site público.",
    "scheme": "Use um endereço que comece com http:// ou https://.",
    "credentials": "Retire usuário e senha do endereço.",
    "port": "Esse endereço usa uma porta que não é aceita.",
    "invalid_url": "Endereço inválido.",
}
_FILE_MESSAGES = {
    "too_large": "O arquivo é grande demais (limite de 10 MB).",
    "unsupported_type": "Envie um PDF, DOCX ou TXT.",
    "empty": "O arquivo está vazio.",
    "unreadable": "Não conseguimos ler esse arquivo.",
    "encrypted": "O PDF tem senha. Retire a senha e envie de novo.",
    "no_text": "Não encontramos texto aí (pode ser um PDF escaneado). Cole o texto manualmente.",
}


class SourceCreate(BaseModel):
    kind: Literal["url", "text"]
    url: str | None = Field(default=None, max_length=2048)
    text: str | None = Field(default=None, max_length=text_extract.MAX_TEXT_CHARS)

    @model_validator(mode="after")
    def _shape(self) -> SourceCreate:
        if self.kind == "url" and not (self.url or "").strip():
            raise ValueError("url required")
        if self.kind == "text" and len((self.text or "").strip()) < MIN_USEFUL_CHARS:
            raise ValueError("text too short")
        return self


def _detail(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _item(source: ContextSource) -> dict:
    return {
        "id": str(source.id),
        "kind": source.kind,
        "label": source.label,
        "status": source.status,
        "error_code": source.error_code,
        "created_at": source.created_at.isoformat() if source.created_at else None,
        "has_proposal": source.proposal is not None,
    }


async def _enforce_quota(session: AsyncSession, tenant_id: UUID) -> None:
    s = get_settings()
    if s.CONTEXT_SOURCES_PER_DAY > 0:
        used = await session.scalar(
            select(func.count())
            .select_from(ContextSource)
            .where(
                ContextSource.tenant_id == tenant_id,
                ContextSource.created_at >= datetime.now(UTC) - _DAY,
            )
        )
        if (used or 0) >= s.CONTEXT_SOURCES_PER_DAY:
            raise HTTPException(429, _detail("source_quota", "Limite de fontes por dia atingido."))
    if s.CONTEXT_SOURCES_IN_FLIGHT > 0:
        running = await session.scalar(
            select(func.count())
            .select_from(ContextSource)
            .where(
                ContextSource.tenant_id == tenant_id,
                ContextSource.status.in_(IN_FLIGHT),
                ContextSource.created_at >= datetime.now(UTC) - _IN_FLIGHT_STALE,
            )
        )
        if (running or 0) >= s.CONTEXT_SOURCES_IN_FLIGHT:
            raise HTTPException(
                429, _detail("source_quota", "Aguarde as fontes em análise terminarem.")
            )


def _arq_pool_or_503(request: Request):
    """Same contract as api/internal.py: no queue means the caller learns it was NOT accepted."""
    pool = getattr(request.app.state, "arq_pool", None)
    if pool is None:
        raise HTTPException(503, _detail("queue_unavailable", "Fila indisponível."))
    return pool


async def _create(
    session: AsyncSession,
    request: Request,
    tenant: Tenant,
    *,
    kind: str,
    label: str,
    url: str | None = None,
    text: str | None = None,
) -> dict:
    pool = _arq_pool_or_503(request)  # BEFORE writing: no queue, no row
    source = ContextSource(tenant_id=tenant.id, kind=kind, label=label, url=url, source_text=text)
    async with _tenant_locks.setdefault(tenant.id, asyncio.Lock()):
        # Held from the count until the row is committed: racing requests see each other's rows.
        await session.execute(select(Tenant.id).where(Tenant.id == tenant.id).with_for_update())
        await _enforce_quota(session, tenant.id)
        session.add(source)
        await session.commit()
    try:
        await pool.enqueue_job("ingest_context_source", str(source.id))
    except Exception as exc:  # noqa: BLE001 - whatever the queue raised, the source was NOT accepted
        # A row nobody will ever process would count against the in-flight cap for good.
        await session.execute(delete(ContextSource).where(ContextSource.id == source.id))
        await session.commit()
        logger.error("context_source_enqueue_failed", error_type=type(exc).__name__)
        raise HTTPException(503, _detail("queue_unavailable", "Fila indisponível.")) from None
    logger.info("context_source_created", source_id=str(source.id), kind=kind)
    return {"id": str(source.id), "status": "pending"}


def _file_error(code: str) -> HTTPException:
    http = status.HTTP_413_CONTENT_TOO_LARGE if code == "too_large" else 422
    message = _FILE_MESSAGES.get(code, _FILE_MESSAGES["unreadable"])
    return HTTPException(http, _detail(code, message))


def _label_from_filename(name: str | None) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (base or "Documento")[:255]


async def _read_upload(request: Request) -> tuple[bytes, str]:
    """The ONE `file` part's bytes and name, read after authentication and under a hard cap."""
    cap = text_extract.MAX_UPLOAD_BYTES + _FORM_OVERHEAD_BYTES
    raw_length = request.headers.get("content-length", "")
    if raw_length.isdigit() and int(raw_length) > cap:
        raise _file_error("too_large")
    receive = CappedReceive(request.receive, cap)
    try:
        form = await Request(request.scope, receive).form(
            max_files=1, max_fields=4, max_part_size=_MAX_TEXT_PART
        )
    except StarletteHTTPException:
        raise _file_error("too_large" if receive.exceeded else "unreadable") from None
    except (ValueError, ClientDisconnect):
        raise _file_error("unreadable") from None
    try:
        uploads = [(k, v) for k, v in form.multi_items() if isinstance(v, UploadFile)]
        if len(uploads) != 1 or uploads[0][0] != "file":
            raise _file_error("unreadable")
        upload = uploads[0][1]
        data = await upload.read(text_extract.MAX_UPLOAD_BYTES + 1)
        return data, _label_from_filename(upload.filename)
    finally:
        await form.close()


async def _get_source(session: AsyncSession, tenant: Tenant, source_id: str) -> ContextSource:
    try:
        sid = UUID(source_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found") from None
    source = await session.scalar(
        select(ContextSource).where(
            ContextSource.id == sid,
            ContextSource.tenant_id == tenant.id,
            ContextSource.deleted_at.is_(None),
        )
    )
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source not found")
    return source


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_source(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    body = await read_json_body(request, SourceCreate)  # after auth: this route declares no body
    if body.kind == KIND_URL:
        try:
            target = validate_url(body.url or "")
        except UnsafeUrlError as exc:
            message = _URL_MESSAGES.get(exc.code, _URL_MESSAGES["invalid_url"])
            raise HTTPException(422, _detail(exc.code, message)) from None
        return await _create(
            session, request, tenant, kind=KIND_URL, label=target.host.lower(), url=body.url.strip()
        )
    return await _create(
        session, request, tenant, kind=KIND_TEXT, label="Texto colado", text=body.text
    )


@router.post("/file", status_code=status.HTTP_202_ACCEPTED)
async def create_source_from_file(
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _arq_pool_or_503(request)  # refuse before spending work on a file nobody can process
    await _enforce_quota(session, tenant.id)  # ...nor on one the clinic has no quota left for
    data, label = await _read_upload(request)
    try:
        # Pure-Python PDF/DOCX parsing takes seconds: never on the loop that serves the webhook.
        async with _PARSE_SLOTS:
            text = await run_in_threadpool(text_extract.extract_upload_text, data, label)
    except ExtractionError as exc:
        raise _file_error(exc.code) from None
    return await _create(session, request, tenant, kind=KIND_FILE, label=label, text=text)


@router.get("")
async def list_sources(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = await session.scalars(
        select(ContextSource)
        .where(ContextSource.tenant_id == tenant.id, ContextSource.deleted_at.is_(None))
        .order_by(ContextSource.created_at.desc())
        .limit(_LIST_LIMIT)
    )
    return {"items": [_item(row) for row in rows]}


@router.get("/{source_id}")
async def get_source(
    source_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    source = await _get_source(session, tenant, source_id)
    body = _item(source)
    if source.proposal is not None:
        body["proposal"] = source.proposal
    return body


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> Response:
    source = await _get_source(session, tenant, source_id)
    # Soft delete: the row stays as an empty counter so deleting never hands quota back, and a job
    # still running keeps its in-flight slot until it ends.
    await session.execute(
        update(ContextSource)
        .where(ContextSource.id == source.id)
        .values(
            deleted_at=datetime.now(UTC),
            proposal=None,
            source_text=None,
            url=None,
            label="Fonte removida",
        )
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
