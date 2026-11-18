"""TASK-085 R / UA3 - the patient's side of D-R10/D-R11 (3rd round), WhatsApp and the Portal.

Contract: tasks/TASK-085/sinal-trocas/mapa-u.md §1.5, §4.5, §4.6, §7 (+ UA1's contract deltas,
results/UA1-implementer.md). Pinned here:

* `patient_context.load_upcoming_appointments(rebooking_id=)`: the suspended consultation the
  patient moves with "Outro horário" comes back (marked) only while its change is REBOOKABLE;
  without the id, today's list byte for byte;
* a suspended edit's old event leaves the agenda AFTER the commit (`AppliedEdit.
  vacate_adjustment_id` -> `old_slot.vacate_old_slot`); every other outcome vacates nothing;
* "Manter a antiga" -> `old_slot.restore_old_slot(trigger="tap")`: restored / slot taken (the
  "Outro horário" buttons) / agenda down / too late;
* "Outro horário" -> `choose_after_expiry(choice="other")` -> the reschedule flow on the old
  consultation (its own agenda, no reschedule limit).

The multi-turn rebook (day -> time -> Confirmar -> `finish_rebook`) lives in
tests/test_sinal_trocas_ua3_rebook.py, which reuses the doubles below.

UA2 (the engine) is written at the same time: until it lands, the `ua2` fixture stubs its frozen
§4.4 surface (`svcdiffother`, `choice_buttons(old_slot_free=)`, `lost_slot_buttons`, the new
`choice_answer_text` kinds and `choose_after_expiry(choice="other")`); once `Choice` has "other"
the fixture does nothing and the real engine answers.
"""

import typing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    Conversation,
    FlowState,
    Patient,
    PixDeposit,
    Tenant,
)
from secretaria.models.deposit_adjustment import (
    MODE_APPLIED,
    MODE_SUSPEND,
    OLD_SLOT_RELEASED,
    OLD_SLOT_RESTORED,
    REBOOKABLE_STATUSES,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_KEPT_OLD,
    STATUS_OLD_SLOT_LOST,
    STATUS_RESTORING,
    DepositAdjustment,
)
from secretaria.services import booking_hold, flow_router as fr, patient_context
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.payments import old_slot, service_change
from secretaria.services.payments.service_change import ChoiceOutcome
from secretaria.workers import tasks
from secretaria.workers.orchestrator import _send_bot_reply
from secretaria.workers.portal import inbound
from secretaria.workers.portal.inbound import _persist_brain_message_inbound
from secretaria.workers.shared import actions, appointment_edit_apply as apply_module
from secretaria.workers.shared.sender import _reply_sender
from tests._edit_flow_support import EditCalendar
from tests._patching import workers_ns
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, get_conversation, sent, wire
from tests._reminders_v2 import (
    WA_ID,
    outbound_messages,
    reload_appointment,
    seed_paid_deposit,
    seed_world,
)
from tests.payments_support import FakeProvider
from tests.test_appointment_edit_paid_service import _edit, _world as _paid_world

TZ = ZoneInfo("America/Sao_Paulo")
OTHER = "svcdiffother"
LABEL_OTHER = "Outro horário"


# --------------------------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------------------------


class Agenda:
    """A Google-like agenda: events by id (a re-sent id answers the existing event, Google's 409
    read as success), the day picker's reads (every day open, the same `times` each day) and a
    call log (`log`) to pin the ORDER of the agenda calls."""

    def __init__(self, times: tuple[str, ...] = ("10:00", "11:00")) -> None:
        self.tzinfo = TZ
        self.times = times
        self.events: dict[str, dict] = {}
        self.created: list[dict] = []
        self.cancelled: list[str] = []
        self.updated: list[tuple] = []
        self.log: list[str] = []
        self.fail_read = False
        self.fail_create: Exception | None = None
        self.on_create = None  # async hook run inside create_event (races)
        # Google (default) refuses a deleted id ("event id is taken"); the secretarIA agenda
        # (`local_style`) hands the cancelled row back.
        self.local_style = False

    def add(self, event_id: str, start: datetime, end: datetime, status: str = "confirmed") -> None:
        self.events[event_id] = {
            "id": event_id,
            "start": _utc(start),
            "end": _utc(end),
            "status": status,
        }

    def live(self) -> dict[str, dict]:
        return {k: v for k, v in self.events.items() if v["status"] != "cancelled"}

    def _overlapping(self, start, end, ignore=None) -> list[dict]:
        return [
            dict(e)
            for e in self.events.values()
            if e["status"] != "cancelled"
            and e["id"] != ignore
            and e["start"] < _utc(end)
            and e["end"] > _utc(start)
        ]

    # --- day picker ---
    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.log.append("days")
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(1, days + 1)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        self.log.append("slots")
        date = (day.date() if hasattr(day, "date") else day).isoformat()
        return [
            {"start": f"{date}T{hour}", "end": f"{date}T{hour}", "label": hour}
            for hour in self.times
        ]

    # --- reads ---
    async def has_conflict(self, start, end, ignore_event_id=None):
        self.log.append("conflict")
        if self.fail_read:
            raise CalendarUnavailableError("down")
        return bool(self._overlapping(start, end, ignore_event_id))

    async def check_availability(self, start, end, max_results=50):
        self.log.append("check")
        if self.fail_read:
            raise CalendarUnavailableError("down")
        return self._overlapping(start, end)

    async def is_slot_free(self, start, end, *, ignore_event_id=None):
        return not self._overlapping(start, end, ignore_event_id)

    async def get_event(self, event_id):
        return self.events.get(event_id)

    # --- writes ---
    async def create_event(
        self, start, end, summary, description="", reminders=None, *, event_id=None
    ):
        self.log.append("create")
        if self.fail_create is not None:
            raise self.fail_create
        if self.on_create is not None:
            await self.on_create()
        event_id = event_id or f"evt-{uuid4().hex[:8]}"
        existing = self.events.get(event_id)
        if existing is not None and existing["status"] == "cancelled":
            if self.local_style:
                return dict(existing)
            # services/calendar.py::CalendarService.create_event on a 409 whose event is cancelled.
            raise CalendarUnavailableError("event id is taken")
        if existing is not None:  # Google's 409 read as success: the existing event
            return {**existing, "htmlLink": f"https://cal/{event_id}"}
        self.add(event_id, start, end)
        self.created.append(
            {
                "id": event_id,
                "start": _utc(start),
                "end": _utc(end),
                "summary": summary,
                "description": description,
            }
        )
        return {"id": event_id, "status": "confirmed", "htmlLink": f"https://cal/{event_id}"}

    async def cancel_event(self, event_id):
        self.log.append("cancel")
        self.cancelled.append(event_id)
        event = self.events.get(event_id)
        if event is None or event["status"] == "cancelled":
            return False
        event["status"] = "cancelled"
        return True

    async def update_event(self, event_id, start, end, **kwargs):
        self.updated.append((event_id, start, end))
        return {"id": event_id}

    async def update_event_details(self, event_id, start, end, summary, description="", **kw):
        self.updated.append((event_id, start, end))
        return {"id": event_id}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------------------------
