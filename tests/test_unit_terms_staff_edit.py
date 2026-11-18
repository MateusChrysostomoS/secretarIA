"""TASK-085 T4b / A9: the clinic's "Editar/Remarcar" at the booking's unit.

* the new service's length is the doctor's own at the unit the booking keeps after this edit
  (the appointment's, or the one a doctor change keeps / clears - `_unit_kept`); no exception or
  no unit = today's base (characterization);
* a PAID sinal: TASK-085 R (D-R1/D-R3) - a change of service, unit (a doctor change that clears
  it), doctor or convênio ALWAYS goes through the money engine, which prices the target at its
  doctor and unit and decides (the same price = nothing for the money); the pending edit always
  carries the booking's `unit_id`. Replaces T4b's "only when a unit's own price moves it" and R4
  ("a doctor change alone never re-prices").
"""

# ruff: noqa: F811

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from secretaria.models import (
    PixDeposit,
    ProfessionalServiceUnitTerms,
    Service,
    Tenant,
)
from secretaria.services import staff_appointment_edit as sae
from tests._r7_support import add_professional
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment
from tests.test_staff_appointment_edit import (  # noqa: F401 - autouse + fixtures
    _adjustments,
    _calendars,
    _edit,
    _paid_world,
    _req,
    _set_unit,
    _unit,
    _utc,
    _world,
    provider,
)


async def _catalog(db, tenant_id) -> dict:
    """Consulta (30) and Retorno (40) as catalog services; the tenant offers both by id."""
    async with db() as session:
        consulta = Service(
            id=uuid4(), tenant_id=tenant_id, name="Consulta", normalized_name="consulta"
        )
        retorno = Service(
            id=uuid4(), tenant_id=tenant_id, name="Retorno", normalized_name="retorno"
        )
        session.add_all([consulta, retorno])
        tenant = await session.get(Tenant, tenant_id)
        tenant.appointment_types = [
            {
                "name": "Consulta",
                "is_active": True,
                "duration_min": 30,
                "service_id": str(consulta.id),
            },
            {
                "name": "Retorno",
                "is_active": True,
                "duration_min": 40,
                "service_id": str(retorno.id),
            },
        ]
        await session.commit()
    return {"Consulta": consulta.id, "Retorno": retorno.id}


async def _own_length(db, tenant_id, professional_id, service_id, unit_id, minutes) -> None:
    async with db() as session:
        session.add(
            ProfessionalServiceUnitTerms(
                tenant_id=tenant_id,
                professional_id=professional_id,
                service_id=service_id,
                unit_id=unit_id,
                duration_min=minutes,
            )
        )
        await session.commit()


async def _at_centro(db, **kwargs):
    """Dra. Ana's booking at Centro (Ana and Dr. Bruno attend Centro; Dr. Carlos does not)."""
    world = await _world(db, professional_name="Dra. Ana", **kwargs)
    ana = world.appointment.professional_id
    services = await _catalog(db, world.tenant.id)
    bruno = await add_professional(db, world.tenant.id, "Dr. Bruno")
    carlos = await add_professional(db, world.tenant.id, "Dr. Carlos")
    centro = await _unit(db, world.tenant.id, "Centro", ana, bruno)
    await _unit(db, world.tenant.id, "Norte", carlos)
    await _set_unit(db, world.appointment.id, centro.id)
    world.appointment = await reload_appointment(db, world.appointment.id)
    return world, services, centro, ana, bruno, carlos


def _length(row) -> timedelta:
    return _utc(row.end_at) - _utc(row.start_at)


async def test_a_new_service_at_a_unit_without_exceptions_takes_todays_base(db):
    world, *_ = await _at_centro(db)
    await _edit(db, world, _req(service="retorno"))
    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Retorno" and _length(row) == timedelta(minutes=40)


