"""TASK-085 / T4a - the money path charges the price of the consultation's UNIT.

Dra. Ana does "Consulta" (base R$ 200,00, 30 min) and "Exame" (base R$ 300,00, 60 min) at Sede
and Centro. Centro has its own Consulta price, R$ 250,00 (SPEC §10-1). Sinal of 30 %: R$ 60,00
on the base, R$ 75,00 at Centro.

* characterization - no exception: the sinal, the quote, the readiness checklist and the
  service-change decision are exactly today's, with units or without;
* A1 the "after" sinal (`_resolve_service_and_price` / `maybe_create_deposit`), A2 the "before"
  quote (`quote_deposit` and the two callers that know the hold's unit), A3 the service change
  (`preview_service_change` / `request_service_change`), A4/D8 the readiness checklist;
* tenant isolation: another clinic's professional, unit or exception never reaches a price.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    PixDeposit,
    Professional,
    ProfessionalServiceUnitTerms,
    ProfessionalUnit,
    Service,
    Tenant,
    Unit,
)
from secretaria.models.clinic_payment_account import ACCOUNT_CONNECTED
from secretaria.models.deposit_adjustment import (
    STATUS_ASKED,
    STATUS_AWAITING_PAYMENT,
    DepositAdjustment,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import booking_hold as holds, email, tenant_config as cfg
from secretaria.services.payments import (
    accounts,
    booking_charge,
    deposit_failure_notice,
    deposit_gate,
    deposit_lifecycle,
    readiness,
    registry,
    service_change,
)
from secretaria.services.payments.provider import ChargeState, CreatedCharge, PaymentStatus
from secretaria.services.pending_identity import RequestCodeOutcome, RequestCodeResult
from tests import test_service_change_webhook as hook
from tests.payments_support import FakeProvider, make_sessionmaker

# TASK-085 R: these tests pin the LEGACY deferred mode of the engine (mode NULL rows).
from tests.test_service_change import legacy_deferred_mode  # noqa: E402, F401

NOW = hook.NOW
WEEK = {
    day: [{"start": "08:00", "end": "18:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(holds, "async_session_factory", maker)
    monkeypatch.setattr(booking_charge, "async_session_factory", maker)
    yield maker
    await engine.dispose()


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider(state=ChargeState(status=PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(
        provider_charge_id="pay_diff_1", copy_paste="000201pix", provider_customer_id="cus_1"
    )
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


@pytest.fixture(autouse=True)
def _quiet_hooks(monkeypatch):
    """The patient notices of a paid/expired difference are not what is tested here."""
    monkeypatch.setattr(service_change, "ADJUSTMENT_EXPIRED_HOOKS", [])
    monkeypatch.setattr(service_change, "ADJUSTMENT_APPLIED_HOOKS", [])


@dataclass
class World:
    tenant: Tenant
    patient: Patient
    doctor: Professional
    consulta: UUID
    exame: UUID
    sede: Unit
    centro: Unit


def _entries(consulta: UUID, exame: UUID, price="R$ 200,00") -> list[dict]:
    return [
        {
            "service_id": str(consulta),
            "name": "Consulta",
            "price": price,
            "duration_min": 30,
            "is_active": True,
        },
        {
            "service_id": str(exame),
            "name": "Exame",
            "price": "R$ 300,00",
            "duration_min": 60,
            "is_active": True,
        },
    ]


async def _seed(
    db,
    *,
    terms: tuple = (),
    percent: int = 30,
    timing: str = "after",
    consulta_price="R$ 200,00",
    name: str = "Clínica A",
) -> World:
    """`terms` = (service attr, unit attr, price, duration) exceptions of Dra. Ana."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name=name,
            phone_number_id=str(uuid4())[:12],
            is_active=True,
            timezone="America/Sao_Paulo",
            business_hours=WEEK,
            pix_deposit_enabled=True,
            pix_deposit_percent=percent,
            deposit_timing=timing,
            deposit_hold_ttl_minutes=30,
            pix_difference_refund_min_days=2,
            contact_email="clinica@example.test",
            appointment_types=[],
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add_all([tenant, patient])
        await session.flush()
        consulta = Service(
            id=uuid4(), tenant_id=tenant.id, name="Consulta", normalized_name="consulta"
        )
        exame = Service(id=uuid4(), tenant_id=tenant.id, name="Exame", normalized_name="exame")
        sede = Unit(id=uuid4(), tenant_id=tenant.id, name="Sede", address="Rua A, 1")
        centro = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro", address="Rua B, 2")
        doctor = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            business_hours=WEEK,
            appointment_types=_entries(consulta.id, exame.id, consulta_price),
        )
        session.add_all([consulta, exame, sede, centro, doctor])
        await session.flush()
        session.add_all(
            [
                ProfessionalUnit(
                    professional_id=doctor.id, unit_id=sede.id, sort_order=0, business_hours=WEEK
                ),
                ProfessionalUnit(
                    professional_id=doctor.id, unit_id=centro.id, sort_order=1, business_hours=WEEK
                ),
            ]
        )
        world = World(tenant, patient, doctor, consulta.id, exame.id, sede, centro)
        for service_attr, unit_attr, price, minutes in terms:
            session.add(
                ProfessionalServiceUnitTerms(
                    tenant_id=tenant.id,
                    professional_id=doctor.id,
                    service_id=getattr(world, service_attr),
                    unit_id=getattr(world, unit_attr).id,
                    price=price,
                    duration_min=minutes,
                )
            )
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
        row.status = ACCOUNT_CONNECTED
        row.last_verified_at = datetime.now(UTC)
        await session.commit()
    return world


