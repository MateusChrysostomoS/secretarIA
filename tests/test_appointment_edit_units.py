""" "Alterar Dados" respects the appointment's unit (TASK-083 final wave, flow review I1).

Router level, no DB: the fake agenda records WHICH hours each read used (`with_unit` returns a
copy carrying the unit's hours, like CalendarService.with_unit) and answers `is_slot_free` from
those hours. Its own hours are 08-18 every day - exactly the unscoped fallback a doctor with no
own hours gets (`services/calendar.py::_windows_for_day`), the failure the review found.

The doctor attends Centro on Tuesdays and Zona Sul on Thursdays; the appointment is at Centro on
Tuesday 2030-10-15 15:20 (São Paulo).
"""

import os

os.environ.setdefault("APP_ENV", "test")

from datetime import timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

from secretaria.services import appointment_edit as ae, flow_router as fr  # noqa: E402
from secretaria.services.appointment_edit_flow import edit_step  # noqa: E402
from secretaria.services.appointment_edit_write import write_appointment_edit  # noqa: E402
from secretaria.services.unit_booking import UnitOption, within_hours  # noqa: E402
from tests._edit_flow_support import (  # noqa: E402
    START_UTC,
    TZ,
    appointment,
    conversation,
    tenant,
)

_WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
FALLBACK = {day: [{"start": "08:00", "end": "18:00"}] for day in _WEEK}
TUESDAY = {"tuesday": [{"start": "08:00", "end": "18:00"}]}
THURSDAY = {"thursday": [{"start": "08:00", "end": "18:00"}]}

CENTRO = UnitOption(id=uuid4(), name="Centro", address="Rua A, 10", business_hours=TUESDAY)
SUL = UnitOption(id=uuid4(), name="Zona Sul", address="Av. B, 20", business_hours=THURSDAY)
NORTE = UnitOption(id=uuid4(), name="Norte", address=None, business_hours=TUESDAY)
OESTE = UnitOption(id=uuid4(), name="Oeste", address=None, business_hours=TUESDAY)


class _Log:
    def __init__(self):
        self.day_hours: list = []
        self.slot_hours: list = []
        self.checked_hours: list = []
        self.created: list = []
        self.descriptions: list = []


class _Agenda:
    def __init__(self, hours=FALLBACK, *, unit_id=None, location=None, log=None):
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

    async def is_slot_free(self, start, end, *, ignore_event_id=None):
        self.log.checked_hours.append(self.hours)
        local_start = start.astimezone(TZ) if start.tzinfo else start
        local_end = end.astimezone(TZ) if end.tzinfo else end
        return within_hours(self.hours, local_start, local_end)

    async def has_conflict(self, start, end, ignore_event_id=None):
        return False

    async def create_event(self, start, end, summary, description=""):
        self.log.created.append((self.unit_id, self.location))
        self.log.descriptions.append(description)
        return {"id": "evt-new", "htmlLink": None}

    async def update_event_details(self, event_id, start, end, summary, description=""):
        self.log.descriptions.append(description)
        return {"id": event_id}


def _doctor(name, units, *, hours=None, types=None, service_units=None):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=types,
        business_hours=hours,
        min_booking_notice_hours=None,
        units=list(units),
        service_units=service_units or {},
    )


def _tap(doctor) -> str:
    return f"{fr._professional_row_title(doctor.name)} ({doctor.id})"


def _appt(doctor, unit=CENTRO, **kw):
    base = appointment(professional_id=str(doctor.id), **kw)
    if unit is not None:
        base["unit_id"] = str(unit.id)
    return base


async def _step(step, body, *, appt, doctors, calendars, draft=None):
    draft = draft or ae.EditDraft.from_appointment(appt, TZ)
    conv = conversation(draft, step)
    ctx = ae.EditContext(calendars={str(d.id): calendars[d.id] for d in doctors})
    return await edit_step(conv, tenant(), body, [appt], doctors, ctx, "Maria")


def _draft(result) -> ae.EditDraft:
    return ae.EditDraft.from_json(result.flow_edit_draft)


# --- Same doctor: the pickers use the appointment's unit --------------------------------


async def test_mudar_data_and_mudar_horario_offer_the_appointments_unit_hours():
    doctor = _doctor("Dra. Ana", [CENTRO, SUL])
    agenda = _Agenda()
    appt = _appt(doctor)

    await _step(
        fr.STEP_EDIT_MENU,
        ae.LABEL_EDIT_DATE,
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
    )
    await _step(
        fr.STEP_EDIT_MENU,
        ae.LABEL_EDIT_TIME,
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
    )

    assert agenda.log.day_hours == [TUESDAY]
    assert agenda.log.slot_hours == [TUESDAY]


async def test_a_centro_tuesday_booking_cannot_keep_its_time_on_a_zona_sul_thursday():
    """Failure (1): moving the date only, the kept time is checked within CENTRO's hours."""
    doctor = _doctor("Dra. Ana", [CENTRO, SUL])
    agenda = _Agenda()
    appt = _appt(doctor)
    draft = ae.EditDraft.from_appointment(appt, TZ).with_stage(
        mode="date",
        target_day="2030-10-17",  # a Thursday
    )

    result = await _step(
        fr.STEP_EDIT_TIME_TOO,
        ae.LABEL_TIME_TOO_NO,
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=draft,
    )

    assert result.flow_step != fr.STEP_EDIT_CONFIRM
    assert result.bubbles[0].body == ae.EDIT_TIME_BUSY
    assert agenda.log.checked_hours == [TUESDAY]


