# ruff: noqa: F811
"""Which agenda answers: Google when a credential covers it, the secretarIA's otherwise.

services/calendar.py::CalendarService.from_tenant_config is THE selection point (native
agenda, founder's decision 2026-10-20). These pin the rule for the clinic's agenda and
for every professional's, the revoked-token case (stays Google, never switches), the
env-var dev token (never used for a tenant), and the Google agenda's routing of `loc_`
ids booked before the clinic connected.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update

from secretaria.config import Settings
from secretaria.models import Tenant
from secretaria.models.calendar_event import CalendarEvent
from secretaria.services import calendar as calendar_module
from secretaria.services.calendar import CalendarService, GoogleTokenRevokedError
from secretaria.services.local_calendar import LocalCalendarService
from secretaria.services.tenant_config import (
    load_tenant_config,
    resolve_professional_calendar,
    set_google_refresh_token,
    set_professional_google_refresh_token,
)
from tests._local_agenda import (
    agenda,
    runtime_config,
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda,
)
from tests._reminder_fixtures import db  # noqa: F401


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch):
    wire_local_agenda(monkeypatch, db)


async def _set_mode(db, tenant, mode: str) -> None:
    async with db() as session, session.begin():
        await session.execute(
            update(Tenant).where(Tenant.id == tenant.id).values(google_calendar_mode=mode)
        )


async def _resolve(db, tenant_id, professional):
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        return await resolve_professional_calendar(session, tenant, professional)


async def _clinic(db, tenant_id):
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        return CalendarService.from_tenant_config(await load_tenant_config(session, tenant))


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


def test_a_token_selects_google_and_none_selects_the_secretaria_agenda():
    tenant_id = uuid4()
    google = CalendarService.from_tenant_config(runtime_config(tenant_id, token="refresh"))
    local = CalendarService.from_tenant_config(runtime_config(tenant_id))

    assert type(google) is CalendarService and google.backend == "google"
    assert isinstance(local, LocalCalendarService) and local.backend == "local"
    assert local.tenant_id == tenant_id and local.professional_id is None
    # Same hours and slot length: the slot arithmetic is the inherited one.
    assert local.default_slot_minutes == google.default_slot_minutes == 30
    assert local._business_hours == google._business_hours


def test_the_env_dev_token_is_never_a_tenants_credential():
    settings = Settings(GOOGLE_REFRESH_TOKEN="env-dev-token", GOOGLE_CLIENT_ID="c")
    assert settings.GOOGLE_REFRESH_TOKEN  # the scaffold IS configured...
    calendar = CalendarService.from_tenant_config(runtime_config(uuid4()))
    # ...and still a tenant without its own token gets the secretarIA agenda.
    assert isinstance(calendar, LocalCalendarService)


def test_the_sole_professional_owns_the_clinic_agenda():
    """`load_tenant_config` resolves a one-doctor clinic through him: same agenda key."""
    tenant_id, professional_id = uuid4(), uuid4()
    clinic = CalendarService.from_tenant_config(
        runtime_config(tenant_id, professional_id=professional_id)
    )
    mine = CalendarService.for_professional(
        runtime_config(tenant_id, professional_id=professional_id),
        professional_id=professional_id,
    )
    assert clinic.professional_id == professional_id
    assert clinic.references_same_calendar(mine)


async def test_clinic_without_google_gets_the_local_agenda(db):
    tenant = await seed_tenant(db)
    calendar = await _clinic(db, tenant.id)
    assert isinstance(calendar, LocalCalendarService)
    assert calendar.tenant_id == tenant.id


async def test_clinic_with_google_keeps_google(db):
    tenant = await seed_tenant(db)
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "clinic-token")
        await session.commit()
    calendar = await _clinic(db, tenant.id)
    assert type(calendar) is CalendarService
    assert calendar._refresh_token_override == "clinic-token"


async def test_doctors_without_google_get_separate_local_agendas(db):
    tenant = await seed_tenant(db)
    ana = await seed_professional(db, tenant, "Dra. Ana")
    bia = await seed_professional(db, tenant, "Dra. Bia")

    ana_cal = await _resolve(db, tenant.id, ana)
    bia_cal = await _resolve(db, tenant.id, bia)
    clinic = await _clinic(db, tenant.id)

    assert isinstance(ana_cal, LocalCalendarService) and ana_cal.professional_id == ana.id
    assert isinstance(bia_cal, LocalCalendarService) and bia_cal.professional_id == bia.id
    assert clinic.professional_id is None  # two active doctors: the clinic's own agenda
    assert not ana_cal.references_same_calendar(bia_cal)
    assert not ana_cal.references_same_calendar(clinic)


async def test_per_professional_own_token_is_google_and_the_colleague_stays_local(db):
    tenant = await seed_tenant(db)
    await _set_mode(db, tenant, "per_professional")
    ana = await seed_professional(db, tenant, "Dra. Ana")
    bia = await seed_professional(db, tenant, "Dra. Bia")
    async with db() as session:
        await set_professional_google_refresh_token(session, ana.id, "ana-token")
        await session.commit()

    ana_cal = await _resolve(db, tenant.id, ana)
    bia_cal = await _resolve(db, tenant.id, bia)

    assert type(ana_cal) is CalendarService and ana_cal._refresh_token_override == "ana-token"
    assert isinstance(bia_cal, LocalCalendarService)


async def test_a_clinic_token_covers_every_doctor_in_both_modes(db):
    for mode in ("per_professional", "shared_account"):
        tenant = await seed_tenant(db)
        await _set_mode(db, tenant, mode)
        ana = await seed_professional(db, tenant, "Dra. Ana")
        async with db() as session:
            await set_google_refresh_token(session, tenant.id, "clinic-token")
            await session.commit()
        calendar = await _resolve(db, tenant.id, ana)
        assert type(calendar) is CalendarService, mode
        assert calendar._refresh_token_override == "clinic-token", mode


async def test_shared_account_never_uses_the_doctors_own_token(db):
    """shared_account books with the clinic token only: without it, local - not his own."""
    tenant = await seed_tenant(db)
    await _set_mode(db, tenant, "shared_account")
    ana = await seed_professional(db, tenant, "Dra. Ana")
    async with db() as session:
        await set_professional_google_refresh_token(session, ana.id, "leftover-own-token")
        await session.commit()
    assert isinstance(await _resolve(db, tenant.id, ana), LocalCalendarService)


async def test_a_revoked_clinic_token_stays_google_and_raises(db, monkeypatch):
    """Never a silent switch to the local agenda: that would split one clinic's bookings."""
    tenant = await seed_tenant(db)
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "revoked-token")
        await session.commit()

    def _revoked(self):
        raise GoogleTokenRevokedError("Google Calendar refresh token rejected")

    monkeypatch.setattr(CalendarService, "_build_service", _revoked)
    calendar = await _clinic(db, tenant.id)
    assert type(calendar) is CalendarService
    start = tomorrow_at(10)
    with pytest.raises(GoogleTokenRevokedError):
        await calendar.check_availability(start, start + timedelta(minutes=30))
    with pytest.raises(GoogleTokenRevokedError):
        await calendar.create_event(start, start + timedelta(minutes=30), "X")


