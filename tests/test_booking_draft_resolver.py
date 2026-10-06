"""resolve_booking_draft: where the AI's draft lands, item by item (TASK-030 P2, spec §4.2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import booking_draft as bd, flow_router as fr  # noqa: E402
from secretaria.services.attendee import (  # noqa: E402
    ATTENDEE_NAME_REQUEST,
    ATTENDEE_QUESTION_BODY,
    ATTENDEE_SELF,
)
from secretaria.services.booking_draft import (  # noqa: E402
    BookingDraft,
    draft_from_record,
    resolve_booking_draft,
)
from secretaria.services.calendar import CalendarUnavailableError  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
NOW = dt.datetime(2026, 10, 5, 9, 0, tzinfo=TZ)  # a Monday
DAY = dt.date(2026, 10, 8)  # the Thursday after


def _tenant(**kw):
    base = dict(
        initial_flows={},
        appointment_types=[
            {"name": "Consulta", "duration_min": 30, "is_active": True, "sort_order": 0},
            {"name": "Retorno", "duration_min": 20, "is_active": True, "sort_order": 1},
        ],
        appointment_duration_min=30,
        business_hours={"thursday": [{"start": "08:00", "end": "12:00"}]},
        collect_insurance=False,
        insurance_mode=None,
        insurances=[],
        insurance_plans=None,
        insurance_accepted_by={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _doctor(name, services, hours=None):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=[
            {"name": s, "duration_min": 30, "is_active": True, "sort_order": i}
            for i, s in enumerate(services)
        ],
        business_hours=(
            hours if hours is not None else {"thursday": [{"start": "08:00", "end": "12:00"}]}
        ),
    )


def _sole():
    """The single professional: inherits the clinic's services and hours (NULL columns)."""
    return SimpleNamespace(
        id=uuid4(),
        name="Dra. Única",
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=None,
        business_hours=None,
    )


def _conv(**kw):
    base = dict(
        id=uuid4(),
        tenant_id=None,  # no tenant: the default gate answers "no holds" without a DB
        flow_state=FlowState.LLM,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        flow_attendee_name=ATTENDEE_SELF,
        flow_draft=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _Cal:
    """Free times per day; can be made unavailable."""

    def __init__(self, free=None, unavailable=False):
        self.tzinfo = TZ
        self.free = free if free is not None else {DAY: ["08:00", "08:30", "10:00"]}
        self.unavailable = unavailable
        self.slot_reads: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        if self.unavailable:
            raise CalendarUnavailableError("down")
        return [
            dt.datetime(d.year, d.month, d.day, tzinfo=TZ)
            for d in sorted(self.free)
            if self.free[d]
        ]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self.unavailable:
            raise CalendarUnavailableError("down")
        self.slot_reads.append((day.date(), max_slots))
        times = self.free.get(day.date(), [])[:max_slots]
        return [{"start": f"{day.date().isoformat()}T{t}", "end": "", "label": t} for t in times]


def _source(cal, asked=None):
    async def _get(professional):
        if asked is not None:
            asked.append(professional)
        return cal

    return _get


async def _resolve(draft, *, conv=None, tenant=None, pros=(), cal=None, asked=None, insurance=None):
    return await resolve_booking_draft(
        draft,
        conversation=conv if conv is not None else _conv(),
        tenant=tenant if tenant is not None else _tenant(),
        professional_rows=list(pros),
        service_catalog=[],
        tenant_insurance=insurance,
        calendar=_source(cal if cal is not None else _Cal(), asked),
        now=NOW,
    )


# --------------------------------------------------------------------------
# Pra quem
# --------------------------------------------------------------------------


async def test_unknown_pra_quem_asks_first_and_parks_the_rest():
    asked: list = []
    res = await _resolve(
        BookingDraft(service="consulta", day=DAY), conv=_conv(flow_attendee_name=None), asked=asked
    )
    assert res.landing_step == fr.STEP_AWAITING_ATTENDEE_CHOICE
    assert res.result.bubbles[0].body == ATTENDEE_QUESTION_BODY
    # The marker keeps its job; the service waits in the draft, canonical.
    assert res.result.flow_selected_type == fr.ATTENDEE_NEXT_BOOK
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.service, parked.day) == ("Consulta", DAY)
    assert res.accepted == ("service", "day")
    assert asked == []  # no agenda is built to ask a question