CENTRO_250 = (("consulta", "centro", "R$ 250,00", None),)


async def _booked(
    db, world: World, unit: Unit | None, *, service="Consulta", paid: int | None = None
):
    """One live appointment of Dra. Ana at `unit` (and its PAID sinal of `paid` cents)."""
    async with db() as session:
        start = NOW + timedelta(days=5)
        appointment = Appointment(
            id=uuid4(),
            tenant_id=world.tenant.id,
            patient_id=world.patient.id,
            google_event_id="evt-1",
            appointment_type=service,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            phone=world.patient.wa_id,
            status=AppointmentStatus.SCHEDULED,
            professional_id=world.doctor.id,
            unit_id=unit.id if unit is not None else None,
        )
        session.add(appointment)
        await session.flush()
        deposit = None
        if paid is not None:
            deposit = PixDeposit(
                tenant_id=world.tenant.id,
                appointment_id=appointment.id,
                patient_id=world.patient.id,
                amount_cents=paid,
                percent_applied=30,
                status=PixDepositStatus.PAID,
                provider="asaas",
                provider_charge_id=f"pay_sinal_{appointment.id.hex[:8]}",
                external_reference=f"dep-ref-{appointment.id.hex[:8]}",
                deposit_timing="after",
                paid_amount_cents=paid,
            )
            session.add(deposit)
        await session.commit()
    return appointment, deposit


async def _price(db, world, appointment):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        return await deposit_lifecycle._resolve_service_and_price(session, tenant, appointment)


# --------------------------------------------------------------------------------------------
# characterization: no exception -> exactly today's values
# --------------------------------------------------------------------------------------------


async def test_without_exceptions_the_price_is_todays_at_every_unit(db):
    world = await _seed(db)
    for unit in (world.sede, world.centro, None):
        appointment, _ = await _booked(db, world, unit)
        assert await _price(db, world, appointment) == ("Consulta", "R$ 200,00")


async def test_without_exceptions_a_raw_base_price_is_read_exactly_as_today(db):
    """A JSON-number price stays its `str`, a zero price stays "no price" (today's truthiness),
    even with an exception for ANOTHER unit on the same service."""
    world = await _seed(db, consulta_price=200, terms=(("consulta", "sede", None, 45),))
    appointment, _ = await _booked(db, world, world.centro)
    assert await _price(db, world, appointment) == ("Consulta", "200")
    zero = await _seed(db, consulta_price=0, name="Clínica Zero")
    appointment, _ = await _booked(db, zero, zero.centro)
    assert await _price(db, zero, appointment) == ("Consulta", None)


