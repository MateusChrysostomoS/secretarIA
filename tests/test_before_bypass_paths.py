"""Every path that could take a slot or book around the deposit (spec sections 4.2 item 4, 4.4).

On a clinic on `before` timing a consultation is created only by a paid deposit. Four other doors
exist and each is closed here: the LLM's creation tools (refuse, send the agent to the buttons),
the LLM's availability tools (held slots are busy), the hub's manual booking, reschedule and
Editar/Remarcar `/edit` (409, decision D4 - a new hub door that moves a time belongs here too)
and, for completeness, what stays open on purpose (a doctor's block, a clinic that
is on `after`, a cancelled add-on).
"""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.ai import tools as ai_tools
from secretaria.api.hub import calendar as hub_calendar
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core import database as core_database
from secretaria.core.database import get_session
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    Conversation,
    Patient,
    Professional,
    Tenant,
)
from secretaria.plugins import multi_professional as mp, multi_unit as mu
from secretaria.services import booking_hold as holds
from secretaria.services.payments import deposit_gate
from secretaria.services.tenant_config import set_google_refresh_token
from tests.before_support import entitlement_summary, make_world
from tests.payments_support import make_sessionmaker

HOLD_START = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
HOLD_END = HOLD_START + timedelta(minutes=30)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(core_database, "async_session_factory", maker)
    monkeypatch.setattr(holds, "async_session_factory", maker)
    yield maker
    await engine.dispose()


class Entitlement:
    value = entitlement_summary()


@pytest.fixture(autouse=True)
def _entitlement(monkeypatch):
    Entitlement.value = entitlement_summary()

    async def get(tenant_id, redis):
        return Entitlement.value

    monkeypatch.setattr(deposit_gate, "get_entitlements", get)


class Calendar:
    """Records what the tools ask of Google; never creates anything on its own."""

    tzinfo = ZoneInfo("America/Sao_Paulo")

    def __init__(self) -> None:
        self.created: list = []
        self.free_slot_kwargs: list[dict] = []
        self.busy: list[dict] = []

    async def create_event(self, start, end, summary, description=""):
        self.created.append((start, end, summary))
        return {"id": "evt-1", "status": "confirmed", "htmlLink": "https://cal/x"}

    async def check_availability(self, start, end, max_results=50):
        return list(self.busy)

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6, **kwargs):
        self.free_slot_kwargs.append(kwargs)
        return [{"start": "2030-03-04T08:00", "end": "2030-03-04T08:30", "label": "08:00"}]


@contextmanager
def _agent(tenant_id, calendar, conversation_id=None, topology=None):
    tokens = [
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(tenant_id)),
        (ai_tools._tenant_config_ctx, ai_tools._tenant_config_ctx.set(None)),
        (ai_tools._conversation_id_ctx, ai_tools._conversation_id_ctx.set(conversation_id)),
        (ai_tools._redis_ctx, ai_tools._redis_ctx.set(None)),
        (ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(calendar)),
    ]
    if topology is not None:
        tokens.append(
            (ai_tools._booking_topology_ctx, ai_tools._booking_topology_ctx.set(topology))
        )
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


CREATE_ARGS = {
    "start": "2030-03-04T10:00:00",
    "end": "2030-03-04T10:30:00",
    "summary": "Consulta - Maria",
    "appointment_type": "Consulta",
}


async def _appointments(db):
    async with db() as session:
        return list(await session.scalars(select(Appointment)))


# ---- the LLM's creation tools ----------------------------------------------------------------


async def test_the_llm_cannot_create_a_consultation_on_a_before_clinic(db):
    async with db() as session:
        world = await make_world(session)
    calendar = Calendar()
    with _agent(world.tenant_id, calendar, world.conversation.id):
        result = await ai_tools.create_event.ainvoke(CREATE_ARGS)
    assert "start_guided_booking" in result["error"]
    assert "NÃO diga ao paciente que a consulta foi marcada" in result["error"]
    assert calendar.created == [] and await _appointments(db) == []


