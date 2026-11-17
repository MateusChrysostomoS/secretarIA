"""TASK-085 / R / S3a - the patient's "Alterar dados" router with a sinal (D-R1, D-R3, §3.8).

Router level (pure, no database). With a PAID sinal:
- D-R3: the doctor and the convênio are offered like any other field (menu, second menu, typed
  labels, the AI); the notice says the sinal may change and the difference is settled;
- D-R1/D-R3: Confirmar hands EVERY change of a money field (service, unit, doctor, convênio) to
  the worker without touching the agenda (`calendar_deferred`), after checking the new window;
  a change of only date/time/patient is today's edit (the router writes the agenda);
- contrato §3.8: a SUSPENDED consultation (waiting for the Pix of the difference) cannot be
  opened or edited - one message (`SUSPENDED_VISIT_TEXT`), nothing changes.
A clinic without a paid sinal sees exactly today's flow (characterization).
"""

import os

os.environ.setdefault("APP_ENV", "test")

from datetime import timedelta  # noqa: E402
from uuid import UUID  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai.formatter import TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import appointment_edit as ae, flow_router as fr  # noqa: E402
from secretaria.services.appointment_edit_flow import (  # noqa: E402
    apply_ai_edit,
    edit_step,
    enter_edit_menu,
    redisplay_edit,
)
from secretaria.services.payments.adjustment_suspension import (  # noqa: E402
    SUSPENDED_VISIT_TEXT,
)
from tests._edit_flow_support import (  # noqa: E402
    DOCTOR_A,
    DOCTOR_B,
    START_UTC,
    TZ,
    EditCalendar,
    appointment,
    context,
    conversation,
    draft_for,
    original_appointment,
    professionals,
    tenant,
)
from tests.test_appointment_edit_units import (  # noqa: E402
    CENTRO,
    NORTE,
    SUL,
    _Agenda,
    _appt,
    _doctor,
    _types,
)


def _labels(result) -> list[str]:
    return result.bubbles[0].labels


def _draft(result) -> ae.EditDraft:
    return ae.EditDraft.from_json(result.flow_edit_draft)


async def _step(step, body, *, draft=None, ctx=None, appt=None):
    appt = appt or (original_appointment(draft) if draft is not None else appointment())
    draft = draft or draft_for(appt)
    return await edit_step(
        conversation(draft, step), tenant(), body, [appt], professionals(), ctx or context()
    )


async def _confirm(draft, *, ctx, appt=None):
    appt = appt or original_appointment(draft)
    return await edit_step(
        conversation(draft, fr.STEP_EDIT_CONFIRM),
        tenant(),
        fr.LABEL_CONFIRM,
        [appt],
        professionals(),
        ctx,
        "Maria",
    )


def _two_agendas(*, free_b: bool = True):
    old, new = EditCalendar(), EditCalendar(free=free_b)
    return old, new, {str(DOCTOR_A): old, str(DOCTOR_B): new}


# --- D-R3: the menus offer the doctor and the convênio with a paid sinal -----------------


def test_the_new_notice_names_the_four_money_fields_and_the_difference():
    assert ae.EDIT_PIX_NOTICE == (
        "Como essa consulta tem sinal pago, trocar serviço, unidade, médico ou convênio pode "
        "mudar o sinal: acertamos a diferença."
    )


def test_a_paid_sinal_offers_every_field_and_adds_the_notice():
    result = enter_edit_menu(tenant(), appointment(), ae.EditContext(paid_deposit=True))
    assert _labels(result) == [
        ae.LABEL_EDIT_DATE,
        ae.LABEL_EDIT_TIME,
        ae.LABEL_EDIT_SERVICE,
        ae.LABEL_EDIT_DOCTOR,
        ae.LABEL_EDIT_MORE,
    ]
    body = result.bubbles[0].body
    assert body == f"{ae.EDIT_MENU_BODY}\n\n{ae.EDIT_PIX_NOTICE}"
    assert "fale com a clínica" not in body


def test_without_a_paid_sinal_the_menu_is_todays():
    """Characterization: a clinic without a sinal (or an unpaid one) sees no notice at all."""
    result = enter_edit_menu(tenant(), appointment(), ae.EditContext())
    assert result.bubbles[0].body == ae.EDIT_MENU_BODY
    assert ae.EDIT_PIX_NOTICE not in result.bubbles[0].body