async def _after_sinal(db, world, appointment, monkeypatch, provider) -> int | None:
    sent: list[int] = []

    async def no_send(session, tenant, appointment, patient, amount_cents, *args, **kw):
        sent.append(amount_cents)

    monkeypatch.setattr(deposit_lifecycle, "_send_deposit_request", no_send)
    provider.created_charge = CreatedCharge(
        provider_charge_id="pay_1", copy_paste="PIX", provider_customer_id="cus_1"
    )
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        patient = await session.get(Patient, world.patient.id)
        row = await session.get(Appointment, appointment.id)
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=row, waba_token="tok"
        )
        await session.commit()
    if deposit is None:
        return None
    [request] = [call[1] for call in provider.calls if call[0] == "create_pix_charge"]
    assert sent == [request.amount_cents] == [deposit.amount_cents]
    return deposit.amount_cents


async def test_without_exceptions_the_after_sinal_is_todays(db, monkeypatch, provider):
    world = await _seed(db)
    appointment, _ = await _booked(db, world, world.centro)
    assert await _after_sinal(db, world, appointment, monkeypatch, provider) == 6000


async def _quote(db, world, **kwargs):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        return await deposit_gate.quote_deposit(
            session,
            tenant,
            patient_id=world.patient.id,
            appointment_type="Consulta",
            professional_id=world.doctor.id,
            insurance=None,
            **kwargs,
        )


async def test_without_exceptions_the_before_quote_is_todays(db):
    world = await _seed(db, timing="before")
    quote = await _quote(db, world)
    assert (quote.outcome, quote.amount_cents, quote.service_label) == (
        "required",
        6000,
        "Consulta",
    )


async def _preview(db, world, appointment, deposit, service="Consulta", **kwargs):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        row = await session.get(Appointment, appointment.id)
        paid = await session.get(PixDeposit, deposit.id)
        return await service_change.preview_service_change(
            session,
            tenant=tenant,
            appointment=row,
            deposit=paid,
            new_service=service,
            professional_id=world.doctor.id,
            start_at=row.start_at,
            now=NOW,
            **kwargs,
        )


async def test_without_exceptions_the_service_change_decision_is_todays(db):
    world = await _seed(db)
    appointment, deposit = await _booked(db, world, world.sede, paid=6000)
    assert (await _preview(db, world, appointment, deposit)).action == "no_change"
    exame = await _preview(db, world, appointment, deposit, "Exame")
    assert (exame.action, exame.diff_cents) == ("increase_charge", 3000)


async def _readiness_names(db, world) -> dict[str, tuple[str, ...]]:
    async with db() as session:
        items = await readiness.evaluate_deposit_readiness(session, tenant_id=world.tenant.id)
    return {item.code: item.names for item in items}


async def test_without_exceptions_the_readiness_checklist_is_todays(db):
    world = await _seed(db, timing="before", percent=2)  # 2 %: R$ 4,00 and R$ 6,00
    found = await _readiness_names(db, world)
    assert found["amount_below_minimum"] == ("Dra. Ana — Consulta: sinal de R$ 4,00",)
    assert "services_without_price" not in found


# --------------------------------------------------------------------------------------------
# A1 - the "after" sinal is computed at the appointment's unit
# --------------------------------------------------------------------------------------------


async def test_a1_the_after_sinal_at_centro_is_charged_on_the_units_own_price(
    db, monkeypatch, provider
):
    world = await _seed(db, terms=CENTRO_250)
    at_centro, _ = await _booked(db, world, world.centro)
    assert await _price(db, world, at_centro) == ("Consulta", "R$ 250,00")
    assert await _after_sinal(db, world, at_centro, monkeypatch, provider) == 7500


async def test_a1_the_after_sinal_at_sede_without_its_own_price_is_the_base(
    db, monkeypatch, provider
):
    world = await _seed(db, terms=CENTRO_250)
    at_sede, _ = await _booked(db, world, world.sede)
    assert await _after_sinal(db, world, at_sede, monkeypatch, provider) == 6000
    no_unit, _ = await _booked(db, world, None)
    assert await _price(db, world, no_unit) == ("Consulta", "R$ 200,00")