@pytest.mark.parametrize(
    "case", ["after", "deposit_off", "addon_cancelled"]
)
async def test_a_clinic_that_is_not_gated_keeps_its_llm_booking(db, case):
    async with db() as session:
        world = await make_world(
            session,
            timing="after" if case == "after" else "before",
            pix_deposit_enabled=case != "deposit_off",
        )
    if case == "addon_cancelled":
        Entitlement.value = entitlement_summary(addon=False)
    calendar = Calendar()
    with _agent(world.tenant_id, calendar, world.conversation.id):
        result = await ai_tools.create_event.ainvoke(CREATE_ARGS)
    assert "error" not in result and len(calendar.created) == 1


async def test_an_unreadable_entitlement_refuses_instead_of_booking_without_the_deposit(db):
    async with db() as session:
        world = await make_world(session)
    Entitlement.value = None
    calendar = Calendar()
    with _agent(world.tenant_id, calendar, world.conversation.id):
        result = await ai_tools.create_event.ainvoke(CREATE_ARGS)
    assert "error" in result and calendar.created == []


async def test_a_failure_reading_the_clinic_refuses_too(db, monkeypatch):
    async with db() as session:
        world = await make_world(session)

    def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(core_database, "async_session_factory", boom)
    calendar = Calendar()
    with _agent(world.tenant_id, calendar, world.conversation.id):
        result = await ai_tools.create_event.ainvoke(CREATE_ARGS)
    assert "error" in result and calendar.created == []


async def test_the_multi_professional_and_multi_unit_creation_tools_refuse_too(db):
    async with db() as session:
        world = await make_world(session)
    calendar = Calendar()
    with _agent(world.tenant_id, calendar, world.conversation.id):
        by_professional = await mp.create_event_for_professional.ainvoke(
            {"professional_name": "Dra. Ana", **CREATE_ARGS}
        )
        by_unit = await mu.create_event_at_unit.ainvoke({"unit_name": "Centro", **CREATE_ARGS})
    assert "start_guided_booking" in by_professional["error"]
    assert "start_guided_booking" in by_unit["error"]
    assert calendar.created == [] and await _appointments(db) == []


# ---- the LLM's availability tools ------------------------------------------------------------


async def _hold_for_another_conversation(db, world, *, professional_id=None, minutes_left=30):
    async with db() as session:
        rival = Patient(id=uuid4(), tenant_id=world.tenant_id, wa_id="5511777777777")
        session.add(rival)
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=world.tenant_id, patient_id=rival.id)
        session.add(conversation)
        await session.commit()
    held = await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=conversation.id,
        patient_id=rival.id,
        professional_id=professional_id,
        appointment_type="Consulta",
        insurance=None,
        start_at=HOLD_START,
        end_at=HOLD_END,
        ttl_minutes=minutes_left,
    )
    return conversation, held


async def test_a_slot_another_patient_is_paying_for_is_busy_for_the_llm(db):
    async with db() as session:
        world = await make_world(session, timing="after")  # holds exist on every clinic
    await _hold_for_another_conversation(db, world)
    calendar = Calendar()
    with _agent(world.tenant_id, calendar, world.conversation.id):
        availability = await ai_tools.check_availability.ainvoke(
            {
                "start": HOLD_START.replace(tzinfo=None).isoformat(),
                "end": HOLD_END.replace(tzinfo=None).isoformat(),
            }
        )
        slots = await ai_tools.list_free_slots.ainvoke({"day": "2030-03-04"})
    assert availability["busy"] == [
        {"start": HOLD_START.isoformat(), "end": HOLD_END.isoformat(), "do_paciente": False}
    ]  # only the window travels: never whose hold it is
    assert calendar.free_slot_kwargs[0]["extra_busy"] == [(HOLD_START, HOLD_END)]
    assert slots["slots"]


async def test_the_patients_own_hold_never_hides_their_own_choice(db):
    async with db() as session:
        world = await make_world(session, timing="after")
    await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=HOLD_START,
        end_at=HOLD_END,
    )
    calendar = Calendar()
    with _agent(world.tenant_id, calendar, world.conversation.id):
        availability = await ai_tools.check_availability.ainvoke(
            {
                "start": HOLD_START.replace(tzinfo=None).isoformat(),
                "end": HOLD_END.replace(tzinfo=None).isoformat(),
            }
        )
    assert availability["busy"] == []


