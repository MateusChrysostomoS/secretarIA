"""TASK-006 part 1 - independent behaviour tests for the convênio catalog's DB layer.

Covers the per-doctor subset rule (service + hub HTTP), the legacy free-text
sync, `resolve_tenant_plan_id`, and the Pix-deposit guard for plans the clinic
flagged "não cobra sinal".

Real in-memory SQLite (StaticPool + create_all), same pattern as
tests/test_deposit_lifecycle.py / tests/test_hub_professionals.py. SQLite does
NOT enforce foreign keys, so every subset assertion here exercises the
APP-LEVEL validation, never the Postgres composite FK.
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
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant  # noqa: E402
from secretaria.models.insurance import (  # noqa: E402
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.models.pix_deposit import PixDeposit  # noqa: E402
from secretaria.models.professional import Professional  # noqa: E402
from secretaria.services import tenant_config  # noqa: E402
from secretaria.services.hub_configuration import apply_tenant_config  # noqa: E402
from secretaria.services.insurance_catalog import (  # noqa: E402
    NotClinicPlans,
    catalog_id_for_slug,
    ensure_catalog_seeded,
    load_tenant_insurance,
    professional_plan_ids,
    resolve_tenant_plan_id,
    set_professional_plans,
    set_tenant_plans,
    sync_legacy_insurances,
)
from secretaria.services.payments import deposit_lifecycle  # noqa: E402

UNIMED = catalog_id_for_slug("unimed")
AMIL = catalog_id_for_slug("amil")
BRADESCO = catalog_id_for_slug("bradesco-saude")


# --------------------------------------------------------------------------
# Fixtures + seed helpers
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
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    yield maker
    await engine.dispose()


async def _tenant(session: AsyncSession, **fields) -> Tenant:
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12], **fields)
    session.add(tenant)
    await session.flush()
    return tenant


async def _professional(
    session: AsyncSession, tenant: Tenant, name: str = "Dra. Ana", *, is_active: bool = True
) -> Professional:
    professional = Professional(
        tenant_id=tenant.id, name=name, google_calendar_id=None, is_active=is_active
    )
    session.add(professional)
    await session.flush()
    return professional


async def _count(session: AsyncSession, model, *where) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(*where))


# --------------------------------------------------------------------------
# 6. Subset rule: a doctor only accepts plans the clinic enabled
# --------------------------------------------------------------------------


async def test_professional_plan_outside_clinic_raises_and_writes_nothing(db):
    async with db() as session:
        tenant = await _tenant(session)
        ana = await _professional(session, tenant)
        await set_tenant_plans(session, tenant, [(UNIMED, True)])
        await set_professional_plans(session, ana, [str(UNIMED)])
        await session.commit()
        ana_id = ana.id

        with pytest.raises(NotClinicPlans) as excinfo:
            # Unimed IS enabled, Amil is NOT: the whole request is refused.
            await set_professional_plans(session, ana, [str(UNIMED), str(AMIL)])
        assert excinfo.value.catalog_ids == [str(AMIL)]
        await session.rollback()

    async with db() as session:
        # Nothing written: the doctor still has exactly what they had.
        assert await professional_plan_ids(session, ana_id) == [str(UNIMED)]
        assert await _count(session, ProfessionalInsurancePlan) == 1


async def test_professional_plan_outside_clinic_with_no_prior_rows(db):
    async with db() as session:
        tenant = await _tenant(session)
        ana = await _professional(session, tenant)
        await set_tenant_plans(session, tenant, [(UNIMED, True)])
        with pytest.raises(NotClinicPlans):
            await set_professional_plans(session, ana, [str(AMIL)])
        assert await _count(session, ProfessionalInsurancePlan) == 0


async def test_plan_of_another_clinic_is_outside_this_clinic(db):
    """Clinic B enabling Amil does not let clinic A's doctor accept Amil."""
    async with db() as session:
        clinic_a = await _tenant(session)
        clinic_b = await _tenant(session)
        ana = await _professional(session, clinic_a)
        await set_tenant_plans(session, clinic_a, [(UNIMED, True)])
        await set_tenant_plans(session, clinic_b, [(AMIL, True)])
        with pytest.raises(NotClinicPlans):
            await set_professional_plans(session, ana, [str(AMIL)])


