"""The `before` booking flow end to end at the confirmation turn (spec sections 5.2, 7, 11).

"Confirmar" on a clinic on `before` timing creates NOTHING on Google and NOTHING in
`appointments`: it holds the slot, creates the Pix and parks the conversation in
`AWAITING_DEPOSIT`. Drives the real router (`_handle_confirmation`) and the real persistence
seam (`_apply_flow_result`) with a scripted provider and scripted senders.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import Appointment, BookingHold, Conversation, FlowState, Message, PixDeposit
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import booking_hold as holds, email, flow_router as fr
from secretaria.services.payments import booking_charge, deposit_gate, deposit_lifecycle, registry
from secretaria.services.payments.provider import ChargeState, PaymentStatus
from secretaria.services.pending_identity import RequestCodeOutcome, RequestCodeResult
from secretaria.workers import tasks
from tests._patching import workers_ns
from tests.before_support import entitlement_summary, make_world, new_charge
from tests.payments_support import FakeProvider, FakeRedis, make_sessionmaker

TZ = ZoneInfo("America/Sao_Paulo")
SLOT_ISO = "2030-03-04T10:00"


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    for module in (workers_ns, holds, booking_charge):
        monkeypatch.setattr(module, "async_session_factory", maker)
    yield maker
    await engine.dispose()


class Calendar:
    tzinfo = TZ

    def __init__(self) -> None:
        self.created: list = []
        self.free_slot_kwargs: list[dict] = []
        self.day_kwargs: list[dict] = []

    async def create_event(self, start, end, summary, description=""):
        self.created.append((start, end, summary))
        return {"id": "evt-1", "htmlLink": "https://calendar.google.com/evt-1"}


class Sender:
    """A channel sender that records what it delivered (WhatsApp shape: caller records rows)."""

    persists_outbound = False

    def __init__(self, *, fail_on: int | None = None) -> None:
        self.sent: list[tuple] = []
        self.fail_on = fail_on
        self.calls = 0

    async def send_text_message(self, to, body):
        self.calls += 1
        if self.fail_on is not None and self.calls >= self.fail_on:
            raise RuntimeError("meta down")
        self.sent.append(("text", to, body))
        return {"messages": [{"id": f"wamid.{self.calls}"}]}

    async def send_buttons(self, to, body, buttons):
        self.calls += 1
        if self.fail_on is not None and self.calls >= self.fail_on:
            raise RuntimeError("meta down")
        self.sent.append(("buttons", to, body, buttons))
        return {"messages": [{"id": f"wamid.{self.calls}"}]}


class Env:
    def __init__(self) -> None:
        self.provider = FakeProvider()
        self.provider.created_charge = new_charge()
        self.emails: list[tuple[str, str]] = []
        self.calendar = Calendar()


@pytest.fixture
def env(monkeypatch) -> Env:
    e = Env()

    async def entitlements(tenant_id, redis):
        return entitlement_summary()

    async def send_email(to_email, clinic_name, kind):
        e.emails.append((to_email, kind))

    async def request_code(tenant_id, external_id):
        return RequestCodeResult(RequestCodeOutcome.NOT_PENDING)

    monkeypatch.setattr(deposit_gate, "get_entitlements", entitlements)
    monkeypatch.setattr(email, "send_payment_alert", send_email)
    monkeypatch.setattr(holds, "request_code", request_code)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", e.provider)
    return e


def _snapshot(conversation) -> SimpleNamespace:
    return SimpleNamespace(
        id=conversation.id,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Consulta",
        flow_selected_day="2030-03-04",
        flow_selected_slot=SLOT_ISO,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
    )


async def _confirm(world, env, *, armed=False):
    gate = holds.BookingGate(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=None,
        external_id=world.patient.external_id or "",
        armed=armed,
        redis=FakeRedis(),
    )
    return await fr._handle_confirmation(
        _snapshot(world.conversation),
        world.tenant,
        env.calendar,
        fr.LABEL_CONFIRM,
        "Maria",
        gate=gate,
    )


def _reply(world, *, channel="whatsapp") -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref=world.patient.wa_id or world.patient.external_id,
        inbound_body=fr.LABEL_CONFIRM,
        channel=channel,
        tenant_id=world.tenant_id,
    )


async def _conversation(db, world) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, world.conversation.id)


async def test_confirming_holds_the_slot_and_creates_nothing_on_google_or_in_appointments(
    db, env
):
    async with db() as session:
        world = await make_world(session)
    result = await _confirm(world, env)

    assert env.calendar.created == []  # nothing on Google
    async with db() as session:
        assert list(await session.scalars(select(Appointment))) == []
        (hold,) = list(await session.scalars(select(BookingHold)))
        (deposit,) = list(await session.scalars(select(PixDeposit)))
    assert result.appointment is None and result.booking_hold is None
    assert result.flow_state is FlowState.AWAITING_DEPOSIT
    request = result.deposit_request
    assert request["deposit_id"] == deposit.id == hold.charge_id
    assert request["amount_cents"] == 6000 and request["copy_paste"] == "PIXCOPIAECOLA"
    # The selection is CARRIED so "Trocar horario" can re-open the picker from it.
    assert (result.flow_selected_type, result.flow_selected_slot) == ("Consulta", SLOT_ISO)


async def test_the_persistence_seam_parks_the_conversation_and_sends_the_pix_in_order(
    db, env, monkeypatch
):
    async with db() as session:
        world = await make_world(session)
    sender = Sender()
    monkeypatch.setattr(workers_ns, "_reply_sender", lambda reply, tenant, token: sender)
    result = await _confirm(world, env)

    handled = await tasks._apply_flow_result(
        _reply(world), result, world.patient.wa_id, tenant=world.tenant, waba_token="wt"
    )

    assert handled is True
    conversation = await _conversation(db, world)
    assert conversation.flow_state == FlowState.AWAITING_DEPOSIT
    assert conversation.flow_selected_type == "Consulta"
    assert [kind for kind, *_ in sender.sent] == ["text", "buttons"]  # ONE message per Pix
    intro, card = sender.sent
    assert "sinal de R$ 60,00" in intro[2] and "30 minutos" in intro[2]
    assert "04/03/2030 às 10:00" in intro[2]  # the clinic's timezone, not UTC
    assert intro[2].rstrip().endswith("```PIXCOPIAECOLA```")  # the code, fenced, at the end
    assert not any(item[2] == "PIXCOPIAECOLA" for item in sender.sent)
    assert [label for _id, label in card[3]] == ["Já paguei", "Trocar horário", "Cancelar reserva"]
    async with db() as session:
        rows = list(await session.scalars(select(Message)))
    assert any("```PIXCOPIAECOLA```" in row.body for row in rows)  # WhatsApp history recorded


async def test_a_portal_patient_with_no_phone_gets_the_pix_as_portal_messages(db, env):
    async with db() as session:
        world = await make_world(session, channel="brain_message")
    result = await _confirm(world, env, armed=True)
    assert result.flow_state is FlowState.AWAITING_DEPOSIT  # NOT_PENDING: straight to the Pix

    handled = await tasks._apply_flow_result(
        _reply(world, channel="brain_message"),
        result,
        None,
        tenant=world.tenant,
        waba_token=None,
    )

    assert handled is True
    async with db() as session:
        bodies = [
            row.body
            for row in await session.scalars(
                select(Message).where(Message.conversation_id == world.conversation.id)
            )
        ]
    assert len(bodies) == 2 and any("```PIXCOPIAECOLA```" in body for body in bodies)
    assert "PIXCOPIAECOLA" not in bodies


async def test_a_pix_that_cannot_be_delivered_voids_the_charge_frees_the_slot_and_calls_a_human(
    db, env, monkeypatch
):
    env.provider.state = ChargeState(PaymentStatus.PENDING)
    async with db() as session:
        world = await make_world(session, contact_email="clinic@example.com")
    sender = Sender(fail_on=1)  # every send fails
    monkeypatch.setattr(workers_ns, "_reply_sender", lambda reply, tenant, token: sender)
    result = await _confirm(world, env)

    await tasks._apply_flow_result(
        _reply(world),
        result,
        world.patient.wa_id,
        redis=FakeRedis(),
        tenant=world.tenant,
        waba_token="wt",
    )

    assert env.provider.called("cancel_charge") == 1
    async with db() as session:
        assert list(await session.scalars(select(BookingHold))) == []
        (deposit,) = list(await session.scalars(select(PixDeposit)))
    assert deposit.status == PixDepositStatus.EXPIRED
    conversation = await _conversation(db, world)
    assert conversation.flow_state == FlowState.IDLE
    assert conversation.handover_state.value == "HUMAN_ACTIVE"
    assert env.emails == []  # owner answer 13: no operational e-mail
    assert sender.calls >= 3  # two attempts at the first message + the blocked notice tries


async def test_one_failed_send_is_retried_once_and_the_rest_still_goes_out(db, env, monkeypatch):
    async with db() as session:
        world = await make_world(session)
    sender = Sender()
    original = sender.send_text_message
    state = {"n": 0}

    async def flaky(to, body):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("meta blip")
        return await original(to, body)

    sender.send_text_message = flaky
    monkeypatch.setattr(workers_ns, "_reply_sender", lambda reply, tenant, token: sender)
    result = await _confirm(world, env)
    await tasks._apply_flow_result(
        _reply(world), result, world.patient.wa_id, tenant=world.tenant, waba_token="wt"
    )
    assert [kind for kind, *_ in sender.sent] == ["text", "buttons"]
    assert env.provider.called("cancel_charge") == 0
    assert (await _conversation(db, world)).flow_state == FlowState.AWAITING_DEPOSIT


async def test_a_blocked_deposit_makes_no_booking_holds_nothing_and_hands_over(db, env):
    async with db() as session:
        world = await make_world(session, with_account=False, contact_email="c@example.com")
    result = await _confirm(world, env)
    assert result.action == "handover"
    assert [b.body for b in result.bubbles] == [deposit_lifecycle.BEFORE_BLOCKED_TEXT]
    assert result.appointment is None and result.deposit_request is None
    assert env.calendar.created == []
    async with db() as session:
        assert list(await session.scalars(select(BookingHold))) == []


async def test_a_clinic_on_after_still_books_with_the_event_and_the_confirmation(db, env):
    async with db() as session:
        world = await make_world(session, timing="after")
    result = await _confirm(world, env)
    assert result.appointment is not None and result.deposit_request is None
    assert len(env.calendar.created) == 1
    assert "está confirmado" in result.bubbles[0].body


async def test_a_fully_held_day_is_offered_and_says_which_times_are_held(db, env):
    """TASK-052 (owner decision 2026-10-11) reverts the TASK-050 port that hid it: the day is
    offered (an unpaid Pix may expire), and picking it lists the held TIMES - never who."""
    from secretaria.models import Patient

    async with db() as session:
        world = await make_world(session)
        rival = Patient(id=uuid4(), tenant_id=world.tenant_id, wa_id="5511888888888")
        session.add(rival)
        await session.flush()
        rival_conversation = Conversation(
            id=uuid4(), tenant_id=world.tenant_id, patient_id=rival.id
        )
        session.add(rival_conversation)
        await session.commit()
    start = datetime.now(UTC) + timedelta(days=2)
    await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=rival_conversation.id,
        patient_id=rival.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )

    held_local = start.astimezone(TZ)
    held_day = held_local.replace(hour=0, minute=0, second=0, microsecond=0)

    class Recording(Calendar):
        # Google says the held day is free, and its ONLY free slot is the held one.
        async def list_available_days(self, start_day, days, slot_minutes=None, **kwargs):
            self.day_kwargs.append(kwargs)
            return [held_day]

        async def list_free_slots(self, day, slot_minutes=None, max_slots=None, **kwargs):
            return [
                {
                    "start": held_local.isoformat(),
                    "end": (held_local + timedelta(minutes=30)).isoformat(),
                    "label": held_local.strftime("%H:%M"),
                }
            ]

    calendar = Recording()
    gate = holds.BookingGate(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=None,
        external_id="",
        armed=False,
    )
    token = fr._ACTIVE_GATE.set(gate)
    try:
        result = await fr.enter_day_picker(
            _snapshot(world.conversation), world.tenant, calendar, duration_minutes=30
        )
        slot_step = await fr._enter_slot_picker(
            _snapshot(world.conversation),
            world.tenant,
            calendar,
            held_day,
            duration_minutes=30,
        )
    finally:
        fr._ACTIVE_GATE.reset(token)
    assert len(calendar.day_kwargs) == 1
    assert result.flow_state != FlowState.MENU
    day_rows = [row[0] for row in result.bubbles[0].rows]
    assert f"day|{held_day.date().isoformat()}|0" in day_rows
    (bubble,) = slot_step.bubbles
    label = held_local.strftime("%H:%M")
    assert bubble.body.startswith(f"Todos os horários livres deste dia ({label})")
    assert "pagamento do sinal" in bubble.body and "voltam a ficar livres" in bubble.body
    assert "5511888888888" not in bubble.body  # times only, never the other patient
    assert [row[0] for row in bubble.rows] == ["dayagain|0"]  # nothing held is tappable
