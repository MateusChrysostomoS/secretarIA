"""TASK-085 R U1 - D-R12 (owner, 2026-11-18): a convênio never exempts the sinal.

We cannot check that the patient really has the convênio, so with the clinic's sinal ON every
consultation pays it, whatever the convênio. The old per-convênio "não cobra sinal" flag
(`tenant_insurance_plans.charge_deposit` / `professional_insurance_plans.charge_deposit`) stays in
the database but is IGNORED, and the hub no longer offers it. The detail text under each convênio
in the patient's list ("Varia por plano...", "Pagamento: confirme...") is gone (owner: "TIRE
ISSO"), on WhatsApp and on the Portal.

What each block catches:
- characterization: a clinic WITHOUT sinal sees nothing change (same answers before and after U1);
- the booking gate (`before`), the post-booking hook (`after`) and the re-quote of an unpaid sinal
  all charge a booking under a legacy "no sinal" plan, in both insurance topologies;
- the paid-sinal change engine's pure rule no longer has a "new booking without sinal" input;
- the patient's convênio overview carries no per-plan payment text and no per-plan deposit;
- the convênio rows are title-only on both channels;
- the hub: no flag in what it returns, an older Portal that still sends the flag is not refused
  (ignored), and a legacy value already stored is left alone.
U1b removed the one-release compat and unmapped the legacy columns: a legacy "no sinal" row is now
planted with SQL on the production-shaped column (tests/_legacy_insurance_flag.py); the hub's
"no flag anywhere" contract is pinned in test_sinal_trocas_U1b_option_gone.py.
"""

import importlib.util
import inspect
import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

from datetime import UTC, datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Patient,
    Professional,
    Tenant,
)
from secretaria.models.insurance import (  # noqa: E402
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.services import flow_router, tenant_config  # noqa: E402
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    ensure_catalog_seeded,
)
from secretaria.services.patient_insurance_overview import (  # noqa: E402
    patient_insurance_overview,
)
from secretaria.services.payments import accounts, deposit_gate, deposit_lifecycle  # noqa: E402
from secretaria.services.payments.service_change_policy import (  # noqa: E402
    decide_service_change,
)
from tests._legacy_insurance_flag import legacy_flags, set_legacy_flag  # noqa: E402
from tests.payments_support import FakeProvider, make_sessionmaker  # noqa: E402

UNIMED = catalog_id_for_slug("unimed")
AMIL = catalog_id_for_slug("amil")
REMOVED_TEXTS = (
    "Varia por plano",
    "Pagamento: confirme",
    "Reembolso:",
    "Coparticipação:",
    "Desconto direto:",
)


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    from secretaria.services.payments import registry

    monkeypatch.setitem(registry._PROVIDERS, "asaas", FakeProvider())


@pytest.fixture
def skips(monkeypatch):
    reasons: list[str] = []
    original = deposit_lifecycle._log_deposit_skip

    def _record(tenant, appointment, reason):
        reasons.append(reason)
        original(tenant, appointment, reason)

    monkeypatch.setattr(deposit_lifecycle, "_log_deposit_skip", _record)
    return reasons


async def _clinic(session, *, sinal=True, timing="before", mode="shared", account=True):
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinic",
        phone_number_id=str(uuid4())[:12],
        pix_deposit_enabled=sinal,
        pix_deposit_percent=30,
        deposit_timing=timing,
        insurance_mode=mode,
        appointment_types=[
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 200,00"}
        ],
    )
    patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
    session.add_all([tenant, patient])
    await session.flush()
    if account:
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        tenant_config.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
        row.status = "connected"
        row.last_verified_at = datetime.now(UTC)
    await session.commit()
    return tenant, patient


async def _legacy_no_sinal_plan(session, tenant, name="Unimed") -> TenantInsurancePlan:
    """A clinic row saved while the "não cobra sinal" option still existed."""
    plan = TenantInsurancePlan(id=uuid4(), tenant_id=tenant.id, custom_name=name)
    session.add(plan)
    await session.flush()
    await set_legacy_flag(session, "tenant_insurance_plans", plan.id, False)
    await session.commit()
    return plan


async def _quote(session, tenant, patient, insurance, professional_id=None):
    return await deposit_gate.quote_deposit(
        session,
        tenant,
        patient_id=patient.id,
        appointment_type="Consulta",
        professional_id=professional_id,
        insurance=insurance,
    )


def _appointment(tenant, patient, **kw) -> Appointment:
    return Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=patient.id,
        google_event_id="evt-1",
        appointment_type="Consulta",
        phone=patient.wa_id,
        status=AppointmentStatus.SCHEDULED,
        **kw,
    )


# --------------------------------------------------------------------------
# Characterization: a clinic without sinal sees nothing change
# --------------------------------------------------------------------------