async def test_a_new_service_takes_the_doctors_length_at_the_bookings_unit(db):
    world, services, centro, ana, bruno, _carlos = await _at_centro(db)
    await _own_length(db, world.tenant.id, ana, services["Retorno"], centro.id, 55)
    # Another doctor's exception at the same unit is not Ana's.
    await _own_length(db, world.tenant.id, bruno, services["Retorno"], centro.id, 25)
    await _edit(db, world, _req(service="retorno"))
    row = await reload_appointment(db, world.appointment.id)
    assert row.unit_id == centro.id and _length(row) == timedelta(minutes=55)


async def test_a_new_doctor_keeping_the_unit_brings_his_own_length_there(db):
    world, services, centro, ana, bruno, _carlos = await _at_centro(db)
    await _own_length(db, world.tenant.id, ana, services["Retorno"], centro.id, 55)
    await _own_length(db, world.tenant.id, bruno, services["Retorno"], centro.id, 35)
    await _edit(db, world, _req(professional_id=bruno, service="Retorno"))
    row = await reload_appointment(db, world.appointment.id)
    assert (row.professional_id, row.unit_id) == (bruno, centro.id)
    assert _length(row) == timedelta(minutes=35)


async def test_a_new_doctor_clearing_the_unit_takes_the_base_length(db):
    """D7: the unit is cleared (Carlos does not attend Centro), so no unit length applies."""
    world, services, centro, ana, _bruno, carlos = await _at_centro(db)
    await _own_length(db, world.tenant.id, ana, services["Retorno"], centro.id, 55)
    await _own_length(db, world.tenant.id, carlos, services["Retorno"], centro.id, 15)
    await _edit(db, world, _req(professional_id=carlos, service="Retorno"))
    row = await reload_appointment(db, world.appointment.id)
    assert (row.professional_id, row.unit_id) == (carlos, None)
    assert _length(row) == timedelta(minutes=40)


async def test_a_time_or_doctor_move_keeps_the_booked_window(db):
    """D6: without a new service the booked length stays, whatever the unit's own length."""
    world, services, centro, ana, bruno, _carlos = await _at_centro(db)
    await _own_length(db, world.tenant.id, ana, services["Consulta"], centro.id, 55)
    await _own_length(db, world.tenant.id, bruno, services["Consulta"], centro.id, 45)
    await _edit(db, world, _req(professional_id=bruno, start_at=world.start_at + timedelta(days=1)))
    row = await reload_appointment(db, world.appointment.id)
    assert _length(row) == timedelta(minutes=30)


# --------------------------------------------------------------------------
# A PAID sinal: a unit that changes the price goes through the engine, which learns the unit
# --------------------------------------------------------------------------