async def test_a1_a_duration_only_exception_keeps_the_base_price(db):
    world = await _seed(db, terms=(("consulta", "centro", None, 45),))
    at_centro, _ = await _booked(db, world, world.centro)
    assert await _price(db, world, at_centro) == ("Consulta", "R$ 200,00")


# --------------------------------------------------------------------------------------------
# A2 - the "before" quote: the patient sees R$ 250 at Centro and pays the sinal on R$ 250
# --------------------------------------------------------------------------------------------


async def test_a2_the_quote_at_centro_is_on_the_units_price(db):
    world = await _seed(db, timing="before", terms=CENTRO_250)
    assert (await _quote(db, world, unit_id=world.centro.id)).amount_cents == 7500
    assert (await _quote(db, world, unit_id=world.sede.id)).amount_cents == 6000
    assert (await _quote(db, world, unit_id=None)).amount_cents == 6000
    assert (await _quote(db, world)).amount_cents == 6000


_REAL_QUOTE = deposit_gate.quote_deposit


async def _real_quote(session, tenant, kwargs):
    """The real `quote_deposit` (not the spy) with the arguments a caller passed."""
    return await _REAL_QUOTE(session, tenant, **kwargs)


class _QuoteSpy:
    def __init__(self, monkeypatch, answer=None):
        self.kwargs: list[dict] = []
        real = deposit_gate.quote_deposit

        async def spy(session, tenant, **kwargs):
            self.kwargs.append(kwargs)
            if answer is not None:
                return answer
            return await real(session, tenant, **kwargs)

        monkeypatch.setattr(deposit_gate, "quote_deposit", spy)


async def test_a2_the_booking_gate_quotes_at_the_holds_unit(db, monkeypatch):
    """WhatsApp goes straight to the Pix: the hold is at Centro, so is the quote and the charge."""
    world = await _seed(db, timing="before", terms=CENTRO_250)
    async with db() as session:
        conversation = Conversation(
            id=uuid4(), tenant_id=world.tenant.id, patient_id=world.patient.id
        )
        session.add(conversation)
        await session.commit()
    fake = FakeProvider()
    fake.created_charge = CreatedCharge(
        provider_charge_id="pay_1", copy_paste="PIX", provider_customer_id="cus_1"
    )
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)

    async def request_code(tenant_id, external_id):  # pragma: no cover - WhatsApp: no code
        return RequestCodeResult(RequestCodeOutcome.SENT, email_masked="m***a@x.com")

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(holds, "request_code", request_code)
    monkeypatch.setattr(email, "send_payment_alert", quiet)
    monkeypatch.setattr(deposit_failure_notice, "notify_deposit_failure", quiet)
    spy = _QuoteSpy(monkeypatch)
    gate = holds.BookingGate(
        tenant_id=world.tenant.id,
        conversation_id=conversation.id,
        patient_id=None,
        external_id="",
        armed=False,
    )
    start = NOW + timedelta(days=30)
    decision = await gate.decide(
        start_at=start,
        end_at=start + timedelta(minutes=30),
        professional_id=world.doctor.id,
        appointment_type="Consulta",
        insurance=None,
        unit_id=world.centro.id,
    )
    assert [kwargs.get("unit_id") for kwargs in spy.kwargs] == [world.centro.id]
    assert decision.outcome == "awaiting_payment"
    assert decision.payment.amount_cents == 7500


