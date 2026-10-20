# ruff: noqa: F811
"""The console writes, reads and acts on each doctor's OWN agenda (api/hub/calendar.py).

Owner's decision 2026-10-19: in a clinic with one Google agenda per doctor, a
consultation or block that has a doctor lives on THAT doctor's agenda - the only one his
patients' availability reads - and only what has no doctor stays on the clinic's. GET
/events lists the clinic's agenda together with every active doctor's; cancel, release
and reschedule act where the event really is, looking on the clinic's agenda for a
LEGACY row (origin never recorded) whose doctor's agenda does not hold it.

The fake is Google itself, not CalendarService: calendars hold events, a refresh token
reaches only its own account's calendars ("primary" is that account's primary), and
anything else answers 404 - so the real CalendarService methods, the real
`resolve_professional_calendar` and both `google_calendar_mode` values run end to end.
No network call is ever made.
"""

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from googleapiclient.errors import HttpError
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Appointment, AppointmentStatus, Patient, Professional, Tenant
from secretaria.services import appointment_release
from secretaria.services.agenda_visibility import AgendaViewer
from secretaria.services.calendar import CalendarService, CalendarUnavailableError
from secretaria.services.tenant_config import (
    resolve_professional_calendar,
    set_google_refresh_token,
    set_professional_google_refresh_token,
)
from tests._agenda_viewer import view_as
from tests._reminder_fixtures import db  # noqa: F401

CALENDAR = "/tenants/me/calendar"
DAY = "2027-03-15"


def _http_error(status: int) -> HttpError:
    return HttpError(resp=SimpleNamespace(status=status, reason="fake"), content=b"{}")


def _at(hour: int, minute: int = 0) -> str:
    return f"{DAY}T{hour:02d}:{minute:02d}:00+00:00"


def _when(hour: int) -> datetime:
    return datetime.fromisoformat(_at(hour))


class _Request:
    def __init__(self, fn) -> None:
        self._fn = fn

    def execute(self):
        return self._fn()


