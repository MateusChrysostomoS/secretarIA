"""Run the AI draft resolver from the worker: fresh reads, the turn's gate, the right agenda.

`services/booking_draft.py::resolve_booking_draft` decides where an AI booking draft lands
and reads nothing itself. This module is its worker half (TASK-030 P2):

  * `_load_draft_context` re-reads the conversation, the ACTIVE roster and the service and
    convênio catalogs in one short session - never the turn-start snapshot, which can be
    several tool calls old by the time a hand-back runs (spec §4.2);
  * `_turn_booking_gate` is the booking gate for THIS turn - the same one `_run_flow` hands
    to `route()` - so a resolver run outside `route()` still sees held slots as busy;
  * `_draft_calendar_source` builds the agenda lazily, for the professional the resolver
    settled on, and only if it reaches the day step;
  * `_resume_booking_draft` is the continuation: the tap that answers "Essa consulta é pra
    você?" re-runs the resolver over the parked draft (`Conversation.flow_draft`), because
    the world may have changed in the two minutes the patient took to answer.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation, FlowState, Tenant, Unit
from secretaria.services.attendee import real_attendee_name
from secretaria.services.booking_details import clinic_address_line
from secretaria.services.booking_draft import (
    DRAFT_ATTENDEE_OTHER,
    DRAFT_ATTENDEE_SELF,
    BookingDraft,
    CalendarSource,
    DraftResolution,
    draft_from_record,
    resolve_booking_draft,
)
from secretaria.services.booking_hold import BookingGate
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_MULTI, booking_topology
from secretaria.services.calendar import CalendarService
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import (
    ATTENDEE_STEPS,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_PROFESSIONAL,
    STEP_AWAITING_SERVICE,
    FlowRouterResult,
    booking_gate_scope,
)
from secretaria.services.insurance_catalog import load_tenant_insurance
from secretaria.services.service_catalog import load_service_catalog
from secretaria.services.tenant_config import list_active_professionals
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.greeting import _flow_professionals, _flow_tenant_snapshot
from secretaria.workers.shared.llm_context import _appointment_calendar

logger = get_logger(__name__)


@dataclass
class DraftContext:
    """Everything the resolver reads, loaded FRESH for one hand-back."""

    tenant: Tenant
    conversation: SimpleNamespace
    professional_rows: list  # ORM rows: agendas are built from these
    professionals: list  # router-shaped (catalog resolved): what the resolver reads
    service_catalog: list
    tenant_insurance: Any
    tenant_snapshot: SimpleNamespace
    topology: str


def _conversation_flow_snapshot(conversation: Conversation) -> SimpleNamespace:
    return SimpleNamespace(
        id=conversation.id,
        tenant_id=conversation.tenant_id,
        patient_id=conversation.patient_id,
        flow_state=conversation.flow_state,
        flow_step=conversation.flow_step,
        flow_selected_type=conversation.flow_selected_type,
        flow_selected_day=conversation.flow_selected_day,
        flow_selected_slot=conversation.flow_selected_slot,
        flow_selected_professional_id=conversation.flow_selected_professional_id,
        flow_selected_insurance=conversation.flow_selected_insurance,
        flow_managing_appointment_id=conversation.flow_managing_appointment_id,
        flow_attendee_name=conversation.flow_attendee_name,
        flow_draft=conversation.flow_draft,
    )


async def _load_draft_context(reply: _ReplyContext, tenant: Tenant) -> DraftContext | None:
    """One short session of fresh reads, scoped to `tenant`. None = no such conversation.

    A conversation that belongs to another tenant is refused (count-only warning): every
    roster, catalog and agenda below is read for `tenant`, so mixing the two would book a
    patient against another clinic's data.
    """
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        if conversation is None:
            return None
        if conversation.tenant_id != tenant.id:
            logger.warning(
                "worker_draft_context_tenant_mismatch",
                conversation_id=str(reply.conversation_id),
            )
            return None
        tenant = await session.get(Tenant, tenant.id)
        if tenant is None:
            return None
        snapshot = _conversation_flow_snapshot(conversation)
        professional_rows = await list_active_professionals(session, tenant.id)
        service_catalog = await load_service_catalog(session, tenant.id)
        tenant_insurance = await load_tenant_insurance(session, tenant.id)
        active_units = await session.scalar(
            select(func.count())
            .select_from(Unit)
            .where(Unit.tenant_id == tenant.id, Unit.is_active.is_(True))
        )
    tenant_snapshot = _flow_tenant_snapshot(
        tenant, professional_rows, service_catalog, tenant_insurance
    )
    tenant_snapshot.clinic_address = (
        None if active_units else clinic_address_line(getattr(tenant, "address", None))
    )
    return DraftContext(
        tenant=tenant,
        conversation=snapshot,
        professional_rows=professional_rows,
        professionals=_flow_professionals(professional_rows, service_catalog),
        service_catalog=service_catalog,
        tenant_insurance=tenant_insurance,
        tenant_snapshot=tenant_snapshot,
        topology=booking_topology(professional_rows),
    )


def _turn_booking_gate(reply: _ReplyContext, tenant: Tenant | None) -> BookingGate:
    """The booking gate for THIS turn. Armed only on Brain-Message.

    Only then does "Confirmar" become a reservation plus a mailed code instead of an
    appointment (services/booking_hold.py). Built unarmed on WhatsApp rather than left as
    None because the hold LOOKUPS must still happen there: a slot a Portal visitor is
    holding has to be invisible to a WhatsApp patient too, or the reservation only half
    exists.

    `tenant` (already loaded by the caller), NOT `reply.tenant_id`: `_ReplyContext.tenant_id`
    is populated only on the identity legs and the degrade paths, and the ORDINARY turn -
    the one that books - leaves it None. Reading it disarmed the gate on exactly the path
    it exists for, SILENTLY (proved in production on 2026-09-21, pinned by
    tests/test_booking_code_gate.py::test_the_gate_arms_on_the_real_reply_path).
    """
    return BookingGate(
        tenant_id=tenant.id if tenant is not None else reply.tenant_id,
        conversation_id=reply.conversation_id,
        patient_id=None,
        external_id=reply.patient_ref,
        armed=reply.channel == CHANNEL_BRAIN_MESSAGE,
    )


def _draft_calendar_source(tenant: Tenant, ctx: DraftContext) -> CalendarSource:
    """The resolver's agenda, built only when it reaches the day step.

    A professional on a multi-professional clinic -> THAT professional's own agenda;
    otherwise the tenant-level one (on a single-professional clinic `load_tenant_config`
    has already resolved that professional's credentials into it). A professional who no
    longer resolves -> None, which makes the picker answer `calendar_unavailable` instead
    of listing days off whichever agenda happened to be at hand.
    """

    async def _calendar_for(professional: Any | None) -> CalendarService | None:
        target: Any = "tenant"
        if professional is not None and ctx.topology == BOOKING_TOPOLOGY_MULTI:
            target = next((row for row in ctx.professional_rows if row.id == professional.id), None)
        async with async_session_factory() as session:
            return await _appointment_calendar(session, ctx.tenant, target)

    return _calendar_for


async def _resolve_draft(
    reply: _ReplyContext,
    tenant: Tenant,
    draft: BookingDraft,
    ctx: DraftContext,
    *,
    gate: BookingGate | None = None,
) -> DraftResolution:
    """`resolve_booking_draft` over `ctx`, inside this turn's booking gate."""
    with booking_gate_scope(gate if gate is not None else _turn_booking_gate(reply, tenant)):
        return await resolve_booking_draft(
            draft,
            conversation=ctx.conversation,
            tenant=ctx.tenant_snapshot,
            professional_rows=ctx.professionals,
            service_catalog=ctx.service_catalog,
            tenant_insurance=ctx.tenant_insurance,
            calendar=_draft_calendar_source(tenant, ctx),
            now=datetime.now(UTC),
        )


def _fold_answer(
    draft: BookingDraft, answered_step: str | None, result: FlowRouterResult
) -> BookingDraft:
    """Write the patient's answer INTO the draft: it is the newest thing they said.

    Pra-quem (P2): the answer decides `attendee` ("Sim, é pra mim" after the AI said
    "other" means the patient). Doctor / service (TASK-030 P3): the one tapped replaces the
    draft's, so the resolver re-checks everything else against it - a stored service the
    new doctor does not offer is dropped and asked again from its own step. Convênio: the
    answer is recorded on the conversation exactly as given (a typed "Outro convênio"
    included) and the draft's own convênio text gives way to it.
    """
    if answered_step in ATTENDEE_STEPS:
        answered_other = real_attendee_name(result.flow_attendee_name) is not None
        return replace(
            draft, attendee=DRAFT_ATTENDEE_OTHER if answered_other else DRAFT_ATTENDEE_SELF
        )
    if answered_step == STEP_AWAITING_INSURANCE:
        return replace(draft, insurance=None)
    if answered_step == STEP_AWAITING_PROFESSIONAL and result.flow_selected_professional_id:
        return replace(draft, professional_id=result.flow_selected_professional_id)
    if answered_step == STEP_AWAITING_SERVICE and result.flow_selected_type:
        return replace(draft, service=result.flow_selected_type)
    return draft


async def _resume_booking_draft(
    reply: _ReplyContext,
    tenant: Tenant,
    result: FlowRouterResult,
    *,
    gate: BookingGate | None = None,
) -> FlowRouterResult:
    """An answer to a question the AI draft was waiting on: land the draft, not the next list.

    Pra-quem (P2) and, since TASK-030 P3, the convênio, doctor and service questions
    (`flow_router.DRAFT_WAIT_STEPS`). `result` is what `route()` computed for the answer
    (the plain button continuation); it is the fallback for a missing, expired or corrupt
    draft and for ANY resolver failure - the patient always gets the next question, never
    nothing. The answer is folded into the draft (`_fold_answer`) and the resolver checks
    everything again against FRESH data. The authorization of a third party stays recorded
    exactly as the plain path would have recorded it.
    """
    plain = replace(result, flow_draft=None, resume_draft=False)
    answered_step: str | None = None
    try:
        ctx = await _load_draft_context(reply, tenant)
        stored = ctx.conversation.flow_draft if ctx is not None else None
        draft = draft_from_record(stored, now=datetime.now(UTC))
        if ctx is None or draft is None:
            logger.info(
                "booking_draft_resume_skipped",
                conversation_id=str(reply.conversation_id),
                tenant_id=str(tenant.id),
                reason="expired_or_invalid" if stored else "missing",
            )
            return plain
        # The conversation row is still on the question being answered: route()'s result
        # has not been persisted yet.
        answered_step = ctx.conversation.flow_step
        draft = _fold_answer(draft, answered_step, result)
        if answered_step in ATTENDEE_STEPS:
            # Preserve the approved attendee transition from P2a. For catalog answers,
            # leave the original question step until its new details are actually sent.
            ctx.conversation.flow_state = result.flow_state
            ctx.conversation.flow_step = result.flow_step
            ctx.conversation.flow_attendee_name = result.flow_attendee_name
        if result.flow_selected_insurance is not None:
            ctx.conversation.flow_selected_insurance = result.flow_selected_insurance
        resolution = await _resolve_draft(reply, tenant, draft, ctx, gate=gate)
    except Exception as exc:
        logger.warning(
            "booking_draft_resume_failed",
            conversation_id=str(reply.conversation_id),
            error_type=type(exc).__name__,
        )
        return plain
    resumed = resolution.result
    resumed.attendee_authorized = (
        result.attendee_authorized and resumed.flow_state is FlowState.SERVICE_CATALOG
    )
    logger.info(
        "booking_draft_resumed",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        answered_step=answered_step,
        landing_step=resolution.landing_step,
        accepted=list(resolution.accepted),
        dropped=dict(resolution.dropped),
        fallback=resolution.fallback,
    )
    return resumed
