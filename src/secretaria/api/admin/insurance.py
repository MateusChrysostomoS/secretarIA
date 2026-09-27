"""Admin (SaaS-owner) — extend the global convênio catalog without a code migration.

POST /admin/insurance-catalog            - add one operator (name + aliases +
                                            mechanism + note), reconciling any
                                            clinic's legacy free text that now
                                            matches it.
GET  /admin/insurance-catalog/unmatched  - operator visibility: which legacy
                                            strings still have no match.

Guarded by `require_admin` (the same `X-Admin-Token` shared secret as every
other admin route). TASK-008 §3.2 - the point is that adding "GEAP" here is
the ENTIRE fix for every clinic that had "GEAP"/"Geap Saúde" as free text; no
deploy touches this file again for the next operator.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.admin.panel import require_admin
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models.insurance import INSURANCE_MECHANISMS, InsuranceCatalogUnmatched
from secretaria.schemas.insurance import (
    InsuranceCatalogAdminCreate,
    InsuranceCatalogAdminRead,
    InsuranceCatalogUnmatchedRead,
)
from secretaria.services.insurance_catalog import (
    DuplicateCatalogEntry,
    create_catalog_entry,
    reconcile_unmatched_insurances,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@router.post(
    "/insurance-catalog",
    response_model=InsuranceCatalogAdminRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_insurance_catalog_entry(
    body: InsuranceCatalogAdminCreate,
    session: AsyncSession = Depends(get_session),
) -> InsuranceCatalogAdminRead:
    if body.mechanism not in INSURANCE_MECHANISMS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_mechanism",
                "message": f"mechanism deve ser um de: {', '.join(INSURANCE_MECHANISMS)}.",
            },
        )
    try:
        entry = await create_catalog_entry(
            session,
            name=body.name,
            aliases=body.aliases,
            mechanism=body.mechanism,
            note=body.note,
        )
    except DuplicateCatalogEntry:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_catalog_entry",
                "message": "Já existe um convênio com esse nome (ou um equivalente) no catálogo.",
            },
        ) from None

    reconciled = await reconcile_unmatched_insurances(session, entry)
    await session.commit()
    logger.info(
        "admin_insurance_catalog_created",
        catalog_id=str(entry.id),
        slug=entry.slug,
        reconciled_count=reconciled,
    )
    return InsuranceCatalogAdminRead(
        id=str(entry.id),
        slug=entry.slug,
        name=entry.name,
        aliases=list(entry.aliases or []),
        mechanism=entry.mechanism,
        note=entry.note,
        is_active=entry.is_active,
        reconciled_count=reconciled,
    )


@router.get("/insurance-catalog/unmatched", response_model=list[InsuranceCatalogUnmatchedRead])
async def list_unmatched_insurance_strings(
    session: AsyncSession = Depends(get_session),
) -> list[InsuranceCatalogUnmatchedRead]:
    """Every legacy convênio string still waiting for a matching catalog entry."""
    rows = await session.scalars(
        select(InsuranceCatalogUnmatched)
        .where(InsuranceCatalogUnmatched.resolved_at.is_(None))
        .order_by(InsuranceCatalogUnmatched.first_seen_at)
    )
    return [
        InsuranceCatalogUnmatchedRead(
            tenant_id=str(row.tenant_id),
            raw_text=row.raw_text,
            first_seen_at=row.first_seen_at.isoformat(),
        )
        for row in rows
    ]
