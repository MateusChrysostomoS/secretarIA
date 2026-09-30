"""`quote_deposit` - the decision the `before` flow makes before it reserves a slot
(spec sections 5.3 and 7, the first six of the nine `maybe_create_deposit` silent paths).

One test per row of the table in the plan: feature off / add-on cancelled / convenio without
deposit / service without a readable price / zero amount are LEGITIMATE `no_deposit` (the booking
commits as before); no patient / no credential / unknown provider / an entitlement nobody can
read are BLOCKED (the booking is never made without its deposit).
"""

from uuid import uuid4

import pytest
import pytest_asyncio

from secretaria.models import Patient, Tenant
from secretaria.models.insurance import TenantInsurancePlan
from secretaria.services import tenant_config as cfg
from secretaria.services.entitlements_client import EntitlementSummary
from secretaria.services.payments import accounts, deposit_gate
from tests.payments_support import FakeProvider, make_sessionmaker


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


def _summary(*, active=True, addon=True) -> EntitlementSummary:
    return EntitlementSummary(
        tenant_id="t",
        status="active" if active else "canceled",
        active=active,
        secretaria_enabled=True,
        plan="pro",
        secretaria_tier="secretaria",
        addons={"pix_deposit": addon},
        limits={},
    )


@pytest.fixture
def entitlement(monkeypatch):
    """`entitlement.set(summary_or_None)` scripts what brain-api answers."""
    state = {"value": _summary()}

    async def fake(tenant_id, redis):
        return state["value"]

    monkeypatch.setattr(deposit_gate, "get_entitlements", fake)

    class Handle:
        @staticmethod
        def set(value):
            state["value"] = value

    return Handle


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    from secretaria.services.payments import registry

    monkeypatch.setitem(registry._PROVIDERS, "asaas", FakeProvider())


async def _seed(session, *, with_account=True, account_status="connected", **tenant_kw):
    values = {
        "clinic_name": "Clinic",
        "phone_number_id": str(uuid4())[:12],
        "pix_deposit_enabled": True,
        "pix_deposit_percent": 30,
        "deposit_timing": "before",
        "deposit_hold_ttl_minutes": 45,
        "appointment_types": [
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 200,00"}
        ],
    }
    values.update(tenant_kw)
    tenant = Tenant(id=uuid4(), **values)
    patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
    session.add_all([tenant, patient])
    await session.flush()
    if with_account:
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
        row.status = account_status
        row.last_verified_at = None if account_status != "connected" else _now()
    await session.commit()
    return tenant, patient


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def _quote(session, tenant, patient, *, service="Consulta", insurance=None):
    return await deposit_gate.quote_deposit(
        session,
        tenant,
        patient_id=patient.id if patient else None,
        appointment_type=service,
        professional_id=None,
        insurance=insurance,
    )


async def test_a_priced_service_with_everything_in_place_requires_the_deposit(db, entitlement):
    async with db() as session:
        tenant, patient = await _seed(session)
        quote = await _quote(session, tenant, patient)
    assert quote.outcome == "required"
    assert quote.amount_cents == 6000  # 30% of R$ 200,00
    assert quote.provider == "asaas"
    assert quote.ttl_minutes == 45
    assert quote.service_label == "Consulta"


async def test_timing_after_never_gates_a_booking(db, entitlement):
    async with db() as session:
        tenant, patient = await _seed(session, deposit_timing="after")
        quote = await _quote(session, tenant, patient)
    assert (quote.outcome, quote.reason) == ("no_deposit", "timing_after")


async def test_path_1_the_feature_switched_off_is_a_normal_booking(db, entitlement):
    async with db() as session:
        tenant, patient = await _seed(session, pix_deposit_enabled=False)
        quote = await _quote(session, tenant, patient)
    assert (quote.outcome, quote.reason) == ("no_deposit", "disabled")


async def test_a_cancelled_addon_never_stops_the_agenda(db, entitlement):
    entitlement.set(_summary(addon=False))
    async with db() as session:
        tenant, patient = await _seed(session)
        quote = await _quote(session, tenant, patient)
    assert (quote.outcome, quote.reason) == ("no_deposit", "no_entitlement")


async def test_an_inactive_subscription_is_not_entitled_either(db, entitlement):
    entitlement.set(_summary(active=False))
    async with db() as session:
        tenant, patient = await _seed(session)
        quote = await _quote(session, tenant, patient)
    assert quote.reason == "no_entitlement"


async def test_an_entitlement_nobody_can_read_blocks_instead_of_guessing(db, entitlement):
    entitlement.set(None)
    async with db() as session:
        tenant, patient = await _seed(session)
        quote = await _quote(session, tenant, patient)
    assert (quote.outcome, quote.reason) == ("blocked", "entitlement_unresolvable")


