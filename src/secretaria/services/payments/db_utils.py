"""Small dialect-aware DB helpers shared by the payments code."""

from typing import Any
from uuid import UUID

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession


async def insert_ignore(
    session: AsyncSession,
    model: type,
    values: dict[str, Any],
    *,
    index_elements: list[str],
) -> UUID | None:
    """`INSERT ... ON CONFLICT (index_elements) DO NOTHING RETURNING id`.

    Returns the new row's id, or None when a row with the same key already existed (the
    replay case). One statement, so it is atomic under concurrent delivery: exactly one
    caller ever gets the id. Postgres in production, SQLite in the unit tests.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        stmt = postgresql.insert(model)
    elif dialect == "sqlite":
        stmt = sqlite.insert(model)
    else:  # pragma: no cover - the repo only runs on those two
        raise RuntimeError(f"insert_ignore does not support the {dialect!r} dialect")
    stmt = stmt.values(**values).on_conflict_do_nothing(index_elements=index_elements)
    return await session.scalar(stmt.returning(model.id))
