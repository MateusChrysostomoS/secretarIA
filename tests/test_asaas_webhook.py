"""Tests for api/webhook_asaas.py (the fast-ACK handler) and
services/payments/deposit_lifecycle.py::apply_asaas_event (the worker core
that does the real auth + dedupe + state-mutation work).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
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
from secretaria.models.processed_asaas_event import ProcessedAsaasEvent  # noqa: E402
from secretaria.services import tenant_config  # noqa: E402
from secretaria.services.payments import deposit_lifecycle  # noqa: E402


class _FakeArqPool:
    """Records every enqueue_job call; installed on app.state.arq_pool."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, name, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))


class _FakeWhatsAppClient:
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


class _FakeCalendarService:
    cancelled: list[str] = []
    should_raise = False

    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    async def cancel_event(self, event_id):
        if _FakeCalendarService.should_raise:
            raise RuntimeError("calendar down")
        _FakeCalendarService.cancelled.append(event_id)


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch):
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(deposit_lifecycle, "WhatsAppClient", _FakeWhatsAppClient)
    _FakeCalendarService.cancelled = []
    _FakeCalendarService.should_raise = False
    monkeypatch.setattr(deposit_lifecycle, "CalendarService", _FakeCalendarService)
    yield


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


async def _seed_deposit_with_tenant(
    db,
    *,
    status: PixDepositStatus = PixDepositStatus.AWAITING,
    asaas_payment_id: str = "pay_abc",
    webhook_token: str | None = "whsec_test",
    start_at=None,
    google_event_id: str = "evt-1",
):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12], is_active=True
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add_all([tenant, patient])
        await session.flush()
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id=google_event_id,
            appointment_type="Consulta",
            start_at=start_at,
            phone=patient.wa_id,
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appointment)
        await session.flush()
        deposit = PixDeposit(
            tenant_id=tenant.id,
            appointment_id=appointment.id,
            patient_id=patient.id,
            asaas_payment_id=asaas_payment_id,
            amount_cents=6000,
            percent_applied=30,
            status=status,
        )
        session.add(deposit)
        if webhook_token:
            await tenant_config.set_asaas_webhook_token(session, tenant.id, webhook_token)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(appointment)
        await session.refresh(deposit)
        return tenant, patient, appointment, deposit


# --------------------------------------------------------------------------
# POST /webhooks/asaas — fast-ACK handler
# --------------------------------------------------------------------------


