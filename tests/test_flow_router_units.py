"""The "which unit?" step of the booking flow (TASK-083 spec §8), router level.

No DB, no network: SimpleNamespace snapshots, a fake agenda that records WHICH hours each read
used (`with_unit` returns a copy carrying the unit's hours, like CalendarService.with_unit), and
a fake booking gate. The same `route()` serves WhatsApp (unarmed gate: the booking is written on
"Confirmar") and the Portal (armed gate: a hold + a mailed code); both are exercised.

0 units -> today's flow, byte for byte; 1 unit -> no question, that unit's hours and address;
2 units -> "Em qual unidade?" with fixed rows, and every later step uses the chosen unit.
"""

import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

os.environ.setdefault("APP_ENV", "test")

from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble  # noqa: E402
from secretaria.core.whatsapp_limits import (  # noqa: E402
    MAX_LIST_ROW_DESCRIPTION_CHARS,
    MAX_LIST_ROW_TITLE_CHARS,
)
from secretaria.models import FlowState  # noqa: E402
from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.booking_hold import GateDecision  # noqa: E402
from secretaria.services.flow_units import (  # noqa: E402
    LABEL_ANOTHER_UNIT,
    STEP_AWAITING_UNIT,
    STEP_AWAITING_UNIT_CONFIRM,
    UNIT_CONFIRM_BODY,
    UNIT_GONE_NOTICE,
)
from secretaria.services.unit_booking import UnitOption  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
CLINIC_HOURS = {d: [{"start": "08:00", "end": "18:00"}] for d in ("monday", "tuesday")}
CENTRO_HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}
SUL_HOURS = {"tuesday": [{"start": "14:00", "end": "18:00"}]}


def _tenant():
    return SimpleNamespace(
        initial_flows={"enabled": True},
        appointment_types=[
            {"name": "Consulta", "duration_min": 30, "is_active": True, "sort_order": 0}
        ],
        appointment_duration_min=30,
        business_hours=CLINIC_HOURS,
        collect_insurance=False,
        insurance_mode=None,
        insurances=[],
        timezone="America/Sao_Paulo",
    )


def _units(count: int) -> list[UnitOption]:
    units = [
        UnitOption(id=uuid4(), name="Centro", address="Rua A, 10", business_hours=CENTRO_HOURS),
        UnitOption(id=uuid4(), name="Zona Sul", address="Av. B, 20", business_hours=SUL_HOURS),
    ]
    return units[:count]


def _doctor(units: list | None = None):
    return SimpleNamespace(
        id=uuid4(),
        name="Dra. Ana",
        specialty=None,
        about=None,
        appointment_types=None,
        business_hours=None,
        min_booking_notice_hours=None,
        units=units if units is not None else [],
    )


def _conversation(**kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Consulta",
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        flow_attendee_name="",
        flow_selected_unit_id=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _Log:
    def __init__(self):
        self.day_hours: list = []
        self.slot_hours: list = []
        self.created: list = []
        self.descriptions: list = []


class _Agenda:
    """`with_unit` returns a copy that carries the unit's hours / id / location."""

    def __init__(self, hours=CLINIC_HOURS, *, unit_id=None, location=None, log=None):
        self.tzinfo = TZ
        self.hours = hours
        self.unit_id = unit_id
        self.location = location
        self.log = log or _Log()

    def with_unit(self, unit_id, location, *, business_hours=None):
        return _Agenda(
            business_hours if business_hours is not None else self.hours,
            unit_id=unit_id,
            location=location,
            log=self.log,
        )

    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.log.day_hours.append(self.hours)
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(1, 4)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6, **_kw):
        self.log.slot_hours.append(self.hours)
        start = day.replace(hour=9, minute=0, tzinfo=None)
        return [{"start": start.isoformat(timespec="minutes"), "label": "09:00"}]

    async def has_conflict(self, start, end, ignore_event_id=None):
        return False

    async def create_event(self, start, end, summary, description=""):
        self.log.created.append((self.unit_id, self.location, summary))
        self.log.descriptions.append(description)
        return {"id": "evt-1", "htmlLink": None}

    async def update_event(self, event_id, start, end):
        return {"id": event_id}


class _Gate:
    """An armed (Portal) or unarmed (WhatsApp) gate that records what `decide` received."""

    def __init__(self, *, armed: bool):
        self.armed = armed
        self.deposit_capable = True
        self.decided: list[dict] = []

    async def busy_windows(self, professional_id):
        return []

    async def decide(self, **kwargs):
        self.decided.append(kwargs)
        if self.armed:
            return GateDecision("held", email_masked="m***@x.com", hold_id=uuid4())
        return GateDecision("commit")


def _day_tap(result) -> str:
    """What the router reads for the first day row of a day picker."""
    [bubble] = [b for b in result.bubbles if isinstance(b, SlotsBubble)]
    row_id, title = bubble.rows[0][0], bubble.rows[0][1]
    return inbound_routing_text(title, row_id)


def _unit_tap(unit) -> str:
    return inbound_routing_text(unit.name, f"unit|{unit.id}")


def _back_row(result) -> tuple:
    [bubble] = [b for b in result.bubbles if isinstance(b, SlotsBubble)]
    return bubble.rows[-1]


# --------------------------------------------------------------------------
# 0 units: nothing changes
# --------------------------------------------------------------------------


async def test_without_units_the_service_confirm_opens_the_day_picker_as_today():
    doctor = _doctor()
    agenda = _Agenda()
    result = await fr.route(
        _conversation(), _tenant(), agenda, fr.LABEL_BOOK_SERVICE, professionals=[doctor]
    )
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_unit_id is None
    assert agenda.log.day_hours == [CLINIC_HOURS]
    assert _back_row(result)[1] == fr.LABEL_ANOTHER_SERVICE


# --------------------------------------------------------------------------
# 1 unit: no question, its hours
# --------------------------------------------------------------------------


async def test_one_unit_is_used_without_asking():
    [centro] = _units(1)
    doctor = _doctor([centro])
    agenda = _Agenda()
    result = await fr.route(
        _conversation(), _tenant(), agenda, fr.LABEL_BOOK_SERVICE, professionals=[doctor]
    )
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_unit_id == centro.id
    assert agenda.log.day_hours == [CENTRO_HOURS]
    # Nothing to choose, so one step back is still the service list.
    assert _back_row(result)[1] == fr.LABEL_ANOTHER_SERVICE


async def test_a_doctor_whose_every_unit_is_closed_cannot_be_booked():
    closed = UnitOption(id=uuid4(), name="Centro", address=None, business_hours={})
    doctor = _doctor([closed])
    result = await fr.route(
        _conversation(), _tenant(), _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[doctor]
    )
    assert result.action == "professional_config_incomplete"
    assert result.professional_config_gap == fr.PROFESSIONAL_GAP_HOURS


