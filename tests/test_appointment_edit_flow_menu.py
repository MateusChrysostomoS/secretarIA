"""The edit flow: menu, date, time and the complete confirmation (TASK-032 R6, spec §5.3-5.5)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime  # noqa: E402
from uuid import UUID  # noqa: E402

from secretaria.ai.formatter import SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import (
    appointment_edit as ae,  # noqa: E402
    flow_router as fr,  # noqa: E402
)
from secretaria.services.appointment_edit_flow import edit_step, enter_edit_menu  # noqa: E402
from tests._edit_flow_support import (  # noqa: E402
    EditCalendar,
    appointment,
    context,
    conversation,
    draft_for,
    original_appointment,
    professionals,
    slots,
    tenant,
)


def _labels(result) -> list[str]:
    return result.bubbles[0].labels


async def _step(step, body, *, draft=None, calendar=None, ctx=None, appt=None, **kw):
    appt = appt or (original_appointment(draft) if draft is not None else appointment())
    draft = draft or draft_for(appt)
    conv = conversation(draft, step, **kw)
    return await edit_step(
        conv,
        tenant(),
        body,
        [appt],
        professionals(),
        ctx or context(calendar),
    )


def test_the_menu_has_five_buttons_and_the_alterar_dados_description():
    appt = appointment()
    result = enter_edit_menu(tenant(), appt, ae.EditContext())

    assert result.flow_state == FlowState.EDIT_BOOKING
    assert result.flow_step == fr.STEP_EDIT_MENU
    assert result.flow_managing_appointment_id == UUID(appt["id"])
    assert result.flow_edit_draft["appointment_id"] == appt["id"]
    assert _labels(result) == [
        ae.LABEL_EDIT_DATE,
        ae.LABEL_EDIT_TIME,
        ae.LABEL_EDIT_SERVICE,
        ae.LABEL_EDIT_DOCTOR,
        ae.LABEL_EDIT_MORE,
    ]
    assert result.bubbles[0].body.startswith("*Alterar Dados*")


def test_a_paid_deposit_hides_service_doctor_and_insurance():
    result = enter_edit_menu(tenant(), appointment(), ae.EditContext(paid_deposit=True))
    assert _labels(result) == [ae.LABEL_EDIT_DATE, ae.LABEL_EDIT_TIME, ae.LABEL_EDIT_MORE]
    assert ae.EDIT_PIX_NOTICE in result.bubbles[0].body


async def test_a_paid_deposit_also_hides_the_insurance_in_the_second_menu():
    ctx = context(paid_deposit=True)
    more = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_MORE, ctx=ctx)
    assert _labels(more) == [ae.LABEL_EDIT_PATIENT, ae.LABEL_EDIT_BACK]


def test_a_reached_reschedule_limit_hides_date_and_time():
    result = enter_edit_menu(tenant(), appointment(), ae.EditContext(reschedule_blocked=True))
    assert _labels(result) == [ae.LABEL_EDIT_SERVICE, ae.LABEL_EDIT_DOCTOR, ae.LABEL_EDIT_MORE]
    assert ae.EDIT_LIMIT_NOTICE in result.bubbles[0].body


async def test_a_hidden_option_typed_by_hand_is_not_honoured():
    ctx = context(reschedule_blocked=True)
    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DATE, ctx=ctx)
    assert result.action == "delegate_llm"


async def test_outro_opens_the_second_menu_and_voltar_comes_back():
    more = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_MORE)
    assert more.flow_step == fr.STEP_EDIT_MORE
    assert _labels(more) == [ae.LABEL_EDIT_INSURANCE, ae.LABEL_EDIT_PATIENT, ae.LABEL_EDIT_BACK]

    back = await _step(
        fr.STEP_EDIT_MORE, ae.LABEL_EDIT_BACK, draft=ae.EditDraft.from_json(more.flow_edit_draft)
    )
    assert back.flow_step == fr.STEP_EDIT_MENU


async def test_mudar_horario_lists_the_free_times_of_the_current_day():
    calendar = EditCalendar(slots=slots("14:00", "16:00"))

    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_TIME, calendar=calendar)

    assert result.flow_step == fr.STEP_EDIT_SLOT
    assert result.flow_selected_day == "2030-10-15"
    bubble = result.bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    assert [row[0] for row in bubble.rows if row[0].startswith("slot|")] == [
        "slot|2030-10-15T14:00",
        "slot|2030-10-15T16:00",
    ]
    assert result.flow_edit_draft["appointment_id"]


async def test_a_slot_tap_shows_the_complete_confirmation_and_changes_nothing():
    calendar = EditCalendar()
    appt = appointment()

    result = await _step(
        fr.STEP_EDIT_SLOT,
        "16:00 (2030-10-15T16:00)",
        draft=draft_for(appt).with_stage(mode="time"),
        calendar=calendar,
        appt=appt,
    )

    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = ae.EditDraft.from_json(result.flow_edit_draft)
    assert draft.current["start_at"] == "2030-10-15T16:00"
    assert draft.current["end_at"] == "2030-10-15T16:40"
    bubble = result.bubbles[0]
    assert bubble.labels == [fr.LABEL_CONFIRM, fr.LABEL_CANCEL, ae.LABEL_EDIT_MORE_DATA]
    assert "Horário: 16:00" in bubble.body
    assert "Médico: Dr. Diogo Raposo" in bubble.body
    assert "• Trazer exames anteriores" in bubble.body
    assert "Endereço: Rua das Flores, 10" in bubble.body
    assert bubble.body.rstrip().endswith("O que mudou: horário.")
    # Review Focus 1: a draft is only a draft.
    assert result.appointment_edit is None
    assert calendar.detail_updates == [] and calendar.created == [] and calendar.cancelled == []


async def test_mudar_data_lists_the_nearest_days_then_asks_about_the_time():
    days = [datetime(2030, 10, d) for d in (10, 12, 14, 15, 16, 17, 18, 21)]
    calendar = EditCalendar(days=days)

    picker = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DATE, calendar=calendar)

    assert picker.flow_step == fr.STEP_EDIT_DAY
    assert ae.EditDraft.from_json(picker.flow_edit_draft).stage == {"mode": "date"}
    assert any(row[0].startswith("day|2030-10-16") for row in picker.bubbles[0].rows)

    asked = await _step(
        fr.STEP_EDIT_DAY,
        "Qua, 16/10 (2030-10-16|0)",
        draft=ae.EditDraft.from_json(picker.flow_edit_draft),
        calendar=calendar,
    )
    assert asked.flow_step == fr.STEP_EDIT_TIME_TOO
    assert asked.bubbles[0].labels == [ae.LABEL_TIME_TOO_YES, ae.LABEL_TIME_TOO_NO]
    assert ae.EditDraft.from_json(asked.flow_edit_draft).stage["target_day"] == "2030-10-16"


def _asked_draft():
    return draft_for().with_stage(mode="date", target_day="2030-10-16")


async def test_keeping_the_time_goes_straight_to_the_confirmation_when_it_is_free():
    result = await _step(
        fr.STEP_EDIT_TIME_TOO,
        ae.LABEL_TIME_TOO_NO,
        draft=_asked_draft(),
        calendar=EditCalendar(free=True),
    )
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = ae.EditDraft.from_json(result.flow_edit_draft)
    assert draft.current["start_at"] == "2030-10-16T15:20"
    assert draft.changed() == ["data"]


async def test_keeping_a_busy_time_lists_that_days_slots_instead():
    calendar = EditCalendar(free=False, slots=slots("09:00", day="2030-10-16"))
    result = await _step(
        fr.STEP_EDIT_TIME_TOO, ae.LABEL_TIME_TOO_NO, draft=_asked_draft(), calendar=calendar
    )
    assert result.flow_step == fr.STEP_EDIT_SLOT
    assert isinstance(result.bubbles[0], TextBubble)
    assert result.bubbles[0].body == ae.EDIT_TIME_BUSY
    assert isinstance(result.bubbles[1], SlotsBubble)


async def test_changing_the_time_too_lists_the_slots_of_the_chosen_day():
    calendar = EditCalendar(slots=slots("09:00", "10:00", day="2030-10-16"))
    result = await _step(
        fr.STEP_EDIT_TIME_TOO, ae.LABEL_TIME_TOO_YES, draft=_asked_draft(), calendar=calendar
    )
    assert result.flow_step == fr.STEP_EDIT_SLOT
    assert result.flow_selected_day == "2030-10-16"


async def test_cancel_on_the_final_card_keeps_the_appointment_and_drops_the_draft():
    draft = draft_for().with_changes(start_at="2030-10-15T16:00", end_at="2030-10-15T16:40")
    result = await _step(fr.STEP_EDIT_CONFIRM, fr.LABEL_CANCEL, draft=draft)

    assert result.flow_state == FlowState.MENU
    assert result.flow_edit_draft is None
    assert result.appointment_edit is None
    assert result.bubbles[0].body == ae.EDIT_KEPT
    assert isinstance(result.bubbles[1], fr.MenuBubble)  # the returning-patient menu follows


async def test_alterar_mais_dados_reopens_the_menu_with_the_draft_intact():
    draft = draft_for().with_changes(start_at="2030-10-15T16:00", end_at="2030-10-15T16:40")
    result = await _step(fr.STEP_EDIT_CONFIRM, ae.LABEL_EDIT_MORE_DATA, draft=draft)

    assert result.flow_step == fr.STEP_EDIT_MENU
    assert ae.EditDraft.from_json(result.flow_edit_draft).current["start_at"] == "2030-10-15T16:00"


async def test_a_missing_appointment_is_reported_and_the_edit_closes():
    appt = appointment()
    conv = conversation(draft_for(appt), fr.STEP_EDIT_MENU)
    result = await edit_step(conv, tenant(), ae.LABEL_EDIT_DATE, [], professionals(), context())
    assert result.flow_state == FlowState.MENU
    assert result.bubbles[0].body == ae.EDIT_STALE
    assert result.flow_edit_draft is None


async def test_free_text_hands_the_turn_to_the_ai_and_keeps_the_draft():
    result = await _step(fr.STEP_EDIT_MENU, "vocês têm estacionamento?")
    assert result.action == "delegate_llm"
    assert result.flow_state == FlowState.EDIT_BOOKING
    assert result.flow_edit_draft is not None


async def test_the_router_dispatches_the_edit_state_to_the_flow():
    appt = appointment()
    draft = draft_for(appt)
    result = await fr.route(
        conversation(draft, fr.STEP_EDIT_MENU),
        tenant(),
        None,
        ae.LABEL_EDIT_MORE,
        upcoming_appointments=[appt],
        professionals=professionals(),
        edit_context=context(),
    )
    assert result.flow_step == fr.STEP_EDIT_MORE
    assert result.flow_edit_draft is not None
