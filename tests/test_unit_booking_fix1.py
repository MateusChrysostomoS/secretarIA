# ruff: noqa: F811
"""TASK-083 Task 7 review, fix round 1 - the five Important findings, one proof each.

1. Every flow-state reset outside the router clears `flow_selected_unit_id` (the unit belongs to
   ONE booking, like the attendee): LLM / attendee / edit expiry, the payer-data opt-out, the
   "quer continuar?" "Não", the deposit wait's `_clear_selection`, `/menu3`, an agent booking.
2. The Portal re-offer after a slot taken at promotion lists the UNIT's days.
3. A doctor whose own hours are empty but whose unit links carry hours can be booked (router and
   AI resolver); without links the "no hours" alert is unchanged.
4. The base `create_event` fails CLOSED when the unit lookup fails (no event, an error).
5. (tests/test_flow_router_units.py) the day and slot lists with the unit back row: <= 10 rows.
"""

import datetime as dt
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select, update

from secretaria.ai import tools as ai_tools
from secretaria.ai.formatter import SlotsBubble
from secretaria.core import database as core_database
from secretaria.models import (
    Appointment,
    BookingHold,
    Conversation,
    FlowState,
    Patient,
)
from secretaria.services import deposit_wait, flow_router as fr
from secretaria.services.booking_draft import BookingDraft, resolve_booking_draft
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_SOLE
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_units import STEP_AWAITING_UNIT
from secretaria.services.local_calendar import LocalCalendarService
from secretaria.services.payments import payer_data
from secretaria.workers import tasks
from secretaria.workers.shared import state_expiry
from secretaria.workers.shared.booking_hold import SlotTakenAtPromotion
from secretaria.workers.shared.debug_reset import reset_conversation_for_debug
from tests._local_agenda import runtime_config
from tests.test_flow_router_units import (
    CENTRO_HOURS,
    _Agenda,
    _conversation,
    _doctor,
    _tenant,
    _units,
)
from tests.test_unit_booking_e2e import (  # noqa: F401 - fixtures
    EXTERNAL_ID,
    TZ as E2E_TZ,
    _conversation as _row,
    _pick_sul_and_a_time,
    _seed,
    _turn,
    db,
    wired,
)

STALE = datetime.now(UTC) - timedelta(days=3)


# --------------------------------------------------------------------------
# 1. Resets clear the unit
# --------------------------------------------------------------------------


