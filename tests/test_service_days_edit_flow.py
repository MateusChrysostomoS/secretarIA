"""TASK-087 T5: "Alterar dados" respects the service's blocked weekdays (patient paths only, D3).

Router level, no DB, on the shared edit-flow fakes (`tests/_edit_flow_support.py`). The appointment
is Tuesday 2030-10-15 15:20 (Consulta, Dr. Diogo, 40 min); Wednesday is 2030-10-16.

The agenda double below behaves like `CalendarService.with_blocked_weekdays`: a blocked weekday has
no free day and `is_slot_free` is False there. Rule R4 (spec §9): the day is only re-checked when
the patient changes day / time / service / doctor / unit - a convênio-only change on a day that
was blocked AFTER the booking was made is never refused.
"""

import copy
import os
from datetime import datetime

os.environ.setdefault("APP_ENV", "test")

from secretaria.services import (  # noqa: E402
    appointment_edit as ae,
    appointment_edit_flow as ef,
    flow_router as fr,
)
from secretaria.services.appointment_edit_flow import edit_step  # noqa: E402
from tests._edit_flow_support import (  # noqa: E402
    DOCTOR_A,
    DOCTOR_B,
    EditCalendar,
    appointment,
    context,
    conversation,
    draft_for,
    original_appointment,
    professionals,
    tenant,
)

_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEDNESDAY = "2030-10-16"
THURSDAY = "2030-10-17"


