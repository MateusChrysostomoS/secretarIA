"""Delete a clinic's patient trail; preserve agenda, money and clinic configuration.

R2 deletions are idempotent and precede DB deletes: a failure leaves the keys
available for retry. Google and Asaas records are intentionally not mutated.
"""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import (
    AnalyticsEvent,
    Appointment,
    ConsentEvent,
    Conversation,
    Message,
    Patient,
    Tenant,
)
from secretaria.models.booking_hold import BookingHold
from secretaria.models.conversation_pii_token_map import ConversationPiiTokenMap
from secretaria.models.pix_deposit import PixDeposit
from secretaria.models.rebooking_decline import RebookingDecline
from secretaria.schemas.tenant_patient_cleanup import CleanupRequest, CleanupResult
from secretaria.services import media_storage

logger = get_logger(__name__)
_WARNINGS = [
    "appointments_preserved_and_locally_anonymized",
    "payments_preserved",
    "google_calendar_and_asaas_unchanged",
]


async def cleanup_patients(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    payload: CleanupRequest | None = None,
) -> CleanupResult:
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    if payload is not None:
        if not payload.confirm:
            raise HTTPException(400, "Set confirm: true to proceed.")
        stmt = stmt.with_for_update()
    tenant = await session.scalar(stmt)
    if tenant is None:
        return CleanupResult(status="not_provisioned")
    if payload is not None and payload.clinic_name != tenant.clinic_name:
        raise HTTPException(409, "clinic_name_mismatch")

    patients = select(Patient.id).where(Patient.tenant_id == tenant_id)
    conversations = select(Conversation.id).where(Conversation.tenant_id == tenant_id)
    foreign_scopes = {
        Conversation: Conversation.patient_id.in_(patients),
        Appointment: or_(
            Appointment.patient_id.in_(patients), Appointment.conversation_id.in_(conversations)
        ),
        PixDeposit: PixDeposit.patient_id.in_(patients),
        BookingHold: or_(
            BookingHold.patient_id.in_(patients), BookingHold.conversation_id.in_(conversations)
        ),
    }
    for model, scope in foreign_scopes.items():
        if await session.scalar(
            select(func.count())
            .select_from(model)
            .where(
                model.tenant_id != tenant_id,
                scope,
            )
        ):
            return CleanupResult(status="blocked", blockers=["cross_clinic_reference"])
    appt_scope = (Appointment.tenant_id == tenant_id) & or_(
        Appointment.patient_id.is_not(None),
        Appointment.conversation_id.is_not(None),
        Appointment.phone.is_not(None),
        Appointment.attendee_name.is_not(None),
    )
    scopes = {
        Patient: Patient.tenant_id == tenant_id,
        Conversation: Conversation.tenant_id == tenant_id,
        Message: Message.conversation_id.in_(conversations),
        ConversationPiiTokenMap: ConversationPiiTokenMap.conversation_id.in_(conversations),
        ConsentEvent: ConsentEvent.tenant_id == tenant_id,
        BookingHold: BookingHold.tenant_id == tenant_id,
        RebookingDecline: RebookingDecline.tenant_id == tenant_id,
    }
    counts = {
        model.__tablename__: int(
            await session.scalar(
                select(func.count()).select_from(model).where(scope),
            )
            or 0
        )
        for model, scope in scopes.items()
    }
    counts["appointments_anonymized"] = int(
        await session.scalar(
            select(func.count()).select_from(Appointment).where(appt_scope),
        )
        or 0
    )
    deposit_scope = (PixDeposit.tenant_id == tenant_id) & PixDeposit.patient_id.in_(patients)
    counts["deposits_detached"] = int(
        await session.scalar(
            select(func.count()).select_from(PixDeposit).where(deposit_scope),
        )
        or 0
    )
    attachments = (
        await session.execute(
            select(Message.attachment, Conversation.patient_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.tenant_id == tenant_id, Message.attachment.is_not(None)),
        )
    ).all()
    keys = set()
    blockers = []
    for attachment, patient_id in attachments:
        key = attachment.get("r2_object_key") if isinstance(attachment, dict) else None
        prefix = media_storage.object_key_prefix(tenant_id, patient_id)
        if not isinstance(key, str) or not key.startswith(prefix) or ".." in key.split("/"):
            blockers.append("attachment_scope_invalid")
        else:
            keys.add(key)
    counts["attachment_objects"] = len(keys)
    if keys and not media_storage.is_configured():
        blockers.append("attachment_storage_unconfigured")
    if blockers or payload is None:
        return CleanupResult(
            status="blocked" if blockers else "ready",
            counts=counts,
            blockers=sorted(set(blockers)),
            warnings=_WARNINGS,
        )

    try:
        for key in keys:
            await media_storage.delete_object_required(key)
        await session.execute(update(PixDeposit).where(deposit_scope).values(patient_id=None))
        await session.execute(
            update(Appointment)
            .where(appt_scope)
            .values(
                patient_id=None,
                conversation_id=None,
                phone=None,
                attendee_name=None,
            )
        )
        # Explicit order also works with SQLite and without database cascades.
        for model in (
            BookingHold,
            RebookingDecline,
            ConversationPiiTokenMap,
            Message,
            ConsentEvent,
            Conversation,
            Patient,
        ):
            await session.execute(delete(model).where(scopes[model]))
        session.add(
            AnalyticsEvent(
                tenant_id=tenant_id,
                event_type="patients_cleaned",
                payload={"counts": counts},
            )
        )
        await session.commit()
    except media_storage.MediaStorageUnavailable:
        await session.rollback()
        return CleanupResult(
            status="failed", blockers=["attachment_delete_failed"], warnings=_WARNINGS
        )
    except SQLAlchemyError:
        await session.rollback()
        return CleanupResult(
            status="failed", blockers=["database_delete_failed_retry_required"], warnings=_WARNINGS
        )
    logger.warning("tenant_patients_cleaned", tenant_id=str(tenant_id), counts=counts)
    return CleanupResult(status="completed", counts=counts, warnings=_WARNINGS)
