"""The deposit can be switched OFF without a working Asaas account (TASK-053, owner decision D-A 2,
2026-10-11), and from then on patients book WITHOUT any deposit, on both timings.

Verify-first: these tests pin what the code already does, so a later change can never make "off"
depend on the account again:
  * `services/payments/settings.py::validate_patch` never validates `pix_deposit_enabled` alone, so
    a manager saves `{"pix_deposit_enabled": false}` whatever the account looks like, and every
    field NOT in the patch keeps its stored value;
  * `deposit_gate.quote_deposit` answers `no_deposit` for a clinic with the deposit off BEFORE it
    ever reads the account, so the `before` booking gate commits (WhatsApp) or asks only for the
    e-mail code (Portal), and nothing is held for payment or charged;
  * `deposit_lifecycle.maybe_create_deposit` skips a clinic with the deposit off (`after`).
And the other side, unchanged on purpose: with the deposit still ON and a broken account, a
`before` clinic stays fail-closed (the booking is refused, nothing held) - the screen tells the
clinic to switch it off.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.api.hub.config import _claim_if_authenticated
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.subscription import SubscriptionClaim
from secretaria.models import Appointment, AppointmentStatus, BookingHold, PixDeposit, Tenant
from secretaria.services import booking_hold as holds
from secretaria.services.payments import (
    accounts,
    booking_charge,
    deposit_lifecycle,
    registry,
)
from secretaria.services.pending_identity import RequestCodeOutcome, RequestCodeResult
from tests.before_support import SLOT_END, SLOT_START, entitlement_summary, make_world, new_charge
from tests.payments_support import FakeProvider, make_sessionmaker

BROKEN = ["none", "revoked", "reauth_required", "error", "incomplete"]
UNTOUCHED = (
    "deposit_timing",
    "payment_provider",
    "deposit_hold_ttl_minutes",
    "pix_deposit_percent",
    "clinic_name",
    "timezone",
)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(holds, "async_session_factory", maker)
    monkeypatch.setattr(booking_charge, "async_session_factory", maker)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider()
    fake.created_charge = new_charge()
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)

    async def entitlements(tenant_id, redis):
        return entitlement_summary()

    async def request_code(tenant_id, external_id):
        return RequestCodeResult(RequestCodeOutcome.SENT, email_masked="m***a@gmail.com")

    monkeypatch.setattr(holds, "request_code", request_code)
    return fake


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    from secretaria.main import app

    for dep in (get_session, get_current_tenant, _claim_if_authenticated):
        app.dependency_overrides.pop(dep, None)


async def _world(db, *, account: str, timing: str, enabled: bool, channel: str = "whatsapp"):
    """One clinic whose Asaas account is `account` ("none" = there is no account row at all)."""
    async with db() as session:
        world = await make_world(
            session,
            timing=timing,
            channel=channel,
            with_account=account != "none",
            pix_deposit_enabled=enabled,
        )
        if account not in ("none", "connected"):
            row = await accounts.get_account_row(
                session, tenant_id=world.tenant_id, provider="asaas"
            )
            row.status = account
            await session.commit()
    return world


def _wire_manager(db, tenant_id):
    from fastapi import Depends

    from secretaria.main import app

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    app.dependency_overrides[_claim_if_authenticated] = lambda: SubscriptionClaim(
        tenant_id=tenant_id, active=True, role="manager", is_manager=True
    )


def _gate(world, *, armed: bool) -> holds.BookingGate:
    return holds.BookingGate(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=None,
        external_id=world.patient.external_id or "",
        armed=armed,
    )


async def _decide(world, *, armed: bool):
    return await _gate(world, armed=armed).decide(
        start_at=SLOT_START,
        end_at=SLOT_END,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
    )


async def _rows(db, model):
    async with db() as session:
        return list(await session.scalars(select(model)))


@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("account", BROKEN)
async def test_a_manager_switches_the_deposit_off_whatever_the_account_looks_like(
    client: AsyncClient, db, timing, account
):
    world = await _world(db, account=account, timing=timing, enabled=True)
    async with db() as session:
        before = await session.get(Tenant, world.tenant_id)
        kept = {field: getattr(before, field) for field in UNTOUCHED}
    _wire_manager(db, world.tenant_id)
    response = await client.put(
        "/tenants/me/configuration", json={"tenant": {"pix_deposit_enabled": False}}
    )
    assert response.status_code == 200, response.text
    async with db() as session:
        stored = await session.get(Tenant, world.tenant_id)
    assert stored.pix_deposit_enabled is False
    # A partial patch: every field not sent keeps its value ("off" wins over the timing).
    assert {field: getattr(stored, field) for field in UNTOUCHED} == kept


@pytest.mark.parametrize("account", BROKEN)
async def test_with_the_deposit_off_a_before_clinic_books_on_whatsapp_without_any_deposit(
    db, provider, account
):
    world = await _world(db, account=account, timing="before", enabled=False)
    decision = await _decide(world, armed=False)
    assert decision.outcome == "commit"
    assert await _rows(db, BookingHold) == [] and await _rows(db, PixDeposit) == []
    assert provider.called("create_pix_charge") == 0


@pytest.mark.parametrize("account", BROKEN)
async def test_with_the_deposit_off_the_portal_only_asks_for_the_e_mail_code(
    db, provider, account
):
    world = await _world(
        db, account=account, timing="before", enabled=False, channel="brain_message"
    )
    decision = await _decide(world, armed=True)
    assert decision.outcome == "held"  # the 10-minute e-mail code gate, not a payment
    (hold,) = await _rows(db, BookingHold)
    assert hold.phase == "code" and hold.charge_id is None
    assert await _rows(db, PixDeposit) == []
    assert provider.called("create_pix_charge") == 0


@pytest.mark.parametrize("account", BROKEN)
async def test_with_the_deposit_off_an_after_clinic_books_and_asks_for_nothing(
    db, provider, account
):
    world = await _world(db, account=account, timing="after", enabled=False)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant_id)
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=world.patient.id,
            google_event_id="evt-off",
            appointment_type="Consulta",
            phone=world.patient.wa_id,
            status=AppointmentStatus.SCHEDULED,
            start_at=datetime(2030, 3, 4, 13, 0, tzinfo=UTC),
        )
        session.add(appointment)
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=world.patient, appointment=appointment, waba_token="wt"
        )
    assert deposit is None
    assert await _rows(db, PixDeposit) == []
    assert provider.called("create_pix_charge") == 0
    # "Marked without payment" (owner decision 2026-10-12): the booking stays a plain
    # confirmed appointment - no deposit row, nothing waiting for a Pix.
    async with db() as session:
        stored = await session.get(Appointment, appointment.id)
    assert stored.status == AppointmentStatus.SCHEDULED


@pytest.mark.parametrize("account", BROKEN)
async def test_with_the_deposit_on_a_broken_account_keeps_a_before_clinic_fail_closed(
    db, provider, account
):
    """Unchanged on purpose: the booking is refused and nothing is held; the clinic's way out is
    switching the deposit off (previous tests), which the screen says in words."""
    world = await _world(db, account=account, timing="before", enabled=True)
    decision = await _decide(world, armed=False)
    assert (decision.outcome, decision.blocked_reason) == ("blocked", "no_credential")
    assert await _rows(db, BookingHold) == [] and await _rows(db, PixDeposit) == []
    assert provider.called("create_pix_charge") == 0
