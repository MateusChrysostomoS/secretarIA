"""TASK-085 T4b: the button flow reads price/duration at the booking's unit (router level).

No DB, no network: SimpleNamespace snapshots shaped like the worker's (`units`, `service_units`
and `service_unit_terms`, the field `unit_booking.attach_unit_options` fills), the fake agenda of
tests/test_flow_router_units.py extended to record the slot length of every read, and its fake
booking gate (armed = the Portal's hold, unarmed = the WhatsApp booking).

* characterization: no exception (no unit, two units, an exception that changes nothing) ->
  exactly today's card, offers, event and details;
* B2/D3: a price that differs between the units is NOT on the service card (never a range); one
  unit for the service -> that unit's value (R2);
* A7: once the unit is picked, ONE line "Consulta na Unidade Centro: R$ 250,00 · 30 min" before
  the day picker - only when the card hid the price, never under a convênio, never repeated for
  the unit already chosen;
* A5/A6: the slots and the event take the unit's duration (SPEC §10 item 4), the hold too;
* SPEC §10 item 3: two doctors with their own values at the same unit, each respected;
* R13: a single-doctor clinic (implicit owner) books with the owner's own list;
* A14: the AI landing's details message prices the service at the unit.
"""

import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")

from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402
from secretaria.services import booking_draft as bd, flow_router as fr  # noqa: E402
from secretaria.services.booking_draft import BookingDraft, resolve_booking_draft  # noqa: E402
from secretaria.services.flow_units import (  # noqa: E402
    STEP_AWAITING_UNIT,
    STEP_AWAITING_UNIT_CONFIRM,
)
from secretaria.services.service_terms import UnitTerms  # noqa: E402
from secretaria.services.unit_booking import UnitOption  # noqa: E402
from tests.test_flow_router_units import (  # noqa: E402
    TZ,
    _Agenda,
    _conversation,
    _day_tap,
    _Gate,
    _unit_tap,
)

SID = uuid4()
OTHER_SID = uuid4()
MONDAY = {"monday": [{"start": "08:00", "end": "12:00"}]}
TUESDAY = {"tuesday": [{"start": "14:00", "end": "18:00"}]}
SEDE = UnitOption(id=uuid4(), name="Sede", address="Rua A, 10", business_hours=MONDAY)
CENTRO = UnitOption(id=uuid4(), name="Centro", address="Av. B, 20", business_hours=TUESDAY)
CONSULTA = {
    "name": "Consulta",
    "duration_min": 30,
    "price": "R$ 200,00",
    "is_active": True,
    "sort_order": 0,
    "service_id": str(SID),
}
CARD_WITH_PRICE = "Consulta R$ 200,00\n\nDeseja agendar esse serviço?"
CARD_WITHOUT_PRICE = "Consulta\n\nDeseja agendar esse serviço?"


class _MinutesAgenda(_Agenda):
    """`_Agenda` that also records the slot length of every day/slot read."""

    def __init__(self, *args, minutes=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.minutes = minutes if minutes is not None else []

    def with_unit(self, unit_id, location, *, business_hours=None):
        return _MinutesAgenda(
            business_hours if business_hours is not None else self.hours,
            unit_id=unit_id,
            location=location,
            log=self.log,
            minutes=self.minutes,
        )

    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.minutes.append(("days", slot_minutes))
        return await super().list_available_days(start_day, days, slot_minutes)

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6, **kw):
        self.minutes.append(("slots", slot_minutes))
        return await super().list_free_slots(day, slot_minutes, max_slots, **kw)

    async def create_event(self, start, end, summary, description=""):
        self.log.created.append((self.unit_id, start, end))
        return {"id": "evt-1", "htmlLink": None}


def _tenant(entries=None, **kw):
    base = dict(
        initial_flows={"enabled": True},
        appointment_types=list(entries or [CONSULTA]),
        appointment_duration_min=30,
        business_hours=MONDAY,
        collect_insurance=False,
        insurance_mode=None,
        insurances=[],
        timezone="America/Sao_Paulo",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _doctor(units=(), terms=None, marks=None, *, name="Dra. Ana", entries=None):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        appointment_types=list([CONSULTA] if entries is None else entries),
        business_hours=None,
        min_booking_notice_hours=None,
        units=list(units),
        service_units=marks or {},
        service_unit_terms=terms if terms is not None else {},
    )