def test_the_reschedule_limit_still_hides_only_date_and_time():
    ctx = ae.EditContext(paid_deposit=True, reschedule_blocked=True)
    result = enter_edit_menu(tenant(), appointment(), ctx)
    assert _labels(result) == [ae.LABEL_EDIT_SERVICE, ae.LABEL_EDIT_DOCTOR, ae.LABEL_EDIT_MORE]
    body = result.bubbles[0].body
    assert ae.EDIT_PIX_NOTICE in body and ae.EDIT_LIMIT_NOTICE in body
    assert "• *Mudar médico*" in body


async def test_the_second_menu_keeps_the_convenio_with_a_paid_sinal():
    more = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_MORE, ctx=context(paid_deposit=True))
    assert _labels(more) == [ae.LABEL_EDIT_INSURANCE, ae.LABEL_EDIT_PATIENT, ae.LABEL_EDIT_BACK]
    assert more.bubbles[0].body == ae.EDIT_MORE_BODY


async def test_mudar_medico_opens_the_doctor_list_with_a_paid_sinal():
    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DOCTOR, ctx=context(paid_deposit=True))
    assert result.action == "reply"
    assert result.flow_step == fr.STEP_EDIT_DOCTOR


async def test_mudar_convenio_opens_the_insurance_list_with_a_paid_sinal():
    result = await _step(fr.STEP_EDIT_MORE, ae.LABEL_EDIT_INSURANCE, ctx=context(paid_deposit=True))
    assert result.flow_step == fr.STEP_EDIT_INSURANCE


async def test_the_doctor_entry_works_inside_another_picker_with_a_paid_sinal():
    result = await _step(fr.STEP_EDIT_SERVICE, ae.LABEL_EDIT_DOCTOR, ctx=context(paid_deposit=True))
    assert result.flow_step == fr.STEP_EDIT_DOCTOR


async def test_a_paid_sinal_picks_a_convenio_and_reaches_the_card():
    result = await _step(fr.STEP_EDIT_INSURANCE, "Amil", ctx=context(paid_deposit=True))
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(result).changed() == ["convênio"]


async def test_a_drafted_doctor_survives_a_sinal_paid_mid_edit():
    """Before D-R3 a sinal paid mid-edit threw the doctor/convênio back; now they stay."""
    draft = draft_for().with_changes(professional_id=str(DOCTOR_B), insurance="Amil")
    result = await _step(
        fr.STEP_EDIT_CONFIRM, ae.LABEL_EDIT_MORE_DATA, draft=draft, ctx=context(paid_deposit=True)
    )
    assert result.flow_step == fr.STEP_EDIT_MENU
    kept = _draft(result)
    assert kept.current["professional_id"] == str(DOCTOR_B)
    assert kept.current["insurance"] == "Amil"


async def test_the_reschedule_limit_still_throws_a_drafted_time_back():
    draft = draft_for().with_changes(
        professional_id=str(DOCTOR_B), start_at="2030-10-16T09:00", end_at="2030-10-16T09:30"
    )
    result = await _step(
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        draft=draft,
        ctx=context(paid_deposit=True, reschedule_blocked=True),
    )
    assert result.flow_step == fr.STEP_EDIT_MENU and result.appointment_edit is None
    kept = _draft(result)
    assert kept.current["start_at"] == draft.original["start_at"]
    assert kept.current["professional_id"] == str(DOCTOR_B)  # only the time goes back


# --- D-R1/D-R3: Confirmar defers every money change to the worker ------------------------


async def test_a_paid_doctor_change_is_checked_on_the_new_agenda_and_deferred():
    old, new, cals = _two_agendas()
    draft = draft_for().with_changes(professional_id=str(DOCTOR_B), end_at="2030-10-15T15:50")
    result = await _confirm(draft, ctx=context(calendars=cals, paid_deposit=True))

    edit = result.appointment_edit
    assert edit["calendar_deferred"] is True
    assert edit["professional_id"] == DOCTOR_B
    assert edit["old_professional_id"] == DOCTOR_A
    assert edit["doctor_changed"] is True and edit["calendar_changed"] is True
    assert edit["google_event_id"] == "evt-old" and edit["google_event_link"] is None
    assert edit["calendar"] is new
    assert new.checked and not old.checked  # the window is checked on the NEW agenda
    assert new.created == [] and new.detail_updates == []
    assert old.created == [] and old.detail_updates == []


