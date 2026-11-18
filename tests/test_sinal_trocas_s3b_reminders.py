"""TASK-085 R / S3b - what S5 left to the workers: a SUSPENDED consultation (a change waiting for
the Pix of its difference, D-R4) gets no reminder, no "não confirmou" warning to the clinic, no
reconcile plan and no reminder-link card. Characterization: a legacy row (`mode` NULL) or a change
already decided hides nothing."""

from datetime import timedelta
from uuid import uuid4

import pytest

from secretaria.models import PixDeposit, PixDepositStatus
from secretaria.models.appointment_reminder import REMINDER_STATUS_SKIPPED
from secretaria.models.deposit_adjustment import (
    MODE_SUSPEND,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_KEPT_OLD,
    DepositAdjustment,
)
from secretaria.schemas.internal import ConversationEntryContext
from secretaria.services import reminder_hooks
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.workers import confirmation_warnings as cw, reminder_engine
from secretaria.workers.portal import open as open_job
from tests._confirmation_warnings import add_warnable_row
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, reminder_rows, wire
from tests._reminders_v2 import NOW, add_reminder, get_reminder, seed_world

# (mode, status): suspended or not.
SUSPENDED = [(MODE_SUSPEND, STATUS_AWAITING_PAYMENT), (MODE_SUSPEND, STATUS_AWAITING_CHOICE)]
NOT_SUSPENDED = [(None, STATUS_AWAITING_PAYMENT), (MODE_SUSPEND, STATUS_KEPT_OLD), (None, None)]


async def _change(db, world, mode, status) -> None:  # noqa: F811
    if status is None:
        return
    async with db() as session:
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=world.tenant.id,
            appointment_id=world.appointment.id,
            patient_id=world.patient.id,
            asaas_payment_id=f"pay-{uuid4()}",
            amount_cents=6000,
            percent_applied=30,
            status=PixDepositStatus.PAID,
        )
        session.add(deposit)
        await session.flush()
        session.add(
            DepositAdjustment(
                id=uuid4(),
                tenant_id=world.tenant.id,
                deposit_id=deposit.id,
                appointment_id=world.appointment.id,
                kind="increase",
                mode=mode,
                status=status,
                old_service="Consulta",
                new_service="Avaliação",
                diff_cents=5000,
                provider_charge_id=f"pay-{uuid4()}",
                expires_at=NOW + timedelta(hours=2),
            )
        )
        await session.commit()


@pytest.mark.parametrize(
    ("mode", "status", "skipped"),
    [
        *[(m, s, True) for m, s in SUSPENDED],
        *[(m, s, False) for m, s in NOT_SUSPENDED],
    ],
)
async def test_the_engine_skips_the_reminder_of_a_suspended_consultation(
    db,  # noqa: F811
    monkeypatch,
    mode,
    status,
    skipped,  # noqa: F811
):
    wire(monkeypatch, db)
    world = await seed_world(db)
    rid = await add_reminder(db, world, kind="day", due_at=NOW - timedelta(minutes=1))
    await _change(db, world, mode, status)

    job = await reminder_engine._prepare(rid, 1, NOW)

    row = await get_reminder(db, rid)
    if skipped:
        assert job is None
        assert (row.status, row.last_error_code) == (
            REMINDER_STATUS_SKIPPED,
            "appointment_suspended",
        )
    else:
        assert job is not None and row.last_error_code is None


@pytest.mark.parametrize(
    ("mode", "status", "warned"),
    [
        *[(m, s, False) for m, s in SUSPENDED],
        *[(m, s, True) for m, s in NOT_SUSPENDED],
    ],
)
async def test_the_clinic_is_not_warned_about_a_suspended_consultation(
    db,  # noqa: F811
    mode,
    status,
    warned,  # noqa: F811
):
    world = await seed_world(db)
    rid = await add_warnable_row(db, world)
    await _change(db, world, mode, status)

    async with db() as session:
        found = await cw.due_candidates(session, NOW)
    assert ([c.reminder_id for c in found] == [rid]) is warned


async def test_the_claim_rechecks_the_suspension(db):  # noqa: F811
    world = await seed_world(db)
    await add_warnable_row(db, world)
    async with db() as session:
        [candidate] = await cw.due_candidates(session, NOW)
    await _change(db, world, MODE_SUSPEND, STATUS_AWAITING_PAYMENT)  # suspended meanwhile

    async with db() as session:
        async with session.begin():
            assert await cw.claim_warning(session, candidate, NOW) is False


async def test_reconcile_waits_for_the_change_to_end(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)
    suspended = await seed_world(db)
    await _change(db, suspended, MODE_SUSPEND, STATUS_AWAITING_CHOICE)
    legacy = await seed_world(db, phone_number_id="pnid-2")
    await _change(db, legacy, None, STATUS_AWAITING_PAYMENT)

    await reminder_hooks.reconcile_missing_reminders(now=NOW)

    assert await reminder_rows(db, suspended.appointment.id) == []
    assert len(await reminder_rows(db, legacy.appointment.id)) == 2


@pytest.mark.parametrize(
    ("mode", "status", "card"),
    [
        (MODE_SUSPEND, STATUS_AWAITING_PAYMENT, False),
        (None, STATUS_AWAITING_PAYMENT, True),
    ],
)
async def test_a_reminder_link_never_presents_a_suspended_consultation(
    db,  # noqa: F811
    monkeypatch,
    mode,
    status,
    card,  # noqa: F811
):
    wire(monkeypatch, db)
    from datetime import UTC, datetime

    world = await seed_world(
        db, channel=CHANNEL_BRAIN_MESSAGE, start_at=datetime.now(UTC) + timedelta(days=3)
    )
    await consent(db, world)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC))
    await _change(db, world, mode, status)

    decision = await open_job._brain_message_entry_decision(
        world.tenant.id,
        world.patient.external_id,
        entry_context=ConversationEntryContext(source="reminder_link", reminder_id=rid),
    )

    assert decision is not None
    assert (decision.appointment_id == world.appointment.id) is card
    if not card:
        assert decision.appointment_id is None  # the ordinary opening instead (S5 hides it)
