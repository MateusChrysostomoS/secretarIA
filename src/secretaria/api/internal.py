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
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient
from secretaria.schemas.internal import (
    InternalAppointment,
    InternalAppointmentList,
    InternalPatient,
    InternalPatientList,
)

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
    """
    expected = get_settings().INTERNAL_API_KEY
    if not expected:
        # No server-side key => the surface is locked, not "try again later".
        logger.warning("internal_auth_unconfigured")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Internal API not configured.",
        )
    if not key or not secrets.compare_digest(key, expected):
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
