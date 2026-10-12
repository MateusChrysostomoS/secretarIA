"""The warning tick: e-mail content, once-only, and the failure modes (TASK-032 R4, spec 4.4)."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from secretaria.core import database as core_database
from secretaria.models import Appointment, AppointmentStatus
from secretaria.workers import confirmation_warnings as cw
from tests._confirmation_warnings import add_warnable_row, set_contact_email
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, get_reminder, seed_world

CLINIC_EMAIL = "contato@clinica.example"
AGENDA = "https://app.exemplo/agenda"


class _AlertSpy:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, to_email: str, **kwargs) -> bool:
        self.calls.append((to_email, kwargs))
        return self.result


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(cw, "get_settings", lambda: SimpleNamespace(DOCTOR_AGENDA_URL=AGENDA))


@pytest.fixture
def alert(monkeypatch: pytest.MonkeyPatch) -> _AlertSpy:
    spy = _AlertSpy()
    monkeypatch.setattr(cw, "send_confirmation_warning_alert", spy)
    return spy


async def _world(db, **kwargs):  # noqa: F811
    world = await seed_world(db, professional_name="Dra. Ana", **kwargs)
    await set_contact_email(db, world.tenant.id, CLINIC_EMAIL)
    return world


async def test_the_warning_names_what_the_clinic_needs_and_nothing_clinical(db, alert):  # noqa: F811
    world = await _world(db, requirements=["jejum de 8 horas"])
    await add_warnable_row(db, world, kind="day")

    report = await cw.run_warning_tick(now=NOW)

    assert (report.candidates, report.claimed, report.emailed) == (1, 1, 1)
    [(to, kwargs)] = alert.calls
    assert to == CLINIC_EMAIL
    assert kwargs["clinic_name"] == "Clínica Olhar"
    assert kwargs["warn_kind"] == "unconfirmed"
    assert kwargs["patient_label"] == "Maria"
    assert kwargs["professional_name"] == "Dra. Ana"
    assert kwargs["service_name"] == "Consulta"
    assert kwargs["when_text"] == "08/10/2026 às 09:00"  # 12:00 UTC in America/Sao_Paulo
    assert kwargs["reminder_label"] == "lembrete de 1 dia antes"
    # start 2026-10-08 12:00 UTC = 09:00 in America/Sao_Paulo: the date is the clinic's day
    assert kwargs["agenda_link"] == f"{AGENDA}?consulta={world.appointment.id}&data=2026-10-08"
    assert "jejum" not in repr(kwargs)  # requirements are clinical-adjacent: never mailed


async def test_the_delivery_failed_variant_is_passed_through(db, alert):  # noqa: F811
    world = await _world(db)
    await add_warnable_row(db, world, kind="hour", status="failed", warn_kind="delivery_failed")

    await cw.run_warning_tick(now=NOW)

    assert alert.calls[0][1]["warn_kind"] == "delivery_failed"
    assert alert.calls[0][1]["reminder_label"] == "lembrete de 1 hora antes"


async def test_a_booking_for_someone_else_names_the_attendee_and_the_holder(db, alert):  # noqa: F811
    world = await _world(db, attendee_name="João")
    await add_warnable_row(db, world)

    await cw.run_warning_tick(now=NOW)

    assert alert.calls[0][1]["patient_label"] == "João (consulta marcada por Maria)"


async def test_a_row_warns_once_even_over_several_ticks(db, alert):  # noqa: F811
    world = await _world(db)
    rid = await add_warnable_row(db, world)

    await cw.run_warning_tick(now=NOW)
    await cw.run_warning_tick(now=NOW + timedelta(minutes=1))
    await cw.run_warning_tick(now=NOW + timedelta(minutes=2))

    assert len(alert.calls) == 1
    assert (await get_reminder(db, rid)).warned_at is not None


async def test_a_confirmation_before_the_tick_means_no_warning(db, alert):  # noqa: F811
    world = await _world(db)
    rid = await add_warnable_row(db, world)
    async with db() as session:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment.id)
            .values(confirmation_count=1, status=AppointmentStatus.CONFIRMED)
        )
        await session.commit()

    report = await cw.run_warning_tick(now=NOW)

    assert report.candidates == 0 and alert.calls == []
    assert (await get_reminder(db, rid)).warned_at is None


async def test_a_clinic_with_the_switch_off_gets_nothing(db, alert):  # noqa: F811
    world = await _world(db, v2=False)
    rid = await add_warnable_row(db, world)

    report = await cw.run_warning_tick(now=NOW)

    assert report.candidates == 0 and alert.calls == []
    assert (await get_reminder(db, rid)).warned_at is None


async def test_without_an_alert_address_the_row_is_still_marked_but_nothing_is_sent(db, alert):  # noqa: F811
    world = await _world(db)
    await set_contact_email(db, world.tenant.id, "   ")
    rid = await add_warnable_row(db, world)

    report = await cw.run_warning_tick(now=NOW)
    again = await cw.run_warning_tick(now=NOW + timedelta(minutes=1))

    assert alert.calls == []
    assert (report.claimed, report.no_address) == (1, 1)
    assert again.candidates == 0  # no retry loop
    assert (await get_reminder(db, rid)).warned_at is not None  # the agenda still turns red


async def test_a_failed_send_is_not_retried_and_keeps_the_red_state(db, alert):  # noqa: F811
    alert.result = False
    world = await _world(db)
    rid = await add_warnable_row(db, world)

    report = await cw.run_warning_tick(now=NOW)
    again = await cw.run_warning_tick(now=NOW + timedelta(minutes=1))

    assert (report.emailed, report.email_failed) == (0, 1)
    assert again.candidates == 0 and len(alert.calls) == 1
    assert (await get_reminder(db, rid)).warned_at is not None


async def test_a_burst_after_an_outage_sends_one_email_for_the_appointment(db, alert):  # noqa: F811
    world = await _world(db)
    ids = [
        await add_warnable_row(db, world, kind="custom", warn_due_at=NOW - timedelta(hours=40)),
        await add_warnable_row(db, world, kind="day", warn_due_at=NOW - timedelta(hours=20)),
        await add_warnable_row(db, world, kind="hour", warn_due_at=NOW - timedelta(minutes=2)),
    ]

    report = await cw.run_warning_tick(now=NOW)

    assert (report.claimed, report.emailed) == (3, 1)
    assert alert.calls[0][1]["reminder_label"] == "lembrete de 1 hora antes"  # the latest
    for rid in ids:
        assert (await get_reminder(db, rid)).warned_at is not None


async def test_an_error_on_one_appointment_does_not_stop_the_others(db, alert, monkeypatch):  # noqa: F811
    first = await _world(db)
    second = await _world(db, phone_number_id="pnid-2", wa_id="5511900000002")
    await add_warnable_row(db, first)
    await add_warnable_row(db, second)
    real = cw.claim_warning
    seen = {"n": 0}

    async def _flaky(session, candidate, now):
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("db hiccup")
        return await real(session, candidate, now)

    monkeypatch.setattr(cw, "claim_warning", _flaky)

    report = await cw.run_warning_tick(now=NOW)

    assert report.errors == 1 and report.emailed == 1


def test_the_agenda_link_handles_an_existing_query_and_an_unset_url(monkeypatch):
    monkeypatch.setattr(cw, "get_settings", lambda: SimpleNamespace(DOCTOR_AGENDA_URL=""))
    assert cw._agenda_link("abc", "2026-10-08") is None
    monkeypatch.setattr(
        cw, "get_settings", lambda: SimpleNamespace(DOCTOR_AGENDA_URL="https://x.y/agenda?v=1")
    )
    assert (
        cw._agenda_link("abc", "2026-10-08")
        == "https://x.y/agenda?v=1&consulta=abc&data=2026-10-08"
    )
    assert (
        cw._agenda_link("abc", None) == "https://x.y/agenda?v=1&consulta=abc"
    )  # no start: no date


async def test_the_cron_entry_point_runs_a_tick(db, alert, monkeypatch):  # noqa: F811
    world = await _world(db)
    await add_warnable_row(db, world)
    real_now = NOW + timedelta(seconds=1)
    monkeypatch.setattr(cw, "datetime", SimpleNamespace(now=lambda tz=None: real_now), raising=True)

    await cw.process_confirmation_warnings({})

    assert len(alert.calls) == 1
