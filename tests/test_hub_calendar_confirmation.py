"""Hub agenda API: confirmation state on GET /events and PATCH status (TASK-032 R1, spec 4.4)."""

# Imported pytest fixtures are deliberately reused as injected parameters.
# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub import calendar as hub_calendar
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Appointment, AppointmentReminder, AppointmentStatus, Tenant
from secretaria.services.reminder_schedule import schedule_reminders
from secretaria.services.tenant_config import set_google_refresh_token
from tests._reminder_fixtures import (  # noqa: F401
    NOW,
    db,
    make_appointment,
    other_tenant,
    tenant,
)

CALENDAR = "/tenants/me/calendar"
LEGACY_EVENT_KEYS = {"id", "summary", "start", "end", "appointment_id"}
INSURANCE_KEYS = {"insurance", "insurance_plan", "deposit"}
NEW_KEYS = {"status", "confirmation_count", "display_state", "attention", "reminders"} | {
    # TASK-044 R7 (spec §5.C): what Editar/Remarcar pre-fills
    "professional_id",
    "professional_name",
    "service",
    "attendee_name",
    "phone",
    "patient_channel",
}


class _FakeCalendarService:
    events: list[dict] = []

    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    async def check_availability(self, start, end):
        return list(type(self).events)

    async def cancel_event(self, event_id: str) -> None:
        return None

    async def update_event(self, event_id, start, end) -> dict:
        return {"id": event_id}


@pytest.fixture(autouse=True)
def _override(db, tenant, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    monkeypatch.setattr(hub_calendar, "CalendarService", _FakeCalendarService)
    app.state.arq_pool = None
    _FakeCalendarService.events = []
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _connect(db, tenant) -> None:  # noqa: F811
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "fake-refresh-token")
        await session.commit()


def _wire(event_id: str) -> dict:
    return {
        "id": event_id,
        "summary": "Consulta",
        "start": "2026-10-07T12:00:00+00:00",
        "end": "2026-10-07T12:30:00+00:00",
    }


async def _events(client: AsyncClient):
    return await client.get(
        f"{CALENDAR}/events",
        params={"start": "2026-10-01T00:00:00Z", "end": "2026-10-31T00:00:00Z"},
    )


async def _booked(db, tenant, event_id="evt-1", **kw):  # noqa: F811
    """Appointment 6 days out (real clock for the PATCH tests) with its 3 rows."""
    start = datetime.now(UTC) + timedelta(days=6)
    appt = await make_appointment(db, tenant, start_at=start, google_event_id=event_id, **kw)
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        t = await session.get(Tenant, tenant.id)
        await schedule_reminders(session, a, t, now=datetime.now(UTC))
        await session.commit()
    return appt


async def _set(db, appt_id, **fields):  # noqa: F811
    async with db() as session:
        a = await session.get(Appointment, appt_id)
        for key, value in fields.items():
            setattr(a, key, value)
        await session.commit()


async def _warn_all(db, appt_id):  # noqa: F811
    async with db() as session:
        for r in await session.scalars(
            select(AppointmentReminder).where(AppointmentReminder.appointment_id == appt_id)
        ):
            r.status = "sent"
            r.warned_at = datetime.now(UTC)
            r.warn_kind = "unconfirmed"
        await session.commit()


# ----------------------------------------------------------------- GET /events


async def test_event_carries_status_count_state_and_reminder_summary(
    client: AsyncClient, db, tenant
):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    _FakeCalendarService.events = [_wire("evt-1")]

    body = (await _events(client)).json()[0]

    assert set(body) == LEGACY_EVENT_KEYS | INSURANCE_KEYS | NEW_KEYS
    assert body["appointment_id"] == str(appt.id)
    assert body["status"] == "scheduled"
    assert body["confirmation_count"] == 0
    assert body["display_state"] == "unconfirmed" and body["attention"] is False
    assert [r["kind"] for r in body["reminders"]] == ["custom", "day", "hour"]
    first = body["reminders"][0]
    assert set(first) == {
        "kind",
        "status",
        "due_at",
        "sent_at",
        "answered_at",
        "answer",
        "warned_at",
        "warn_kind",
    }
    assert first["status"] == "pending" and first["sent_at"] is None
    # aware UTC on the wire, even though SQLite hands back naive datetimes
    assert first["due_at"].endswith("Z")