def _differs():
    """Sede keeps the base (R$ 200,00 / 30 min), Centro has its own price."""
    return _doctor([SEDE, CENTRO], {SID: {CENTRO.id: UnitTerms("R$ 250,00", None)}})


def _longer_at_centro():
    """Same price everywhere; Centro takes 40 minutes instead of 30."""
    return _doctor([SEDE, CENTRO], {SID: {CENTRO.id: UnitTerms(None, 40)}})


def _card_body(result) -> str:
    [bubble] = result.bubbles
    assert isinstance(bubble, ButtonBubble)
    return bubble.body


async def _card(doctor, **conv):
    """The service card the tap on "Consulta" draws (doctor-first list, doctor selected)."""
    result = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_SERVICE,
            flow_selected_type=None,
            flow_selected_professional_id=doctor.id,
            **conv,
        ),
        _tenant(),
        _MinutesAgenda(),
        "Consulta",
        professionals=[doctor, _doctor(name="Dr. Outro")],
    )
    assert result.flow_step == fr.STEP_AWAITING_SERVICE_CONFIRM
    return _card_body(result)


async def _pick_unit(doctor, unit, agenda=None, **conv):
    return await fr.route(
        _conversation(
            flow_step=STEP_AWAITING_UNIT, flow_selected_professional_id=doctor.id, **conv
        ),
        _tenant(),
        agenda or _MinutesAgenda(),
        _unit_tap(unit),
        professionals=[doctor],
    )


def _texts(result) -> list[str]:
    return [b.body for b in result.bubbles if isinstance(b, TextBubble)]


async def _book(doctor, unit, agenda, *, gate=None):
    """Unit picked -> day -> slot -> Confirmar, every turn through `route()`."""
    days = await _pick_unit(doctor, unit, agenda)
    assert days.flow_step == fr.STEP_AWAITING_DAY
    common = dict(
        flow_selected_professional_id=doctor.id, flow_selected_unit_id=days.flow_selected_unit_id
    )
    slots = await fr.route(
        _conversation(flow_step=days.flow_step, **common),
        _tenant(),
        agenda,
        _day_tap(days),
        professionals=[doctor],
    )
    assert slots.flow_step == fr.STEP_AWAITING_SLOT
    [slot_list] = [b for b in slots.bubbles if isinstance(b, SlotsBubble)]
    slot_tap = inbound_routing_text(slot_list.rows[0][1], slot_list.rows[0][0])
    recap = await fr.route(
        _conversation(
            flow_step=slots.flow_step, flow_selected_day=slots.flow_selected_day, **common
        ),
        _tenant(),
        agenda,
        slot_tap,
        professionals=[doctor],
    )
    assert recap.flow_step == fr.STEP_AWAITING_CONFIRMATION
    return await fr.route(
        _conversation(
            flow_step=recap.flow_step,
            flow_selected_day=recap.flow_selected_day,
            flow_selected_slot=recap.flow_selected_slot,
            **common,
        ),
        _tenant(),
        agenda,
        fr.LABEL_CONFIRM,
        patient_name="Maria",
        professionals=[doctor],
        gate=gate,
    )


# --------------------------------------------------------------------------
# Characterization: no exception -> exactly today's output
# --------------------------------------------------------------------------


async def test_without_units_the_card_offers_and_event_are_todays():
    doctor = _doctor()
    assert await _card(doctor) == CARD_WITH_PRICE
    agenda = _MinutesAgenda()
    days = await fr.route(
        _conversation(flow_selected_professional_id=doctor.id),
        _tenant(),
        agenda,
        fr.LABEL_BOOK_SERVICE,
        professionals=[doctor],
    )
    assert days.flow_step == fr.STEP_AWAITING_DAY
    assert _texts(days) == []
    assert agenda.minutes == [("days", 30)]


