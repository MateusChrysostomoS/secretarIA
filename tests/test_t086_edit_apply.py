"""TASK-086 T4b (A18) - "Alterar Dados" re-checks the convênio matrix inside the row lock.

The card was confirmed against the turn's snapshot; the clinic may have refused the cell since.
`apply_appointment_edit` reads the matrix straight from the database under the existing
`with_for_update` and refuses (None, nothing written) when the service, the doctor, the unit or
the convênio changed into a refused cell. A pure reschedule is never refused. Both modes.

Real in-memory SQLite (tests/_reminder_fixtures.db). No PII: synthetic names only.
"""

# ruff: noqa: F811

import os

os.environ.setdefault("APP_ENV", "test")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Professional,
    ProfessionalInsurancePlan,
    Service,
    Tenant,
    TenantInsurancePlan,
    Unit,
)
from secretaria.models.insurance_acceptance_exception import (  # noqa: E402
    InsuranceAcceptanceException,
)
from secretaria.services.appointment_edit_write import row_draft  # noqa: E402
from secretaria.services.payments import service_change  # noqa: E402
from secretaria.workers.shared import appointment_edit_apply as apply_mod  # noqa: E402
from secretaria.workers.shared.appointment_edit_apply import apply_appointment_edit  # noqa: E402
from tests._reminder_fixtures import db  # noqa: F401, E402

START = datetime.now(UTC).replace(microsecond=0) + timedelta(days=5)


class World:
    pass


async def _world(maker, mode="shared") -> World:
    w = World()
    async with maker() as session:
        w.tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinica Teste",
            phone_number_id=str(uuid4())[:12],
            insurance_mode=mode,
            timezone="America/Sao_Paulo",
        )
        session.add(w.tenant)
        await session.flush()
        w.prof = Professional(tenant_id=w.tenant.id, name="Dra. Teste", is_active=True)
        w.consulta = Service(tenant_id=w.tenant.id, name="Consulta", normalized_name="consulta")
        w.retorno = Service(tenant_id=w.tenant.id, name="Retorno", normalized_name="retorno")
        w.centro = Unit(tenant_id=w.tenant.id, name="Centro")
        w.sul = Unit(tenant_id=w.tenant.id, name="Sul")
        w.unimed = TenantInsurancePlan(tenant_id=w.tenant.id, custom_name="Unimed")
        w.amil = TenantInsurancePlan(tenant_id=w.tenant.id, custom_name="Amil")
        w.patient = Patient(id=uuid4(), tenant_id=w.tenant.id, wa_id="5500000000001", name="P")
        session.add_all(
            [w.prof, w.consulta, w.retorno, w.centro, w.sul, w.unimed, w.amil, w.patient]
        )
        await session.flush()
        w.prof_unimed = ProfessionalInsurancePlan(
            professional_id=w.prof.id, tenant_id=w.tenant.id, custom_name="Unimed"
        )
        w.prof_amil = ProfessionalInsurancePlan(
            professional_id=w.prof.id, tenant_id=w.tenant.id, custom_name="Amil"
        )
        w.appt = Appointment(
            id=uuid4(),
            tenant_id=w.tenant.id,
            patient_id=w.patient.id,
            google_event_id="evt-1",
            appointment_type="Consulta",
            start_at=START,
            end_at=START + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
            professional_id=w.prof.id,
            unit_id=w.centro.id,
            insurance="Unimed",
        )
        session.add_all([w.prof_unimed, w.prof_amil, w.appt])
        await session.commit()
    return w


async def _refuse(maker, w: World, service, unit, plan) -> None:
    """The clinic refuses a cell AFTER the patient saw the card (the race)."""
    async with maker() as session:
        independent = isinstance(plan, ProfessionalInsurancePlan)
        session.add(
            InsuranceAcceptanceException(
                tenant_id=w.tenant.id,
                professional_id=w.prof.id if independent else None,
                service_id=service.id,
                unit_id=unit.id if unit is not None else None,
                tenant_plan_id=None if independent else plan.id,
                professional_plan_id=plan.id if independent else None,
            )
        )
        await session.commit()


def _edit(w: World, row, **changes) -> dict:
    """The router's edit dict (`_apply_confirm`) for the row, with `changes` applied."""
    edit = {
        "appointment_id": row.id,
        "old_google_event_id": row.google_event_id,
        "google_event_id": row.google_event_id,
        "google_event_link": None,
        "appointment_type": row.appointment_type,
        "professional_id": row.professional_id,
        "old_professional_id": row.professional_id,
        "old_google_calendar_source": row.google_calendar_source,
        "insurance": row.insurance,
        "attendee_name": None,
        "start_at": START,
        "end_at": START + timedelta(minutes=30),
        "time_changed": False,
        "doctor_changed": False,
        "calendar_changed": False,
        "original": dict(row_draft(row, w.tenant.timezone).original),
        "calendar": None,
        "patient_name": "P",
        "calendar_deferred": False,
        "service_id": w.consulta.id,
    }
    edit.update(changes)
    return edit


