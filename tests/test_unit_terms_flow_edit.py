"""TASK-085 T4b / A8: the patient's "Alterar dados" sizes the new window at the draft's unit.

Router level, no DB, on the fakes of tests/test_appointment_edit_units.py (every service lasts
40 minutes by default; the appointment is at Centro, Tuesday 15:20, 40 minutes). Each scenario
runs twice: without exceptions the new `end_at` is exactly today's (characterization), and with
the doctor's own duration at the unit the new `end_at` follows that unit (D2/D7). The MONEY side
of a unit change is T4a's (workers/shared/appointment_edit_apply.py) - not exercised here.
"""

import os

os.environ.setdefault("APP_ENV", "test")

from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.service_terms import UnitTerms  # noqa: E402
from tests.test_appointment_edit_units import (  # noqa: E402
    CENTRO,
    CONSULTA_ID,
    EXAME_ID,
    LESTE,
    NORTE,
    SUL,
    _Agenda,
    _ai,
    _ana_with_exame_at,
    _appt,
    _beto,
    _change_doctor,
    _doctor,
    _draft,
    _step,
    _types,
    _unit_tap,
)


def _minutes(result) -> int:
    return _draft(result).duration_minutes


async def _service_and_unit(terms):
    """(a) "Exame" is done only at Zona Sul / Leste: the unit list, then Leste."""
    doctor = _ana_with_exame_at(SUL, LESTE)
    doctor.service_unit_terms = terms
    appt = _appt(doctor)
    agenda = _Agenda()
    listed = await _step(
        fr.STEP_EDIT_SERVICE, "Exame", appt=appt, doctors=[doctor], calendars={doctor.id: agenda}
    )
    picked = await _step(
        fr.STEP_EDIT_UNIT,
        _unit_tap(LESTE),
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: agenda},
        draft=_draft(listed),
    )
    assert picked.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(picked).current["unit_id"] == str(LESTE.id)
    return picked


async def test_service_and_unit_picked_together_take_the_units_length():
    assert _minutes(await _service_and_unit({})) == 40
    longer = await _service_and_unit({EXAME_ID: {LESTE.id: UnitTerms(None, 60)}})
    assert _minutes(longer) == 60
    # A price-only exception keeps the base length.
    priced = await _service_and_unit({EXAME_ID: {LESTE.id: UnitTerms("R$ 300,00", None)}})
    assert _minutes(priced) == 40


async def _service_at_current_unit(terms):
    """A service done everywhere, changed at the booking's own unit (Centro)."""
    doctor = _doctor("Dra. Ana", [CENTRO, SUL], types=_types("Consulta", "Exame"))
    doctor.service_unit_terms = terms
    appt = _appt(doctor)
    result = await _step(
        fr.STEP_EDIT_SERVICE,
        "Exame",
        appt=appt,
        doctors=[doctor],
        calendars={doctor.id: _Agenda()},
    )
    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(result).current["unit_id"] == str(CENTRO.id)
    return result


async def test_a_new_service_at_the_booking_unit_takes_that_units_length():
    assert _minutes(await _service_at_current_unit({})) == 40
    # Exame lasts 50 at Centro (30 at Zona Sul does not matter: the booking is at Centro).
    terms = {EXAME_ID: {CENTRO.id: UnitTerms(None, 50), SUL.id: UnitTerms(None, 30)}}
    assert _minutes(await _service_at_current_unit(terms)) == 50


async def test_a_new_doctor_keeping_the_unit_takes_his_length_there():
    for terms, expected in (({}, 40), ({CONSULTA_ID: {CENTRO.id: UnitTerms(None, 50)}}, 50)):
        new = _beto([NORTE, CENTRO], Consulta=[CENTRO, NORTE])
        new.service_unit_terms = terms
        result, *_ = await _change_doctor(new)
        assert result.flow_step == fr.STEP_EDIT_CONFIRM
        assert _draft(result).current["unit_id"] == str(CENTRO.id)
        assert _minutes(result) == expected


async def test_a_new_doctor_with_one_unit_takes_his_length_there():
    for terms, expected in (({}, 40), ({CONSULTA_ID: {NORTE.id: UnitTerms(None, 55)}}, 55)):
        new = _doctor("Dr. Beto", [NORTE], types=_types("Consulta"))
        new.service_unit_terms = terms
        result, *_ = await _change_doctor(new)
        assert _draft(result).current["unit_id"] == str(NORTE.id)
        assert _minutes(result) == expected


async def test_a_service_picked_on_the_new_doctors_unit_list_takes_his_length_there():
    for terms, expected in (({}, 40), ({EXAME_ID: {CENTRO.id: UnitTerms(None, 25)}}, 25)):
        new = _beto([CENTRO, SUL], Consulta=[SUL])
        new.service_unit_terms = terms
        listed, appt, doctors, calendars = await _change_doctor(new)
        assert listed.flow_step == fr.STEP_EDIT_SERVICE
        picked = await _step(
            fr.STEP_EDIT_SERVICE,
            "Exame",
            appt=appt,
            doctors=doctors,
            calendars=calendars,
            draft=_draft(listed),
        )
        assert _draft(picked).current["unit_id"] == str(CENTRO.id)
        assert _draft(picked).current["service"] == "Exame"
        assert _minutes(picked) == expected


async def test_an_ai_unit_change_takes_the_new_units_length():
    """The patient (through the AI) moves the booking to Leste: Leste's length, set AFTER the
    unit is settled (it used to be computed before the unit was known)."""
    for terms, expected in (({}, 40), ({CONSULTA_ID: {LESTE.id: UnitTerms(None, 70)}}, 70)):
        doctor = _doctor("Dra. Ana", [CENTRO, SUL, LESTE], types=_types("Consulta", "Exame"))
        doctor.service_unit_terms = terms
        result = await _ai({"unit": "leste"}, appt=_appt(doctor), doctors=[doctor])
        assert result.flow_step == fr.STEP_EDIT_CONFIRM
        assert _draft(result).current["unit_id"] == str(LESTE.id)
        assert _minutes(result) == expected


async def test_an_ai_service_and_unit_change_takes_that_units_length():
    for terms, expected in (({}, 40), ({EXAME_ID: {LESTE.id: UnitTerms(None, 45)}}, 45)):
        doctor = _ana_with_exame_at(SUL, LESTE)
        doctor.service_unit_terms = terms
        result = await _ai(
            {"service": "Exame", "unit": "Leste"}, appt=_appt(doctor), doctors=[doctor]
        )
        assert result.flow_step == fr.STEP_EDIT_CONFIRM
        assert _minutes(result) == expected