async def test_two_units_without_exceptions_show_the_price_and_no_extra_line():
    doctor = _doctor([SEDE, CENTRO])
    assert await _card(doctor) == CARD_WITH_PRICE
    agenda = _MinutesAgenda()
    booked = await _book(doctor, CENTRO, agenda)
    assert agenda.minutes == [("days", 30), ("slots", 30)]
    [(unit_id, start, end)] = agenda.log.created
    assert unit_id == CENTRO.id and end - start == timedelta(minutes=30)
    assert booked.appointment["end_at"] - booked.appointment["start_at"] == timedelta(minutes=30)
    picked = await _pick_unit(doctor, SEDE)
    assert _texts(picked) == []
    assert isinstance(picked.bubbles[0], SlotsBubble)


async def test_an_exception_that_changes_nothing_shown_keeps_todays_card_and_no_line():
    # Centro's own price is the same money ("200" == "R$ 200,00"); another service has its own.
    doctor = _doctor(
        [SEDE, CENTRO],
        {SID: {CENTRO.id: UnitTerms("200", None)}, OTHER_SID: {SEDE.id: UnitTerms("R$ 9", 5)}},
    )
    assert await _card(doctor) == CARD_WITH_PRICE
    picked = await _pick_unit(doctor, CENTRO)
    assert _texts(picked) == []


async def test_a_card_under_a_convenio_never_shows_a_price_as_today():
    for doctor in (_doctor([SEDE, CENTRO]), _differs()):
        body = await _card(doctor, flow_selected_insurance="Unimed")
        assert body == "Consulta — convênio Unimed\n\nDeseja agendar esse serviço?"


async def test_single_doctor_clinic_books_with_the_owners_own_list():
    """R13: no doctor is ever selected on a one-doctor clinic; the tenant snapshot IS the
    doctor's list (workers/shared/greeting.py::_flow_tenant_snapshot), so the event takes the
    owner's own 45 minutes, not the clinic default."""
    own = {**CONSULTA, "duration_min": 45}
    doctor = _doctor(entries=[own])
    tenant = _tenant([own])
    agenda = _MinutesAgenda()
    conv = dict(
        flow_step=fr.STEP_AWAITING_CONFIRMATION,
        flow_selected_day="2099-01-06",
        flow_selected_slot="2099-01-06T09:00",
    )
    booked = await fr.route(
        _conversation(**conv), tenant, agenda, fr.LABEL_CONFIRM, professionals=[doctor]
    )
    [(_unit, start, end)] = agenda.log.created
    assert end - start == timedelta(minutes=45)
    assert booked.appointment["professional_id"] == doctor.id


async def test_the_ai_details_message_prices_like_today_without_exceptions():
    doctor = _doctor([SEDE, CENTRO])
    text = bd._booking_details(
        SimpleNamespace(service=CONSULTA),
        SimpleNamespace(
            flow_selected_professional_id=doctor.id,
            flow_selected_insurance=None,
            flow_attendee_name="",
        ),
        conversation=SimpleNamespace(flow_state=FlowState.LLM),
        tenant=_tenant(initial_flows={"ai_draft_v2": True}),
        owner=doctor,
        unit=CENTRO,
    )
    assert "🏥 Serviço: Consulta — R$ 200,00" in text


# --------------------------------------------------------------------------
# B2 / D3: the service card before the unit
# --------------------------------------------------------------------------


async def test_a_price_that_differs_between_units_is_not_on_the_card():
    assert await _card(_differs()) == CARD_WITHOUT_PRICE


async def test_a_service_done_at_one_unit_shows_that_units_price():
    # R2: Consulta only at Centro (of the doctor's two units), Centro charges R$ 250,00.
    doctor = _doctor(
        [SEDE, CENTRO],
        {SID: {CENTRO.id: UnitTerms("R$ 250,00", None)}},
        {SID: frozenset({CENTRO.id})},
    )
    assert await _card(doctor) == "Consulta R$ 250,00\n\nDeseja agendar esse serviço?"
    # A doctor with a single unit that has its own price: that price.
    single = _doctor([CENTRO], {SID: {CENTRO.id: UnitTerms("R$ 250,00", None)}})
    assert await _card(single) == "Consulta R$ 250,00\n\nDeseja agendar esse serviço?"


async def test_the_resumed_card_hides_a_differing_price_too():
    doctor = _differs()
    result = await fr.resume_bubbles(
        _conversation(flow_selected_professional_id=doctor.id),
        _tenant(),
        _MinutesAgenda(),
        professionals=[doctor],
    )
    assert _card_body(result) == CARD_WITHOUT_PRICE