# --------------------------------------------------------------------------
# 2 units: the question, then the chosen unit everywhere
# --------------------------------------------------------------------------


async def test_two_units_ask_which_one_with_fixed_rows():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    agenda = _Agenda()
    result = await fr.route(
        _conversation(flow_selected_insurance="Particular"),
        _tenant(),
        agenda,
        fr.LABEL_BOOK_SERVICE,
        professionals=[doctor],
    )
    assert result.action == "reply"
    assert result.flow_state is FlowState.SERVICE_CATALOG
    assert result.flow_step == STEP_AWAITING_UNIT
    assert result.flow_selected_unit_id is None
    # The booking so far rides along.
    assert result.flow_selected_type == "Consulta"
    assert result.flow_selected_insurance == "Particular"
    [bubble] = result.bubbles
    assert isinstance(bubble, SlotsBubble)
    assert bubble.rows[0] == (f"unit|{centro.id}", "Centro", "Rua A, 10")
    assert bubble.rows[1] == (f"unit|{sul.id}", "Zona Sul", "Av. B, 20")
    assert bubble.rows[2][1] == fr.LABEL_ANOTHER_SERVICE
    assert agenda.log.day_hours == []  # nothing read before the unit is known


async def test_long_unit_names_and_addresses_fit_the_whatsapp_row_limits():
    long_unit = UnitOption(
        id=uuid4(),
        name="Unidade Avenida Paulista Esquina Consolação",
        address="Avenida Paulista, 1000, conjunto 1203, Bela Vista, São Paulo/SP, CEP 01310-100",
        business_hours=CENTRO_HOURS,
    )
    doctor = _doctor([long_unit, _units(2)[1]])
    result = await fr.route(
        _conversation(), _tenant(), _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[doctor]
    )
    row = result.bubbles[0].rows[0]
    assert len(row[1]) <= MAX_LIST_ROW_TITLE_CHARS
    assert len(row[2]) <= MAX_LIST_ROW_DESCRIPTION_CHARS
    # The tap carries the uuid, so the truncated title still resolves.
    picked = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT),
        _tenant(),
        _Agenda(),
        inbound_routing_text(row[1], row[0]),
        professionals=[doctor],
    )
    assert picked.flow_selected_unit_id == long_unit.id


async def test_picking_a_unit_opens_its_days_with_a_back_row_to_the_units():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    agenda = _Agenda()
    result = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT),
        _tenant(),
        agenda,
        _unit_tap(sul),
        professionals=[doctor],
    )
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert result.flow_selected_unit_id == sul.id
    assert agenda.log.day_hours == [SUL_HOURS]
    assert _back_row(result)[1] == LABEL_ANOTHER_UNIT


async def test_a_typed_unit_name_works_and_talk_goes_to_the_model_keeping_the_step():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    typed = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT),
        _tenant(),
        _Agenda(),
        "zona sul",
        professionals=[doctor],
    )
    assert typed.flow_selected_unit_id == sul.id
    talk = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT),
        _tenant(),
        _Agenda(),
        "qual fica mais perto do metrô?",
        professionals=[doctor],
    )
    assert talk.action == "delegate_llm"
    assert talk.flow_step == STEP_AWAITING_UNIT  # the list stays tappable


async def test_the_chosen_units_hours_drive_the_slot_list_on_the_next_turn():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    agenda = _Agenda()
    days = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT),
        _tenant(),
        agenda,
        _unit_tap(sul),
        professionals=[doctor],
    )
    slots = await fr.route(
        _conversation(flow_step=days.flow_step, flow_selected_unit_id=days.flow_selected_unit_id),
        _tenant(),
        agenda,
        _day_tap(days),
        professionals=[doctor],
    )
    assert slots.flow_step == fr.STEP_AWAITING_SLOT
    assert agenda.log.slot_hours == [SUL_HOURS]
    assert slots.flow_selected_unit_id == sul.id
    assert _back_row(slots)[1] == LABEL_ANOTHER_UNIT


async def test_back_from_the_day_and_the_slot_lists_returns_to_the_unit_list():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    for step in (fr.STEP_AWAITING_DAY, fr.STEP_AWAITING_SLOT):
        back = await fr.route(
            _conversation(flow_step=step, flow_selected_unit_id=sul.id, flow_selected_day="x"),
            _tenant(),
            _Agenda(),
            inbound_routing_text(LABEL_ANOTHER_UNIT, "dayback|unit"),
            professionals=[doctor],
        )
        assert back.flow_step == STEP_AWAITING_UNIT, step
        assert back.flow_selected_unit_id is None
        assert back.flow_selected_type == "Consulta"


async def test_back_from_the_unit_list_goes_to_the_service_list_and_drops_the_unit():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    back = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT, flow_selected_unit_id=sul.id),
        _tenant(),
        _Agenda(),
        inbound_routing_text(fr.LABEL_ANOTHER_SERVICE, "dayback|service"),
        professionals=[doctor],
    )
    assert back.flow_step == fr.STEP_AWAITING_SERVICE
    assert back.flow_selected_unit_id is None


async def test_a_day_step_without_a_chosen_unit_asks_for_it_first():
    """Safety net: a booking that reached the day list without the unit (a doctor linked to a
    second unit mid-booking, a hand-back) is never booked without one."""
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    result = await fr.route(
        _conversation(flow_step=fr.STEP_AWAITING_DAY),
        _tenant(),
        _Agenda(),
        "segunda",
        professionals=[doctor],
    )
    assert result.flow_step == STEP_AWAITING_UNIT


async def test_the_recap_and_the_whatsapp_confirmation_name_the_unit_and_its_address():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    agenda = _Agenda()
    recap = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_SLOT,
            flow_selected_day="2099-01-06",
            flow_selected_unit_id=sul.id,
        ),
        _tenant(),
        agenda,
        "09:00 (2099-01-06T09:00)",
        professionals=[doctor],
    )
    [card] = recap.bubbles
    assert isinstance(card, ButtonBubble)
    assert "Zona Sul — Av. B, 20" in card.body
    assert recap.flow_selected_unit_id == sul.id

    gate = _Gate(armed=False)
    booked = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_CONFIRMATION,
            flow_selected_day="2099-01-06",
            flow_selected_slot="2099-01-06T09:00",
            flow_selected_unit_id=sul.id,
        ),
        _tenant(),
        agenda,
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=[doctor],
        gate=gate,
    )
    assert booked.appointment["unit_id"] == sul.id
    assert agenda.log.created == [(sul.id, "Zona Sul — Av. B, 20", "Consulta - Maria")]
    [text] = [b for b in booked.bubbles if isinstance(b, TextBubble)]
    # Follow-up (owner 2026-11-13): the unit and its address each on their own line.
    assert "Unidade: Zona Sul\nEndereço: Av. B, 20\n" in text.body
    # Final wave F3: the patient's "add to my calendar" link carries the unit's location.
    assert "location=Zona%20Sul%20%E2%80%94%20Av.%20B%2C%2020" in text.body
    assert gate.decided[0]["unit_id"] == sul.id
    # The booking is over: the unit is not carried any further.
    assert booked.flow_selected_unit_id is None