async def test_a2_the_portal_code_turn_quotes_at_the_holds_unit(db, monkeypatch):
    """`deposit_turn._payment_after_code`: the code-phase hold remembers the unit; so does the
    quote made once the code is verified."""
    from secretaria.workers import tasks
    from tests._patching import workers_ns

    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    world = await _seed(db, timing="before", terms=CENTRO_250)
    async with db() as session:
        conversation = Conversation(
            id=uuid4(), tenant_id=world.tenant.id, patient_id=world.patient.id
        )
        session.add(conversation)
        await session.commit()
        tenant = await session.get(Tenant, world.tenant.id)
    start = NOW + timedelta(days=30)
    held = await holds.place_hold(
        tenant_id=world.tenant.id,
        conversation_id=conversation.id,
        patient_id=world.patient.id,
        professional_id=world.doctor.id,
        appointment_type="Consulta",
        insurance=None,
        start_at=start,
        end_at=start + timedelta(minutes=30),
        unit_id=world.centro.id,
    )
    assert held is not None
    spy = _QuoteSpy(monkeypatch, answer=deposit_gate.DepositQuote("no_deposit", "timing_after"))
    reply = tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_ref="x",
        inbound_body="123456",
        channel="brain_message",
        tenant_id=world.tenant.id,
    )
    assert await tasks._payment_after_code(reply, tenant=tenant, redis=None) is None
    [kwargs] = spy.kwargs
    assert kwargs["unit_id"] == world.centro.id
    # The same arguments quote R$ 75,00 (the amount the Pix of this hold would carry).
    async with db() as session:
        fresh = await session.get(Tenant, world.tenant.id)
        quote = await _real_quote(session, fresh, kwargs)
    assert quote.amount_cents == 7500


# --------------------------------------------------------------------------------------------
# A3 - the service change resolves the new price at the unit of the change
# --------------------------------------------------------------------------------------------


async def test_a3_preview_resolves_at_the_appointments_unit_unless_told_otherwise(db):
    world = await _seed(db, terms=CENTRO_250)
    appointment, deposit = await _booked(db, world, world.sede, paid=6000)
    assert (await _preview(db, world, appointment, deposit)).action == "no_change"
    moved = await _preview(db, world, appointment, deposit, unit_id=world.centro.id)
    assert (moved.action, moved.diff_cents) == ("increase_charge", 1500)
    at_centro, paid_centro = await _booked(db, world, world.centro, paid=7500)
    assert (await _preview(db, world, at_centro, paid_centro)).action == "no_change"
    back = await _preview(db, world, at_centro, paid_centro, unit_id=None)
    assert (back.action, back.diff_cents) == ("decrease_ask", 1500)


async def _request(db, world, appointment, deposit, service, **edit):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        row = await session.get(Appointment, appointment.id)
        paid = await session.get(PixDeposit, deposit.id)
        pending_edit = {
            "appointment_id": row.id,
            "appointment_type": service,
            "professional_id": world.doctor.id,
            "insurance": None,
            "attendee_name": None,
            "google_event_id": row.google_event_id,
            "google_event_link": None,
            "start_at": row.start_at,
            "end_at": row.end_at,
            "time_changed": False,
            "doctor_changed": False,
            "calendar_changed": False,
            **edit,
        }
        result = await service_change.request_service_change(
            session,
            tenant=tenant,
            appointment=row,
            deposit=paid,
            new_service=service,
            pending_edit=pending_edit,
            now=NOW,
        )
        await session.commit()
    return result


async def _adjustments(db) -> list[DepositAdjustment]:
    async with db() as session:
        return list((await session.scalars(select(DepositAdjustment))).all())