async def test_removing_plan_at_clinic_level_removes_it_from_doctors(db):
    async with db() as session:
        tenant = await _tenant(session)
        ana = await _professional(session, tenant, "Dra. Ana")
        bruno = await _professional(session, tenant, "Dr. Bruno")
        await set_tenant_plans(session, tenant, [(UNIMED, True), (AMIL, True)])
        await set_professional_plans(session, ana, [str(UNIMED), str(AMIL)])
        await set_professional_plans(session, bruno, [str(UNIMED)])
        await session.commit()

        await set_tenant_plans(session, tenant, [(AMIL, True)])
        await session.commit()

    async with db() as session:
        assert await professional_plan_ids(session, ana.id) == [str(AMIL)]
        assert await professional_plan_ids(session, bruno.id) == []
        insurance = await load_tenant_insurance(session, tenant.id)
        assert [plan["name"] for plan in insurance.plans] == ["Amil"]
        assert insurance.accepted_by.get(str(ana.id)) == frozenset({str(AMIL)})
        assert str(bruno.id) not in insurance.accepted_by


async def test_plan_added_at_clinic_level_is_not_granted_to_doctors(db):
    async with db() as session:
        tenant = await _tenant(session)
        ana = await _professional(session, tenant)
        await set_tenant_plans(session, tenant, [(UNIMED, True)])
        assert await professional_plan_ids(session, ana.id) == []


# ---- the same rule over HTTP ---------------------------------------------