def _link_details(body: str) -> str:
    """The decoded `details` of the patient's add-to-calendar link inside a confirmation."""
    [link] = [line for line in body.splitlines() if line.startswith("https://calendar.google")]
    return parse_qs(urlparse(link).query)["details"][0]


async def _book(units: list, unit_id=None, *, insurance=None):
    doctor = _doctor(units)
    agenda = _Agenda()
    booked = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_CONFIRMATION,
            flow_selected_day="2099-01-06",
            flow_selected_slot="2099-01-06T09:00",
            flow_selected_unit_id=unit_id,
            flow_selected_insurance=insurance,
        ),
        _tenant(),
        agenda,
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=[doctor],
        gate=_Gate(armed=False),
    )
    [text] = [b for b in booked.bubbles if isinstance(b, TextBubble)]
    return agenda, booked, text.body


async def test_the_event_and_the_patient_link_say_unit_address_and_doctor():
    """Owner 2026-11-13: the last message (with the link) makes the unit and the address
    clear, and the event the patient adds from it carries them in its "Detalhes"."""
    centro, sul = _units(2)
    agenda, booked, body = await _book([centro, sul], sul.id, insurance="Particular")
    [description] = agenda.log.descriptions
    assert description == (
        "Serviço: Consulta\n"
        "Profissional: Dra. Ana\n"
        "Unidade: Zona Sul\n"
        "Endereço: Av. B, 20\n"
        "Convênio: Particular"
    )
    # The confirmation names the unit and its address on their own lines.
    assert "Profissional: Dra. Ana\nUnidade: Zona Sul\nEndereço: Av. B, 20\n" in body
    # The link's details (decoded): what the patient's Google Calendar shows.
    assert _link_details(body) == (
        "Serviço: Consulta\n"
        "Profissional: Dra. Ana\n"
        "Data e horário: terça-feira, 06/01/2099 às 09:00\n"
        "Unidade: Zona Sul\n"
        "Endereço: Av. B, 20\n"
        "Convênio: Particular"
    )


async def test_one_unit_without_the_question_still_stamps_its_lines():
    """A doctor with ONE unit is never asked which one - the booking still carries it."""
    [centro] = _units(1)
    agenda, booked, body = await _book([centro])
    assert booked.appointment["unit_id"] == centro.id
    assert "Unidade: Centro\nEndereço: Rua A, 10" in agenda.log.descriptions[0]
    assert "Unidade: Centro\nEndereço: Rua A, 10\n" in body
    assert "Unidade: Centro\nEndereço: Rua A, 10" in _link_details(body)


async def test_without_units_the_event_description_is_todays_and_the_link_gains_details():
    agenda, _booked, body = await _book([])
    assert agenda.log.descriptions == ["Serviço: Consulta\nConvênio: não informado"]
    assert "Unidade" not in body and "Endereço" not in body
    details = _link_details(body)
    assert "Unidade" not in details and "Endereço" not in details
    assert "Data e horário: terça-feira, 06/01/2099 às 09:00" in details
    assert "Profissional: Dra. Ana" in details


async def test_a_booking_without_units_keeps_the_exact_confirmation_dict_and_text():
    doctor = _doctor()
    agenda = _Agenda()
    gate = _Gate(armed=False)
    booked = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_CONFIRMATION,
            flow_selected_day="2099-01-06",
            flow_selected_slot="2099-01-06T09:00",
        ),
        _tenant(),
        agenda,
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=[doctor],
        gate=gate,
    )
    assert "unit_id" not in booked.appointment
    assert "unit_id" not in gate.decided[0]
    assert agenda.log.created == [(None, None, "Consulta - Maria")]
    [text] = [b for b in booked.bubbles if isinstance(b, TextBubble)]
    assert "Local:" not in text.body
    assert "location=" not in text.body


async def test_the_portal_holds_the_slot_with_the_unit():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    agenda = _Agenda()
    gate = _Gate(armed=True)
    held = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_CONFIRMATION,
            flow_selected_day="2099-01-05",
            flow_selected_slot="2099-01-05T09:00",
            flow_selected_unit_id=centro.id,
        ),
        _tenant(),
        agenda,
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=[doctor],
        gate=gate,
    )
    assert held.flow_state is FlowState.AWAITING_EMAIL_CODE
    assert held.booking_hold is not None
    assert gate.decided[0]["unit_id"] == centro.id
    assert agenda.log.created == []  # nothing written before the code


async def test_a_multi_doctor_clinic_scopes_the_unit_to_the_chosen_doctor():
    centro, sul = _units(2)
    ana, bruno = _doctor([centro, sul]), _doctor()
    bruno.name = "Dr. Bruno"
    asked = await fr.route(
        _conversation(flow_selected_professional_id=ana.id),
        _tenant(),
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=[ana, bruno],
    )
    assert asked.flow_step == STEP_AWAITING_UNIT
    assert asked.flow_selected_professional_id == ana.id
    plain = await fr.route(
        _conversation(flow_selected_professional_id=bruno.id),
        _tenant(),
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=[ana, bruno],
    )
    assert plain.flow_step == fr.STEP_AWAITING_DAY
    assert plain.flow_selected_unit_id is None


# --------------------------------------------------------------------------
# Interrupted conversations resume where they were
# --------------------------------------------------------------------------


async def test_a_conversation_interrupted_at_the_unit_step_resumes_on_the_unit_list():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    resumed = await fr.resume_bubbles(
        _conversation(flow_step=STEP_AWAITING_UNIT), _tenant(), _Agenda(), [doctor]
    )
    assert resumed.flow_step == STEP_AWAITING_UNIT
    assert [row[0] for row in resumed.bubbles[0].rows[:2]] == [
        f"unit|{centro.id}",
        f"unit|{sul.id}",
    ]


async def test_a_conversation_interrupted_on_the_day_list_resumes_with_the_units_hours():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    agenda = _Agenda()
    resumed = await fr.resume_bubbles(
        _conversation(flow_step=fr.STEP_AWAITING_DAY, flow_selected_unit_id=centro.id),
        _tenant(),
        agenda,
        [doctor],
    )
    assert resumed.flow_step == fr.STEP_AWAITING_DAY
    assert resumed.flow_selected_unit_id == centro.id
    assert agenda.log.day_hours == [CENTRO_HOURS]


async def test_a_unit_deactivated_mid_booking_is_asked_again():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    gone = uuid4()
    result = await fr.route(
        _conversation(flow_step=fr.STEP_AWAITING_DAY, flow_selected_unit_id=gone),
        _tenant(),
        _Agenda(),
        "segunda",
        professionals=[doctor],
    )
    assert result.flow_step == STEP_AWAITING_UNIT