async def test_malformed_json_returns_200_ignored(client: AsyncClient) -> None:
    response = await client.post(
        "/webhooks/asaas", content=b"{not valid json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


async def test_non_object_payload_returns_200_ignored(client: AsyncClient) -> None:
    response = await client.post(
        "/webhooks/asaas", content=b"[1, 2, 3]", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


async def test_enqueues_with_header_token(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.main import app as fastapi_app

    fake_pool = _FakeArqPool()
    monkeypatch.setattr(fastapi_app.state, "arq_pool", fake_pool, raising=False)

    response = await client.post(
        "/webhooks/asaas",
        json={"id": "evt_1", "event": "PAYMENT_RECEIVED", "payment": {"id": "pay_1"}},
        headers={"asaas-access-token": "shared-secret-token"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(fake_pool.calls) == 1
    name, args, kwargs = fake_pool.calls[0]
    assert name == "process_asaas_event"
    assert args == ()
    assert kwargs == {
        "event_id": "evt_1",
        "event_type": "PAYMENT_RECEIVED",
        "payment_id": "pay_1",
        "access_token": "shared-secret-token",
    }
    assert "shared-secret-token" not in response.text


async def test_event_id_fallback_when_id_missing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.main import app as fastapi_app

    fake_pool = _FakeArqPool()
    monkeypatch.setattr(fastapi_app.state, "arq_pool", fake_pool, raising=False)

    response = await client.post(
        "/webhooks/asaas",
        json={"event": "PAYMENT_OVERDUE", "payment": {"id": "pay_2"}},
    )
    assert response.status_code == 200
    _, _, kwargs = fake_pool.calls[0]
    assert kwargs["event_id"] == "PAYMENT_OVERDUE:pay_2"


async def test_pool_unavailable_returns_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.main import app as fastapi_app

    monkeypatch.setattr(fastapi_app.state, "arq_pool", None, raising=False)
    response = await client.post(
        "/webhooks/asaas",
        json={"id": "evt_3", "event": "PAYMENT_RECEIVED", "payment": {"id": "pay_3"}},
    )
    assert response.status_code == 503


async def test_extra_unknown_payload_fields_tolerated_end_to_end(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.main import app as fastapi_app

    fake_pool = _FakeArqPool()
    monkeypatch.setattr(fastapi_app.state, "arq_pool", fake_pool, raising=False)

    response = await client.post(
        "/webhooks/asaas",
        json={
            "id": "evt_novel",
            "event": "PAYMENT_RECEIVED",
            "payment": {
                "id": "pay_novel",
                "someBrandNewField": {"nested": True},
                "value": 60.0,
            },
            "unexpectedTopLevelKey": ["a", "b", "c"],
            "dateCreated": "2026-07-21",
        },
        headers={"asaas-access-token": "tok"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    _, _, kwargs = fake_pool.calls[0]
    assert kwargs["event_id"] == "evt_novel"
    assert kwargs["event_type"] == "PAYMENT_RECEIVED"
    assert kwargs["payment_id"] == "pay_novel"


# --------------------------------------------------------------------------
# apply_asaas_event — auth, dedupe, state mutation
# --------------------------------------------------------------------------


async def test_auth_mismatch_no_claim_no_mutation(db) -> None:
    tenant, patient, appointment, deposit = await _seed_deposit_with_tenant(
        db, webhook_token="expected-token"
    )
    async with db() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-auth",
            event_type="PAYMENT_RECEIVED",
            payment_id=deposit.asaas_payment_id,
            access_token_header="wrong-token",
        )
    assert outcome == "auth_failed"

    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
        claims = (await session2.scalars(select(ProcessedAsaasEvent))).all()
    assert refreshed.status == PixDepositStatus.AWAITING
    assert claims == []


async def test_auth_missing_header_no_claim(db) -> None:
    tenant, patient, appointment, deposit = await _seed_deposit_with_tenant(
        db, webhook_token="expected-token"
    )
    async with db() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-noauth",
            event_type="PAYMENT_RECEIVED",
            payment_id=deposit.asaas_payment_id,
            access_token_header=None,
        )
    assert outcome == "auth_failed"


async def test_auth_no_expected_token_provisioned_fails_closed(db) -> None:
    tenant, patient, appointment, deposit = await _seed_deposit_with_tenant(db, webhook_token=None)
    async with db() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-noprov",
            event_type="PAYMENT_RECEIVED",
            payment_id=deposit.asaas_payment_id,
            access_token_header="anything",
        )
    assert outcome == "auth_failed"


async def test_duplicate_event_id_claimed_once(db) -> None:
    tenant, patient, appointment, deposit = await _seed_deposit_with_tenant(
        db, status=PixDepositStatus.AWAITING, webhook_token="tok"
    )
    async with db() as session:
        first = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-dup",
            event_type="PAYMENT_RECEIVED",
            payment_id=deposit.asaas_payment_id,
            access_token_header="tok",
        )
    async with db() as session2:
        second = await deposit_lifecycle.apply_asaas_event(
            session2,
            event_id="evt-dup",
            event_type="PAYMENT_RECEIVED",
            payment_id=deposit.asaas_payment_id,
            access_token_header="tok",
        )

    assert first == "paid"
    assert second == "duplicate"
    async with db() as session3:
        refreshed = await session3.get(PixDeposit, deposit.id)
        claim_count = len(
            (
                await session3.scalars(
                    select(ProcessedAsaasEvent).where(ProcessedAsaasEvent.event_id == "evt-dup")
                )
            ).all()
        )
    assert refreshed.status == PixDepositStatus.PAID  # unchanged by the duplicate call
    assert claim_count == 1


async def test_payment_received_awaiting_to_paid(db) -> None:
    tenant, patient, appointment, deposit = await _seed_deposit_with_tenant(
        db, status=PixDepositStatus.AWAITING, webhook_token="tok"
    )
    async with db() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-received",
            event_type="PAYMENT_RECEIVED",
            payment_id=deposit.asaas_payment_id,
            access_token_header="tok",
        )
    assert outcome == "paid"

    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
    assert refreshed.status == PixDepositStatus.PAID
    assert refreshed.paid_at is not None
    assert len(_FakeWhatsAppClient.created) == 1
    assert _FakeWhatsAppClient.created[0].sent[0][0] == "text"


async def test_payment_confirmed_idempotent_when_already_paid(db) -> None:
    tenant, patient, appointment, deposit = await _seed_deposit_with_tenant(
        db, status=PixDepositStatus.PAID, webhook_token="tok"
    )
    async with db() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-confirmed",
            event_type="PAYMENT_CONFIRMED",
            payment_id=deposit.asaas_payment_id,
            access_token_header="tok",
        )
    assert outcome == "noop"

    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
        claimed = (
            await session2.scalars(
                select(ProcessedAsaasEvent).where(ProcessedAsaasEvent.event_id == "evt-confirmed")
            )
        ).all()
    assert refreshed.status == PixDepositStatus.PAID  # unchanged
    assert len(claimed) == 1  # still claimed even though a no-op
    assert _FakeWhatsAppClient.created == []  # no notification for a no-op


async def test_payment_overdue_expires_and_cancels_appointment(db) -> None:
    tenant, patient, appointment, deposit = await _seed_deposit_with_tenant(
        db, status=PixDepositStatus.AWAITING, webhook_token="tok"
    )
    async with db() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-overdue",
            event_type="PAYMENT_OVERDUE",
            payment_id=deposit.asaas_payment_id,
            access_token_header="tok",
        )
    assert outcome == "expired"

    async with db() as session2:
        refreshed_deposit = await session2.get(PixDeposit, deposit.id)
        refreshed_appt = await session2.get(Appointment, appointment.id)
    assert refreshed_deposit.status == PixDepositStatus.EXPIRED
    assert refreshed_deposit.resolved_at is not None
    assert refreshed_appt.status == AppointmentStatus.CANCELLED
    assert len(_FakeWhatsAppClient.created) == 1  # "reserva expirou" notice


async def test_payment_deleted_when_already_paid_is_noop(db) -> None:
    """PAYMENT_DELETED only applies the EXPIRED transition when status==AWAITING."""
    tenant, patient, appointment, deposit = await _seed_deposit_with_tenant(
        db, status=PixDepositStatus.PAID, webhook_token="tok"
    )
    async with db() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-deleted",
            event_type="PAYMENT_DELETED",
            payment_id=deposit.asaas_payment_id,
            access_token_header="tok",
        )
    assert outcome == "noop"
    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
        refreshed_appt = await session2.get(Appointment, appointment.id)
    assert refreshed.status == PixDepositStatus.PAID
    assert refreshed_appt.status == AppointmentStatus.SCHEDULED


