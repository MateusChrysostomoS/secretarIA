"""The AI draft waits on the convênio, doctor and service questions too (TASK-030 P3).

Router and resolver level, pure. The owner's 2026-10-03 ruling: when the resolver lands on
any booking question and the draft still holds what comes after it, the patient's ANSWER
resumes the draft instead of falling into the next button step.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.booking_draft import (  # noqa: E402
    BookingDraft,
    draft_from_record,
    draft_record,
)
from tests.test_booking_draft_resolver import (  # noqa: E402
    DAY,
    NOW,
    _doctor,
    _resolve,
    _tenant as _resolver_tenant,
)
from tests.test_flow_router import _conversation, _FakeCalendar, _tenant  # noqa: E402

RECORD = draft_record(
    BookingDraft(
        service="Primeira Consulta", attendee="self", day=dt.date(2026, 10, 8), time=dt.time(10, 0)
    ),
    saved_at=dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC),
)


def _doctor_snapshot(name):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=None,
        business_hours=None,
    )


ANA = _doctor_snapshot("Dra. Ana")
BETO = _doctor_snapshot("Dr. Beto")


def _collecting():
    tenant = _tenant()
    tenant.collect_insurance = True
    tenant.insurance_mode = "shared"
    tenant.insurances = ["Unimed"]
    return tenant


def _at(step, *, draft=RECORD, **kw):
    return _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=step,
        flow_attendee_name="",
        flow_draft=draft,
        **kw,
    )


# --------------------------------------------------------------------------
# Router: which answers resume, which keep waiting, which drop the draft
# --------------------------------------------------------------------------


async def test_the_convenio_answer_asks_the_worker_to_resume():
    res = await fr.route(
        _at(fr.STEP_AWAITING_INSURANCE, flow_selected_type="Primeira Consulta"),
        _collecting(),
        _FakeCalendar(),
        "Unimed",
    )
    assert res.resume_draft is True
    assert res.flow_selected_insurance == "Unimed"


async def test_outro_convenio_keeps_waiting_for_the_typed_name():
    res = await fr.route(
        _at(fr.STEP_AWAITING_INSURANCE, flow_selected_type="Primeira Consulta"),
        _collecting(),
        _FakeCalendar(),
        fr.LABEL_INSURANCE_OTHER,
    )
    assert res.flow_step == fr.STEP_AWAITING_INSURANCE
    assert res.resume_draft is False
    assert res.flow_draft == RECORD


async def test_the_doctor_tap_asks_the_worker_to_resume():
    res = await fr.route(
        _at(fr.STEP_AWAITING_PROFESSIONAL, flow_selected_type="Primeira Consulta"),
        _tenant(),
        None,
        "Dr. Beto",
        professionals=[ANA, BETO],
    )
    assert res.resume_draft is True
    assert res.flow_selected_professional_id == BETO.id


async def test_the_service_tap_asks_the_worker_to_resume():
    res = await fr.route(_at(fr.STEP_AWAITING_SERVICE), _tenant(), None, "Primeira Consulta")
    assert res.resume_draft is True
    assert res.flow_selected_type == "Primeira Consulta"


async def test_free_text_on_a_question_keeps_the_draft_for_the_llm():
    res = await fr.route(
        _at(fr.STEP_AWAITING_PROFESSIONAL),
        _tenant(),
        None,
        "hmm, qual deles é melhor?",
        professionals=[ANA, BETO],
    )
    assert res.action == "delegate_llm"
    assert res.flow_draft == RECORD
    assert res.resume_draft is False


async def test_the_help_row_drops_the_draft():
    res = await fr.route(
        _at(fr.STEP_AWAITING_PROFESSIONAL),
        _tenant(),
        None,
        fr.LABEL_DONT_KNOW,
        professionals=[ANA, BETO],
    )
    assert res.flow_step == fr.STEP_PROFESSIONAL_HELP
    assert res.flow_draft is None
    assert res.resume_draft is False


@pytest.mark.parametrize(
    "step, body, extra",
    [
        (fr.STEP_AWAITING_INSURANCE, "Unimed", {"flow_selected_type": "Primeira Consulta"}),
        (fr.STEP_AWAITING_PROFESSIONAL, "Dr. Beto", {}),
        (fr.STEP_AWAITING_SERVICE, "Primeira Consulta", {}),
    ],
)
async def test_without_a_parked_draft_the_buttons_are_untouched(step, body, extra):
    res = await fr.route(
        _at(step, draft=None, **extra),
        _collecting(),
        _FakeCalendar(),
        body,
        professionals=[ANA, BETO] if step == fr.STEP_AWAITING_PROFESSIONAL else None,
    )
    assert res.resume_draft is False
    assert res.flow_draft is None


def test_the_draft_wait_steps_are_pra_quem_plus_the_three_questions():
    assert fr.DRAFT_WAIT_STEPS == (
        *fr.ATTENDEE_STEPS,
        fr.STEP_AWAITING_INSURANCE,
        fr.STEP_AWAITING_PROFESSIONAL,
        fr.STEP_AWAITING_SERVICE,
    )


# --------------------------------------------------------------------------
# Resolver: a question landing parks what comes after it (switch on)
# --------------------------------------------------------------------------


def _switched_resolver_tenant(**kw):
    return _resolver_tenant(initial_flows={"ai_draft_v2": True}, **kw)


def _collecting_resolver_tenant(**kw):
    return _switched_resolver_tenant(
        collect_insurance=True, insurance_mode="shared", insurances=["Unimed"], **kw
    )


async def test_a_convenio_question_parks_the_day_and_time_with_the_switch_on():
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)),
        tenant=_collecting_resolver_tenant(),
    )
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.service, parked.day, parked.time) == ("Consulta", DAY, dt.time(10, 0))
    assert res.accepted == ("service", "day", "time")


async def test_without_the_switch_the_question_parks_nothing():
    tenant = _resolver_tenant(
        collect_insurance=True, insurance_mode="shared", insurances=["Unimed"]
    )
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)), tenant=tenant
    )
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    assert res.result.flow_draft is None
    assert res.accepted == ("service",)


async def test_a_service_alone_also_waits_so_the_details_are_not_skipped():
    res = await _resolve(BookingDraft(service="Consulta"), tenant=_collecting_resolver_tenant())
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    assert draft_from_record(res.result.flow_draft, now=NOW).service == "Consulta"


async def test_a_doctor_question_parks_the_service_and_the_day():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Consulta"])
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY),
        tenant=_switched_resolver_tenant(),
        pros=[ana, beto],
    )
    assert res.landing_step == fr.STEP_AWAITING_PROFESSIONAL
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.service, parked.day) == ("Consulta", DAY)


async def test_a_question_with_nothing_after_it_parks_nothing():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Retorno"])
    res = await _resolve(
        BookingDraft(professional_id=ana.id), tenant=_switched_resolver_tenant(), pros=[ana, beto]
    )
    assert res.landing_step == fr.STEP_AWAITING_SERVICE
    assert res.result.flow_draft is None


@pytest.mark.parametrize("outcome", ["full", "unavailable", "incomplete"])
async def test_answer_preserves_terminal_or_failed_booking_continuation(outcome):
    tenant = _collecting()
    professionals = None
    if outcome == "incomplete":
        doctor = _doctor_snapshot("Dra. Única")
        doctor.business_hours = {}
        professionals = [doctor]
    calendar = _FakeCalendar(days=[], unavailable=outcome == "unavailable")
    result = await fr.route(
        _at(fr.STEP_AWAITING_INSURANCE, flow_selected_type="Primeira Consulta"),
        tenant,
        calendar,
        "Unimed",
        professionals=professionals,
    )
    assert result.resume_draft is False
    if outcome == "unavailable":
        assert result.action == "calendar_unavailable"
    elif outcome == "incomplete":
        assert result.action == "professional_config_incomplete"
    else:
        assert result.flow_state == FlowState.MENU
        assert result.bubbles[0].body == fr.NO_AVAILABLE_DAYS_MESSAGE
    assert result.flow_draft is None