async def test_a_picked_unit_gone_before_confirmar_reopens_the_days_of_the_one_left():
    """Final wave M1: picked Centro, Centro closed between the recap and "Confirmar", only Zona
    Sul is left. Never book at Zona Sul at a time chosen within Centro's hours: the days are
    offered again, now within Zona Sul's, and nothing is written."""
    centro, sul = _units(2)
    doctor = _doctor([sul])  # Centro was deactivated / unlinked
    agenda = _Agenda()
    result = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_CONFIRMATION,
            flow_selected_professional_id=doctor.id,
            flow_selected_day="2099-01-05",
            flow_selected_slot="2099-01-05T09:00",
            flow_selected_unit_id=centro.id,
        ),
        _tenant(),
        agenda,
        fr.LABEL_CONFIRM,
        professionals=[doctor],
    )
    assert agenda.log.created == []
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert agenda.log.day_hours == [SUL_HOURS]
    assert result.flow_selected_unit_id == sul.id
    assert result.bubbles[0].body.startswith(UNIT_GONE_NOTICE)


def test_the_unit_row_prefix_carries_its_payload_on_both_channels():
    title, row_id = "Centro", f"unit|{uuid4()}"
    assert inbound_routing_text(title, row_id) == f"{title} ({row_id.split('|', 1)[1]})"


class _FullAgenda(_Agenda):
    """Every day free, a full day of slots: the lists hit their row budget."""

    def with_unit(self, unit_id, location, *, business_hours=None):
        return _FullAgenda(
            business_hours if business_hours is not None else self.hours,
            unit_id=unit_id,
            location=location,
            log=self.log,
        )

    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.log.day_hours.append(self.hours)
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(1, days + 1)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6, **_kw):
        self.log.slot_hours.append(self.hours)
        start = day.replace(hour=8, minute=0, tzinfo=None)
        return [
            {
                "start": (start + timedelta(minutes=30 * i)).isoformat(timespec="minutes"),
                "label": (start + timedelta(minutes=30 * i)).strftime("%H:%M"),
            }
            for i in range(min(max_slots, 20))
        ]


async def test_the_day_and_slot_lists_with_the_unit_back_row_fit_ten_rows():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    agenda = _FullAgenda()
    days = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT),
        _tenant(),
        agenda,
        _unit_tap(sul),
        professionals=[doctor],
    )
    [day_list] = [b for b in days.bubbles if isinstance(b, SlotsBubble)]
    assert len(day_list.rows) <= 10
    assert sum(1 for row in day_list.rows if row[0].startswith("day|")) == fr.DAY_PICKER_PAGE_SIZE
    assert day_list.rows[-1][1] == LABEL_ANOTHER_UNIT

    first_day = day_list.rows[0]
    slots = await fr.route(
        _conversation(flow_step=days.flow_step, flow_selected_unit_id=sul.id),
        _tenant(),
        agenda,
        inbound_routing_text(first_day[1], first_day[0]),
        professionals=[doctor],
    )
    [slot_list] = [b for b in slots.bubbles if isinstance(b, SlotsBubble)]
    assert len(slot_list.rows) <= 10
    assert sum(1 for row in slot_list.rows if row[0].startswith("slot|")) == (
        fr.SLOT_PICKER_MAX_SLOTS
    )
    assert slot_list.rows[-1][1] == LABEL_ANOTHER_UNIT
    assert agenda.log.slot_hours == [SUL_HOURS]



# --------------------------------------------------------------------------
# Rescheduling keeps the unit
# --------------------------------------------------------------------------


def _upcoming(doctor, unit, *, with_unit=True):
    start = datetime(2099, 1, 5, 9, 0, tzinfo=TZ)
    appt = {
        "id": str(uuid4()),
        "google_event_id": "evt-9",
        "appointment_type": "Consulta",
        "start_at": start,
        "end_at": start + timedelta(minutes=30),
        "professional_id": str(doctor.id),
    }
    if with_unit:
        appt["unit_id"] = str(unit.id)
    return appt


async def test_a_reschedule_is_offered_within_the_appointments_unit_hours_and_keeps_it():
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    appt = _upcoming(doctor, sul)
    agenda = _Agenda()
    opened = await fr.enter_manage_action(
        "reschedule", _tenant(), [appt], [doctor], calendar=agenda
    )
    assert opened.flow_step == fr.STEP_MANAGE_DAY
    assert agenda.log.day_hours == [SUL_HOURS]

    managing = dict(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_managing_appointment_id=opened.flow_managing_appointment_id,
    )
    slots = await fr.route(
        _conversation(flow_step=fr.STEP_MANAGE_DAY, **managing),
        _tenant(),
        agenda,
        _day_tap(opened),
        upcoming_appointments=[appt],
        professionals=[doctor],
    )
    assert slots.flow_step == fr.STEP_MANAGE_SLOT
    assert agenda.log.slot_hours == [SUL_HOURS]

    moved = await fr.route(
        _conversation(
            flow_step=fr.STEP_MANAGE_CONFIRM,
            flow_selected_slot="2099-01-06T15:00",
            **managing,
        ),
        _tenant(),
        agenda,
        fr.LABEL_CONFIRM,
        upcoming_appointments=[appt],
        professionals=[doctor],
    )
    # Only the window moves: the row (and so its unit) stays the same appointment.
    assert set(moved.appointment_reschedule) == {"google_event_id", "start_at", "end_at"}


async def test_a_reschedule_without_a_unit_by_a_doctor_with_units_uses_their_units_hours():
    """Final wave I2: a booking from before the units (or a hub "encaixe" with no unit) of a
    doctor who now attends at two units is offered within the UNION of their hours - never the
    clinic's / the 08-18 fallback - and no unit is stamped (none was chosen)."""
    centro, sul = _units(2)
    doctor = _doctor([centro, sul])
    agenda = _Agenda()
    await fr.enter_manage_action(
        "reschedule",
        _tenant(),
        [_upcoming(doctor, sul, with_unit=False)],
        [doctor],
        calendar=agenda,
    )
    assert agenda.log.day_hours == [{**CENTRO_HOURS, **SUL_HOURS}]


async def test_a_reschedule_without_a_unit_by_a_doctor_with_one_unit_uses_its_hours():
    (centro,) = _units(1)
    doctor = _doctor([centro])
    agenda = _Agenda()
    await fr.enter_manage_action(
        "reschedule",
        _tenant(),
        [_upcoming(doctor, centro, with_unit=False)],
        [doctor],
        calendar=agenda,
    )
    assert agenda.log.day_hours == [CENTRO_HOURS]