def _stale_conversation(state, **kw):
    base = dict(
        flow_state=state,
        flow_step=None,
        flow_selected_type="Consulta",
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        flow_attendee_name="Maria",
        flow_draft=None,
        flow_replaces_appointment_id=None,
        flow_edit_draft=None,
        flow_selected_unit_id=uuid4(),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_llm_expiry_clears_the_unit():
    conversation = _stale_conversation(FlowState.LLM)
    tenant = SimpleNamespace(initial_flows={})
    assert state_expiry._expire_stale_llm_state(conversation, tenant, STALE) is True
    assert conversation.flow_selected_unit_id is None


def test_the_attendee_and_edit_expiries_clear_the_unit():
    tenant = SimpleNamespace(initial_flows={})
    attendee = _stale_conversation(FlowState.SERVICE_CATALOG, flow_step=fr.STEP_AWAITING_DAY)
    assert state_expiry._expire_stale_attendee_step(attendee, tenant, STALE) is True
    assert attendee.flow_selected_unit_id is None
    edit = _stale_conversation(FlowState.EDIT_BOOKING)
    assert state_expiry._expire_stale_edit_state(edit, tenant, STALE) is True
    assert edit.flow_selected_unit_id is None


def test_leaving_the_deposit_wait_clears_the_unit():
    conversation = _stale_conversation(FlowState.AWAITING_DEPOSIT)
    deposit_wait._clear_selection(conversation)
    assert conversation.flow_selected_unit_id is None


async def _set(db, conversation_id, **values):
    async with db() as session, session.begin():
        await session.execute(
            update(Conversation).where(Conversation.id == conversation_id).values(**values)
        )


async def test_the_payer_data_opt_out_clears_the_unit(db):
    tenant, _doctor_row, centro, _sul, conversation = await _seed(db, channel="whatsapp", units=2)
    for timing in ("before", "after"):
        await _set(
            db,
            conversation.id,
            flow_state=FlowState.AWAITING_PAYER_DATA,
            flow_step=payer_data.step_for(timing, "document", 0),
            flow_selected_unit_id=centro.id,
        )
        await _turn(tenant, "whatsapp", payer_data.LABEL_OPTOUT)
        assert (await _row(db, conversation.id)).flow_selected_unit_id is None, timing


async def test_the_no_to_quer_continuar_clears_the_unit(db):
    tenant, _doctor_row, centro, _sul, conversation = await _seed(db, channel="whatsapp", units=2)
    await _set(
        db,
        conversation.id,
        flow_step=fr.STEP_AWAITING_DAY,
        flow_selected_unit_id=centro.id,
        reactivation_origin=FlowState.SERVICE_CATALOG.value,
    )
    await _turn(tenant, "whatsapp", "Não")
    assert (await _row(db, conversation.id)).flow_selected_unit_id is None


async def test_the_debug_reset_clears_the_unit(db):
    tenant, _doctor_row, centro, _sul, conversation = await _seed(db, channel="whatsapp", units=2)
    await _set(db, conversation.id, flow_selected_unit_id=centro.id)
    async with db() as session, session.begin():
        row = await session.get(Conversation, conversation.id)
        patient = await session.get(Patient, row.patient_id)
        await reset_conversation_for_debug(session, conversation=row, patient=patient)
    assert (await _row(db, conversation.id)).flow_selected_unit_id is None


async def test_an_agent_booking_consumes_the_unit(db, monkeypatch):
    monkeypatch.setattr(core_database, "async_session_factory", db)
    tenant, _doctor_row, centro, _sul, conversation = await _seed(db, channel="whatsapp", units=2)
    await _set(db, conversation.id, flow_selected_unit_id=centro.id)
    tokens = [
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(tenant.id)),
        (ai_tools._conversation_id_ctx, ai_tools._conversation_id_ctx.set(conversation.id)),
    ]
    try:
        start = datetime(2099, 1, 5, 13, 0, tzinfo=UTC)
        await ai_tools._persist_appointment(
            {"id": "evt-agent"}, start, start + timedelta(minutes=30), "Consulta",
            unit_id=centro.id,
        )
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
    assert (await _row(db, conversation.id)).flow_selected_unit_id is None


# --------------------------------------------------------------------------
# 2. The Portal re-offer after a taken slot lists the unit's days
# --------------------------------------------------------------------------


async def test_the_portal_reoffer_after_a_taken_slot_lists_the_units_days(db):
    tenant, doctor, _centro, sul, conversation = await _seed(
        db, channel=CHANNEL_BRAIN_MESSAGE, units=2
    )
    start_local = await _pick_sul_and_a_time(db, tenant, CHANNEL_BRAIN_MESSAGE, sul, conversation)
    await _turn(tenant, CHANNEL_BRAIN_MESSAGE, fr.LABEL_CONFIRM)
    async with db() as session:
        [hold] = list(await session.scalars(select(BookingHold)))
    assert hold.unit_id == sul.id
    # SQLite keeps the wall time and drops the offset; store the instant, as Postgres does.
    start_utc = start_local.astimezone(UTC)
    async with db() as session, session.begin():
        row = await session.get(BookingHold, hold.id)
        row.start_at, row.end_at = start_utc, start_utc + timedelta(minutes=30)
    # Somebody takes the time on the doctor's agenda while the code is being typed.
    await LocalCalendarService.for_config(
        _runtime(tenant.id), professional_id=doctor.id
    ).create_event(start_utc, start_utc + timedelta(minutes=30), "Outro paciente")

    taken = await tasks._promote_booking_hold(
        tasks._ReplyContext(
            channel=CHANNEL_BRAIN_MESSAGE,
            conversation_id=conversation.id,
            tenant_id=None,
            patient_ref=EXTERNAL_ID,
            inbound_body="123456",
        ),
        tenant=tenant,
        waba_token=None,
        redis=None,
    )
    assert isinstance(taken, SlotTakenAtPromotion)
    [bubble] = [b for b in taken.reoffer.bubbles if isinstance(b, SlotsBubble)]
    weekdays = {
        dt.date.fromisoformat(row[0].split("|")[1]).weekday()
        for row in bubble.rows
        if row[0].startswith("day|")
    }
    assert weekdays == {1}  # Zona Sul: Tuesdays only - never the agenda's whole week
    assert taken.reoffer.flow_selected_unit_id == sul.id
    async with db() as session:
        assert list(await session.scalars(select(Appointment))) == []