def _spy(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    real = sae.service_change.request_paid_change

    async def _recording(session, **kwargs):
        calls.append(kwargs)
        return await real(session, **kwargs)

    monkeypatch.setattr(sae.service_change, "request_paid_change", _recording)
    return calls


async def _paid_at_centro(db, *, centro_price: str | None = None, paid_cents: int = 6000):
    """Dra. Ana's PAID booking of Consulta (R$ 200,00) at Centro; `centro_price` is Ana's own
    price there. Dr. Bruno attends Centro, Dr. Carlos does not (moving to him clears the unit)."""
    world = await _paid_world(db, professional_name="Dra. Ana")
    ana = world.appointment.professional_id
    bruno = await add_professional(db, world.tenant.id, "Dr. Bruno")
    carlos = await add_professional(db, world.tenant.id, "Dr. Carlos")
    centro = await _unit(db, world.tenant.id, "Centro", ana, bruno)
    await _set_unit(db, world.appointment.id, centro.id)
    async with db() as session:
        # The catalog links "Consulta" by name (`resolve_entries` stamps its service_id).
        consulta = Service(
            id=uuid4(), tenant_id=world.tenant.id, name="Consulta", normalized_name="consulta"
        )
        session.add(consulta)
        await session.flush()
        if centro_price is not None:
            session.add(
                ProfessionalServiceUnitTerms(
                    tenant_id=world.tenant.id,
                    professional_id=ana,
                    service_id=consulta.id,
                    unit_id=centro.id,
                    price=centro_price,
                )
            )
        (await session.scalar(select(PixDeposit))).amount_cents = paid_cents
        await session.commit()
    world.appointment = await reload_appointment(db, world.appointment.id)
    return world, centro, bruno, carlos


async def test_paid_unit_cleared_without_unit_prices_changes_nothing_for_money(
    db, provider, monkeypatch
):
    """D-R1/D-R3: a doctor change that clears the unit asks the engine (with the unit cleared),
    and without per-unit prices the price is the same: nothing for the money - exactly today's
    edit, no `service_change`."""
    world, _centro, _bruno, carlos = await _paid_at_centro(db)
    calls = _spy(monkeypatch)

    outcome = await _edit(db, world, _req(professional_id=carlos))

    [call] = calls
    assert call["pending_edit"]["unit_id"] is None
    assert call["pending_edit"]["professional_id"] == carlos
    assert outcome.service_change is None
    assert [c.label for c in outcome.changes] == ["Médico"]
    row = await reload_appointment(db, world.appointment.id)
    assert (row.professional_id, row.unit_id) == (carlos, None)
    assert await _adjustments(db) == [] and provider.calls == []


async def test_paid_unit_cleared_off_a_unit_price_goes_through_the_engine(
    db, provider, monkeypatch
):
    """Centro charges R$ 250,00 (sinal 30% = R$ 75,00 paid); moving to Dr. Carlos clears the
    unit, so the booking is priced at his base (R$ 200,00): the engine decides, told the unit."""
    world, _centro, _bruno, carlos = await _paid_at_centro(
        db, centro_price="R$ 250,00", paid_cents=7500
    )
    calls = _spy(monkeypatch)

    outcome = await _edit(db, world, _req(professional_id=carlos))

    [call] = calls
    assert call["new_service"] == "Consulta"
    assert "unit_id" in call["pending_edit"] and call["pending_edit"]["unit_id"] is None
    assert call["pending_edit"]["professional_id"] == carlos
    assert outcome.service_change is not None
    assert outcome.service_change.diff_cents == 1500  # 30% of R$ 250,00 -> 30% of R$ 200,00
    row = await reload_appointment(db, world.appointment.id)
    assert (row.professional_id, row.unit_id, row.appointment_type) == (carlos, None, "Consulta")
    assert provider.calls == []  # a decrease never charges


async def test_paid_service_change_hands_the_bookings_unit_to_the_engine(db, provider, monkeypatch):
    world, centro, _bruno, _carlos = await _paid_at_centro(db)
    calls = _spy(monkeypatch)

    outcome = await _edit(db, world, _req(service="Avaliação"))

    [call] = calls
    assert call["pending_edit"]["unit_id"] == centro.id
    assert outcome.service_change.status == "applied_awaiting_payment"  # D-R5
    [adjustment] = await _adjustments(db)
    assert adjustment.pending_edit["unit_id"] == str(centro.id)


async def test_paid_doctor_change_keeping_the_unit_is_priced_at_the_new_doctor_there(
    db, provider, monkeypatch
):
    """D-R3 (replaces R4 "a doctor change alone never re-prices"): Ana charges R$ 250,00 at
    Centro (R$ 75,00 paid); Dr. Bruno, who also attends Centro, has no price of his own there -
    his base R$ 200,00 (sinal R$ 60,00): R$ 15,00 cheaper, asked like any cheaper change (D-R2)."""
    world, centro, bruno, _carlos = await _paid_at_centro(
        db, centro_price="R$ 250,00", paid_cents=7500
    )
    calls = _spy(monkeypatch)

    outcome = await _edit(db, world, _req(professional_id=bruno))

    [call] = calls
    assert (call["pending_edit"]["professional_id"], call["pending_edit"]["unit_id"]) == (
        bruno,
        centro.id,
    )
    assert (outcome.service_change.status, outcome.service_change.diff_cents) == ("asked", 1500)
    row = await reload_appointment(db, world.appointment.id)
    assert (row.professional_id, row.unit_id) == (bruno, centro.id)
    assert provider.calls == []
