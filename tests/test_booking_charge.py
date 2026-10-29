"""`begin_payment` / `abandon_charge` (services/payments/booking_charge.py, spec sections 5.2, 7).

The invariant every test here guards: there is NO way out of `begin_payment` that leaves a hold
behind without a live, recorded charge (decision D3), and NO way out of `abandon_charge` that
deletes a charge the provider says is paid.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import BookingHold, PixDeposit
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import booking_hold as holds
from secretaria.services.payments import accounts, booking_charge, registry
from secretaria.services.payments.deposit_gate import DepositQuote
from secretaria.services.payments.provider import ChargeState, PaymentStatus, ProviderError
from tests.before_support import SLOT_END, SLOT_START, make_world, new_charge
from tests.payments_support import FakeProvider, make_sessionmaker

QUOTE = DepositQuote(
    "required", "required", amount_cents=6000, provider="asaas", ttl_minutes=30,
    service_label="Consulta",
)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(holds, "async_session_factory", maker)
    monkeypatch.setattr(booking_charge, "async_session_factory", maker)
    yield maker
    await engine.dispose()


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider()
    fake.created_charge = new_charge()
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def _begin(world, **overrides):
    args = {
        "tenant_id": world.tenant_id,
        "conversation_id": world.conversation.id,
        "patient_id": world.patient.id,
        "quote": QUOTE,
        "professional_id": None,
        "appointment_type": "Consulta",
        "insurance": None,
        "attendee_name": None,
        "start_at": SLOT_START,
        "end_at": SLOT_END,
    }
    args.update(overrides)
    return await booking_charge.begin_payment(**args)


async def _holds(db):
    async with db() as session:
        return list(await session.scalars(select(BookingHold)))


async def _deposits(db):
    async with db() as session:
        return list(await session.scalars(select(PixDeposit)))


async def test_the_happy_path_holds_the_slot_and_records_one_awaiting_charge(db, provider):
    async with db() as session:
        world = await make_world(session)
    started = await _begin(world)

    (hold,) = await _holds(db)
    (deposit,) = await _deposits(db)
    assert (hold.phase, hold.charge_id) == ("payment", deposit.id)
    assert hold.expires_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(minutes=28)
    assert deposit.status == PixDepositStatus.AWAITING
    assert deposit.deposit_timing == "before"
    assert deposit.appointment_id is None
    assert deposit.external_reference == str(deposit.id)
    assert deposit.provider_charge_id == "pay_1" and deposit.asaas_payment_id == "pay_1"
    assert deposit.conversation_id == world.conversation.id
    assert deposit.hold_id == hold.id
    assert deposit.expires_at == hold.expires_at
    assert deposit.booking_snapshot["appointment_type"] == "Consulta"
    assert deposit.booking_snapshot["start_at"] == SLOT_START.isoformat()
    assert "Maria" not in str(deposit.booking_snapshot)  # never the patient's name
    (call,) = [c for c in provider.calls if c[0] == "create_pix_charge"]
    assert call[1].external_reference == str(deposit.id)
    assert call[1].amount_cents == 6000
    assert started.copy_paste == "PIXCOPIAECOLA" and started.deposit_id == deposit.id


async def test_a_window_somebody_else_holds_is_slot_taken_and_nothing_is_created(db, provider):
    from secretaria.models import Conversation, Patient

    async with db() as session:
        world = await make_world(session)
        rival = Patient(id=uuid4(), tenant_id=world.tenant_id, wa_id="5511888888888")
        session.add(rival)
        await session.flush()
        rival_conversation = Conversation(
            id=uuid4(), tenant_id=world.tenant_id, patient_id=rival.id
        )
        session.add(rival_conversation)
        await session.commit()
    rival_hold = await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=rival_conversation.id,
        patient_id=rival.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
        # TASK-072: only a Pix (payment phase) blocks a slot; a code wait reserves nothing.
        phase="payment",
        charge_id=uuid4(),
    )
    assert rival_hold is not None
    assert await _begin(world) is None
    assert await _deposits(db) == []
    assert provider.called("create_pix_charge") == 0
    assert [h.id for h in await _holds(db)] == [rival_hold.id]  # the rival's hold is untouched


@pytest.mark.parametrize(
    "code", ["payer_document_required", "payer_email_required", "customer_failed"]
)
async def test_a_permanent_provider_failure_raises_and_leaves_no_hold(db, provider, code):
    provider.create_error = ProviderError(code, status_code=400)
    async with db() as session:
        world = await make_world(session)
    with pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world)
    assert raised.value.code == code
    assert await _holds(db) == [] and await _deposits(db) == []
    assert provider.called("create_pix_charge") == 1  # no retry for a non-retryable code


async def test_a_retryable_failure_is_retried_once_and_can_succeed(db, provider):
    calls = {"n": 0}
    real = provider.create_pix_charge

    async def flaky(account, request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("http_503", status_code=503, retryable=True)
        return await real(account, request)

    provider.create_pix_charge = flaky
    async with db() as session:
        world = await make_world(session)
    assert await _begin(world) is not None
    assert calls["n"] == 2


async def test_two_retryable_failures_release_the_hold(db, provider):
    provider.create_error = ProviderError("http_503", status_code=503, retryable=True)
    async with db() as session:
        world = await make_world(session)
    with pytest.raises(booking_charge.ChargeStartError):
        await _begin(world)
    assert provider.called("create_pix_charge") == 2
    assert await _holds(db) == []


async def test_a_charge_without_a_pix_code_is_voided_and_the_hold_released(db, provider):
    provider.created_charge = new_charge(copy_paste=None)
    async with db() as session:
        world = await make_world(session)
    with pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world)
    assert raised.value.code == "no_copy_paste"
    assert provider.called("cancel_charge") == 1
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_a_failure_recording_the_charge_voids_it_and_leaves_no_dangling_hold(
    db, provider, monkeypatch
):
    """Review focus: the charge EXISTS at the provider but the row cannot be written."""

    async def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(accounts, "remember_customer_id", boom)
    async with db() as session:
        world = await make_world(session)
    with pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world)
    assert raised.value.code == "persist_failed"
    assert provider.called("cancel_charge") == 1
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_a_code_phase_hold_is_extended_in_place_never_replaced(db, provider):
    async with db() as session:
        world = await make_world(session, channel="brain_message")
    code_hold = await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
    )
    started = await _begin(world, existing_hold=code_hold)
    (hold,) = await _holds(db)
    assert hold.id == code_hold.id == started.hold_id
    assert (hold.phase, hold.charge_id) == ("payment", started.deposit_id)
    assert hold.expires_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(minutes=28)


async def test_a_second_hold_never_replaces_the_one_behind_a_live_charge(db, provider):
    async with db() as session:
        world = await make_world(session)
    await _begin(world)
    provider.created_charge = new_charge("pay_2")
    later = SLOT_START + timedelta(hours=2)
    with pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world, start_at=later, end_at=later + timedelta(minutes=30))
    assert raised.value.code == "charge_pending"
    (hold,) = await _holds(db)
    assert hold.start_at.replace(tzinfo=UTC) == SLOT_START  # the first reservation is intact


async def test_a_patient_of_another_clinic_cannot_be_charged(db, provider):
    async with db() as session:
        world = await make_world(session)
        other = await make_world(session)
    with pytest.raises(booking_charge.ChargeStartError):
        await _begin(world, patient_id=other.patient.id)
    assert await _holds(db) == []


# ---- abandon_charge ---------------------------------------------------------------------


async def _live_charge(db, provider):
    async with db() as session:
        world = await make_world(session)
    started = await _begin(world)
    return world, started


async def test_abandoning_voids_the_charge_frees_the_slot_and_owes_no_notice(db, provider):
    provider.state = ChargeState(PaymentStatus.PENDING)
    world, started = await _live_charge(db, provider)
    async with db() as session:
        outcome = await booking_charge.abandon_charge(session, world.tenant, started.deposit_id)
    assert outcome == "voided"
    assert provider.called("cancel_charge") == 1
    (deposit,) = await _deposits(db)
    assert deposit.status == PixDepositStatus.EXPIRED
    assert deposit.expired_notice_sent_at is not None
    assert deposit.conversation_id is None
    assert await _holds(db) == []


async def test_a_charge_the_provider_says_is_paid_is_never_deleted_by_abandoning(db, provider):
    provider.state = ChargeState(PaymentStatus.PAID, paid_amount_cents=6000)
    world, started = await _live_charge(db, provider)
    async with db() as session:
        outcome = await booking_charge.abandon_charge(session, world.tenant, started.deposit_id)
    assert outcome == "paid"
    assert provider.called("cancel_charge") == 0
    (deposit,) = await _deposits(db)
    assert deposit.status == PixDepositStatus.AWAITING  # the paid path settles it
    assert len(await _holds(db)) == 1  # and the slot stays held for that settlement


async def test_a_provider_outage_still_frees_the_slot_and_leaves_the_void_to_the_cron(
    db, provider
):
    provider.get_error = ProviderError("http_503", status_code=503, retryable=True)
    world, started = await _live_charge(db, provider)
    async with db() as session:
        outcome = await booking_charge.abandon_charge(session, world.tenant, started.deposit_id)
    assert outcome == "voided"
    (deposit,) = await _deposits(db)
    assert deposit.status == PixDepositStatus.AWAITING  # the expiry job voids it later
    assert deposit.expired_notice_sent_at is None
    assert await _holds(db) == []


async def test_abandoning_a_resolved_or_foreign_charge_is_a_noop(db, provider):
    provider.state = ChargeState(PaymentStatus.PENDING)
    world, started = await _live_charge(db, provider)
    async with db() as session:
        other = await make_world(session)
        assert (
            await booking_charge.abandon_charge(session, other.tenant, started.deposit_id) == "gone"
        )
        assert await booking_charge.abandon_charge(session, world.tenant, uuid4()) == "gone"
    assert provider.called("cancel_charge") == 0
    assert len(await _holds(db)) == 1


async def test_every_call_uses_the_account_of_the_charges_own_clinic(db, provider):
    provider.state = ChargeState(PaymentStatus.PENDING)
    world, started = await _live_charge(db, provider)
    async with db() as session:
        await booking_charge.abandon_charge(session, world.tenant, started.deposit_id)
    assert set(provider.tenants_seen) == {world.tenant_id}


# --------------------------------------------------------------------------
# Plano 2, Task 2: every failed charge carries a SAFE reason + stage up to one log line
# --------------------------------------------------------------------------

CPF = "52998224725"


@pytest.fixture
def asaas(monkeypatch):
    """The REAL `AsaasProvider` in the registry, with a fake Asaas client behind `client_for`."""
    from secretaria.services.payments import asaas_provider
    from tests.payments_support import FakeAsaasClient

    def install(**kwargs) -> FakeAsaasClient:
        fake = FakeAsaasClient(**kwargs)
        monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
        return fake

    monkeypatch.setitem(registry._PROVIDERS, "asaas", asaas_provider.AsaasProvider())
    return install


def _charge_failed_logs(logs):
    return [entry for entry in logs if entry["event"] == "payment_charge_failed"]


async def test_customer_400_logs_customer_rejected(db, asaas):
    from secretaria.services.payments.asaas import AsaasError
    from tests.before_support import recorded_logs

    asaas(errors={"create_customer": AsaasError(400, "invalid_name")})
    async with db() as session:
        world = await make_world(session)
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world, payer_document=CPF, payer_email="maria@example.com")

    assert raised.value.code == "customer_failed"  # the old symbolic code does not change
    assert (raised.value.stage, raised.value.reason) == ("customer", "customer_rejected")
    assert raised.value.status_code == 400
    (line,) = _charge_failed_logs(logs)
    assert line == {
        "event": "payment_charge_failed",
        "log_level": "warning",
        "tenant_id": str(world.tenant_id),
        "stage": "customer",
        "reason": "customer_rejected",
        "status": 400,
        "code_hint": "invalid_name",
    }
    dumped = str(logs)
    for secret in ("Maria", CPF, "5511999999999", "maria@example.com", "'key'"):
        assert secret not in dumped
    assert await _holds(db) == []


async def test_charge_401_is_api_key_invalid(db, asaas):
    from secretaria.services.payments.asaas import AsaasError
    from tests.before_support import recorded_logs

    fake = asaas(errors={"create_pix_payment": AsaasError(401)})
    async with db() as session:
        world = await make_world(session)
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world, payer_document=CPF)

    assert raised.value.code == "charge_failed"
    assert (raised.value.stage, raised.value.reason) == ("charge", "api_key_invalid")
    assert [c[0] for c in fake.calls].count("create_pix_payment") == 1  # 401 is not retried
    (line,) = _charge_failed_logs(logs)
    assert (line["stage"], line["reason"], line["status"]) == ("charge", "api_key_invalid", 401)


async def test_charge_503_retried_once_then_provider_unavailable(db, asaas):
    from secretaria.services.payments.asaas import AsaasError
    from tests.before_support import recorded_logs

    fake = asaas(errors={"create_pix_payment": AsaasError(503)})
    async with db() as session:
        world = await make_world(session)
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world, payer_document=CPF)

    assert [c[0] for c in fake.calls].count("create_pix_payment") == 2  # exactly one retry
    assert (raised.value.stage, raised.value.reason) == ("charge", "provider_unavailable")
    (line,) = _charge_failed_logs(logs)  # logged once, for the final failure
    assert (line["reason"], line["status"]) == ("provider_unavailable", 503)
    assert await _holds(db) == []


async def test_qr_failure_surfaces_qr_unavailable(db, asaas):
    from tests.before_support import recorded_logs

    fake = asaas(qr_payload=None)
    async with db() as session:
        world = await make_world(session)
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world, payer_document=CPF)

    assert raised.value.code == "no_copy_paste"
    assert (raised.value.stage, raised.value.reason) == ("qr", "qr_unavailable")
    assert ("delete_payment", "pay_fake") in fake.calls  # the charge was voided
    (line,) = _charge_failed_logs(logs)
    assert (line["stage"], line["reason"]) == ("qr", "qr_unavailable")
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_two_qr_errors_still_end_in_qr_unavailable_after_two_attempts(db, asaas):
    from secretaria.services.payments.asaas import AsaasError
    from tests.before_support import recorded_logs

    fake = asaas(errors={"get_pix_qr": AsaasError(404, "invalid_action")})
    async with db() as session:
        world = await make_world(session)
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world, payer_document=CPF)

    assert [c[0] for c in fake.calls].count("get_pix_qr") == 2
    assert raised.value.code == "no_copy_paste"
    assert (raised.value.stage, raised.value.reason) == ("qr", "qr_unavailable")
    assert ("delete_payment", "pay_fake") in fake.calls  # voided, as before
    qr_logs = [entry for entry in logs if entry["event"] == "asaas_qr_failed"]
    assert [(e["attempt"], e["status"], e["code_hint"]) for e in qr_logs] == [
        (1, 404, "invalid_action"),
        (2, 404, "invalid_action"),
    ]
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_persist_failure_surfaces_persist_failed(db, provider, monkeypatch):
    from tests.before_support import recorded_logs

    async def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(accounts, "remember_customer_id", boom)
    async with db() as session:
        world = await make_world(session)
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world)

    assert raised.value.code == "persist_failed"
    assert (raised.value.stage, raised.value.reason) == ("persist", "persist_failed")
    (line,) = _charge_failed_logs(logs)
    assert (line["stage"], line["reason"], line["status"]) == ("persist", "persist_failed", None)
    assert any(entry["event"] == "payment_begin_failed" for entry in logs)


async def test_no_account_surfaces_no_account(db, provider):
    from tests.before_support import recorded_logs

    async with db() as session:
        world = await make_world(session, with_account=False)
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world)

    assert raised.value.code == "no_account"
    assert (raised.value.stage, raised.value.reason) == ("config", "no_account")
    (line,) = _charge_failed_logs(logs)
    assert (line["stage"], line["reason"]) == ("config", "no_account")


async def test_a_pending_charge_has_no_reason_and_logs_nothing(db, provider):
    from tests.before_support import recorded_logs

    async with db() as session:
        world = await make_world(session)
    await _begin(world)
    provider.created_charge = new_charge("pay_2")
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world)
    assert raised.value.code == "charge_pending"
    assert raised.value.reason is None and raised.value.stage is None
    assert _charge_failed_logs(logs) == []


async def test_a_cpf_question_is_not_logged_as_a_failed_charge(db, asaas):
    """No CPF given yet and Asaas asks for one: the caller asks the patient, nothing failed."""
    from secretaria.services.payments.asaas import AsaasError
    from tests.before_support import recorded_logs

    asaas(errors={"create_customer": AsaasError(400, "invalid_cpfCnpj")})
    async with db() as session:
        world = await make_world(session)
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world)

    assert raised.value.code == "payer_document_required"
    assert _charge_failed_logs(logs) == []
    assert await _holds(db) == []


async def test_a_cpf_given_and_still_refused_is_logged_as_a_failed_charge(db, asaas):
    """The CPF WAS given and the customer is still refused: the real blocked case, logged."""
    from secretaria.services.payments.asaas import AsaasError
    from tests.before_support import recorded_logs

    asaas(errors={"create_customer": AsaasError(400, "invalid_cpfCnpj")})
    async with db() as session:
        world = await make_world(session)
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world, payer_document=CPF)

    assert raised.value.code == "customer_failed"
    (line,) = _charge_failed_logs(logs)
    assert (line["stage"], line["reason"], line["status"]) == ("customer", "customer_rejected", 400)


async def test_an_e_mail_question_is_not_logged_but_a_repeated_one_is(db, provider):
    from tests.before_support import recorded_logs

    provider.create_error = ProviderError(
        "payer_email_required", status_code=400, reason="charge_rejected", stage="charge"
    )
    async with db() as session:
        world = await make_world(session)
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError):
        await _begin(world, payer_document=CPF)
    assert _charge_failed_logs(logs) == []

    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError):
        await _begin(world, payer_document=CPF, payer_email="maria@example.com")
    (line,) = _charge_failed_logs(logs)
    assert line["reason"] == "charge_rejected"


# --------------------------------------------------------------------------
# Task 3B: no orphan customer is left in the clinic's Asaas when the charge is voided.


async def test_qr_failure_voids_charge_and_deletes_customer_created_here(db, asaas):
    fake = asaas(customer_id="cus_new", qr_payload=None)
    async with db() as session:
        world = await make_world(session)
    with pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world, payer_document=CPF)

    assert raised.value.code == "no_copy_paste"
    names = [c[0] for c in fake.calls]
    assert ("delete_payment", "pay_fake") in fake.calls
    assert ("delete_customer", "cus_new") in fake.calls
    assert names.index("delete_payment") < names.index("delete_customer")  # void first
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_persist_failure_voids_charge_and_deletes_customer_created_here(
    db, asaas, monkeypatch
):
    async def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    fake = asaas(customer_id="cus_new")
    monkeypatch.setattr(accounts, "remember_customer_id", boom)
    async with db() as session:
        world = await make_world(session)
    with pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world, payer_document=CPF)

    assert raised.value.code == "persist_failed"
    names = [c[0] for c in fake.calls]
    assert ("delete_payment", "pay_fake") in fake.calls
    assert ("delete_customer", "cus_new") in fake.calls
    assert names.index("delete_payment") < names.index("delete_customer")
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_stored_customer_is_never_deleted_on_failure(db, asaas):
    fake = asaas(qr_payload=None)
    async with db() as session:
        world = await make_world(session)
        await accounts.remember_customer_id(
            session,
            tenant_id=world.tenant_id,
            patient_id=world.patient.id,
            provider="asaas",
            provider_customer_id="cus_stored",
        )
        await session.commit()
    with pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world, payer_document=CPF)

    assert raised.value.code == "no_copy_paste"
    names = [c[0] for c in fake.calls]
    assert "create_customer" not in names
    assert ("create_pix_payment", "cus_stored") == fake.calls[0][:2]
    assert ("delete_payment", "pay_fake") in fake.calls
    assert "delete_customer" not in names


async def test_customer_delete_failure_does_not_hide_original_reason(db, asaas):
    from secretaria.services.payments.asaas import AsaasError
    from tests.before_support import recorded_logs

    fake = asaas(
        customer_id="cus_new", qr_payload=None, errors={"delete_customer": AsaasError(500)}
    )
    async with db() as session:
        world = await make_world(session)
    with recorded_logs() as logs, pytest.raises(booking_charge.ChargeStartError) as raised:
        await _begin(world, payer_document=CPF)

    assert raised.value.code == "no_copy_paste"
    assert (raised.value.stage, raised.value.reason) == ("qr", "qr_unavailable")
    assert ("delete_customer", "cus_new") in fake.calls
    (line,) = _charge_failed_logs(logs)
    assert (line["stage"], line["reason"]) == ("qr", "qr_unavailable")
    (cleanup,) = [e for e in logs if e["event"] == "payment_begin_customer_cleanup_failed"]
    assert cleanup == {
        "event": "payment_begin_customer_cleanup_failed",
        "log_level": "warning",
        "error_type": "ProviderError",
    }
    assert "cus_new" not in str(logs) and CPF not in str(logs)
    assert await _holds(db) == [] and await _deposits(db) == []
