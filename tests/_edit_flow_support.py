"""Shared fakes for the TASK-032 R6 edit-flow tests (router level, no database)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from secretaria.models import FlowState
from secretaria.services import appointment_edit as ae
from secretaria.services.calendar import CalendarUnavailableError
from tests.test_flow_router import _FakeCalendar

TZ = ZoneInfo("America/Sao_Paulo")
DOCTOR_A = uuid4()
DOCTOR_B = uuid4()
# Tuesday 2030-10-15, 15:20 in São Paulo (UTC-3).
START_UTC = datetime(2030, 10, 15, 18, 20, tzinfo=UTC)


class EditCalendar(_FakeCalendar):
    """The router suite's fake agenda plus the two methods the edit flow needs."""

    def __init__(self, *, free: bool = True, fail_update: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.free = free
        self.fail_update = fail_update
        self.checked: list[tuple] = []
        self.detail_updates: list[tuple] = []

    async def is_slot_free(self, start, end, *, ignore_event_id=None):
        if self._unavailable:
            raise CalendarUnavailableError("down")
        self.checked.append((start, end, ignore_event_id))
        return self.free

    async def update_event_details(self, event_id, start, end, summary, description=""):
        if self._unavailable or self.fail_update:
            raise CalendarUnavailableError("down")
        self.detail_updates.append((event_id, start, end, summary, description))
        return {"id": event_id}

    async def create_event(self, start, end, summary, description=""):
        if self.fail_update:
            raise CalendarUnavailableError("down")
        return await super().create_event(start, end, summary, description)


def slots(*hours: str, day: str = "2030-10-15") -> list[dict]:
    return [{"start": f"{day}T{hour}", "end": f"{day}T{hour}", "label": hour} for hour in hours]


def tenant(**kwargs):
    base = dict(
        initial_flows={},
        appointment_types=[{"name": "Consulta", "duration_min": 40, "is_active": True}],
        appointment_duration_min=40,
        business_hours={},
        timezone="America/Sao_Paulo",
        address="Rua das Flores, 10",
        clinic_name="Clínica Olhar",
        insurance_plans=[{"id": "p1", "name": "Unimed"}, {"id": "p2", "name": "Amil"}],
        insurance_mode="clinic_with_exceptions",
        insurance_accepted_by={str(DOCTOR_A): ["p1"], str(DOCTOR_B): ["p1", "p2"]},
        collect_insurance=True,
        pix_reschedule_limit=2,
        reminders_v2_enabled=True,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def professionals():
    return [
        SimpleNamespace(
            id=DOCTOR_A,
            name="Dr. Diogo Raposo",
            specialty="Cardiologia",
            about=None,
            context_doctor_message=None,
            appointment_types=[
                {
                    "name": "Consulta",
                    "duration_min": 40,
                    "is_active": True,
                    "requirements": ["Trazer exames anteriores"],
                },
                {"name": "Retorno", "duration_min": 20, "is_active": True},
            ],
            business_hours=None,
        ),
        SimpleNamespace(
            id=DOCTOR_B,
            name="Dra. Ana Lima",
            specialty="Oftalmologia",
            about=None,
            context_doctor_message=None,
            appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
            business_hours=None,
        ),
    ]


def appointment(**kwargs) -> dict:
    base = {
        "id": str(uuid4()),
        "google_event_id": "evt-old",
        "appointment_type": "Consulta",
        "start_at": START_UTC,
        "end_at": START_UTC + timedelta(minutes=40),
        "professional_id": str(DOCTOR_A),
        "insurance": "Unimed",
        "attendee_name": None,
    }
    base.update(kwargs)
    return base


def draft_for(appt: dict | None = None) -> ae.EditDraft:
    return ae.EditDraft.from_appointment(appt or appointment(), TZ)


def original_appointment(draft: ae.EditDraft) -> dict:
    """The original appointment of a supplied draft, including its stable id."""
    return appointment(
        id=draft.appointment_id,
        appointment_type=draft.original["service"],
        professional_id=draft.original["professional_id"],
        start_at=datetime.fromisoformat(draft.original["start_at"])
        .replace(tzinfo=TZ)
        .astimezone(UTC),
        end_at=datetime.fromisoformat(draft.original["end_at"]).replace(tzinfo=TZ).astimezone(UTC),
        insurance=draft.original["insurance"],
        attendee_name=draft.original["attendee_name"],
    )


def conversation(draft: ae.EditDraft, step: str, **kwargs):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=step,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=(
            UUID(draft.current["professional_id"]) if draft.current["professional_id"] else None
        ),
        flow_selected_insurance=None,
        flow_managing_appointment_id=UUID(draft.appointment_id),
        flow_attendee_name=None,
        flow_draft=None,
        flow_replaces_appointment_id=None,
        flow_edit_draft=draft.to_json(),
        patient_id=uuid4(),
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def context(calendar=None, *, calendars=None, **kwargs) -> ae.EditContext:
    cals = calendars if calendars is not None else {str(DOCTOR_A): calendar or EditCalendar()}
    return ae.EditContext(calendars=cals, **kwargs)
