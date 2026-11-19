"""TASK-085 / T4 final review - the four findings fixed in one pass.

1. A one-doctor clinic's AI tools receive the turn's catalog as `RuntimeAppointmentType`
   objects (what `load_tenant_config` builds), not the stored dicts. The service-per-unit rule
   (`list_units` with a service, `create_event_at_unit`, the base `create_event`) must read the
   DOCTOR's own dict catalog - never crash, never drop the restriction.
2. A UNIT-only change refused for "no payment account" / a provider refusal carries its label,
   so the patient reads about the unit, not about a "novo serviço".
3. The v2 `get_availability` answer keeps the `unit` label (per window, and at the top level for
   a one-unit doctor); a clinic without units reads exactly what it read before.
4. A paid UNIT-only difference whose consultation was moved to another unit meanwhile applies
   nothing (the patient is not told "agora é ... na Unidade X").
"""

import os

os.environ.setdefault("APP_ENV", "test")

from datetime import UTC, date, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.ai import availability_tool as at, tools as ai_tools  # noqa: E402
from secretaria.ai.tool_output import allowlisted  # noqa: E402
from secretaria.models import Appointment, Tenant, Unit  # noqa: E402
from secretaria.models.deposit_adjustment import (  # noqa: E402
    STATUS_AWAITING_PAYMENT,
    DepositAdjustment,
)
from secretaria.plugins import multi_unit as mu  # noqa: E402
from secretaria.services import agenda as agenda_service, local_calendar  # noqa: E402
from secretaria.services.local_calendar import LocalCalendarService  # noqa: E402
from secretaria.services.payments import accounts, registry, service_change  # noqa: E402
from secretaria.services.payments.provider import (  # noqa: E402
    ChargeState,
    CreatedCharge,
    PaymentStatus,
    ProviderError,
)
from secretaria.services.tenant_config import (  # noqa: E402
    RuntimeAppointmentType,
    TenantRuntimeConfig,
)
from tests import (  # noqa: E402
    test_unit_booking_ai_tools as ai,
    test_unit_terms_money_edit as money,
)
from tests._edit_flow_support import EditCalendar  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402, F401
from tests._reminders_r3 import wire  # noqa: E402
from tests._reminders_v2 import reload_appointment  # noqa: E402
from tests.payments_support import FakeProvider  # noqa: E402

