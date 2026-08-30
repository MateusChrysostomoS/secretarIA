"""Doctor hub — professionals CRUD (multi_professional addon).

GET   /tenants/me/professionals        - list (always allowed, even when the
                                          addon is disabled — a tenant that lost
                                          the addon can still see what it had).
                                          Rows include onboarding completeness
                                          (contract v1 §10 item E).
POST  /tenants/me/professionals        - create.
PATCH /tenants/me/professionals/{id}   - update (name, google_calendar_id, is_active).
PUT   /tenants/me/professionals/{id}/config
                                        - per-professional config (business_hours,
                                          appointment_types, specialty, about,
                                          context_doctor_message, google_calendar_id).

NO professional email is written or returned by any route here. That address
belongs to brain-api (`users.email`, written by the invite flow) and is read per
use through services/brain_professionals.py — the alert worker asks for it
rather than holding a copy that would drift. A screen that wants to display it
reads brain-api's `linked_user_email` from `GET /doctor/professionals`.
POST  /tenants/me/professionals/calendars
                                        - shared_account mode: the same thing for
                                          EVERY active professional that has no
                                          calendar yet, in ONE request. What the hub
                                          calls right after a save that switches the
                                          tenant to shared_account, so "Conta unica"
                                          actually produces one internal agenda per
                                          doctor instead of leaving one button per
                                          doctor to find and press. Idempotent;
                                          answers 200 with a per-row report.
POST  /tenants/me/professionals/{id}/calendar
                                        - shared_account mode only (contract in
                                          docs/CHECKPOINT_google_calendar_modes.md):
                                          idempotently create a secondary Google
                                          Calendar for this professional under the
                                          clinic's connected account and persist its
                                          id onto google_calendar_id. Never gated by
                                          entitlements (core platform wiring, not an
                                          addon) — 422 `clinic_calendar_not_connected`
                                          when the clinic has no Calendar connected,
                                          409 `google_reconnect_required` when the
                                          stored clinic token predates the
                                          `calendar.app.created` scope.

Entitlement + limit enforcement (brain-api is the source of truth, fetched
fresh — `redis=None` — since this path is not hot):
  - Addon disabled -> creating, or activating (is_active: false -> true), a
    professional is rejected with 403 {"detail": "multi_professional_not_entitled"}.
  - Creating/activating an ACTIVE professional beyond `limits["professionals"]`
    is rejected with 409 {"detail": "professional_limit_reached"}.
  - A failed entitlement fetch (None) fails CLOSED: 503, never silently allowed.
  - Renaming, changing the calendar id, DEACTIVATING, or saving the `/config`
    body never touch entitlements/limits at all — those are always allowed
    regardless of addon state, so a downgraded tenant can still tidy up its
    existing rows (same "config save is never gated" principle as the
    tenant-level PUT /tenants/me/config — see api/hub/config.py).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.professional import Professional
from secretaria.schemas.professional import (
    ProfessionalCalendarBulkItem,
    ProfessionalCalendarBulkResult,
    ProfessionalCalendarConnect,
    ProfessionalConfigUpdate,
    ProfessionalCreate,
    ProfessionalListItem,
    ProfessionalRead,
    ProfessionalUpdate,
)
from secretaria.services import hub_configuration as hubcfg
from secretaria.services.calendar import (
    CalendarUnavailableError,
    GoogleScopeInsufficientError,
)
from secretaria.services.entitlements_client import get_entitlements, is_entitled
from secretaria.services.tenant_config import (
    ClinicCalendarNotConnectedError,
    ensure_professional_secondary_calendar,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/professionals", tags=["hub-professionals"])

ADDON_KEY = "multi_professional"
LIMIT_KEY = "professionals"


def _read_model(professional: Professional) -> ProfessionalRead:
    return ProfessionalRead(
        id=str(professional.id),
        name=professional.name,
        google_calendar_id=professional.google_calendar_id,
        is_active=professional.is_active,
        created_at=professional.created_at,
    )


# Both thin wrappers below delegate to services/hub_configuration.py, which is
# also what PUT /tenants/me/configuration uses. Sharing the implementation is
# the point: the transactional endpoint and these legacy ones must agree on
# what a professional row looks like and on who is allowed to reach it.


async def _list_item(
    session: AsyncSession, professional: Professional, tenant: Tenant
) -> ProfessionalListItem:
    return await hubcfg.professional_list_item(session, professional, tenant)


async def _get_professional(
    session: AsyncSession, tenant: Tenant, professional_id: str
) -> Professional:
    try:
        return await hubcfg.resolve_professional(session, tenant, professional_id)
    except hubcfg.ProfessionalNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional not found") from None


async def _count_active(session: AsyncSession, tenant_id: UUID) -> int:
    result = await session.scalar(
        select(func.count())
        .select_from(Professional)
        .where(Professional.tenant_id == tenant_id, Professional.is_active.is_(True))
    )
    return int(result or 0)


async def _require_entitled_within_limit(session: AsyncSession, tenant: Tenant) -> None:
    """Gate an activate/create-active mutation. Raises HTTPException on failure.

    Fetches entitlements fresh (redis=None — this path is not hot). None
    (fetch failure) fails CLOSED: 503, never treated as "allowed".
    """
    summary = await get_entitlements(tenant.id, redis=None)
    if summary is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not verify entitlements right now"
        )
    if not is_entitled(summary, ADDON_KEY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "multi_professional_not_entitled")

    limit = summary.limits.get(LIMIT_KEY)
    if limit is not None:
        active_count = await _count_active(session, tenant.id)
        if active_count >= limit:
            raise HTTPException(status.HTTP_409_CONFLICT, "professional_limit_reached")


@router.get("", response_model=list[ProfessionalListItem])
async def list_professionals(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[ProfessionalListItem]:
    rows = await session.scalars(
        select(Professional).where(Professional.tenant_id == tenant.id).order_by(Professional.name)
    )
    return [await _list_item(session, p, tenant) for p in rows]


@router.post("", response_model=ProfessionalRead, status_code=status.HTTP_201_CREATED)
async def create_professional(
    body: ProfessionalCreate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalRead:
    if body.is_active:
        await _require_entitled_within_limit(session, tenant)

    professional = Professional(
        tenant_id=tenant.id,
        name=body.name,
        google_calendar_id=body.google_calendar_id,
        is_active=body.is_active,
    )
    session.add(professional)
    await session.commit()
    await session.refresh(professional)
    logger.info(
        "hub_professional_created", tenant_id=str(tenant.id), professional_id=str(professional.id)
    )

    await _ensure_calendar_for_new_professional(session, tenant, professional)
    return _read_model(professional)


async def _ensure_calendar_for_new_professional(
    session: AsyncSession, tenant: Tenant, professional: Professional
) -> None:
    """In shared_account mode, a professional joining gets their agenda too.

    BEST EFFORT, and after the professional is already committed. A clinic in
    `shared_account` mode has said "every doctor lives inside our one Google
    account"; a doctor added afterwards belongs there as well, and making the
    clinic notice a missing calendar later is exactly the gap the bulk endpoint
    exists to close.

    Never allowed to fail the creation itself: the professional is a real row
    that must exist regardless of Google's mood, and the retry paths (the bulk
    endpoint, the per-row button) are both idempotent. So every failure here is
    logged and swallowed — including "the clinic has no Google account", which
    for a tenant mid-onboarding is the normal case, not an error.
    """
    if tenant.google_calendar_mode != "shared_account" or not professional.is_active:
        return
    # Ids are read BEFORE the try: `session.rollback()` expires every instance,
    # so reading them inside the handler would issue lazy database IO from an
    # exception path — the same trap api/hub/config.py documents.
    tenant_id, professional_id = str(tenant.id), str(professional.id)
    try:
        result = await ensure_professional_secondary_calendar(session, tenant, professional)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.info(
            "hub_professional_autocalendar_skipped",
            tenant_id=tenant_id,
            professional_id=professional_id,
            code=type(exc).__name__,
        )
        # The caller still has to serialize this row, and the rollback just
        # expired it. Reload it here rather than leaving a landmine one
        # attribute access away.
        await session.refresh(professional)
        return
    logger.info(
        "hub_professional_autocalendar_created",
        tenant_id=tenant_id,
        professional_id=professional_id,
        created=result.created,
    )


@router.patch("/{professional_id}", response_model=ProfessionalRead)
async def update_professional(
    professional_id: str,
    body: ProfessionalUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalRead:
    professional = await _get_professional(session, tenant, professional_id)
    data = body.model_dump(exclude_unset=True)

    activating = data.get("is_active") is True and not professional.is_active
    if activating:
        await _require_entitled_within_limit(session, tenant)

    if "name" in data:
        professional.name = data["name"]
    if "google_calendar_id" in data:
        professional.google_calendar_id = data["google_calendar_id"]
    if "is_active" in data:
        professional.is_active = data["is_active"]

    await session.commit()
    await session.refresh(professional)
    logger.info(
        "hub_professional_updated", tenant_id=str(tenant.id), professional_id=str(professional.id)
    )
    return _read_model(professional)


@router.put("/{professional_id}/config", response_model=ProfessionalListItem)
async def update_professional_config(
    professional_id: str,
    body: ProfessionalConfigUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalListItem:
    """Per-professional config save — NEVER gated by entitlements/limits/activation
    (contract v1 §10 item E, same "config save is always allowed" principle as
    the tenant-level PUT /tenants/me/config).

    LEGACY single-scope save. A screen that edits the tenant and a professional
    together should use PUT /tenants/me/configuration instead, which commits
    both in one transaction rather than leaving a half-saved state behind when
    the second request fails.

    `business_hours` / `appointment_types` are three-state: absent leaves them
    alone, explicit `null` goes back to inheriting the clinic's legacy column,
    and `{}` / `[]` writes an empty OWN override that inherits nothing (see
    ProfessionalConfigUpdate).
    """
    professional = await _get_professional(session, tenant, professional_id)
    data = body.model_dump(exclude_unset=True)

    try:
        await hubcfg.check_appointment_type_service_ids(session, tenant, data)
    except hubcfg.UnknownServiceIds as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unknown_service_ids",
                "message": (
                    "Um ou mais serviços selecionados não existem mais no catálogo "
                    "da clínica. Recarregue a página e escolha de novo."
                ),
                "service_ids": exc.service_ids,
            },
        ) from None

    hubcfg.apply_professional_config(professional, data)

    await session.commit()
    await session.refresh(professional)
    logger.info(
        "hub_professional_config_updated",
        tenant_id=str(tenant.id),
        professional_id=str(professional.id),
        fields=sorted(data.keys()),
        # Categorical: which side each field now resolves from, AFTER the save.
        # Makes "this save turned inheritance into an empty override" legible
        # without logging a single hour or service name.
        **hubcfg.config_source_fields(professional),
    )
    return await _list_item(session, professional, tenant)


@router.post("/calendars", response_model=ProfessionalCalendarBulkResult)
async def create_professional_calendars(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalCalendarBulkResult:
    """shared_account mode: give EVERY active professional a dedicated calendar.

    The per-professional POST below is the same operation for one row. This one
    exists because switching a clinic to `shared_account` is a single decision
    about the WHOLE clinic, and until now the only way to act on it was to find
    and press one button per doctor — so a clinic that flipped the mode and
    saved got nothing at all, which reads as the feature being broken rather
    than as it being unfinished.

    Idempotent for the same reason the singular one is: a professional that
    already has a `google_calendar_id` is counted under `already` and never
    sent to Google twice. Re-running after a partial failure retries only what
    failed.

    Two conditions are properties of the CLINIC, not of any one professional,
    so they fail the whole request rather than every row in turn:

      - no clinic Google account connected -> 422 `clinic_calendar_not_connected`,
        and nothing was created (that check runs before the first Google call).
      - the stored clinic token predates the `calendar.app.created` scope ->
        409 `google_reconnect_required`, but only AFTER committing whatever
        already succeeded: those calendars exist inside Google, and dropping
        their ids here would orphan them.

    Anything else that goes wrong for ONE professional (a Google outage
    mid-run) is reported on that row and the run continues. Hence 200 with a
    per-row report rather than a single status: the successes are already
    committed, and the clinic has to be able to see which doctors still need
    one.

    NOT gated on the tenant actually being in `shared_account` mode. The hub
    calls this right after the save that sets the mode, and refusing based on a
    value the same client just wrote is a race with no upside; a secondary
    calendar is inert in `per_professional` mode anyway, since the routing rule
    is what decides whether `google_calendar_id` is ever paired with the
    clinic's credentials.
    """
    professionals = list(
        await session.scalars(
            select(Professional)
            .where(Professional.tenant_id == tenant.id, Professional.is_active.is_(True))
            .order_by(Professional.created_at)
        )
    )

    items: list[ProfessionalCalendarBulkItem] = []
    created = already = failed = 0

    for professional in professionals:
        try:
            result = await ensure_professional_secondary_calendar(session, tenant, professional)
        except ClinicCalendarNotConnectedError:
            await session.rollback()
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "clinic_calendar_not_connected",
                    "message": (
                        "A clínica ainda não conectou uma conta do Google Calendar. "
                        "Conecte a agenda da clínica antes de criar agendas por profissional."
                    ),
                },
            ) from None
        except GoogleScopeInsufficientError:
            await session.commit()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "google_reconnect_required",
                    "message": (
                        "A conexão da clínica com o Google Calendar não tem mais a "
                        "permissão necessária para criar agendas. Reconecte a conta "
                        "Google da clínica para continuar."
                    ),
                },
            ) from None
        except Exception as exc:
            # Per-professional failure: keep going and report the row. `error`
            # is a CODE, never the exception text — a Google error body can
            # carry the clinic's own account details.
            failed += 1
            code = (
                "calendar_unavailable"
                if isinstance(exc, CalendarUnavailableError)
                else "unexpected_error"
            )
            logger.warning(
                "hub_professional_bulk_calendar_failed",
                tenant_id=str(tenant.id),
                professional_id=str(professional.id),
                code=code,
            )
            items.append(
                ProfessionalCalendarBulkItem(
                    professional_id=str(professional.id),
                    name=professional.name,
                    google_calendar_id=professional.google_calendar_id,
                    created=False,
                    error=code,
                )
            )
            continue

        if result.created:
            created += 1
        else:
            already += 1
        items.append(
            ProfessionalCalendarBulkItem(
                professional_id=str(result.professional_id),
                name=professional.name,
                google_calendar_id=result.google_calendar_id,
                created=result.created,
            )
        )

    await session.commit()
    logger.info(
        "hub_professional_calendars_ensured",
        tenant_id=str(tenant.id),
        created=created,
        already=already,
        failed=failed,
    )
    return ProfessionalCalendarBulkResult(
        created=created, already=already, failed=failed, items=items
    )


@router.post("/{professional_id}/calendar", response_model=ProfessionalCalendarConnect)
async def create_professional_calendar(
    professional_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalCalendarConnect:
    """shared_account mode: idempotently create this professional's secondary
    Google Calendar under the clinic's connected account (item 3 of
    docs/CHECKPOINT_google_calendar_modes.md).

    Never gated by entitlements/limits — same "config save is always
    allowed" principle as `update_professional_config` above (this is core
    platform wiring, not an addon). Ownership is enforced by `_get_professional`
    (404 for an unknown id or one belonging to another tenant), exactly like
    every other professional-scoped hub endpoint.
    """
    professional = await _get_professional(session, tenant, professional_id)
    try:
        result = await ensure_professional_secondary_calendar(session, tenant, professional)
    except ClinicCalendarNotConnectedError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "clinic_calendar_not_connected",
                "message": (
                    "A clínica ainda não conectou uma conta do Google Calendar. "
                    "Conecte a agenda da clínica antes de criar agendas por profissional."
                ),
            },
        ) from None
    except GoogleScopeInsufficientError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "google_reconnect_required",
                "message": (
                    "A conexão da clínica com o Google Calendar não tem mais a "
                    "permissão necessária para criar agendas. Reconecte a conta "
                    "Google da clínica para continuar."
                ),
            },
        ) from None

    await session.commit()
    logger.info(
        "hub_professional_secondary_calendar_ensured",
        tenant_id=str(tenant.id),
        professional_id=str(professional.id),
        created=result.created,
    )
    return ProfessionalCalendarConnect(
        professional_id=str(result.professional_id),
        google_calendar_id=result.google_calendar_id,
        created=result.created,
    )
