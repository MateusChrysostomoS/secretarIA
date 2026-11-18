"""TASK-085 R / UA1 - a SUSPENDED consultation no longer takes its slot (D-R10, mapa-u.md §1.3).

While the difference Pix of a `mode="suspend"` change is open (or the patient chooses, or the old
consultation is being put back, or its slot was lost), the old consultation's EVENT is off the
agenda, so its LIVE row must not count as busy time either. The two "is this slot taken by a LIVE
row?" queries owned by UA1 add `adjustment_suspension.occupies_slot_clause`:

* `services/agenda.py::shared_busy` - the same person's agenda at another clinic (TASK-083);
* `services/release_keep.py::_consultation_overlaps` - the clinic accepting "Manter horário".

Characterization first: a row without a change, a legacy row (`mode` NULL), an `applied` change
and a `suspend` change that ended (`kept_old`, `applied`, `rebooked`, ...) keep taking the slot,
exactly as today.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update

from secretaria.models import (
    Agenda,
    Appointment,
    AppointmentStatus,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Professional,
)
from secretaria.models.deposit_adjustment import (
    KIND_INCREASE,
    MODE_APPLIED,
    MODE_SUSPEND,
    STATUS_APPLIED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_KEPT_OLD,
    STATUS_OLD_SLOT_LOST,
    STATUS_REBOOKED,
    STATUS_RESTORING,
    DepositAdjustment,
)
from secretaria.services import release_keep
from secretaria.services.agenda import shared_busy
from tests import _reminder_fixtures
from tests._local_agenda import seed_professional, seed_tenant, wire_local_agenda

START = datetime(2030, 3, 8, 13, 0, tzinfo=UTC)
END = START + timedelta(minutes=30)
db = _reminder_fixtures.db


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch):
    wire_local_agenda(monkeypatch, db)


async def _link(db, *professionals):
    async with db() as session:
        row = Agenda(person_key=f"acct-{uuid4().hex[:6]}")
        session.add(row)
        await session.flush()
        for professional in professionals:
            (await session.get(Professional, professional.id)).agenda_id = row.id
        await session.commit()


async def _visit(db, tenant, professional, *, mode=None, status=None):
    """A LIVE consultation of `professional` on [START, END), with a paid sinal and - when
    `status` is given - one difference change of that mode/status."""
    async with db() as session:
        wa_id = f"55119{uuid4().int % 10**8:08d}"
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id=wa_id, name="P")
        session.add(patient)
        await session.flush()
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id=f"evt{uuid4().hex[:8]}",
            appointment_type="Consulta",
            professional_id=professional.id if professional is not None else None,
            start_at=START,
            end_at=END,
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appointment)
        await session.flush()
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=tenant.id,
            appointment_id=appointment.id,
            patient_id=patient.id,
            provider="asaas",
            amount_cents=6000,
            percent_applied=30,
            status=PixDepositStatus.PAID,
        )
        session.add(deposit)
        await session.flush()
        if status is not None:
            session.add(
                DepositAdjustment(
                    tenant_id=tenant.id,
                    deposit_id=deposit.id,
                    appointment_id=appointment.id,
                    kind=KIND_INCREASE,
                    mode=mode,
                    status=status,
                    old_service="Consulta",
                    new_service="Avaliação",
                    diff_cents=3000,
                )
            )
        await session.commit()
        return appointment


async def _world(db):
    """Dr. X at clinic P and at clinic C share one person's agenda."""
    tenant_p = await seed_tenant(db, name="Clínica P")
    tenant_c = await seed_tenant(db, name="Clínica C")
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    x_c = await seed_professional(db, tenant_c, "Dr. X")
    await _link(db, x_p, x_c)
    return tenant_p, tenant_c, x_p, x_c


async def _c_sees(db, x_c) -> list:
    async with db() as session:
        return await shared_busy(session, x_c.id, START, END)


# --------------------------------------------------------------------------
# agenda.shared_busy (the same person at another clinic)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        (None, None),  # no change at all: today
        (None, STATUS_AWAITING_PAYMENT),  # legacy deferred row: the old visit stands, visible
        (MODE_APPLIED, STATUS_AWAITING_PAYMENT),  # D-R5: the change already stood
        (MODE_SUSPEND, STATUS_KEPT_OLD),  # the change ended: the old visit is back
        (MODE_SUSPEND, STATUS_APPLIED),
        (MODE_SUSPEND, STATUS_REBOOKED),
    ],
)
async def test_a_sibling_visit_that_is_not_suspended_still_takes_the_slot(db, mode, status):
    tenant_p, _tenant_c, x_p, x_c = await _world(db)
    await _visit(db, tenant_p, x_p, mode=mode, status=status)

    busy = await _c_sees(db, x_c)

    assert [(w.start, w.end) for w in busy] == [(START, END)]


