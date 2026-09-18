"""Internal service-to-service API (`/internal/*`) — INTERNAL ONLY.

secretaria faces nothing public (see `auth-jwt-multitenant`): no browser, no human, no
JWT ever reaches it. This router is the ONLY inbound *data* surface for sibling Brain Co
services — today brain-api fetching a tenant's appointments and patients to render the
doctor portal. EVERY route is gated by `require_internal_api_key` at the router level, so
there is no anonymous access and no route to forget to protect.

The acting tenant is ALWAYS taken from the URL path and every query is scoped to it
(`WHERE tenant_id = :tenant_id`), so one caller can never read another tenant's rows. The
caller (brain-api) derives that path id from its own validated JWT, never from client
input — the internal network + shared key is the trust boundary, the path id is the scope.

This is a DIFFERENT mechanism from:
  * `services/precheck.py` — the OUTBOUND `X-Internal-Api-Key` we *send* to precheck.
  * `api/admin/panel.py` — the `X-Admin-Token` admin surface (a separate secret).
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    Security,
    status,
)
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.attachment_http import (
    checked_upload,
    is_multipart,
    read_attachment_form,
    read_json_body,
    stream_attachment,
)
from secretaria.config import get_settings
from secretaria.core import attachments
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Conversation, Message, MessageDirection, Patient
from secretaria.schemas.conversation import attachment_read_or_none, interactive_read_or_none
from secretaria.schemas.internal import (
    BrainMessageAck,
    BrainMessageInbound,
    BrainMessageInboundForm,
    BrainMessageMessage,
    BrainMessageMessageList,
    InternalAppointment,
    InternalAppointmentList,
    InternalPatient,
    InternalPatientList,
)
from secretaria.services import media_storage
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE

logger = get_logger(__name__)

# Declaring the scheme (instead of a bare Header dependency) makes Swagger render the
# "Authorize" lock on these routes; auto_error is off so we return our own status/message.
_internal_key_scheme = APIKeyHeader(
    name="X-Internal-Api-Key",
    auto_error=False,
    description="Service-to-service shared secret. Configure via the INTERNAL_API_KEY env var.",
)


def require_internal_api_key(
    key: Annotated[str | None, Security(_internal_key_scheme)] = None,
) -> None:
    """Gate `/internal/*` on the `X-Internal-Api-Key` shared secret. Fail CLOSED.

    Compared in constant time (`secrets.compare_digest`); the key is NEVER logged.

    Rejection codes are 401/403 (NOT the 503 that `api/admin/panel.py` uses for an
    unconfigured admin token): the internal contract returns 401/403 in every reject
    case, and returning the same family whether the server is unconfigured or the caller
    is wrong avoids advertising server-config state to an unauthenticated caller.

    Accepts INTERNAL_API_KEY_PREVIOUS as well as INTERNAL_API_KEY (any-of) so a key
    can be rotated without downtime: deploy the new value as INTERNAL_API_KEY, keep
    the old one as INTERNAL_API_KEY_PREVIOUS until every caller has switched, then
    drop it. We only ever SEND INTERNAL_API_KEY outbound (services/precheck.py and
    core/subscription.py never send the previous value).
    """
    settings = get_settings()
    expected = settings.INTERNAL_API_KEY
    if not expected:
        # No server-side key => the surface is locked, not "try again later".
        logger.warning("internal_auth_unconfigured")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Internal API not configured.",
        )
    previous = settings.INTERNAL_API_KEY_PREVIOUS
    is_current = bool(key) and secrets.compare_digest(key, expected)
    is_previous = bool(key) and bool(previous) and secrets.compare_digest(key, previous)
    if not (is_current or is_previous):
        # Never log `key` (the candidate secret) — only that auth failed.
        logger.warning("internal_auth_failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal API key.",
        )


router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_api_key)],
)

_DEFAULT_PAGE = 50
_MAX_PAGE = 200

_INTERNAL_RESPONSES = {
    401: {"description": "Missing or invalid X-Internal-Api-Key."},
    403: {"description": "INTERNAL_API_KEY not configured on the server."},
}


def _appointment_dto(row: Appointment) -> InternalAppointment:
    """Project an Appointment field-by-field (never `model_validate` — no Google ids)."""
    return InternalAppointment(
        id=row.id,
        patient_id=row.patient_id,
        appointment_type=row.appointment_type,
        start_at=row.start_at,
        end_at=row.end_at,
        status=row.status,
        phone=row.phone,
    )


def _patient_dto(row: Patient) -> InternalPatient:
    """Project a Patient field-by-field."""
    return InternalPatient(
        id=row.id,
        name=row.name,
        wa_id=row.wa_id,
        channel=row.channel,
        external_id=row.external_id,
        created_at=row.created_at,
    )


@router.get(
    "/tenants/{tenant_id}/appointments",
    response_model=InternalAppointmentList,
    summary="Appointments for a tenant (internal)",
    description=(
        "Returns the tenant's appointments (lean projection — no Google event ids). "
        "Scoped to the path tenant_id. Requires the X-Internal-Api-Key header."
    ),
    responses=_INTERNAL_RESPONSES,
)
async def list_tenant_appointments(
    tenant_id: Annotated[UUID, Path(description="Tenant UUID — the only scope.")],
    limit: Annotated[int, Query(ge=1, le=_MAX_PAGE)] = _DEFAULT_PAGE,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = Depends(get_session),
) -> InternalAppointmentList:
    stmt = (
        select(Appointment)
        .where(Appointment.tenant_id == tenant_id)
        # Dated appointments first (newest start), then undated block-slots; stable tiebreak.
        # `is_(None)` orders portably (False<True) without relying on NULLS LAST syntax.
        .order_by(
            Appointment.start_at.is_(None),
            Appointment.start_at.desc(),
            Appointment.created_at.desc(),
            Appointment.id,
        )
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    logger.info("internal_appointments_listed", tenant_id=str(tenant_id), count=len(rows))
    return InternalAppointmentList(data=[_appointment_dto(row) for row in rows])


@router.get(
    "/tenants/{tenant_id}/patients",
    response_model=InternalPatientList,
    summary="Patients for a tenant (internal)",
    description=(
        "Returns the tenant's patients. Scoped to the path tenant_id. "
        "Requires the X-Internal-Api-Key header."
    ),
    responses=_INTERNAL_RESPONSES,
)
async def list_tenant_patients(
    tenant_id: Annotated[UUID, Path(description="Tenant UUID — the only scope.")],
    limit: Annotated[int, Query(ge=1, le=_MAX_PAGE)] = _DEFAULT_PAGE,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = Depends(get_session),
) -> InternalPatientList:
    stmt = (
        select(Patient)
        .where(Patient.tenant_id == tenant_id)
        .order_by(Patient.created_at.desc(), Patient.id)
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    logger.info("internal_patients_listed", tenant_id=str(tenant_id), count=len(rows))
    return InternalPatientList(data=[_patient_dto(row) for row in rows])


# --------------------------------------------------------------------------
# Brain-Message channel
# --------------------------------------------------------------------------
#
# The second surface a patient can reach a clinic on, alongside WhatsApp. Both
# routes below are called by the brain-api switchboard, which owns the
# patient's end-user session and decides whether a message belongs to
# secretarIA, to PreCheck, or to both. That is why they sit behind the SAME
# `require_internal_api_key` as everything else here rather than growing a
# patient-facing auth of their own: the caller is a service, and the only
# end-user identity involved was validated one hop earlier (`auth-jwt-multitenant`).
#
# Scope discipline is unchanged, just spelled with a different key: every query
# is pinned to `tenant_id` AND the patient's `external_id` AND
# `channel == "brain_message"`, so a caller can no more read another tenant's
# conversation here than it can on the tenant routes above.


_INBOUND_FORM_SCHEMA: dict = {
    "type": "object",
    "required": ["tenant_id", "external_id", "file"],
    "properties": {
        "tenant_id": {"type": "string", "format": "uuid"},
        "external_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "text": {"type": "string", "description": "Optional caption."},
        "patient_name": {"type": "string", "maxLength": 255},
        "interactive_reply_id": {"type": "string"},
        "file": {
            "type": "string",
            "format": "binary",
            "description": "JPEG, PNG, WEBP, GIF or PDF (by content), 1 byte to 20 MiB.",
        },
    },
    "additionalProperties": False,
}

_QUOTA_WINDOW = timedelta(hours=24)


def _arq_pool_or_503(request: Request):
    """The queue, or 503: the caller must learn the message was NOT accepted."""
    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        logger.error("brain_message_arq_pool_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Queue unavailable.",
        )
    return arq_pool


async def _inbound_attachment_bytes(
    session: AsyncSession, *, tenant_id: UUID, since: datetime, patient_id: UUID | None = None
) -> int:
    """Bytes PATIENTS stored since `since`, for the clinic or for one of its patients.

    Read from the rows themselves - the quota is as persistent as the messages - through
    the partial index `ix_messages_attachment_created_at` (only rows with a file). Staff
    uploads (OUTBOUND) never count against a patient or the clinic.
    """
    stmt = (
        select(Message.id, Message.attachment)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.tenant_id == tenant_id,
            Message.direction == MessageDirection.INBOUND,
            Message.attachment.is_not(None),
            Message.created_at >= since,
        )
    )
    if patient_id is not None:
        stmt = stmt.where(Conversation.patient_id == patient_id)
    total = 0
    for message_id, blob in (await session.execute(stmt)).all():
        record = attachments.stored_attachment_or_none(blob, message_id=message_id)
        if record is not None:
            total += record.size_bytes
    return total


async def _enforce_attachment_quota(
    session: AsyncSession, *, tenant_id: UUID, patient_id: UUID, incoming_bytes: int
) -> None:
    """Refuse (429, permanent for today) a file that would pass a daily byte quota.

    brain-api counts uploads per minute, in memory, per process; this is the persisted
    ceiling its CHECKPOINT §4.1 makes an obligation of this side - per patient and, the
    one that binds (an unverified visitor's identity costs one call), per clinic. What is
    counted is what is PERSISTED: a file stored but whose job has not written its row yet
    is not, so concurrent uploads can overshoot by what is in flight - bounded by
    brain-api's per-minute limits and the seconds a job takes.
    """
    settings = get_settings()
    since = datetime.now(UTC) - _QUOTA_WINDOW
    for scope, limit, owner in (
        ("patient", settings.ATTACHMENT_DAILY_BYTES_PER_PATIENT, patient_id),
        ("tenant", settings.ATTACHMENT_DAILY_BYTES_PER_TENANT, None),
    ):
        if limit <= 0:
            continue
        used = await _inbound_attachment_bytes(
            session, tenant_id=tenant_id, since=since, patient_id=owner
        )
        if used + incoming_bytes > limit:
            logger.warning(
                "brain_message_attachment_quota_exceeded",
                tenant_id=str(tenant_id),
                scope=scope,
                used_bytes=used,
                limit_bytes=limit,
            )
            raise attachments.AttachmentRefused(attachments.ATTACHMENT_QUOTA_EXCEEDED)


async def _attachment_gate(
    session: AsyncSession, tenant_id: UUID, external_id: str, incoming_bytes: int
) -> UUID:
    """ONE short read decides whether this file may be stored. Returns the patient's id.

    Consent (LGPD): a file - possibly health data - is refused (409) until THIS patient
    accepted the terms (`Patient.lgpd_accepted_at`, set by the consent gate in
    workers/tasks.py). An unknown patient has accepted nothing either: brain-api opens
    uploads to visitors who never verified an e-mail (its CHECKPOINT §6.3), so a new
    visitor's first message can be text, never a file. Nothing is stored on refusal.
    """
    patient = await session.scalar(
        select(Patient).where(
            Patient.tenant_id == tenant_id,
            Patient.channel == CHANNEL_BRAIN_MESSAGE,
            Patient.external_id == external_id,
        )
    )
    if patient is None or patient.lgpd_accepted_at is None:
        raise attachments.AttachmentRefused(attachments.ATTACHMENT_CONSENT_REQUIRED)
    await _enforce_attachment_quota(
        session, tenant_id=tenant_id, patient_id=patient.id, incoming_bytes=incoming_bytes
    )
    return patient.id


async def _inbound_with_attachment(
    request: Request, session: AsyncSession, arq_pool
) -> BrainMessageAck:
    """The multipart branch: validate, gate, store, enqueue - in cost order.

    1. The body, read after auth and capped (413 before a byte past the ceiling is parsed).
    2. The file judged by its CONTENT, again - brain-api judged it too, and that is no
       reason to skip it here (defence in depth).
    3. The gate, in one short read (consent, then quota); the pooled connection goes back
       BEFORE the upload, never held through storage I/O.
    4. The bytes go to R2 HERE, in the API, not in the worker: the job carries a
       reference, never 20 MiB through Redis. If the enqueue then fails, the object is
       removed again.
    Every refusal is logged by CODE and clinic - never the file's name or a byte of it.
    """
    tenant_id: UUID | None = None
    try:
        fields, upload, form = await read_attachment_form(request, BrainMessageInboundForm)
        try:
            tenant_id = fields.tenant_id
            checked = await checked_upload(upload)
            patient_id = await _attachment_gate(
                session, fields.tenant_id, fields.external_id, checked.size_bytes
            )
            await session.close()
            key = media_storage.new_object_key(
                media_storage.object_key_prefix(fields.tenant_id, patient_id)
            )
            try:
                await media_storage.put_object(key, upload.file, checked.kind.content_type)
            except media_storage.MediaStorageUnavailable:
                raise attachments.AttachmentRefused(
                    attachments.ATTACHMENT_STORAGE_UNAVAILABLE
                ) from None
        finally:
            await form.close()
    except attachments.AttachmentRefused as refused:
        logger.info(
            "brain_message_attachment_refused",
            tenant_id=str(tenant_id) if tenant_id is not None else None,
            code=refused.code,
        )
        raise HTTPException(refused.status_code, refused.detail) from None

    try:
        await arq_pool.enqueue_job(
            "process_brain_message_inbound",
            str(fields.tenant_id),
            fields.external_id,
            text=fields.text,
            patient_name=fields.patient_name,
            interactive_reply_id=fields.interactive_reply_id,
            attachment=attachments.attachment_record(checked, key),
        )
    except Exception:
        await media_storage.delete_object(key)
        raise
    logger.info(
        "brain_message_inbound_queued",
        tenant_id=str(fields.tenant_id),
        external_id=fields.external_id,
        attachment_content_type=checked.kind.content_type,
        attachment_size_bytes=checked.size_bytes,
    )
    return BrainMessageAck(status="queued")


@router.post(
    "/brain-message/inbound",
    response_model=BrainMessageAck,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Deliver a Brain-Message patient message (internal)",
    description=(
        "Accepts one inbound message from a patient on the Brain-Message channel and "
        "queues it for the same agent pipeline WhatsApp uses. Returns 202 immediately; "
        "the reply appears on the messages endpoint. `application/json` carries text; "
        "`multipart/form-data` carries ONE file (part `file`: JPEG, PNG, WEBP, GIF or PDF "
        "by content, up to 20 MiB) plus the same fields as form fields, `text` becoming an "
        "optional caption. Refusals of a file are 4xx with "
        '`{"detail": {"code", "message"}}`. Requires the X-Internal-Api-Key header.'
    ),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": BrainMessageInbound.model_json_schema()},
                "multipart/form-data": {"schema": _INBOUND_FORM_SCHEMA},
            },
        }
    },
    responses={
        **_INTERNAL_RESPONSES,
        409: {"description": "A file before the patient accepted the LGPD terms."},
        413: {"description": "The file, or the whole body, is over the 20 MiB ceiling."},
        415: {"description": "The file's content is not one of the five accepted types."},
        422: {"description": "Malformed body, or a file name that contradicts its content."},
        429: {"description": "Daily attachment byte quota (patient or clinic) exhausted."},
        503: {"description": "Queue or attachment storage unavailable."},
    },
)
async def brain_message_inbound(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> BrainMessageAck:
    """Enqueue one Brain-Message turn. ACK FAST — no model call happens here.

    The `whatsapp-webhook-arq` golden rule, applied to a channel Meta has
    nothing to do with: an agent turn takes seconds, so it belongs on the arq
    worker and never inside a request. The temptation to call the pipeline
    inline is real — this handler already has everything it needs — and it is
    exactly the trap that rule exists to prevent, so the new channel gets no
    exception. `process_brain_message_inbound` is registered in
    `workers/arq_worker.py` alongside `process_webhook_event`.

    Fail-closed on a missing queue with 503: the caller must learn the message
    was NOT accepted. Answering 202 with nothing enqueued would silently drop a
    patient's message.

    Two encodings on one path (brain-api CHECKPOINT §4.1). Neither body is declared
    on the route: FastAPI would otherwise read - for a file, spool to disk - the whole
    request before `require_internal_api_key` runs (api/attachment_http.py). A text
    message enqueues exactly the job it always did (no `attachment` argument), so a
    worker that predates files still runs it.
    """
    if is_multipart(request):
        return await _inbound_with_attachment(request, session, _arq_pool_or_503(request))
    payload = await read_json_body(request, BrainMessageInbound)
    arq_pool = _arq_pool_or_503(request)
    await arq_pool.enqueue_job(
        "process_brain_message_inbound",
        str(payload.tenant_id),
        payload.external_id,
        text=payload.text,
        patient_name=payload.patient_name,
        dedupe_id=payload.dedupe_id,
        interactive_reply_id=payload.interactive_reply_id,
    )
    logger.info(
        "brain_message_inbound_queued",
        tenant_id=str(payload.tenant_id),
        # The external_id is the patient's identifier on this channel; logged
        # whole because — unlike a wa_id — it is an opaque session handle, not
        # a phone number, so it carries no personal data of its own.
        external_id=payload.external_id,
    )
    return BrainMessageAck(status="queued")


@router.get(
    "/brain-message/conversations/{external_id}/messages",
    response_model=BrainMessageMessageList,
    summary="Messages of one Brain-Message patient (internal)",
    description=(
        "Returns the message history of ONE patient on the Brain-Message channel, "
        "scoped to tenant_id + external_id. `since` returns only messages created "
        "strictly after that instant (poll cursor). Requires the X-Internal-Api-Key header."
    ),
    responses=_INTERNAL_RESPONSES,
)
async def list_brain_message_messages(
    external_id: Annotated[str, Path(description="The patient's id on the Brain-Message channel.")],
    tenant_id: Annotated[UUID, Query(description="Tenant UUID — REQUIRED; the outer scope.")],
    since: Annotated[
        datetime | None,
        Query(description="Return only messages created strictly after this instant."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_PAGE)] = _DEFAULT_PAGE,
    session: AsyncSession = Depends(get_session),
) -> BrainMessageMessageList:
    """One patient's messages. Both scopes are REQUIRED and both are enforced.

    `tenant_id` is a required query parameter rather than an optional filter on
    purpose: an `external_id` alone is a bearer-like handle, and making the
    tenant optional would turn a guessed id into a cross-tenant read. Requiring
    it means the caller must already know which clinic it is asking about, and
    422s if it does not.

    The scope chain is three links and every one of them is in the SQL: the
    patient is looked up by (tenant_id, channel="brain_message", external_id),
    the conversation by (tenant_id, that patient), and the messages by that
    conversation id. A WhatsApp patient who happens to share the string is not
    reachable here — the channel predicate excludes them.

    An unknown patient returns an empty list, not 404. This endpoint is polled;
    a patient who has been created but whose first turn has not committed yet is
    an ordinary state, not an error, and answering 404 would also confirm to a
    caller which external_ids do not exist.
    """
    patient = await session.scalar(
        select(Patient).where(
            Patient.tenant_id == tenant_id,
            Patient.channel == CHANNEL_BRAIN_MESSAGE,
            Patient.external_id == external_id,
        )
    )
    if patient is None:
        logger.info("brain_message_messages_unknown_patient", tenant_id=str(tenant_id))
        return BrainMessageMessageList(data=[])

    conversation_id = await session.scalar(
        select(Conversation.id).where(
            Conversation.tenant_id == tenant_id,
            Conversation.patient_id == patient.id,
        )
    )
    if conversation_id is None:
        return BrainMessageMessageList(data=[])

    stmt = select(Message).where(Message.conversation_id == conversation_id)
    if since is not None:
        stmt = stmt.where(Message.created_at > since)
    # Oldest first: this is a transcript, and the caller appends it to what it
    # already shows. `id` breaks ties between rows written in the same tick.
    stmt = stmt.order_by(Message.created_at, Message.id).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    logger.info(
        "brain_message_messages_listed",
        tenant_id=str(tenant_id),
        conversation_id=str(conversation_id),
        count=len(rows),
    )
    return BrainMessageMessageList(
        data=[
            BrainMessageMessage(
                id=row.id,
                direction=row.direction,
                sender=row.sender,
                body=row.body,
                created_at=row.created_at,
                interactive=interactive_read_or_none(row.interactive, message_id=row.id),
                interactive_reply_id=row.interactive_reply_id,
                attachment=attachment_read_or_none(row.attachment, message_id=row.id),
            )
            for row in rows
        ]
    )


@router.get(
    "/brain-message/media/{message_id}",
    response_class=Response,
    summary="The file one Brain-Message message carries (internal)",
    description=(
        "Streams the bytes of a message's file, typed by what was stored. The caller "
        "(brain-api) serves it to the patient; this route never returns a storage URL. "
        "Ownership is checked here, in one query, against tenant_id + external_id. "
        "Requires the X-Internal-Api-Key header."
    ),
    responses={
        **_INTERNAL_RESPONSES,
        200: {
            "description": "The file's bytes.",
            "content": {content_type: {} for content_type in attachments.ALLOWED_KINDS},
        },
        404: {"description": "No such file in this patient's conversation - one answer."},
        503: {"description": "Attachment storage unavailable (retryable)."},
    },
)
async def get_brain_message_media(
    message_id: Annotated[str, Path(description="The message's id (messages.id, a UUID).")],
    tenant_id: Annotated[UUID, Query(description="Tenant UUID — REQUIRED; the outer scope.")],
    external_id: Annotated[
        str,
        Query(min_length=1, max_length=64, description="The patient's Brain-Message id."),
    ],
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The file, only inside its owner's conversation (brain-api CHECKPOINT §4.3).

    THIS route is what stops patient B reading patient A's file - brain-api only
    guarantees that both query values come from the patient's session. So ownership is
    decided in ONE query by (message id, tenant, channel, external_id), never by id first
    and a comparison after. A message of another patient or clinic, one without a file,
    an unknown or malformed id: the same 404, confirming nothing.
    """
    try:
        message_uuid: UUID | None = UUID(message_id)
    except ValueError:
        message_uuid = None
    record = None
    if message_uuid is not None:
        row = (
            await session.execute(
                select(Message.id, Message.attachment)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .join(Patient, Patient.id == Conversation.patient_id)
                .where(
                    Message.id == message_uuid,
                    Conversation.tenant_id == tenant_id,
                    Patient.tenant_id == tenant_id,
                    Patient.channel == CHANNEL_BRAIN_MESSAGE,
                    Patient.external_id == external_id,
                    Message.attachment.is_not(None),
                )
            )
        ).first()
        if row is not None:
            record = attachments.stored_attachment_or_none(row.attachment, message_id=row.id)
    # Nothing to write: hand the pooled connection back before streaming.
    await session.close()
    if record is None:
        logger.info("brain_message_media_not_found", tenant_id=str(tenant_id))
    return await stream_attachment(record)