async def test_no_sinal_clinic_the_gate_still_answers_disabled(db):
    async with db() as session:
        tenant, patient = await _clinic(session, sinal=False)
        await _legacy_no_sinal_plan(session, tenant)
        with_plan = await _quote(session, tenant, patient, "Unimed")
        particular = await _quote(session, tenant, patient, None)
    assert (with_plan.outcome, with_plan.reason) == ("no_deposit", "disabled")
    assert (particular.outcome, particular.reason) == ("no_deposit", "disabled")


async def test_no_sinal_clinic_the_after_hook_still_skips_as_disabled(db, skips):
    async with db() as session:
        tenant, patient = await _clinic(session, sinal=False, timing="after")
        plan = await _legacy_no_sinal_plan(session, tenant)
        appointment = _appointment(tenant, patient, insurance="Unimed", insurance_plan_id=plan.id)
        session.add(appointment)
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="t"
        )
    assert deposit is None
    assert skips == [deposit_lifecycle.DEPOSIT_SKIP_DISABLED]


async def test_no_sinal_clinic_a_requote_is_still_zero(db):
    async with db() as session:
        tenant, _patient = await _clinic(session, sinal=False)
        plan = await _legacy_no_sinal_plan(session, tenant)
        cents = await deposit_lifecycle.requote_amount_cents(
            session,
            tenant=tenant,
            appointment=SimpleNamespace(appointment_type="Consulta", insurance_plan_id=plan.id),
        )
    assert cents == 0


async def test_no_sinal_clinic_the_overview_still_says_no_sinal(db):
    async with db() as session:
        tenant, _patient = await _clinic(session, sinal=False)
        await _legacy_no_sinal_plan(session, tenant, name="GEAP")
        overview = await patient_insurance_overview(session, tenant.id)
    assert overview["deposit"] == {"enabled": False, "percent": None}
    # U1b: a plan without a clinic note is not listed on the patient's sheet.
    assert overview["plans"] == []


# --------------------------------------------------------------------------
# D-R12: with the sinal on, a convênio never exempts it
# --------------------------------------------------------------------------


async def test_before_gate_charges_a_legacy_no_sinal_clinic_plan(db):
    async with db() as session:
        tenant, patient = await _clinic(session)
        await _legacy_no_sinal_plan(session, tenant)
        quote = await _quote(session, tenant, patient, "Unimed")
    assert (quote.outcome, quote.amount_cents) == ("required", 6000)


async def test_before_gate_charges_a_legacy_no_sinal_doctor_plan_in_independent_mode(db):
    async with db() as session:
        tenant, patient = await _clinic(session, mode="independent")
        doctor = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        session.add(doctor)
        await session.flush()
        plan = ProfessionalInsurancePlan(
            professional_id=doctor.id, tenant_id=tenant.id, catalog_id=AMIL
        )
        session.add(plan)
        await session.flush()
        await set_legacy_flag(session, "professional_insurance_plans", plan.id, False)
        await session.commit()
        quote = await _quote(session, tenant, patient, "Amil", professional_id=doctor.id)
    assert (quote.outcome, quote.amount_cents) == ("required", 6000)


async def test_after_hook_goes_past_a_legacy_no_sinal_plan(db, skips):
    async with db() as session:
        tenant, patient = await _clinic(session, timing="after", account=False)
        plan = await _legacy_no_sinal_plan(session, tenant)
        appointment = _appointment(tenant, patient, insurance="Unimed", insurance_plan_id=plan.id)
        session.add(appointment)
        await session.commit()
        await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="t"
        )
    # Went past where the convênio used to stop it, and stopped at the next guard (no account).
    assert skips == [deposit_lifecycle.DEPOSIT_SKIP_NO_API_KEY]


async def test_requote_charges_the_full_sinal_under_a_legacy_no_sinal_plan(db):
    async with db() as session:
        tenant, _patient = await _clinic(session)
        plan = await _legacy_no_sinal_plan(session, tenant)
        with_plan = await deposit_lifecycle.requote_amount_cents(
            session,
            tenant=tenant,
            appointment=SimpleNamespace(appointment_type="Consulta", insurance_plan_id=plan.id),
        )
        particular = await deposit_lifecycle.requote_amount_cents(
            session, tenant=tenant, appointment=SimpleNamespace(appointment_type="Consulta")
        )
    assert with_plan == particular == 6000


def test_the_convenio_exemption_is_gone_from_the_code():
    assert not hasattr(deposit_lifecycle, "_insurance_charges_deposit")
    assert not hasattr(deposit_lifecycle, "DEPOSIT_SKIP_INSURANCE_NO_CHARGE")
    assert not hasattr(deposit_gate, "REASON_INSURANCE_NO_CHARGE")


def test_the_paid_change_rule_has_no_new_booking_without_sinal_input():
    """D-R3's "convênio sem sinal = sinal novo 0" is revoked: convênio <-> particular does not move
    the sinal by itself, so the pure rule no longer takes that input."""
    assert "new_charges_deposit" not in inspect.signature(decide_service_change).parameters