async def test_without_holds_the_free_slot_call_is_exactly_what_it_was(db):
    async with db() as session:
        world = await make_world(session, timing="after")
    calendar = Calendar()
    with _agent(world.tenant_id, calendar, world.conversation.id):
        await ai_tools.list_free_slots.ainvoke({"day": "2030-03-04"})
    assert calendar.free_slot_kwargs == [{}]  # no `extra_busy` kwarg for a double without it


# ---- the hub ---------------------------------------------------------------------------------


class HubCalendar:
    created: list = []
    updated: list = []

    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    async def create_event(self, start, end, summary, description=""):
        HubCalendar.created.append(start)
        return {"id": f"evt-{uuid4()}", "htmlLink": "https://cal/x"}

    async def update_event(self, event_id, start, end):
        HubCalendar.updated.append(start)
        return {"id": event_id}

    # POST /edit (TASK-032 R7) asks Google whether the slot is free, then patches the event.
    async def is_slot_free(self, start, end, ignore_event_id=None):
        return True

    async def update_event_details(self, event_id, start, end, summary, description=""):
        HubCalendar.updated.append(start)
        return {"id": event_id}

    def references_same_calendar(self, other):
        return True


@pytest_asyncio.fixture
async def hub_world(db, monkeypatch):
    from fastapi import Depends

    from secretaria.main import app

    HubCalendar.created, HubCalendar.updated = [], []
    async with db() as session:
        world = await make_world(session, timing="after")
        await set_google_refresh_token(session, world.tenant_id, "fake-refresh-token")
        await session.commit()

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, world.tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    monkeypatch.setattr(hub_calendar, "CalendarService", HubCalendar)
    yield world
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


def _body(start=HOLD_START, end=HOLD_END):
    return {"start": start.isoformat(), "end": end.isoformat(), "summary": "Consulta - Joao"}


async def test_a_manual_booking_over_a_held_slot_is_409_and_creates_nothing(
    client: AsyncClient, db, hub_world
):
    await _hold_for_another_conversation(db, hub_world)
    response = await client.post("/tenants/me/calendar/appointments", json=_body())
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "slot_held_awaiting_payment"
    assert HubCalendar.created == [] and await _appointments(db) == []


async def test_a_manual_booking_on_a_free_slot_needs_no_deposit_and_still_works(
    client: AsyncClient, db, hub_world
):
    response = await client.post("/tenants/me/calendar/appointments", json=_body())
    assert response.status_code == 201
    assert len(HubCalendar.created) == 1  # decision D4: a human's booking is not gated


async def test_an_expired_hold_no_longer_blocks_the_hub(client: AsyncClient, db, hub_world):
    conversation, held = await _hold_for_another_conversation(db, hub_world)
    async with db() as session:
        row = await session.get(BookingHold, held.id)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert (
        await client.post("/tenants/me/calendar/appointments", json=_body())
    ).status_code == 201


async def test_a_hold_on_a_neighbouring_window_does_not_block(client: AsyncClient, db, hub_world):
    await _hold_for_another_conversation(db, hub_world)
    later = HOLD_END  # half-open windows: adjacent is not overlapping
    response = await client.post(
        "/tenants/me/calendar/appointments", json=_body(later, later + timedelta(minutes=30))
    )
    assert response.status_code == 201


async def test_a_doctors_block_is_not_restricted_by_a_hold(client: AsyncClient, db, hub_world):
    await _hold_for_another_conversation(db, hub_world)
    response = await client.post(
        "/tenants/me/calendar/blocks",
        json={"start": HOLD_START.isoformat(), "end": HOLD_END.isoformat(), "summary": "Almoço"},
    )
    assert response.status_code == 201  # the promotion revalidates and asks the clinic instead


async def test_rescheduling_onto_a_held_slot_is_409(client: AsyncClient, db, hub_world):
    await _hold_for_another_conversation(db, hub_world)
    async with db() as session:
        appointment = Appointment(
            tenant_id=hub_world.tenant_id,
            google_event_id="evt-old",
            appointment_type="Consulta",
            start_at=HOLD_START + timedelta(days=1),
            end_at=HOLD_END + timedelta(days=1),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appointment)
        await session.commit()
        appointment_id = str(appointment.id)
    response = await client.post(
        f"/tenants/me/calendar/appointments/{appointment_id}/reschedule",
        json={"new_start": HOLD_START.isoformat(), "new_end": HOLD_END.isoformat()},
    )
    assert response.status_code == 409
    assert HubCalendar.updated == []


