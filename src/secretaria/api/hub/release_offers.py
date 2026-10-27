"""Doctor hub - released consultations with a deposit (TASK-052; the screen is TASK-054).

    GET  /tenants/me/release-offers?pending_only=false&mine=false&limit=100
    POST /tenants/me/release-offers/{offer_id}/refunded
    POST /tenants/me/release-offers/{offer_id}/keep/accept
    POST /tenants/me/release-offers/{offer_id}/keep/refuse

Who (owner decision 2026-10-12): EVERY clinic role - secretary, manager, doctor - but only on what
it already sees in the agenda. The scope is the agenda's own (`get_agenda_viewer` ->
`services/agenda_visibility.py::AgendaViewer`, computed by brain-api `services/hub_scope.py`:
secretary and manager = the whole clinic, a plain doctor = his own; a manager who is also a doctor
narrows with `mine=true`, exactly like GET /calendar/events in api/hub/calendar.py). An offer
outside that scope, or of another clinic, is a 404 (never a 403). Bodies are parsed BY HAND (a
validation error would echo the note). Nothing here moves money or calls a provider (D15):
"refunded" records what the CLINIC did in the Asaas panel; accept/refuse decide the patient's
"Manter" request. Repeating any action is a 200 with the current state.
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_agenda_viewer, get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, PixDeposit, Tenant
from secretaria.models.deposit_release_offer import (
    DECISION_KEEP_DECLINED,
    DECISION_KEEP_REQUESTED,
    KEEP_OUTCOME_REFUSED,
    REFUND_DUE,
    REFUND_NOT_RECEIVED,
    SILENT_DECISIONS,
    DepositReleaseOffer,
)
from secretaria.schemas.release_offers import ReleaseOfferListOut, ReleaseOfferOut
from secretaria.services import release_keep, release_offer, release_offer_dispatch, reminder_hooks
from secretaria.services.agenda_visibility import AgendaViewer
from secretaria.services.payments import adjustment_queries, attention_view
from secretaria.services.payments.attention_view import NAME_MAX_CHARS, ResolveRefused

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/release-offers", tags=["hub-release-offers"])

DEFAULT_LIMIT = 100
MAX_LIMIT = 200
MAX_BODY_BYTES = 4096
DEFAULT_NOTE = "Devolução registrada na lista de consultas liberadas."


def _detail(code: str, status_code: int) -> JSONResponse:
    return JSONResponse({"detail": {"code": code}}, status_code=status_code)


async def _rows(
    session: AsyncSession,
    tenant_id: UUID,
    viewer: AgendaViewer,
    *,
    own_only: bool = False,
    pending_only: bool = False,
    limit: int = DEFAULT_LIMIT,
    offer_id: UUID | None = None,
) -> list:
    narrowed = viewer.restricted or own_only
    if narrowed and viewer.professional_id is None:
        return []  # an "own" viewer without a professional reaches nothing
    query = (
        select(DepositReleaseOffer, Appointment, PixDeposit, Patient.name)
        .join(Appointment, Appointment.id == DepositReleaseOffer.released_appointment_id)
        .join(PixDeposit, PixDeposit.id == DepositReleaseOffer.deposit_id)
        .outerjoin(Patient, Patient.id == DepositReleaseOffer.patient_id)
        .where(
            DepositReleaseOffer.tenant_id == tenant_id,
            Appointment.tenant_id == tenant_id,
            PixDeposit.tenant_id == tenant_id,
        )
    )
    if narrowed:
        query = query.where(Appointment.professional_id == viewer.professional_id)
    if offer_id is not None:
        query = query.where(DepositReleaseOffer.id == offer_id)
    if pending_only:
        query = query.where(
            or_(
                DepositReleaseOffer.refund_status.in_((REFUND_DUE, REFUND_NOT_RECEIVED)),
                DepositReleaseOffer.decision.in_((*SILENT_DECISIONS, DECISION_KEEP_REQUESTED))
                & DepositReleaseOffer.deposit_paid.is_(True),
            )
        )
    query = query.order_by(DepositReleaseOffer.released_at.desc()).limit(limit)
    return (await session.execute(query)).all()


async def _held(session: AsyncSession, tenant_id: UUID, rows: list) -> dict[UUID, int]:
    """What the clinic holds per deposit of the page (one query): `amount_cents` + the delta of
    `adjustment_queries.load_adjustment_totals` (the `held_deposit_cents` formula)."""
    totals = await adjustment_queries.load_adjustment_totals(
        session, tenant_id, [row[2].id for row in rows]
    )
    held: dict[UUID, int] = {}
    for _offer, _appointment, deposit, _name in rows:
        found = totals.get(deposit.id)
        held[deposit.id] = int(deposit.amount_cents or 0) + (found.held_delta_cents if found else 0)
    return held


def _out(row, held: dict[UUID, int]) -> ReleaseOfferOut:
    offer, appointment, deposit, patient_name = row
    status = deposit.status.value if hasattr(deposit.status, "value") else str(deposit.status)
    return ReleaseOfferOut(
        id=str(offer.id),
        released_appointment_id=str(offer.released_appointment_id),
        new_appointment_id=str(offer.new_appointment_id) if offer.new_appointment_id else None,
        conversation_id=str(offer.conversation_id) if offer.conversation_id else None,
        patient_display_name=(patient_name or "").strip() or None,
        professional_id=str(appointment.professional_id) if appointment.professional_id else None,
        service_label=appointment.appointment_type,
        slot_start=appointment.start_at,
        slot_end=appointment.end_at,
        amount_cents=deposit.amount_cents,
        held_cents=held.get(deposit.id, int(deposit.amount_cents or 0)),
        deposit_paid_at_release=offer.deposit_paid,
        deposit_status=status,
        refund_status=offer.refund_status,
        refund_source=offer.refund_source,
        decision=offer.decision,
        patient_decision=release_offer.patient_decision(offer),
        keep_deadline_at=offer.keep_deadline_at,
        released_at=offer.released_at,
        decide_by_at=offer.decide_by_at,
        expires_at=offer.expires_at,
        first_contact_at=offer.first_contact_at,
        refund_due_at=offer.refund_due_at,
        refund_marked_at=offer.refund_marked_at,
        patient_answered_at=offer.patient_answered_at,
        can_mark_refunded=offer.deposit_paid
        and offer.refund_status == REFUND_DUE
        and offer.decision != DECISION_KEEP_REQUESTED
        and offer.deposit_reused_at is None,
        can_decide_keep=offer.decision == DECISION_KEEP_REQUESTED,
        deposit_reused=offer.deposit_reused_at is not None,
        carry_check=offer.carry_check,
        state_note=release_offer.DEPOSIT_REUSED_LABEL
        if offer.deposit_reused_at is not None
        else release_offer.CARRY_REFUSED_LABEL
        if offer.carry_check in ("refunded", "unknown")
        else None,
    )


async def _body(request: Request) -> dict | None:
    raw = await request.body()
    if not raw:
        return {}
    if len(raw) > MAX_BODY_BYTES:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _name(data: dict) -> str | None:
    value = data.get("resolved_by_name")
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > NAME_MAX_CHARS:
        return None
    return value.strip()


async def _visible(session, tenant: Tenant, viewer: AgendaViewer, offer_id: str):
    """(offer uuid, row) when the offer exists in this clinic AND in this viewer's agenda."""
    try:
        offer_uuid = UUID(offer_id)
    except ValueError:
        return None, None
    rows = await _rows(session, tenant.id, viewer, offer_id=offer_uuid)
    return offer_uuid, (rows[0] if rows else None)