# TASK-085 R: these tests pin the LEGACY deferred mode of the engine (mode NULL rows).
from tests.test_service_change import legacy_deferred_mode  # noqa: E402, F401


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider(state=ChargeState(status=PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(provider_charge_id="pay_diff_1", copy_paste="000201pix")
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


# --------------------------------------------------------------------------------------------
# 1. AI unit tools in a one-doctor clinic (the turn's catalog is RuntimeAppointmentType)
# --------------------------------------------------------------------------------------------


def _runtime(*names: str) -> list[RuntimeAppointmentType]:
    """The turn's effective catalog exactly as `load_tenant_config` hands it to the tools."""
    return [RuntimeAppointmentType(name=n, description=None, duration_min=30) for n in names]


async def _one_doctor_two_units(db):  # noqa: F811
    """Dra. Ana at Centro (Fri) and Zona Sul (Mon); Limpeza only at Zona Sul."""
    tenant, centro, sul, doctors = await ai._seed(db, links={"Dra. Ana": ["Centro", "Zona Sul"]})
    ana, limpeza, retorno = doctors["Dra. Ana"], uuid4(), uuid4()
    entries = [ai._entry("Limpeza", limpeza), ai._entry("Retorno", retorno)]
    await ai._restrict(db, ana, limpeza, sul, entries=entries)
    return tenant, centro, sul, ana


async def test_list_units_for_a_service_reads_the_doctors_catalog(db):  # noqa: F811
    tenant, _centro, _sul, ana = await _one_doctor_two_units(db)
    with ai._context(tenant.id, sole=ana.id, appointment_types=_runtime("Limpeza", "Retorno")):
        restricted = await mu.list_units.ainvoke({"service_name": "limpeza"})
        free_service = await mu.list_units.ainvoke({"service_name": "Retorno"})
        no_service = await mu.list_units.ainvoke({})
    assert [u["name"] for u in restricted["units"]] == ["Zona Sul"]
    assert sorted(u["name"] for u in free_service["units"]) == ["Centro", "Zona Sul"]
    assert sorted(u["name"] for u in no_service["units"]) == ["Centro", "Zona Sul"]


async def test_list_units_unknown_service_keeps_todays_error(db):  # noqa: F811
    tenant, _centro, _sul, ana = await _one_doctor_two_units(db)
    with ai._context(tenant.id, sole=ana.id, appointment_types=_runtime("Limpeza", "Retorno")):
        result = await mu.list_units.ainvoke({"service_name": "Clareamento"})
    assert "error" in result and "Limpeza" in result["error"]


async def test_create_event_at_unit_books_the_allowed_unit_and_refuses_the_other(db):  # noqa: F811
    tenant, _centro, sul, ana = await _one_doctor_two_units(db)
    agenda = ai._Agenda()
    with ai._context(
        tenant.id, calendar=agenda, sole=ana.id, appointment_types=_runtime("Limpeza", "Retorno")
    ):
        refused = await mu.create_event_at_unit.ainvoke(
            {"unit_name": "Centro", "appointment_type": "Limpeza", **ai.WINDOW}
        )
        booked = await mu.create_event_at_unit.ainvoke(
            {
                "unit_name": "Zona Sul",
                "appointment_type": "Limpeza",
                "start": "2026-07-13T15:00:00",
                "end": "2026-07-13T15:30:00",
                "summary": "Consulta",
            }
        )
    assert "Esse serviço não é feito nessa unidade" in refused["error"]
    assert refused["valid_units"] == ["Zona Sul"]
    assert "id" in booked, booked
    [appointment] = await ai._appointments(db)
    assert (appointment.unit_id, appointment.appointment_type) == (sul.id, "Limpeza")


async def test_the_base_create_event_books_at_the_doctors_only_unit_with_a_service(db):  # noqa: F811
    """Same root cause in the base tool: a one-unit doctor whose service IS done there books."""
    tenant, centro, _sul, doctors = await ai._seed(db, links={"Dra. Ana": ["Centro"]})
    ana, limpeza = doctors["Dra. Ana"], uuid4()
    await ai._restrict(db, ana, limpeza, centro, entries=[ai._entry("Limpeza", limpeza)])
    agenda = ai._Agenda()
    with ai._context(
        tenant.id, calendar=agenda, sole=ana.id, appointment_types=_runtime("Limpeza")
    ):
        booked = await ai_tools.create_event.ainvoke({"appointment_type": "Limpeza", **ai.WINDOW})
    assert "id" in booked, booked
    [appointment] = await ai._appointments(db)
    assert appointment.unit_id == centro.id


async def test_the_base_create_event_still_refuses_a_service_not_done_at_the_unit(db):  # noqa: F811
    tenant, centro, sul, doctors = await ai._seed(db, links={"Dra. Ana": ["Centro"]})
    ana, limpeza = doctors["Dra. Ana"], uuid4()
    await ai._restrict(db, ana, limpeza, sul, entries=[ai._entry("Limpeza", limpeza)])
    with ai._context(
        tenant.id, calendar=ai._Agenda(), sole=ana.id, appointment_types=_runtime("Limpeza")
    ):
        refused = await ai_tools.create_event.ainvoke({"appointment_type": "Limpeza", **ai.WINDOW})
    assert "Esse serviço não é feito nessa unidade" in refused["error"]
    assert await ai._appointments(db) == []
    assert centro.id != sul.id


# --------------------------------------------------------------------------------------------
# 2. A unit-only change refused by the payment side names the unit
# --------------------------------------------------------------------------------------------

_SERVICE_WORDS = "novo serviço"


async def test_a_unit_only_change_without_a_payment_account_names_the_unit(
    db,  # noqa: F811
    provider,
    monkeypatch,
):
    world = await money._world(db, centro=("R$ 250,00", None))

    async def _no_account(*_args, **_kwargs):
        return None

    monkeypatch.setattr(accounts, "load_charge_account", _no_account)
    await money._confirm(world, money._unit_edit(world, EditCalendar()))

    row = await reload_appointment(db, world.appointment.id)
    assert row.unit_id == world.sede
    unit_text = service_change.blocked_text("no_payment_account", unit_only=True)
    assert unit_text in money._texts()
    assert not any(_SERVICE_WORDS in text for text in money._texts())


async def test_a_unit_only_change_refused_by_the_provider_carries_its_label(
    db,  # noqa: F811
    provider,
):
    world = await money._world(db, centro=("R$ 250,00", None))
    provider.create_error = ProviderError("invalid", retryable=False)
    async with db() as session:
        result = await _request_unit_change(session, world)
    assert (result.kind, result.reason) == ("blocked", "charge_failed")
    assert result.label == "Consulta na Unidade Centro"


async def test_a_charge_without_code_on_a_unit_only_change_carries_its_label(
    db,  # noqa: F811
    provider,
):
    world = await money._world(db, centro=("R$ 250,00", None))
    provider.created_charge = CreatedCharge(provider_charge_id="pay_x", copy_paste="")
    async with db() as session:
        result = await _request_unit_change(session, world)
    assert (result.kind, result.reason) == ("blocked", "charge_failed")
    assert result.label == "Consulta na Unidade Centro"


async def test_a_service_change_refusal_keeps_todays_shape(db, provider, monkeypatch):  # noqa: F811
    """Characterization: a SERVICE change (no unit) refused for no account has no label and
    reads today's words, byte for byte."""
    from tests import test_service_change as base

    async with db() as session:
        tenant, _patient, appointment, deposit = await base._seed(session)
        await session.commit()

    async def _no_account(*_args, **_kwargs):
        return None

    monkeypatch.setattr(accounts, "load_charge_account", _no_account)
    async with db() as session:
        tenant = await session.get(Tenant, tenant.id)
        appointment = await session.get(Appointment, appointment.id)
        deposit = await session.get(base.PixDeposit, deposit.id)
        result = await service_change.request_service_change(
            session,
            tenant=tenant,
            appointment=appointment,
            deposit=deposit,
            new_service="Avaliação",
            pending_edit=base._edit(appointment, "Avaliação", 60),
            now=base.NOW,
        )
    assert (result.kind, result.reason, result.label) == ("blocked", "no_payment_account", None)
    assert service_change.blocked_text(result.reason, unit_only=False) == (
        "Não foi possível calcular o sinal do novo serviço. Sua consulta continua como estava. "
        "Para trocar, fale com a clínica."
    )


async def _request_unit_change(session, world):
    tenant = await session.get(Tenant, world.tenant.id)
    appointment = await session.get(Appointment, world.appointment.id)
    deposit = await session.scalar(
        select(money.PixDeposit).where(money.PixDeposit.appointment_id == appointment.id)
    )
    edit = money._unit_edit(world, EditCalendar())
    edit.pop("calendar")
    return await service_change.request_service_change(
        session,
        tenant=tenant,
        appointment=appointment,
        deposit=deposit,
        new_service="Consulta",
        pending_edit=edit,
        now=datetime.now(UTC),
    )


# --------------------------------------------------------------------------------------------
# 3. The v2 availability answer keeps the unit label
# --------------------------------------------------------------------------------------------

WEEK = {
    d: [{"start": "08:00", "end": "18:00"}]
    for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


async def _availability(db, monkeypatch, links):  # noqa: F811
    monkeypatch.setattr(local_calendar, "async_session_factory", db)
    monkeypatch.setattr(agenda_service, "async_session_factory", db)
    tenant, _centro, _sul, doctors = await ai._seed(db, links=links)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.business_hours = WEEK
        await session.commit()
    ana = doctors["Dra. Ana"]
    config = ai._config(tenant.id, ana.id)
    config = TenantRuntimeConfig(**{**config.__dict__, "business_hours": WEEK})
    agenda = LocalCalendarService.for_config(config, professional_id=ana.id)
    with ai._context(tenant.id, calendar=agenda, sole=ana.id):
        token = ai_tools._tenant_config_ctx.set(config)
        try:
            return await at.get_availability.ainvoke({})
        finally:
            ai_tools._tenant_config_ctx.reset(token)


async def test_v2_availability_keeps_each_windows_unit(db, monkeypatch):  # noqa: F811
    result = await _availability(db, monkeypatch, {"Dra. Ana": ["Centro", "Zona Sul"]})
    out = allowlisted("get_availability", result)
    assert out["windows"] and all("unit" in w for w in out["windows"])
    assert {w["unit"] for w in out["windows"]} == {"Centro", "Zona Sul"}
    for window in out["windows"]:
        weekday = date.fromisoformat(window["day"]).weekday()
        assert weekday == (4 if window["unit"] == "Centro" else 0)


async def test_v2_availability_keeps_the_one_units_label(db, monkeypatch):  # noqa: F811
    result = await _availability(db, monkeypatch, {"Dra. Ana": ["Centro"]})
    out = allowlisted("get_availability", result)
    assert out["unit"] == "Centro"
    # `weekday` rides on every window since TASK-087 T7 (dia da semana nas listas).
    assert all(set(w) == {"day", "weekday", "start", "end"} for w in out["windows"])


async def test_v2_availability_without_units_is_unchanged(db, monkeypatch):  # noqa: F811
    result = await _availability(db, monkeypatch, {"Dra. Ana": []})
    assert "unit" not in result
    assert all(set(w) == {"day", "weekday", "start", "end"} for w in result["windows"])
    assert allowlisted("get_availability", result) == result


def test_the_unit_label_is_a_plain_scalar_only():
    answer = {
        "windows": [{"day": "2026-10-06", "start": "08:00", "end": "09:00", "unit": "Centro"}],
        "unit": "Centro",
    }
    assert allowlisted("get_availability", answer) == answer
    nested = {"windows": [], "unit": {"who": "x"}}
    assert "unit" not in allowlisted("get_availability", nested)


# --------------------------------------------------------------------------------------------
# 4. A paid unit-only difference whose consultation moved unit meanwhile applies nothing
# --------------------------------------------------------------------------------------------


async def _awaiting_unit_change(db, provider):  # noqa: F811
    world = await money._world(db, centro=("R$ 250,00", None))
    await money._confirm(world, money._unit_edit(world, EditCalendar()))
    [adjustment] = await money._adjustments(db)
    assert adjustment.status == STATUS_AWAITING_PAYMENT
    return world, adjustment


async def _apply(db, world, adjustment_id):  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        adjustment = await session.get(DepositAdjustment, adjustment_id)
        fields = await service_change.apply_paid_adjustment(
            session, tenant, adjustment, now=datetime.now(UTC)
        )
        await session.commit()
    return fields


async def test_a_paid_unit_change_applies_when_the_unit_is_as_asked(db, provider):  # noqa: F811
    world, adjustment = await _awaiting_unit_change(db, provider)
    fields = await _apply(db, world, adjustment.id)
    assert fields is not None
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.unit_id) == ("Consulta", world.centro)


async def test_a_paid_unit_change_onto_a_unit_moved_meanwhile_applies_nothing(db, provider):  # noqa: F811
    world, adjustment = await _awaiting_unit_change(db, provider)
    async with db() as session:
        norte = Unit(id=uuid4(), tenant_id=world.tenant.id, name="Norte", address="Rua N, 1")
        session.add(norte)
        row = await session.get(Appointment, world.appointment.id)
        row.unit_id = norte.id
        before_end = row.end_at
        await session.commit()

    assert await _apply(db, world, adjustment.id) is None
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.unit_id) == ("Consulta", norte.id)
    assert row.end_at == before_end


async def test_a_paid_unit_change_already_at_the_asked_unit_still_applies(db, provider):  # noqa: F811
    """Decision: staff moved it to the very unit the patient paid for - the change holds."""
    world, adjustment = await _awaiting_unit_change(db, provider)
    async with db() as session:
        row = await session.get(Appointment, world.appointment.id)
        row.unit_id = world.centro
        await session.commit()
    assert await _apply(db, world, adjustment.id) is not None
    row = await reload_appointment(db, world.appointment.id)
    assert row.unit_id == world.centro


async def test_a_paid_service_change_onto_a_moved_unit_still_applies(db, provider):  # noqa: F811
    """Characterization: a SERVICE change keeps today's rule - the service changes, the staff's
    unit stays (tests/test_service_change_units.py has the same for the hub world)."""
    world, adjustment = await _awaiting_unit_change(db, provider)
    async with db() as session:
        stored = await session.get(DepositAdjustment, adjustment.id)
        stored.pending_edit = {**stored.pending_edit, "appointment_type": "Retorno"}
        norte = Unit(id=uuid4(), tenant_id=world.tenant.id, name="Norte", address="Rua N, 1")
        session.add(norte)
        row = await session.get(Appointment, world.appointment.id)
        row.unit_id = norte.id
        await session.commit()
    assert await _apply(db, world, adjustment.id) is not None
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.unit_id) == ("Retorno", norte.id)
