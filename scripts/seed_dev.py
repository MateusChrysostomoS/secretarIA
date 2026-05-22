"""Create a development tenant from environment settings.

Usage:
    uv run python scripts/seed_dev.py
"""

import asyncio

from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory, engine
from secretaria.core.logging import get_logger, setup_logging
from secretaria.models import Tenant

logger = get_logger(__name__)


async def seed() -> None:
    """Insert a single development tenant if one does not already exist."""
    setup_logging()
    settings = get_settings()
    phone_number_id = settings.META_PHONE_NUMBER_ID or "dev-phone-number-id"

    async with async_session_factory() as session:
        async with session.begin():
            existing = await session.scalar(
                select(Tenant).where(Tenant.phone_number_id == phone_number_id)
            )
            if existing is not None:
                logger.info("seed_tenant_exists", tenant_id=str(existing.id))
                return
            tenant = Tenant(
                clinic_name="Clinica de Desenvolvimento",
                phone_number_id=phone_number_id,
                access_token=settings.META_ACCESS_TOKEN,
                precheck_enabled=False,
            )
            session.add(tenant)
        logger.info("seed_tenant_created", tenant_id=str(tenant.id))

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
