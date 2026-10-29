"""The deposit "breaker" (TASK-071, Plan 3, task 5).

While the clinic's payment account is KNOWN to be failing for an account-level reason, the
deposit step is skipped altogether: the patient is never asked for the CPF, no customer and no
charge is created, the booking takes the normal no-deposit path and the clinic is told (within
the daily e-mail cap). The cooldown counts from the last REAL charge failure: once it is over,
the next booking tries the charge again, even under continuous traffic.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.config import Settings
from secretaria.models import Appointment, AppointmentStatus, BookingHold, FlowState, PixDeposit
from secretaria.services import booking_hold as holds, email, flow_router as fr
from secretaria.services.payments import (
    accounts,
    booking_charge,
    deposit_failure_notice,
    payer_data as pdata,
    registry,
)
from secretaria.services.payments.deposit_failure_notice import BREAKER_REASONS, breaker_open
from secretaria.services.payments.failure_reason import FailureReason
from secretaria.services.payments.provider import ChargeState, PaymentStatus, ProviderError
from secretaria.services.pending_identity import RequestCodeOutcome, RequestCodeResult
from secretaria.workers import tasks
from tests._patching import workers_ns
from tests.before_support import SLOT_END, SLOT_START, make_world, new_charge
from tests.payments_support import FakeProvider, FakeRedis, make_sessionmaker

DOCUMENT = "52998224725"  # a well-known valid test number, never a real person's


class RefusingAsaas(FakeProvider):
    """Asaas asks for the CPF of a new customer, then refuses the charge while `refuse`."""

    refuse = True

    async def create_pix_charge(self, account, request):
        self.calls.append(("create_pix_charge", request))
        if not (request.payer_document or request.provider_customer_id):
            raise ProviderError("payer_document_required", status_code=400)
        if self.refuse:
            raise ProviderError(
                "charge_failed", status_code=400, reason="account_not_enabled", stage="charge"
            )
        return self.created_charge


class Calendar:
    tzinfo = ZoneInfo("America/Sao_Paulo")

    def __init__(self) -> None:
        self.created: list = []

    async def has_conflict(self, start, end, ignore_event_id=None):
        return False

    async def create_event(self, start, end, summary, description="", **kwargs):
        self.created.append(start)
        return {"id": f"evt-{len(self.created)}", "htmlLink": "https://calendar/evt"}


class Env:
    def __init__(self) -> None:
        self.provider = RefusingAsaas()
        self.provider.created_charge = new_charge()
        self.provider.state = ChargeState(PaymentStatus.PENDING)
        self.calendar = Calendar()
        self.alerts: list[dict] = []
        self.code_outcome = RequestCodeOutcome.NOT_PENDING


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    for module in (workers_ns, holds, booking_charge, deposit_failure_notice):
        monkeypatch.setattr(module, "async_session_factory", maker)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    yield maker
    await engine.dispose()


@pytest.fixture
def env(monkeypatch) -> Env:
    e = Env()

    async def alert(to_email, clinic_name, **kwargs):
        e.alerts.append({"to": to_email, **kwargs})
        return True

    async def request_code(tenant_id, external_id):
        return RequestCodeResult(e.code_outcome, email_masked="m***a@gmail.com")

    async def no_emails(tenant_id):
        return {}

    monkeypatch.setattr(email, "send_deposit_unavailable_alert", alert)
    monkeypatch.setattr(deposit_failure_notice, "fetch_professional_emails", no_emails)
    monkeypatch.setattr(holds, "request_code", request_code)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", e.provider)
    return e


async def _fail(db, world, reason: str, *, at: datetime | None = None) -> None:
    """Record a REAL charge failure on the clinic's account (optionally back-dated)."""
    async with db() as session:
        await accounts.record_charge_failure(
            session, tenant_id=world.tenant_id, provider="asaas", reason=reason, stage="charge"
        )
        if at is not None:
            row = await accounts.get_account_row(
                session, tenant_id=world.tenant_id, provider="asaas"
            )
            row.last_charge_failure_at = at
        await session.commit()


async def _account(db, world):
    async with db() as session:
        return await accounts.get_account_row(session, tenant_id=world.tenant_id, provider="asaas")


def _gate(world, *, armed=False, redis=None) -> holds.BookingGate:
    return holds.BookingGate(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=None,
        external_id=world.patient.external_id or "",
        armed=armed,
        redis=redis or FakeRedis(),
    )


async def _decide(world, **kw) -> holds.GateDecision:
    return await _gate(world, **kw).decide(
        start_at=SLOT_START,
        end_at=SLOT_END,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
    )


async def _confirm(world, env):
    """The router's "Confirmar" on WhatsApp (the gate is not armed there)."""
    snapshot = type(
        "Snap",
        (),
        {
            "id": world.conversation.id,
            "flow_state": FlowState.SERVICE_CATALOG,
            "flow_step": fr.STEP_AWAITING_CONFIRMATION,
            "flow_selected_type": "Consulta",
            "flow_selected_day": "2030-03-04",
            "flow_selected_slot": "2030-03-04T10:00",
            "flow_selected_professional_id": None,
            "flow_selected_insurance": None,
            "flow_managing_appointment_id": None,
            "flow_attendee_name": None,
        },
    )()
    return await fr._handle_confirmation(
        snapshot, world.tenant, env.calendar, fr.LABEL_CONFIRM, "Maria", gate=_gate(world)
    )