async def test_a_doctor_with_no_own_hours_never_falls_back_to_every_day_08_18():
    """Failure (2): own hours `{}` + unit hours only -> the unit's days, not Sunday 08:00."""
    doctor = _doctor("Dra. Ana", [CENTRO], hours={})
    agenda = _Agenda()
    appt = _appt(doctor)
    draft = ae.EditDraft.from_appointment(appt, TZ).with_stage(
        mode="date",
        target_day="2030-10-20",  # a Sunday
    )

    result = await _step(
        fr.STEP_EDIT_TIME_TOO,
        ae.LABEL_TIME_TOO_NO,
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=draft,
    )

    assert result.flow_step != fr.STEP_EDIT_CONFIRM
    assert agenda.log.checked_hours == [TUESDAY]


# --- Doctor change: the unit follows the new doctor -------------------------------------


async def _change_doctor(new_doctor, *, appt_unit=CENTRO):
    old = _doctor("Dra. Ana", [CENTRO, SUL])
    appt = _appt(old, appt_unit)
    calendars = {old.id: _Agenda(), new_doctor.id: _Agenda()}
    result = await _step(
        fr.STEP_EDIT_DOCTOR,
        _tap(new_doctor),
        appt=appt,
        doctors=[old, new_doctor],
        calendars=calendars,
    )
    return result, appt, [old, new_doctor], calendars


async def test_a_new_doctor_who_attends_the_unit_keeps_it():
    new = _doctor("Dr. Beto", [NORTE, CENTRO])
    result, *_ = await _change_doctor(new)
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(result).current["unit_id"] == str(CENTRO.id)


async def test_a_new_doctor_with_exactly_one_unit_moves_the_booking_there():
    new = _doctor("Dr. Beto", [NORTE])
    result, appt, doctors, calendars = await _change_doctor(new)
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft(result)
    assert draft.current["unit_id"] == str(NORTE.id)
    assert draft.original["unit_id"] == str(CENTRO.id)

    confirmed = await _step(
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        appt=appt,
        doctors=doctors,
        calendars=calendars,
        draft=draft,
    )
    edit = confirmed.appointment_edit
    assert edit is not None and edit["unit_id"] == NORTE.id
    # The event on the new doctor's agenda carries the new unit (its id and location).
    assert calendars[new.id].log.created == [(NORTE.id, "Norte")]


async def test_a_new_doctor_without_units_clears_the_unit():
    new = _doctor("Dr. Beto", [])
    result, *_ = await _change_doctor(new)
    assert _draft(result).current["unit_id"] is None


async def test_going_back_to_the_original_doctor_restores_the_original_unit():
    old = _doctor("Dra. Ana", [CENTRO, SUL])
    new = _doctor("Dr. Beto", [NORTE])
    appt = _appt(old)
    calendars = {old.id: _Agenda(), new.id: _Agenda()}
    first = await _step(
        fr.STEP_EDIT_DOCTOR, _tap(new), appt=appt, doctors=[old, new], calendars=calendars
    )
    back = await _step(
        fr.STEP_EDIT_DOCTOR,
        _tap(old),
        appt=appt,
        doctors=[old, new],
        calendars=calendars,
        draft=_draft(first),
    )
    assert _draft(back).current["unit_id"] == str(CENTRO.id)


async def test_without_any_unit_the_draft_and_the_edit_are_exactly_todays():
    old = _doctor("Dra. Ana", [])
    new = _doctor("Dr. Beto", [])
    appt = _appt(old, None)
    calendars = {old.id: _Agenda(), new.id: _Agenda()}
    result = await _step(
        fr.STEP_EDIT_DOCTOR, _tap(new), appt=appt, doctors=[old, new], calendars=calendars
    )
    draft = _draft(result)
    assert "unit_id" not in draft.current and "unit_id" not in draft.original
    confirmed = await _step(
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        appt=appt,
        doctors=[old, new],
        calendars=calendars,
        draft=draft,
    )
    assert "unit_id" not in confirmed.appointment_edit


# --- The event's description keeps (or follows) the unit (TASK-083 follow-up) -------------


async def test_an_edit_at_the_same_unit_keeps_the_unit_lines_in_the_event():
    doctor = _doctor("Dra. Ana", [CENTRO, SUL])
    agenda = _Agenda()
    appt = _appt(doctor)
    draft = ae.EditDraft.from_appointment(appt, TZ).with_changes(insurance="Amil")
    confirmed = await _step(
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=draft,
    )
    assert confirmed.appointment_edit is not None
    assert agenda.log.descriptions == [
        "Serviço: Consulta\n"
        "Profissional: Dra. Ana\n"
        "Unidade: Centro\n"
        "Endereço: Rua A, 10\n"
        "Convênio: Amil"
    ]


async def test_a_doctor_change_writes_the_new_units_lines():
    new = _doctor("Dr. Beto", [NORTE])
    result, appt, doctors, calendars = await _change_doctor(new)
    await _step(
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        appt=appt,
        doctors=doctors,
        calendars=calendars,
        draft=_draft(result),
    )
    [description] = calendars[new.id].log.descriptions
    assert "Profissional: Dr. Beto\nUnidade: Norte\nConvênio:" in description
    assert "Endereço" not in description  # Norte has no address