async def test_the_clinic_catalog_branch_card_hides_a_differing_price():
    doctor = _differs()
    result = await fr.route(
        _conversation(flow_step=fr.STEP_AWAITING_CATALOG_SERVICE, flow_selected_type=None),
        _tenant(),
        _MinutesAgenda(),
        "Consulta",
        professionals=[doctor, _doctor(name="Dr. Outro", entries=[])],
    )
    assert _card_body(result) == CARD_WITHOUT_PRICE
    assert result.flow_selected_professional_id == doctor.id


async def test_the_scoped_service_help_never_sees_a_hidden_price(monkeypatch):
    seen = []

    async def _help(*, conversation_id, services, patient_message, final_round):
        seen.append(services)
        return SimpleNamespace(kind="clarify", question="Qual?", choice=None, message=None)

    monkeypatch.setattr(fr, "run_service_help", _help)
    doctor = _differs()
    await fr.route(
        _conversation(
            flow_step=fr.STEP_SERVICE_HELP,
            flow_selected_type=None,
            flow_selected_professional_id=doctor.id,
        ),
        _tenant(),
        _MinutesAgenda(),
        "não sei qual",
        professionals=[doctor, _doctor(name="Dr. Outro")],
    )
    [[service]] = seen
    assert service["name"] == "Consulta" and service.get("price") is None


# --------------------------------------------------------------------------
# A7: the confirmation line once the unit is known
# --------------------------------------------------------------------------


async def test_picking_a_unit_sends_its_values_once_before_the_day_picker():
    doctor = _differs()
    picked = await _pick_unit(doctor, CENTRO)
    assert picked.flow_step == fr.STEP_AWAITING_DAY
    first, picker = picked.bubbles
    assert isinstance(first, TextBubble)
    assert first.body == "Consulta na Unidade Centro: R$ 250,00 · 30 min"
    assert isinstance(picker, SlotsBubble)
    sede = await _pick_unit(doctor, SEDE)
    assert _texts(sede) == ["Consulta na Unidade Sede: R$ 200,00 · 30 min"]


async def test_the_line_never_doubles_the_word_unidade_and_drops_a_missing_price():
    named = UnitOption(id=uuid4(), name="Unidade Centro", address=None, business_hours=TUESDAY)
    doctor = _doctor([SEDE, named], {SID: {named.id: UnitTerms("R$ 250,00", 40)}})
    picked = await _pick_unit(doctor, named)
    assert _texts(picked) == ["Consulta na Unidade Centro: R$ 250,00 · 40 min"]

    no_base_price = {**CONSULTA, "price": None}
    priced_elsewhere = _doctor(
        [SEDE, CENTRO],
        {SID: {CENTRO.id: UnitTerms("R$ 250,00", None)}},
        entries=[no_base_price],
    )
    at_sede = await _pick_unit(priced_elsewhere, SEDE)
    assert _texts(at_sede) == ["Consulta na Unidade Sede: 30 min"]


async def test_no_line_under_a_convenio_or_when_the_price_is_the_same():
    under_plan = await _pick_unit(_differs(), CENTRO, flow_selected_insurance="Unimed")
    assert _texts(under_plan) == []
    particular = await _pick_unit(_differs(), CENTRO, flow_selected_insurance="Particular")
    assert _texts(particular) == ["Consulta na Unidade Centro: R$ 250,00 · 30 min"]
    same_price = await _pick_unit(_longer_at_centro(), CENTRO)
    assert _texts(same_price) == []


async def test_the_line_is_not_repeated_for_the_unit_already_chosen():
    doctor = _differs()
    # The day picker drawn again for the SAME unit (a resumed day step, a re-entry through the
    # service confirmation): no second line.
    resumed = await fr.resume_bubbles(
        _conversation(
            flow_step=fr.STEP_AWAITING_DAY,
            flow_selected_professional_id=doctor.id,
            flow_selected_unit_id=CENTRO.id,
        ),
        _tenant(),
        _MinutesAgenda(),
        professionals=[doctor],
    )
    assert resumed.flow_step == fr.STEP_AWAITING_DAY and _texts(resumed) == []
    again = await fr.route(
        _conversation(flow_selected_professional_id=doctor.id, flow_selected_unit_id=CENTRO.id),
        _tenant(),
        _MinutesAgenda(),
        fr.LABEL_BOOK_SERVICE,
        professionals=[doctor],
    )
    assert again.flow_step == fr.STEP_AWAITING_DAY and _texts(again) == []


