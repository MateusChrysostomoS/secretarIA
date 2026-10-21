# ruff: noqa: F811
"""Connect-time sync: the secretarIA agenda's FUTURE events go to Google, once.

services/calendar_sync.py::push_future_local_events, its arq job
(workers/calendar_sync.py), its trigger in the OAuth callback (api/hub/oauth.py) and
the manual retry (POST /admin/tenants/{id}/calendar/sync-local). Google is a recording
double behind the real factories; everything else is real.
"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from arq import Retry
from httpx import AsyncClient
from sqlalchemy import select, update

from secretaria.api.hub import oauth as oauth_api
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Appointment, AppointmentStatus, Tenant
from secretaria.models.calendar_event import CalendarEvent
from secretaria.services import calendar_sync, google_oauth
from secretaria.services.calendar import (
    CalendarService,
    CalendarUnavailableError,
    GoogleTokenRevokedError,
)
from secretaria.services.calendar_sync import google_id_for, push_future_local_events
from secretaria.services.tenant_config import (
    set_google_refresh_token,
    set_professional_google_refresh_token,
)
from secretaria.workers import calendar_sync as sync_job
from tests._local_agenda import (
    agenda,
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda as _wire_agenda,
)
from tests._reminder_fixtures import db  # noqa: F401


class _Google:
    """Every Google create, by calendar credential; optional failures."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.existing: set[str] = set()
        self.fail: Exception | None = None

    def install(self, monkeypatch) -> None:
        google = self

        async def _create(self, start, end, summary, description="", reminders=None, *,
                          event_id=None):
            if google.fail is not None:
                raise google.fail
            if event_id in google.existing:
                raise _HttpConflict()
            google.created.append({
                "token": self._refresh_token_override,
                "calendar_id": self._calendar_id,
                "id": event_id,
                "start": start,
                "end": end,
                "summary": summary,
            })
            google.existing.add(event_id)
            return {"id": event_id, "htmlLink": f"https://calendar.google.com/{event_id}"}

        async def _get(self, event_id):
            if event_id in google.existing:
                return {"id": event_id, "htmlLink": f"https://calendar.google.com/{event_id}"}
            return None

        monkeypatch.setattr(CalendarService, "create_event", _create)
        monkeypatch.setattr(CalendarService, "get_event", _get)


class _HttpConflict(Exception):
    resp = SimpleNamespace(status=409)


@pytest.fixture
def google(db, monkeypatch) -> _Google:
    _wire_agenda(monkeypatch, db)
    monkeypatch.setattr(calendar_sync, "async_session_factory", db)
    fake = _Google()
    fake.install(monkeypatch)
    return fake


async def _book(db, tenant, start, *, professional_id=None, status=AppointmentStatus.SCHEDULED):
    """A booking on the secretarIA agenda, the way every booking path writes one."""
    event = await agenda(tenant.id, professional_id).create_event(
        start, start + timedelta(minutes=30), "Consulta - Ana", "Convênio: não informado"
    )
    async with db() as session:
        appointment = Appointment(
            tenant_id=tenant.id,
            professional_id=professional_id,
            google_event_id=event["id"],
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            phone="5511999990000",
            status=status,
        )
        session.add(appointment)
        await session.commit()
        await session.refresh(appointment)
    return event["id"], appointment.id


async def _connect_clinic(db, tenant) -> None:
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "clinic-token")
        await session.commit()


async def _row(db, event_id) -> CalendarEvent:
    async with db() as session:
        return await session.get(CalendarEvent, event_id)


