"""`maybe_create_deposit` behind the provider abstraction (spec sections 5.1, 6.1, 6.4).

The `after` timing is today's behaviour - the consultation exists, the Pix is asked for next -
now reaching the PSP through `PaymentProvider` and recording the provider-neutral columns.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    PatientPaymentCustomer,
    Tenant,
)
from secretaria.models.pix_deposit import (
    DEPOSIT_TIMING_AFTER,
    FULFILLMENT_NONE,
    PixDeposit,
    PixDepositStatus,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, deposit_lifecycle, registry
from secretaria.services.payments.provider import CreatedCharge, ProviderError
from tests.payments_support import FakeProvider, make_sessionmaker


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


class _Whatsapp:
    persists_outbound = False
    sent: list = []

    def __init__(self, *a, **k):
        pass

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        _Whatsapp.sent.append((to, body))
        return {}


@pytest.fixture(autouse=True)
def _whatsapp(monkeypatch):
    _Whatsapp.sent = []
    monkeypatch.setattr(deposit_lifecycle, "WhatsAppClient", _Whatsapp)


def _provider(monkeypatch, *, name="asaas", **kw) -> FakeProvider:
    fake = FakeProvider(name=name, **kw)
    fake.created_charge = CreatedCharge(
        provider_charge_id="pay_new", copy_paste="COPYPASTE", provider_customer_id="cus_new"
    )
    monkeypatch.setitem(registry._PROVIDERS, name, fake)
    return fake


async def _seed(session, *, provider="asaas", with_account=True, **tenant_kw):
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinic",
        phone_number_id=str(uuid4())[:12],
        pix_deposit_enabled=True,
        payment_provider=provider,
        appointment_types=[
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 200,00"}
        ],
        **tenant_kw,
    )
    patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
    session.add_all([tenant, patient])
    await session.flush()
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=patient.id,
        google_event_id="evt-1",
        appointment_type="Consulta",
        phone=patient.wa_id,
        status=AppointmentStatus.SCHEDULED,
        start_at=datetime(2026, 10, 5, 13, 0, tzinfo=UTC),
    )
    session.add(appointment)
    if with_account:
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider=provider
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
        row.status = "connected"
    await session.commit()
    return tenant, patient, appointment


async def _create(session, tenant, patient, appointment):
    deposit = await deposit_lifecycle.maybe_create_deposit(
        session, tenant=tenant, patient=patient, appointment=appointment, waba_token="wt"
    )
    await session.commit()
    return deposit


async def test_records_the_provider_neutral_columns_and_uses_its_own_id_as_the_reference(
    db, monkeypatch
):
    fake = _provider(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        deposit = await _create(session, tenant, patient, appointment)

    request = fake.calls[0][1]
    # The deposit's own id, not the appointment's (in `before` the appointment comes later).
    assert request.external_reference == str(deposit.id)
    assert request.amount_cents == 6000 and request.payer_document is None
    assert request.due_date.isoformat() >= "2026-01-01"
    assert (deposit.provider, deposit.provider_charge_id) == ("asaas", "pay_new")
    assert deposit.asaas_payment_id == "pay_new"  # dual-write until the "narrow" phase
    assert deposit.external_reference == str(deposit.id)
    assert deposit.deposit_timing == DEPOSIT_TIMING_AFTER
    assert (deposit.fulfillment_status, deposit.expires_at) == (FULFILLMENT_NONE, None)
    assert deposit.status == PixDepositStatus.AWAITING and deposit.pix_copy_paste == "COPYPASTE"
    assert "COPYPASTE" in _Whatsapp.sent[0][1]


async def test_the_customer_id_is_remembered_per_patient_and_reused(db, monkeypatch):
    fake = _provider(monkeypatch)
    fake.created_charge = [
        CreatedCharge("pay_1", "COPY1", provider_customer_id="cus_new"),
        CreatedCharge("pay_2", "COPY2", provider_customer_id="cus_new"),
    ]
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        await _create(session, tenant, patient, appointment)
        mapping = await session.scalar(select(PatientPaymentCustomer))
        assert (mapping.provider, mapping.provider_customer_id) == ("asaas", "cus_new")
        assert patient.asaas_customer_id == "cus_new"  # legacy column dual-written

        second = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id="evt-2",
            appointment_type="Consulta",
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(second)
        await session.commit()
        await _create(session, tenant, patient, second)

    assert fake.calls[1][1].provider_customer_id == "cus_new"


async def test_a_provider_failure_persists_nothing_and_never_raises(db, monkeypatch):
    fake = _provider(monkeypatch)
    fake.create_error = ProviderError("charge_failed", status_code=503, retryable=True)
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        assert await _create(session, tenant, patient, appointment) is None
        assert (await session.scalars(select(PixDeposit))).all() == []
    assert _Whatsapp.sent == []


async def test_a_provider_failure_logs_the_closed_reason(db, monkeypatch):
    from tests.before_support import recorded_logs

    fake = _provider(monkeypatch)
    fake.create_error = ProviderError(
        "charge_failed",
        status_code=400,
        reason="charge_rejected",
        stage="charge",
        code_hint="invalid_object",
    )
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        with recorded_logs() as logs:
            assert await _create(session, tenant, patient, appointment) is None
    (line,) = [e for e in logs if e["event"] == "pix_deposit_charge_failed"]
    assert line["code"] == "charge_failed"
    assert (line["reason"], line["stage"], line["status"], line["code_hint"]) == (
        "charge_rejected",
        "charge",
        400,
        "invalid_object",
    )
    assert set(line) == {
        "event",
        "log_level",
        "code",
        "reason",
        "stage",
        "status",
        "code_hint",
        "tenant_id",
        "appointment_id",
    }


async def test_a_payer_document_requirement_is_a_clean_skip_until_f2_collects_it(db, monkeypatch):
    fake = _provider(monkeypatch)
    fake.create_error = ProviderError("payer_document_required", status_code=400)
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        assert await _create(session, tenant, patient, appointment) is None


async def test_a_charge_without_a_copy_paste_is_kept_because_it_is_real(db, monkeypatch):
    fake = _provider(monkeypatch)
    fake.created_charge = CreatedCharge(provider_charge_id="pay_new", copy_paste=None)
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        deposit = await _create(session, tenant, patient, appointment)
    assert deposit is not None and deposit.pix_copy_paste is None


async def test_a_clinic_without_an_account_is_a_silent_skip(db, monkeypatch):
    fake = _provider(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(session, with_account=False)
        assert await _create(session, tenant, patient, appointment) is None
    assert fake.calls == []


async def test_a_provider_with_no_wrapper_yet_is_a_silent_skip(db, monkeypatch):
    fake = _provider(monkeypatch)  # asaas registered, but the clinic chose mercadopago
    async with db() as session:
        tenant, patient, appointment = await _seed(session, provider="mercadopago")
        assert await _create(session, tenant, patient, appointment) is None
    assert fake.calls == []


async def test_a_revoked_account_cannot_create_a_charge(db, monkeypatch):
    fake = _provider(monkeypatch)
    async with db() as session:
        tenant, patient, appointment = await _seed(session)
        row = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
        row.status = "revoked"
        await session.commit()
        assert await _create(session, tenant, patient, appointment) is None
    assert fake.calls == []
