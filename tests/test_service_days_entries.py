"""TASK-087 review P1: the patient entries that build the doctor's snapshot themselves read the
UNIT's blocked weekdays, not just the base list.

Reminder "Remarcar" (`apptresched`), the doctor-cancelled rebooking buttons (`rebooksame`) and
the AI hand-backs that resume the button flow (`start_guided_booking`, the v1 booking draft) used
to hand the router a snapshot without `units` / `service_unit_terms` (or the ORM rows), so
`flow_units.blocked_weekdays_of` silently fell back to the base list: a unit's own block was
offered, and a unit's own "nothing blocked" ([]) hid the base days. They now get the router-shaped
roster (`_flow_professionals` + `attach_unit_options`), the one `route()` reads.

Real worker entry points on an in-memory DB (units, links and per-unit terms seeded as rows), the
native agenda behind them (open every day 08-18), the dispatched bubbles captured.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import json  # noqa: E402
from datetime import UTC, date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai.formatter import SlotsBubble  # noqa: E402
from secretaria.ai.graph import (  # noqa: E402
    BOOKING_DRAFT_SENTINEL_PREFIX,
    START_GUIDED_BOOKING_SENTINEL_PREFIX,
)
from secretaria.core import database as core_database  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    Professional,
    ProfessionalServiceUnitTerms,
    ProfessionalUnit,
    Service,
    Tenant,
    Unit,
)
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.attendee import ATTENDEE_SELF  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from tests._local_agenda import WEEK_HOURS, agenda, wire_local_agenda  # noqa: E402
from tests._patching import workers_ns  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

TZ = ZoneInfo("America/Sao_Paulo")
MONDAY = 0


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    wire_local_agenda(monkeypatch, db)

    async def _token(session, tenant_id):
        return "waba-token"

    monkeypatch.setattr(workers_ns, "get_waba_token", _token)


@pytest.fixture
def bubbles(monkeypatch):
    captured: list = []

    async def _dispatch(reply, sent, tenant=None, waba_token=None):
        captured.extend(sent)
        return len(sent)

    monkeypatch.setattr(workers_ns, "_dispatch_bubbles", _dispatch)
    return captured


@pytest.fixture
def native_agenda(monkeypatch):
    """Every entry's agenda is the clinic's native one (no Google, no credentials)."""

    async def _calendar(session, tenant, target):
        return agenda(tenant.id)

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _calendar)


def _next_tuesday_10() -> datetime:
    first = datetime.now(TZ).date() + timedelta(days=2)
    day = first + timedelta(days=(1 - first.weekday()) % 7)
    return datetime(day.year, day.month, day.day, 10, 0, tzinfo=TZ).astimezone(UTC)


async def _clinic(
    db,  # noqa: F811
    *,
    base: list[str],
    own: dict[str, list[str]],
    units: tuple[str, ...] = ("Centro", "Norte"),
    appointment_unit: str | None = "Centro",
    status: AppointmentStatus = AppointmentStatus.SCHEDULED,
    colleague: bool = False,
):
    """Dra. Ana does Cirurgia (base `base`) at `units`; `own` = each unit's own list (a
    `professional_service_unit_terms` row). One patient (pra-quem answered), one Cirurgia on a
    Tuesday at `appointment_unit`. `colleague` adds Dr. Bruno (no units) for a multi clinic."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
            timezone="America/Sao_Paulo",
            business_hours=WEEK_HOURS,
            initial_flows={},
            reminders_v2_enabled=False,
        )
        session.add(tenant)
        await session.flush()
        service = Service(
            id=uuid4(), tenant_id=tenant.id, name="Cirurgia", normalized_name="cirurgia"
        )
        entry = {
            "name": "Cirurgia",
            "service_id": str(service.id),
            "duration_min": 30,
            "is_active": True,
            "blocked_weekdays": base,
        }
        ana = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            business_hours=WEEK_HOURS,
            appointment_types=[entry],
        )
        rows = {name: Unit(id=uuid4(), tenant_id=tenant.id, name=name) for name in units}
        session.add_all([service, ana, *rows.values()])
        if colleague:
            session.add(
                Professional(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    name="Dr. Bruno",
                    is_active=True,
                    business_hours=WEEK_HOURS,
                    appointment_types=[{**entry, "blocked_weekdays": []}],
                )
            )
        await session.flush()
        for order, unit in enumerate(rows.values()):
            session.add(
                ProfessionalUnit(
                    professional_id=ana.id, unit_id=unit.id, is_active=True, sort_order=order
                )
            )
        for name, blocked in own.items():
            session.add(
                ProfessionalServiceUnitTerms(
                    tenant_id=tenant.id,
                    professional_id=ana.id,
                    service_id=service.id,
                    unit_id=rows[name].id,
                    blocked_weekdays=blocked,
                )
            )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, flow_attendee_name=ATTENDEE_SELF
        )
        start = _next_tuesday_10()
        appointment = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id,
            professional_id=ana.id,
            unit_id=rows[appointment_unit].id if appointment_unit else None,
            google_event_id="",
            appointment_type="Cirurgia",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            status=status,
        )
        session.add_all([conversation, appointment])
        await session.commit()
        for obj in (tenant, ana, conversation, appointment):
            await session.refresh(obj)
    return SimpleNamespace(
        tenant=tenant, ana=ana, conversation=conversation, appointment=appointment
    )


