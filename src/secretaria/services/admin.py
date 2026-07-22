"""Destructive admin operations shared by the CLI script and the API.

Single source of truth for which tables are wiped so the `scripts/reset_db.py`
CLI and the `POST /admin/reset` endpoint stay in sync.
"""

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Conversation, Patient, Tenant

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


# --- Single-tenant removal (brain-api "delete clinic" orchestration) -------
# Unlike wipe_data (an all-or-nothing fleet truncate), this removes ONE clinic:
# the tenant row plus the empty config that cascades from it (professionals,
# credentials, units, ...). It deliberately does NOT delete conversation
# history: a tenant that still has conversations/patients/appointments is
# REFUSED (HAS_DATA), because the tenant_id FKs are ON DELETE CASCADE and there
# is no way to drop the clinic row while keeping that history — so declining is
# the only way to honor "delete the clinic, not the conversations".


class DeleteTenantOutcome(Enum):
    """Result of `delete_tenant` — the API layer maps this to a status code."""

    DELETED = "deleted"  # tenant removed (had no conversation history)
    NOT_FOUND = "not_found"  # no tenant with that id -> 404
    HAS_DATA = "has_data"  # conversation history present -> 409 (kept intact)


@dataclass(frozen=True)
class DeleteTenantResult:
    """`delete_tenant`'s outcome plus the blocking data counts (0 unless HAS_DATA)."""

    outcome: DeleteTenantOutcome
    conversations: int = 0
    patients: int = 0
    appointments: int = 0


async def _count(session: AsyncSession, model: type, tenant_id: UUID) -> int:
    """COUNT(*) of a tenant-scoped table for one tenant."""
    total = await session.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    )
    return int(total or 0)


async def delete_tenant(session: AsyncSession, tenant_id: UUID) -> DeleteTenantResult:
    """Delete a single clinic (tenant) — but never its conversation history.

    Refuses (HAS_DATA) when the tenant still has any conversation, patient or
    appointment row, so this path can only ever remove a clean/empty clinic (the
    brain-api "delete clinic" flow targets clinics with no anamnesis and no
    conversation). When clean, `session.delete` removes the tenant and the empty
    config that cascades from it (professionals, credentials, units, ...). Does
    NOT commit — the API layer commits after mapping the outcome to a status.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return DeleteTenantResult(DeleteTenantOutcome.NOT_FOUND)

    conversations = await _count(session, Conversation, tenant_id)
    patients = await _count(session, Patient, tenant_id)
    appointments = await _count(session, Appointment, tenant_id)
    if conversations or patients or appointments:
        return DeleteTenantResult(
            DeleteTenantOutcome.HAS_DATA,
            conversations=conversations,
            patients=patients,
            appointments=appointments,
        )

    await session.delete(tenant)
    return DeleteTenantResult(DeleteTenantOutcome.DELETED)