async def test_path_2_a_plan_that_does_not_charge_a_deposit_books_normally(db, entitlement):
    async with db() as session:
        tenant, patient = await _seed(session)
        plan = TenantInsurancePlan(
            id=uuid4(), tenant_id=tenant.id, custom_name="Unimed", charge_deposit=False
        )
        session.add(plan)
        await session.commit()
        quote = await _quote(session, tenant, patient, insurance="Unimed")
    assert (quote.outcome, quote.reason) == ("no_deposit", "insurance_no_charge")


async def test_path_5_a_service_without_a_readable_price_books_normally(db, entitlement):
    async with db() as session:
        tenant, patient = await _seed(
            session,
            appointment_types=[
                {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "a combinar"}
            ],
        )
        quote = await _quote(session, tenant, patient)
    assert (quote.outcome, quote.reason) == ("no_deposit", "unparseable_price")


async def test_path_5_a_service_the_catalog_does_not_have_books_normally(db, entitlement):
    async with db() as session:
        tenant, patient = await _seed(session)
        quote = await _quote(session, tenant, patient, service="Inexistente")
    assert (quote.outcome, quote.reason) == ("no_deposit", "no_service")


async def test_path_6_a_zero_amount_books_normally(db, entitlement):
    async with db() as session:
        tenant, patient = await _seed(session, pix_deposit_percent=1)
        tenant.appointment_types = [
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 0,10"}
        ]
        quote = await _quote(session, tenant, patient)
    assert (quote.outcome, quote.reason) == ("no_deposit", "zero_amount")


async def test_path_3_no_patient_blocks(db, entitlement):
    async with db() as session:
        tenant, _patient = await _seed(session)
        quote = await _quote(session, tenant, None)
    assert (quote.outcome, quote.reason) == ("blocked", "no_patient")


async def test_path_4_no_credential_blocks(db, entitlement):
    async with db() as session:
        tenant, patient = await _seed(session, with_account=False)
        quote = await _quote(session, tenant, patient)
    assert (quote.outcome, quote.reason) == ("blocked", "no_credential")


@pytest.mark.parametrize("status", ["incomplete", "reauth_required", "error"])
async def test_path_4_an_account_that_is_not_connected_and_verified_blocks(
    db, entitlement, status
):
    async with db() as session:
        tenant, patient = await _seed(session, account_status=status)
        quote = await _quote(session, tenant, patient)
    assert (quote.outcome, quote.reason) == ("blocked", "no_credential")


async def test_an_unknown_provider_blocks(db, entitlement):
    async with db() as session:
        tenant, patient = await _seed(session, payment_provider="nopay")
        quote = await _quote(session, tenant, patient)
    assert (quote.outcome, quote.reason) == ("blocked", "no_provider")


async def test_a_no_price_service_needs_no_credential_at_all(db, entitlement):
    """Legitimate `no_deposit` is decided before the operational checks: a clinic whose key
    broke can still book the services that carry no deposit."""
    async with db() as session:
        tenant, patient = await _seed(
            session,
            with_account=False,
            appointment_types=[
                {"name": "Consulta", "duration_min": 30, "is_active": True, "price": ""}
            ],
        )
        quote = await _quote(session, tenant, patient)
    assert quote.outcome == "no_deposit"


@pytest.mark.parametrize(
    ("stored", "expected"), [(None, 30), (0, 30), (10, 30), (45, 45), (500, 120)]
)
def test_the_hold_ttl_is_always_inside_the_supported_range(stored, expected):
    assert deposit_gate.clamp_ttl(stored) == expected


async def test_the_llm_guard_is_active_only_for_an_enabled_before_clinic(db, entitlement):
    async with db() as session:
        before, _ = await _seed(session)
        after, _ = await _seed(session, deposit_timing="after")
        off, _ = await _seed(session, pix_deposit_enabled=False)
    assert await deposit_gate.before_gate_active(before) is True
    assert await deposit_gate.before_gate_active(after) is False
    assert await deposit_gate.before_gate_active(off) is False


async def test_the_llm_guard_fails_closed_when_the_entitlement_is_unreadable(db, entitlement):
    entitlement.set(None)
    async with db() as session:
        tenant, _ = await _seed(session)
    assert await deposit_gate.before_gate_active(tenant) is True


async def test_the_llm_guard_steps_aside_when_the_addon_was_cancelled(db, entitlement):
    entitlement.set(_summary(addon=False))
    async with db() as session:
        tenant, _ = await _seed(session)
    assert await deposit_gate.before_gate_active(tenant) is False