async def test_other_with_a_service_asks_the_name_and_parks_the_service():
    res = await _resolve(
        BookingDraft(service="Consulta", attendee="other"), conv=_conv(flow_attendee_name=None)
    )
    assert res.landing_step == fr.STEP_AWAITING_ATTENDEE_NAME
    assert res.result.bubbles[0].body == ATTENDEE_NAME_REQUEST
    parked = draft_from_record(res.result.flow_draft, now=NOW)
    assert (parked.service, parked.attendee) == ("Consulta", "other")
    assert res.accepted == ("service", "for_whom")


async def test_other_with_an_authorized_name_already_recorded_goes_on():
    res = await _resolve(
        BookingDraft(service="Consulta", attendee="other"),
        conv=_conv(flow_attendee_name="Maria da Silva"),
    )
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_attendee_name == "Maria da Silva"


async def test_me_overrides_a_recorded_third_party():
    res = await _resolve(
        BookingDraft(service="Consulta", attendee="self"),
        conv=_conv(flow_attendee_name="Maria da Silva"),
    )
    assert res.result.flow_attendee_name == ATTENDEE_SELF


# --------------------------------------------------------------------------
# Convênio
# --------------------------------------------------------------------------


def _collecting(**kw):
    return _tenant(collect_insurance=True, insurance_mode="shared", insurances=["Unimed"], **kw)


async def test_an_unknown_plan_is_dropped_and_the_convenio_is_asked():
    res = await _resolve(
        BookingDraft(service="Consulta", insurance="Plano X"), tenant=_collecting()
    )
    assert res.landing_step == fr.STEP_AWAITING_INSURANCE
    assert res.dropped == {"insurance": "unmatched_plan"}
    assert res.result.flow_selected_type == "Consulta"  # rides to the day picker after it


async def test_a_known_plan_is_recorded_canonically():
    res = await _resolve(BookingDraft(service="Consulta", insurance="unimed"), tenant=_collecting())
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_insurance == "Unimed"


async def test_a_recorded_typed_convenio_is_kept_verbatim():
    """A typed "Outro convênio" is the patient's answer, not something to re-validate."""
    res = await _resolve(
        BookingDraft(service="Consulta"),
        conv=_conv(flow_selected_insurance="Amil Dental"),
        tenant=_collecting(),
    )
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_insurance == "Amil Dental"


@pytest.mark.parametrize("typed, recorded", [("Plano X", None), ("unimed", "Unimed")])
async def test_a_clinic_that_does_not_collect_never_asks_the_convenio(typed, recorded):
    tenant = _tenant(collect_insurance=True, insurance_mode=None, insurances=["Unimed"])
    res = await _resolve(BookingDraft(service="Consulta", insurance=typed), tenant=tenant)
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_insurance == recorded


# --------------------------------------------------------------------------
# Profissional e serviço
# --------------------------------------------------------------------------


async def test_a_doctor_who_does_not_offer_the_service_keeps_the_doctor():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Retorno"])
    res = await _resolve(BookingDraft(service="Retorno", professional_id=ana.id), pros=[ana, beto])
    assert res.landing_step == fr.STEP_AWAITING_SERVICE
    assert res.dropped == {"service": "not_offered_by_professional"}
    assert res.result.flow_selected_professional_id == ana.id
    assert res.accepted == ("professional",)


async def test_a_service_only_one_doctor_offers_picks_that_doctor():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Retorno"])
    asked: list = []
    res = await _resolve(BookingDraft(service="retorno"), pros=[ana, beto], asked=asked)
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_professional_id == beto.id
    assert res.result.flow_selected_type == "Retorno"
    assert asked == [beto]


async def test_a_recorded_doctor_who_does_not_offer_the_new_service_gives_way():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Retorno"])
    res = await _resolve(
        BookingDraft(service="Retorno"),
        conv=_conv(flow_selected_professional_id=ana.id),
        pros=[ana, beto],
    )
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_professional_id == beto.id
    assert res.dropped == {}  # the recorded doctor was not SUPPLIED, so nothing is "dropped"


async def test_a_service_two_doctors_offer_asks_the_doctor_and_keeps_the_service():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Consulta"])
    res = await _resolve(BookingDraft(service="Consulta"), pros=[ana, beto])
    assert res.landing_step == fr.STEP_AWAITING_PROFESSIONAL
    assert res.result.flow_selected_type == "Consulta"
    assert res.result.flow_selected_professional_id is None