def _reply(clinic) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=clinic.conversation.id, patient_ref="5511999999999", inbound_body=""
    )


def _weekdays(captured: list) -> set[int]:
    """The weekdays of the day picker the entry sent (its `day|<iso>|<page>` rows)."""
    [picker] = [b for b in captured if isinstance(b, SlotsBubble)]
    days = [
        date.fromisoformat(row[0].split("|")[1])
        for row in picker.rows
        if row[0].startswith(fr.ROW_DAY_PREFIX)
    ]
    assert days, "the day picker offered no day at all"
    return {day.weekday() for day in days}


# --------------------------------------------------------------------------- reminder "Remarcar"


async def test_apptresched_uses_the_units_own_block(db, bubbles, native_agenda):  # noqa: F811
    """The consultation is at Centro, which blocks Monday on its own (base: nothing)."""
    clinic = await _clinic(db, base=[], own={"Centro": ["monday"]})

    await tasks._handle_action_button(_reply(clinic), "apptresched", str(clinic.appointment.id))

    assert MONDAY not in _weekdays(bubbles)


async def test_apptresched_honours_the_units_own_empty_list(db, bubbles, native_agenda):  # noqa: F811
    """The base blocks Monday, Centro's own list blocks nothing: Monday is offered there."""
    clinic = await _clinic(db, base=["monday"], own={"Centro": []})

    await tasks._handle_action_button(_reply(clinic), "apptresched", str(clinic.appointment.id))

    assert MONDAY in _weekdays(bubbles)


# --------------------------------------------------------------------------- doctor cancelled


async def test_rebooksame_uses_the_units_own_blocks(db, bubbles, native_agenda):  # noqa: F811
    """No unit chosen yet (R1): both units block Monday on their own lists -> no Monday."""
    clinic = await _clinic(
        db,
        base=[],
        own={"Centro": ["monday"], "Norte": ["monday"]},
        status=AppointmentStatus.CANCELLED,
    )

    await tasks._handle_action_button(_reply(clinic), "rebooksame", str(clinic.appointment.id))

    assert MONDAY not in _weekdays(bubbles)


async def test_rebooksame_keeps_a_day_one_unit_frees(db, bubbles, native_agenda):  # noqa: F811
    """The base blocks Monday; Centro's own list frees it -> Monday stays (R1)."""
    clinic = await _clinic(
        db, base=["monday"], own={"Centro": []}, status=AppointmentStatus.CANCELLED
    )

    await tasks._handle_action_button(_reply(clinic), "rebooksame", str(clinic.appointment.id))

    assert MONDAY in _weekdays(bubbles)


# --------------------------------------------------------------------------- AI hand-backs


async def test_start_guided_booking_uses_the_units_own_block(db, bubbles, native_agenda):  # noqa: F811
    """A sole doctor at ONE unit (used silently) that blocks Monday on its own list."""
    clinic = await _clinic(db, base=[], own={"Centro": ["monday"]}, units=("Centro",))

    await tasks._handle_start_guided_booking(
        _reply(clinic),
        f"{START_GUIDED_BOOKING_SENTINEL_PREFIX}Cirurgia",
        clinic.tenant,
        [],
        "5511999999999",
    )

    assert MONDAY not in _weekdays(bubbles)


async def test_legacy_booking_draft_uses_the_units_own_block(db, bubbles, native_agenda):  # noqa: F811
    """The v1 draft (switch off, pra-quem answered) on a multi clinic, doctor named."""
    clinic = await _clinic(
        db, base=[], own={"Centro": ["monday"]}, units=("Centro",), colleague=True
    )

    await tasks._handle_set_booking_draft(
        _reply(clinic),
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps({"t": "Cirurgia", "p": str(clinic.ana.id)}),
        clinic.tenant,
        None,
        [],
        "5511999999999",
    )

    assert MONDAY not in _weekdays(bubbles)
