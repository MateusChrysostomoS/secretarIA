"""Destructive admin operations shared by the CLI script and the API.

Single source of truth for which tables are wiped so the `scripts/reset_db.py`
CLI and the `POST /admin/reset` endpoint stay in sync.
"""

from sqlalchemy import text

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger

logger = get_logger(__name__)

# Conversation-related tables. Order is from child to parent — even though
# `TRUNCATE ... CASCADE` would handle FKs on its own, keeping the order
# explicit makes the intent (and any future swap to DELETE) obvious.
DEFAULT_WIPE_TABLES: tuple[str, ...] = (
    "messages",
    "conversations",
    "patients",
    "processed_events",
)
TENANT_TABLE = "tenants"


async def wipe_data(*, include_tenants: bool = False) -> list[str]:
    """Truncate the conversation tables and optionally the tenants table.

    Returns the list of tables actually wiped, in execution order, so the
    caller can log or surface it to the operator.
    """
    tables = list(DEFAULT_WIPE_TABLES)
    if include_tenants:
        tables.append(TENANT_TABLE)

    async with async_session_factory() as session:
        async with session.begin():
            for table in tables:
                await session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
    logger.warning("admin_wipe_completed", tables=tables)
    return tables
