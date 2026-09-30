"""Guards that keep the `after` machinery out of a `before` booking (spec sections 5.2, 9).

The post-booking hook of a `before` clinic's appointment must never ask for a SECOND deposit
(the paid one already exists), an appointment that already has a deposit row never gets another,
and the arq job hands its Redis pool to the promotion (it fires the post-booking hooks with it).
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from secretaria.models import Appointment, AppointmentStatus, PixDeposit
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services.payments import deposit_lifecycle, registry
from secretaria.services.payments.provider import CreatedCharge
from secretaria.workers import payments_tasks
from tests.before_support import make_world
from tests.payments_support import FakeProvider, make_sessionmaker


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


class _NoWhatsApp:
    """Nothing in these tests may reach Meta: a send that slips through fails loudly."""

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        raise AssertionError("a WhatsApp client was built: the guard did not stop the charge")


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider()
    fake.created_charge = CreatedCharge(provider_charge_id="pay_x", copy_paste="COPY")
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    monkeypatch.setattr(deposit_lifecycle, "WhatsAppClient", _NoWhatsApp)
    return fake


async def _appointment(session, world):
    appointment = Appointment(
        id=uuid4(),
        tenant_id=world.tenant_id,
        patient_id=world.patient.id,
        google_event_id="evt-1",
        appointment_type="Consulta",
        phone=world.patient.wa_id,
        status=AppointmentStatus.SCHEDULED,
        start_at=datetime(2030, 3, 4, 13, 0, tzinfo=UTC),
    )
    session.add(appointment)
    await session.commit()
    return appointment


async def test_the_after_hook_never_charges_a_before_booking_a_second_time(db, provider):
    async with db() as session:
        world = await make_world(session, timing="before")
        appointment = await _appointment(session, world)
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session,
            tenant=world.tenant,
            patient=world.patient,
            appointment=appointment,
            waba_token="wt",
        )
    assert deposit is None
    assert provider.called("create_pix_charge") == 0


async def test_an_appointment_that_already_has_a_deposit_is_never_charged_again(db, provider):
    async with db() as session:
        world = await make_world(session, timing="after")
        appointment = await _appointment(session, world)
        session.add(
            PixDeposit(
                tenant_id=world.tenant_id,
                appointment_id=appointment.id,
                patient_id=world.patient.id,
                amount_cents=6000,
                percent_applied=30,
                status=PixDepositStatus.PAID,
            )
        )
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session,
            tenant=world.tenant,
            patient=world.patient,
            appointment=appointment,
            waba_token="wt",
        )
    assert deposit is None
    assert provider.called("create_pix_charge") == 0


async def test_the_fulfillment_job_hands_its_redis_pool_to_the_promotion(db, monkeypatch):
    seen = {}

    async def fake_fulfill(session, *, deposit_id, redis=None, now=None):
        seen["redis"] = redis
        return "promoted"

    monkeypatch.setattr(payments_tasks, "async_session_factory", db)
    monkeypatch.setattr(payments_tasks.fulfillment, "fulfill_paid_charge", fake_fulfill)
    pool = object()
    await payments_tasks.fulfill_paid_charge_job({"redis": pool}, str(uuid4()))
    assert seen["redis"] is pool