class BlockableCalendar(EditCalendar):
    """`EditCalendar` + the filter `CalendarService.with_blocked_weekdays` gives the real one."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.blocked: frozenset[str] = frozenset()
        self.seen: list[frozenset[str]] = []  # the `blocked` set of every copy handed out

    def with_blocked_weekdays(self, blocked):
        scoped = copy.copy(self)
        scoped.blocked = frozenset(blocked)
        self.seen.append(scoped.blocked)
        return scoped

    def _is_blocked(self, day) -> bool:
        return _KEYS[day.weekday()] in self.blocked

    async def list_available_days(self, start_day, days, slot_minutes=None):
        found = await super().list_available_days(start_day, days, slot_minutes)
        return [day for day in found if not self._is_blocked(day)]

    async def is_slot_free(self, start, end, *, ignore_event_id=None):
        if self._is_blocked(start):
            self.checked.append((start, end, ignore_event_id))
            return False
        return await super().is_slot_free(start, end, ignore_event_id=ignore_event_id)


def _doctors(*, consulta_blocks=(), cirurgia_blocks=("tuesday",), b_consulta_blocks=()):
    """Dr. Diogo: Consulta + Cirurgia; Dra. Ana: Consulta. Each with its own blocked days."""
    diogo, ana = professionals()
    diogo.appointment_types = [
        {
            "name": "Consulta",
            "duration_min": 40,
            "is_active": True,
            "blocked_weekdays": list(consulta_blocks),
        },
        {
            "name": "Cirurgia",
            "duration_min": 40,
            "is_active": True,
            "blocked_weekdays": list(cirurgia_blocks),
        },
    ]
    ana.appointment_types = [
        {
            "name": "Consulta",
            "duration_min": 40,
            "is_active": True,
            "blocked_weekdays": list(b_consulta_blocks),
        }
    ]
    return [diogo, ana]


def _ctx(**kwargs):
    calendars = {str(DOCTOR_A): BlockableCalendar(), str(DOCTOR_B): BlockableCalendar()}
    return context(calendars=calendars, **kwargs), calendars


async def _step(step, body, *, doctors, ctx, draft=None, appt=None):
    appt = appt or appointment()
    draft = draft or draft_for(appt)
    conv = conversation(draft, step)
    return await edit_step(conv, tenant(), body, [appt], doctors, ctx, "Maria")


def _draft(result) -> ae.EditDraft:
    return ae.EditDraft.from_json(result.flow_edit_draft)


def _row_ids(result) -> list[str]:
    return [row[0] for bubble in result.bubbles for row in (getattr(bubble, "rows", None) or [])]


# --- Day picker --------------------------------------------------------------------------


async def test_edit_change_day_hides_blocked():
    doctors = _doctors(consulta_blocks=("wednesday",))
    ctx, calendars = _ctx()

    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DATE, doctors=doctors, ctx=ctx)

    ids = _row_ids(result)
    assert any(f"day|{THURSDAY}" in row for row in ids), ids
    assert not any(f"day|{WEDNESDAY}" in row for row in ids), ids
    assert calendars[str(DOCTOR_A)].seen == [frozenset({"wednesday"})]


async def test_edit_day_picker_of_a_service_without_blocks_shows_every_day():
    doctors = _doctors(consulta_blocks=())
    ctx, _ = _ctx()

    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DATE, doctors=doctors, ctx=ctx)

    assert any(f"day|{WEDNESDAY}" in row for row in _row_ids(result))


async def test_edit_time_list_reads_the_filtered_agenda():
    """The time list of a day (`_slots_for`) reads the same service-scoped agenda."""
    doctors = _doctors(consulta_blocks=("wednesday",))
    ctx, calendars = _ctx()
    draft = draft_for(appointment())

    await ef._slots_for(
        conversation(draft, fr.STEP_EDIT_SLOT),
        tenant(),
        draft,
        datetime(2030, 10, 16),
        doctors,
        ctx,
    )

    assert calendars[str(DOCTOR_A)].seen == [frozenset({"wednesday"})]


# --- Changing the service ----------------------------------------------------------------


async def test_edit_change_service_to_blocked_on_current_day_reslots():
    """Cirurgia is not done on Tuesdays and the visit is on one: no confirmation, a new day."""
    doctors = _doctors(cirurgia_blocks=("tuesday",))
    ctx, _ = _ctx()

    result = await _step(
        fr.STEP_EDIT_SERVICE, fr._service_row_title("Cirurgia"), doctors=doctors, ctx=ctx
    )

    assert result.flow_step != fr.STEP_EDIT_CONFIRM
    assert result.flow_step in fr._EDIT_DAY_STEPS
    assert result.bubbles[0].body == ae.EDIT_RESLOT_NOTICE
    assert _draft(result).current["service"] == "Cirurgia"  # kept, only the day is asked


async def test_edit_change_service_blocked_on_another_day_goes_to_confirm():
    doctors = _doctors(cirurgia_blocks=("wednesday",))
    ctx, _ = _ctx()

    result = await _step(
        fr.STEP_EDIT_SERVICE, fr._service_row_title("Cirurgia"), doctors=doctors, ctx=ctx
    )

    assert result.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(result).changed() == ["serviço"]


async def test_edit_confirm_refuses_a_service_blocked_on_the_day_and_writes_nothing():
    """Last defence: a confirmation drafted by other means (stale card, forged state)."""
    doctors = _doctors(cirurgia_blocks=("tuesday",))
    ctx, calendars = _ctx()
    appt = appointment()
    forged = draft_for(appt).with_changes(service="Cirurgia")

    result = await _step(
        fr.STEP_EDIT_CONFIRM, fr.LABEL_CONFIRM, doctors=doctors, ctx=ctx, draft=forged, appt=appt
    )

    assert result.appointment_edit is None
    assert result.flow_step in fr._EDIT_DAY_STEPS
    assert calendars[str(DOCTOR_A)].detail_updates == []
    assert calendars[str(DOCTOR_A)].created == []


# --- R4: not touching day/time/service/doctor/unit never re-checks the day ---------------


async def test_edit_change_insurance_on_blocked_day_ok():
    """The Tuesday visit exists; Tuesdays were blocked for Consulta afterwards. Changing only the
    convênio is not refused and the day is never re-checked."""
    doctors = _doctors(consulta_blocks=("tuesday",))
    ctx, calendars = _ctx()

    picked = await _step(
        fr.STEP_EDIT_INSURANCE, fr.LABEL_INSURANCE_PARTICULAR, doctors=doctors, ctx=ctx
    )
    assert picked.flow_step == fr.STEP_EDIT_CONFIRM
    assert _draft(picked).changed() == ["convênio"]

    done = await _step(
        fr.STEP_EDIT_CONFIRM,
        fr.LABEL_CONFIRM,
        doctors=doctors,
        ctx=ctx,
        draft=_draft(picked),
        appt=original_appointment(_draft(picked)),
    )
    assert done.appointment_edit is not None
    assert done.flow_step not in fr._EDIT_DAY_STEPS
    assert calendars[str(DOCTOR_A)].checked == []  # the day was never re-checked


# --- Changing the doctor -----------------------------------------------------------------


async def test_edit_doctor_list_marks_blocked_as_ocupado():
    doctors = _doctors(b_consulta_blocks=("tuesday",))
    ctx, _ = _ctx()

    result = await _step(fr.STEP_EDIT_MENU, ae.LABEL_EDIT_DOCTOR, doctors=doctors, ctx=ctx)

    rows = {row[0]: row for bubble in result.bubbles for row in bubble.rows}
    marks = {pid: rows[f"prof|{pid}"][2] for pid in (DOCTOR_A, DOCTOR_B)}
    assert "Livre" in marks[DOCTOR_A], marks
    assert "Ocupado" in marks[DOCTOR_B], marks


async def test_edit_new_doctor_blocking_the_day_for_the_service_reslots():
    doctors = _doctors(b_consulta_blocks=("tuesday",))
    ctx, _ = _ctx()

    result = await _step(
        fr.STEP_EDIT_DOCTOR, fr._professional_row_title("Dra. Ana Lima"), doctors=doctors, ctx=ctx
    )

    assert result.flow_step != fr.STEP_EDIT_CONFIRM
    assert result.flow_step in fr._EDIT_DAY_STEPS


# --- The AI proposes a day ---------------------------------------------------------------


async def test_edit_ai_proposed_blocked_day_asks_for_another_day():
    doctors = _doctors(consulta_blocks=("wednesday",))
    ctx, _ = _ctx()
    appt = appointment()
    draft = draft_for(appt)

    result = await ef.apply_ai_edit(
        conversation(draft, fr.STEP_EDIT_MENU),
        tenant(),
        {"day": WEDNESDAY, "time": "10:00"},
        [appt],
        doctors,
        ctx,
    )

    assert result.flow_step != fr.STEP_EDIT_CONFIRM
    assert result.flow_step in fr._EDIT_DAY_STEPS
