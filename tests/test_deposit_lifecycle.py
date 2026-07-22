"""Tests for services/payments/deposit_lifecycle.py — the Pix deposit money brain.

In-memory-sqlite pattern established by tests/test_reminders_plugin.py: a real
DB, monkeypatched in place via a `db` fixture. `_asaas_client_for` is
monkeypatched with a fake recording client (never a real network call, and
never AsaasClient's own httpx plumbing — that is test_asaas_client.py's job).
`WhatsAppClient` is faked the same way test_reminders_plugin.py does it.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant  # noqa: E402
from secretaria.models.pix_deposit import PixDeposit, PixDepositStatus  # noqa: E402
from secretaria.services import tenant_config  # noqa: E402
from secretaria.services.payments import deposit_lifecycle  # noqa: E402

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeAsaasClient:
    """Records every call; every failure mode is injectable per-instance."""

    def __init__(
        self,
        *,
        customer_id="cus_fake",
        payment_id="pay_fake",
        qr_payload="00020126copiaecola6304ABCD",
        create_customer_error=None,
        create_payment_error=None,
        qr_error=None,
        refund_error=None,
        delete_error=None,
    ):
        self.customer_id = customer_id
        self.payment_id = payment_id
        self.qr_payload = qr_payload
        self.create_customer_error = create_customer_error
        self.create_payment_error = create_payment_error
        self.qr_error = qr_error
        self.refund_error = refund_error
        self.delete_error = delete_error
        self.calls: list[tuple] = []

    async def create_customer(self, name, mobile_phone):
        self.calls.append(("create_customer", name, mobile_phone))
        if self.create_customer_error:
            raise self.create_customer_error
        return self.customer_id

    async def create_pix_payment(
        self, customer_id, value_cents, external_reference, description, due_date
    ):
        self.calls.append(
            ("create_pix_payment", customer_id, value_cents, external_reference, due_date)
        )
        if self.create_payment_error:
            raise self.create_payment_error
        return {"id": self.payment_id, "status": "PENDING"}

    async def get_pix_qr(self, payment_id):
        self.calls.append(("get_pix_qr", payment_id))
        if self.qr_error:
            raise self.qr_error
        return {"payload": self.qr_payload, "encodedImage": "b64", "expirationDate": "2026-07-21"}

    async def refund_payment(self, payment_id, value_cents):
        self.calls.append(("refund_payment", payment_id, value_cents))
        if self.refund_error:
            raise self.refund_error
        return {"id": payment_id, "status": "REFUNDED"}

    async def delete_payment(self, payment_id):
        self.calls.append(("delete_payment", payment_id))
        if self.delete_error:
            raise self.delete_error
        return {"deleted": True}


class _FakeWhatsAppClient:
    """Records constructed instances + sends; installed in place of the real client."""

    created: list["_FakeWhatsAppClient"] = []

    def __init__(self, access_token=None, phone_number_id=None):
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self.sent: list[tuple] = []
        _FakeWhatsAppClient.created.append(self)

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls(access_token=waba_token, phone_number_id=tenant.phone_number_id)

    async def send_text_message(self, to, body):
        self.sent.append(("text", to, body))
        return {"messages": [{"id": "wamid.test"}]}


class _FailingWhatsAppClient:
    """Every send raises — used to prove a send failure never blocks a booking."""

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        raise RuntimeError("whatsapp down")


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, **kwargs) -> FakeAsaasClient:
    client = FakeAsaasClient(**kwargs)
    monkeypatch.setattr(deposit_lifecycle, "_asaas_client_for", lambda api_key: client)
    return client


def _install_fake_whatsapp(monkeypatch: pytest.MonkeyPatch) -> type[_FakeWhatsAppClient]:
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(deposit_lifecycle, "WhatsAppClient", _FakeWhatsAppClient)
    return _FakeWhatsAppClient


# --------------------------------------------------------------------------
# DB fixture + seed helpers
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed(
    session: AsyncSession,
    *,
    pix_deposit_enabled: bool = True,
    pix_deposit_percent: int = 30,
    pix_refund_window_hours: int = 24,
    pix_retention_policy: str = "total",
    pix_partial_refund_percent: int = 50,
    pix_reschedule_limit: int = 2,
    price: str | None = "R$ 200,00",
    appointment_type: str = "Consulta",
    start_at: datetime | None = None,
    asaas_api_key: str | None = "fake-asaas-key",
    patient_asaas_customer_id: str | None = None,
) -> tuple[Tenant, Patient, Appointment]:
    """Tenant + patient + one appointment, all in the SAME open session."""
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinic",
        phone_number_id=str(uuid4())[:12],
        is_active=True,
        pix_deposit_enabled=pix_deposit_enabled,
        pix_deposit_percent=pix_deposit_percent,
        pix_refund_window_hours=pix_refund_window_hours,
        pix_retention_policy=pix_retention_policy,
        pix_partial_refund_percent=pix_partial_refund_percent,
        pix_reschedule_limit=pix_reschedule_limit,
        appointment_types=[
            {
                "name": appointment_type,
                "duration_min": 30,
                "is_active": True,
                **({"price": price} if price is not None else {}),
            }
        ],
    )
    patient = Patient(
        id=uuid4(),
        tenant_id=tenant.id,
        wa_id="5511999999999",
        name="Maria",
        asaas_customer_id=patient_asaas_customer_id,
    )
    session.add_all([tenant, patient])
    await session.flush()
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=patient.id,
        google_event_id="evt-1",
        appointment_type=appointment_type,
        start_at=start_at,
        phone=patient.wa_id,
        status=AppointmentStatus.SCHEDULED,
    )
    session.add(appointment)
    if asaas_api_key:
        await tenant_config.set_asaas_api_key(session, tenant.id, asaas_api_key)
    await session.flush()
    return tenant, patient, appointment


async def _seed_deposit(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    *,
    status: PixDepositStatus = PixDepositStatus.PAID,
    amount_cents: int = 6000,
    asaas_payment_id: str | None = "pay_existing",
    reschedule_count: int = 0,
) -> PixDeposit:
    deposit = PixDeposit(
        tenant_id=tenant.id,
        appointment_id=appointment.id,
        patient_id=appointment.patient_id,
        asaas_payment_id=asaas_payment_id,
        amount_cents=amount_cents,
        percent_applied=tenant.pix_deposit_percent,
        status=status,
        reschedule_count=reschedule_count,
    )
    session.add(deposit)
    await session.flush()
    return deposit


# --------------------------------------------------------------------------
# maybe_create_deposit — guards
# --------------------------------------------------------------------------


async def test_guard_disabled_returns_none_no_row(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session, pix_deposit_enabled=False)
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
    assert deposit is None
    async with db() as session2:
        rows = (await session2.scalars(select(PixDeposit))).all()
    assert rows == []


async def test_guard_no_patient_returns_none(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=None, appointment=appointment, waba_token="tok"
        )
    assert deposit is None


async def test_guard_no_api_key_returns_none(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session, asaas_api_key=None)
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
    assert deposit is None


async def test_guard_unparseable_price_returns_none(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session, price="a combinar")
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
    assert deposit is None


async def test_guard_no_matching_appointment_type_returns_none(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session, appointment_type="Retorno")
        appointment.appointment_type = "Nome Diferente"  # doesn't match the catalog entry
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
    assert deposit is None


async def test_guard_zero_amount_returns_none(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session, pix_deposit_percent=0)
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
    assert deposit is None
    async with db() as session2:
        rows = (await session2.scalars(select(PixDeposit))).all()
    assert rows == []


# --------------------------------------------------------------------------
# maybe_create_deposit — happy path + partial-failure resilience
# --------------------------------------------------------------------------


async def test_happy_path_creates_deposit_and_sends_message(db, monkeypatch):
    fake = _install_fake_client(monkeypatch, payment_id="pay_new", qr_payload="COPYPASTE123")
    fake_wa = _install_fake_whatsapp(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(
            session, price="R$ 200,00", pix_deposit_percent=30
        )
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="waba-tok"
        )
        await session.commit()

    assert deposit is not None
    assert deposit.amount_cents == 6000  # R$200,00 * 30% = R$60,00
    assert deposit.percent_applied == 30
    assert deposit.status == PixDepositStatus.AWAITING
    assert deposit.asaas_payment_id == "pay_new"
    assert deposit.pix_copy_paste == "COPYPASTE123"

    async with db() as session2:
        rows = (await session2.scalars(select(PixDeposit))).all()
    assert len(rows) == 1

    assert len(fake_wa.created) == 1
    kind, to, body = fake_wa.created[0].sent[0]
    assert kind == "text"
    assert to == "5511999999999"
    assert "COPYPASTE123" in body
    assert "R$ 60,00" in body

    assert ("create_customer", "Maria", "5511999999999") in fake.calls


async def test_reuses_existing_asaas_customer_id(db, monkeypatch):
    fake = _install_fake_client(monkeypatch)
    _install_fake_whatsapp(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(
            session, patient_asaas_customer_id="cus_existing"
        )
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
        await session.commit()

    assert deposit is not None
    assert all(call[0] != "create_customer" for call in fake.calls)
    payment_call = next(call for call in fake.calls if call[0] == "create_pix_payment")
    assert payment_call[1] == "cus_existing"


async def test_create_payment_failure_no_row_persisted(db, monkeypatch):
    _install_fake_client(monkeypatch, create_payment_error=RuntimeError("asaas down"))
    _install_fake_whatsapp(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
        await session.commit()

    assert deposit is None
    async with db() as session2:
        rows = (await session2.scalars(select(PixDeposit))).all()
    assert rows == []


async def test_create_customer_failure_no_row_persisted(db, monkeypatch):
    _install_fake_client(monkeypatch, create_customer_error=RuntimeError("asaas down"))
    _install_fake_whatsapp(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
        await session.commit()

    assert deposit is None  # never raised into the caller
    async with db() as session2:
        rows = (await session2.scalars(select(PixDeposit))).all()
    assert rows == []


async def test_qr_failure_still_persists_deposit_without_copy_paste(db, monkeypatch):
    _install_fake_client(monkeypatch, qr_error=RuntimeError("qr down"))
    _install_fake_whatsapp(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
        await session.commit()

    assert deposit is not None  # a REAL Asaas payment now exists — must stay resolvable
    assert deposit.pix_copy_paste is None
    assert deposit.asaas_payment_id is not None


async def test_whatsapp_send_failure_does_not_raise_and_booking_unaffected(db, monkeypatch):
    _install_fake_client(monkeypatch)
    monkeypatch.setattr(deposit_lifecycle, "WhatsAppClient", _FailingWhatsAppClient)
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
        await session.commit()

    assert deposit is not None  # never raised


async def test_waba_token_none_is_fetched_via_tenant_config(db, monkeypatch):
    _install_fake_client(monkeypatch)
    fake_wa = _install_fake_whatsapp(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        await tenant_config.set_waba_token(session, tenant.id, "decrypted-token")
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token=None
        )
        await session.commit()

    assert deposit is not None
    assert fake_wa.created[0]._access_token == "decrypted-token"


# --------------------------------------------------------------------------
# on_appointment_cancelled
# --------------------------------------------------------------------------


async def test_cancel_outside_window_full_refund(db, monkeypatch):
    fake = _install_fake_client(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(
            session, pix_refund_window_hours=24, start_at=datetime.now(UTC) + timedelta(hours=48)
        )
        deposit = await _seed_deposit(
            session, tenant, appointment, status=PixDepositStatus.PAID, amount_cents=6000
        )
        await session.commit()
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment
        )
        await session.commit()

    assert outcome == "refunded"
    assert fake.calls == [("refund_payment", "pay_existing", None)]
    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
    assert refreshed.status == PixDepositStatus.CANCELLED_REFUNDED
    assert refreshed.refunded_amount_cents == 6000
    assert refreshed.resolved_at is not None


async def test_cancel_inside_window_total_retention_no_psp_call(db, monkeypatch):
    fake = _install_fake_client(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(
            session,
            pix_refund_window_hours=24,
            pix_retention_policy="total",
            start_at=datetime.now(UTC) + timedelta(hours=2),
        )
        deposit = await _seed_deposit(
            session, tenant, appointment, status=PixDepositStatus.PAID, amount_cents=6000
        )
        await session.commit()
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment
        )
        await session.commit()

    assert outcome == "retained"
    assert fake.calls == []  # NO PSP call for total retention
    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
    assert refreshed.status == PixDepositStatus.CANCELLED_RETAINED
    assert refreshed.refunded_amount_cents == 0


async def test_cancel_inside_window_partial_refund_amount_math(db, monkeypatch):
    fake = _install_fake_client(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(
            session,
            pix_refund_window_hours=24,
            pix_retention_policy="partial",
            pix_partial_refund_percent=40,
            start_at=datetime.now(UTC) + timedelta(hours=2),
        )
        deposit = await _seed_deposit(
            session, tenant, appointment, status=PixDepositStatus.PAID, amount_cents=6000
        )
        await session.commit()
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment
        )
        await session.commit()

    assert outcome == "partial_refund"
    assert fake.calls == [("refund_payment", "pay_existing", 2400)]  # 6000 * 40% = 2400
    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
    assert refreshed.status == PixDepositStatus.CANCELLED_RETAINED
    assert refreshed.refunded_amount_cents == 2400


async def test_cancel_awaiting_deposit_voided(db, monkeypatch):
    fake = _install_fake_client(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(
            session, start_at=datetime.now(UTC) + timedelta(hours=48)
        )
        deposit = await _seed_deposit(
            session, tenant, appointment, status=PixDepositStatus.AWAITING
        )
        await session.commit()
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment
        )
        await session.commit()

    assert outcome == "voided"
    assert fake.calls == [("delete_payment", "pay_existing")]
    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
    assert refreshed.status == PixDepositStatus.EXPIRED
    assert refreshed.resolved_at is not None


async def test_cancel_refund_api_failure_stays_paid(db, monkeypatch):
    _install_fake_client(monkeypatch, refund_error=RuntimeError("asaas down"))
    async with db() as session:
        tenant, patient, appointment = await _seed(
            session, pix_refund_window_hours=24, start_at=datetime.now(UTC) + timedelta(hours=48)
        )
        deposit = await _seed_deposit(
            session, tenant, appointment, status=PixDepositStatus.PAID, amount_cents=6000
        )
        await session.commit()
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment
        )
        await session.commit()

    assert outcome == "refund_failed"
    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
    assert refreshed.status == PixDepositStatus.PAID  # never pretend
    assert refreshed.refunded_amount_cents is None


async def test_cancel_no_deposit_returns_none(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        await session.commit()
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment
        )
    assert outcome is None


async def test_cancel_already_resolved_returns_none(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        await _seed_deposit(
            session, tenant, appointment, status=PixDepositStatus.CANCELLED_REFUNDED
        )
        await session.commit()
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment
        )
    assert outcome is None


# --------------------------------------------------------------------------
# on_no_show
# --------------------------------------------------------------------------


async def test_no_show_awaiting_voided(db, monkeypatch):
    fake = _install_fake_client(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        deposit = await _seed_deposit(
            session, tenant, appointment, status=PixDepositStatus.AWAITING
        )
        await session.commit()
        outcome = await deposit_lifecycle.on_no_show(
            session, tenant=tenant, appointment=appointment
        )
        await session.commit()

    assert outcome == "voided"
    assert fake.calls == [("delete_payment", "pay_existing")]
    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
    assert refreshed.status == PixDepositStatus.EXPIRED


async def test_no_show_paid_retained(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        deposit = await _seed_deposit(
            session, tenant, appointment, status=PixDepositStatus.PAID, amount_cents=6000
        )
        await session.commit()
        outcome = await deposit_lifecycle.on_no_show(
            session, tenant=tenant, appointment=appointment
        )
        await session.commit()

    assert outcome == "retained"
    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
    assert refreshed.status == PixDepositStatus.NO_SHOW_RETAINED
    assert refreshed.refunded_amount_cents == 0


async def test_no_show_no_deposit_returns_none(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        await session.commit()
        outcome = await deposit_lifecycle.on_no_show(
            session, tenant=tenant, appointment=appointment
        )
    assert outcome is None


# --------------------------------------------------------------------------
# register_reschedule
# --------------------------------------------------------------------------


async def test_register_reschedule_no_deposit_returns_true_zero(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        await session.commit()
        allowed, count = await deposit_lifecycle.register_reschedule(
            session, tenant=tenant, appointment=appointment
        )
    assert (allowed, count) == (True, 0)


async def test_register_reschedule_increments(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session, pix_reschedule_limit=2)
        deposit = await _seed_deposit(session, tenant, appointment, reschedule_count=0)
        await session.commit()
        allowed, count = await deposit_lifecycle.register_reschedule(
            session, tenant=tenant, appointment=appointment
        )
        await session.commit()

    assert (allowed, count) == (True, 1)
    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
    assert refreshed.reschedule_count == 1


async def test_register_reschedule_blocks_at_limit(db):
    async with db() as session:
        tenant, patient, appointment = await _seed(session, pix_reschedule_limit=2)
        deposit = await _seed_deposit(session, tenant, appointment, reschedule_count=2)
        await session.commit()
        allowed, count = await deposit_lifecycle.register_reschedule(
            session, tenant=tenant, appointment=appointment
        )
        await session.commit()

    assert (allowed, count) == (False, 2)
    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
    assert refreshed.reschedule_count == 2  # not incremented


# --------------------------------------------------------------------------
# cancellation_notice
# --------------------------------------------------------------------------


def test_cancellation_notice_texts_for_each_outcome():
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", pix_refund_window_hours=24)
    deposit = PixDeposit(
        id=uuid4(),
        tenant_id=tenant.id,
        appointment_id=uuid4(),
        amount_cents=6000,
        percent_applied=30,
        refunded_amount_cents=2400,
    )

    refunded = deposit_lifecycle.cancellation_notice("refunded", tenant, deposit)
    assert refunded is not None and "estorno" in refunded.lower()

    partial = deposit_lifecycle.cancellation_notice("partial_refund", tenant, deposit)
    assert partial is not None and "R$ 24,00" in partial

    retained = deposit_lifecycle.cancellation_notice("retained", tenant, deposit)
    assert retained is not None and "24h" in retained

    failed = deposit_lifecycle.cancellation_notice("refund_failed", tenant, deposit)
    assert failed is not None

    assert deposit_lifecycle.cancellation_notice("voided", tenant, deposit) is None
    assert deposit_lifecycle.cancellation_notice(None, tenant, deposit) is None
