"""LGPD privacy endpoints — internal service-to-service (`/internal/privacy/*`).

brain-api-owned contract (implemented exactly, per its already-live caller):
  GET    /internal/privacy/tenants/{tenant_id}/subjects/{wa_id}/export
  DELETE /internal/privacy/tenants/{tenant_id}/subjects/{wa_id}

Same `require_internal_api_key` gate as api/internal.py (imported, not
redefined) — every route here is equally internal-only, scoped to the path
tenant_id exactly like the rest of that router. See api/internal.py's module
docstring for the trust-boundary rationale; it applies unchanged here.

Export is an IDEMPOTENT read: an unknown wa_id for a valid tenant returns an
EMPTY bundle (patient=None, every list empty) — NEVER a 404. A "no data"
answer must look identical whether the tenant never talked to this patient
or whether erasure already ran against them.

Erase is a HARD DELETE for the conversational trail — messages, then their
conversations, then this subject's consent events, then the patient row
itself — but ANONYMIZES appointments instead of deleting them: an
appointment is the clinic's own operational/agenda record (what happened,
when, with whom on staff), and losing it would break the clinic's books, not
just the patient's data. "Anonymized" means `patient_id` set NULL (the FK is
already `ON DELETE SET NULL`, so deleting the patient row would eventually
achieve this anyway — it is done explicitly and FIRST here so the `phone`
column, which the FK cascade does NOT touch, is blanked too) and `phone`
cleared. The row itself, its dates/type/status, stays — and is counted under
the `"appointments"` key in the erasure result.

Analytics events are STATED, not filtered: they never carry a patient
reference at all (see models/analytics_event.py) — there is nothing to erase
there, so the erasure result never mentions them and the export bundle's
`analytics_events` list is always `[]`.

`processed_events` (the reminder idempotency ledger, plugins/reminders.py)
is also left untouched on purpose — its keys embed appointment ids, not
wa_ids, and carry no personal data.

Repeat erasure calls are idempotent: once the patient row is gone, the
lookup at the top of `erase_subject` finds nothing and every count is 0.
"""

import hashlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.internal import require_internal_api_key
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, ConsentEvent, Conversation, Message, Patient
from secretaria.schemas.privacy import (
    PrivacyAppointment,
    PrivacyConsentEvent,
    PrivacyConversation,
    PrivacyEraseResult,
    PrivacyExportBundle,
    PrivacyMessage,
    PrivacyPatient,
)

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal/privacy",
    tags=["internal", "internal-privacy"],
    dependencies=[Depends(require_internal_api_key)],
)

_TENANT_PATH = Annotated[UUID, Path(description="Tenant UUID — the only scope.")]
_WA_ID_PATH = Annotated[str, Path(description="The subject's WhatsApp id (wa_id).")]

_EMPTY_ERASE_COUNTS = {
    "patients": 0,
    "conversations": 0,
    "messages": 0,
    "appointments": 0,
    "consent_events": 0,
}

_INTERNAL_RESPONSES = {
    401: {"description": "Missing or invalid X-Internal-Api-Key."},
    403: {"description": "INTERNAL_API_KEY not configured on the server."},
}


def _hash_wa_id(wa_id: str) -> str:
    """SHA-256 hex digest of a wa_id — logged instead of the raw value, ever."""
    return hashlib.sha256(wa_id.encode("utf-8")).hexdigest()