def _runtime(tenant_id):
    return runtime_config(tenant_id)


# --------------------------------------------------------------------------
# 3. Own hours empty, unit hours set: bookable
# --------------------------------------------------------------------------


async def test_a_doctor_with_no_own_hours_but_unit_hours_is_booked_through_the_units():
    two = _doctor(_units(2))
    two.business_hours = {}  # own EMPTY schedule: only the units say when
    asked = await fr.route(
        _conversation(), _tenant(), _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[two]
    )
    assert asked.flow_step == STEP_AWAITING_UNIT

    [centro] = _units(1)
    one = _doctor([centro])
    one.business_hours = {}
    agenda = _Agenda()
    days = await fr.route(
        _conversation(), _tenant(), agenda, fr.LABEL_BOOK_SERVICE, professionals=[one]
    )
    assert days.flow_step == fr.STEP_AWAITING_DAY
    assert agenda.log.day_hours == [CENTRO_HOURS]


async def test_without_links_an_empty_own_schedule_still_alerts_the_clinic():
    plain = _doctor()
    plain.business_hours = {}
    result = await fr.route(
        _conversation(), _tenant(), _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[plain]
    )
    assert result.action == "professional_config_incomplete"


async def test_the_ai_resolver_lands_a_unit_doctor_with_no_own_hours():
    two = _doctor(_units(2))
    two.business_hours = {}
    two.context_doctor_message = None
    conversation = _conversation(flow_state=FlowState.LLM, flow_step=None, tenant_id=None,
                                 flow_draft=None)

    async def _calendar(professional):
        return _Agenda()

    resolution = await resolve_booking_draft(
        BookingDraft(service="Consulta"),
        conversation=conversation,
        tenant=_tenant(),
        professional_rows=[two],
        service_catalog=[],
        tenant_insurance=None,
        calendar=_calendar,
        now=datetime(2026, 10, 5, 9, 0, tzinfo=E2E_TZ),
    )
    assert resolution.result.action == "reply"
    assert resolution.result.flow_step == STEP_AWAITING_UNIT


# --------------------------------------------------------------------------
# 4. The base create_event fails closed on a failed unit lookup
# --------------------------------------------------------------------------


async def test_the_base_create_event_books_nothing_when_the_unit_lookup_fails(monkeypatch):
    from secretaria.ai import unit_scope

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(unit_scope, "sole_professional_row", _boom)
    agenda = _Agenda()
    tenant_id = uuid4()
    config = SimpleNamespace(professional_id=uuid4(), tenant_id=tenant_id)
    tokens = [
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(tenant_id)),
        (ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(agenda)),
        (ai_tools._tenant_config_ctx, ai_tools._tenant_config_ctx.set(config)),
        (ai_tools._booking_topology_ctx, ai_tools._booking_topology_ctx.set(BOOKING_TOPOLOGY_SOLE)),
    ]

    async def _no_deposit(_tool):
        return None

    monkeypatch.setattr(ai_tools, "_blocked_by_required_deposit", _no_deposit)
    try:
        result = await ai_tools.create_event.ainvoke(
            {"start": "2026-07-10T09:00:00", "end": "2026-07-10T09:30:00", "summary": "Consulta"}
        )
        listed = await ai_tools.list_free_slots.ainvoke({"day": "2026-07-10"})
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
    assert "error" in result
    assert agenda.log.created == []
    assert "slots" in listed  # a READ still answers (the agenda's own hours)