async def _answer(session, tenant: Tenant, viewer: AgendaViewer, offer_uuid: UUID) -> JSONResponse:
    rows = await _rows(session, tenant.id, viewer, offer_id=offer_uuid)
    (row,) = rows
    held = await _held(session, tenant.id, rows)
    return JSONResponse(_out(row, held).model_dump(mode="json"), status_code=200)


async def _dispatch_now(session, offer_uuid: UUID) -> None:
    try:  # what the action made due now (the patient's messages); the sweep is the retry
        await release_offer_dispatch.dispatch_due(session, offer_id=offer_uuid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("release_offer_dispatch_failed", error_type=type(exc).__name__)


@router.get("", response_model=ReleaseOfferListOut)
async def list_release_offers(
    pending_only: bool = Query(default=False),
    mine: bool = Query(default=False),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    tenant: Tenant = Depends(get_current_tenant),
    viewer: AgendaViewer = Depends(get_agenda_viewer),
    session: AsyncSession = Depends(get_session),
):
    if mine and not viewer.restricted and viewer.professional_id is None:
        return _detail("no_own_agenda", 422)  # same answer as GET /calendar/events?mine=true
    rows = await _rows(
        session, tenant.id, viewer, own_only=mine, pending_only=pending_only, limit=limit
    )
    held = await _held(session, tenant.id, rows)
    return ReleaseOfferListOut(items=[_out(row, held) for row in rows])


@router.post("/{offer_id}/refunded")
async def mark_offer_refunded(
    offer_id: str,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    viewer: AgendaViewer = Depends(get_agenda_viewer),
    session: AsyncSession = Depends(get_session),
):
    data = await _body(request)
    if data is None:
        return _detail("invalid_body", 422)
    try:
        _action, note, name = attention_view.check_resolution_input(
            release_offer.REFUNDED_ACTION,
            data.get("note") or DEFAULT_NOTE,
            data.get("resolved_by_name"),
        )
    except ResolveRefused as refused:
        return _detail(refused.code, 422)
    offer_uuid, row = await _visible(session, tenant, viewer, offer_id)
    if row is None:
        return _detail("not_found", 404)
    outcome = await release_offer.mark_refunded(
        session, tenant_id=tenant.id, offer_id=offer_uuid, note=note, resolved_by_name=name
    )
    if outcome in ("not_due", "keep_pending", "deposit_reused"):
        code = {"not_due": "refund_not_due"}.get(outcome, outcome)
        return _detail(code, 409)
    if outcome == "not_found":
        return _detail("not_found", 404)
    if outcome == "marked":
        await _dispatch_now(session, offer_uuid)
    logger.info("release_offer_marked_refunded", outcome=outcome)
    return await _answer(session, tenant, viewer, offer_uuid)


@router.post("/{offer_id}/keep/accept")
async def accept_offer_keep(
    offer_id: str,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    viewer: AgendaViewer = Depends(get_agenda_viewer),
    session: AsyncSession = Depends(get_session),
):
    data = await _body(request)
    name = _name(data or {})
    if data is None or name is None:
        return _detail("resolved_by_required", 422)
    offer_uuid, row = await _visible(session, tenant, viewer, offer_id)
    if row is None:
        return _detail("not_found", 404)
    offer = row[0]
    result = await release_keep.accept_keep(session, tenant=tenant, offer=offer, resolved_by=name)
    if result.outcome == release_keep.ACCEPT_UNAVAILABLE:
        return _detail(result.outcome, 503)
    if result.outcome not in (release_keep.ACCEPT_KEPT, release_keep.ACCEPT_ALREADY):
        return _detail(result.outcome, 409)
    if result.outcome == release_keep.ACCEPT_KEPT:
        await reminder_hooks.after_appointment_booked(result.appointment.id)
        await _dispatch_now(session, offer_uuid)
    return await _answer(session, tenant, viewer, offer_uuid)


@router.post("/{offer_id}/keep/refuse")
async def refuse_offer_keep(
    offer_id: str,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    viewer: AgendaViewer = Depends(get_agenda_viewer),
    session: AsyncSession = Depends(get_session),
):
    data = await _body(request)
    name = _name(data or {})
    if data is None or name is None:
        return _detail("resolved_by_required", 422)
    offer_uuid, row = await _visible(session, tenant, viewer, offer_id)
    if row is None:
        return _detail("not_found", 404)
    offer = row[0]
    if offer.decision == DECISION_KEEP_DECLINED and offer.keep_outcome == KEEP_OUTCOME_REFUSED:
        return await _answer(session, tenant, viewer, offer_uuid)  # refused before: same answer
    if not await release_offer.decline_keep(
        session, offer=offer, outcome=KEEP_OUTCOME_REFUSED, resolved_by=name
    ):
        await session.rollback()
        return _detail("not_requested", 409)
    await session.commit()
    await _dispatch_now(session, offer_uuid)
    return await _answer(session, tenant, viewer, offer_uuid)