async def _appointment(db, appointment_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


# --------------------------------------------------------------------------
# The push
# --------------------------------------------------------------------------


async def test_future_events_move_to_google_and_past_ones_stay(db, google):
    tenant = await seed_tenant(db)
    future_event, future_appt = await _book(db, tenant, tomorrow_at(10))
    past_event, past_appt = await _book(db, tenant, tomorrow_at(10) - timedelta(days=3))
    await _connect_clinic(db, tenant)

    summary = await push_future_local_events(tenant.id)

    assert (summary.pushed, summary.failed, summary.left_local) == (1, 0, 0)
    [created] = google.created
    assert created["token"] == "clinic-token"
    assert created["id"] == google_id_for(future_event)
    assert created["summary"] == "Consulta - Ana"
    pushed_row = await _row(db, future_event)
    assert pushed_row.google_event_id == google_id_for(future_event)
    assert pushed_row.synced_at is not None
    appointment = await _appointment(db, future_appt)
    assert appointment.google_event_id == google_id_for(future_event)
    assert appointment.google_event_link == f"https://calendar.google.com/{created['id']}"
    # The past booking is left where it was, and its appointment keeps the local id.
    assert (await _row(db, past_event)).google_event_id is None
    assert (await _appointment(db, past_appt)).google_event_id == past_event
    # The pushed row no longer counts as busy here: Google's copy is the event now.
    start = tomorrow_at(10)
    assert not await agenda(tenant.id).has_conflict(start, start + timedelta(minutes=30))


async def test_the_google_id_is_deterministic_and_valid_base32hex():
    local_id = "loc_" + uuid4().hex
    google_id = google_id_for(local_id)
    assert google_id == google_id_for(local_id)
    assert 5 <= len(google_id) <= 1024
    assert set(google_id) <= set("0123456789abcdefghijklmnopqrstuv")


async def test_rerunning_pushes_nothing_twice(db, google):
    tenant = await seed_tenant(db)
    await _book(db, tenant, tomorrow_at(10))
    await _connect_clinic(db, tenant)

    first = await push_future_local_events(tenant.id)
    second = await push_future_local_events(tenant.id)

    assert (first.pushed, second.pushed, second.recovered) == (1, 0, 0)
    assert len(google.created) == 1


async def test_a_lost_commit_is_recovered_from_googles_copy(db, google):
    """Google created it last time but the row never recorded it: 409, adopt the copy."""
    tenant = await seed_tenant(db)
    event_id, appointment_id = await _book(db, tenant, tomorrow_at(10))
    await _connect_clinic(db, tenant)
    google.existing.add(google_id_for(event_id))

    summary = await push_future_local_events(tenant.id)

    assert (summary.pushed, summary.recovered) == (0, 1)
    assert google.created == []
    assert (await _appointment(db, appointment_id)).google_event_id == google_id_for(event_id)


async def test_a_cancelled_or_ended_event_is_never_pushed(db, google):
    tenant = await seed_tenant(db)
    event_id, _ = await _book(db, tenant, tomorrow_at(10))
    await agenda(tenant.id).cancel_event(event_id)
    await _connect_clinic(db, tenant)

    summary = await push_future_local_events(tenant.id)

    assert summary.pushed == 0 and google.created == []


async def test_without_any_credential_nothing_leaves_the_secretaria_agenda(db, google):
    tenant = await seed_tenant(db)
    await _book(db, tenant, tomorrow_at(10))

    summary = await push_future_local_events(tenant.id)

    assert (summary.pushed, summary.left_local) == (0, 1)
    assert google.created == []


async def test_only_the_agendas_a_credential_now_covers_are_pushed(db, google):
    tenant = await seed_tenant(db)
    async with db() as session, session.begin():
        await session.execute(
            update(Tenant).where(Tenant.id == tenant.id)
            .values(google_calendar_mode="per_professional")
        )
    ana = await seed_professional(db, tenant, "Dra. Ana")
    bia = await seed_professional(db, tenant, "Dra. Bia")
    ana_event, _ = await _book(db, tenant, tomorrow_at(10), professional_id=ana.id)
    bia_event, _ = await _book(db, tenant, tomorrow_at(10), professional_id=bia.id)
    async with db() as session:
        await set_professional_google_refresh_token(session, ana.id, "ana-token")
        await session.commit()

    summary = await push_future_local_events(tenant.id)

    assert (summary.pushed, summary.left_local) == (1, 1)
    assert [c["token"] for c in google.created] == ["ana-token"]
    assert (await _row(db, ana_event)).google_event_id is not None
    assert (await _row(db, bia_event)).google_event_id is None


async def test_an_outage_changes_nothing_and_asks_for_a_retry(db, google):
    tenant = await seed_tenant(db)
    event_id, appointment_id = await _book(db, tenant, tomorrow_at(10))
    await _book(db, tenant, tomorrow_at(11))
    await _connect_clinic(db, tenant)
    google.fail = CalendarUnavailableError("Google Calendar returned HTTP 503")

    summary = await push_future_local_events(tenant.id)

    assert summary.unavailable and not summary.reconnect_required
    assert (summary.pushed, summary.failed) == (0, 2)
    assert (await _row(db, event_id)).google_event_id is None
    assert (await _appointment(db, appointment_id)).google_event_id == event_id
    with pytest.raises(Retry):
        await sync_job.sync_local_calendar_to_google({"job_try": 1}, str(tenant.id))

    # Once Google is back the retry finishes the job.
    google.fail = None
    result = await sync_job.sync_local_calendar_to_google({"job_try": 2}, str(tenant.id))
    assert result["pushed"] == 2


async def test_a_revoked_token_is_not_retried(db, google):
    tenant = await seed_tenant(db)
    await _book(db, tenant, tomorrow_at(10))
    await _connect_clinic(db, tenant)
    google.fail = GoogleTokenRevokedError("Google Calendar refresh token rejected")

    result = await sync_job.sync_local_calendar_to_google({"job_try": 1}, str(tenant.id))

    assert result["reconnect_required"] is True and result["pushed"] == 0


async def test_after_the_push_the_google_agenda_reaches_the_copy(db, google, monkeypatch):
    """A stale `loc_` reference (an in-flight conversation) still cancels the right event."""
    tenant = await seed_tenant(db)
    event_id, _ = await _book(db, tenant, tomorrow_at(10))
    await _connect_clinic(db, tenant)
    await push_future_local_events(tenant.id)
    deleted: list[str] = []

    class _Client:
        def events(self):
            return self

        def delete(self, calendarId, eventId):
            deleted.append(eventId)
            return self

        def execute(self):
            return None

    monkeypatch.setattr(CalendarService, "_client", lambda self: _Client())
    async with db() as session:
        tenant_row = await session.get(Tenant, tenant.id)
        from secretaria.services.tenant_config import load_tenant_config

        clinic = CalendarService.from_tenant_config(await load_tenant_config(session, tenant_row))

    assert await clinic.cancel_event(event_id) is True
    assert deleted == [google_id_for(event_id)]


# --------------------------------------------------------------------------
# Triggers
# --------------------------------------------------------------------------


class _Pool:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, *args))


