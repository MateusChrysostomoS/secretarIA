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
        return {"id": "evt-new", "htmlLink": None}

    async def update_event_details(self, event_id, start, end, summary, description=""):
        return {"id": event_id}


def _doctor(name, units, *, hours=None):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=None,
        business_hours=hours,
        min_booking_notice_hours=None,
        units=list(units),
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


async def test_a_new_doctor_with_several_other_units_clears_the_unit():
    new = _doctor("Dr. Beto", [NORTE, OESTE])
    result, appt, doctors, calendars = await _change_doctor(new)
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    draft = _draft(result)
    assert draft.current["unit_id"] is None

    confirmed = await _step(
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        appt=appt,
        doctors=doctors,
        calendars=calendars,
        draft=draft,
    )
    assert confirmed.appointment_edit["unit_id"] is None
    assert calendars[new.id].log.created == [(None, None)]


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
    assert with_unit.with_changes(unit_id=str(SUL.id)).changed() == []