# UA2's frozen surface (mapa-u §4.4), stubbed until the engine lands
# --------------------------------------------------------------------------------------------


def ua2_landed() -> bool:
    return "other" in typing.get_args(service_change.Choice)


REBOOK_TEXT = "Certo! Escolha o novo dia da sua consulta ({old}). O sinal pago continua valendo."
SLOT_TAKEN_TEXT = (
    "O horário da consulta antiga acabou de ser ocupado. Você pode escolher outro horário, "
    "reenviar o Pix ou cancelar."
)
RESTORE_FAILED_TEXT = "Não consegui devolver sua consulta agora. Tente de novo em instantes."


@pytest.fixture
def ua2(monkeypatch):
    """The parts of UA2's contract this slice calls (mapa-u §4.4/§4.6), only while missing."""
    if ua2_landed():
        return
    sc = service_change
    for name, value in {
        "ACTION_CHOICE_OTHER": OTHER,
        "LABEL_CHOICE_OTHER": LABEL_OTHER,
        "CHOICE_OTHER": "other",
        "LABEL_CANCEL_CONSULTATION": "Cancelar consulta",
    }.items():
        monkeypatch.setattr(sc, name, value, raising=False)
    choice_actions = frozenset(sc.CHOICE_ACTIONS | {OTHER})
    for module in (sc, actions, inbound):
        monkeypatch.setattr(module, "CHOICE_ACTIONS", choice_actions)
    monkeypatch.setattr(sc, "CHOICE_OF_ACTION", {**sc.CHOICE_OF_ACTION, OTHER: "other"})

    def choice_buttons(adjustment_id, *, resend=True, old_slot_free=True):
        first = (
            (sc.ACTION_CHOICE_KEEP, sc.LABEL_CHOICE_KEEP) if old_slot_free else (OTHER, LABEL_OTHER)
        )
        pairs = [first]
        if resend:
            pairs.append((sc.ACTION_CHOICE_RESEND, sc.LABEL_CHOICE_RESEND))
        pairs.append((sc.ACTION_CHOICE_BOTH, sc.LABEL_CHOICE_BOTH))
        return [(f"{action}|{adjustment_id}", label) for action, label in pairs]

    def lost_slot_buttons(adjustment_id):
        return [
            (f"{OTHER}|{adjustment_id}", LABEL_OTHER),
            (f"{sc.ACTION_CHOICE_BOTH}|{adjustment_id}", "Cancelar consulta"),
        ]

    real_answer = sc.choice_answer_text

    def choice_answer_text(outcome):
        texts = {
            "rebook": REBOOK_TEXT.format(old=outcome.old_service or "a consulta antiga"),
            "slot_taken": SLOT_TAKEN_TEXT,
            "restore_failed": RESTORE_FAILED_TEXT,
        }
        return texts.get(outcome.kind) or real_answer(outcome)

    real_choose = sc.choose_after_expiry

    async def choose_after_expiry(session, *, tenant_id, adjustment_id, choice, now):
        if choice != "other":
            return await real_choose(
                session, tenant_id=tenant_id, adjustment_id=adjustment_id, choice=choice, now=now
            )
        adjustment = await session.scalar(
            select(DepositAdjustment).where(
                DepositAdjustment.id == adjustment_id, DepositAdjustment.tenant_id == tenant_id
            )
        )
        if adjustment is None or adjustment.mode != MODE_SUSPEND:
            return ChoiceOutcome(kind="not_found")
        base = {
            "adjustment_id": adjustment.id,
            "appointment_id": adjustment.appointment_id,
            "diff_cents": adjustment.diff_cents,
            "old_service": adjustment.old_service,
            "new_service": adjustment.new_service,
        }
        expired = adjustment.status == STATUS_AWAITING_CHOICE and (
            adjustment.expires_at is None or _utc(now) >= _utc(adjustment.expires_at)
        )
        if adjustment.status not in REBOOKABLE_STATUSES or expired:
            return ChoiceOutcome(kind="already_decided", **base)
        await booking_hold.release_difference_hold_in(session, adjustment.id)
        return ChoiceOutcome(kind="rebook", **base)

    monkeypatch.setattr(sc, "choice_buttons", choice_buttons)
    monkeypatch.setattr(sc, "lost_slot_buttons", lost_slot_buttons, raising=False)
    monkeypatch.setattr(sc, "choice_answer_text", choice_answer_text)
    monkeypatch.setattr(sc, "choose_after_expiry", choose_after_expiry)