# --------------------------------------------------------------------------
# A Google agenda acting on an event booked locally before connecting
# --------------------------------------------------------------------------


class _GoogleSpy:
    """A Google client that records every call (`events().<op>(...).execute()`)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def events(self):
        return self

    def _record(self, op, kwargs):
        self.calls.append((op, kwargs))
        return self

    def delete(self, **kwargs):
        return self._record("delete", kwargs)

    def patch(self, **kwargs):
        return self._record("patch", kwargs)

    def execute(self):
        return {"id": self.calls[-1][1].get("eventId")}


async def test_google_agenda_cancels_a_never_pushed_local_event_locally(db, monkeypatch):
    tenant = await seed_tenant(db)
    start = tomorrow_at(10)
    event = await agenda(tenant.id).create_event(start, start + timedelta(minutes=30), "Antiga")
    spy = _GoogleSpy()
    monkeypatch.setattr(CalendarService, "_client", lambda self: spy)
    google = CalendarService.from_tenant_config(runtime_config(tenant.id, token="t"))

    assert await google.cancel_event(event["id"]) is True
    assert spy.calls == []  # Google was never asked about a `loc_` id
    assert not await agenda(tenant.id).has_conflict(start, start + timedelta(minutes=30))


async def test_google_agenda_acts_on_the_google_copy_of_a_pushed_event(db, monkeypatch):
    tenant = await seed_tenant(db)
    start = tomorrow_at(10)
    event = await agenda(tenant.id).create_event(start, start + timedelta(minutes=30), "Futura")
    copy_id = "sec" + event["id"][4:]
    async with db() as session, session.begin():
        await session.execute(
            update(CalendarEvent)
            .where(CalendarEvent.id == event["id"])
            .values(google_event_id=copy_id)
        )
    spy = _GoogleSpy()
    monkeypatch.setattr(CalendarService, "_client", lambda self: spy)
    google = CalendarService.from_tenant_config(runtime_config(tenant.id, token="t"))

    assert await google.cancel_event(event["id"]) is True
    await google.update_event(event["id"], start, start + timedelta(minutes=30))
    assert [(op, kw["eventId"]) for op, kw in spy.calls] == [
        ("delete", copy_id),
        ("patch", copy_id),
    ]


def test_a_bare_env_service_passes_ids_straight_to_google():
    service = CalendarService()
    assert service._handles_local_ids("loc_abc") is False
    assert calendar_module.is_local_event_id("loc_abc") is True
    assert calendar_module.is_local_event_id("evt-123") is False
    assert calendar_module.is_local_event_id(None) is False