async def test_unknown_event_type_claimed_noop(db) -> None:
    tenant, patient, appointment, deposit = await _seed_deposit_with_tenant(
        db, status=PixDepositStatus.AWAITING, webhook_token="tok"
    )
    async with db() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-unknown-type",
            event_type="SOME_NEW_EVENT_TYPE",
            payment_id=deposit.asaas_payment_id,
            access_token_header="tok",
        )
    assert outcome == "noop"

    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
        claimed = (
            await session2.scalars(
                select(ProcessedAsaasEvent).where(
                    ProcessedAsaasEvent.event_id == "evt-unknown-type"
                )
            )
        ).all()
    assert refreshed.status == PixDepositStatus.AWAITING  # untouched
    assert len(claimed) == 1  # still claimed (forward-compatible)


async def test_payment_refunded_logs_only_no_mutation(db) -> None:
    tenant, patient, appointment, deposit = await _seed_deposit_with_tenant(
        db, status=PixDepositStatus.PAID, webhook_token="tok"
    )
    async with db() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-refunded",
            event_type="PAYMENT_REFUNDED",
            payment_id=deposit.asaas_payment_id,
            access_token_header="tok",
        )
    assert outcome == "noop"
    async with db() as session2:
        refreshed = await session2.get(PixDeposit, deposit.id)
    assert refreshed.status == PixDepositStatus.PAID  # unchanged, log-only


async def test_unknown_payment_id_no_claim(db) -> None:
    async with db() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-unknown-payment",
            event_type="PAYMENT_RECEIVED",
            payment_id="pay_does_not_exist",
            access_token_header="whatever",
        )
    assert outcome == "unknown_payment"
    async with db() as session2:
        claims = (await session2.scalars(select(ProcessedAsaasEvent))).all()
    assert claims == []


async def test_missing_payment_id_no_claim(db) -> None:
    async with db() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id="evt-no-payment",
            event_type="PAYMENT_RECEIVED",
            payment_id=None,
            access_token_header="whatever",
        )
    assert outcome == "unknown_payment"
    async with db() as session2:
        claims = (await session2.scalars(select(ProcessedAsaasEvent))).all()
    assert claims == []