async def test_without_a_unit_the_edited_events_description_is_todays():
    doctor = _doctor("Dra. Ana", [])
    agenda = _Agenda()
    appt = _appt(doctor, None)
    draft = ae.EditDraft.from_appointment(appt, TZ).with_changes(insurance="Amil")
    await _step(
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=draft,
    )
    assert agenda.log.descriptions == ["Serviço: Consulta\nConvênio: Amil"]


# --- The writer -------------------------------------------------------------------------


def _row(unit_id):
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        appointment_type="Consulta",
        professional_id=uuid4(),
        google_calendar_source="professional",
        start_at=START_UTC,
        end_at=START_UTC + timedelta(minutes=40),
        insurance=None,
        attendee_name=None,
        google_event_id="evt-old",
        google_event_link=None,
        status=None,
        unit_id=unit_id,
    )


async def _write(row, **extra):
    from secretaria.services import appointment_edit_write as writer

    async def no_plans(*_a, **_k):
        return None, None

    original = writer.resolve_booking_plan_ids
    writer.resolve_booking_plan_ids = no_plans
    try:
        edit = {
            "appointment_type": "Consulta",
            "professional_id": uuid4(),
            "insurance": None,
            "attendee_name": None,
            "google_event_id": "evt-new",
            "google_event_link": None,
            "start_at": row.start_at,
            "end_at": row.end_at,
            "time_changed": False,
            "doctor_changed": True,
            **extra,
        }
        return await write_appointment_edit(
            None,
            row,
            tenant_id=row.tenant_id,
            timezone="America/Sao_Paulo",
            edit=edit,
            source="test",
            idempotency_key="k",
        )
    finally:
        writer.resolve_booking_plan_ids = original


async def test_the_writer_moves_the_unit_only_when_the_edit_carries_one():
    kept = _row(CENTRO.id)
    await _write(kept)
    assert kept.unit_id == CENTRO.id

    moved = _row(CENTRO.id)
    await _write(moved, unit_id=NORTE.id)
    assert moved.unit_id == NORTE.id

    cleared = _row(CENTRO.id)
    await _write(cleared, unit_id=None)
    assert cleared.unit_id is None


def test_the_draft_carries_the_unit_only_for_a_booking_with_one():
    doctor = _doctor("Dra. Ana", [CENTRO])
    with_unit = ae.EditDraft.from_appointment(_appt(doctor), TZ)
    without = ae.EditDraft.from_appointment(_appt(doctor, None), TZ)
    assert with_unit.original["unit_id"] == str(CENTRO.id)
    assert "unit_id" not in without.original
    assert UUID(with_unit.with_changes(unit_id=str(SUL.id)).current["unit_id"]) == SUL.id
    # Follow-up E1 (D4): a unit change is a change the patient confirms.
    assert with_unit.with_changes(unit_id=str(SUL.id)).changed() == ["unidade"]
    assert without.with_changes(insurance="Amil").changed() == ["convênio"]


# --- Service x unit (final review, finding 1): no path books a service where it isn't done --

CONSULTA_ID = uuid4()
EXAME_ID = uuid4()
RETORNO_ID = uuid4()
FECHADA = UnitOption(id=uuid4(), name="Fechada", address=None, business_hours={})


def _types(*names):
    ids = {"Consulta": CONSULTA_ID, "Exame": EXAME_ID, "Retorno": RETORNO_ID}
    return [
        {"name": name, "duration_min": 40, "is_active": True, "service_id": str(ids[name])}
        for name in names
    ]


async def test_changing_the_service_to_one_done_at_the_unit_goes_on():
    doctor = _doctor(
        "Dra. Ana",
        [CENTRO, SUL],
        types=_types("Consulta", "Exame"),
        service_units={EXAME_ID: frozenset({CENTRO.id})},
    )
    appt = _appt(doctor)
    result = await _step(
        fr.STEP_EDIT_SERVICE, "Exame", appt=appt, doctors=[doctor], calendars={doctor.id: _Agenda()}
    )
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft(result)
    assert draft.current["service"] == "Exame"
    assert draft.current["unit_id"] == str(CENTRO.id)


async def test_without_marks_a_service_change_is_todays():
    doctor = _doctor("Dra. Ana", [CENTRO, SUL], types=_types("Consulta", "Exame"))
    appt = _appt(doctor)
    result = await _step(
        fr.STEP_EDIT_SERVICE, "Exame", appt=appt, doctors=[doctor], calendars={doctor.id: _Agenda()}
    )
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(result).current["service"] == "Exame"


async def test_a_new_doctor_moves_the_booking_to_the_unit_where_the_service_is_done():
    """The new doctor attends CENTRO too, but does the service only at NORTE."""
    new = _doctor(
        "Dr. Beto",
        [NORTE, CENTRO],
        types=_types("Consulta"),
        service_units={CONSULTA_ID: frozenset({NORTE.id})},
    )
    result, *_ = await _change_doctor(new)
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(result).current["unit_id"] == str(NORTE.id)