class _FakeGoogle:
    """Google Calendar as the console sees it, per account and per calendar."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.calendars: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, str, str | None]] = []
        self.down: set[str] = set()
        self.barrier: threading.Barrier | None = None
        self._lock = threading.Lock()

    def add_account(self, token: str, primary: str, *secondaries: str) -> None:
        self.accounts[token] = {"primary": primary, "calendars": {primary, *secondaries}}
        for calendar_id in (primary, *secondaries):
            self.calendars.setdefault(calendar_id, {})

    def put(self, calendar_id: str, event_id: str, start: str, end: str) -> None:
        self.calendars[calendar_id][event_id] = {
            "id": event_id,
            "summary": "Consulta",
            "status": "confirmed",
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }

    def ids(self, calendar_id: str) -> set[str]:
        return set(self.calendars[calendar_id])

    def where(self, method: str) -> list[str]:
        return [calendar for name, calendar, _ in self.calls if name == method]

    def _reach(self, token: str, calendar_id: str) -> str:
        if token in self.down:
            raise _http_error(503)
        account = self.accounts[token]
        real = account["primary"] if calendar_id == "primary" else calendar_id
        if real not in account["calendars"]:
            raise _http_error(404)
        return real

    def _record(self, call: tuple) -> None:
        with self._lock:
            self.calls.append(call)

    def client(self, token: str):
        google = self

        class _Events:
            def list(self, calendarId, timeMin, timeMax, **_kw):  # noqa: N803
                def run():
                    real = google._reach(token, calendarId)
                    google._record(("list", real, None))
                    if google.barrier is not None:
                        google.barrier.wait()
                    lo, hi = datetime.fromisoformat(timeMin), datetime.fromisoformat(timeMax)
                    items = [
                        e
                        for e in google.calendars[real].values()
                        if datetime.fromisoformat(e["start"]["dateTime"]) < hi
                        and datetime.fromisoformat(e["end"]["dateTime"]) > lo
                    ]
                    items.sort(key=lambda e: e["start"]["dateTime"])
                    return {"items": items}

                return _Request(run)

            def insert(self, calendarId, body):  # noqa: N803
                def run():
                    real = google._reach(token, calendarId)
                    event_id = f"evt-new-{real}-{len(google.calendars[real])}"
                    google._record(("insert", real, event_id))
                    google.put(real, event_id, body["start"]["dateTime"], body["end"]["dateTime"])
                    return {**google.calendars[real][event_id], "htmlLink": f"https://g/{real}"}

                return _Request(run)

            def _event(self, method, calendarId, eventId):  # noqa: N803
                real = google._reach(token, calendarId)
                google._record((method, real, eventId))
                if eventId not in google.calendars[real]:
                    raise _http_error(404)
                return real

            def delete(self, calendarId, eventId):  # noqa: N803
                def run():
                    real = self._event("delete", calendarId, eventId)
                    del google.calendars[real][eventId]

                return _Request(run)

            def patch(self, calendarId, eventId, body):  # noqa: N803
                def run():
                    real = self._event("patch", calendarId, eventId)
                    event = google.calendars[real][eventId]
                    event["start"] = {"dateTime": body["start"]["dateTime"]}
                    event["end"] = {"dateTime": body["end"]["dateTime"]}
                    return dict(event)

                return _Request(run)

        return SimpleNamespace(events=lambda: _Events())


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> _FakeGoogle:
    fake = _FakeGoogle()
    monkeypatch.setattr(
        CalendarService, "_build_service", lambda self: fake.client(self._refresh_token_override)
    )
    return fake


@pytest.fixture
def clinic():
    """Filled by `_shared_account_clinic` / `_per_professional_clinic`."""
    return SimpleNamespace(tenant=None, ana=None, bia=None)


@pytest.fixture(autouse=True)
def _override(db, clinic):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, clinic.tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    app.state.arq_pool = None
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _make_clinic(
    db, clinic, *, mode: str, ana_calendar, bia_calendar, ana_token=None, connected=True
):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=None,
            google_calendar_id="primary",
            google_calendar_mode=mode,
        )
        session.add(tenant)
        await session.flush()
        ana = Professional(
            id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", google_calendar_id=ana_calendar
        )
        bia = Professional(
            id=uuid4(), tenant_id=tenant.id, name="Dra. Bia", google_calendar_id=bia_calendar
        )
        session.add_all([ana, bia])
        await session.flush()
        if connected:
            await set_google_refresh_token(session, tenant.id, "tok-clinic")
        if ana_token:
            await set_professional_google_refresh_token(session, ana.id, ana_token)
        await session.commit()
    clinic.tenant, clinic.ana, clinic.bia = tenant, ana, bia


async def _shared_account_clinic(db, clinic, google: _FakeGoogle, **kwargs) -> None:
    """One clinic account; each doctor has a secondary calendar inside it."""
    google.add_account("tok-clinic", "cal-clinic", "cal-ana", "cal-bia")
    await _make_clinic(
        db, clinic, mode="shared_account", ana_calendar="cal-ana", bia_calendar="cal-bia", **kwargs
    )


async def _per_professional_clinic(db, clinic, google: _FakeGoogle) -> None:
    """Ana connected her OWN Google ("primary" of her account); Bia connected nothing,
    so she resolves to exactly the clinic's agenda."""
    google.add_account("tok-clinic", "cal-clinic")
    google.add_account("tok-ana", "cal-ana-own")
    await _make_clinic(
        db,
        clinic,
        mode="per_professional",
        ana_calendar=None,
        bia_calendar=None,
        ana_token="tok-ana",
    )


async def _appointment(
    db, clinic, event_id: str, professional=None, *, hour: int = 10, source=None, patient=False
) -> Appointment:
    """A row; `source` None is a LEGACY row (origin never recorded, e.g. a Portal booking)."""
    start = _when(hour)
    async with db() as session:
        patient_id = None
        if patient:
            row = Patient(id=uuid4(), tenant_id=clinic.tenant.id, wa_id="5511999990000")
            session.add(row)
            await session.flush()
            patient_id = row.id
        appt = Appointment(
            id=uuid4(),
            tenant_id=clinic.tenant.id,
            professional_id=professional.id if professional is not None else None,
            google_calendar_source=source,
            patient_id=patient_id,
            google_event_id=event_id,
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            phone=None,
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appt)
        await session.commit()
        return appt


async def _row(db, appt_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, UUID(str(appt_id)))


def _window(hour: int = 10) -> dict:
    return {"start": _at(hour), "end": _at(hour, 30)}


async def _cancel(client: AsyncClient, appt: Appointment):
    return await client.post(f"{CALENDAR}/appointments/{appt.id}/cancel", json={"confirm": True})