async def test_a_reschedule_by_a_doctor_without_units_reads_the_whole_agenda_as_today():
    (centro,) = _units(1)
    doctor = _doctor([])
    agenda = _Agenda()
    await fr.enter_manage_action(
        "reschedule",
        _tenant(),
        [_upcoming(doctor, centro, with_unit=False)],
        [doctor],
        calendar=agenda,
    )
    assert agenda.log.day_hours == [CLINIC_HOURS]


def test_the_union_of_unit_hours_merges_overlapping_windows_per_day():
    from secretaria.services.flow_units import union_hours

    morning = UnitOption(
        id=uuid4(),
        name="A",
        address=None,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
    )
    overlap = UnitOption(
        id=uuid4(),
        name="B",
        address=None,
        business_hours={
            "monday": [{"start": "11:00", "end": "14:00"}, {"start": "16:00", "end": "18:00"}],
            "friday": [{"start": "09:00", "end": "10:00"}],
        },
    )
    assert union_hours([morning, overlap]) == {
        "monday": [{"start": "08:00", "end": "14:00"}, {"start": "16:00", "end": "18:00"}],
        "friday": [{"start": "09:00", "end": "10:00"}],
    }


# --------------------------------------------------------------------------
# Service x doctor x unit (TASK-083 follow-up T4): the unit list is filtered by the service
# --------------------------------------------------------------------------

CONSULTA_ID = uuid4()
EXAME_ID = uuid4()
NORTE_HOURS = {"wednesday": [{"start": "08:00", "end": "12:00"}]}


def _svc_tenant(*, with_ids: bool = True):
    tenant = _tenant()
    consulta = {"name": "Consulta", "duration_min": 30, "is_active": True, "sort_order": 0}
    exame = {"name": "Exame", "duration_min": 30, "is_active": True, "sort_order": 1}
    if with_ids:
        consulta["service_id"] = str(CONSULTA_ID)
        exame["service_id"] = str(EXAME_ID)
    tenant.appointment_types = [consulta, exame]
    return tenant


def _three_units() -> list[UnitOption]:
    centro, sul = _units(2)
    norte = UnitOption(id=uuid4(), name="Norte", address="Rua C, 30", business_hours=NORTE_HOURS)
    return [centro, sul, norte]


def _marked_doctor(units: list, marks: dict, name: str = "Dra. Ana"):
    doctor = _doctor(units)
    doctor.name = name
    doctor.service_units = marks
    return doctor


def _unit_row_ids(result) -> list[str]:
    [bubble] = [b for b in result.bubbles if isinstance(b, SlotsBubble)]
    return [row[0] for row in bubble.rows if row[0].startswith("unit|")]


async def test_unit_list_shows_only_units_of_service():
    centro, sul, norte = _three_units()
    doctor = _marked_doctor(
        [centro, sul, norte], {CONSULTA_ID: frozenset({centro.id, norte.id})}
    )
    result = await fr.route(
        _conversation(), _svc_tenant(), _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[doctor]
    )
    assert result.flow_step == STEP_AWAITING_UNIT
    assert _unit_row_ids(result) == [f"unit|{centro.id}", f"unit|{norte.id}"]
    assert _back_row(result)[1] == fr.LABEL_ANOTHER_SERVICE
    assert result.notify_no_unit is None
    # A tap on a unit outside the filter is not accepted as the pick.
    tapped = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT),
        _svc_tenant(),
        _Agenda(),
        _unit_tap(sul),
        professionals=[doctor],
    )
    assert tapped.flow_selected_unit_id != sul.id


async def test_service_without_marks_lists_all_units():
    centro, sul, norte = _three_units()
    # Marks exist, but only for another service: Consulta is done at every unit.
    doctor = _marked_doctor([centro, sul, norte], {EXAME_ID: frozenset({sul.id})})
    result = await fr.route(
        _conversation(), _svc_tenant(), _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[doctor]
    )
    assert result.flow_step == STEP_AWAITING_UNIT
    assert _unit_row_ids(result) == [f"unit|{u.id}" for u in (centro, sul, norte)]


def _zero_unit_doctor(name: str = "Dra. Ana"):
    """Consulta is restricted to a unit that is closed: no bookable unit does it."""
    centro, sul = _units(2)
    closed = UnitOption(id=uuid4(), name="Fechada", address=None, business_hours={})
    return _marked_doctor([centro, sul, closed], {CONSULTA_ID: frozenset({closed.id})}, name)


async def test_zero_units_sets_notify_no_unit_and_returns_to_doctor_list():
    ana = _zero_unit_doctor()
    bruno = _marked_doctor([], {}, "Dr. Bruno")
    carla = _zero_unit_doctor("Dra. Carla")  # does Consulta, but nowhere either
    agenda = _Agenda()
    result = await fr.route(
        _conversation(flow_selected_professional_id=ana.id, flow_selected_insurance="Particular"),
        _svc_tenant(),
        agenda,
        fr.LABEL_BOOK_SERVICE,
        professionals=[ana, bruno, carla],
    )
    assert result.action == "reply"
    assert result.notify_no_unit == (ana.id, CONSULTA_ID, "Consulta")
    assert result.flow_state is FlowState.SERVICE_CATALOG
    assert result.flow_step == fr.STEP_AWAITING_PROFESSIONAL
    # The service rides along (a doctor tap goes straight to its card); the doctor and the
    # unit do not; the convênio answer is kept.
    assert result.flow_selected_type == "Consulta"
    assert result.flow_selected_professional_id is None
    assert result.flow_selected_unit_id is None
    assert result.flow_selected_insurance == "Particular"
    [bubble] = result.bubbles
    assert bubble.body.startswith(
        "Não consegui seguir com Consulta com Dra. Ana agora. Avisei a clínica. "
        "Vamos escolher outro médico?"
    )
    prof_rows = [row[0] for row in bubble.rows if row[0].startswith("prof|")]
    assert prof_rows == [f"prof|{bruno.id}"]  # only who really does it; never Ana again
    assert agenda.log.day_hours == []
    # No dead end: the other doctor's tap opens the service card with that doctor.
    picked = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_PROFESSIONAL,
            flow_selected_type=result.flow_selected_type,
        ),
        _svc_tenant(),
        _Agenda(),
        inbound_routing_text("Dr. Bruno", f"prof|{bruno.id}"),
        professionals=[ana, bruno, carla],
    )
    assert picked.flow_step == fr.STEP_AWAITING_SERVICE_CONFIRM
    assert picked.flow_selected_professional_id == bruno.id
    assert picked.notify_no_unit is None