async def _apply(maker, w: World, **changes):
    async with maker() as session:
        row = await session.get(Appointment, w.appt.id)
        tenant = await session.get(Tenant, w.tenant.id)
        applied = await apply_appointment_edit(
            session,
            tenant=tenant,
            tenant_id=w.tenant.id,
            patient_id=w.patient.id,
            edit=_edit(w, row, **changes),
        )
        if applied is not None:
            await session.commit()
        return applied


async def _row(maker, w: World) -> Appointment:
    async with maker() as session:
        return await session.get(Appointment, w.appt.id)


async def test_without_any_exception_a_convenio_change_is_applied(db):
    w = await _world(db)
    assert await _apply(db, w, insurance="Amil") is not None
    assert (await _row(db, w)).insurance == "Amil"


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_a_convenio_refused_after_the_card_is_refused_and_nothing_written(db, mode):
    w = await _world(db, mode)
    plan = w.amil if mode == "shared" else w.prof_amil
    await _refuse(db, w, w.consulta, w.centro, plan)

    assert await _apply(db, w, insurance="Amil") is None
    row = await _row(db, w)
    assert row.insurance == "Unimed"
    assert row.insurance_plan_id is None and row.insurance_professional_plan_id is None


async def test_a_service_change_into_a_refused_cell_is_refused(db):
    w = await _world(db)
    await _refuse(db, w, w.retorno, w.centro, w.unimed)
    assert await _apply(db, w, appointment_type="Retorno", service_id=w.retorno.id) is None
    assert (await _row(db, w)).appointment_type == "Consulta"


async def test_a_unit_change_into_a_refused_cell_is_refused(db):
    w = await _world(db)
    await _refuse(db, w, w.consulta, w.sul, w.unimed)
    assert await _apply(db, w, unit_id=w.sul.id) is None
    assert (await _row(db, w)).unit_id == w.centro.id


async def test_a_refusal_elsewhere_does_not_block_the_change(db):
    w = await _world(db)
    await _refuse(db, w, w.consulta, w.sul, w.amil)  # Amil refused at Sul, not at Centro
    assert await _apply(db, w, insurance="Amil") is not None
    assert (await _row(db, w)).insurance == "Amil"


async def test_a_pure_reschedule_is_never_refused(db):
    w = await _world(db)
    await _refuse(db, w, w.consulta, w.centro, w.unimed)  # the booking's own cell
    new_start = START + timedelta(days=1)
    applied = await _apply(
        db,
        w,
        start_at=new_start,
        end_at=new_start + timedelta(minutes=30),
        time_changed=True,
    )
    assert applied is not None


async def test_a_paid_service_change_into_a_refused_cell_is_refused_before_the_money(
    db,
    monkeypatch,
):
    w = await _world(db)
    async with db() as session:
        session.add(
            PixDeposit(
                id=uuid4(),
                tenant_id=w.tenant.id,
                appointment_id=w.appt.id,
                patient_id=w.patient.id,
                asaas_payment_id=f"pay-{uuid4()}",
                amount_cents=10000,
                percent_applied=30,
                status=PixDepositStatus.PAID,
            )
        )
        await session.commit()
    await _refuse(db, w, w.retorno, w.centro, w.unimed)

    async def never(*_a, **_kw):
        raise AssertionError("the money must not be decided for a refused change")

    monkeypatch.setattr(service_change, "request_service_change", never)
    monkeypatch.setattr(apply_mod.service_change, "request_service_change", never)
    applied = await _apply(
        db, w, appointment_type="Retorno", service_id=w.retorno.id, calendar_deferred=True
    )
    assert applied is None


async def test_the_refusal_is_logged_with_ids_only(db, monkeypatch):
    w = await _world(db)
    await _refuse(db, w, w.consulta, w.centro, w.amil)
    seen: list = []
    monkeypatch.setattr(apply_mod, "log_refusal", lambda event, **ids: seen.append((event, ids)))
    assert await _apply(db, w, insurance="Amil") is None
    [(event, ids)] = seen
    assert event == "appointment_edit_insurance_refused"
    assert "Amil" not in str(ids) and "Unimed" not in str(ids)
    assert ids["appointment_id"] == w.appt.id