def _asked_for_cpf(result) -> bool:
    bodies = " ".join(getattr(b, "body", "") or "" for b in result.bubbles)
    return pdata.ask_text("document", "asaas") in bodies


# ---- the six behaviours of the brief ----------------------------------------------------------


async def test_open_breaker_skips_cpf_question_and_books(db, env):
    async with db() as session:
        world = await make_world(session, contact_email="clinic@example.com")
    await _fail(db, world, "account_not_enabled")
    failed_at = (await _account(db, world)).last_charge_failure_at

    result = await _confirm(world, env)

    assert not _asked_for_cpf(result)
    assert result.flow_state is not FlowState.AWAITING_PAYER_DATA
    assert result.appointment is not None and result.action == "reply"
    assert len(env.calendar.created) == 1
    assert env.provider.calls == []  # no customer, no charge
    async with db() as session:
        assert list(await session.scalars(select(PixDeposit))) == []
    (alert,) = env.alerts
    assert alert["to"] == "clinic@example.com" and alert["reason_text"]
    # The skip itself is not a new failure: the cooldown keeps counting from the real one.
    assert (await _account(db, world)).last_charge_failure_at == failed_at

    decision = await _decide(world)
    assert (decision.outcome, decision.deposit_waived_reason) == ("commit", "account_not_enabled")


async def test_breaker_closes_after_cooldown_and_retries_real_charge(db, env, monkeypatch):
    """Continuous traffic during the cooldown must not keep the breaker open forever."""
    real_now = datetime.now(UTC)
    async with db() as session:
        world = await make_world(session, contact_email="clinic@example.com")
    await _fail(db, world, "api_key_invalid", at=real_now - timedelta(minutes=25))

    clock = {"now": real_now}
    monkeypatch.setattr(deposit_failure_notice, "_now", lambda: clock["now"])

    counters = FakeRedis()  # the daily e-mail counter lives across the bookings
    for _ in range(4):  # bookings inside the cooldown: all skipped, never charged
        decision = await _decide(world, redis=counters)
        assert (decision.outcome, decision.deposit_waived_reason) == ("commit", "api_key_invalid")
    assert env.provider.calls == []
    assert len(env.alerts) == deposit_failure_notice.DAILY_EMAIL_CAP  # capped, not flooded

    clock["now"] = real_now + timedelta(minutes=6)  # 31 minutes after the REAL failure
    decision = await _decide(world)

    assert env.provider.called("create_pix_charge") == 1  # the real charge is tried again
    assert decision.outcome == "needs_payer_data"  # ...so the CPF is asked for again

    # The retried charge fails again: the breaker reopens from this new real failure.
    before = (await _account(db, world)).last_charge_failure_at
    redis = FakeRedis()
    assert await pdata.remember(
        redis,
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        kind="document",
        value=DOCUMENT,
    )
    decision = await _decide(world, redis=redis)
    assert decision.outcome == "commit"
    row = await _account(db, world)
    assert accounts._utc(row.last_charge_failure_at) > accounts._utc(before)
    assert row.last_charge_failure_reason == "account_not_enabled"
    clock["now"] = datetime.now(UTC)
    assert breaker_open(row, clock["now"]) is True


async def test_patient_level_failure_does_not_open_breaker(db, env):
    async with db() as session:
        world = await make_world(session)
    await _fail(db, world, "customer_document_invalid")

    assert breaker_open(await _account(db, world), datetime.now(UTC)) is False
    decision = await _decide(world)
    assert decision.outcome == "needs_payer_data"
    assert env.provider.called("create_pix_charge") == 1


@pytest.mark.parametrize("reason", ["provider_unavailable", "rate_limited"])
async def test_transient_failure_does_not_open_breaker(db, env, reason):
    async with db() as session:
        world = await make_world(session)
    await _fail(db, world, reason)

    assert breaker_open(await _account(db, world), datetime.now(UTC)) is False
    decision = await _decide(world)
    assert decision.outcome == "needs_payer_data"


async def test_successful_test_charge_closes_breaker(db, env):
    """Ruling R3: Plan 4's "Testar cobrança" button calls `record_charge_success`."""
    async with db() as session:
        world = await make_world(session)
    await _fail(db, world, "permission_denied", at=datetime.now(UTC) - timedelta(seconds=5))
    assert breaker_open(await _account(db, world), datetime.now(UTC)) is True

    async with db() as session:
        await accounts.record_charge_success(session, tenant_id=world.tenant_id, provider="asaas")
        await session.commit()

    assert breaker_open(await _account(db, world), datetime.now(UTC)) is False
    decision = await _decide(world)
    assert decision.outcome == "needs_payer_data"