async def test_zero_units_single_doctor_clinic_returns_to_service_list():
    ana = _zero_unit_doctor()
    result = await fr.route(
        _conversation(), _svc_tenant(), _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[ana]
    )
    assert result.notify_no_unit == (ana.id, CONSULTA_ID, "Consulta")
    assert result.flow_step == fr.STEP_AWAITING_SERVICE
    assert result.flow_selected_unit_id is None
    [bubble] = result.bubbles
    assert bubble.body.startswith(
        "Não consegui seguir com Consulta com Dra. Ana agora. Avisei a clínica."
    )
    assert "Vamos escolher outro médico?" not in bubble.body
    # The service is NOT hidden from the list (spec §9.9).
    assert [row[0] for row in bubble.rows if row[0].startswith("svc|")] == [
        "svc|Consulta",
        "svc|Exame",
    ]
    # Another service of the same doctor books normally.
    exame = await fr.route(
        _conversation(flow_selected_type="Exame"),
        _svc_tenant(),
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=[ana],
    )
    assert exame.flow_step == STEP_AWAITING_UNIT
    assert exame.notify_no_unit is None


async def test_zero_units_when_only_that_doctor_does_the_service_goes_to_service_list():
    """Two doctors, but the other one does not offer the service (the same holds when the
    service was picked first and nobody else does it): no doctor list, the service list."""
    ana = _zero_unit_doctor()
    bruno = _marked_doctor([], {}, "Dr. Bruno")
    bruno.appointment_types = [
        {"name": "Exame", "duration_min": 30, "is_active": True, "service_id": str(EXAME_ID)}
    ]
    result = await fr.route(
        _conversation(flow_selected_professional_id=ana.id),
        _svc_tenant(),
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=[ana, bruno],
    )
    assert result.notify_no_unit == (ana.id, CONSULTA_ID, "Consulta")
    assert result.flow_step == fr.STEP_AWAITING_SERVICE
    assert result.flow_selected_professional_id == ana.id
    assert [row[0] for row in result.bubbles[0].rows if row[0].startswith("svc|")] == [
        "svc|Consulta",
        "svc|Exame",
    ]


async def test_service_picked_first_then_doctor_filters_units():
    centro, sul, norte = _three_units()
    ana = _marked_doctor([centro, sul, norte], {CONSULTA_ID: frozenset({sul.id, norte.id})})
    bruno = _marked_doctor([], {}, "Dr. Bruno")
    card = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_SERVICE_PROFESSIONAL, flow_selected_type="Consulta"
        ),
        _svc_tenant(),
        _Agenda(),
        inbound_routing_text("Dra. Ana", f"prof|{ana.id}"),
        professionals=[ana, bruno],
    )
    assert card.flow_step == fr.STEP_AWAITING_SERVICE_CONFIRM
    assert card.flow_selected_professional_id == ana.id
    units = await fr.route(
        _conversation(flow_selected_professional_id=ana.id),
        _svc_tenant(),
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=[ana, bruno],
    )
    assert units.flow_step == STEP_AWAITING_UNIT
    assert _unit_row_ids(units) == [f"unit|{sul.id}", f"unit|{norte.id}"]


async def test_no_service_id_entry_is_unrestricted():
    centro, sul, norte = _three_units()
    # A mark keyed by a service the entry cannot point at (the entry has no service_id).
    doctor = _marked_doctor([centro, sul, norte], {CONSULTA_ID: frozenset({centro.id})})
    result = await fr.route(
        _conversation(),
        _svc_tenant(with_ids=False),
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=[doctor],
    )
    assert result.flow_step == STEP_AWAITING_UNIT
    assert _unit_row_ids(result) == [f"unit|{u.id}" for u in (centro, sul, norte)]
    assert result.notify_no_unit is None


async def test_change_service_clears_unit_and_refilters():
    centro, sul, norte = _three_units()
    doctor = _marked_doctor(
        [centro, sul, norte],
        {CONSULTA_ID: frozenset({centro.id, norte.id}), EXAME_ID: frozenset({sul.id, norte.id})},
    )
    # Back from the unit list to the services: the unit is dropped.
    back = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT, flow_selected_unit_id=centro.id),
        _svc_tenant(),
        _Agenda(),
        inbound_routing_text(fr.LABEL_ANOTHER_SERVICE, "dayback|service"),
        professionals=[doctor],
    )
    assert back.flow_step == fr.STEP_AWAITING_SERVICE
    assert back.flow_selected_unit_id is None
    # A unit left over from the other service (Centro, Consulta-only) never leaks into Exame:
    # Exame's own list is asked, without Centro.
    exame = await fr.route(
        _conversation(flow_selected_type="Exame", flow_selected_unit_id=centro.id),
        _svc_tenant(),
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=[doctor],
    )
    assert exame.flow_step == STEP_AWAITING_UNIT
    assert exame.flow_selected_unit_id is None
    assert _unit_row_ids(exame) == [f"unit|{sul.id}", f"unit|{norte.id}"]
    # The same leftover at the day step is asked again rather than booked at Centro.
    day = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_DAY,
            flow_selected_type="Exame",
            flow_selected_unit_id=centro.id,
        ),
        _svc_tenant(),
        _Agenda(),
        "segunda",
        professionals=[doctor],
    )
    assert day.flow_step == STEP_AWAITING_UNIT
    assert _unit_row_ids(day) == [f"unit|{sul.id}", f"unit|{norte.id}"]


async def test_change_doctor_clears_unit_and_refilters():
    centro, sul, norte = _three_units()
    ana = _marked_doctor([centro, sul], {})
    bruno = _marked_doctor([centro, sul, norte], {CONSULTA_ID: frozenset({sul.id, norte.id})})
    result = await fr.route(
        _conversation(flow_selected_professional_id=bruno.id, flow_selected_unit_id=centro.id),
        _svc_tenant(),
        _Agenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=[ana, bruno],
    )
    assert result.flow_step == STEP_AWAITING_UNIT
    assert result.flow_selected_unit_id is None
    assert _unit_row_ids(result) == [f"unit|{sul.id}", f"unit|{norte.id}"]