async def _reschedule(client: AsyncClient, appt: Appointment, hour: int = 15):
    return await client.post(
        f"{CALENDAR}/appointments/{appt.id}/reschedule",
        json={"new_start": _at(hour), "new_end": _at(hour, 30)},
    )


async def _events(client: AsyncClient, **params):
    return await client.get(
        f"{CALENDAR}/events",
        params={"start": f"{DAY}T00:00:00Z", "end": "2027-03-16T00:00:00Z", **params},
    )


# ---------------------------------------------------------------------------
# Create / block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["appointments", "blocks"])
async def test_a_doctors_consultation_or_block_is_written_on_her_own_agenda(
    client: AsyncClient, db, clinic, google, path
):
    """The double booking: written on the clinic's agenda, the slot stayed free for
    Ana's patients, whose availability reads only HER agenda."""
    await _shared_account_clinic(db, clinic, google)

    resp = await client.post(
        f"{CALENDAR}/{path}",
        json={**_window(), "summary": "Consulta", "professional_id": str(clinic.ana.id)},
    )

    assert resp.status_code == 201, resp.text
    row = await _row(db, resp.json()["id"])
    assert row.professional_id == clinic.ana.id
    assert row.google_calendar_source == "professional"
    assert google.ids("cal-ana") == {row.google_event_id}
    assert google.ids("cal-clinic") == set()
    async with db() as session:
        tenant = await session.get(Tenant, clinic.tenant.id)
        ana = await session.get(Professional, clinic.ana.id)
        agenda = await resolve_professional_calendar(session, tenant, ana)
    busy = await agenda.check_availability(_when(10), _when(11))
    assert [e["id"] for e in busy] == [row.google_event_id]  # Ana's patients see it busy


async def test_a_doctor_who_connected_her_own_google_gets_it_there(
    client: AsyncClient, db, clinic, google
):
    await _per_professional_clinic(db, clinic, google)

    resp = await client.post(
        f"{CALENDAR}/appointments",
        json={**_window(), "summary": "Consulta", "professional_id": str(clinic.ana.id)},
    )

    assert resp.status_code == 201, resp.text
    assert google.ids("cal-ana-own") == {resp.json()["google_event_id"]}
    assert google.ids("cal-clinic") == set()


@pytest.mark.parametrize("path", ["appointments", "blocks"])
async def test_without_a_doctor_it_stays_on_the_clinics_agenda(
    client: AsyncClient, db, clinic, google, path
):
    await _shared_account_clinic(db, clinic, google)

    resp = await client.post(f"{CALENDAR}/{path}", json={**_window(), "summary": "Consulta"})

    assert resp.status_code == 201, resp.text
    row = await _row(db, resp.json()["id"])
    assert row.professional_id is None and row.google_calendar_source == "clinic"
    assert google.ids("cal-clinic") == {row.google_event_id}
    assert google.ids("cal-ana") == set() and google.ids("cal-bia") == set()


async def test_a_doctors_agenda_without_google_is_the_secretarias_never_the_env_token(
    client: AsyncClient, db, clinic, google, monkeypatch
):
    """Native agenda (2026-10-20): no more 422. With no credential the doctor's agenda is
    the secretarIA's own - never a Google agenda built on the env fallback token."""
    from secretaria.models.calendar_event import CalendarEvent
    from secretaria.services import local_calendar

    monkeypatch.setattr(local_calendar, "async_session_factory", db)
    await _shared_account_clinic(db, clinic, google, connected=False)

    resp = await client.post(
        f"{CALENDAR}/appointments",
        json={**_window(), "summary": "Consulta", "professional_id": str(clinic.ana.id)},
    )

    assert resp.status_code == 201, resp.text
    assert google.calls == []
    async with db() as session:
        event = await session.get(CalendarEvent, resp.json()["google_event_id"])
    assert event is not None and event.professional_id == clinic.ana.id


async def test_a_consultation_created_for_a_doctor_is_cancelled_on_her_agenda(
    client: AsyncClient, db, clinic, google
):
    await _shared_account_clinic(db, clinic, google)
    created = await client.post(
        f"{CALENDAR}/appointments",
        json={**_window(), "summary": "Consulta", "professional_id": str(clinic.ana.id)},
    )

    resp = await client.post(
        f"{CALENDAR}/appointments/{created.json()['id']}/cancel", json={"confirm": True}
    )

    assert resp.status_code == 200
    assert google.ids("cal-ana") == set()
    assert google.where("delete") == ["cal-ana"]