@router.get(
    "/tenants/{tenant_id}/subjects/{wa_id}/export",
    response_model=PrivacyExportBundle,
    summary="LGPD data export for one subject (internal)",
    description=(
        "Everything secretaria holds for (tenant_id, wa_id): patient, appointments, "
        "conversations, messages, consent events. Unknown wa_id -> an EMPTY bundle, "
        "never 404 (idempotent read). Requires the X-Internal-Api-Key header."
    ),
    responses=_INTERNAL_RESPONSES,
)
async def export_subject(
    tenant_id: _TENANT_PATH,
    wa_id: _WA_ID_PATH,
    session: AsyncSession = Depends(get_session),
) -> PrivacyExportBundle:
    patient = await session.scalar(
        select(Patient).where(Patient.tenant_id == tenant_id, Patient.wa_id == wa_id)
    )
    if patient is None:
        logger.info("privacy_export_empty_bundle", tenant_id=str(tenant_id))
        return PrivacyExportBundle(
            tenant_id=tenant_id,
            wa_id=wa_id,
            patient=None,
            appointments=[],
            conversations=[],
            messages=[],
            consent_events=[],
            analytics_events=[],
        )

    appointments = (
        await session.scalars(
            select(Appointment).where(
                Appointment.tenant_id == tenant_id, Appointment.patient_id == patient.id
            )
        )
    ).all()
    conversations = (
        await session.scalars(
            select(Conversation).where(
                Conversation.tenant_id == tenant_id, Conversation.patient_id == patient.id
            )
        )
    ).all()
    conv_ids = [c.id for c in conversations]
    messages = []
    if conv_ids:
        messages = (
            await session.scalars(select(Message).where(Message.conversation_id.in_(conv_ids)))
        ).all()
    consent_events = (
        await session.scalars(
            select(ConsentEvent).where(
                ConsentEvent.tenant_id == tenant_id, ConsentEvent.wa_id == wa_id
            )
        )
    ).all()

    logger.info("privacy_export_bundle_built", tenant_id=str(tenant_id))
    return PrivacyExportBundle(
        tenant_id=tenant_id,
        wa_id=wa_id,
        patient=PrivacyPatient.model_validate(patient),
        appointments=[PrivacyAppointment.model_validate(a) for a in appointments],
        conversations=[PrivacyConversation.model_validate(c) for c in conversations],
        messages=[PrivacyMessage.model_validate(m) for m in messages],
        consent_events=[PrivacyConsentEvent.model_validate(e) for e in consent_events],
        analytics_events=[],
    )


@router.delete(
    "/tenants/{tenant_id}/subjects/{wa_id}",
    response_model=PrivacyEraseResult,
    summary="LGPD erasure for one subject (internal)",
    description=(
        "Hard-deletes messages, conversations and consent events for (tenant_id, wa_id), "
        "then the patient row. Appointments are ANONYMIZED (patient_id + phone cleared), "
        "not deleted — kept as the clinic's own agenda record. Idempotent: repeat -> zero "
        "counts. Requires the X-Internal-Api-Key header."
    ),
    responses=_INTERNAL_RESPONSES,
)
async def erase_subject(
    tenant_id: _TENANT_PATH,
    wa_id: _WA_ID_PATH,
    session: AsyncSession = Depends(get_session),
) -> PrivacyEraseResult:
    patient = await session.scalar(
        select(Patient).where(Patient.tenant_id == tenant_id, Patient.wa_id == wa_id)
    )
    if patient is None:
        # Idempotent: unknown or already-erased subject -> zero counts, no error.
        logger.warning(
            "privacy_erasure_executed",
            tenant_id=str(tenant_id),
            wa_id_sha256=_hash_wa_id(wa_id),
            counts=_EMPTY_ERASE_COUNTS,
        )
        return PrivacyEraseResult(erased=dict(_EMPTY_ERASE_COUNTS))

    counts = dict(_EMPTY_ERASE_COUNTS)

    # Appointments: ANONYMIZE, never delete — see module docstring.
    appt_result = await session.execute(
        update(Appointment)
        .where(Appointment.tenant_id == tenant_id, Appointment.patient_id == patient.id)
        .values(patient_id=None, phone=None)
    )
    counts["appointments"] = appt_result.rowcount or 0

    conv_ids = (
        await session.scalars(
            select(Conversation.id).where(
                Conversation.tenant_id == tenant_id, Conversation.patient_id == patient.id
            )
        )
    ).all()
    if conv_ids:
        msg_result = await session.execute(
            delete(Message).where(Message.conversation_id.in_(conv_ids))
        )
        counts["messages"] = msg_result.rowcount or 0
        conv_result = await session.execute(
            delete(Conversation).where(Conversation.id.in_(conv_ids))
        )
        counts["conversations"] = conv_result.rowcount or 0

    consent_result = await session.execute(
        delete(ConsentEvent).where(
            ConsentEvent.tenant_id == tenant_id, ConsentEvent.wa_id == wa_id
        )
    )
    counts["consent_events"] = consent_result.rowcount or 0

    await session.execute(delete(Patient).where(Patient.id == patient.id))
    counts["patients"] = 1

    await session.commit()

    logger.warning(
        "privacy_erasure_executed",
        tenant_id=str(tenant_id),
        wa_id_sha256=_hash_wa_id(wa_id),
        counts=counts,
    )
    return PrivacyEraseResult(erased=counts)