async def test_an_unknown_doctor_is_dropped_and_the_doctor_list_shown():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Consulta"])
    res = await _resolve(BookingDraft(professional_id=uuid4()), pros=[ana, beto])
    assert res.landing_step == fr.STEP_AWAITING_PROFESSIONAL
    assert res.dropped == {"professional": "unknown_professional"}


async def test_a_service_not_in_the_catalog_is_dropped_and_the_service_list_shown():
    res = await _resolve(BookingDraft(service="Botox"))
    assert res.landing_step == fr.STEP_AWAITING_SERVICE
    assert res.dropped == {"service": "not_in_catalog"}
    assert res.fallback is None  # the menu is NOT where an invalid item lands


async def test_a_chosen_doctor_with_no_services_reaches_the_clinic_alert():
    ana, beto = _doctor("Dra. Ana", []), _doctor("Dr. Beto", ["Consulta"])
    res = await _resolve(BookingDraft(professional_id=ana.id), pros=[ana, beto])
    assert res.result.action == "professional_config_incomplete"
    assert res.landing_step == "config_incomplete"
    assert res.fallback == "professional_config_incomplete"
    assert res.accepted == ("professional",)


async def test_no_bookable_catalog_falls_back_to_the_plain_menu():
    res = await _resolve(BookingDraft(service="Consulta"), tenant=_tenant(appointment_types=[]))
    assert res.result.flow_state == FlowState.MENU
    assert [type(b).__name__ for b in res.result.bubbles] == ["MenuBubble"]
    assert (res.landing_step, res.fallback) == ("menu", "no_bookable_catalog")


async def test_another_tenants_roster_and_catalog_are_never_used():
    foreign = _doctor("Dr. De Outra Clínica", ["Ortodontia"])
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Consulta"])
    asked: list = []
    res = await _resolve(
        BookingDraft(service="Ortodontia", professional_id=foreign.id),
        pros=[ana, beto],
        asked=asked,
    )
    assert res.dropped == {"professional": "unknown_professional", "service": "not_in_catalog"}
    assert res.landing_step == fr.STEP_AWAITING_PROFESSIONAL
    assert res.result.flow_selected_professional_id is None
    assert asked == []


async def test_a_single_professional_is_implicit():
    asked: list = []
    res = await _resolve(BookingDraft(service="Consulta"), pros=[_sole()], asked=asked)
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.result.flow_selected_professional_id is None
    assert asked == [None]  # the tenant-level agenda, which is the sole doctor's


async def test_the_pra_quem_marker_is_never_read_as_a_service():
    res = await _resolve(BookingDraft(), conv=_conv(flow_selected_type=fr.ATTENDEE_NEXT_BOOK))
    assert res.landing_step == fr.STEP_AWAITING_SERVICE


def test_landing_step_names_the_step_or_the_pseudo_step():
    def r(**kw):
        return fr.FlowRouterResult(**kw)

    assert (
        bd.landing_step(
            r(action="reply", flow_state=FlowState.SERVICE_CATALOG, flow_step="awaiting_day")
        )
        == "awaiting_day"
    )
    assert bd.landing_step(r(action="reply", flow_state=FlowState.MENU)) == "menu"
    assert (
        bd.landing_step(
            r(
                action="calendar_unavailable",
                flow_state=FlowState.SERVICE_CATALOG,
                flow_step="awaiting_day",
            )
        )
        == "human_handover"
    )
    assert (
        bd.landing_step(r(action="professional_config_incomplete", flow_state=FlowState.IDLE))
        == "config_incomplete"
    )


# --------------------------------------------------------------------------
# Router support the resolver relies on
# --------------------------------------------------------------------------


def test_booking_gate_scope_publishes_and_always_resets():
    gate = object()
    assert fr._ACTIVE_GATE.get() is None
    with pytest.raises(RuntimeError):
        with fr.booking_gate_scope(gate):
            assert fr._ACTIVE_GATE.get() is gate
            raise RuntimeError("boom")
    assert fr._ACTIVE_GATE.get() is None