async def test_a_new_doctor_who_does_the_service_at_no_open_unit_is_refused():
    new = _doctor(
        "Dr. Beto",
        [NORTE, FECHADA],
        types=_types("Consulta"),
        service_units={CONSULTA_ID: frozenset({FECHADA.id})},
    )
    result, appt, *_ = await _change_doctor(new)
    assert result.flow_step == fr.STEP_EDIT_MENU
    assert result.bubbles[0].body == ae.EDIT_DOCTOR_SERVICE_NO_UNIT
    draft = _draft(result)
    assert draft.current["professional_id"] == appt["professional_id"]
    assert draft.current["unit_id"] == str(CENTRO.id)


async def test_a_service_picked_after_a_doctor_change_takes_the_unit_where_it_is_done():
    old = _doctor("Dra. Ana", [CENTRO, SUL])
    new = _doctor(
        "Dr. Beto",
        [NORTE, SUL],
        types=_types("Exame"),
        service_units={EXAME_ID: frozenset({SUL.id})},
    )
    appt = _appt(old)
    calendars = {old.id: _Agenda(), new.id: _Agenda()}
    listed = await _step(
        fr.STEP_EDIT_DOCTOR, _tap(new), appt=appt, doctors=[old, new], calendars=calendars
    )
    assert listed.flow_step == fr.STEP_EDIT_SERVICE
    # Follow-up E1 (D2): he does not attend CENTRO; SUL is the only unit where he does anything,
    # so his services are listed there.
    assert _draft(listed).current["unit_id"] == str(SUL.id)

    picked = await _step(
        fr.STEP_EDIT_SERVICE,
        "Exame",
        appt=appt,
        doctors=[old, new],
        calendars=calendars,
        draft=_draft(listed),
    )
    assert _draft(picked).current["service"] == "Exame"
    assert _draft(picked).current["unit_id"] == str(SUL.id)


async def test_a_new_doctor_with_no_service_at_any_open_unit_is_refused_at_once():
    """Follow-up E1: the notice is for a doctor with no bookable unit at all (here his only
    service is done at a closed unit) - shown at the doctor step, the doctor unchanged."""
    old = _doctor("Dra. Ana", [CENTRO, SUL])
    new = _doctor(
        "Dr. Beto",
        [NORTE, FECHADA],
        types=_types("Exame"),
        service_units={EXAME_ID: frozenset({FECHADA.id})},
    )
    appt = _appt(old)
    calendars = {old.id: _Agenda(), new.id: _Agenda()}
    picked = await _step(
        fr.STEP_EDIT_DOCTOR, _tap(new), appt=appt, doctors=[old, new], calendars=calendars
    )
    assert picked.flow_step == fr.STEP_EDIT_MENU
    assert picked.bubbles[0].body == ae.EDIT_DOCTOR_SERVICE_NO_UNIT
    draft = _draft(picked)
    assert draft.current["professional_id"] == str(old.id)
    assert draft.current["service"] == "Consulta"
    assert draft.current["unit_id"] == str(CENTRO.id)
    assert draft.changed() == []


async def test_a_reschedule_keeps_the_unit_even_if_the_service_is_no_longer_done_there():
    """Spec §9.4: an existing appointment is not invalidated - same service, same doctor,
    new date at its own unit (CENTRO), although the service is now restricted to SUL."""
    doctor = _doctor(
        "Dra. Ana",
        [CENTRO, SUL],
        types=_types("Consulta"),
        service_units={CONSULTA_ID: frozenset({SUL.id})},
    )
    agenda = _Agenda()
    appt = _appt(doctor)
    draft = ae.EditDraft.from_appointment(appt, TZ).with_stage(
        mode="date",
        target_day="2030-10-22",  # the next Tuesday
    )
    moved = await _step(
        fr.STEP_EDIT_TIME_TOO,
        ae.LABEL_TIME_TOO_NO,
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=draft,
    )
    assert moved.flow_step == fr.STEP_EDIT_CONFIRM
    assert agenda.log.checked_hours == [TUESDAY]
    assert _draft(moved).current["unit_id"] == str(CENTRO.id)

    # Picking the same service again is not a change: still at CENTRO, no refusal.
    same = await _step(
        fr.STEP_EDIT_SERVICE,
        "Consulta",
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=_draft(moved),
    )
    assert same.flow_step == fr.STEP_EDIT_CONFIRM

    confirmed = await _step(
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=_draft(same),
    )
    assert confirmed.appointment_edit is not None
    assert "unit_id" not in confirmed.appointment_edit


async def _ai(proposal, *, appt, doctors):
    from secretaria.services import appointment_edit_flow as ef

    draft = ae.EditDraft.from_appointment(appt, TZ)
    ctx = ae.EditContext(calendars={str(d.id): _Agenda() for d in doctors})
    return await ef.apply_ai_edit(
        conversation(draft, fr.STEP_EDIT_MENU), tenant(), proposal, [appt], doctors, ctx
    )


async def test_an_ai_service_change_to_a_unit_where_it_is_not_done_is_not_drafted():
    doctor = _doctor(
        "Dra. Ana",
        [CENTRO, SUL],
        types=_types("Consulta", "Exame"),
        service_units={EXAME_ID: frozenset({SUL.id})},
    )
    appt = _appt(doctor)
    result = await _ai({"service": "Exame"}, appt=appt, doctors=[doctor])
    assert _draft(result).current["service"] == "Consulta"
    assert _draft(result).current["unit_id"] == str(CENTRO.id)


