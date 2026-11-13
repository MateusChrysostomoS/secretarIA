# ruff: noqa: F811
"""The unit and its address in what the patient and the clinic READ (TASK-083 spec §8).

Reminders (every surface reads `reminder_sentence`) and the "consulta marcada" e-mails (patient,
clinic, doctor). A booking without a unit reads exactly as before.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.models import Appointment, AppointmentStatus, Unit
from secretaria.plugins import booking_notifications, professional_notification
from secretaria.services.email import _TEMPLATES, _SafeDict
from secretaria.services.reminder_text import (
    ReminderContent,
    load_reminder_content,
    reminder_sentence,
)
from tests._local_agenda import seed_tenant
from tests._reminder_fixtures import db  # noqa: F401 - fixture

START = datetime(2099, 1, 5, 13, 0, tzinfo=UTC)


def _content(**kw) -> ReminderContent:
    return ReminderContent(
        clinic_name="Clínica", timezone="America/Sao_Paulo", start_at=START,
        service_name="Consulta", doctor_name="Dra. Ana", **kw,
    )


def test_the_reminder_says_where_for_a_booking_at_a_unit_and_nothing_more_otherwise():
    plain = reminder_sentence(_content())
    assert plain == (
        "LEMBRE-SE: Sua consulta com Dra. Ana está marcada para o dia 05/01/2099 às 10:00 "
        "para Consulta."
    )
    at_unit = reminder_sentence(_content(unit_location="Centro — Rua A, 10"))
    assert at_unit == f"{plain} Local: Centro — Rua A, 10."


async def _unit(db, tenant, name="Centro", address="Rua A, 10") -> Unit:
    async with db() as session:
        unit = Unit(id=uuid4(), tenant_id=tenant.id, name=name, address=address)
        session.add(unit)
        await session.commit()
        return unit


def _appointment(tenant, unit_id=None) -> Appointment:
    return Appointment(
        id=uuid4(), tenant_id=tenant.id, google_event_id="evt", appointment_type="Consulta",
        start_at=START, end_at=START, status=AppointmentStatus.SCHEDULED, unit_id=unit_id,
    )


@pytest.mark.asyncio
async def test_the_reminder_content_reads_the_tenants_own_unit_only(db):
    tenant = await seed_tenant(db)
    other = await seed_tenant(db, name="Outra")
    unit = await _unit(db, tenant)
    foreign = await _unit(db, other, "Alheia")
    async with db() as session:
        own = await load_reminder_content(session, tenant, _appointment(tenant, unit.id))
        stray = await load_reminder_content(session, tenant, _appointment(tenant, foreign.id))
        none = await load_reminder_content(session, tenant, _appointment(tenant))
    assert own.unit_location == "Centro — Rua A, 10"
    assert stray.unit_location is None
    assert none.unit_location is None


@pytest.mark.asyncio
async def test_the_booking_e_mails_carry_the_unit_lines(db, monkeypatch):
    monkeypatch.setattr(booking_notifications, "async_session_factory", db)
    monkeypatch.setattr(professional_notification, "async_session_factory", db)
    tenant = await seed_tenant(db)
    unit = await _unit(db, tenant)
    booked = _appointment(tenant, unit.id)
    lines = "Unidade: Centro\nEndereço: Rua A, 10\n"

    assert await booking_notifications.booking_unit_line(tenant, booked) == lines
    assert await booking_notifications.booking_unit_line(tenant, _appointment(tenant)) == ""
    assert await professional_notification._unit_line(tenant.id, booked) == lines
    variables = booking_notifications._variables(
        tenant, None, booked, "", "", await booking_notifications.booking_unit_line(tenant, booked)
    )
    for template in (
        "appointment_booked_clinic",
        "appointment_booked_patient",
        "appointment_booked_professional",
    ):
        body = _TEMPLATES[template].body.format_map(_SafeDict(variables))
        assert lines in body, template


@pytest.mark.asyncio
async def test_the_patients_calendar_link_location_is_the_tenants_own_unit_only(db, monkeypatch):
    """Final wave F3: the e-mailed add-to-calendar link of a booking at a unit carries the
    unit's "name — address"; none for a booking without one, nor for a foreign unit id."""
    monkeypatch.setattr(booking_notifications, "async_session_factory", db)
    tenant = await seed_tenant(db)
    other = await seed_tenant(db, name="Outra")
    unit = await _unit(db, tenant)
    foreign = await _unit(db, other, "Alheia")
    location = booking_notifications.booking_unit_location
    assert await location(tenant, _appointment(tenant, unit.id)) == "Centro — Rua A, 10"
    assert await location(tenant, _appointment(tenant, foreign.id)) is None
    assert await location(tenant, _appointment(tenant)) is None


def test_a_sender_that_does_not_know_the_unit_line_renders_nothing_for_it():
    body = _TEMPLATES["appointment_booked_clinic"].body.format_map(
        _SafeDict(
            {
                "clinic_name": "Clínica", "appointment_id": "x", "service": "Consulta",
                "when": "05/01", "insurance_line": "", "professional_line": "",
                "agenda_line": "",
            }
        )
    )
    assert "{unit_line}" not in body and "Unidade:" not in body


def test_the_text_helpers_do_not_need_a_database():
    assert booking_notifications._variables(
        SimpleNamespace(clinic_name="C", timezone=None), None,
        SimpleNamespace(id=uuid4(), appointment_type=None, start_at=START), "",
    )["unit_line"] == ""
