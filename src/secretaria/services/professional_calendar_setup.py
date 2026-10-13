"""Prepare shared-account calendars after a professional registration is committed."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Professional, Tenant
from secretaria.services.tenant_config import ensure_professional_secondary_calendar

logger = get_logger(__name__)


async def prepare_shared_professional_calendar(
    session: AsyncSession, tenant: Tenant, professional: Professional
) -> None:
    """Best effort; keep registration and the existing explicit retry paths intact.

    Called only AFTER registration commits. Lock and reload the professional
    before checking its calendar, so concurrent callers reuse a committed id.
    Google success followed by a database failure still needs reconciliation;
    there is no distributed transaction with Google Calendar.
    """
    if tenant.google_calendar_mode != "shared_account" or not professional.is_active:
        return
    tenant_id, professional_id = tenant.id, professional.id
    try:
        await session.scalar(
            select(Professional)
            .where(Professional.id == professional_id, Professional.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not professional.is_active:
            await session.commit()
            return
        result = await ensure_professional_secondary_calendar(session, tenant, professional)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.info(
            "professional_autocalendar_skipped",
            tenant_id=str(tenant_id),
            professional_id=str(professional_id),
            code=type(exc).__name__,
        )
        # Rollback expires ORM instances; callers must still serialize the row.
        await session.refresh(professional)
        return
    logger.info(
        "professional_autocalendar_created",
        tenant_id=str(tenant_id),
        professional_id=str(professional_id),
        created=result.created,
    )