async def test_an_ai_doctor_change_takes_the_unit_where_the_service_is_done():
    old = _doctor("Dra. Ana", [CENTRO, SUL])
    new = _doctor(
        "Dr. Beto",
        [NORTE, CENTRO],
        types=_types("Consulta"),
        service_units={CONSULTA_ID: frozenset({NORTE.id})},
    )
    appt = _appt(old)
    result = await _ai({"professional": "Dr. Beto"}, appt=appt, doctors=[old, new])
    assert _draft(result).current["professional_id"] == str(new.id)
    assert _draft(result).current["unit_id"] == str(NORTE.id)


# --- Follow-up E1: "Alterar dados" picks the unit when the service or the doctor needs it ---

LESTE = UnitOption(id=uuid4(), name="Leste", address="Rua C, 30", business_hours=TUESDAY)
CHANGE_UNIT_ROW = ("edit|change_unit", "Alterar unidade", "Ver serviços em outra unidade")


def _unit_tap(unit) -> str:
    return f"{unit.name} ({unit.id})"


def _rows(result) -> list:
    return list(result.bubbles[-1].rows)


def _row_ids(result) -> list[str]:
    return [row[0] for row in _rows(result)]


def _ana_with_exame_at(*units, at=(CENTRO, SUL, LESTE)):
    return _doctor(
        "Dra. Ana",
        list(at),
        types=_types("Consulta", "Exame"),
        service_units={EXAME_ID: frozenset(u.id for u in units)},
    )


def _beto(units, **marks):
    """Dr. Beto with Consulta + Exame; `marks` restrict a service (by name) to some units."""
    ids = {"Consulta": CONSULTA_ID, "Exame": EXAME_ID}
    return _doctor(
        "Dr. Beto",
        units,
        types=_types("Consulta", "Exame"),
        service_units={ids[name]: frozenset(u.id for u in at) for name, at in marks.items()},
    )


async def test_service_not_at_current_unit_opens_unit_list_of_that_service():
    doctor = _ana_with_exame_at(SUL, LESTE)
    appt = _appt(doctor)
    result = await _step(
        fr.STEP_EDIT_SERVICE, "Exame", appt=appt, doctors=[doctor], calendars={doctor.id: _Agenda()}
    )
    assert result.flow_step == fr.STEP_EDIT_UNIT
    bubble = result.bubbles[-1]
    assert bubble.body == ae.EDIT_UNIT_FOR_SERVICE_BODY.format(servico="Exame")
    assert bubble.button_label == "Ver unidades" and bubble.section_title == "Unidades"
    assert _row_ids(result) == [f"unit|{SUL.id}", f"unit|{LESTE.id}"]
    draft = _draft(result)
    # Nothing is drafted before the unit is picked.
    assert draft.current["service"] == "Consulta"
    assert draft.current["unit_id"] == str(CENTRO.id)
    assert draft.stage["pending_service"] == "Exame"

    # D3: one unit doing the service is still an explicit one-row choice.
    only = _ana_with_exame_at(SUL)
    single = await _step(
        fr.STEP_EDIT_SERVICE,
        "Exame",
        appt=_appt(only),
        doctors=[only],
        calendars={only.id: _Agenda()},
    )
    assert single.flow_step == fr.STEP_EDIT_UNIT
    assert _row_ids(single) == [f"unit|{SUL.id}"]


async def test_picking_unit_applies_service_and_unit_and_rechecks_slot():
    doctor = _ana_with_exame_at(SUL, LESTE)
    appt = _appt(doctor)
    agenda = _Agenda()
    listed = await _step(
        fr.STEP_EDIT_SERVICE, "Exame", appt=appt, doctors=[doctor], calendars={doctor.id: agenda}
    )
    agenda.log.checked_hours.clear()

    # LESTE opens on Tuesdays: the current time is free there -> the confirmation.
    picked = await _step(
        fr.STEP_EDIT_UNIT,
        _unit_tap(LESTE),
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=_draft(listed),
    )
    assert picked.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft(picked)
    assert draft.current["service"] == "Exame"
    assert draft.current["unit_id"] == str(LESTE.id)
    assert agenda.log.checked_hours == [TUESDAY]

    # SUL opens on Thursdays only: the Tuesday time is not free there -> day, then time.
    moved = await _step(
        fr.STEP_EDIT_UNIT,
        _unit_tap(SUL),
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=_draft(listed),
    )
    assert moved.flow_step in fr._EDIT_DAY_STEPS
    assert moved.bubbles[0].body == ae.EDIT_RESLOT_NOTICE
    assert _draft(moved).current["unit_id"] == str(SUL.id)
    assert _draft(moved).current["service"] == "Exame"


async def test_service_done_nowhere_keeps_old_service_with_notice():
    doctor = _doctor(
        "Dra. Ana",
        [CENTRO, SUL, FECHADA],
        types=_types("Consulta", "Exame"),
        service_units={EXAME_ID: frozenset({FECHADA.id})},
    )
    appt = _appt(doctor)
    result = await _step(
        fr.STEP_EDIT_SERVICE, "Exame", appt=appt, doctors=[doctor], calendars={doctor.id: _Agenda()}
    )
    assert result.flow_step == fr.STEP_EDIT_MENU
    assert result.bubbles[0].body == ae.EDIT_SERVICE_NOT_AT_UNIT
    draft = _draft(result)
    assert draft.current["service"] == "Consulta"
    assert draft.current["unit_id"] == str(CENTRO.id)