async def test_the_insurance_answer_continues_to_the_chosen_doctors_services():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Retorno"])
    conv = _conv(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_INSURANCE,
        flow_selected_professional_id=ana.id,
    )
    res = await fr._handle_insurance(conv, _collecting(), "Unimed", professionals=[ana, beto])
    assert res.flow_step == fr.STEP_AWAITING_SERVICE
    assert res.flow_selected_professional_id == ana.id
    assert res.flow_selected_insurance == "Unimed"


async def test_the_insurance_answer_with_a_service_but_no_doctor_lists_the_doctors():
    ana, beto = _doctor("Dra. Ana", ["Consulta"]), _doctor("Dr. Beto", ["Consulta"])
    conv = _conv(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_INSURANCE,
        flow_selected_type="Consulta",
    )
    res = await fr._handle_insurance(conv, _collecting(), "Unimed", professionals=[ana, beto])
    assert res.flow_step == fr.STEP_AWAITING_PROFESSIONAL
    assert res.flow_selected_type == "Consulta"
    assert res.flow_selected_insurance == "Unimed"


# --------------------------------------------------------------------------
# Dia e horário (Task A5)
# --------------------------------------------------------------------------


async def test_a_valid_day_and_time_lands_on_that_days_slot_list(monkeypatch):
    seen: list = []

    async def _hook(**kwargs):
        seen.append(kwargs)
        return None

    monkeypatch.setattr(bd, "_express_confirmation", _hook)
    res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)))
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.result.flow_selected_day == "2026-10-08"
    assert res.result.flow_selected_type == "Consulta"
    assert res.accepted == ("service", "day", "time")
    # P3's hook was offered the RE-DERIVED slot: the agenda's own 10:00, aware.
    (call,) = seen
    assert call["slot_start"] == dt.datetime(2026, 10, 8, 10, 0, tzinfo=TZ)
    assert call["service"]["name"] == "Consulta"


async def test_the_express_hook_result_wins_and_keeps_every_answer(monkeypatch):
    async def _hook(**kwargs):
        return fr.FlowRouterResult(
            action="reply",
            flow_state=FlowState.SERVICE_CATALOG,
            flow_step=fr.STEP_AWAITING_CONFIRMATION,
            flow_selected_day="2026-10-08",
            flow_selected_slot="2026-10-08T10:00",
        )

    monkeypatch.setattr(bd, "_express_confirmation", _hook)
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)),
        conv=_conv(flow_attendee_name="Maria da Silva"),
    )
    assert res.landing_step == fr.STEP_AWAITING_CONFIRMATION
    assert res.result.flow_selected_type == "Consulta"
    assert res.result.flow_attendee_name == "Maria da Silva"


async def test_express_confirmation_needs_the_clinic_switch():
    """P3 filled the hook; without `initial_flows.ai_draft_v2` it still answers None."""
    assert (
        await bd._express_confirmation(
            state=None,
            tenant=None,
            professional=None,
            service={},
            slot_start=NOW,
            duration_minutes=30,
            calendar=None,
            professionals=[],
        )
        is None
    )


async def test_a_time_outside_business_hours_lands_on_that_days_slot_list():
    res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=dt.time(18, 0)))
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.result.flow_selected_day == "2026-10-08"
    assert res.dropped == {"time": "no_free_slot"}
    assert res.accepted == ("service", "day")


async def test_a_day_in_the_past_is_dropped():
    res = await _resolve(
        BookingDraft(service="Consulta", day=dt.date(2026, 10, 4), time=dt.time(10, 0))
    )
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.dropped == {"day": "out_of_window", "time": "missing_day"}
    assert res.result.bubbles[0].body.startswith(bd.DRAFT_DAY_OUT_OF_WINDOW_PREFIX)


async def test_a_day_beyond_the_window_is_dropped():
    beyond = NOW.date() + dt.timedelta(days=fr.DAY_PICKER_WINDOW_DAYS)
    res = await _resolve(BookingDraft(service="Consulta", day=beyond))
    assert res.dropped == {"day": "out_of_window"}


async def test_the_last_day_of_the_window_is_accepted():
    last = NOW.date() + dt.timedelta(days=fr.DAY_PICKER_WINDOW_DAYS - 1)
    res = await _resolve(BookingDraft(service="Consulta", day=last), cal=_Cal({last: ["09:00"]}))
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.result.flow_selected_day == last.isoformat()


