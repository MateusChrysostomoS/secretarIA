# ruff: noqa: F811
"""TASK-083 characterization: what the agenda does TODAY, with no shared `agenda_id`.

These tests pin the pre-multi-unit behaviour so the shared-agenda work (spec §3: without an
`agenda_id` everything is identical to today) can prove it changed nothing for a clinic that
is not linked to one. They run on the unmodified HEAD and must keep passing after every
later task: a failure here means a no-agenda path changed behaviour.

Pinned: two doctors of one tenant never block each other; and the free-slot walk,
`slot_taken_at_confirm` and the local exclusive write of one tenant ignore the events
of another tenant entirely (there is no cross-tenant busy time today).
"""

from datetime import timedelta

import pytest

from secretaria.services.availability import slot_taken_at_confirm
from secretaria.services.calendar import CalendarSlotTakenError
from secretaria.services.local_calendar import create_event_exclusive
from tests._local_agenda import (
    agenda,
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda as _wire_agenda,
)
from tests._reminder_fixtures import db  # noqa: F401


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch):
    _wire_agenda(monkeypatch, db)


def _window(hour: int = 10):
    start = tomorrow_at(hour)
    return start, start + timedelta(minutes=30)


async def test_two_professionals_of_one_tenant_do_not_block_each_other(db):
    tenant = await seed_tenant(db)
    ana = await seed_professional(db, tenant, "Dra. Ana")
    bia = await seed_professional(db, tenant, "Dra. Bia")
    start, end = _window()

    await create_event_exclusive(
        agenda(tenant.id, ana.id), start=start, end=end, summary="Consulta - A"
    )

    bia_cal = agenda(tenant.id, bia.id)
    assert not await bia_cal.has_conflict(start, end)
    assert await bia_cal.is_slot_free(start, end)
    assert not await slot_taken_at_confirm(bia_cal, start, end, source="characterization")
    labels = [
        s["label"]
        for s in await bia_cal.list_free_slots(day=start.astimezone(bia_cal.tzinfo), max_slots=30)
    ]
    assert "10:00" in labels
    # ...and Bia can book the very same window exclusively.
    booked = await create_event_exclusive(bia_cal, start=start, end=end, summary="Consulta - B")
    assert booked["id"]
    # Ana's own agenda still refuses the window she already holds.
    assert await slot_taken_at_confirm(
        agenda(tenant.id, ana.id), start, end, source="characterization"
    )


async def test_free_slots_of_a_tenant_ignore_another_tenants_events(db):
    tenant_p = await seed_tenant(db, name="Clínica P")
    tenant_c = await seed_tenant(db, name="Clínica C")
    start, end = _window()
    await agenda(tenant_p.id).create_event(start, end, "Consulta - P")

    cal_c = agenda(tenant_c.id)
    slots = await cal_c.list_free_slots(day=start.astimezone(cal_c.tzinfo), max_slots=30)
    assert "10:00" in [s["label"] for s in slots]
    around = (start - timedelta(hours=1), end + timedelta(hours=1))
    assert await cal_c.check_availability(*around) == []
    # Control: the tenant that owns the event does not offer the slot.
    cal_p = agenda(tenant_p.id)
    own = await cal_p.list_free_slots(day=start.astimezone(cal_p.tzinfo), max_slots=30)
    assert "10:00" not in [s["label"] for s in own]


async def test_slot_taken_at_confirm_ignores_another_tenants_events(db):
    tenant_p = await seed_tenant(db, name="Clínica P")
    tenant_c = await seed_tenant(db, name="Clínica C")
    start, end = _window()
    await agenda(tenant_p.id).create_event(start, end, "Consulta - P")

    source = "characterization"
    assert not await slot_taken_at_confirm(agenda(tenant_c.id), start, end, source=source)
    assert await slot_taken_at_confirm(agenda(tenant_p.id), start, end, source=source)


async def test_local_exclusive_write_ignores_another_tenants_events(db):
    tenant_p = await seed_tenant(db, name="Clínica P")
    tenant_c = await seed_tenant(db, name="Clínica C")
    start, end = _window()
    await agenda(tenant_p.id).create_event(start, end, "Consulta - P")

    booked = await create_event_exclusive(
        agenda(tenant_c.id), start=start, end=end, summary="Consulta - C"
    )
    assert booked["id"]
    # Within one tenant the same write is refused (the exclusivity that exists today).
    with pytest.raises(CalendarSlotTakenError):
        await create_event_exclusive(
            agenda(tenant_p.id), start=start, end=end, summary="Consulta - P2"
        )
