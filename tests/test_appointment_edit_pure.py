"""The pure pieces of "Alterar Dados" (TASK-032 R6, spec §5.2/§5.5)."""

from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from secretaria.core.whatsapp_limits import MAX_BUTTON_LABEL_CHARS, MAX_INTERACTIVE_BODY_CHARS
from secretaria.services import appointment_edit as ae

TZ = ZoneInfo("America/Sao_Paulo")
DOCTOR = str(uuid4())


def _appt(**kw):
    base = {
        "id": str(uuid4()),
        "appointment_type": "Consulta",
        "start_at": datetime(2026, 10, 16, 18, 20, tzinfo=UTC),  # 15:20 in São Paulo
        "end_at": datetime(2026, 10, 16, 19, 0, tzinfo=UTC),
        "professional_id": DOCTOR,
        "insurance": "Unimed",
        "attendee_name": None,
    }
    base.update(kw)
    return base


def test_every_label_fits_a_button_and_a_list_row():
    labels = [
        ae.LABEL_EDIT_DATE,
        ae.LABEL_EDIT_TIME,
        ae.LABEL_EDIT_SERVICE,
        ae.LABEL_EDIT_DOCTOR,
        ae.LABEL_EDIT_MORE,
        ae.LABEL_EDIT_INSURANCE,
        ae.LABEL_EDIT_PATIENT,
        ae.LABEL_EDIT_BACK,
        ae.LABEL_EDIT_MORE_DATA,
        ae.LABEL_TIME_TOO_YES,
        ae.LABEL_TIME_TOO_NO,
    ]
    assert all(0 < len(label) <= MAX_BUTTON_LABEL_CHARS for label in labels)
    assert len(ae.EDIT_MENU_BODY) <= MAX_INTERACTIVE_BODY_CHARS


def test_the_menu_text_carries_the_owners_description_of_alterar_dados():
    assert "Alterar Dados" in ae.EDIT_MENU_BODY
    assert "qualquer outra coisa dessa sua consulta já marcada" in ae.EDIT_MENU_BODY


def test_a_draft_starts_equal_to_the_appointment_in_clinic_time():
    draft = ae.EditDraft.from_appointment(_appt(), TZ)
    assert draft.current == draft.original
    assert draft.current["start_at"] == "2026-10-16T15:20"
    assert draft.current["end_at"] == "2026-10-16T16:00"
    assert draft.duration_minutes == 40
    assert draft.changed() == []


def test_changed_names_each_field_once_in_a_fixed_order():
    draft = ae.EditDraft.from_appointment(_appt(), TZ)
    changed = draft.with_changes(
        start_at="2026-10-17T09:00",
        end_at="2026-10-17T09:40",
        service="Retorno",
        professional_id=str(uuid4()),
        insurance=None,
        attendee_name="Ana",
    )
    assert changed.changed() == ["data", "horário", "serviço", "médico", "convênio", "paciente"]
    only_time = draft.with_changes(start_at="2026-10-16T16:00", end_at="2026-10-16T16:40")
    assert only_time.changed() == ["horário"]
    only_day = draft.with_changes(start_at="2026-10-19T15:20", end_at="2026-10-19T16:00")
    assert only_day.changed() == ["data"]


def test_the_original_never_moves_and_the_draft_is_immutable():
    draft = ae.EditDraft.from_appointment(_appt(), TZ)
    changed = draft.with_changes(service="Retorno")
    assert draft.current["service"] == "Consulta"
    assert changed.original["service"] == "Consulta"
    with pytest.raises(AttributeError):
        draft.appointment_id = "x"  # type: ignore[misc]


def test_json_round_trip_and_garbage_is_none():
    draft = ae.EditDraft.from_appointment(_appt(), TZ).with_stage(
        mode="date", target_day="2026-10-20"
    )
    again = ae.EditDraft.from_json(draft.to_json())
    assert again == draft
    for garbage in (
        None,
        {},
        {"appointment_id": "x"},
        "nope",
        {"appointment_id": "x", "current": 1},
    ):
        assert ae.EditDraft.from_json(garbage) is None


def test_with_stage_replaces_the_whole_stage():
    draft = ae.EditDraft.from_appointment(_appt(), TZ).with_stage(mode="date")
    assert draft.with_stage().stage == {}
    assert draft.with_stage(mode="reslot").stage == {"mode": "reslot"}


def test_the_recap_lists_everything_and_what_changed():
    draft = ae.EditDraft.from_appointment(_appt(), TZ).with_changes(
        start_at="2026-10-19T10:00", end_at="2026-10-19T10:40", attendee_name="Ana Souza"
    )
    text = ae.build_edit_recap(
        draft,
        doctor="Dr. Diogo Raposo",
        address="Rua das Flores, 10",
        requirements=["Trazer exames anteriores", "Jejum de 4 horas"],
    )
    assert "Serviço: Consulta" in text
    assert "Médico: Dr. Diogo Raposo" in text
    assert "Data: 19/10/2026 (segunda-feira)" in text
    assert "Horário: 10:00" in text
    assert "Convênio: Unimed" in text
    assert "Paciente: Ana Souza" in text
    assert "Endereço: Rua das Flores, 10" in text
    assert "• Trazer exames anteriores" in text
    assert text.rstrip().endswith("O que mudou: data, horário, paciente.")


def test_the_recap_defaults_and_the_nothing_changed_line():
    text = ae.build_edit_recap(
        ae.EditDraft.from_appointment(_appt(insurance=None), TZ),
        doctor=None,
        address=None,
        requirements=[],
    )
    assert "Médico:" not in text
    assert "Endereço:" not in text
    assert "Orientações" not in text
    assert "Convênio: não informado" in text
    assert "Paciente: você" in text
    assert text.rstrip().endswith("Ainda não mudou nada.")


def test_a_long_recap_drops_orientations_before_it_breaks_the_card_limit():
    text = ae.build_edit_recap(
        ae.EditDraft.from_appointment(_appt(), TZ),
        doctor="Dr. Diogo Raposo",
        address="Rua " + "x" * 200,
        requirements=["Trazer exames anteriores e documentos " + "y" * 120] * 12,
    )
    assert len(text) <= MAX_INTERACTIVE_BODY_CHARS
    assert "Serviço: Consulta" in text and "Horário: 15:20" in text


def test_the_context_resolves_calendars_by_doctor_or_clinic():
    clinic, doctor = object(), object()
    ctx = ae.EditContext(calendars={"tenant": clinic, DOCTOR: doctor})
    assert ctx.calendar_for(DOCTOR) is doctor
    assert ctx.calendar_for(None) is clinic
    assert ctx.calendar_for(str(uuid4())) is None
    assert ae.EditContext().paid_deposit is False and ae.EditContext().reschedule_blocked is False


def test_the_final_recap_can_report_the_applied_update():
    draft = ae.EditDraft.from_appointment(_appt(), TZ)
    text = ae.build_edit_recap(draft, doctor=None, address=None, requirements=[], title="Pronto!")
    assert text.startswith("Pronto!")