@pytest.mark.parametrize(
    "status",
    [STATUS_AWAITING_PAYMENT, STATUS_AWAITING_CHOICE, STATUS_RESTORING, STATUS_OLD_SLOT_LOST],
)
async def test_a_suspended_sibling_visit_leaves_the_slot_free(db, status):
    tenant_p, _tenant_c, x_p, x_c = await _world(db)
    await _visit(db, tenant_p, x_p, mode=MODE_SUSPEND, status=status)

    assert await _c_sees(db, x_c) == []


async def test_the_slot_comes_back_when_the_change_ends(db):
    tenant_p, _tenant_c, x_p, x_c = await _world(db)
    visit = await _visit(db, tenant_p, x_p, mode=MODE_SUSPEND, status=STATUS_AWAITING_CHOICE)
    assert await _c_sees(db, x_c) == []

    async with db() as session:
        await session.execute(
            update(DepositAdjustment)
            .where(DepositAdjustment.appointment_id == visit.id)
            .values(status=STATUS_KEPT_OLD)
        )
        await session.commit()

    assert len(await _c_sees(db, x_c)) == 1


async def test_another_suspended_visit_does_not_free_a_live_one_on_the_same_window(db):
    tenant_p, _tenant_c, x_p, x_c = await _world(db)
    await _visit(db, tenant_p, x_p, mode=MODE_SUSPEND, status=STATUS_AWAITING_PAYMENT)
    await _visit(db, tenant_p, x_p)

    assert len(await _c_sees(db, x_c)) == 1


# --------------------------------------------------------------------------
# release_keep._consultation_overlaps (the clinic accepts "Manter horário")
# --------------------------------------------------------------------------


def _released(tenant, professional) -> Appointment:
    """The released row being kept (never in the database query: `id` is excluded)."""
    return Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        professional_id=professional.id if professional is not None else None,
        start_at=START,
        end_at=END,
        status=AppointmentStatus.CANCELLED,
        google_event_id="evt-released",
    )


async def _overlaps(db, released) -> bool:
    async with db() as session:
        return await release_keep._consultation_overlaps(session, released, START, END)


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        (None, None),
        (None, STATUS_AWAITING_PAYMENT),
        (MODE_APPLIED, STATUS_AWAITING_PAYMENT),
        (MODE_SUSPEND, STATUS_KEPT_OLD),
    ],
)
async def test_keep_is_refused_over_a_visit_that_is_not_suspended(db, mode, status):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    await _visit(db, tenant, doctor, mode=mode, status=status)

    assert await _overlaps(db, _released(tenant, doctor)) is True


@pytest.mark.parametrize(
    "status",
    [STATUS_AWAITING_PAYMENT, STATUS_AWAITING_CHOICE, STATUS_RESTORING, STATUS_OLD_SLOT_LOST],
)
async def test_keep_ignores_a_suspended_visit_of_the_same_agenda(db, status):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    await _visit(db, tenant, doctor, mode=MODE_SUSPEND, status=status)

    assert await _overlaps(db, _released(tenant, doctor)) is False


async def test_keep_ignores_a_suspended_visit_on_the_clinic_agenda_too(db):
    tenant = await seed_tenant(db)
    await _visit(db, tenant, None, mode=MODE_SUSPEND, status=STATUS_AWAITING_PAYMENT)
    assert await _overlaps(db, _released(tenant, None)) is False

    await _visit(db, tenant, None)
    assert await _overlaps(db, _released(tenant, None)) is True


async def test_keep_ignores_a_suspended_sibling_visit_at_another_clinic(db):
    tenant_p, tenant_c, x_p, x_c = await _world(db)
    await _visit(db, tenant_p, x_p, mode=MODE_SUSPEND, status=STATUS_AWAITING_CHOICE)
    assert await _overlaps(db, _released(tenant_c, x_c)) is False

    await _visit(db, tenant_p, x_p)
    assert await _overlaps(db, _released(tenant_c, x_c)) is True


async def test_a_suspension_of_another_clinic_never_frees_this_clinics_row(db):
    """Isolation: the predicate matches the adjustment by the ROW's own clinic. An adjustment
    row carrying another clinic's id never hides this clinic's consultation."""
    tenant = await seed_tenant(db)
    other = await seed_tenant(db, name="Outra")
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    visit = await _visit(db, tenant, doctor)
    async with db() as session:
        deposit = PixDeposit(
            id=uuid4(), tenant_id=other.id, appointment_id=None, provider="asaas",
            amount_cents=6000, percent_applied=30, status=PixDepositStatus.PAID,
        )
        session.add(deposit)
        await session.flush()
        session.add(
            DepositAdjustment(
                tenant_id=other.id, deposit_id=deposit.id, appointment_id=visit.id,
                kind=KIND_INCREASE, mode=MODE_SUSPEND, status=STATUS_AWAITING_PAYMENT,
                old_service="Consulta", new_service="Avaliação", diff_cents=3000,
            )
        )
        await session.commit()

    assert await _overlaps(db, _released(tenant, doctor)) is True