async def test_new_doctor_without_service_at_unit_lists_his_services_there():
    new = _beto([CENTRO, SUL], Consulta=[SUL])
    result, appt, doctors, calendars = await _change_doctor(new)
    assert result.flow_step == fr.STEP_EDIT_SERVICE
    bubble = result.bubbles[-1]
    assert bubble.body == ae.EDIT_SERVICES_AT_UNIT_BODY.format(medico="Dr. Beto", unidade="Centro")
    assert bubble.button_label == "Ver serviços" and bubble.section_title == "Serviços"
    assert _row_ids(result) == ["svc|Exame", "edit|change_unit"]
    assert _rows(result)[-1] == CHANGE_UNIT_ROW
    draft = _draft(result)
    assert draft.current["professional_id"] == str(new.id)
    assert draft.current["unit_id"] == str(CENTRO.id)
    assert draft.stage["at_unit"] == str(CENTRO.id)

    picked = await _step(
        fr.STEP_EDIT_SERVICE,
        "Exame",
        appt=appt,
        doctors=doctors,
        calendars=calendars,
        draft=draft,
    )
    assert picked.flow_step == fr.STEP_EDIT_CONFIRM
    done = _draft(picked)
    assert done.current["service"] == "Exame"
    assert done.current["professional_id"] == str(new.id)
    assert done.current["unit_id"] == str(CENTRO.id)


async def test_change_unit_row_only_when_doctor_does_service_elsewhere():
    # He does not offer Consulta at all.
    no_service = _doctor("Dr. Beto", [CENTRO, SUL], types=_types("Exame"))
    result, *_ = await _change_doctor(no_service)
    assert result.flow_step == fr.STEP_EDIT_SERVICE
    assert _row_ids(result) == ["svc|Exame"]

    # He does Consulta only at a closed unit.
    closed = _beto([CENTRO, FECHADA], Consulta=[FECHADA])
    result, *_ = await _change_doctor(closed)
    assert result.flow_step == fr.STEP_EDIT_SERVICE
    assert _row_ids(result) == ["svc|Exame"]

    # He does Consulta at another open unit: the extra row, last.
    elsewhere = _beto([CENTRO, SUL], Consulta=[SUL])
    result, *_ = await _change_doctor(elsewhere)
    assert _row_ids(result)[-1] == "edit|change_unit"


async def test_change_unit_lists_doctor_units_then_services_at_chosen_unit():
    new = _beto([CENTRO, SUL], Consulta=[SUL])
    listed, appt, doctors, calendars = await _change_doctor(new)

    units = await _step(
        fr.STEP_EDIT_SERVICE,
        "Alterar unidade",
        appt=appt,
        doctors=doctors,
        calendars=calendars,
        draft=_draft(listed),
    )
    assert units.flow_step == fr.STEP_EDIT_UNIT
    bubble = units.bubbles[-1]
    assert bubble.body == ae.EDIT_DOCTOR_UNITS_BODY.format(medico="Dr. Beto")
    assert bubble.button_label == "Ver unidades" and bubble.section_title == "Unidades"
    assert _row_ids(units) == [f"unit|{CENTRO.id}", f"unit|{SUL.id}"]

    at_sul = await _step(
        fr.STEP_EDIT_UNIT,
        _unit_tap(SUL),
        appt=appt,
        doctors=doctors,
        calendars=calendars,
        draft=_draft(units),
    )
    assert at_sul.flow_step == fr.STEP_EDIT_SERVICE
    assert at_sul.bubbles[-1].body == ae.EDIT_SERVICES_AT_UNIT_BODY.format(
        medico="Dr. Beto", unidade="Zona Sul"
    )
    # Another unit exists, so the row stays to go back to the unit list.
    assert _row_ids(at_sul) == ["svc|Consulta", "svc|Exame", "edit|change_unit"]

    picked = await _step(
        fr.STEP_EDIT_SERVICE,
        "Consulta",
        appt=appt,
        doctors=doctors,
        calendars=calendars,
        draft=_draft(at_sul),
    )
    # SUL opens on Thursdays: the Tuesday time is re-picked for the new doctor/unit.
    assert picked.flow_step in fr._EDIT_DAY_STEPS
    assert picked.bubbles[0].body == ae.EDIT_RESLOT_NOTICE
    draft = _draft(picked)
    assert draft.current["professional_id"] == str(new.id)
    assert draft.current["service"] == "Consulta"
    assert draft.current["unit_id"] == str(SUL.id)