@pytest_asyncio.fixture
async def hub(db):
    """A seeded clinic with Unimed enabled and one doctor; app deps overridden."""
    from secretaria.main import app

    async with db() as session:
        tenant = await _tenant(session)
        ana = await _professional(session, tenant)
        await set_tenant_plans(session, tenant, [(UNIMED, True)])
        await session.commit()

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant():
        return tenant

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield tenant, ana
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def test_http_put_doctor_plan_outside_clinic_is_422(client: AsyncClient, hub, db):
    _tenant_row, ana = hub
    response = await client.put(
        f"/tenants/me/professionals/{ana.id}/insurance-plans",
        json={"catalog_ids": [str(UNIMED), str(AMIL)]},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "plans_not_enabled_by_clinic"
    assert detail["catalog_ids"] == [str(AMIL)]
    async with db() as session:
        assert await _count(session, ProfessionalInsurancePlan) == 0


async def test_http_put_doctor_plan_inside_clinic_is_saved(client: AsyncClient, hub, db):
    _tenant_row, ana = hub
    response = await client.put(
        f"/tenants/me/professionals/{ana.id}/insurance-plans",
        json={"catalog_ids": [str(UNIMED)]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted_catalog_ids"] == [str(UNIMED)]
    got = await client.get(f"/tenants/me/professionals/{ana.id}/insurance-plans")
    assert got.status_code == 200
    assert got.json()["accepted_catalog_ids"] == [str(UNIMED)]
    assert [plan["catalog_id"] for plan in got.json()["selectable"]] == [str(UNIMED)]


async def test_http_clinic_put_removing_plan_cascades_to_doctor(client: AsyncClient, hub, db):
    _tenant_row, ana = hub
    saved = await client.put(
        f"/tenants/me/professionals/{ana.id}/insurance-plans",
        json={"catalog_ids": [str(UNIMED)]},
    )
    assert saved.status_code == 200, saved.text
    response = await client.put(
        "/tenants/me/insurance-plans",
        json={"plans": [{"catalog_id": str(AMIL), "charge_deposit": False}]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == [{"catalog_id": str(AMIL), "name": "Amil", "charge_deposit": False}]
    got = await client.get(f"/tenants/me/professionals/{ana.id}/insurance-plans")
    assert got.json()["accepted_catalog_ids"] == []


# --------------------------------------------------------------------------
# 7. Legacy free-text sync
# --------------------------------------------------------------------------


async def test_legacy_sync_matches_catalog_and_keeps_unknown_verbatim(db):
    async with db() as session:
        tenant = await _tenant(session)
        ana = await _professional(session, tenant, "Dra. Ana")
        bruno = await _professional(session, tenant, "Dr. Bruno")
        retired = await _professional(session, tenant, "Dr. Inativo", is_active=False)

        await sync_legacy_insurances(session, tenant, ["unimed ", "Plano Inexistente"])
        await session.commit()

    async with db() as session:
        plans = (
            await session.scalars(
                select(TenantInsurancePlan).where(TenantInsurancePlan.tenant_id == tenant.id)
            )
        ).all()
        # Only Unimed became a plan row; nothing for "Plano Inexistente".
        assert [plan.catalog_id for plan in plans] == [UNIMED]
        assert plans[0].charge_deposit is True
        # Granted to every ACTIVE doctor, not to the inactive one.
        assert await professional_plan_ids(session, ana.id) == [str(UNIMED)]
        assert await professional_plan_ids(session, bruno.id) == [str(UNIMED)]
        assert await professional_plan_ids(session, retired.id) == []
        stored = await session.get(Tenant, tenant.id)
        assert "Plano Inexistente" in stored.insurances
        assert len(stored.insurances) == 2


async def test_legacy_sync_keeps_existing_plan_settings(db):
    """A plan already enabled keeps charge_deposit and its (explicit) doctors."""
    async with db() as session:
        tenant = await _tenant(session)
        ana = await _professional(session, tenant, "Dra. Ana")
        bruno = await _professional(session, tenant, "Dr. Bruno")
        await set_tenant_plans(session, tenant, [(UNIMED, False)])
        await set_professional_plans(session, ana, [str(UNIMED)])
        await sync_legacy_insurances(session, tenant, ["Unimed"])
        charge = await session.scalar(
            select(TenantInsurancePlan.charge_deposit).where(
                TenantInsurancePlan.tenant_id == tenant.id
            )
        )
        assert charge is False
        assert await professional_plan_ids(session, ana.id) == [str(UNIMED)]
        # Not newly added -> not force-granted to Bruno.
        assert await professional_plan_ids(session, bruno.id) == []


async def test_hub_config_put_of_legacy_insurances_reaches_the_catalog(db):
    async with db() as session:
        tenant = await _tenant(session)
        ana = await _professional(session, tenant)
        await apply_tenant_config(session, tenant, {"insurances": ["Bradesco Saude", "Xyz"]})
        await session.commit()
    async with db() as session:
        insurance = await load_tenant_insurance(session, tenant.id)
        # Catalog plan first (with its id); the legacy string that matches no
        # catalog entry is still offered, id-less, until part 2 retires it.
        assert insurance.plans == [
            {"id": str(BRADESCO), "name": "Bradesco Saúde"},
            {"id": None, "name": "Xyz"},
        ]
        assert insurance.accepted_by[str(ana.id)] == frozenset({str(BRADESCO)})


async def test_legacy_only_plan_is_offered_but_a_removed_catalog_plan_does_not_return(db):
    """Unmatched legacy strings stay on offer (never silently dropped), but a
    catalog plan the clinic REMOVED through the new endpoint must not come
    back through the legacy mirror as an id-less option."""
    async with db() as session:
        tenant = await _tenant(session, insurances=["unimed ", "GEAP"])
        await sync_legacy_insurances(session, tenant, tenant.insurances)
        await session.commit()
    async with db() as session:
        before = await load_tenant_insurance(session, tenant.id)
        assert before.plans == [
            {"id": str(UNIMED), "name": "Unimed"},
            {"id": None, "name": "GEAP"},
        ]
        tenant = await session.get(Tenant, tenant.id)
        await set_tenant_plans(session, tenant, [])
        await session.commit()
        assert tenant.insurances == ["GEAP"]
    async with db() as session:
        after = await load_tenant_insurance(session, tenant.id)
        assert after.plans == [{"id": None, "name": "GEAP"}]


# --------------------------------------------------------------------------
# 8. Deposit guard
# --------------------------------------------------------------------------


class _RecordingAsaas:
    def __init__(self):
        self.calls: list = []

    def __getattr__(self, name):
        async def _record(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"Asaas must not be called ({name})")

        return _record


async def _seed_booking(
    session: AsyncSession,
    *,
    charge_deposit: bool | None,
    plan_id=UNIMED,
    with_plan_id: bool = True,
    asaas_api_key: str | None = None,
):
    """Clinic with Pix on; optional plan row; one appointment under `plan_id`."""
    tenant = await _tenant(
        session,
        is_active=True,
        pix_deposit_enabled=True,
        pix_deposit_percent=30,
        appointment_types=[
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 200,00"}
        ],
    )
    if charge_deposit is not None:
        await set_tenant_plans(session, tenant, [(plan_id, charge_deposit)])
    patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
    session.add(patient)
    await session.flush()
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=patient.id,
        google_event_id="evt-1",
        appointment_type="Consulta",
        phone=patient.wa_id,
        status=AppointmentStatus.SCHEDULED,
        insurance="Unimed" if with_plan_id else "Particular",
        insurance_plan_id=plan_id if with_plan_id else None,
    )
    session.add(appointment)
    if asaas_api_key:
        await tenant_config.set_asaas_api_key(session, tenant.id, asaas_api_key)
    await session.flush()
    return tenant, patient, appointment


@pytest.fixture
def skips(monkeypatch):
    reasons: list[str] = []
    original = deposit_lifecycle._log_deposit_skip

    def _record(tenant, appointment, reason):
        reasons.append(reason)
        original(tenant, appointment, reason)

    monkeypatch.setattr(deposit_lifecycle, "_log_deposit_skip", _record)
    return reasons


@pytest.fixture
def asaas(monkeypatch):
    client = _RecordingAsaas()
    monkeypatch.setattr(deposit_lifecycle, "_asaas_client_for", lambda api_key: client)
    return client


async def test_deposit_plan_no_charge_skips_without_asaas(db, skips, asaas):
    async with db() as session:
        tenant, patient, appointment = await _seed_booking(
            session, charge_deposit=False, asaas_api_key="fake-key"
        )
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
    assert deposit is None
    assert skips == [deposit_lifecycle.DEPOSIT_SKIP_INSURANCE_NO_CHARGE]
    assert asaas.calls == []
    async with db() as session:
        assert await _count(session, PixDeposit) == 0


async def test_deposit_plan_that_charges_passes_the_insurance_guard(db, skips):
    async with db() as session:
        tenant, patient, appointment = await _seed_booking(session, charge_deposit=True)
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
    assert deposit is None
    # Went past the new guard and stopped at the NEXT one (no Asaas key).
    assert skips == [deposit_lifecycle.DEPOSIT_SKIP_NO_API_KEY]


async def test_deposit_without_plan_id_is_tenant_policy_only(db, skips):
    """ "Particular"/"Outro": no plan id -> the guard never interferes."""
    async with db() as session:
        tenant, patient, appointment = await _seed_booking(
            session, charge_deposit=False, with_plan_id=False
        )
        await session.commit()
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
    assert deposit is None
    assert skips == [deposit_lifecycle.DEPOSIT_SKIP_NO_API_KEY]


async def test_deposit_plan_no_longer_enabled_is_tenant_policy_only(db, skips):
    """A plan id the clinic no longer accepts -> tenant policy, not a silent skip."""
    async with db() as session:
        tenant, patient, appointment = await _seed_booking(session, charge_deposit=None)
        await session.commit()
        await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
    assert skips == [deposit_lifecycle.DEPOSIT_SKIP_NO_API_KEY]


async def test_deposit_other_clinics_no_charge_flag_does_not_leak(db, skips):
    """Clinic B marking Unimed "no deposit" never affects clinic A's Unimed booking."""
    async with db() as session:
        tenant, patient, appointment = await _seed_booking(session, charge_deposit=True)
        other = await _tenant(session)
        await set_tenant_plans(session, other, [(UNIMED, False)])
        await session.commit()
        await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
    assert deposit_lifecycle.DEPOSIT_SKIP_INSURANCE_NO_CHARGE not in skips


async def test_deposit_disabled_tenant_still_wins_first(db, skips):
    async with db() as session:
        tenant, patient, appointment = await _seed_booking(session, charge_deposit=False)
        tenant.pix_deposit_enabled = False
        await session.commit()
        await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
    assert skips == [deposit_lifecycle.DEPOSIT_SKIP_DISABLED]


# --------------------------------------------------------------------------
# 9. resolve_tenant_plan_id
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Bradesco Saúde", "bradesco saude", "  BRADESCO   SAÚDE  ", "Bradesco\tSaude"],
)
async def test_resolve_plan_name_variants(db, text):
    async with db() as session:
        tenant = await _tenant(session)
        await set_tenant_plans(session, tenant, [(BRADESCO, True), (UNIMED, True)])
        assert await resolve_tenant_plan_id(session, tenant.id, text) == BRADESCO


@pytest.mark.parametrize(
    "text",
    ["Particular", "Outro convênio", "Plano Inexistente", "Unimed BH", "Amil", "", "   ", None],
)
async def test_resolve_non_clinic_plan_is_none(db, text):
    async with db() as session:
        tenant = await _tenant(session)
        other = await _tenant(session)
        await set_tenant_plans(session, tenant, [(BRADESCO, True), (UNIMED, True)])
        # Amil is enabled by ANOTHER clinic only.
        await set_tenant_plans(session, other, [(AMIL, True)])
        assert await resolve_tenant_plan_id(session, tenant.id, text) is None


# --------------------------------------------------------------------------
# DB -> worker snapshot -> router, end to end
# --------------------------------------------------------------------------


def _flow_tenant(tenant: Tenant, professionals, insurance):
    from secretaria.workers.tasks import _flow_tenant_snapshot

    return _flow_tenant_snapshot(tenant, professionals, None, insurance)


async def _book_until_after_attendee(snapshot, professionals):
    from types import SimpleNamespace

    from secretaria.models import FlowState
    from secretaria.services.attendee import LABEL_ATTENDEE_SELF
    from secretaria.services.flow_router import LABEL_BOOK, route

    def _conv(result=None):
        fields = dict(
            id=uuid4(),
            flow_state=FlowState.IDLE,
            flow_step=None,
            flow_selected_type=None,
            flow_selected_day=None,
            flow_selected_slot=None,
            flow_selected_professional_id=None,
            flow_selected_insurance=None,
            flow_managing_appointment_id=None,
            flow_attendee_name=None,
            patient_id=None,
        )
        if result is not None:
            for key in list(fields):
                if hasattr(result, key) and key != "id":
                    fields[key] = getattr(result, key)
        return SimpleNamespace(**fields)

    asked = await route(_conv(), snapshot, None, LABEL_BOOK, professionals=professionals)
    after = await route(
        _conv(asked), snapshot, None, LABEL_ATTENDEE_SELF, professionals=professionals
    )
    return after, _conv


_FLOWS_ON = {"enabled": True, "buttons": ["Serviços e Custo", "Horários", "Outro"]}
_SERVICES = [{"name": "Consulta", "duration_min": 30, "is_active": True}]


async def test_e2e_db_acceptance_marks_doctor_list(db):
    from secretaria.schemas.webhook import inbound_routing_text
    from secretaria.services.flow_router import (
        INSURANCE_ACCEPTED_MARK,
        STEP_AWAITING_INSURANCE,
        STEP_AWAITING_PROFESSIONAL,
        route,
    )

    async with db() as session:
        tenant = await _tenant(
            session,
            collect_insurance=True,
            initial_flows=_FLOWS_ON,
            appointment_types=_SERVICES,
            business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        )
        ana = await _professional(session, tenant, "Dra. Ana")
        bruno = await _professional(session, tenant, "Dr. Bruno")
        await set_tenant_plans(session, tenant, [(UNIMED, True), (AMIL, True)])
        await set_professional_plans(session, ana, [str(UNIMED)])
        await set_professional_plans(session, bruno, [str(AMIL)])
        await session.commit()
        insurance = await load_tenant_insurance(session, tenant.id)

    professionals = [ana, bruno]
    snapshot = _flow_tenant(tenant, professionals, insurance)
    after, conv = await _book_until_after_attendee(snapshot, professionals)
    assert after.flow_step == STEP_AWAITING_INSURANCE
    unimed_row = next(row for row in after.bubbles[0].rows if row[1] == "Unimed")
    doctors = await route(
        conv(after),
        snapshot,
        None,
        inbound_routing_text(unimed_row[1], unimed_row[0]),
        professionals=professionals,
    )
    assert doctors.flow_step == STEP_AWAITING_PROFESSIONAL
    by_id = {row[0]: row[2] for row in doctors.bubbles[0].rows}
    assert str(by_id[f"prof|{ana.id}"]).startswith(INSURANCE_ACCEPTED_MARK)
    assert f"prof|{bruno.id}" in by_id  # listed...
    assert not str(by_id[f"prof|{bruno.id}"] or "").startswith(INSURANCE_ACCEPTED_MARK)


async def test_e2e_clinic_with_only_non_catalog_legacy_plans_still_asks_insurance(db):
    """collect_insurance on + legacy plans that match NO catalog entry.

    Before the catalog, this clinic's patients were asked the convênio with
    its own plans listed. After the migration/sync those strings stay only in
    `tenants.insurances` (docs/CHECKPOINT_convenio_catalogo.md §6), the worker
    snapshot now always carries `insurance_plans=[]`, and the router treats
    that empty list as "no plans" -> the step is silently skipped even though
    the clinic's toggle is ON. Expected: the question is still asked.
    """
    from secretaria.services.flow_router import STEP_AWAITING_INSURANCE

    async with db() as session:
        tenant = await _tenant(
            session,
            collect_insurance=True,
            initial_flows=_FLOWS_ON,
            appointment_types=_SERVICES,
            business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        )
        solo = await _professional(session, tenant, "Dra. Única")
        await sync_legacy_insurances(session, tenant, ["GEAP", "Cabesp"])
        await session.commit()
        assert tenant.insurances == ["GEAP", "Cabesp"]
        insurance = await load_tenant_insurance(session, tenant.id)

    snapshot = _flow_tenant(tenant, [solo], insurance)
    after, _conv = await _book_until_after_attendee(snapshot, [solo])
    assert after.flow_step == STEP_AWAITING_INSURANCE, (
        f"convênio step skipped: got {after.flow_step!r} although collect_insurance=True "
        f"and tenants.insurances={tenant.insurances!r}"
    )