async def test_a_paid_doctor_change_into_a_busy_agenda_goes_back_to_day_and_time():
    _old, new, cals = _two_agendas(free_b=False)
    draft = draft_for().with_changes(professional_id=str(DOCTOR_B), end_at="2030-10-15T15:50")
    result = await _confirm(draft, ctx=context(calendars=cals, paid_deposit=True))
    assert result.appointment_edit is None
    assert result.flow_step == fr.STEP_EDIT_DAY
    assert new.created == []


async def test_a_paid_convenio_change_is_deferred_without_a_window_check():
    calendar = EditCalendar(free=False)  # a window check would fail; none is needed
    draft = draft_for().with_changes(insurance="Amil")
    result = await _confirm(draft, ctx=context(calendar, paid_deposit=True))

    edit = result.appointment_edit
    assert edit["calendar_deferred"] is True
    assert edit["insurance"] == "Amil" and edit["calendar"] is calendar
    assert calendar.checked == [] and calendar.detail_updates == [] and calendar.created == []


async def test_to_particular_is_a_money_change_too():
    calendar = EditCalendar()
    draft = draft_for().with_changes(insurance=fr.LABEL_INSURANCE_PARTICULAR)
    result = await _confirm(draft, ctx=context(calendar, paid_deposit=True))
    assert result.appointment_edit["calendar_deferred"] is True
    assert calendar.detail_updates == []


@pytest.mark.parametrize(
    "changes",
    [
        {"start_at": "2030-10-16T09:00", "end_at": "2030-10-16T09:40"},
        {"attendee_name": "Joana Souza"},
    ],
)
async def test_a_paid_change_without_a_money_field_is_todays_edit(changes):
    """Characterization: date/time or who it is for never move the sinal - the router writes
    the agenda at Confirmar exactly as before."""
    calendar = EditCalendar()
    draft = draft_for().with_changes(**changes)
    result = await _confirm(draft, ctx=context(calendar, paid_deposit=True))
    edit = result.appointment_edit
    assert edit["calendar_deferred"] is False
    assert len(calendar.detail_updates) == 1


async def test_without_a_paid_sinal_a_doctor_change_still_creates_the_event_here():
    """Characterization: an unpaid (or no) sinal keeps today's write at Confirmar; the worker
    re-quotes an unpaid sinal (D-R6, S3b) - the router does not change for it."""
    old, new, cals = _two_agendas()
    draft = draft_for().with_changes(professional_id=str(DOCTOR_B), end_at="2030-10-15T15:50")
    result = await _confirm(draft, ctx=context(calendars=cals))
    edit = result.appointment_edit
    assert edit["calendar_deferred"] is False
    assert len(new.created) == 1 and edit["google_event_id"] != "evt-old"


async def test_a_paid_unit_change_is_deferred_even_at_the_same_doctor():
    """D-R1: unit = service. The router never decides whether the price moved - it hands every
    unit change on a paid sinal to the worker and the engine decides."""
    ana = _doctor("Dra. Ana", [CENTRO, SUL], types=_types("Consulta"))
    agenda = _Agenda()
    appt = _appt(ana)
    draft = ae.EditDraft.from_appointment(appt, TZ).with_changes(unit_id=str(SUL.id))
    # Thursday at Zona Sul (its own hours), so the new window is free there.
    thursday = draft.with_changes(start_at="2030-10-17T15:20", end_at="2030-10-17T16:00")
    ctx = ae.EditContext(calendars={str(ana.id): agenda}, paid_deposit=True)
    result = await edit_step(
        conversation(thursday, fr.STEP_EDIT_CONFIRM),
        tenant(),
        fr.LABEL_CONFIRM,
        [appt],
        [ana],
        ctx,
        "Maria",
    )
    edit = result.appointment_edit
    assert edit is not None and edit["calendar_deferred"] is True
    assert edit["unit_id"] == SUL.id
    assert agenda.log.created == [] and agenda.log.descriptions == []