async def test_new_doctor_not_at_preselected_unit_goes_to_unit_list():
    new = _doctor("Dr. Beto", [NORTE, OESTE])
    listed, appt, doctors, calendars = await _change_doctor(new)
    assert listed.flow_step == fr.STEP_EDIT_UNIT
    assert listed.bubbles[-1].body == ae.EDIT_DOCTOR_UNITS_BODY.format(medico="Dr. Beto")
    assert _row_ids(listed) == [f"unit|{NORTE.id}", f"unit|{OESTE.id}"]

    at_norte = await _step(
        fr.STEP_EDIT_UNIT,
        "norte",  # typed, not tapped
        appt=appt,
        doctors=doctors,
        calendars=calendars,
        draft=_draft(listed),
    )
    assert at_norte.flow_step == fr.STEP_EDIT_SERVICE
    assert _row_ids(at_norte) == ["svc|Consulta", "edit|change_unit"]

    picked = await _step(
        fr.STEP_EDIT_SERVICE,
        "Consulta",
        appt=appt,
        doctors=doctors,
        calendars=calendars,
        draft=_draft(at_norte),
    )
    assert picked.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft(picked)
    assert draft.current["unit_id"] == str(NORTE.id)
    confirmed = await _step(
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        appt=appt,
        doctors=doctors,
        calendars=calendars,
        draft=draft,
    )
    assert confirmed.appointment_edit["unit_id"] == NORTE.id
    assert calendars[new.id].log.created == [(NORTE.id, "Norte")]

    # A single unit where he works is implied: no list, his services there.
    alone = _doctor("Dr. Beto", [OESTE], types=_types("Exame"))
    result, *_ = await _change_doctor(alone)
    assert result.flow_step == fr.STEP_EDIT_SERVICE
    assert _row_ids(result) == ["svc|Exame"]
    assert _draft(result).current["unit_id"] == str(OESTE.id)


async def test_new_doctor_doing_service_at_unit_keeps_unit_as_today():
    new = _beto([NORTE, CENTRO], Consulta=[CENTRO, NORTE])
    result, *_ = await _change_doctor(new)
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft(result)
    assert draft.current["unit_id"] == str(CENTRO.id)
    assert draft.changed() == ["médico"]


async def test_reschedule_keeps_original_unit():
    """Spec §9.4: same doctor and service, a new time - the unit is never asked again, even if
    the service has since left the booking's unit."""
    doctor = _doctor(
        "Dra. Ana",
        [CENTRO, SUL],
        types=_types("Consulta"),
        service_units={CONSULTA_ID: frozenset({SUL.id})},
    )
    agenda = _Agenda()
    appt = _appt(doctor)
    picker = await _step(
        fr.STEP_EDIT_MENU,
        ae.LABEL_EDIT_TIME,
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
    )
    assert picker.flow_step == fr.STEP_EDIT_SLOT
    moved = await _step(
        fr.STEP_EDIT_SLOT,
        "09:00 (2030-10-15T09:00)",
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=_draft(picker),
    )
    assert moved.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft(moved)
    assert draft.current["unit_id"] == str(CENTRO.id)
    assert draft.changed() == ["horário"]
    assert "Unidade: Centro" in moved.bubbles[-1].body


async def test_clinic_without_units_edit_flow_unchanged():
    old = _doctor("Dra. Ana", [], types=_types("Consulta", "Exame"))
    new = _doctor("Dr. Beto", [], types=_types("Consulta", "Exame"))
    appt = _appt(old, None)
    calendars = {old.id: _Agenda(), new.id: _Agenda()}
    service = await _step(
        fr.STEP_EDIT_SERVICE, "Exame", appt=appt, doctors=[old, new], calendars=calendars
    )
    assert service.flow_step == fr.STEP_EDIT_CONFIRM
    body = service.bubbles[-1].body
    assert "Unidade" not in body and "unidade" not in body
    assert "Endereço: Rua das Flores, 10" in body
    assert body.endswith("O que mudou: serviço.")
    listed = await _step(
        fr.STEP_EDIT_MENU,
        ae.LABEL_EDIT_SERVICE,
        appt=appt,
        doctors=[old, new],
        calendars=calendars,
    )
    assert listed.bubbles[-1].body == ae.EDIT_SERVICE_BODY
    assert _row_ids(listed) == ["svc|Consulta", "svc|Exame"]
    doctor = await _step(
        fr.STEP_EDIT_DOCTOR, _tap(new), appt=appt, doctors=[old, new], calendars=calendars
    )
    assert doctor.flow_step == fr.STEP_EDIT_CONFIRM
    assert "unit_id" not in _draft(doctor).current


async def test_summary_lists_unit_change_and_unit_address():
    doctor = _ana_with_exame_at(LESTE)
    appt = _appt(doctor)
    agenda = _Agenda()
    listed = await _step(
        fr.STEP_EDIT_SERVICE, "Exame", appt=appt, doctors=[doctor], calendars={doctor.id: agenda}
    )
    card = await _step(
        fr.STEP_EDIT_UNIT,
        _unit_tap(LESTE),
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=_draft(listed),
    )
    body = card.bubbles[-1].body
    assert "Médico: Dra. Ana\nUnidade: Leste\n" in body
    assert "Endereço: Rua C, 30" in body
    assert "Rua das Flores" not in body
    assert body.endswith("O que mudou: serviço, unidade.")

    confirmed = await _step(
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=_draft(card),
    )
    assert confirmed.appointment_edit["unit_id"] == LESTE.id
    recap = confirmed.bubbles[0].body
    assert recap.startswith(ae.EDIT_APPLIED)
    assert "Unidade: Leste" in recap and "Endereço: Rua C, 30" in recap
    # The event itself carries the new unit's lines.
    assert agenda.log.descriptions and "Unidade: Leste" in agenda.log.descriptions[-1]