async def test_back_row_unit_only_when_list():
    centro, sul, norte = _three_units()
    # list (2 filtered units): the day picker's back row goes to the units.
    listed = _marked_doctor([centro, sul, norte], {CONSULTA_ID: frozenset({sul.id, norte.id})})
    days = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT),
        _svc_tenant(),
        _Agenda(),
        _unit_tap(sul),
        professionals=[listed],
    )
    assert days.flow_step == fr.STEP_AWAITING_DAY
    assert _back_row(days)[1] == LABEL_ANOTHER_UNIT

    # confirm (the filter leaves 1 of 2+): the Sim/Não step (T5); after "Sim" there is no
    # real choice, so one step back is the service list.
    confirm = _marked_doctor([centro, sul, norte], {CONSULTA_ID: frozenset({sul.id})})
    asked = await fr.route(
        _conversation(), _svc_tenant(), _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[confirm]
    )
    assert asked.flow_step == STEP_AWAITING_UNIT_CONFIRM
    agenda = _Agenda()
    picked = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT_CONFIRM),
        _svc_tenant(),
        agenda,
        fr.LABEL_YES,
        professionals=[confirm],
    )
    assert picked.flow_step == fr.STEP_AWAITING_DAY
    assert picked.flow_selected_unit_id == sul.id
    assert agenda.log.day_hours == [SUL_HOURS]
    assert _back_row(picked)[1] == fr.LABEL_ANOTHER_SERVICE
    slots = await fr.route(
        _conversation(flow_step=fr.STEP_AWAITING_DAY, flow_selected_unit_id=sul.id),
        _svc_tenant(),
        agenda,
        _day_tap(picked),
        professionals=[confirm],
    )
    assert slots.flow_step == fr.STEP_AWAITING_SLOT
    assert slots.flow_selected_unit_id == sul.id
    assert _back_row(slots)[1] == fr.LABEL_ANOTHER_SERVICE
    # A stale "Outra unidade" tap with no real choice falls back to the service list.
    back = await fr.route(
        _conversation(flow_step=fr.STEP_AWAITING_DAY, flow_selected_unit_id=sul.id),
        _svc_tenant(),
        _Agenda(),
        inbound_routing_text(LABEL_ANOTHER_UNIT, "dayback|unit"),
        professionals=[confirm],
    )
    assert back.flow_step == fr.STEP_AWAITING_SERVICE

    # implied (the doctor has one unit): no question, back row to the services.
    implied = _marked_doctor([sul], {CONSULTA_ID: frozenset({sul.id})})
    direct = await fr.route(
        _conversation(), _svc_tenant(), _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[implied]
    )
    assert direct.flow_step == fr.STEP_AWAITING_DAY
    assert direct.flow_selected_unit_id == sul.id
    assert _back_row(direct)[1] == fr.LABEL_ANOTHER_SERVICE


async def test_resume_on_the_unit_step_lists_only_the_services_units():
    centro, sul, norte = _three_units()
    doctor = _marked_doctor([centro, sul, norte], {CONSULTA_ID: frozenset({sul.id, norte.id})})
    resumed = await fr.resume_bubbles(
        _conversation(flow_step=STEP_AWAITING_UNIT), _svc_tenant(), _Agenda(), [doctor]
    )
    assert resumed.flow_step == STEP_AWAITING_UNIT
    assert _unit_row_ids(resumed) == [f"unit|{sul.id}", f"unit|{norte.id}"]


async def test_the_booking_writes_at_the_picked_unit_of_the_service():
    centro, sul, norte = _three_units()
    doctor = _marked_doctor([centro, sul, norte], {CONSULTA_ID: frozenset({sul.id, norte.id})})
    agenda = _Agenda()
    gate = _Gate(armed=False)
    booked = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_CONFIRMATION,
            flow_selected_day="2099-01-06",
            flow_selected_slot="2099-01-06T09:00",
            flow_selected_unit_id=sul.id,
        ),
        _svc_tenant(),
        agenda,
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=[doctor],
        gate=gate,
    )
    assert booked.appointment["unit_id"] == sul.id
    assert gate.decided[0]["unit_id"] == sul.id


# --------------------------------------------------------------------------
# The Sim/Não confirmation (follow-up T5, spec §5.2-3, §9.1, §9.8)
# --------------------------------------------------------------------------

SUL_CONFIRM_BODY = (
    "O serviço Consulta feito por Dra. Ana é feito *apenas na unidade Zona Sul*, em Av. B, 20. "
    "Você confirma essa unidade?"
)


def _confirm_doctor(name: str = "Dra. Ana"):
    """Three bookable units, Consulta done only at Zona Sul: the filter leaves 1 of 3."""
    centro, sul, norte = _three_units()
    return _marked_doctor([centro, sul, norte], {CONSULTA_ID: frozenset({sul.id})}, name), sul


def _confirm_bubble(result) -> fr.MenuBubble:
    [bubble] = result.bubbles
    assert isinstance(bubble, fr.MenuBubble)
    return bubble


async def test_confirm_message_exact_text_and_buttons():
    assert UNIT_CONFIRM_BODY == (
        "O serviço {servico} feito por {medico} é feito *apenas na unidade {unidade}*, "
        "em {endereco}. Você confirma essa unidade?"
    )
    doctor, _sul = _confirm_doctor()
    agenda = _Agenda()
    result = await fr.route(
        _conversation(), _svc_tenant(), agenda, fr.LABEL_BOOK_SERVICE, professionals=[doctor]
    )
    assert result.action == "reply"
    assert result.flow_state is FlowState.SERVICE_CATALOG
    assert result.flow_step == STEP_AWAITING_UNIT_CONFIRM
    bubble = _confirm_bubble(result)
    assert bubble.body == SUL_CONFIRM_BODY
    assert bubble.labels == ["✅ Sim", "❌ Não"]
    # The unit is NOT saved before "Sim"; no day was read; nobody is told anything.
    assert result.flow_selected_unit_id is None
    assert result.flow_selected_type == "Consulta"
    assert agenda.log.day_hours == []
    assert result.notify_no_unit is None
    # A unit without an address: the sentence stays whole, never "None".
    centro, sul, norte = _three_units()
    bare = UnitOption(id=sul.id, name="Zona Sul", address=None, business_hours=SUL_HOURS)
    no_address = _marked_doctor([centro, bare, norte], {CONSULTA_ID: frozenset({bare.id})})
    plain = await fr.route(
        _conversation(), _svc_tenant(), _Agenda(), fr.LABEL_BOOK_SERVICE, professionals=[no_address]
    )
    assert _confirm_bubble(plain).body == (
        "O serviço Consulta feito por Dra. Ana é feito *apenas na unidade Zona Sul*. "
        "Você confirma essa unidade?"
    )


def test_unit_confirm_body_highlights_only_unit():
    """TASK-088 C: "apenas na unidade X" is WhatsApp bold; the closing question is untouched."""
    from secretaria.services import flow_units

    doctor, _sul = _confirm_doctor()
    centro = UnitOption(
        id=uuid4(), name="Unidade Centro", address="Rua A, 10.", business_hours=SUL_HOURS
    )
    bare = UnitOption(id=uuid4(), name="Unidade Centro", address=None, business_hours=SUL_HOURS)
    with_address = flow_units.unit_confirm_body("Consulta", doctor, centro)
    without_address = flow_units.unit_confirm_body("Consulta", doctor, bare)
    assert "é feito *apenas na unidade Unidade Centro*, em Rua A, 10." in with_address
    assert "é feito *apenas na unidade Unidade Centro*." in without_address
    for body in (with_address, without_address):
        assert body.count("*") == 2
        assert body.endswith("Você confirma essa unidade?")