async def test_a_day_without_free_time_is_dropped():
    # Another day IS free, so the picker lists it (an agenda with no free day at all is
    # the picker's own "não encontrei horários" + menu, `no_free_days`).
    cal = _Cal({DAY: [], dt.date(2026, 10, 9): ["09:00"]})
    res = await _resolve(BookingDraft(service="Consulta", day=DAY), cal=cal)
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.dropped == {"day": "day_unavailable"}
    assert res.result.bubbles[0].body.startswith(bd.DRAFT_DAY_UNAVAILABLE_PREFIX)


async def test_a_time_without_a_day_is_ignored():
    res = await _resolve(BookingDraft(service="Consulta", time=dt.time(10, 0)))
    assert res.landing_step == fr.STEP_AWAITING_DAY
    assert res.dropped == {"time": "missing_day"}


class _HoldGate:
    armed = False

    def __init__(self, windows):
        self._windows = windows
        self.asked: list = []

    async def busy_windows(self, professional_id):
        self.asked.append(professional_id)
        return list(self._windows.get(professional_id, []))


_HELD_1000 = (
    dt.datetime(2026, 10, 8, 10, 0, tzinfo=TZ),
    dt.datetime(2026, 10, 8, 10, 30, tzinfo=TZ),
)


async def test_a_held_slot_is_busy_inside_the_callers_gate_scope():
    with fr.booking_gate_scope(_HoldGate({None: [_HELD_1000]})):
        res = await _resolve(BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)))
    assert res.dropped == {"time": "no_free_slot"}


async def test_a_direct_call_without_a_gate_still_sees_holds(monkeypatch):
    from secretaria.services import booking_hold

    async def _held_windows(tenant_id, professional_id, *, exclude_conversation=None):
        return [_HELD_1000] if professional_id is None else []

    monkeypatch.setattr(booking_hold, "held_windows", _held_windows)
    assert fr._ACTIVE_GATE.get() is None
    res = await _resolve(
        BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)),
        conv=_conv(tenant_id=uuid4()),
    )
    assert res.dropped == {"time": "no_free_slot"}
    assert fr._ACTIVE_GATE.get() is None  # the default gate is taken down again


async def test_a_sole_professionals_holds_count():
    sole = _sole()
    gate = _HoldGate({sole.id: [_HELD_1000]})
    with fr.booking_gate_scope(gate):
        res = await _resolve(
            BookingDraft(service="Consulta", day=DAY, time=dt.time(10, 0)), pros=[sole]
        )
    assert res.dropped == {"time": "no_free_slot"}
    # Asked by the resolver AND by the slot list it lands on - always under the sole id.
    assert set(gate.asked) == {sole.id}


async def test_an_unreachable_agenda_hands_over():
    res = await _resolve(BookingDraft(service="Consulta", day=DAY), cal=_Cal(unavailable=True))
    assert res.result.action == "calendar_unavailable"
    assert (res.landing_step, res.fallback) == ("human_handover", "calendar_unavailable")
    assert res.result.flow_selected_type == "Consulta"


async def test_no_agenda_at_all_hands_over():
    async def _none(_professional):
        return None

    res = await resolve_booking_draft(
        BookingDraft(service="Consulta", day=DAY),
        conversation=_conv(),
        tenant=_tenant(),
        professional_rows=[],
        service_catalog=[],
        tenant_insurance=None,
        calendar=_none,
        now=NOW,
    )
    assert res.fallback == "calendar_unavailable"


async def test_a_doctor_without_hours_reaches_the_clinic_alert():
    ana = _doctor("Dra. Ana", ["Consulta"], hours={})
    beto = _doctor("Dr. Beto", ["Consulta"])
    res = await _resolve(
        BookingDraft(service="Consulta", professional_id=ana.id, day=DAY), pros=[ana, beto]
    )
    assert res.result.action == "professional_config_incomplete"
    assert res.result.professional_config_gap == "hours"


async def test_ask_day_without_a_prefix_is_unchanged():
    res = await fr._ask_day(_conv(), _tenant(), _Cal(), None, None)
    assert res.bubbles[0].body == fr.DAY_PICKER_BODY


