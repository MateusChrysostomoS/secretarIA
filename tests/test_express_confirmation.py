"""Express confirmation: a complete AI draft lands on the details and the card (TASK-030 P3).

Resolver level (services/booking_draft.py), pure: fake agenda, no database. Helpers come
from tests/test_booking_draft_resolver.py (P2a).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
from datetime import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import booking_draft as bd, flow_router as fr  # noqa: E402
from secretaria.services.booking_draft import BookingDraft, resolve_booking_draft  # noqa: E402
from tests.test_booking_draft_resolver import (  # noqa: E402
    _HELD_1000,
    DAY,
    TZ,
    _Cal,
    _conv,
    _doctor,
    _HoldGate,
    _resolve,
    _sole,
    _source,
    _tenant,
)

TEN = dt.time(10, 0)
ADDRESS = "Rua A, 1, Centro, São Paulo/SP, CEP 01000-000"
FULL = {
    "name": "Consulta",
    "duration_min": 30,
    "is_active": True,
    "sort_order": 0,
    "price": "250",
    "long_description": "Avaliação completa.",
    "requirements": ["Jejum de 8 horas", "Trazer exames anteriores"],
}


def _on(**kw):
    return _tenant(initial_flows={"ai_draft_v2": True}, **kw)


def _kinds(resolution):
    return [type(bubble).__name__ for bubble in resolution.result.bubbles]


async def test_a_full_draft_on_a_sole_clinic_lands_on_details_and_the_card():
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=TEN), tenant=_on(), pros=[_sole()]
    )
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    details, card = res.result.bubbles
    assert isinstance(details, TextBubble) and isinstance(card, ButtonBubble)
    assert details.body == (
        "Confira os detalhes da sua consulta:\n\n"
        "🥼 Profissional: Dra. Única\n"
        "🏥 Serviço: Consulta\n"
        "👤 Para: você"
    )
    assert card.body == "Consulta\nProfissional: Dra. Única\n08/10/2026 às 10:00"
    assert (res.result.flow_selected_day, res.result.flow_selected_slot) == (
        "2026-10-08",
        "2026-10-08T10:00",
    )
    assert res.result.flow_selected_type == "Consulta"
    assert res.accepted == ("service", "day", "time")
    assert res.fallback is None


async def test_the_card_and_the_stored_slot_are_the_same_instant():
    """skill date-derived-ui-labels: the label the patient reads and the slot `Confirmar`
    books derive from ONE value."""
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=TEN), tenant=_on(), pros=[_sole()]
    )
    stored = datetime.fromisoformat(res.result.flow_selected_slot)
    assert stored.strftime(fr.WHEN_FORMAT) in res.result.bubbles[-1].body


async def test_every_detail_of_a_multi_doctor_booking_for_someone_else():
    ana = _doctor("Dra. Ana", [])
    ana.specialty = "Cardiologia"
    ana.appointment_types = [dict(FULL)]
    beto = _doctor("Dr. Beto", ["Retorno"])
    asked: list = []
    tenant = _on(
        collect_insurance=True,
        insurance_mode="shared",
        insurances=["Unimed"],
        clinic_address=ADDRESS,
    )
    res = await resolve_booking_draft(
        BookingDraft(
            service="Consulta",
            professional_id=ana.id,
            insurance="unimed",
            attendee="other",
            day=DAY,
            time=TEN,
        ),
        conversation=_conv(flow_attendee_name="Maria da Silva"),
        tenant=tenant,
        professional_rows=[ana, beto],
        service_catalog=[],
        tenant_insurance=None,
        calendar=_source(_Cal(), asked),
        now=dt.datetime(2026, 10, 5, 9, 0, tzinfo=TZ),
    )
    details, card = res.result.bubbles
    assert details.body == (
        "Confira os detalhes da sua consulta:\n\n"
        "🥼 Profissional: Dra. Ana — Cardiologia\n"
        "🏥 Serviço: Consulta — R$250\n"
        "Convênio: Unimed\n"
        "👤 Para: Maria da Silva\n"
        f"Endereço: {ADDRESS}\n\n"
        "Avaliação completa.\n\n"
        "O que levar / preparo:\n"
        "• Jejum de 8 horas\n"
        "• Trazer exames anteriores"
    )
    assert card.body == (
        "Consulta\nProfissional: Dra. Ana\nPaciente: Maria da Silva\n08/10/2026 às 10:00"
    )
    assert res.result.flow_selected_professional_id == ana.id
    assert res.result.flow_selected_insurance == "Unimed"
    assert res.result.flow_attendee_name == "Maria da Silva"
    assert res.accepted == ("service", "professional", "insurance", "for_whom", "day", "time")
    assert asked == [ana]


async def test_the_express_landing_books_nothing():
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=TEN), tenant=_on(), pros=[_sole()]
    )
    assert res.result.appointment is None
    assert res.result.booking_hold is None
    assert res.result.appointment_cancel_id is None
    assert res.result.appointment_reschedule is None


async def test_details_are_skipped_when_the_patient_already_saw_them():
    seen = _conv(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SLOT,
        flow_selected_type="Consulta",
        flow_selected_day="2026-10-08",
    )
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=TEN), conv=seen, tenant=_on(), pros=[_sole()]
    )
    assert _kinds(res) == ["ButtonBubble"]
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION


async def test_a_different_service_shows_the_details_again():
    seen = _conv(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SLOT,
        flow_selected_type="Retorno",
    )
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=TEN), conv=seen, tenant=_on(), pros=[_sole()]
    )
    assert _kinds(res) == ["TextBubble", "ButtonBubble"]


async def test_without_the_switch_the_patient_lands_on_the_slot_list_as_in_p2():
    res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=TEN), pros=[_sole()])
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert _kinds(res) == ["SlotsBubble"]


async def test_a_time_held_on_the_sole_doctors_agenda_lands_on_the_slot_list():
    sole = _sole()
    gate = _HoldGate({sole.id: [_HELD_1000]})
    with fr.booking_gate_scope(gate):
        res = await _resolve(
            BookingDraft(service="Consulta", day=DAY, time=TEN), tenant=_on(), pros=[sole]
        )
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.dropped == {"time": "no_free_slot"}
    assert _kinds(res) == ["TextBubble", "SlotsBubble"]
    rows = [row[0] for row in res.result.bubbles[1].rows]
    assert "slot|2026-10-08T10:00" not in rows
    assert set(gate.asked) == {sole.id}


async def test_a_day_picker_landing_opens_with_the_details():
    res = await _resolve(BookingDraft(service="Consulta"), tenant=_on(), pros=[_sole()])
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert _kinds(res) == ["TextBubble", "SlotsBubble"]


async def _resolve_at(draft, *, now, cal, pros):
    return await resolve_booking_draft(
        draft,
        conversation=_conv(),
        tenant=_on(),
        professional_rows=list(pros),
        service_catalog=[],
        tenant_insurance=None,
        calendar=_source(cal),
        now=now,
    )


async def test_today_with_the_time_already_past_lands_on_todays_remaining_slots():
    # The calendar's own walk never offers a slot that already started
    # (CalendarService._walk_free_slots, `cursor < now`); the fake mirrors that.
    afternoon = dt.datetime(2026, 10, 8, 14, 0, tzinfo=TZ)
    res = await _resolve_at(
        BookingDraft(service="Consulta", day=DAY, time=TEN),
        now=afternoon,
        cal=_Cal({DAY: ["14:30", "15:00"]}),
        pros=[_sole()],
    )
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.dropped == {"time": "no_free_slot"}
    assert [row[0] for row in res.result.bubbles[-1].rows][:2] == [
        "slot|2026-10-08T14:30",
        "slot|2026-10-08T15:00",
    ]


async def test_today_with_nothing_left_lands_on_the_day_picker():
    afternoon = dt.datetime(2026, 10, 8, 14, 0, tzinfo=TZ)
    res = await _resolve_at(
        BookingDraft(service="Consulta", day=DAY, time=TEN),
        now=afternoon,
        cal=_Cal({DAY: [], dt.date(2026, 10, 9): ["09:00"]}),
        pros=[_sole()],
    )
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.dropped == {"day": "day_unavailable", "time": "missing_day"}
    assert res.result.bubbles[-1].body.startswith(bd.DRAFT_DAY_UNAVAILABLE_PREFIX)


async def test_express_confirmation_is_off_without_the_switch():
    state = fr._DayPickerState(flow_selected_type="Consulta", flow_attendee_name="")
    assert (
        await bd._express_confirmation(
            state=state,
            tenant=_tenant(),
            professional=None,
            service={"name": "Consulta"},
            slot_start=dt.datetime(2026, 10, 8, 10, 0, tzinfo=TZ),
            duration_minutes=30,
            calendar=_Cal(),
            professionals=[],
        )
        is None
    )


@pytest.mark.parametrize(
    "state, step, recorded, professional_id, expected",
    [
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_SERVICE_CONFIRM, "Consulta", None, True),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_DAY_RETRY, "consulta", None, True),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_SLOT, "Consulta", None, True),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_CONFIRMATION, "Consulta", None, True),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_RETRY, "Consulta", None, True),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_INSURANCE, "Consulta", None, False),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_SLOT, "Retorno", None, False),
        (FlowState.SERVICE_CATALOG, fr.STEP_AWAITING_SLOT, "Consulta", "outro-medico", False),
        (FlowState.LLM, None, "Consulta", None, False),
        (
            FlowState.SERVICE_CATALOG,
            fr.STEP_AWAITING_ATTENDEE_CHOICE,
            fr.ATTENDEE_NEXT_BOOK,
            None,
            False,
        ),
    ],
)
def test_details_already_shown(state, step, recorded, professional_id, expected):
    conversation = SimpleNamespace(
        flow_state=state,
        flow_step=step,
        flow_selected_type=recorded,
        flow_selected_professional_id=professional_id,
    )
    assert (
        bd.details_already_shown(conversation, service_name="Consulta", professional_id=None)
        is expected
    )


def test_no_conversation_means_not_shown():
    assert bd.details_already_shown(None, service_name="Consulta", professional_id=None) is False
