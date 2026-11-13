# ruff: noqa: F811
"""TASK-083 spec §7 item 4: the clinic's hub WARNS about the doctor's other clinic, never blocks.

Clinic P has Dr. X booked for a window; clinic C (the one acting in the hub) has Dr. X too, on
the same person's agenda. The staff of C may still put a consultation there (an "encaixe" is
the clinic's call): create, edit and reschedule succeed and answer
`conflicts_other_agenda: true` (default false; nothing about P is said). C's OWN agenda still
refuses an edit onto a time C itself booked (409 `slot_unavailable`, as before). Without an
agenda every answer is what it was, plus `conflicts_other_agenda: false`.
"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Agenda, Appointment, AppointmentStatus, Professional, Tenant
from secretaria.services import appointment_release, clinic_action_notice
from secretaria.services.tenant_config import set_google_refresh_token
from tests._google_fake import install_google_fake
from tests._local_agenda import (
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda,
)
from tests._reminder_fixtures import db  # noqa: F401

CALENDAR = "/tenants/me/calendar"


@pytest.fixture
def acting() -> dict:
    return {"tenant_id": None}


@pytest.fixture(autouse=True)
def _wire(db, acting, monkeypatch: pytest.MonkeyPatch):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, acting["tenant_id"])

    async def _staff_notice(*_args, **_kwargs):
        return SimpleNamespace(code=None, whatsapp_link=None)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    wire_local_agenda(monkeypatch, db)
    for name in ("notify_staff_edit", "notify_staff_confirmation", "notify_attended"):
        monkeypatch.setattr(clinic_action_notice, name, _staff_notice)

    async def _notice(*_args, **_kwargs):
        return "whatsapp_queued"

    monkeypatch.setattr(appointment_release, "notify_cancelled_patient", _notice)
    app.state.arq_pool = None
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _link(db, *professionals):
    async with db() as session:
        row = Agenda(person_key=f"acct-{uuid4().hex[:8]}")
        session.add(row)
        await session.flush()
        for professional in professionals:
            (await session.get(Professional, professional.id)).agenda_id = row.id
        await session.commit()


async def _world(db, acting, *, shared: bool = True):
    """C (acting) and P each have Dr. X; P booked him at 10:00 tomorrow."""
    tenant_p = await seed_tenant(db, name="Clínica P")
    tenant_c = await seed_tenant(db, name="Clínica C")
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    x_c = await seed_professional(db, tenant_c, "Dr. X")
    if shared:
        await _link(db, x_p, x_c)
    start = tomorrow_at(10)
    async with db() as session:
        session.add(
            Appointment(
                tenant_id=tenant_p.id,
                professional_id=x_p.id,
                google_event_id="evt-p",
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()
    acting["tenant_id"] = tenant_c.id
    return tenant_c, x_c, start


async def _create(client, start, professional, **extra):
    return await client.post(
        f"{CALENDAR}/appointments",
        json={
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=30)).isoformat(),
            "summary": "Consulta - Joana",
            "phone": "5511999990000",
            "professional_id": str(professional.id),
            **extra,
        },
    )


# --------------------------------------------------------------------------
# The secretarIA agenda
# --------------------------------------------------------------------------


async def test_create_over_the_other_clinic_succeeds_with_a_warning(
    client: AsyncClient, db, acting
):
    _tenant_c, x_c, start = await _world(db, acting)

    over = await _create(client, start, x_c)
    free = await _create(client, start + timedelta(hours=2), x_c)

    assert over.status_code == 201, over.text
    assert over.json()["conflicts_other_agenda"] is True
    assert free.status_code == 201 and free.json()["conflicts_other_agenda"] is False
    assert "Clínica P" not in over.text and "evt-p" not in over.text


async def test_edit_over_the_other_clinic_succeeds_with_a_warning(client: AsyncClient, db, acting):
    _tenant_c, x_c, start = await _world(db, acting)
    created = (await _create(client, start + timedelta(hours=3), x_c)).json()

    moved = await client.post(
        f"{CALENDAR}/appointments/{created['id']}/edit", json={"start_at": start.isoformat()}
    )

    assert moved.status_code == 200, moved.text
    assert moved.json()["conflicts_other_agenda"] is True


async def test_edit_over_the_clinics_own_booking_is_still_refused(client: AsyncClient, db, acting):
    _tenant_c, x_c, start = await _world(db, acting)
    # Hours stay before 18:00 UTC: the staff free check reads the UTC instant's own wall clock
    # against the opening hours (pre-existing, tests/test_hub_calendar_local.py does the same).
    own_busy = start + timedelta(hours=2)
    await _create(client, own_busy, x_c)
    created = (await _create(client, start + timedelta(hours=3), x_c)).json()

    taken = await client.post(
        f"{CALENDAR}/appointments/{created['id']}/edit", json={"start_at": own_busy.isoformat()}
    )
    assert taken.status_code == 409
    assert taken.json()["detail"]["code"] == "slot_unavailable"

    free = start + timedelta(hours=4)
    moved = await client.post(
        f"{CALENDAR}/appointments/{created['id']}/edit", json={"start_at": free.isoformat()}
    )
    assert moved.status_code == 200 and moved.json()["conflicts_other_agenda"] is False


async def test_reschedule_over_the_other_clinic_succeeds_with_a_warning(
    client: AsyncClient, db, acting
):
    _tenant_c, x_c, start = await _world(db, acting)
    created = (await _create(client, start + timedelta(hours=3), x_c)).json()

    over = await client.post(
        f"{CALENDAR}/appointments/{created['id']}/reschedule",
        json={"new_start": start.isoformat(),
              "new_end": (start + timedelta(minutes=30)).isoformat()},
    )
    free_start = start + timedelta(hours=5)
    free = await client.post(
        f"{CALENDAR}/appointments/{created['id']}/reschedule",
        json={"new_start": free_start.isoformat(),
              "new_end": (free_start + timedelta(minutes=30)).isoformat()},
    )

    assert over.status_code == 200, over.text
    assert over.json()["conflicts_other_agenda"] is True
    assert free.status_code == 200 and free.json()["conflicts_other_agenda"] is False


async def test_without_an_agenda_nothing_is_flagged(client: AsyncClient, db, acting):
    _tenant_c, x_c, start = await _world(db, acting, shared=False)

    created = await _create(client, start, x_c)
    assert created.status_code == 201 and created.json()["conflicts_other_agenda"] is False
    moved = await client.post(
        f"{CALENDAR}/appointments/{created.json()['id']}/edit",
        json={"start_at": (start + timedelta(hours=1)).isoformat()},
    )
    assert moved.status_code == 200 and moved.json()["conflicts_other_agenda"] is False
    resched = await client.post(
        f"{CALENDAR}/appointments/{created.json()['id']}/reschedule",
        json={"new_start": start.isoformat(),
              "new_end": (start + timedelta(minutes=30)).isoformat()},
    )
    assert resched.status_code == 200 and resched.json()["conflicts_other_agenda"] is False


async def test_other_clinic_actions_keep_their_keys(client: AsyncClient, db, acting):
    """Only create/edit/reschedule grew the key (frozen contract); a block did not."""
    _tenant_c, x_c, start = await _world(db, acting)
    block = await client.post(
        f"{CALENDAR}/blocks",
        json={"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat(),
              "professional_id": str(x_c.id)},
    )
    assert block.status_code == 201 and "conflicts_other_agenda" not in block.json()


# --------------------------------------------------------------------------
# A Google agenda
# --------------------------------------------------------------------------


async def test_google_create_and_edit_over_the_other_clinic_warn(
    client: AsyncClient, db, acting, monkeypatch
):
    google = install_google_fake(monkeypatch)
    tenant_c, x_c, start = await _world(db, acting)
    async with db() as session:
        await set_google_refresh_token(session, tenant_c.id, "tok-c")
        await session.commit()

    other = (await _create(client, start + timedelta(hours=3), x_c)).json()
    assert other["conflicts_other_agenda"] is False
    moved = await client.post(
        f"{CALENDAR}/appointments/{other['id']}/edit", json={"start_at": start.isoformat()}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["conflicts_other_agenda"] is True

    over = await _create(client, start, x_c)  # a create is an encaixe: never refused
    assert over.status_code == 201, over.text
    assert over.json()["conflicts_other_agenda"] is True
    assert len(google.events_of("tok-c")) == 2

    # The clinic's own Google events (both at 10:00 now) still refuse an edit onto them.
    third = (await _create(client, start + timedelta(hours=5), x_c)).json()
    taken = await client.post(
        f"{CALENDAR}/appointments/{third['id']}/edit", json={"start_at": start.isoformat()}
    )
    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "slot_unavailable"
