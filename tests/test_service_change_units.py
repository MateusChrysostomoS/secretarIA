"""A service change on a PAID sinal carries the unit picked with it (follow-up E1 review).

"Alterar Dados" may move the booking to another unit together with the new service (the old
unit does not do it). The money side (`service_change`) defers that write - now, or after the
Pix of the difference is paid - through `serialize_edit` / `deserialize_edit`: the unit must
survive both, and a payload stored before (no `unit_id` key) leaves the unit as it is.
"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from secretaria.models import Appointment, Tenant, Unit
from secretaria.services.payments import service_change
from tests import test_service_change as base, test_service_change_webhook as hook

NOW = base.NOW
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks


def _row():
    start = NOW + timedelta(days=5)
    return SimpleNamespace(
        id=uuid4(),
        start_at=start,
        end_at=start + timedelta(minutes=30),
        appointment_type="Consulta",
        google_event_id="evt-1",
        professional_id=None,
    )


def test_serialize_round_trip_carries_the_unit_only_when_the_edit_has_one():
    row = _row()
    unit_id = uuid4()
    edit = {**base._edit(row, "Avaliação", 60), "unit_id": unit_id}
    data = service_change.serialize_edit(edit, row, source="flow")
    assert data["unit_id"] == str(unit_id)
    assert service_change.deserialize_edit(data)["unit_id"] == unit_id

    cleared = service_change.serialize_edit({**edit, "unit_id": None}, row, source="flow")
    assert cleared["unit_id"] is None
    assert service_change.deserialize_edit(cleared)["unit_id"] is None

    plain = service_change.serialize_edit(base._edit(row, "Avaliação", 60), row, source="flow")
    assert "unit_id" not in plain
    assert "unit_id" not in service_change.deserialize_edit(plain)


async def _seed_with_units(db):
    async with db() as session:
        seeded = await base._seed(session)
        tenant, _patient, appointment, _deposit = seeded
        old = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro", address="Rua A, 10")
        new = Unit(id=uuid4(), tenant_id=tenant.id, name="Leste", address="Rua C, 30")
        session.add_all([old, new])
        row = await session.get(Appointment, appointment.id)
        row.unit_id = old.id
        await session.commit()
    return seeded, old.id, new.id


async def _request(db, seeded, service, minutes, **extra):
    tenant, _patient, appointment, deposit = seeded
    async with db() as session:
        tenant = await session.get(Tenant, tenant.id)
        appointment = await session.get(Appointment, appointment.id)
        deposit = await session.get(base.PixDeposit, deposit.id)
        result = await service_change.request_service_change(
            session,
            tenant=tenant,
            appointment=appointment,
            deposit=deposit,
            new_service=service,
            pending_edit={**base._edit(appointment, service, minutes), **extra},
            now=NOW,
        )
        await session.commit()
        return result


async def test_a_change_that_stands_now_writes_the_new_unit(db, provider):
    seeded, _old, new = await _seed_with_units(db)
    # Retorno is cheaper and far ahead: the change stands now (the refund question follows).
    result = await _request(db, seeded, "Retorno", 20, unit_id=new)
    assert result.kind == "asked"
    row = await base._appointment(db, seeded[2].id)
    assert row.appointment_type == "Retorno"
    assert row.unit_id == new


async def test_a_paid_difference_applies_the_new_unit(db, provider):
    seeded, _old, new = await _seed_with_units(db)
    created = await _request(db, seeded, "Avaliação", 60, unit_id=new)
    assert created.kind == "awaiting_payment"
    assert (await hook._adjustment(db)).pending_edit["unit_id"] == str(new)
    row = await base._appointment(db, seeded[2].id)
    assert row.unit_id != new  # nothing moves before the Pix is paid

    hook._paid(provider, created)
    processed = await hook._process(db, seeded[0])
    assert processed.outcome == "adjustment_applied"
    row = await base._appointment(db, seeded[2].id)
    assert row.appointment_type == "Avaliação"
    assert row.unit_id == new


async def test_an_old_payload_without_a_unit_leaves_the_unit_unchanged(db, provider):
    seeded, old, _new = await _seed_with_units(db)
    created = await _request(db, seeded, "Avaliação", 60)
    assert "unit_id" not in (await hook._adjustment(db)).pending_edit
    hook._paid(provider, created)
    processed = await hook._process(db, seeded[0])
    assert processed.outcome == "adjustment_applied"
    row = await base._appointment(db, seeded[2].id)
    assert row.appointment_type == "Avaliação"
    assert row.unit_id == old
