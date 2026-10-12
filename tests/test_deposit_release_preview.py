"""Preview of what a cancellation does to a paid deposit (TASK-032 R4, spec 4.4)."""

from datetime import timedelta
from uuid import uuid4

import pytest

from secretaria.models import Appointment, PixDeposit, PixDepositStatus, Tenant
from secretaria.services.payments import deposit_lifecycle
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, seed_world


async def _setup(
    db,  # noqa: F811
    *,
    hours_ahead: float,
    policy: str = "total",
    status=PixDepositStatus.PAID,
    **world_kwargs,
):
    world = await seed_world(db, start_at=NOW + timedelta(hours=hours_ahead), **world_kwargs)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.pix_retention_policy = policy
        session.add(
            PixDeposit(
                id=uuid4(),
                tenant_id=world.tenant.id,
                appointment_id=world.appointment.id,
                patient_id=world.patient.id,
                asaas_payment_id=f"pay-{uuid4()}",
                amount_cents=10000,
                percent_applied=30,
                status=status,
            )
        )
        await session.commit()
    return world


async def _predicted_and_actual(db, world):  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
        predicted = deposit_lifecycle.preview_cancellation_outcome(
            tenant, deposit, appointment, now=NOW
        )
        actual = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment, now=NOW
        )
        return predicted, actual


@pytest.mark.parametrize(
    ("hours_ahead", "policy", "expected"),
    [
        (72, "total", "refund_due"),
        (72, "partial", "refund_due"),
        (2, "total", "retained"),
        (2, "partial", "partial_refund_due"),
    ],
)
async def test_the_preview_matches_what_the_cancellation_really_does(
    db,  # noqa: F811
    hours_ahead,
    policy,
    expected,
):
    world = await _setup(db, hours_ahead=hours_ahead, policy=policy)

    predicted, actual = await _predicted_and_actual(db, world)

    assert predicted == actual == expected


async def test_an_unpaid_charge_is_voided(db):  # noqa: F811
    world = await _setup(db, hours_ahead=2, status=PixDepositStatus.AWAITING)

    predicted, actual = await _predicted_and_actual(db, world)

    # The preview says what is intended. The real void asks the provider first (TASK-017) and,
    # with no provider account to ask (this seed has none), fails safe to "payment_in_flight":
    # an unpaid charge is never released while the provider may still have the money.
    assert predicted == "voided"
    assert actual == "payment_in_flight"


async def test_no_deposit_and_an_already_resolved_one_have_no_outcome(db):  # noqa: F811
    bare = await seed_world(db)
    resolved = await _setup(
        db,
        hours_ahead=2,
        status=PixDepositStatus.CANCELLED_REFUNDED,
        phone_number_id="pnid-2",
        wa_id="5511900000002",
    )

    for world in (bare, resolved):
        predicted, actual = await _predicted_and_actual(db, world)
        assert predicted is None and actual is None


def _deposit(amount: int = 10000) -> PixDeposit:
    return PixDeposit(amount_cents=amount, status=PixDepositStatus.PAID)


def _tenant(policy: str = "total", percent: int = 50) -> Tenant:
    return Tenant(
        clinic_name="Clínica",
        pix_refund_window_hours=24,
        pix_retention_policy=policy,
        pix_partial_refund_percent=percent,
    )


def test_retained_text_says_the_clinic_keeps_the_deposit():
    text = deposit_lifecycle.release_warning_text("retained", _tenant(), _deposit())

    assert "100,00" in text and "24h" in text and "fica com o sinal" in text


def test_partial_text_gives_the_refunded_part():
    text = deposit_lifecycle.release_warning_text(
        "partial_refund_due", _tenant("partial", 40), _deposit()
    )

    assert "40,00" in text and "retido" in text and "painel" in text


def test_refund_due_text_says_the_clinic_returns_the_whole_deposit():
    text = deposit_lifecycle.release_warning_text("refund_due", _tenant(), _deposit())

    assert "100,00" in text and "por inteiro" in text and "estorn" not in text


async def test_a_clinic_cancellation_preview_is_always_refund_due(db):  # noqa: F811
    world = await _setup(db, hours_ahead=2, policy="total")
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
        predicted = deposit_lifecycle.preview_cancellation_outcome(
            tenant,
            deposit,
            appointment,
            now=NOW,
            cancelled_by=deposit_lifecycle.CANCELLED_BY_CLINIC,
        )
        actual = await deposit_lifecycle.on_appointment_cancelled(
            session,
            tenant=tenant,
            appointment=appointment,
            now=NOW,
            cancelled_by=deposit_lifecycle.CANCELLED_BY_CLINIC,
        )
    assert predicted == actual == "refund_due"


def test_an_unknown_outcome_still_warns():
    text = deposit_lifecycle.release_warning_text(None, _tenant(), _deposit())

    assert "100,00" in text and "sinal" in text