# --------------------------------------------------------------------------------------------
# The world: a "before" clinic, a doctor, a PAID sinal, a suspended change of the consultation
# --------------------------------------------------------------------------------------------


@pytest.fixture
def agenda(monkeypatch, db):  # noqa: F811
    cal = Agenda()
    wire(monkeypatch, db, calendar=cal)

    async def _resolve(*args, **kwargs):
        return cal

    monkeypatch.setattr(workers_ns, "resolve_professional_calendar", _resolve)
    monkeypatch.setattr(service_change, "calendar_for_appointment", _resolve)
    monkeypatch.setattr(old_slot, "async_session_factory", db)

    async def _no_holds(*args, **kwargs):
        return []

    monkeypatch.setattr(fr, "_hold_windows", _no_holds)
    return cal


async def ua3_world(
    db,  # noqa: F811
    *,
    channel: str | None = None,
    status: str = STATUS_AWAITING_CHOICE,
    old_slot_state: str | None = OLD_SLOT_RELEASED,
    expires_in: timedelta = timedelta(hours=2),
    mode: str | None = MODE_SUSPEND,
    with_hold: bool = True,
):
    """(world, adjustment id): the old consultation (Consulta, +3 days, 30 min, `evt-old`) of a
    change to Avaliação +4 days that was suspended and whose Pix expired."""
    kwargs = {"channel": channel} if channel else {}
    world = await seed_world(
        db,
        start_at=datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(days=3),
        google_event_id="evt-old",
        professional_name="Dra. Ana",
        phone_number_id=f"pnid-{uuid4().hex[:8]}",  # two clinics may live in one test
        **kwargs,
    )
    await consent(db, world)
    await seed_paid_deposit(db, world)
    new_start = world.start_at + timedelta(days=1)
    adjustment_id = uuid4()
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.pix_deposit_enabled = True
        tenant.pix_deposit_percent = 30
        tenant.deposit_timing = "before"
        tenant.pix_reschedule_limit = 2
        tenant.deposit_hold_ttl_minutes = 30
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == world.appointment.id)
        )
        session.add(
            DepositAdjustment(
                id=adjustment_id,
                tenant_id=tenant.id,
                deposit_id=deposit.id,
                appointment_id=world.appointment.id,
                kind="increase",
                mode=mode,
                status=status,
                old_service="Consulta",
                new_service="Avaliação",
                diff_cents=5000,
                provider_charge_id=f"pay_old_{adjustment_id.hex[:8]}",
                expires_at=datetime.now(UTC) + expires_in,
                asked_at=datetime.now(UTC) if status == STATUS_AWAITING_CHOICE else None,
                old_slot=old_slot_state,
                old_slot_at=datetime.now(UTC) if old_slot_state else None,
                pending_edit={
                    "appointment_type": "Avaliação",
                    "start_at": new_start.isoformat(),
                    "end_at": (new_start + timedelta(minutes=30)).isoformat(),
                },
            )
        )
        await session.flush()
        if with_hold:
            await booking_hold.place_difference_hold_in(
                session,
                tenant_id=tenant.id,
                adjustment_id=adjustment_id,
                patient_id=world.patient.id,
                professional_id=world.appointment.professional_id,
                unit_id=None,
                appointment_type="Avaliação",
                insurance=None,
                attendee_name=None,
                start_at=new_start,
                end_at=new_start + timedelta(minutes=30),
                expires_at=datetime.now(UTC) + expires_in + timedelta(minutes=15),
            )
        await session.commit()
        await session.refresh(tenant)
        world.tenant = tenant
    return world, adjustment_id


def reply_of(world, channel: str | None = None):
    if channel == CHANNEL_BRAIN_MESSAGE:
        return tasks._ReplyContext(
            conversation_id=world.conversation.id,
            patient_ref=world.patient.external_id,
            inbound_body="",
            channel=CHANNEL_BRAIN_MESSAGE,
        )
    return tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )


async def adjustment_of(db, adjustment_id) -> DepositAdjustment:  # noqa: F811
    async with db() as session:
        return await session.get(DepositAdjustment, adjustment_id)


async def holds(db) -> list[BookingHold]:  # noqa: F811
    async with db() as session:
        return list((await session.scalars(select(BookingHold))).all())


def wa_messages() -> list[tuple]:
    return [item for item in sent() if item[0] in ("text", "buttons", "list")]


async def portal_card(world, body: str, buttons: list[tuple[str, str]]) -> None:
    """A card the Portal recorded with its options (what the engine's follow-up sends)."""
    client = _reply_sender(reply_of(world, CHANNEL_BRAIN_MESSAGE), world.tenant, None)
    await client.send_buttons(world.patient.external_id, body, buttons)


async def portal_tap(world, button_id: str, title: str):
    reply = await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text=title,
        interactive_reply_id=button_id,
    )
    if reply is not None:
        await _send_bot_reply(reply)
    return reply


# --------------------------------------------------------------------------------------------
# load_upcoming_appointments(rebooking_id=)
# --------------------------------------------------------------------------------------------