async def _a_booked_appointment(db, world, *, start=HOLD_START + timedelta(days=1)):
    async with db() as session:
        appointment = Appointment(
            tenant_id=world.tenant_id,
            patient_id=world.patient.id,
            google_event_id="evt-old",
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appointment)
        await session.commit()
        return appointment.id, appointment.start_at


@pytest.mark.parametrize("allow_overlap", [False, True])
async def test_editing_onto_a_held_slot_is_409_even_as_an_encaixe(
    client: AsyncClient, db, hub_world, allow_overlap
):
    """The clinic's Editar/Remarcar (POST /edit, TASK-032 R7) is a door too (D4): neither a
    plain move nor an encaixe may take the slot another patient is paying for."""
    await _hold_for_another_conversation(db, hub_world)
    appointment_id, start_before = await _a_booked_appointment(db, hub_world)
    response = await client.post(
        f"/tenants/me/calendar/appointments/{appointment_id}/edit",
        json={"start_at": HOLD_START.isoformat(), "allow_overlap": allow_overlap},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "slot_held_awaiting_payment"
    assert HubCalendar.updated == []  # Google never touched
    async with db() as session:
        row = await session.get(Appointment, appointment_id)
        # nor the row (SQLite hands the timestamp back naive)
        assert row.start_at.replace(tzinfo=None) == start_before.replace(tzinfo=None)


async def test_editing_next_to_a_held_slot_still_works(client: AsyncClient, db, hub_world):
    await _hold_for_another_conversation(db, hub_world)
    appointment_id, _ = await _a_booked_appointment(db, hub_world)
    response = await client.post(
        f"/tenants/me/calendar/appointments/{appointment_id}/edit",
        json={"start_at": HOLD_END.isoformat()},  # adjacent: half-open windows do not overlap
    )
    assert response.status_code == 200, response.text
    assert HubCalendar.updated == [HOLD_END]


async def test_editing_only_the_doctor_onto_a_held_slot_is_409(client: AsyncClient, db, hub_world):
    """Same window, different doctor: the hold of that doctor's slot still counts (D4)."""
    async with db() as session:
        doctor = Professional(id=uuid4(), tenant_id=hub_world.tenant_id, name="Dra. Ana")
        session.add(doctor)
        await session.commit()
    await _hold_for_another_conversation(db, hub_world, professional_id=doctor.id)
    appointment_id, _ = await _a_booked_appointment(db, hub_world, start=HOLD_START)
    response = await client.post(
        f"/tenants/me/calendar/appointments/{appointment_id}/edit",
        json={"professional_id": str(doctor.id)},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "slot_held_awaiting_payment"
    assert HubCalendar.updated == []


async def test_an_edit_that_keeps_window_and_doctor_never_asks_the_hold_gate(
    client: AsyncClient, db, hub_world
):
    """The appointment sits on a slot someone else holds (no gate ran when it was made); a
    change that moves neither time nor doctor must not be refused because of it."""
    await _hold_for_another_conversation(db, hub_world)
    appointment_id, _ = await _a_booked_appointment(db, hub_world, start=HOLD_START)
    response = await client.post(
        f"/tenants/me/calendar/appointments/{appointment_id}/edit",
        json={"attendee_name": "Maria"},
    )
    assert response.status_code == 200, response.text


async def test_a_hold_of_another_clinic_never_blocks_this_one(client: AsyncClient, db, hub_world):
    async with db() as session:
        other = await make_world(session, timing="after")
    await _hold_for_another_conversation(db, other)
    response = await client.post("/tenants/me/calendar/appointments", json=_body())
    assert response.status_code == 201


def test_the_hub_message_says_what_to_do_and_names_no_patient():
    text = hub_calendar.SLOT_HELD_MESSAGE
    assert "pagando o sinal" in text and "Escolha outro horário" in text