@pytest.fixture
def app_wired(db):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _fake_get_session
    pool = _Pool()
    app.state.arq_pool = pool
    yield app, pool
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.state.arq_pool = None


async def test_a_successful_connection_enqueues_the_sync(client: AsyncClient, db, app_wired,
                                                          monkeypatch):
    _app, pool = app_wired
    tenant = await seed_tenant(db)
    ana = await seed_professional(db, tenant, "Dra. Ana")

    async def _exchange(code):
        return "fresh-token"

    monkeypatch.setattr(google_oauth, "exchange_code_for_refresh_token", _exchange)
    for payload in ({"tenant_id": str(tenant.id)},
                    {"tenant_id": str(tenant.id), "professional_id": str(ana.id)}):
        monkeypatch.setattr(oauth_api, "verify", lambda *_a, _p=payload: _p)
        resp = await client.get("/oauth/google/callback", params={"code": "c", "state": "s"})
        assert resp.status_code in (200, 302)

    assert pool.jobs == [
        ("sync_local_calendar_to_google", str(tenant.id), None),
        ("sync_local_calendar_to_google", str(tenant.id), str(ana.id)),
    ]


async def test_a_failed_connection_enqueues_nothing(client: AsyncClient, db, app_wired,
                                                     monkeypatch):
    _app, pool = app_wired
    tenant = await seed_tenant(db)

    async def _exchange(code):
        return None  # Google sent no refresh token

    monkeypatch.setattr(google_oauth, "exchange_code_for_refresh_token", _exchange)
    monkeypatch.setattr(oauth_api, "verify", lambda *_a: {"tenant_id": str(tenant.id)})
    await client.get("/oauth/google/callback", params={"code": "c", "state": "s"})

    assert pool.jobs == []


async def test_the_admin_route_runs_the_sync_now(client: AsyncClient, db, app_wired, google,
                                                  monkeypatch):
    from secretaria.api.admin.panel import require_admin

    app, _pool = app_wired
    app.dependency_overrides[require_admin] = lambda: None
    tenant = await seed_tenant(db)
    await _book(db, tenant, tomorrow_at(10))
    await _connect_clinic(db, tenant)
    try:
        resp = await client.post(f"/admin/tenants/{tenant.id}/calendar/sync-local")
        missing = await client.post(f"/admin/tenants/{uuid4()}/calendar/sync-local")
    finally:
        app.dependency_overrides.pop(require_admin, None)

    assert resp.status_code == 200 and resp.json()["pushed"] == 1
    assert missing.status_code == 404


async def test_the_hub_still_lists_pre_connection_bookings(client: AsyncClient, db, app_wired,
                                                           google, monkeypatch):
    """After connecting, the past local bookings stay visible next to Google's events."""
    app, _pool = app_wired
    tenant = await seed_tenant(db)
    past = tomorrow_at(10) - timedelta(days=2)
    past_event, past_appt = await _book(db, tenant, past)
    await _connect_clinic(db, tenant)

    async def _google_list(self, start, end, max_events=0):
        return [{"id": "g-1", "summary": "No Google", "start": (past + timedelta(hours=2))
                 .isoformat(), "end": (past + timedelta(hours=3)).isoformat()}]

    monkeypatch.setattr(CalendarService, "list_events", _google_list)

    async def _tenant():
        async with db() as session:
            return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_current_tenant] = _tenant
    resp = await client.get(
        "/tenants/me/calendar/events",
        params={"start": (past - timedelta(days=1)).isoformat(),
                "end": (past + timedelta(days=1)).isoformat()},
    )

    assert resp.status_code == 200, resp.text
    by_id = {event["id"]: event for event in resp.json()}
    assert by_id[past_event]["calendar_backend"] == "local"
    assert by_id[past_event]["appointment_id"] == str(past_appt)
    assert by_id["g-1"]["calendar_backend"] == "google"
    assert [event["id"] for event in resp.json()] == [past_event, "g-1"]  # chronological
    async with db() as session:
        assert (await session.scalar(
            select(CalendarEvent.google_event_id).where(CalendarEvent.id == past_event)
        )) is None