async def test_no_line_when_the_only_unit_was_confirmed():
    # R2: the card already showed Centro's price, so "Sim" opens the days without a line.
    doctor = _doctor(
        [SEDE, CENTRO],
        {SID: {CENTRO.id: UnitTerms("R$ 250,00", None)}},
        {SID: frozenset({CENTRO.id})},
    )
    yes = await fr.route(
        _conversation(
            flow_step=STEP_AWAITING_UNIT_CONFIRM, flow_selected_professional_id=doctor.id
        ),
        _tenant(),
        _MinutesAgenda(),
        fr.LABEL_YES,
        professionals=[doctor],
    )
    assert yes.flow_step == fr.STEP_AWAITING_DAY
    assert yes.flow_selected_unit_id == CENTRO.id
    assert _texts(yes) == []


# --------------------------------------------------------------------------
# A5 / A6: the unit's duration drives the offers, the hold and the event (SPEC §10 item 4)
# --------------------------------------------------------------------------


async def test_centro_offers_and_books_40_minute_windows_sede_keeps_30():
    doctor = _longer_at_centro()
    agenda = _MinutesAgenda()
    booked = await _book(doctor, CENTRO, agenda)
    assert agenda.minutes == [("days", 40), ("slots", 40)]
    [(unit_id, start, end)] = agenda.log.created
    assert unit_id == CENTRO.id and end - start == timedelta(minutes=40)
    assert booked.appointment["end_at"] - booked.appointment["start_at"] == timedelta(minutes=40)

    at_sede = _MinutesAgenda()
    await _book(doctor, SEDE, at_sede)
    assert at_sede.minutes == [("days", 30), ("slots", 30)]
    [(_unit, start, end)] = at_sede.log.created
    assert end - start == timedelta(minutes=30)


async def test_the_portal_hold_has_the_same_length_as_the_event_would():
    doctor = _longer_at_centro()
    gate = _Gate(armed=True)
    held = await _book(doctor, CENTRO, _MinutesAgenda(), gate=gate)
    assert held.flow_state is FlowState.AWAITING_EMAIL_CODE
    [decided] = gate.decided
    assert decided["end_at"] - decided["start_at"] == timedelta(minutes=40)
    assert decided["unit_id"] == CENTRO.id


async def test_the_unit_confirmation_yes_opens_days_of_the_units_length():
    doctor = _doctor(
        [SEDE, CENTRO], {SID: {CENTRO.id: UnitTerms(None, 50)}}, {SID: frozenset({CENTRO.id})}
    )
    agenda = _MinutesAgenda()
    await fr.route(
        _conversation(
            flow_step=STEP_AWAITING_UNIT_CONFIRM, flow_selected_professional_id=doctor.id
        ),
        _tenant(),
        agenda,
        fr.LABEL_YES,
        professionals=[doctor],
    )
    assert agenda.minutes == [("days", 50)]


async def test_an_implied_single_unit_slots_with_its_length_and_a_retry_relists_it():
    doctor = _doctor([CENTRO], {SID: {CENTRO.id: UnitTerms(None, 45)}})
    agenda = _MinutesAgenda()
    days = await fr.route(
        _conversation(flow_selected_professional_id=doctor.id),
        _tenant(),
        agenda,
        fr.LABEL_BOOK_SERVICE,
        professionals=[doctor],
    )
    assert days.flow_selected_unit_id == CENTRO.id
    retry = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_RETRY,
            flow_selected_professional_id=doctor.id,
            flow_selected_unit_id=CENTRO.id,
            flow_selected_day="2099-01-06",
        ),
        _tenant(),
        agenda,
        fr.LABEL_RETRY_YES,
        professionals=[doctor],
    )
    assert retry.flow_step == fr.STEP_AWAITING_SLOT
    assert agenda.minutes == [("days", 45), ("slots", 45)]


