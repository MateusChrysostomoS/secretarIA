"""Regression: the current (flag-OFF) handback must carry day and who, too."""

import datetime as dt
from uuid import uuid4

import pytest

from secretaria.ai.prompts import secretary_system_prompt
from secretaria.ai.tools import BookingDraftRequested, set_booking_draft
from secretaria.services import flow_router as fr
from secretaria.services.booking_draft import BookingDraft
from tests.test_booking_draft_resolver import DAY, _Cal, _conv, _doctor, _resolve, _sole, _tenant
from tests.test_prompts import _config
from tests.test_set_booking_draft import sole as _sole_fixture
from tests.test_set_booking_draft_v2 import ANA, clinic as _clinic_fixture

sole = _sole_fixture
clinic = _clinic_fixture


async def test_flag_off_tool_carries_the_patient_and_the_requested_day(sole):
    with pytest.raises(BookingDraftRequested) as caught:
        await set_booking_draft.ainvoke(
            {"service": "Limpeza", "insurance": "Particular", "for_whom": "me", "day": "2026-10-08"}
        )
    assert caught.value.draft.attendee == "self"
    assert caught.value.draft.day == DAY
    assert caught.value.draft.insurance == "Particular"


async def test_unknown_who_remains_unknown_when_no_new_fields_are_given(sole):
    with pytest.raises(BookingDraftRequested) as caught:
        await set_booking_draft.ainvoke({"service": "Limpeza"})
    assert caught.value.draft.attendee is None
    assert caught.value.draft.day is None


@pytest.mark.parametrize("text, day", [
    ("07/10", dt.date(2026, 10, 7)),
    ("amanhã", dt.date(2026, 10, 7)),
    ("quinta da semana que vem", dt.date(2026, 10, 15)),
])
async def test_current_tool_accepts_clinic_local_date_expressions(sole, monkeypatch, text, day):
    from secretaria.services import booking_dates

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 10, 7, 1, 30, tzinfo=dt.UTC)

    monkeypatch.setattr(booking_dates, "datetime", Clock)
    with pytest.raises(BookingDraftRequested) as caught:
        await set_booking_draft.ainvoke({"service": "Limpeza", "for_whom": "me", "day": text})
    assert caught.value.draft.day == day
    assert caught.value.draft.attendee == "self"


async def test_invalid_date_does_not_start_a_booking(sole):
    out = await set_booking_draft.ainvoke({"service": "Limpeza", "day": "31/02"})
    assert "error" in out


async def test_unambiguous_first_name_keeps_the_chosen_doctor(clinic):
    with pytest.raises(BookingDraftRequested) as caught:
        await set_booking_draft.ainvoke(
            {"professional": "Ana", "for_whom": "me", "day": "2026-10-08"}
        )
    assert caught.value.draft.professional_id == ANA.id


async def test_ambiguous_doctor_correction_never_reuses_the_previous_doctor(clinic):
    roster, _log = clinic
    roster[:] = [
        _doctor("Dr. Diogo Raposo", ["Consulta"]),
        _doctor("Dr. Diogo Silva", ["Consulta"]),
        _doctor("Dr. Rafael", ["Consulta"]),
    ]
    with pytest.raises(BookingDraftRequested) as caught:
        await set_booking_draft.ainvoke({
            "service": "Consulta", "professional": "Diogo", "for_whom": "me",
            "day": DAY.isoformat(), "time": "10:00",
        })
    draft = BookingDraft.from_payload(caught.value.draft.to_payload())
    res = await _resolve(
        draft, tenant=_tenant(), pros=roster,
        conv=_conv(flow_selected_professional_id=roster[-1].id),
    )
    assert res.landing_step == fr.STEP_AWAITING_PROFESSIONAL
    assert res.result.flow_draft["d"] == DAY.isoformat()
    assert res.result.flow_draft["h"] == "10:00"
    assert res.result.flow_selected_professional_id is None


@pytest.mark.parametrize("doctor_count", [1, 2])
async def test_doctor_removed_before_fresh_resolution_is_not_replaced_implicitly(doctor_count):
    roster = [_doctor("Dr. Rafael", ["Consulta"])]
    if doctor_count == 2:
        roster.append(_doctor("Dra. Ana", ["Limpeza"]))
    res = await _resolve(
        BookingDraft(
            service="Consulta", professional_id=uuid4(), attendee="self",
            day=DAY, time=dt.time(10),
        ),
        tenant=_tenant(), pros=roster,
    )
    assert res.landing_step == fr.STEP_AWAITING_PROFESSIONAL
    assert res.result.flow_draft["d"] == DAY.isoformat()
    assert res.result.flow_selected_professional_id is None
    assert res.result.flow_draft["p_unresolved"] is True


async def test_full_explicit_request_reaches_confirmation_even_with_rollout_off():
    res = await _resolve(
        BookingDraft(service="Consulta", attendee="self", day=DAY, time=dt.time(10)),
        conv=_conv(flow_attendee_name=None), tenant=_tenant(), pros=[_sole()],
    )
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert res.result.flow_selected_slot == f"{DAY.isoformat()}T10:00"
    assert res.result.flow_attendee_name == ""
    assert res.result.action == "reply"  # preparing a card never books


async def test_known_self_and_day_ask_only_the_time_with_rollout_off():
    res = await _resolve(
        BookingDraft(service="Consulta", attendee="self", day=DAY),
        conv=_conv(flow_attendee_name=None), tenant=_tenant(), pros=[_sole()],
    )
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.result.flow_selected_day == DAY.isoformat()
    assert res.result.flow_attendee_name == ""


async def test_missing_service_preserves_day_on_flag_off_question():
    res = await _resolve(
        BookingDraft(attendee="self", day=DAY), tenant=_tenant(), pros=[_sole()],
    )
    assert res.landing_step == fr.STEP_AWAITING_SERVICE
    assert res.result.flow_draft["d"] == DAY.isoformat()


async def test_no_guessing_an_unavailable_time_with_rollout_off():
    res = await _resolve(
        BookingDraft(service="Consulta", attendee="self", day=DAY, time=dt.time(9)),
        tenant=_tenant(), pros=[_sole()], cal=_Cal({DAY: ["10:00"]}),
    )
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.result.flow_selected_slot is None


def test_prompt_dates_are_local_and_teach_relative_dates():
    prompt = secretary_system_prompt(
        _config(conversation_state="O paciente tocou Outro."),
        now=dt.datetime(2026, 10, 7, 1, 30, tzinfo=dt.UTC),
    )
    assert "Hoje é 2026-10-06" in prompt
    assert "2026-10-07" in prompt and "2026-10-15" in prompt
    assert "amanhã" in prompt and "semana que vem" in prompt
    assert 'for_whom="me"' in prompt
    assert "Particular" in prompt
    assert "set_booking_draft" in prompt