@pytest.mark.parametrize(("count", "expected"), [(1, "confirmed"), (2, "confirmed_twice")])
async def test_confirmed_states(client: AsyncClient, db, tenant, count, expected):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _set(db, appt.id, confirmation_count=count, status=AppointmentStatus.CONFIRMED)
    _FakeCalendarService.events = [_wire("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["confirmation_count"] == count
    assert body["display_state"] == expected and body["attention"] is False
    assert body["status"] == "confirmed"


async def test_warned_and_unconfirmed_is_attention(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _warn_all(db, appt.id)
    _FakeCalendarService.events = [_wire("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["display_state"] == "attention" and body["attention"] is True


async def test_reminders_of_an_older_start_are_not_listed(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _set(db, appt.id, start_at=datetime.now(UTC) + timedelta(days=20))
    _FakeCalendarService.events = [_wire("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["reminders"] == []


async def test_event_without_a_local_appointment_has_null_confirmation_keys(
    client: AsyncClient, db, tenant
):  # noqa: F811
    await _connect(db, tenant)
    _FakeCalendarService.events = [_wire("typed-straight-into-google")]

    body = (await _events(client)).json()[0]

    assert body["appointment_id"] is None
    assert {k: body[k] for k in NEW_KEYS} == {k: None for k in NEW_KEYS}


async def test_another_clinics_appointment_never_leaks_its_state(
    client: AsyncClient, db, tenant, other_tenant
):  # noqa: F811
    await _connect(db, tenant)
    foreign = await _booked(db, other_tenant, event_id="shared-event")
    await _set(db, foreign.id, confirmation_count=2, status=AppointmentStatus.CONFIRMED)
    _FakeCalendarService.events = [_wire("shared-event")]

    body = (await _events(client)).json()[0]

    assert body["appointment_id"] is None
    assert {k: body[k] for k in NEW_KEYS} == {k: None for k in NEW_KEYS}


async def test_reminders_are_loaded_with_one_query_for_the_whole_page(
    client: AsyncClient, db, tenant
):  # noqa: F811
    await _connect(db, tenant)
    for n in range(4):
        await _booked(db, tenant, event_id=f"evt-{n}")
    _FakeCalendarService.events = [_wire(f"evt-{n}") for n in range(4)]
    statements: list[str] = []
    engine = db.kw["bind"].sync_engine

    def _on(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _on)
    try:
        assert len((await _events(client)).json()) == 4
    finally:
        event.remove(engine, "before_cursor_execute", _on)

    assert sum("FROM appointment_reminders" in s for s in statements) == 1


# ----------------------------------------------------------------- PATCH status


async def _patch(client: AsyncClient, appt_id, status_value: str):
    return await client.patch(
        f"{CALENDAR}/appointments/{appt_id}/status", json={"status": status_value}
    )


async def _reload(db, appt_id) -> Appointment:  # noqa: F811
    async with db() as session:
        return await session.get(Appointment, appt_id)


async def _statuses(db, appt_id) -> set[str]:  # noqa: F811
    async with db() as session:
        return {
            r.status
            for r in await session.scalars(
                select(AppointmentReminder).where(AppointmentReminder.appointment_id == appt_id)
            )
        }


async def test_patch_confirmed_counts_one_staff_confirmation_and_is_idempotent(
    client: AsyncClient, db, tenant
):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)

    first = await _patch(client, appt.id, "confirmed")
    again = await _patch(client, appt.id, "confirmed")

    assert first.status_code == 200 and again.status_code == 200
    body = again.json()
    assert body["status"] == "confirmed" and body["confirmation_count"] == 1
    a = await _reload(db, appt.id)
    assert a.confirmation_count == 1
    assert a.first_confirmed_at is not None and a.last_confirmed_at is not None


async def test_patch_confirmed_does_not_raise_a_patient_confirmed_twice_booking(
    client: AsyncClient, db, tenant
):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _set(db, appt.id, confirmation_count=2, status=AppointmentStatus.CONFIRMED)

    body = (await _patch(client, appt.id, "confirmed")).json()

    assert body["confirmation_count"] == 2


async def test_patch_back_to_scheduled_zeroes_the_counter(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _patch(client, appt.id, "confirmed")

    body = (await _patch(client, appt.id, "scheduled")).json()

    assert body["status"] == "scheduled" and body["confirmation_count"] == 0
    a = await _reload(db, appt.id)
    assert a.first_confirmed_at is None and a.last_confirmed_at is None


@pytest.mark.parametrize("terminal", ["cancelled", "attended"])
async def test_patch_to_a_terminal_status_cancels_the_pending_reminders(
    client: AsyncClient, db, tenant, terminal
):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    assert await _statuses(db, appt.id) == {"pending"}

    response = await _patch(client, appt.id, terminal)

    assert response.status_code == 200
    assert await _statuses(db, appt.id) == {"cancelled"}


async def test_patch_no_longer_resurrects_a_cancelled_booking(client: AsyncClient, db, tenant):  # noqa: F811
    """R7 (spec 2026-10-09 §3): confirmed/attended on a cancelled booking is 409 not_live."""
    await _connect(db, tenant)
    appt = await _booked(db, tenant, status=AppointmentStatus.CANCELLED)

    attended = await _patch(client, appt.id, "attended")
    confirmed = await _patch(client, appt.id, "confirmed")

    assert attended.status_code == confirmed.status_code == 409
    assert confirmed.json()["detail"]["code"] == "not_live"
    assert (await _reload(db, appt.id)).status == AppointmentStatus.CANCELLED

async def test_patch_on_another_clinics_appointment_is_404_and_changes_nothing(
    client: AsyncClient, db, tenant, other_tenant
):  # noqa: F811
    await _connect(db, tenant)
    foreign = await _booked(db, other_tenant)

    response = await _patch(client, foreign.id, "confirmed")

    assert response.status_code == 404
    assert (await _reload(db, foreign.id)).confirmation_count == 0
    assert await _statuses(db, foreign.id) == {"pending"}


async def test_existing_appointment_read_keys_are_unchanged(client: AsyncClient, db, tenant):  # noqa: F811
    await _connect(db, tenant)
    appt = await _booked(db, tenant)

    body = (await _patch(client, appt.id, "confirmed")).json()

    legacy = {
        "id",
        "tenant_id",
        "patient_id",
        "conversation_id",
        "google_event_id",
        "google_event_link",
        "appointment_type",
        "start_at",
        "end_at",
        "phone",
        "status",
        "created_at",
        "updated_at",
        "deposit_status",
        "deposit_outcome",
    }
    assert set(body) == legacy | {"confirmation_count", "patient_notice", "whatsapp_link"}


async def test_same_start_reschedule_only_exposes_fresh_generation(client: AsyncClient, db, tenant):
    from secretaria.services.reminder_schedule import reschedule_reminders

    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _warn_all(db, appt.id)
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        a.status = AppointmentStatus.RESCHEDULED
        await reschedule_reminders(session, a, await session.get(Tenant, tenant.id), now=NOW)
        await session.commit()
    _FakeCalendarService.events = [_wire("evt-1")]
    body = (await _events(client)).json()[0]
    assert body["display_state"] == "unconfirmed" and body["attention"] is False
    assert len(body["reminders"]) == 3
    assert {r["status"] for r in body["reminders"]} == {"pending"}
    assert all(r["warned_at"] is None for r in body["reminders"])


async def test_patch_no_show_after_the_start_cancels_the_pending_reminders(
    client: AsyncClient, db, tenant
):  # noqa: F811
    """R7: no_show is only accepted after the start (spec 2026-10-09 §1)."""
    await _connect(db, tenant)
    appt = await _booked(db, tenant)
    await _set(db, appt.id, start_at=datetime.now(UTC) - timedelta(minutes=5))
    assert await _statuses(db, appt.id) == {"pending"}

    response = await _patch(client, appt.id, "no_show")

    assert response.status_code == 200
    assert await _statuses(db, appt.id) == {"cancelled"}
