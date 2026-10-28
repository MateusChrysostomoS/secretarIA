"""The nine silent `None` paths of `maybe_create_deposit`, one row each (spec section 7, D3).

In `after` timing those paths stay tolerated (the consultation already exists). In `before` timing
every one of them is an EXPLICIT decision taken before or while the slot is reserved: the four
that are a clinic's own choice (feature off, convenio without deposit, no readable price, zero
amount) book normally; the five that are errors (no patient, no usable credential, the customer
cannot be created, the charge cannot be created, the answer has no charge id) plus the two extras
(no Pix code, the message cannot be sent) BLOCK the booking. Whatever the row, the invariant is the
same: no dangling hold, no orphan charge, and never a consultation without its deposit.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import BookingHold, PixDeposit
from secretaria.models.insurance import TenantInsurancePlan
from secretaria.services import booking_hold as holds, email
from secretaria.services.payments import booking_charge, deposit_gate, registry
from secretaria.services.payments.deposit_gate import DepositQuote
from secretaria.services.payments.provider import ProviderError
from tests.before_support import SLOT_END, SLOT_START, entitlement_summary, make_world, new_charge
from tests.payments_support import FakeProvider, FakeRedis, make_sessionmaker


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(holds, "async_session_factory", maker)
    monkeypatch.setattr(booking_charge, "async_session_factory", maker)
    yield maker
    await engine.dispose()


@pytest.fixture
def env(monkeypatch):
    provider = FakeProvider()
    provider.created_charge = new_charge()
    emails: list[str] = []

    async def entitlements(tenant_id, redis):
        return entitlement_summary()

    async def send_email(to_email, clinic_name, kind):
        emails.append(kind)

    monkeypatch.setattr(email, "send_payment_alert", send_email)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", provider)
    return type("Env", (), {"provider": provider, "emails": emails})


async def _decide(db, world, *, insurance=None):
    gate = holds.BookingGate(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=None,
        external_id="",
        armed=False,
        redis=FakeRedis(),
    )
    return await gate.decide(
        start_at=SLOT_START,
        end_at=SLOT_END,
        professional_id=None,
        appointment_type="Consulta",
        insurance=insurance,
    )


async def _nothing_left_behind(db):
    async with db() as session:
        assert list(await session.scalars(select(BookingHold))) == []
        assert list(await session.scalars(select(PixDeposit))) == []


# The four rows that are a clinic's own choice: the booking goes ahead exactly as it always did.
@pytest.mark.parametrize(
    ("row", "tenant_kw", "insurance"),
    [
        (1, {"pix_deposit_enabled": False}, None),
        (2, {}, "Unimed"),
        (5, {"price": "a combinar"}, None),
        (6, {"price": "R$ 0,10", "percent": 1}, None),
    ],
)
async def test_the_rows_that_are_a_clinics_choice_book_normally(
    db, env, row, tenant_kw, insurance
):
    async with db() as session:
        world = await make_world(session, **tenant_kw)
        if row == 2:
            session.add(
                TenantInsurancePlan(
                    id=uuid4(), tenant_id=world.tenant_id, custom_name="Unimed",
                    charge_deposit=False,
                )
            )
            await session.commit()
    decision = await _decide(db, world, insurance=insurance)
    assert decision.outcome == "commit"
    await _nothing_left_behind(db)
    assert env.provider.called("create_pix_charge") == 0
    assert env.emails == []  # a choice is not an alert


async def test_row_3_no_patient_is_an_error_decided_before_anything_is_reserved(db, env):
    async with db() as session:
        world = await make_world(session)
        quote = await deposit_gate.quote_deposit(
            session, world.tenant, patient_id=None, appointment_type="Consulta",
            professional_id=None, insurance=None,
        )
    assert (quote.outcome, quote.reason) == ("blocked", "no_patient")


async def test_row_4_no_usable_credential_blocks_alerts_and_holds_nothing(db, env):
    async with db() as session:
        world = await make_world(session, with_account=False, contact_email="c@example.com")
    decision = await _decide(db, world)
    assert (decision.outcome, decision.blocked_reason) == ("blocked", "no_credential")
    await _nothing_left_behind(db)
    assert env.emails == []  # owner answer 13: no operational e-mail; a human takes the chat


@pytest.mark.parametrize(
    ("row", "error", "created", "code"),
    [
        (7, ProviderError("customer_failed", status_code=400), None, "customer_failed"),
        (8, ProviderError("charge_failed", status_code=400), None, "charge_failed"),
        (8, ProviderError("http_500", status_code=500, retryable=True), None, "http_500"),
        (9, None, new_charge(charge_id=""), "no_charge_id"),
        ("QR", None, new_charge(copy_paste=None), "no_copy_paste"),
    ],
)
async def test_the_rows_that_are_errors_block_alert_and_leave_no_dangling_hold(
    db, env, row, error, created, code
):
    env.provider.create_error = error
    if created is not None:
        env.provider.created_charge = created
    async with db() as session:
        world = await make_world(session, contact_email="c@example.com")
    decision = await _decide(db, world)
    assert (decision.outcome, decision.blocked_reason) == ("blocked", code)
    await _nothing_left_behind(db)  # never a hold without a live charge, never an orphan row
    assert env.emails == []  # owner answer 13: no operational e-mail; a human takes the chat
    if row == "QR":
        assert env.provider.called("cancel_charge") == 1  # a charge without a code is voided


@pytest.mark.parametrize(
    ("code", "field"),
    [("payer_document_required", "document"), ("payer_email_required", "email")],
)
async def test_row_d7_a_provider_that_needs_a_payer_field_asks_for_it_instead_of_blocking(
    db, env, code, field
):
    """Decision D7 (Task 14, generalised): `payer_document_required` (Asaas) and
    `payer_email_required` (Mercado Pago) are a QUESTION, not an error. Nothing is held, nothing
    is charged and the clinic is not alerted (it is not an operational failure)."""
    env.provider.create_error = ProviderError(code, status_code=400)
    async with db() as session:
        world = await make_world(session, contact_email="c@example.com")
    decision = await _decide(db, world)
    assert (decision.outcome, decision.payer_field, decision.payer_provider) == (
        "needs_payer_data",
        field,
        "asaas",
    )
    await _nothing_left_behind(db)
    assert env.emails == []


async def test_an_answer_with_no_charge_id_has_nothing_to_void_and_nothing_to_record(db, env):
    env.provider.created_charge = new_charge(charge_id="")
    async with db() as session:
        world = await make_world(session)
    with pytest.raises(booking_charge.ChargeStartError) as raised:
        await booking_charge.begin_payment(
            tenant_id=world.tenant_id,
            conversation_id=world.conversation.id,
            patient_id=world.patient.id,
            quote=DepositQuote(
                "required", "required", amount_cents=6000, provider="asaas", ttl_minutes=30
            ),
            professional_id=None,
            appointment_type="Consulta",
            insurance=None,
            attendee_name=None,
            start_at=SLOT_START,
            end_at=SLOT_END,
        )
    assert raised.value.code == "no_charge_id"
    assert env.provider.called("cancel_charge") == 0
    await _nothing_left_behind(db)
