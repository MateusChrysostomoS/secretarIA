"""The edit flow: service, doctor, convênio and patient (TASK-032 R6, spec §5.4)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import timedelta  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble  # noqa: E402
from secretaria.services import (
    appointment_edit as ae,  # noqa: E402
    flow_router as fr,  # noqa: E402
)
from secretaria.services.appointment_edit_flow import edit_step  # noqa: E402
from secretaria.services.attendee import (  # noqa: E402
    ATTENDEE_NAME_INVALID,
    ATTENDEE_NAME_REQUEST,
    ATTENDEE_QUESTION_BODY,
    LABEL_ATTENDEE_AUTH_BACK,
    LABEL_ATTENDEE_AUTH_CONFIRM,
    LABEL_ATTENDEE_OTHER,
    LABEL_ATTENDEE_SELF,
)
from tests._edit_flow_support import (  # noqa: E402
    DOCTOR_A,
    DOCTOR_B,
    START_UTC,
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


async def _step(step, body, *, draft=None, ctx=None, appt=None, **kw):
    appt = appt or (original_appointment(draft) if draft is not None else appointment())
    draft = draft or draft_for(appt)
    conv = conversation(draft, step, **kw)
    return await edit_step(conv, tenant(), body, [appt], professionals(), ctx or context())


def _draft_of(result) -> ae.EditDraft:
    return ae.EditDraft.from_json(result.flow_edit_draft)


def _rows(result):
    return result.bubbles[-1].rows


# --- Service -------------------------------------------------------------------------


async def test_mudar_servico_lists_only_the_current_doctors_services():
    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_SERVICE)

    assert result.flow_step == fr.STEP_EDIT_SERVICE
    assert [row[0] for row in _rows(result)] == ["svc|Consulta", "svc|Retorno"]


async def test_a_shorter_service_keeps_the_time_and_goes_to_the_confirmation():
    result = await _step(fr.STEP_EDIT_SERVICE, fr._service_row_title("Retorno"))

    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft_of(result)
    assert draft.current["service"] == "Retorno"
    assert draft.current["end_at"] == "2030-10-15T15:40"  # 20 minutes from 15:20
    assert draft.changed() == ["serviço"]


async def test_a_longer_service_that_does_not_fit_asks_for_a_new_time():
    appt = appointment(appointment_type="Retorno", end_at=START_UTC + timedelta(minutes=20))
    calendar = EditCalendar(free=False)

    result = await _step(
        fr.STEP_EDIT_SERVICE,
        fr._service_row_title("Consulta"),
        appt=appt,
        ctx=context(calendar),
    )

    assert result.flow_step == fr.STEP_EDIT_DAY
    assert result.bubbles[0].body == ae.EDIT_RESLOT_NOTICE
    assert _draft_of(result).stage["mode"] == "reslot"
    # Never a confirmation for a window that is not free.
    assert not any(isinstance(b, fr.MenuBubble) for b in result.bubbles)


# --- Doctor --------------------------------------------------------------------------


async def test_the_doctor_list_marks_free_and_busy():
    busy_b = EditCalendar(free=False)
    ctx = context(calendars={str(DOCTOR_A): EditCalendar(free=True), str(DOCTOR_B): busy_b})

    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DOCTOR, ctx=ctx)

    assert result.flow_step == fr.STEP_EDIT_DOCTOR
    assert isinstance(result.bubbles[0], SlotsBubble)
    rows = _rows(result)
    assert [row[0] for row in rows] == [f"prof|{DOCTOR_A}", f"prof|{DOCTOR_B}"]
    assert rows[0][2].startswith(ae.MARK_FREE)
    assert rows[1][2].startswith(ae.MARK_BUSY)
    assert all(len(row[2]) <= 72 for row in rows)


async def test_the_current_doctors_own_event_is_not_a_conflict():
    calendar = EditCalendar(free=True)
    ctx = context(calendars={str(DOCTOR_A): calendar, str(DOCTOR_B): EditCalendar()})

    await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DOCTOR, ctx=ctx)

    assert calendar.checked and calendar.checked[0][2] == "evt-old"


def _doctor_tap(name, doctor_id):
    return f"{fr._professional_row_title(name)} ({doctor_id})"


async def test_a_free_doctor_goes_to_the_confirmation():
    ctx = context(calendars={str(DOCTOR_A): EditCalendar(), str(DOCTOR_B): EditCalendar(free=True)})

    result = await _step(fr.STEP_EDIT_DOCTOR, _doctor_tap("Dra. Ana Lima", DOCTOR_B), ctx=ctx)

    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft_of(result)
    assert draft.current["professional_id"] == str(DOCTOR_B)
    assert draft.current["end_at"] == "2030-10-15T15:50"  # her Consulta is 30 minutes
    assert draft.changed() == ["médico"]
    assert "Médico: Dra. Ana Lima" in result.bubbles[0].body


async def test_a_busy_doctor_continues_to_day_and_time():
    ctx = context(
        calendars={str(DOCTOR_A): EditCalendar(), str(DOCTOR_B): EditCalendar(free=False)}
    )

    result = await _step(fr.STEP_EDIT_DOCTOR, _doctor_tap("Dra. Ana Lima", DOCTOR_B), ctx=ctx)

    assert result.flow_step == fr.STEP_EDIT_DAY
    assert result.bubbles[0].body == ae.EDIT_RESLOT_NOTICE
    assert _draft_of(result).current["professional_id"] == str(DOCTOR_B)


async def test_a_doctor_who_does_not_offer_the_service_lists_their_services():
    appt = appointment(appointment_type="Retorno", end_at=START_UTC + timedelta(minutes=20))
    ctx = context(calendars={str(DOCTOR_A): EditCalendar(), str(DOCTOR_B): EditCalendar()})

    result = await _step(
        fr.STEP_EDIT_DOCTOR, _doctor_tap("Dra. Ana Lima", DOCTOR_B), ctx=ctx, appt=appt
    )

    assert result.flow_step == fr.STEP_EDIT_SERVICE
    assert [row[0] for row in _rows(result)] == ["svc|Consulta"]
    assert _draft_of(result).stage == {"after_doctor": "1"}

    picked = await edit_step(
        conversation(_draft_of(result), fr.STEP_EDIT_SERVICE),
        tenant(),
        fr._service_row_title("Consulta"),
        [appt],
        professionals(),
        ctx,
    )
    assert picked.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft_of(picked).changed() == ["serviço", "médico"]


# --- Convênio ------------------------------------------------------------------------


async def test_mudar_convenio_marks_what_the_current_doctor_accepts():
    result = await _step(fr.STEP_EDIT_MORE, ae.LABEL_EDIT_INSURANCE)

    assert result.flow_step == fr.STEP_EDIT_INSURANCE
    rows = {row[0]: row for row in _rows(result)}
    assert rows["ins|Unimed"][2].startswith(ae.MARK_FREE)
    assert rows["ins|Amil"][2].startswith(ae.MARK_BUSY)  # Dr. Diogo only takes Unimed
    assert {"ins|particular", "ins|outro"} <= set(rows)


async def test_choosing_a_plan_goes_to_the_confirmation():
    result = await _step(fr.STEP_EDIT_INSURANCE, "Amil")
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft_of(result).changed() == ["convênio"]
    assert "Convênio: Amil" in result.bubbles[0].body


async def test_outro_convenio_asks_for_the_name_then_stores_it():
    asked = await _step(fr.STEP_EDIT_INSURANCE, fr.LABEL_INSURANCE_OTHER)
    assert asked.flow_step == fr.STEP_EDIT_INSURANCE_OTHER
    assert asked.bubbles[0].body == fr.INSURANCE_PROMPT_OTHER

    stored = await _step(fr.STEP_EDIT_INSURANCE_OTHER, "Bradesco Saúde", draft=_draft_of(asked))
    assert stored.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft_of(stored).current["insurance"] == "Bradesco Saúde"


async def test_a_question_in_the_convenio_step_goes_to_the_ai_with_the_draft_kept():
    result = await _step(fr.STEP_EDIT_INSURANCE, "qual o endereço de vocês?")
    assert result.action == "delegate_llm"
    assert result.flow_edit_draft is not None


# --- Patient -------------------------------------------------------------------------


async def test_mudar_paciente_asks_who_it_is_for():
    result = await _step(fr.STEP_EDIT_MORE, ae.LABEL_EDIT_PATIENT)
    assert result.flow_step == fr.STEP_EDIT_ATT_CHOICE
    assert result.bubbles[0].body == ATTENDEE_QUESTION_BODY
    assert result.bubbles[0].labels == [LABEL_ATTENDEE_SELF, LABEL_ATTENDEE_OTHER]


async def test_for_myself_clears_a_third_party_and_confirms():
    draft = draft_for(appointment(attendee_name="Ana Souza"))
    result = await _step(fr.STEP_EDIT_ATT_CHOICE, LABEL_ATTENDEE_SELF, draft=draft)
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft_of(result).current["attendee_name"] is None
    assert _draft_of(result).changed() == ["paciente"]


async def test_for_someone_else_asks_the_name_then_the_authorization():
    asked = await _step(fr.STEP_EDIT_ATT_CHOICE, LABEL_ATTENDEE_OTHER)
    assert asked.flow_step == fr.STEP_EDIT_ATT_NAME
    assert asked.bubbles[0].body == ATTENDEE_NAME_REQUEST

    invalid = await _step(fr.STEP_EDIT_ATT_NAME, "???", draft=_draft_of(asked))
    assert invalid.bubbles[0].body == ATTENDEE_NAME_INVALID

    authorization = await _step(fr.STEP_EDIT_ATT_NAME, "Ana Souza", draft=_draft_of(asked))
    assert authorization.flow_step == fr.STEP_EDIT_ATT_AUTH
    assert isinstance(authorization.bubbles[0], ButtonBubble)
    assert authorization.flow_attendee_name == "Ana Souza"
    # Still a proposal: the draft's patient is unchanged until the sentence is confirmed.
    assert _draft_of(authorization).current["attendee_name"] is None

    confirmed = await _step(
        fr.STEP_EDIT_ATT_AUTH, LABEL_ATTENDEE_AUTH_CONFIRM, draft=_draft_of(authorization)
    )
    assert confirmed.flow_step == fr.STEP_EDIT_CONFIRM
    assert confirmed.attendee_authorized is True
    assert _draft_of(confirmed).current["attendee_name"] == "Ana Souza"
    assert "Paciente: Ana Souza" in confirmed.bubbles[0].body


async def test_declining_the_authorization_goes_back_to_the_question():
    asked = await _step(fr.STEP_EDIT_ATT_CHOICE, LABEL_ATTENDEE_OTHER)
    authorization = await _step(fr.STEP_EDIT_ATT_NAME, "Ana Souza", draft=_draft_of(asked))

    back = await _step(
        fr.STEP_EDIT_ATT_AUTH, LABEL_ATTENDEE_AUTH_BACK, draft=_draft_of(authorization)
    )

    assert back.flow_step == fr.STEP_EDIT_ATT_CHOICE
    assert _draft_of(back).current["attendee_name"] is None
    assert back.attendee_authorized is False


async def test_a_doctor_without_services_says_so_and_shows_the_menu():
    pros = professionals()
    pros[0].appointment_types = []
    appt = appointment()
    conv = conversation(draft_for(appt), fr.STEP_EDIT_MENU)
    result = await edit_step(
        conv, tenant(appointment_types=[]), ae.LABEL_EDIT_SERVICE, [appt], pros, context()
    )
    assert isinstance(result.bubbles[0], TextBubble)
    assert result.bubbles[0].body == ae.EDIT_NO_SERVICES
    assert result.flow_step == fr.STEP_EDIT_MENU


async def test_slots_helper_is_importable_for_the_next_task():
    assert slots("10:00")[0]["label"] == "10:00"