async def test_two_doctors_at_the_same_unit_keep_their_own_values():
    """SPEC §10 item 3."""
    ana = _doctor([SEDE, CENTRO], {SID: {CENTRO.id: UnitTerms("R$ 250,00", 40)}}, name="Dra. Ana")
    beto = _doctor([SEDE, CENTRO], {SID: {CENTRO.id: UnitTerms("R$ 300,00", 50)}}, name="Dr. Beto")
    for doctor, price, minutes in ((ana, "R$ 250,00", 40), (beto, "R$ 300,00", 50)):
        agenda = _MinutesAgenda()
        picked = await fr.route(
            _conversation(flow_step=STEP_AWAITING_UNIT, flow_selected_professional_id=doctor.id),
            _tenant(),
            agenda,
            _unit_tap(CENTRO),
            professionals=[ana, beto],
        )
        assert _texts(picked) == [f"Consulta na Unidade Centro: {price} · {minutes} min"]
        assert agenda.minutes == [("days", minutes)]
        confirm = await fr.route(
            _conversation(
                flow_step=fr.STEP_AWAITING_CONFIRMATION,
                flow_selected_professional_id=doctor.id,
                flow_selected_unit_id=CENTRO.id,
                flow_selected_day="2099-01-06",
                flow_selected_slot="2099-01-06T15:00",
            ),
            _tenant(),
            agenda,
            fr.LABEL_CONFIRM,
            professionals=[ana, beto],
        )
        assert confirm.appointment["end_at"] - confirm.appointment["start_at"] == timedelta(
            minutes=minutes
        )


async def test_single_doctor_clinic_takes_the_owners_exception_at_the_unit():
    """R13 with an exception: the implicit owner's Centro length sizes the event."""
    doctor = _longer_at_centro()
    agenda = _MinutesAgenda()
    await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_CONFIRMATION,
            flow_selected_unit_id=CENTRO.id,
            flow_selected_day="2099-01-06",
            flow_selected_slot="2099-01-06T15:00",
        ),
        _tenant(),
        agenda,
        fr.LABEL_CONFIRM,
        professionals=[doctor],
    )
    [(unit_id, start, end)] = agenda.log.created
    assert unit_id == CENTRO.id and end - start == timedelta(minutes=40)


# --------------------------------------------------------------------------
# A14 + the AI landing's slot length
# --------------------------------------------------------------------------


def _details(doctor, unit):
    return bd._booking_details(
        SimpleNamespace(service=CONSULTA),
        SimpleNamespace(
            flow_selected_professional_id=doctor.id,
            flow_selected_insurance=None,
            flow_attendee_name="",
        ),
        conversation=SimpleNamespace(flow_state=FlowState.LLM),
        tenant=_tenant(initial_flows={"ai_draft_v2": True}),
        owner=doctor,
        unit=unit,
    )


def test_the_ai_details_message_prices_the_service_at_the_unit():
    doctor = _differs()
    assert "🏥 Serviço: Consulta — R$ 250,00" in _details(doctor, CENTRO)
    assert "🏥 Serviço: Consulta — R$ 200,00" in _details(doctor, SEDE)
    # No unit known and the price differs: no price at all (D3), never a range.
    unknown = _details(doctor, None)
    assert "🏥 Serviço: Consulta\n" in unknown and "R$" not in unknown


async def test_the_ai_landing_on_a_day_scans_with_the_units_length():
    doctor = _longer_at_centro()
    doctor.context_doctor_message = None
    agenda = _MinutesAgenda()

    async def _calendar(_professional):
        return agenda

    conversation = _conversation(
        flow_state=FlowState.LLM,
        flow_step=None,
        tenant_id=None,
        flow_draft=None,
        flow_selected_unit_id=CENTRO.id,
    )
    day = (datetime.now(TZ) + timedelta(days=2)).date()
    await resolve_booking_draft(
        BookingDraft(service="Consulta", day=day, attendee="self"),
        conversation=conversation,
        tenant=_tenant(),
        professional_rows=[doctor],
        service_catalog=[],
        tenant_insurance=None,
        calendar=_calendar,
        now=datetime.now(TZ),
    )
    assert ("slots", 40) in agenda.minutes
    assert all(minutes == 40 for _kind, minutes in agenda.minutes)