async def test_yes_selects_unit_and_opens_day_picker():
    doctor, sul = _confirm_doctor()
    for answer in (fr.LABEL_YES, "sim"):
        agenda = _Agenda()
        result = await fr.route(
            _conversation(flow_step=STEP_AWAITING_UNIT_CONFIRM),
            _svc_tenant(),
            agenda,
            answer,
            professionals=[doctor],
        )
        assert result.flow_step == fr.STEP_AWAITING_DAY
        assert result.flow_selected_unit_id == sul.id
        assert result.flow_selected_type == "Consulta"
        assert agenda.log.day_hours == [SUL_HOURS]
        # No real unit choice was made: one step back is the service list.
        assert _back_row(result)[1] == fr.LABEL_ANOTHER_SERVICE
    # The next turn (a day tap) keeps the unit and reads its hours.
    slots = await fr.route(
        _conversation(flow_step=fr.STEP_AWAITING_DAY, flow_selected_unit_id=sul.id),
        _svc_tenant(),
        agenda,
        _day_tap(result),
        professionals=[doctor],
    )
    assert slots.flow_step == fr.STEP_AWAITING_SLOT
    assert slots.flow_selected_unit_id == sul.id
    assert agenda.log.slot_hours == [SUL_HOURS]


async def test_no_returns_to_doctor_list_without_email_effect():
    ana, _sul = _confirm_doctor()
    bruno = _marked_doctor([], {}, "Dr. Bruno")
    carla = _zero_unit_doctor("Dra. Carla")  # offers Consulta, but no unit does it
    agenda = _Agenda()
    result = await fr.route(
        _conversation(
            flow_step=STEP_AWAITING_UNIT_CONFIRM,
            flow_selected_professional_id=ana.id,
            flow_selected_insurance="Particular",
        ),
        _svc_tenant(),
        agenda,
        fr.LABEL_NO,
        professionals=[ana, bruno, carla],
    )
    assert result.action == "reply"
    assert result.notify_no_unit is None  # no e-mail to the clinic
    assert result.flow_step == fr.STEP_AWAITING_PROFESSIONAL
    assert result.flow_selected_type == "Consulta"
    assert result.flow_selected_professional_id is None
    assert result.flow_selected_unit_id is None
    assert result.flow_selected_insurance == "Particular"
    [bubble] = result.bubbles
    assert "Avisei a clínica" not in bubble.body
    assert "Não consegui" not in bubble.body
    assert [row[0] for row in bubble.rows if row[0].startswith("prof|")] == [f"prof|{bruno.id}"]
    assert agenda.log.day_hours == []
    # Typed "não" is the same answer.
    typed = await fr.route(
        _conversation(
            flow_step=STEP_AWAITING_UNIT_CONFIRM, flow_selected_professional_id=ana.id
        ),
        _svc_tenant(),
        _Agenda(),
        "não",
        professionals=[ana, bruno, carla],
    )
    assert typed.flow_step == fr.STEP_AWAITING_PROFESSIONAL
    assert typed.notify_no_unit is None


async def test_no_single_doctor_goes_to_service_list():
    doctor, _sul = _confirm_doctor()
    result = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT_CONFIRM),
        _svc_tenant(),
        _Agenda(),
        fr.LABEL_NO,
        professionals=[doctor],
    )
    assert result.notify_no_unit is None
    assert result.flow_step == fr.STEP_AWAITING_SERVICE
    assert result.flow_selected_unit_id is None
    [bubble] = result.bubbles
    assert "Avisei a clínica" not in bubble.body
    assert "Não consegui" not in bubble.body
    # The service stays in the list; nothing is a dead end.
    assert [row[0] for row in bubble.rows if row[0].startswith("svc|")] == [
        "svc|Consulta",
        "svc|Exame",
    ]


async def test_doctor_with_one_unit_skips_confirm():
    _centro, sul, _norte = _three_units()
    # One linked unit, the service restricted to it.
    single = _marked_doctor([sul], {CONSULTA_ID: frozenset({sul.id})})
    # Two linked units but one closed: only one bookable, so nothing was "reduced".
    closed = UnitOption(id=uuid4(), name="Fechada", address="Rua Z, 1", business_hours={})
    one_open = _marked_doctor([sul, closed], {CONSULTA_ID: frozenset({sul.id})})
    for doctor in (single, one_open):
        agenda = _Agenda()
        result = await fr.route(
            _conversation(), _svc_tenant(), agenda, fr.LABEL_BOOK_SERVICE, professionals=[doctor]
        )
        assert result.flow_step == fr.STEP_AWAITING_DAY
        assert result.flow_selected_unit_id == sul.id
        assert agenda.log.day_hours == [SUL_HOURS]


async def test_free_text_on_confirm_step_delegates_once():
    doctor, sul = _confirm_doctor()
    talk = await fr.route(
        _conversation(flow_step=STEP_AWAITING_UNIT_CONFIRM),
        _svc_tenant(),
        _Agenda(),
        "essa unidade tem estacionamento?",
        professionals=[doctor],
    )
    # One model turn on this same step: the step, the booking and the (unsaved) unit stay.
    assert talk.action == "delegate_llm"
    assert talk.flow_state is FlowState.SERVICE_CATALOG
    assert talk.flow_step == STEP_AWAITING_UNIT_CONFIRM
    assert talk.flow_selected_type == "Consulta"
    assert talk.flow_selected_unit_id is None
    assert talk.notify_no_unit is None
    # The buttons keep working on the next turn.
    yes = await fr.route(
        _conversation(flow_step=talk.flow_step),
        _svc_tenant(),
        _Agenda(),
        fr.LABEL_YES,
        professionals=[doctor],
    )
    assert yes.flow_step == fr.STEP_AWAITING_DAY
    assert yes.flow_selected_unit_id == sul.id


async def test_confirm_survives_resume():
    doctor, _sul = _confirm_doctor()
    resumed = await fr.resume_bubbles(
        _conversation(flow_step=STEP_AWAITING_UNIT_CONFIRM), _svc_tenant(), _Agenda(), [doctor]
    )
    assert resumed.action == "reply"
    assert resumed.flow_step == STEP_AWAITING_UNIT_CONFIRM
    assert resumed.flow_selected_unit_id is None
    bubble = _confirm_bubble(resumed)
    assert bubble.body == SUL_CONFIRM_BODY
    assert bubble.labels == [fr.LABEL_YES, fr.LABEL_NO]


async def test_a_day_step_without_the_confirmed_unit_asks_the_confirmation():
    """The safety net: a later step with a `confirm` choice and no unit asks Sim/Não first;
    a stale pick outside the service's filter is never used."""
    doctor, _sul = _confirm_doctor()
    centro = doctor.units[0]
    result = await fr.route(
        _conversation(flow_step=fr.STEP_AWAITING_DAY, flow_selected_unit_id=centro.id),
        _svc_tenant(),
        _Agenda(),
        "segunda",
        professionals=[doctor],
    )
    assert result.flow_step == STEP_AWAITING_UNIT_CONFIRM
    assert result.flow_selected_unit_id is None
    assert _confirm_bubble(result).body == SUL_CONFIRM_BODY