async def test_summary_with_clinic_address_dict_does_not_crash():
    doctor = _doctor("Dra. Ana", [])
    agenda = _Agenda()
    appt = _appt(doctor, None)
    draft = ae.EditDraft.from_appointment(appt, TZ)
    clinic = tenant(
        address={"line": "Rua X, 1", "neighborhood": "Centro", "city": "São Paulo", "state": "SP"}
    )
    ctx = ae.EditContext(calendars={str(doctor.id): agenda})
    card = await edit_step(
        conversation(draft, fr.STEP_EDIT_INSURANCE), clinic, "Amil", [appt], [doctor], ctx, "Maria"
    )
    assert card.flow_step == fr.STEP_EDIT_CONFIRM
    assert "Endereço: Rua X, 1, Centro, São Paulo/SP" in card.bubbles[-1].body
    confirmed = await edit_step(
        conversation(_draft(card), fr.STEP_EDIT_CONFIRM),
        clinic,
        fr.LABEL_CONFIRM,
        [appt],
        [doctor],
        ctx,
        "Maria",
    )
    assert confirmed.appointment_edit is not None
    assert "Endereço: Rua X, 1, Centro, São Paulo/SP" in confirmed.bubbles[0].body


async def test_ai_proposal_service_not_at_unit_opens_unit_list():
    doctor = _ana_with_exame_at(SUL, LESTE)
    appt = _appt(doctor)
    result = await _ai({"service": "Exame"}, appt=appt, doctors=[doctor])
    assert result.flow_step == fr.STEP_EDIT_UNIT
    assert _row_ids(result) == [f"unit|{SUL.id}", f"unit|{LESTE.id}"]
    assert _draft(result).current["service"] == "Consulta"
    assert _draft(result).stage["pending_service"] == "Exame"

    # A new doctor who does the current service at no unit he can be booked at: his services at
    # the booking's unit (step b) instead of a silent no-op.
    old = _doctor("Dra. Ana", [CENTRO, SUL])
    new = _beto([CENTRO], Consulta=[FECHADA])
    result = await _ai({"professional": "Dr. Beto"}, appt=_appt(old), doctors=[old, new])
    assert result.flow_step == fr.STEP_EDIT_SERVICE
    assert _row_ids(result) == ["svc|Exame"]


async def test_ai_proposal_with_valid_unit_sets_unit():
    doctor = _ana_with_exame_at(SUL, LESTE)
    appt = _appt(doctor)
    result = await _ai({"service": "Exame", "unit": "Leste"}, appt=appt, doctors=[doctor])
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft(result)
    assert draft.current["service"] == "Exame"
    assert draft.current["unit_id"] == str(LESTE.id)

    # The unit alone (Consulta is done everywhere): only the unit changes.
    only_unit = await _ai({"unit": "leste"}, appt=appt, doctors=[doctor])
    assert only_unit.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(only_unit).current["unit_id"] == str(LESTE.id)
    assert only_unit.bubbles[-1].body.endswith("O que mudou: unidade.")

    # A unit whose hours do not hold the current time: day, then time.
    thursday = await _ai({"service": "Exame", "unit": "Zona Sul"}, appt=appt, doctors=[doctor])
    assert thursday.flow_step in fr._EDIT_DAY_STEPS
    assert _draft(thursday).current["unit_id"] == str(SUL.id)


async def test_ai_proposal_with_invalid_unit_opens_unit_list():
    doctor = _ana_with_exame_at(SUL, LESTE)
    appt = _appt(doctor)
    for proposal in ({"service": "Exame", "unit": "Centro"}, {"service": "Exame", "unit": "Lua"}):
        result = await _ai(proposal, appt=appt, doctors=[doctor])
        assert result.flow_step == fr.STEP_EDIT_UNIT
        assert _row_ids(result) == [f"unit|{SUL.id}", f"unit|{LESTE.id}"]
        assert _draft(result).current["service"] == "Consulta"
        assert _draft(result).current["unit_id"] == str(CENTRO.id)


async def test_free_text_on_unit_step_delegates_once():
    from secretaria.services import appointment_edit_flow as ef
    from secretaria.services.llm_context import _STEP_LABELS

    doctor = _ana_with_exame_at(SUL, LESTE)
    appt = _appt(doctor)
    listed = await _step(
        fr.STEP_EDIT_SERVICE, "Exame", appt=appt, doctors=[doctor], calendars={doctor.id: _Agenda()}
    )
    talk = await _step(
        fr.STEP_EDIT_UNIT,
        "qual fica mais perto do metrô?",
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: _Agenda()},
        draft=_draft(listed),
    )
    assert talk.action == "delegate_llm"
    assert talk.flow_step == fr.STEP_EDIT_UNIT
    assert talk.flow_edit_draft == listed.flow_edit_draft
    assert fr.STEP_EDIT_UNIT in _STEP_LABELS

    # The model could not help: the same list is drawn again, never a second model turn.
    ctx = ae.EditContext(calendars={str(doctor.id): _Agenda()})
    again = await ef.redisplay_edit(
        conversation(_draft(listed), fr.STEP_EDIT_UNIT), tenant(), [appt], [doctor], ctx
    )
    assert again.action == "reply"
    assert again.flow_step == fr.STEP_EDIT_UNIT
    assert _row_ids(again) == _row_ids(listed)
    assert again.flow_edit_draft == listed.flow_edit_draft