async def _upcoming(db, world, rebooking_id=None, *, patient_id=None, tenant_id=None):  # noqa: F811
    async with db() as session:
        return await patient_context.load_upcoming_appointments(
            session,
            tenant_id or world.tenant.id,
            patient_id or world.patient.id,
            rebooking_id=rebooking_id,
        )


async def test_without_a_rebooking_id_the_list_is_todays(db, agenda):  # noqa: F811
    """Characterization: a suspended consultation stays hidden; a visible one has no new keys."""
    world, _adjustment_id = await ua3_world(db)
    assert await _upcoming(db, world) == []
    async with db() as session:
        await session.execute(update(DepositAdjustment).values(status=STATUS_KEPT_OLD))
        await session.commit()
    [row] = await _upcoming(db, world)
    assert "rebook_adjustment_id" not in row and "rebook_tenant_id" not in row
    # The id of a visible consultation changes nothing either.
    assert await _upcoming(db, world, world.appointment.id) == [row]


@pytest.mark.parametrize("status", [STATUS_AWAITING_CHOICE, STATUS_OLD_SLOT_LOST])
async def test_the_consultation_being_rebooked_comes_back_marked(db, agenda, status):  # noqa: F811
    world, adjustment_id = await ua3_world(db, status=status)
    [row] = await _upcoming(db, world, world.appointment.id)
    assert row["id"] == str(world.appointment.id)
    assert row["rebook_adjustment_id"] == str(adjustment_id)
    assert row["rebook_tenant_id"] == str(world.tenant.id)
    assert row["google_event_id"] == "evt-old"
    assert row["professional_id"] == str(world.appointment.professional_id)


async def test_the_rebooked_consultation_is_listed_in_date_order(db, agenda):  # noqa: F811
    world, adjustment_id = await ua3_world(db)
    async with db() as session:
        for days in (1, 6):
            session.add(
                Appointment(
                    id=uuid4(),
                    tenant_id=world.tenant.id,
                    patient_id=world.patient.id,
                    conversation_id=world.conversation.id,
                    google_event_id=f"evt-{days}",
                    appointment_type="Retorno",
                    start_at=datetime.now(UTC) + timedelta(days=days),
                    end_at=datetime.now(UTC) + timedelta(days=days, minutes=30),
                    status=AppointmentStatus.SCHEDULED,
                )
            )
        await session.commit()
    rows = await _upcoming(db, world, world.appointment.id)
    assert [row["google_event_id"] for row in rows] == ["evt-1", "evt-old", "evt-6"]
    assert [bool(row.get("rebook_adjustment_id")) for row in rows] == [False, True, False]


@pytest.mark.parametrize(
    "status", [STATUS_AWAITING_PAYMENT, STATUS_RESTORING, STATUS_KEPT_OLD, "rebooked", "cancelled"]
)
async def test_a_change_that_is_not_rebookable_adds_nothing(db, agenda, status):  # noqa: F811
    world, _adjustment_id = await ua3_world(db, status=status)
    rows = await _upcoming(db, world, world.appointment.id)
    assert all("rebook_adjustment_id" not in row for row in rows)
    if status in (STATUS_AWAITING_PAYMENT, STATUS_RESTORING):
        assert rows == []  # still suspended: hidden


async def test_a_legacy_or_applied_change_is_never_rebooked(db, agenda):  # noqa: F811
    for mode in (None, MODE_APPLIED):
        world, _adjustment_id = await ua3_world(db, mode=mode)
        rows = await _upcoming(db, world, world.appointment.id)
        assert all("rebook_adjustment_id" not in row for row in rows)


async def test_another_patients_or_clinics_id_adds_nothing(db, agenda):  # noqa: F811
    world, _adjustment_id = await ua3_world(db)
    other, _other_adjustment = await ua3_world(db)
    # Another patient's consultation, asked from this patient's turn.
    assert await _upcoming(db, world, other.appointment.id) == []
    # The right consultation under another clinic's id.
    assert await _upcoming(db, world, world.appointment.id, tenant_id=other.tenant.id) == []


async def test_a_cancelled_or_past_consultation_is_never_rebooked(db, agenda):  # noqa: F811
    world, _adjustment_id = await ua3_world(db)
    async with db() as session:
        row = await session.get(Appointment, world.appointment.id)
        row.status = AppointmentStatus.CANCELLED
        await session.commit()
    assert await _upcoming(db, world, world.appointment.id) == []
    async with db() as session:
        row = await session.get(Appointment, world.appointment.id)
        row.status = AppointmentStatus.SCHEDULED
        row.start_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()
    assert await _upcoming(db, world, world.appointment.id) == []