async def test_breaker_is_per_tenant(db, env):
    async with db() as session:
        clinic_a = await make_world(session)
        clinic_b = await make_world(session)
    await _fail(db, clinic_a, "pix_key_missing")

    decision_b = await _decide(clinic_b)
    assert decision_b.outcome == "needs_payer_data"
    assert env.provider.called("create_pix_charge") == 1

    decision_a = await _decide(clinic_a)
    assert (decision_a.outcome, decision_a.deposit_waived_reason) == ("commit", "pix_key_missing")
    assert env.provider.called("create_pix_charge") == 1  # A never reached the provider


# ---- the predicate, the Portal paths and fail-safety -------------------------------------------


def test_breaker_reasons_are_the_account_level_ones():
    assert BREAKER_REASONS == frozenset(
        {
            FailureReason.api_key_invalid,
            FailureReason.permission_denied,
            FailureReason.qr_unavailable,
            FailureReason.account_not_enabled,
            FailureReason.pix_key_missing,
        }
    )


def test_breaker_open_predicate():
    now = datetime.now(UTC)
    row = type(
        "Row",
        (),
        {
            "last_charge_failure_reason": "qr_unavailable",
            "last_charge_failure_at": now - timedelta(minutes=29),
            "last_charge_ok_at": None,
        },
    )()
    assert breaker_open(row, now) is True
    row.last_charge_failure_at = now - timedelta(minutes=30)
    assert breaker_open(row, now) is False  # the cooldown is over
    row.last_charge_failure_at = (now - timedelta(minutes=1)).replace(tzinfo=None)  # SQLite
    assert breaker_open(row, now) is True
    row.last_charge_ok_at = now  # a newer success: not failing any more
    assert breaker_open(row, now) is False
    assert breaker_open(None, now) is False


async def test_open_breaker_on_portal_takes_the_code_hold_then_promotes_without_charge(
    db, env, monkeypatch
):
    """Ruling R4: the Portal keeps the normal no-deposit path (hold + mailed code); once the code
    is verified the worker promotes the hold without starting a Pix."""
    env.code_outcome = RequestCodeOutcome.SENT
    async with db() as session:
        world = await make_world(session, channel="brain_message", contact_email="c@example.com")
    await _fail(db, world, "account_not_enabled")

    decision = await _decide(world, armed=True)
    assert decision.outcome == "held" and decision.hold_id is not None
    assert env.provider.calls == [] and env.alerts == []  # nothing booked yet

    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref=world.patient.external_id,
        inbound_body="123456",
        channel="brain_message",
        tenant_id=world.tenant_id,
    )
    from secretaria.workers.shared.deposit_fallback import DepositWaived

    waived = await tasks._payment_after_code(reply, tenant=tenant, redis=FakeRedis())
    assert waived == DepositWaived("account_not_enabled")
    assert env.provider.calls == []
    async with db() as session:
        (hold,) = list(await session.scalars(select(BookingHold)))
    assert (hold.phase, hold.charge_id) == ("code", None)
    assert len(env.alerts) == 1

    calendar = Calendar()

    async def appointment_calendar(*args, **kwargs):
        return calendar

    monkeypatch.setattr(workers_ns, "_appointment_calendar", appointment_calendar)
    text = await tasks._promote_booking_hold(
        reply,
        tenant=tenant,
        waba_token=None,
        redis=None,
        deposit_waived_reason=waived.reason,
    )
    assert "está confirmado" in text
    async with db() as session:
        (appointment,) = list(await session.scalars(select(Appointment)))
    assert appointment.status == AppointmentStatus.SCHEDULED
    # Task 6: the agenda marks the booking made without the deposit (breaker skip, Portal).
    assert appointment.deposit_waived_reason == "account_not_enabled"


async def test_open_breaker_on_whatsapp_records_waived_reason(db, env):
    """Task 6, router path: the breaker's commit carries the reason to the appointment."""
    async with db() as session:
        world = await make_world(session)
    await _fail(db, world, "pix_key_missing")

    result = await _confirm(world, env)

    assert result.appointment["deposit_waived_reason"] == "pix_key_missing"


@pytest.mark.parametrize("armed", [False, True])
async def test_open_breaker_still_blocks_over_a_live_charge(db, env, armed):
    """`charge_pending` is not technical: the breaker never books next to a payable Pix."""
    from uuid import uuid4

    env.code_outcome = RequestCodeOutcome.SENT
    async with db() as session:
        world = await make_world(session, channel="brain_message" if armed else "whatsapp")
    await _fail(db, world, "account_not_enabled")
    assert await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=SLOT_START + timedelta(hours=2),
        end_at=SLOT_END + timedelta(hours=2),
        phase="payment",
        charge_id=uuid4(),
    )

    decision = await _decide(world, armed=armed)

    assert (decision.outcome, decision.blocked_reason) == ("blocked", "charge_pending")
    assert env.provider.calls == [] and env.alerts == []


async def test_breaker_read_error_is_treated_as_closed(db, env, monkeypatch):
    async with db() as session:
        world = await make_world(session)
    await _fail(db, world, "account_not_enabled")

    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(deposit_failure_notice.accounts, "get_account_row", broken)
    assert await deposit_failure_notice.breaker_reason(world.tenant_id, "asaas") is None