async def test_a_paid_doctor_and_unit_change_is_deferred_with_the_new_unit():
    """Fix round 1: a new doctor AND his unit, window free there -> deferred, the edit carries
    the new unit (the worker prices and writes it), nothing written on any agenda."""
    ana = _doctor("Dra. Ana", [CENTRO], types=_types("Consulta"))
    beto = _doctor("Dr. Beto", [NORTE], types=_types("Consulta"))
    agendas = {ana.id: _Agenda(), beto.id: _Agenda()}
    appt = _appt(ana)
    draft = ae.EditDraft.from_appointment(appt, TZ).with_changes(
        professional_id=str(beto.id), unit_id=str(NORTE.id)
    )
    ctx = ae.EditContext(
        calendars={str(d.id): agendas[d.id] for d in (ana, beto)}, paid_deposit=True
    )
    result = await edit_step(
        conversation(draft, fr.STEP_EDIT_CONFIRM),
        tenant(),
        fr.LABEL_CONFIRM,
        [appt],
        [ana, beto],
        ctx,
        "Maria",
    )
    edit = result.appointment_edit
    assert edit is not None and edit["calendar_deferred"] is True
    assert edit["professional_id"] == beto.id and edit["unit_id"] == NORTE.id
    assert agendas[beto.id].log.checked_hours  # the new doctor's window was checked
    assert agendas[beto.id].log.created == [] and agendas[ana.id].log.created == []


# --- Fix round 1: at the reschedule limit a busy new doctor goes back, in plain words -----


def _limit_ctx(*, free_b: bool):
    _old, new, cals = _two_agendas(free_b=free_b)
    return new, context(calendars=cals, paid_deposit=True, reschedule_blocked=True)


async def test_at_the_limit_a_busy_new_doctor_goes_back_with_a_doctor_line():
    new, ctx = _limit_ctx(free_b=False)
    doctor_b = professionals()[1]
    tap = f"{fr._professional_row_title(doctor_b.name)} ({doctor_b.id})"
    result = await _step(fr.STEP_EDIT_DOCTOR, tap, ctx=ctx)

    assert result.flow_step == fr.STEP_EDIT_MENU
    assert result.bubbles[0].body == ae.EDIT_DOCTOR_NO_ROOM
    assert ae.EDIT_SERVICE_NO_ROOM not in [getattr(b, "body", None) for b in result.bubbles]
    back = _draft(result)
    assert back.current["professional_id"] == str(DOCTOR_A)
    assert back.changed() == []
    assert new.created == [] and new.detail_updates == []


async def test_at_the_limit_confirming_a_parked_busy_doctor_never_loops():
    """A doctor drafted before the limit was reached: Confirmar finds him busy -> the doctor
    line and the appointment's own doctor again; a second Confirmar says nothing changed."""
    new, ctx = _limit_ctx(free_b=False)
    draft = draft_for().with_changes(professional_id=str(DOCTOR_B), end_at="2030-10-15T15:50")
    first = await _confirm(draft, ctx=ctx)
    assert first.appointment_edit is None
    assert first.bubbles[0].body == ae.EDIT_DOCTOR_NO_ROOM
    back = _draft(first)
    assert back.changed() == []
    assert back.current["end_at"] == draft.original["end_at"]
    again = await _confirm(back, ctx=ctx)
    assert again.bubbles[0].body == ae.EDIT_NOTHING_CHANGED
    assert new.created == [] and new.detail_updates == []


def test_the_doctor_line_is_plain_and_offers_a_way_out():
    assert "outro médico" in ae.EDIT_DOCTOR_NO_ROOM and "clínica" in ae.EDIT_DOCTOR_NO_ROOM


async def test_at_the_limit_a_free_new_doctor_is_still_deferred():
    new, ctx = _limit_ctx(free_b=True)
    draft = draft_for().with_changes(professional_id=str(DOCTOR_B), end_at="2030-10-15T15:50")
    result = await _confirm(draft, ctx=ctx)
    assert result.appointment_edit["calendar_deferred"] is True
    assert new.created == []


# --- D-R3: the AI may draft the doctor and the convênio with a paid sinal -----------------


async def test_the_ai_drafts_a_convenio_with_a_paid_sinal():
    draft = draft_for()
    result = await apply_ai_edit(
        conversation(draft, fr.STEP_EDIT_MENU),
        tenant(),
        {"insurance": "Amil"},
        [original_appointment(draft)],
        professionals(),
        context(paid_deposit=True),
    )
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert result.appointment_edit is None  # the AI never confirms
    assert result.flow_edit_draft["current"]["insurance"] == "Amil"


async def test_the_ai_drafts_a_doctor_with_a_paid_sinal():
    old, new, cals = _two_agendas()
    draft = draft_for()
    result = await apply_ai_edit(
        conversation(draft, fr.STEP_EDIT_MENU),
        tenant(),
        {"professional": "Dra. Ana Lima"},
        [original_appointment(draft)],
        professionals(),
        context(calendars=cals, paid_deposit=True),
    )
    assert result.appointment_edit is None
    assert result.flow_edit_draft["current"]["professional_id"] == str(DOCTOR_B)
    assert new.created == [] and old.created == []