@pytest.mark.parametrize("attendee", ["other", None])
async def test_a_captured_name_still_waiting_for_authorization_cannot_skip_it(attendee):
    asked = []
    res = await _resolve(
        BookingDraft(service="Consulta", attendee=attendee, day=DAY, time=dt.time(10, 0)),
        conv=_conv(
            flow_state=FlowState.LLM,
            flow_step=fr.STEP_AWAITING_ATTENDEE_AUTH,
            flow_attendee_name="Pessoa de teste",
        ),
        asked=asked,
    )
    assert res.landing_step == fr.STEP_AWAITING_ATTENDEE_AUTH
    assert res.result.flow_attendee_name == "Pessoa de teste"
    assert res.result.attendee_authorized is False
    assert draft_from_record(res.result.flow_draft, now=NOW).day == DAY
    assert asked == []


async def test_reserved_early_slots_do_not_hide_later_free_times():
    times = [
        "08:00",
        "08:30",
        "09:00",
        "09:30",
        "10:00",
        "10:30",
        "11:00",
        "11:30",
        "12:00",
        "12:30",
    ]
    early_holds = [
        (
            dt.datetime(2026, 10, 8, 8, 0, tzinfo=TZ),
            dt.datetime(2026, 10, 8, 12, 0, tzinfo=TZ),
        )
    ]
    with fr.booking_gate_scope(_HoldGate({None: early_holds})):
        res = await _resolve(
            BookingDraft(service="Consulta", day=DAY, time=dt.time(12, 0)),
            cal=_Cal({DAY: times}),
        )
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.result.flow_selected_day == "2026-10-08"
    assert res.dropped == {}
    assert [row[0] for row in res.result.bubbles[0].rows if row[0].startswith("slot|")] == [
        "slot|2026-10-08T12:00",
        "slot|2026-10-08T12:30",
    ]


async def test_the_picker_limits_free_slots_after_subtracting_holds():
    times = [
        "08:00",
        "08:30",
        "09:00",
        "09:30",
        "10:00",
        "10:30",
        "11:00",
        "11:30",
        "12:00",
        "12:30",
    ]
    early_hold = (
        dt.datetime(2026, 10, 8, 8, 0, tzinfo=TZ),
        dt.datetime(2026, 10, 8, 8, 30, tzinfo=TZ),
    )
    with fr.booking_gate_scope(_HoldGate({None: [early_hold]})):
        res = await _resolve(BookingDraft(service="Consulta", day=DAY), cal=_Cal({DAY: times}))
    rows = res.result.bubbles[0].rows
    assert [row[0] for row in rows if row[0].startswith("slot|")] == [
        "slot|2026-10-08T08:30",
        "slot|2026-10-08T09:00",
        "slot|2026-10-08T09:30",
        "slot|2026-10-08T10:00",
        "slot|2026-10-08T10:30",
        "slot|2026-10-08T11:00",
        "slot|2026-10-08T11:30",
        "slot|2026-10-08T12:00",
    ]
    assert rows[-2][0] == "dayagain|0"
    assert rows[-1][0] == "dayback|service"


def _five_minute_agenda():
    starts = [dt.datetime(2026, 10, 8, 8, 0) + dt.timedelta(minutes=5 * i) for i in range(120)]
    return _Cal({DAY: [start.strftime("%H:%M") for start in starts]})


async def test_a_short_service_can_accept_a_free_time_beyond_the_first_96_slots():
    res = await _resolve(
        BookingDraft(service="Consulta curta", day=DAY, time=dt.time(17, 0)),
        tenant=_tenant(appointment_types=[{"name": "Consulta curta", "duration_min": 5}]),
        cal=_five_minute_agenda(),
    )
    assert res.dropped == {}
    assert res.accepted == ("service", "day", "time")
    assert res.landing_step == fr.STEP_AWAITING_SLOT


async def test_a_short_service_keeps_a_day_with_only_late_free_slots_after_holds():
    hold = (
        dt.datetime(2026, 10, 8, 8, 0, tzinfo=TZ),
        dt.datetime(2026, 10, 8, 16, 0, tzinfo=TZ),
    )
    with fr.booking_gate_scope(_HoldGate({None: [hold]})):
        res = await _resolve(
            BookingDraft(service="Consulta curta", day=DAY, time=dt.time(17, 0)),
            tenant=_tenant(appointment_types=[{"name": "Consulta curta", "duration_min": 5}]),
            cal=_five_minute_agenda(),
        )
    assert res.dropped == {}
    assert res.landing_step == fr.STEP_AWAITING_SLOT
    assert res.result.bubbles[0].rows[0][0] == "slot|2026-10-08T16:00"