# ---------------------------------------------------------------------------
# Cancel / release
# ---------------------------------------------------------------------------


async def test_cancel_deletes_on_the_owners_agenda_not_the_clinics(
    client: AsyncClient, db, clinic, google
):
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-ana", "evt-a", _at(10), _at(10, 30))
    google.put("cal-clinic", "evt-clinic", _at(10), _at(10, 30))
    appt = await _appointment(db, clinic, "evt-a", clinic.ana)

    resp = await _cancel(client, appt)

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert google.ids("cal-ana") == set()
    assert google.ids("cal-clinic") == {"evt-clinic"}
    assert google.where("delete") == ["cal-ana"]


async def test_cancel_falls_back_to_the_clinic_for_a_misplaced_legacy_event(
    client: AsyncClient, db, clinic, google
):
    """Portal bookings written before 75b4a63 name the doctor but sit on the clinic's
    agenda: the owner's 404 used to be reported as success, leaving the slot booked."""
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-clinic", "evt-legacy", _at(10), _at(10, 30))
    appt = await _appointment(db, clinic, "evt-legacy", clinic.ana)

    resp = await _cancel(client, appt)

    assert resp.status_code == 200
    assert google.ids("cal-clinic") == set()
    assert google.where("delete") == ["cal-ana", "cal-clinic"]
    assert (await _row(db, appt.id)).status == AppointmentStatus.CANCELLED


async def test_a_recorded_doctor_origin_is_never_second_guessed(
    client: AsyncClient, db, clinic, google
):
    """Only a row without a recorded origin is looked for on the clinic's agenda."""
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-clinic", "evt-x", _at(10), _at(10, 30))
    appt = await _appointment(db, clinic, "evt-x", clinic.ana, source="professional")

    resp = await _cancel(client, appt)

    assert resp.status_code == 200
    assert google.where("delete") == ["cal-ana"]
    assert google.ids("cal-clinic") == {"evt-x"}


async def test_a_hub_row_written_on_the_clinics_agenda_before_is_cancelled_there(
    client: AsyncClient, db, clinic, google
):
    """Explicit "clinic" origin with a doctor: what the console wrote until 2026-10-19."""
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-clinic", "evt-c", _at(10), _at(10, 30))
    appt = await _appointment(db, clinic, "evt-c", clinic.ana, source="clinic")

    resp = await _cancel(client, appt)

    assert resp.status_code == 200
    assert google.where("delete") == ["cal-clinic"]


async def test_cancel_of_an_event_on_no_agenda_still_cancels_the_consultation(
    client: AsyncClient, db, clinic, google
):
    """Deleted straight in Google: the end state (no event, row cancelled) holds."""
    await _shared_account_clinic(db, clinic, google)
    appt = await _appointment(db, clinic, "evt-gone", clinic.ana)

    resp = await _cancel(client, appt)

    assert resp.status_code == 200
    assert (await _row(db, appt.id)).status == AppointmentStatus.CANCELLED
    assert google.where("delete") == ["cal-ana", "cal-clinic"]


async def test_cancel_reaches_a_doctors_own_google_account(client: AsyncClient, db, clinic, google):
    """per_professional: "primary" under Ana's token is HER agenda; the same id on the
    clinic's (a coincidence here) stays untouched."""
    await _per_professional_clinic(db, clinic, google)
    google.put("cal-ana-own", "evt-x", _at(10), _at(10, 30))
    google.put("cal-clinic", "evt-x", _at(10), _at(10, 30))
    appt = await _appointment(db, clinic, "evt-x", clinic.ana)

    resp = await _cancel(client, appt)

    assert resp.status_code == 200
    assert google.ids("cal-ana-own") == set()
    assert google.ids("cal-clinic") == {"evt-x"}


async def test_a_google_outage_on_cancel_changes_nothing(client: AsyncClient, db, clinic, google):
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-ana", "evt-a", _at(10), _at(10, 30))
    appt = await _appointment(db, clinic, "evt-a", clinic.ana)
    google.down.add("tok-clinic")

    resp = await _cancel(client, appt)

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "calendar_unavailable"
    assert (await _row(db, appt.id)).status == AppointmentStatus.SCHEDULED
    assert google.ids("cal-ana") == {"evt-a"}


