# ruff: noqa: F811
"""TASK-083 models: `agendas`, `professional_units` and the new nullable columns (spec §5)."""

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from secretaria.models import (
    Agenda,
    BookingHold,
    CalendarEvent,
    Professional,
    ProfessionalUnit,
    Unit,
)
from tests._local_agenda import seed_professional, seed_tenant
from tests._reminder_fixtures import db  # noqa: F401


def test_the_new_models_are_exported_and_registered_on_the_metadata():
    from secretaria.core.database import Base

    assert Agenda.__tablename__ == "agendas"
    assert ProfessionalUnit.__tablename__ == "professional_units"
    assert {"agendas", "professional_units"} <= set(Base.metadata.tables)


def test_new_columns_are_all_nullable_so_existing_rows_keep_their_meaning():
    assert Professional.__table__.c.agenda_id.nullable is True
    assert CalendarEvent.__table__.c.unit_id.nullable is True
    assert BookingHold.__table__.c.unit_id.nullable is True


def test_foreign_keys_follow_the_spec():
    (agenda_fk,) = Professional.__table__.c.agenda_id.foreign_keys
    assert agenda_fk.target_fullname == "agendas.id"
    assert agenda_fk.ondelete == "SET NULL"
    links = ProfessionalUnit.__table__
    assert {fk.target_fullname: fk.ondelete for c in links.c for fk in c.foreign_keys} == {
        "professionals.id": "CASCADE",
        "units.id": "CASCADE",
    }
    # calendar_events.unit_id / booking_holds.unit_id are historical labels: no FK.
    assert not CalendarEvent.__table__.c.unit_id.foreign_keys
    assert not BookingHold.__table__.c.unit_id.foreign_keys


async def test_agenda_person_key_is_unique(db):
    async with db() as session:
        session.add(Agenda(person_key="acct-1"))
        await session.commit()
    async with db() as session:
        session.add(Agenda(person_key="acct-1"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_two_professionals_of_different_tenants_share_one_agenda(db):
    tenant_p = await seed_tenant(db, name="Clínica P")
    tenant_c = await seed_tenant(db, name="Clínica C")
    pro_p = await seed_professional(db, tenant_p, "Dr. X")
    pro_c = await seed_professional(db, tenant_c, "Dr. X")
    assert pro_p.agenda_id is None and pro_c.agenda_id is None

    async with db() as session:
        agenda = Agenda(person_key="acct-1")
        session.add(agenda)
        await session.flush()
        for pid in (pro_p.id, pro_c.id):
            (await session.get(Professional, pid)).agenda_id = agenda.id
        await session.commit()
        agenda_id = agenda.id

    async with db() as session:
        got = {(await session.get(Professional, p.id)).agenda_id for p in (pro_p, pro_c)}
    assert got == {agenda_id}


async def test_professional_unit_defaults_and_composite_key(db):
    tenant = await seed_tenant(db)
    pro = await seed_professional(db, tenant, "Dra. Ana")
    unit_id = uuid4()
    async with db() as session:
        session.add(Unit(id=unit_id, tenant_id=tenant.id, name="Centro"))
        await session.flush()
        session.add(ProfessionalUnit(professional_id=pro.id, unit_id=unit_id))
        await session.commit()

    async with db() as session:
        link = await session.get(ProfessionalUnit, (pro.id, unit_id))
        assert link.business_hours is None  # NULL = inherit the professional's hours
        assert link.is_active is True
        assert link.sort_order == 0
        assert link.created_at is not None
    async with db() as session:
        session.add(ProfessionalUnit(professional_id=pro.id, unit_id=unit_id))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_unit_id_columns_round_trip_on_events_and_holds(db):
    tenant = await seed_tenant(db)
    unit_id = uuid4()
    from datetime import UTC, datetime, timedelta

    start = datetime.now(UTC)
    async with db() as session:
        session.add(
            CalendarEvent(
                id="loc_x", tenant_id=tenant.id, start_at=start, end_at=start + timedelta(hours=1),
                unit_id=unit_id,
            )
        )
        session.add(CalendarEvent(
            id="loc_y", tenant_id=tenant.id, start_at=start, end_at=start + timedelta(hours=1)
        ))
        await session.commit()
    async with db() as session:
        assert (await session.get(CalendarEvent, "loc_x")).unit_id == unit_id
        assert (await session.get(CalendarEvent, "loc_y")).unit_id is None