# --------------------------------------------------------------------------
# The patient's convênio list and overview: no detail text, no per-plan sinal
# --------------------------------------------------------------------------


async def test_overview_has_no_per_plan_payment_text_or_deposit(db):
    async with db() as session:
        tenant, _patient = await _clinic(session)
        session.add(TenantInsurancePlan(tenant_id=tenant.id, catalog_id=UNIMED))
        geap = TenantInsurancePlan(tenant_id=tenant.id, custom_name="GEAP")
        session.add(geap)
        await session.flush()
        await set_legacy_flag(session, "tenant_insurance_plans", geap.id, False)
        await session.commit()
        overview = await patient_insurance_overview(session, tenant.id)
    assert overview["deposit"] == {"enabled": True, "percent": 30}
    # TASK-090: no plan carries a clinic payment note any more, so none is listed.
    assert overview["plans"] == []


async def test_overview_route_returns_the_slim_shape(db, client):
    from secretaria.main import app

    async def _session():
        async with db() as session:
            yield session

    async with db() as session:
        tenant, _patient = await _clinic(session)
        session.add(TenantInsurancePlan(tenant_id=tenant.id, catalog_id=UNIMED))
        await session.commit()
    app.dependency_overrides[get_session] = _session
    try:
        response = await client.get(
            "/internal/brain-message/insurance-overview",
            params={"tenant_id": str(tenant.id)},
            headers={"X-Internal-Api-Key": os.environ["INTERNAL_API_KEY"]},
        )
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert response.status_code == 200, response.text
    # U1b: Unimed carries no clinic note, so the sheet lists no plan - only the sinal rule.
    assert response.json() == {"deposit": {"enabled": True, "percent": 30}, "plans": []}


def test_the_portal_rows_carry_no_description():
    rows = flow_router.portal_insurance_rows(
        [{"id": "p1", "name": "Exemplo", "mechanism": "reembolso"}, {"id": "p2", "name": "X"}]
    )
    assert rows == [("ins|Exemplo", "Exemplo", None), ("ins|X", "X", None)]


def test_the_whatsapp_and_portal_rows_are_title_only():
    tenant = SimpleNamespace(
        insurance_plans=[
            {"id": "p1", "name": "Exemplo", "mechanism": "variavel_por_plano"},
            {"id": "p2", "name": "Outro Plano"},
        ],
        insurances=None,
    )
    result = flow_router._enter_insurance(tenant)
    bubble = result.bubbles[0]
    assert bubble.rows[:2] == [("ins|Exemplo", "Exemplo"), ("ins|Outro Plano", "Outro Plano")]
    assert all(len(row) == 2 for row in bubble.rows)
    portal_items = bubble.source.items
    assert all(row[2] is None for row in portal_items)
    shown = repr(bubble.rows) + repr(portal_items)
    for text in REMOVED_TEXTS:
        assert text not in shown


def test_the_payment_copy_module_is_gone():
    assert importlib.util.find_spec("secretaria.services.insurance_payment_copy") is None


# --------------------------------------------------------------------------
# The hub: an older Portal's flag is ignored and a stored legacy value is never rewritten
# (U1b removed the one-release compat; the "no flag in any read" contract and the gone toggle
# route are pinned in test_sinal_trocas_U1b_option_gone.py)
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def hub(db):
    from fastapi import Depends
    from sqlalchemy.ext.asyncio import AsyncSession

    from secretaria.main import app

    async with db() as session:
        tenant, _patient = await _clinic(session, mode="shared")
        tenant_id = tenant.id

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    yield tenant_id
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def test_hub_ignores_an_old_flag_and_never_rewrites_a_legacy_value(
    client: AsyncClient, hub, db
):
    async with db() as session:
        # A row saved while the "não cobra sinal" option existed.
        amil = TenantInsurancePlan(tenant_id=hub, catalog_id=AMIL)
        session.add(amil)
        await session.flush()
        await set_legacy_flag(session, "tenant_insurance_plans", amil.id, False)
        await session.commit()
    saved = await client.put(
        "/tenants/me/insurance-plans",
        json={
            "plans": [
                {"catalog_id": str(UNIMED), "charge_deposit": False},
                {"catalog_id": str(AMIL), "charge_deposit": False},
            ]
        },
    )
    assert saved.status_code == 200, saved.text
    custom = await client.post(
        "/tenants/me/insurance-plans/custom",
        json={"custom_name": "GEAP", "charge_deposit": False},
    )
    assert custom.status_code == 201, custom.text
    # TASK-090 Adendo 1 (C6): no rule sent, none stored.
    assert custom.json()["custom_payment_note"] is None
    listed = await client.get("/tenants/me/insurance-plans")
    assert all("charge_deposit" not in plan for plan in listed.json())
    async with db() as session:
        stored = await legacy_flags(session, "tenant_insurance_plans")
    # The old screen's "no" was never written; the new rows got the column's default; the
    # legacy False is kept but never read.
    assert stored == [False, True, True]