# --------------------------------------------------------------------------------------------
# A suspended edit: the old event leaves the agenda AFTER the commit (D-R10)
# --------------------------------------------------------------------------------------------


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    from secretaria.services.payments import registry
    from secretaria.services.payments.provider import ChargeState, CreatedCharge, PaymentStatus

    fake = FakeProvider(state=ChargeState(status=PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(provider_charge_id="pay_diff_1", copy_paste="000201pix")
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def _confirm_edit(world, edit, channel=None):
    from secretaria.ai.formatter import TextBubble
    from secretaria.services import appointment_edit as ae

    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[
            TextBubble(body=f"{ae.EDIT_APPLIED}\n\nServiço: {edit['appointment_type']}"),
            fr.MenuBubble(body="menu", labels=["Agendar", "Outro"]),
        ],
        flow_state=FlowState.MENU,
        appointment_edit=edit,
    )
    await tasks._apply_flow_result(
        reply_of(world, channel), result, WA_ID, tenant=world.tenant, waba_token="t"
    )


async def _paid_world_with_agenda(db, monkeypatch, *, timing: str, channel=None):  # noqa: F811
    wire(monkeypatch, db)
    world = await _paid_world(db, channel=channel)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.deposit_timing = timing
        await session.commit()
        await session.refresh(tenant)
        world.tenant = tenant
    cal = Agenda()
    cal.add("evt-old", world.start_at, world.start_at + timedelta(minutes=30))

    async def _resolve(*args, **kwargs):
        return cal

    monkeypatch.setattr(service_change, "calendar_for_appointment", _resolve)
    monkeypatch.setattr(old_slot, "async_session_factory", db)
    return world, cal


@pytest.mark.parametrize("channel", [None, CHANNEL_BRAIN_MESSAGE])
async def test_a_suspended_edit_takes_the_old_event_off_the_agenda_after_the_commit(
    db,  # noqa: F811
    provider,
    monkeypatch,
    channel,
):
    world, cal = await _paid_world_with_agenda(db, monkeypatch, timing="before", channel=channel)
    seen: list[tuple] = []
    real_vacate = old_slot.vacate_old_slot

    async def _vacate(tenant_id, adjustment_id):
        # Runs AFTER the commit: a fresh session already sees the change.
        async with db() as session:
            committed = await session.get(DepositAdjustment, adjustment_id)
        seen.append((tenant_id, adjustment_id, committed is not None))
        return await real_vacate(tenant_id, adjustment_id)

    monkeypatch.setattr(old_slot, "vacate_old_slot", _vacate)

    await _confirm_edit(world, _edit(world, "Avaliação", 45, EditCalendar()), channel)

    [adjustment] = await _all_adjustments(db)
    assert (adjustment.mode, adjustment.status) == (MODE_SUSPEND, STATUS_AWAITING_PAYMENT)
    assert seen == [(world.tenant.id, adjustment.id, True)]
    assert adjustment.old_slot == OLD_SLOT_RELEASED
    assert cal.cancelled == ["evt-old"] and "evt-old" not in cal.live()
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.google_event_id) == ("Consulta", "evt-old")  # row intact


async def _all_adjustments(db) -> list[DepositAdjustment]:  # noqa: F811
    async with db() as session:
        return list((await session.scalars(select(DepositAdjustment))).all())


async def test_an_after_clinic_change_never_vacates(db, provider, monkeypatch):  # noqa: F811
    """Characterization: D-R5 (the change stands now) keeps today's agenda handling."""
    world, cal = await _paid_world_with_agenda(db, monkeypatch, timing="after")
    calls = []

    async def _vacate(*args):
        calls.append(args)
        return "not_applicable"

    monkeypatch.setattr(old_slot, "vacate_old_slot", _vacate)
    await _confirm_edit(world, _edit(world, "Avaliação", 45, EditCalendar()))
    [adjustment] = await _all_adjustments(db)
    assert adjustment.mode == MODE_APPLIED
    assert calls == [] and cal.cancelled == []


async def test_a_blocked_or_cheaper_change_never_vacates(db, provider, monkeypatch):  # noqa: F811
    world, cal = await _paid_world_with_agenda(db, monkeypatch, timing="before")
    calls = []

    async def _vacate(*args):
        calls.append(args)
        return "not_applicable"

    monkeypatch.setattr(old_slot, "vacate_old_slot", _vacate)
    # A cheaper service: the edit is written now (asked / applied), never suspended.
    await _confirm_edit(world, _edit(world, "Retorno", 20, EditCalendar()))
    assert calls == [] and cal.cancelled == []


def test_applied_edit_carries_the_change_to_vacate_only_when_suspended():
    """`AppliedEdit.vacate_adjustment_id` defaults to None (every edit of before UA3)."""
    applied = apply_module.AppliedEdit(
        appointment_id=uuid4(), moved=False, old_event_id=None, old_professional_id=None
    )
    assert applied.vacate_adjustment_id is None


async def test_finish_edit_vacates_then_deletes_a_doctor_changes_old_event(monkeypatch):
    calls: list[tuple] = []

    async def _vacate(tenant_id, adjustment_id):
        calls.append(("vacate", tenant_id, adjustment_id))
        raise RuntimeError("defence: a raise never escapes")

    monkeypatch.setattr(old_slot, "vacate_old_slot", _vacate)
    tenant = SimpleNamespace(id=uuid4())
    adjustment_id = uuid4()
    await apply_module._finish_edit(
        tenant,
        apply_module.AppliedEdit(
            appointment_id=uuid4(),
            moved=False,
            old_event_id=None,
            old_professional_id=None,
            vacate_adjustment_id=adjustment_id,
        ),
    )
    assert calls == [("vacate", tenant.id, adjustment_id)]


# --------------------------------------------------------------------------------------------
# "Manter a antiga": restore_old_slot(trigger="tap")
# --------------------------------------------------------------------------------------------


@pytest.fixture
def replans(monkeypatch) -> list:
    calls: list = []

    async def _followup(tenant_id, appointment_id):
        calls.append(appointment_id)

    monkeypatch.setattr(service_change, "followup_kept_old", _followup)
    return calls