async def _row(db, appointment_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def test_a3_request_resolves_at_the_pending_edits_unit_when_the_key_is_there(db, provider):
    """Paid at Sede (R$ 200 -> R$ 60); the same service moved to Centro (R$ 250 -> R$ 75)."""
    world = await _seed(db, terms=CENTRO_250)
    appointment, deposit = await _booked(db, world, world.sede, paid=6000)
    result = await _request(db, world, appointment, deposit, "Consulta", unit_id=world.centro.id)
    assert (result.kind, result.diff_cents) == ("awaiting_payment", 1500)
    assert result.label == "Consulta na Unidade Centro"
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_AWAITING_PAYMENT
    assert (adjustment.old_service, adjustment.new_service) == (
        "Consulta na Unidade Sede",
        "Consulta na Unidade Centro",
    )
    # The data written when it is paid stays the SERVICE's name and the new unit.
    assert adjustment.pending_edit["appointment_type"] == "Consulta"
    assert adjustment.pending_edit["unit_id"] == str(world.centro.id)
    [charge] = [call[1] for call in provider.calls if call[0] == "create_pix_charge"]
    assert charge.amount_cents == 1500
    assert "Consulta na Unidade Centro" in charge.description
    row = await _row(db, appointment.id)
    assert (row.appointment_type, row.unit_id) == ("Consulta", world.sede.id)

    # Paid: the row moves to Centro, the notice names the new place.
    hook._paid(provider, result, amount=1500)
    processed = await hook._process(db, world.tenant, amount=1500)
    assert processed.outcome == "adjustment_applied"
    row = await _row(db, appointment.id)
    assert (row.appointment_type, row.unit_id) == ("Consulta", world.centro.id)
    assert service_change.adjustment_applied_text(adjustment.new_service, 1500).endswith(
        "Sua consulta agora é Consulta na Unidade Centro."
    )


async def test_a3_a_unit_key_with_none_resolves_at_the_base(db, provider):
    world = await _seed(db, terms=CENTRO_250)
    appointment, deposit = await _booked(db, world, world.centro, paid=7500)
    result = await _request(db, world, appointment, deposit, "Consulta", unit_id=None)
    assert (result.kind, result.diff_cents) == ("asked", 1500)
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_ASKED
    # A unit cleared: the old place is named, the new one is the service alone.
    assert (adjustment.old_service, adjustment.new_service) == (
        "Consulta na Unidade Centro",
        "Consulta",
    )
    row = await _row(db, appointment.id)
    assert (row.appointment_type, row.unit_id) == ("Consulta", None)


async def test_a3_without_the_key_the_new_service_is_priced_at_the_appointments_unit(db, provider):
    """Exame R$ 300 (base) / R$ 400 at Centro. Booked at Centro (Consulta R$ 250 -> R$ 75):
    a change to Exame with no unit in the edit stays at Centro -> R$ 120, +R$ 45."""
    world = await _seed(db, terms=(*CENTRO_250, ("exame", "centro", "R$ 400,00", None)))
    appointment, deposit = await _booked(db, world, world.centro, paid=7500)
    result = await _request(db, world, appointment, deposit, "Exame")
    assert (result.kind, result.diff_cents) == ("awaiting_payment", 4500)
    assert result.label is None  # a service change keeps today's label
    [adjustment] = await _adjustments(db)
    assert (adjustment.old_service, adjustment.new_service) == ("Consulta", "Exame")


async def test_a3_a_service_change_with_a_new_unit_keeps_todays_label(db, provider):
    world = await _seed(db, terms=(("exame", "centro", "R$ 400,00", None),))
    appointment, deposit = await _booked(db, world, world.sede, paid=6000)
    result = await _request(db, world, appointment, deposit, "Exame", unit_id=world.centro.id)
    assert (result.kind, result.diff_cents, result.label) == ("awaiting_payment", 6000, None)
    [adjustment] = await _adjustments(db)
    assert (adjustment.old_service, adjustment.new_service) == ("Consulta", "Exame")


async def test_a3_a_foreign_professional_or_unit_never_leaks_a_price(db):
    """Clinic B: its own Dra. Ana charges R$ 900 (R$ 950 at its Centro). Neither reaches A."""
    world = await _seed(db, terms=CENTRO_250)
    other = await _seed(
        db,
        name="Clínica B",
        consulta_price="R$ 900,00",
        terms=(("consulta", "centro", "R$ 950,00", None),),
    )
    appointment, deposit = await _booked(db, world, world.sede, paid=6000)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        row = await session.get(Appointment, appointment.id)
        paid = await session.get(PixDeposit, deposit.id)
        foreign_doctor = await service_change.preview_service_change(
            session,
            tenant=tenant,
            appointment=row,
            deposit=paid,
            new_service="Consulta",
            professional_id=other.doctor.id,
            start_at=row.start_at,
            now=NOW,
        )
        foreign_unit = await service_change.preview_service_change(
            session,
            tenant=tenant,
            appointment=row,
            deposit=paid,
            new_service="Consulta",
            professional_id=world.doctor.id,
            start_at=row.start_at,
            now=NOW,
            unit_id=other.centro.id,
        )
    # Not B's R$ 900: A's own catalog has no such service for an unknown doctor.
    assert (foreign_doctor.action, foreign_doctor.reason) == ("blocked", "price_unavailable")
    # Not B's R$ 950: A's doctor at a unit A does not know is A's base.
    assert foreign_unit.action == "no_change"


# --------------------------------------------------------------------------------------------
# A4 / D8 - the readiness checklist evaluates every unit's own price
# --------------------------------------------------------------------------------------------


async def test_a4_a_unit_price_below_the_floor_is_named_with_its_unit(db):
    world = await _seed(db, timing="before", terms=(("consulta", "centro", "R$ 10,00", None),))
    found = await _readiness_names(db, world)
    assert found["amount_below_minimum"] == ("Dra. Ana — Consulta (Centro): sinal de R$ 3,00",)


async def test_a4_a_unit_price_equal_to_a_low_base_is_not_warned_twice(db):
    """Base R$ 10 (sinal R$ 3) and Centro's own "R$ 10" (same cents): one line, the base's."""
    world = await _seed(
        db, timing="before", consulta_price="R$ 10,00", terms=(("consulta", "centro", "10", None),)
    )
    found = await _readiness_names(db, world)
    assert found["amount_below_minimum"] == ("Dra. Ana — Consulta: sinal de R$ 3,00",)


async def test_a4_base_fine_and_unit_fine_raise_no_warning(db):
    world = await _seed(db, timing="before", terms=CENTRO_250)
    found = await _readiness_names(db, world)
    assert "amount_below_minimum" not in found and "services_without_price" not in found


async def test_a4_an_unreadable_unit_price_is_a_service_without_price(db):
    world = await _seed(db, timing="before", terms=(("consulta", "centro", "a combinar", None),))
    found = await _readiness_names(db, world)
    assert found["services_without_price"] == ("Dra. Ana — Consulta (Centro)",)


async def test_a4_exceptions_nobody_can_book_raise_no_warning(db):
    """A deactivated unit, a link switched off, and a unit where the service is not done."""
    from secretaria.models.professional_service_unit import ProfessionalServiceUnit

    world = await _seed(
        db,
        timing="before",
        terms=(
            ("consulta", "centro", "R$ 10,00", None),
            ("exame", "sede", "R$ 10,00", None),
        ),
    )
    async with db() as session:
        centro = await session.get(Unit, world.centro.id)
        centro.is_active = False
        # Exame only at Centro (now inactive): its Sede exception is never booked either.
        session.add(
            ProfessionalServiceUnit(
                professional_id=world.doctor.id, service_id=world.exame, unit_id=world.centro.id
            )
        )
        await session.commit()
    found = await _readiness_names(db, world)
    assert "amount_below_minimum" not in found

    linked_off = await _seed(
        db, name="Clínica C", timing="before", terms=(("consulta", "centro", "R$ 10,00", None),)
    )
    async with db() as session:
        link = await session.scalar(
            select(ProfessionalUnit).where(
                ProfessionalUnit.professional_id == linked_off.doctor.id,
                ProfessionalUnit.unit_id == linked_off.centro.id,
            )
        )
        link.is_active = False
        await session.commit()
    assert "amount_below_minimum" not in await _readiness_names(db, linked_off)


# --------------------------------------------------------------------------------------------
# tenant isolation
# --------------------------------------------------------------------------------------------


async def test_another_clinics_exception_never_changes_this_clinics_sinal(
    db, monkeypatch, provider
):
    """A row stamped with clinic B's tenant (the writer never makes one) is invisible to A."""
    world = await _seed(db)
    other = await _seed(db, name="Clínica B")
    async with db() as session:
        session.add(
            ProfessionalServiceUnitTerms(
                tenant_id=other.tenant.id,
                professional_id=world.doctor.id,
                service_id=world.consulta,
                unit_id=world.sede.id,
                price="R$ 999,00",
            )
        )
        await session.commit()
    appointment, _ = await _booked(db, world, world.sede)
    assert await _price(db, world, appointment) == ("Consulta", "R$ 200,00")
    assert await _after_sinal(db, world, appointment, monkeypatch, provider) == 6000