@pytest.mark.parametrize("field,step", [("doctor", fr.STEP_EDIT_DOCTOR), ("insurance", None)])
async def test_the_ai_opens_the_doctor_and_convenio_lists_with_a_paid_sinal(field, step):
    draft = draft_for()
    result = await apply_ai_edit(
        conversation(draft, fr.STEP_EDIT_MENU),
        tenant(),
        {"open_field": field},
        [original_appointment(draft)],
        professionals(),
        context(paid_deposit=True),
    )
    assert result.flow_step == (step or fr.STEP_EDIT_INSURANCE)


# --- contrato §3.8: a SUSPENDED consultation is never edited by the patient ---------------


def _is_suspended_refusal(result) -> bool:
    return (
        result.action == "reply"
        and [b.body for b in result.bubbles] == [SUSPENDED_VISIT_TEXT]
        and isinstance(result.bubbles[0], TextBubble)
        and result.flow_state != FlowState.EDIT_BOOKING
        and result.flow_edit_draft is None
        and result.appointment_edit is None
    )


def test_opening_the_edit_of_a_suspended_consultation_is_refused():
    result = enter_edit_menu(
        tenant(), appointment(), ae.EditContext(paid_deposit=True, suspended=True)
    )
    assert _is_suspended_refusal(result)


@pytest.mark.parametrize(
    "step,body",
    [
        (fr.STEP_EDIT_MENU, ae.LABEL_EDIT_SERVICE),
        (fr.STEP_EDIT_CONFIRM, fr.LABEL_CONFIRM),
        (fr.STEP_EDIT_DOCTOR, "qualquer coisa"),
    ],
)
async def test_any_step_of_a_suspended_consultation_is_refused(step, body):
    calendar = EditCalendar()
    draft = draft_for().with_changes(service="Retorno", end_at="2030-10-15T15:40")
    appt = original_appointment(draft)
    result = await edit_step(
        conversation(draft, step),
        tenant(),
        body,
        [appt],
        professionals(),
        context(calendar, paid_deposit=True, suspended=True),
        "Maria",
    )
    assert _is_suspended_refusal(result)
    assert calendar.detail_updates == [] and calendar.created == [] and calendar.checked == []


async def test_a_suspended_consultation_hidden_from_the_list_still_gets_the_suspended_words():
    """S5 hides a suspended consultation from the patient's lists, so the worker hands the
    router an empty list: the refusal is still the suspension's, never "não está mais ativa"."""
    draft = draft_for()
    result = await edit_step(
        conversation(draft, fr.STEP_EDIT_MENU),
        tenant(),
        ae.LABEL_EDIT_DOCTOR,
        [],
        professionals(),
        context(suspended=True),
    )
    assert _is_suspended_refusal(result)


async def test_the_ai_cannot_edit_a_suspended_consultation():
    draft = draft_for()
    result = await apply_ai_edit(
        conversation(draft, fr.STEP_EDIT_MENU),
        tenant(),
        {"service": "Retorno"},
        [original_appointment(draft)],
        professionals(),
        context(paid_deposit=True, suspended=True),
    )
    assert _is_suspended_refusal(result)


async def test_redisplaying_a_suspended_edit_is_refused():
    draft = draft_for()
    result = await redisplay_edit(
        conversation(draft, fr.STEP_EDIT_MENU),
        tenant(),
        [original_appointment(draft)],
        professionals(),
        context(suspended=True),
    )
    assert _is_suspended_refusal(result)


def test_a_context_is_not_suspended_by_default():
    assert ae.EditContext().suspended is False


async def test_a_paid_sinal_confirmation_still_moves_the_time_when_the_window_is_free():
    """A money change with a new time: the new window is checked, nothing written."""
    calendar = EditCalendar()
    draft = draft_for().with_changes(
        insurance="Amil", start_at="2030-10-16T09:00", end_at="2030-10-16T09:40"
    )
    result = await _confirm(draft, ctx=context(calendar, paid_deposit=True))
    edit = result.appointment_edit
    assert edit["calendar_deferred"] is True and edit["time_changed"] is True
    assert calendar.checked and calendar.detail_updates == []
    assert edit["start_at"] == START_UTC + timedelta(hours=-6, minutes=-20) + timedelta(days=1)
    assert UUID(str(edit["appointment_id"])) == UUID(draft.appointment_id)