async def test_keep_with_the_old_slot_free_brings_the_consultation_back(
    db,  # noqa: F811
    agenda,
    ua2,
    replans,
):
    world, adjustment_id = await ua3_world(db)

    await tasks._handle_action_button(reply_of(world), "svcdiffkeep", str(adjustment_id))

    adjustment = await adjustment_of(db, adjustment_id)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_KEPT_OLD, OLD_SLOT_RESTORED)
    [created] = agenda.created
    assert created["id"] == old_slot.restore_event_id(adjustment_id, calendar=agenda)
    assert created["start"] == world.start_at
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == created["id"]
    assert await holds(db) == []  # the new window is free again
    [(kind, _to, body)] = wa_messages()
    assert kind == "text"
    assert body == service_change.choice_answer_text(
        ChoiceOutcome(kind="kept_old", old_service="Consulta")
    )
    assert replans == [world.appointment.id]


async def test_keep_with_the_old_slot_taken_offers_another_time(db, agenda, ua2, replans):  # noqa: F811
    world, adjustment_id = await ua3_world(db)
    agenda.add("evt-someone", world.start_at, world.start_at + timedelta(minutes=30))

    await tasks._handle_action_button(reply_of(world), "svcdiffkeep", str(adjustment_id))

    adjustment = await adjustment_of(db, adjustment_id)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_AWAITING_CHOICE, OLD_SLOT_RELEASED)
    assert agenda.created == [] and len(await holds(db)) == 1  # nothing changed
    [(kind, _to, body, buttons)] = wa_messages()
    assert kind == "buttons"
    assert body == service_change.choice_answer_text(
        ChoiceOutcome(kind="slot_taken", adjustment_id=adjustment_id)
    )
    assert buttons == service_change.choice_buttons(adjustment_id, old_slot_free=False)
    assert buttons[0] == (f"{OTHER}|{adjustment_id}", LABEL_OTHER)
    assert [label for _id, label in buttons] == [LABEL_OTHER, "Reenviar o Pix", "Cancelar ambas"]
    assert replans == []


async def test_keep_with_the_agenda_down_changes_nothing_and_asks_again(
    db,  # noqa: F811
    agenda,
    ua2,
    replans,
):
    world, adjustment_id = await ua3_world(db)
    agenda.fail_read = True

    await tasks._handle_action_button(reply_of(world), "svcdiffkeep", str(adjustment_id))

    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE
    [(kind, _to, body, buttons)] = wa_messages()
    assert kind == "buttons"
    assert body == service_change.choice_answer_text(
        ChoiceOutcome(kind="restore_failed", adjustment_id=adjustment_id)
    )
    assert buttons == service_change.choice_buttons(adjustment_id)
    assert replans == []


async def test_keep_after_the_deadline_is_already_decided(db, agenda, ua2, replans):  # noqa: F811
    world, adjustment_id = await ua3_world(db, expires_in=timedelta(minutes=-1))

    await tasks._handle_action_button(reply_of(world), "svcdiffkeep", str(adjustment_id))

    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE
    assert [item[2] for item in wa_messages()] == [
        service_change.choice_answer_text(ChoiceOutcome(kind="already_decided"))
    ]
    assert agenda.created == [] and replans == []


async def test_keep_of_an_event_that_never_left_is_only_the_transition(
    db,  # noqa: F811
    agenda,
    ua2,
    replans,
):
    """Characterization: `old_slot` NULL (the vacate never ran) - today's keep, no agenda call."""
    world, adjustment_id = await ua3_world(db, old_slot_state=None)

    await tasks._handle_action_button(reply_of(world), "svcdiffkeep", str(adjustment_id))

    assert (await adjustment_of(db, adjustment_id)).status == STATUS_KEPT_OLD
    assert agenda.created == [] and agenda.log == []
    assert (await reload_appointment(db, world.appointment.id)).google_event_id == "evt-old"
    assert replans == [world.appointment.id]


async def test_keep_of_another_patient_changes_nothing(db, agenda, ua2, replans):  # noqa: F811
    world, adjustment_id = await ua3_world(db)
    async with db() as session:
        stranger = Patient(id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900000000")
        session.add(stranger)
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=world.tenant.id, patient_id=stranger.id)
        session.add(conversation)
        await session.commit()

    await tasks._handle_action_button(
        reply_of(SimpleNamespace(conversation=conversation)), "svcdiffkeep", str(adjustment_id)
    )

    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE
    assert agenda.created == []
    assert [item[2] for item in wa_messages()] == ["Não encontrei essa troca de consulta."]


async def test_a_portal_keep_with_the_slot_taken_records_the_other_time_card(
    db,  # noqa: F811
    agenda,
    ua2,
    replans,
):
    world, adjustment_id = await ua3_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    agenda.add("evt-someone", world.start_at, world.start_at + timedelta(minutes=30))
    await portal_card(world, "escolha", service_change.choice_buttons(adjustment_id))

    reply = await portal_tap(world, f"svcdiffkeep|{adjustment_id}", "Manter a antiga")

    assert reply.action_button == ("svcdiffkeep", str(adjustment_id))
    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE
    cards = [m for m in await outbound_messages(db, world.conversation.id) if m.interactive]
    assert [o["id"] for o in cards[-1].interactive["options"]] == [
        button_id
        for button_id, _label in service_change.choice_buttons(adjustment_id, old_slot_free=False)
    ]
    # "Outro horário" from THAT card is an action on this channel (its id was on the card).
    reply = await portal_tap(world, f"{OTHER}|{adjustment_id}", LABEL_OTHER)
    assert reply.action_button == (OTHER, str(adjustment_id))
    assert (await get_conversation(db, world)).flow_state == FlowState.MANAGE_BOOKING