async def test_release_falls_back_to_the_clinic_for_a_misplaced_legacy_event(
    client: AsyncClient, db, clinic, google, monkeypatch
):
    async def _no_notice(*_args, **_kwargs) -> str:
        return "no_channel"

    monkeypatch.setattr(appointment_release, "notify_released_patient", _no_notice)
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-clinic", "evt-legacy", _at(10), _at(10, 30))
    appt = await _appointment(db, clinic, "evt-legacy", clinic.ana, patient=True)

    resp = await client.post(f"{CALENDAR}/appointments/{appt.id}/release", json={})

    assert resp.status_code == 200, resp.text
    assert google.ids("cal-clinic") == set()
    assert google.where("delete") == ["cal-ana", "cal-clinic"]
    assert (await _row(db, appt.id)).status == AppointmentStatus.CANCELLED


# ---------------------------------------------------------------------------
# Reschedule
# ---------------------------------------------------------------------------


async def test_reschedule_moves_the_event_on_the_owners_agenda(
    client: AsyncClient, db, clinic, google
):
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-ana", "evt-a", _at(10), _at(10, 30))
    appt = await _appointment(db, clinic, "evt-a", clinic.ana)

    resp = await _reschedule(client, appt, hour=15)

    assert resp.status_code == 200
    assert resp.json()["status"] == "rescheduled"
    moved = google.calendars["cal-ana"]["evt-a"]
    assert datetime.fromisoformat(moved["start"]["dateTime"]) == _when(15)
    assert google.where("patch") == ["cal-ana"]


async def test_reschedule_falls_back_to_the_clinic_for_a_misplaced_legacy_event(
    client: AsyncClient, db, clinic, google
):
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-clinic", "evt-legacy", _at(10), _at(10, 30))
    appt = await _appointment(db, clinic, "evt-legacy", clinic.ana)

    resp = await _reschedule(client, appt, hour=16)

    assert resp.status_code == 200
    moved = google.calendars["cal-clinic"]["evt-legacy"]
    assert datetime.fromisoformat(moved["start"]["dateTime"]) == _when(16)
    assert google.where("patch") == ["cal-ana", "cal-clinic"]
    assert (await _row(db, appt.id)).start_at.replace(tzinfo=UTC) == _when(16)


async def test_reschedule_of_an_event_on_no_agenda_is_a_409_and_changes_nothing(
    client: AsyncClient, db, clinic, google
):
    """Before: Google's 404 escaped as a raw 500."""
    await _shared_account_clinic(db, clinic, google)
    appt = await _appointment(db, clinic, "evt-gone", clinic.ana)

    resp = await _reschedule(client, appt)

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "calendar_event_not_found"
    row = await _row(db, appt.id)
    assert row.status == AppointmentStatus.SCHEDULED
    assert row.start_at.replace(tzinfo=UTC) == _when(10)


async def test_a_google_outage_on_reschedule_changes_nothing(
    client: AsyncClient, db, clinic, google
):
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-ana", "evt-a", _at(10), _at(10, 30))
    appt = await _appointment(db, clinic, "evt-a", clinic.ana)
    google.down.add("tok-clinic")

    resp = await _reschedule(client, appt)

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "calendar_unavailable"
    assert (await _row(db, appt.id)).status == AppointmentStatus.SCHEDULED


# ---------------------------------------------------------------------------
# GET /events
# ---------------------------------------------------------------------------