async def test_a_portal_keep_with_the_slot_free_restores(db, agenda, ua2, replans):  # noqa: F811
    world, adjustment_id = await ua3_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    await portal_card(world, "escolha", service_change.choice_buttons(adjustment_id))

    await portal_tap(world, f"svcdiffkeep|{adjustment_id}", "Manter a antiga")

    assert (await adjustment_of(db, adjustment_id)).status == STATUS_KEPT_OLD
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert bodies[-1] == service_change.kept_old_notice_text("Consulta")
    assert replans == [world.appointment.id]


# --------------------------------------------------------------------------------------------
# "Outro horário": the reschedule flow on the old consultation
# --------------------------------------------------------------------------------------------


def _day_picker(messages) -> tuple:
    return next(item for item in messages if item[0] == "list")


@pytest.mark.parametrize("status", [STATUS_AWAITING_CHOICE, STATUS_OLD_SLOT_LOST])
async def test_other_time_opens_the_day_picker_on_the_old_consultation(
    db,  # noqa: F811
    agenda,
    ua2,
    status,
):
    world, adjustment_id = await ua3_world(db, status=status)

    await tasks._handle_action_button(reply_of(world), OTHER, str(adjustment_id))

    adjustment = await adjustment_of(db, adjustment_id)
    assert adjustment.status == status  # nothing decided yet: only the new window is released
    assert await holds(db) == []
    messages = wa_messages()
    assert messages[0][:3] == (
        "text",
        WA_ID,
        service_change.choice_answer_text(
            ChoiceOutcome(kind="rebook", old_service="Consulta", adjustment_id=adjustment_id)
        ),
    )
    assert _day_picker(messages)  # the day list follows in the same reply
    conversation = await get_conversation(db, world)
    assert conversation.flow_state == FlowState.MANAGE_BOOKING
    assert conversation.flow_managing_appointment_id == world.appointment.id
    assert conversation.flow_step == fr.STEP_MANAGE_DAY
    # Nothing on the agenda yet; the reschedule limit is not used.
    assert agenda.created == []
    async with db() as session:
        deposit = await session.scalar(select(PixDeposit))
    assert (deposit.reschedule_count or 0) == 0


async def test_other_time_ignores_the_reschedule_limit(db, agenda, ua2):  # noqa: F811
    """mapa-u §7.6: "Outro horário" is neither blocked nor counted by the sinal's limit."""
    world, adjustment_id = await ua3_world(db)
    async with db() as session:
        await session.execute(update(PixDeposit).values(reschedule_count=2))
        await session.commit()

    await tasks._handle_action_button(reply_of(world), OTHER, str(adjustment_id))

    messages = wa_messages()
    assert _day_picker(messages)
    assert not any("restam" in str(item[2]) for item in messages)
    assert not any("limite" in str(item[2]).lower() for item in messages)
    assert (await get_conversation(db, world)).flow_state == FlowState.MANAGE_BOOKING


async def test_other_time_after_the_change_was_decided_is_one_line(db, agenda, ua2):  # noqa: F811
    world, adjustment_id = await ua3_world(db, status=STATUS_KEPT_OLD)

    await tasks._handle_action_button(reply_of(world), OTHER, str(adjustment_id))

    assert [item[2] for item in wa_messages()] == [
        service_change.choice_answer_text(ChoiceOutcome(kind="already_decided"))
    ]
    assert (await get_conversation(db, world)).flow_state != FlowState.MANAGE_BOOKING


async def test_other_time_of_another_patient_changes_nothing(db, agenda, ua2):  # noqa: F811
    world, adjustment_id = await ua3_world(db)
    other_world, _other_adjustment = await ua3_world(db)

    await tasks._handle_action_button(reply_of(other_world), OTHER, str(adjustment_id))

    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE
    assert len(await holds(db)) == 2
    assert [item[2] for item in wa_messages()] == ["Não encontrei essa troca de consulta."]


async def test_other_time_whose_consultation_vanished_is_already_decided(
    db,  # noqa: F811
    agenda,
    ua2,
    monkeypatch,
):
    """Decided between the engine's answer and the read of the consultation: one line."""
    world, adjustment_id = await ua3_world(db)

    async def _gone(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "secretaria.workers.shared.service_change_actions.load_upcoming_appointments", _gone
    )
    await tasks._handle_action_button(reply_of(world), OTHER, str(adjustment_id))

    assert [item[2] for item in wa_messages()] == [
        service_change.choice_answer_text(ChoiceOutcome(kind="already_decided"))
    ]


async def test_a_lost_slot_card_on_the_portal_routes_other_time(db, agenda, ua2):  # noqa: F811
    world, adjustment_id = await ua3_world(
        db, channel=CHANNEL_BRAIN_MESSAGE, status=STATUS_OLD_SLOT_LOST
    )
    await portal_card(world, "ocupado", service_change.lost_slot_buttons(adjustment_id))

    reply = await portal_tap(world, f"{OTHER}|{adjustment_id}", LABEL_OTHER)

    assert reply.action_button == (OTHER, str(adjustment_id))
    conversation = await get_conversation(db, world)
    assert conversation.flow_state == FlowState.MANAGE_BOOKING
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    # The answer, then the day picker - the last two messages of the turn.
    assert bodies[-2] == service_change.choice_answer_text(
        ChoiceOutcome(kind="rebook", old_service="Consulta")
    )
    assert bodies[-1].startswith("Para quando você gostaria de remarcar?")


async def test_a_forged_portal_other_time_does_nothing(db, agenda, ua2):  # noqa: F811
    """No card of this conversation offered that id: never an action."""
    world, adjustment_id = await ua3_world(db, channel=CHANNEL_BRAIN_MESSAGE)

    reply = await portal_tap(world, f"{OTHER}|{adjustment_id}", LABEL_OTHER)

    assert reply is None or reply.action_button is None
    assert len(await holds(db)) == 1
    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE


def test_other_time_id_fits_whatsapp():
    assert len(f"{OTHER}|{uuid4()}") <= 256
    assert len(LABEL_OTHER) <= 20


# --------------------------------------------------------------------------------------------
# Fix round 1 (reviews/UA3-review.md): I2 the AI, M1 a lost slot after its time, M2 the discard
# --------------------------------------------------------------------------------------------


async def test_the_ai_never_sees_the_consultation_being_moved(db, agenda, ua2, monkeypatch):  # noqa: F811
    """I2 (reviewer probe): free text at the confirmation card goes to the AI; the vacated
    consultation (its old slot often someone else's now) is never in its appointment context."""
    from tests._reminders_r3 import turn
    from tests.test_sinal_trocas_ua3_rebook import wa_rebook_to_first_slot

    world, adjustment_id = await ua3_world(db)
    await wa_rebook_to_first_slot(db, world, adjustment_id)
    seen: dict = {}

    async def _agent(message, *args, **kwargs):
        seen["context"] = kwargs.get("appointment_context")
        return "Resposta da IA."

    monkeypatch.setattr(workers_ns, "run_agent", _agent)

    await turn(db, world, "pode ser mais cedo?")

    assert "context" in seen  # the AI did answer this turn
    assert not seen["context"]


async def test_the_ai_still_sees_the_other_consultations(db, agenda, ua2, monkeypatch):  # noqa: F811
    """Characterization: only the marked row is dropped - a visible consultation still reaches
    the AI on the same delegated turn."""
    from tests._reminders_r3 import turn
    from tests.test_sinal_trocas_ua3_rebook import wa_rebook_to_first_slot

    world, adjustment_id = await ua3_world(db)
    async with db() as session:
        start = datetime.now(UTC) + timedelta(days=6)
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=world.tenant.id,
                patient_id=world.patient.id,
                conversation_id=world.conversation.id,
                google_event_id="evt-other",
                appointment_type="Retorno",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()
    await wa_rebook_to_first_slot(db, world, adjustment_id)
    seen: dict = {}

    async def _agent(message, *args, **kwargs):
        seen["context"] = kwargs.get("appointment_context")
        return "Resposta da IA."

    monkeypatch.setattr(workers_ns, "run_agent", _agent)

    await turn(db, world, "pode ser mais cedo?")

    assert "Retorno" in (seen.get("context") or "")
    assert "Consulta" not in seen["context"]


async def _move_old_start_to_the_past(db, world) -> None:  # noqa: F811
    async with db() as session:
        past = datetime.now(UTC) - timedelta(minutes=30)
        await session.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment.id)
            .values(start_at=past, end_at=past + timedelta(minutes=30))
        )
        await session.commit()


async def test_a_lost_slot_is_rebookable_after_its_old_time(db, agenda, ua2):  # noqa: F811
    """M1 (mapa-u §7.4): "Outro horário" of an `old_slot_lost` change is valid until the clinic
    resolves it - even once the old time has passed."""
    world, adjustment_id = await ua3_world(db, status=STATUS_OLD_SLOT_LOST)
    await _move_old_start_to_the_past(db, world)
    [row] = await _upcoming(db, world, world.appointment.id)
    assert row["rebook_adjustment_id"] == str(adjustment_id)

    await tasks._handle_action_button(reply_of(world), OTHER, str(adjustment_id))

    assert _day_picker(wa_messages())
    conversation = await get_conversation(db, world)
    assert conversation.flow_state == FlowState.MANAGE_BOOKING
    assert conversation.flow_managing_appointment_id == world.appointment.id


async def test_a_lost_slot_past_its_old_time_rebooks_end_to_end(db, agenda, ua2):  # noqa: F811
    from tests.test_sinal_trocas_ua3_rebook import wa_confirm, wa_rebook_to_first_slot

    world, adjustment_id = await ua3_world(db, status=STATUS_OLD_SLOT_LOST)
    await _move_old_start_to_the_past(db, world)

    new_start = await wa_rebook_to_first_slot(db, world, adjustment_id)
    await wa_confirm(db, world)

    assert (await adjustment_of(db, adjustment_id)).status == "rebooked"
    row = await reload_appointment(db, world.appointment.id)
    assert row.start_at.replace(tzinfo=None) == new_start.replace(tzinfo=None)


async def test_a_choice_past_the_old_time_keeps_its_deadline(db, agenda, ua2):  # noqa: F811
    """`awaiting_choice` keeps its own deadline: past the old time it is never rebookable."""
    world, _adjustment_id = await ua3_world(db, status=STATUS_AWAITING_CHOICE)
    await _move_old_start_to_the_past(db, world)
    assert await _upcoming(db, world, world.appointment.id) == []


async def test_an_unverified_discard_keeps_the_event(monkeypatch):
    """M2: when the "does the row point at it?" read fails, the event is KEPT (it may be the one a
    twin turn committed) - a leftover is the safe failure."""
    from secretaria.workers.shared import flow_runner

    class _BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("database down")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(flow_runner, "async_session_factory", lambda: _BrokenSession())
    cal = Agenda()
    cal.add("difmovex", datetime.now(UTC), datetime.now(UTC) + timedelta(minutes=30))

    await flow_runner._discard_rebook_event(
        SimpleNamespace(id=uuid4()),
        {"event_id": "difmovex", "calendar": cal, "adjustment_id": uuid4()},
    )

    assert cal.cancelled == [] and "difmovex" in cal.live()