async def test_events_unite_the_clinic_and_every_doctors_agenda(
    client: AsyncClient, db, clinic, google
):
    """Patient bookings live on their doctor's agenda: reading only the clinic's hid them."""
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-bia", "evt-b", _at(8), _at(8, 30))
    google.put("cal-clinic", "evt-typed-clinic", _at(9), _at(9, 30))
    google.put("cal-ana", "evt-a", _at(10), _at(10, 30))
    google.put("cal-clinic", "evt-legacy", _at(11), _at(11, 30))
    google.put("cal-ana", "evt-typed-ana", _at(12), _at(12, 30))
    appt_b = await _appointment(db, clinic, "evt-b", clinic.bia, hour=8)
    appt_a = await _appointment(db, clinic, "evt-a", clinic.ana, hour=10)
    appt_legacy = await _appointment(db, clinic, "evt-legacy", clinic.ana, hour=11)

    resp = await _events(client)

    assert resp.status_code == 200
    body = resp.json()
    assert [e["id"] for e in body] == [
        "evt-b",
        "evt-typed-clinic",
        "evt-a",
        "evt-legacy",
        "evt-typed-ana",
    ]
    by_id = {e["id"]: e for e in body}
    assert by_id["evt-b"]["appointment_id"] == str(appt_b.id)
    assert by_id["evt-a"]["appointment_id"] == str(appt_a.id)
    assert by_id["evt-legacy"]["appointment_id"] == str(appt_legacy.id)
    assert by_id["evt-typed-ana"]["appointment_id"] is None
    assert by_id["evt-b"]["professional_id"] == str(clinic.bia.id)
    assert by_id["evt-a"]["professional_name"] == "Dra. Ana"
    assert sorted(google.where("list")) == ["cal-ana", "cal-bia", "cal-clinic"]


async def test_events_read_each_agenda_once_and_list_each_event_once(
    client: AsyncClient, db, clinic, google
):
    """per_professional: Bia resolves to exactly the clinic's agenda (read once, not
    twice), and one event visible from two agendas is listed once."""
    await _per_professional_clinic(db, clinic, google)
    google.put("cal-clinic", "evt-shared", _at(9), _at(9, 30))
    google.put("cal-ana-own", "evt-shared", _at(9), _at(9, 30))
    google.put("cal-ana-own", "evt-a", _at(10), _at(10, 30))

    body = (await _events(client)).json()

    assert [e["id"] for e in body] == ["evt-shared", "evt-a"]
    assert sorted(google.where("list")) == ["cal-ana-own", "cal-clinic"]


async def test_events_are_read_from_every_agenda_in_parallel(
    client: AsyncClient, db, clinic, google
):
    """Three agendas, one page: each read waits until all three are in flight, which
    only a concurrent read ever reaches."""
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-ana", "evt-a", _at(10), _at(10, 30))
    google.barrier = threading.Barrier(3, timeout=5)

    resp = await _events(client)

    assert resp.status_code == 200
    assert [e["id"] for e in resp.json()] == ["evt-a"]


async def test_a_failing_doctor_agenda_does_not_blank_the_page(
    client: AsyncClient, db, clinic, google
):
    await _per_professional_clinic(db, clinic, google)
    google.put("cal-clinic", "evt-c", _at(9), _at(9, 30))
    google.put("cal-ana-own", "evt-a", _at(10), _at(10, 30))
    google.down.add("tok-ana")

    resp = await _events(client)

    assert resp.status_code == 200
    assert [e["id"] for e in resp.json()] == ["evt-c"]


async def test_the_clinic_agenda_failing_fails_the_page_as_before(
    client: AsyncClient, db, clinic, google
):
    await _per_professional_clinic(db, clinic, google)
    google.put("cal-ana-own", "evt-a", _at(10), _at(10, 30))
    google.down.add("tok-clinic")

    with pytest.raises(CalendarUnavailableError):
        await _events(client)


async def test_a_restricted_doctor_reads_only_the_clinics_agenda_and_her_own(
    client: AsyncClient, db, clinic, google
):
    """TASK-044 scoping unchanged: only her own appointments - wherever they sit - and
    never a Google-only event; another doctor's agenda is not even read."""
    await _shared_account_clinic(db, clinic, google)
    google.put("cal-ana", "evt-a", _at(10), _at(10, 30))
    google.put("cal-ana", "evt-typed-ana", _at(11), _at(11, 30))
    google.put("cal-clinic", "evt-legacy", _at(12), _at(12, 30))
    google.put("cal-bia", "evt-b", _at(13), _at(13, 30))
    await _appointment(db, clinic, "evt-a", clinic.ana, hour=10)
    await _appointment(db, clinic, "evt-legacy", clinic.ana, hour=12, source="clinic")
    await _appointment(db, clinic, "evt-b", clinic.bia, hour=13)
    view_as(AgendaViewer("own", clinic.ana.id))

    resp = await _events(client)

    assert resp.status_code == 200
    assert [e["id"] for e in resp.json()] == ["evt-a", "evt-legacy"]
    assert sorted(google.where("list")) == ["cal-ana", "cal-clinic"]
