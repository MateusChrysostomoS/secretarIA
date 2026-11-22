"""TASK-008 - `Tenant.insurance_mode` matrix, extensible catalog, convênio "Outro".

Real in-memory SQLite (StaticPool + create_all), same pattern as
tests/test_convenio_catalogo_db.py. Covers what that file's TASK-006 tests
could not, because the mode column did not exist yet:

1. `insurance_mode` x catalog (empty/filled) x professional count/acceptance -
   `_insurance_step_skip_reason` and `_accepts_plan`'s marking rule, end to end
   through the real flow (DB -> worker snapshot -> `route()`), covering the
   representative combinations of each dimension (not the full 4x2x4x2
   cross-product, which test_convenio_catalogo_flow.py's 52 pure-router tests
   already exercise finely for the marking mechanics alone).
2. `insurance_mode is None` -> the convênio step is skipped, full stop.
3. Reconciliation: a legacy string with no catalog match, resolved the moment
   an admin adds a matching catalog entry.
4. Convênio "Outro" (clinic + professional, independent mode): creation,
   appearance in the patient's list, payment-note copy in the booking
   confirmation, and the Pix-deposit guard for a custom line.
5. `enter_guided_booking` respects the very same skip/resolution as the button
   flow (already exercised throughout test_convenio_catalogo_flow.py's §5 via
   the shared `_tenant()` fixture there; one more explicit, mode-focused
   assertion here).
"""

import os
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.admin.panel import require_admin  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant  # noqa: E402
from secretaria.models.insurance import (  # noqa: E402
    InsuranceCatalogUnmatched,
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.models.pix_deposit import PixDeposit  # noqa: E402
from secretaria.models.professional import Professional  # noqa: E402
from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402
from secretaria.services.attendee import LABEL_ATTENDEE_SELF  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    INSURANCE_SKIP_EMPTY_CATALOG,
    INSURANCE_SKIP_NO_MODE,
    LABEL_BOOK,
    STEP_AWAITING_ATTENDEE_CHOICE,
    STEP_AWAITING_DAY,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_SERVICE,
    _insurance_step_skip_reason,
    enter_guided_booking,
    route,
)
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    create_catalog_entry,
    create_professional_custom_plan,
    create_tenant_custom_plan,
    ensure_catalog_seeded,
    list_professional_plans,
    list_tenant_plans,
    load_tenant_insurance,
    reconcile_unmatched_insurances,
    record_unmatched,
    resolve_booking_plan_ids,
    set_professional_clinic_plans,
    set_tenant_plans,
)
from secretaria.services.payments import deposit_lifecycle  # noqa: E402
from tests._legacy_insurance_flag import set_legacy_flag  # noqa: E402

UNIMED = catalog_id_for_slug("unimed")
AMIL = catalog_id_for_slug("amil")

_FLOWS_ON = {"enabled": True, "buttons": ["Serviços e Custo", "Horários", "Outro"]}
_SERVICES = [{"name": "Consulta", "duration_min": 30, "is_active": True}]
_HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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


async def _professional(session: AsyncSession, tenant: Tenant, name: str) -> Professional:
    professional = Professional(tenant_id=tenant.id, name=name, is_active=True)
    session.add(professional)
    await session.flush()
    return professional


async def _plan_id(session: AsyncSession, tenant_id, catalog_id) -> str:
    for plan, entry in await list_tenant_plans(session, tenant_id):
        if entry is not None and entry.id == catalog_id:
            return str(plan.id)
    raise AssertionError("plan not found")


def _flow_snapshot(tenant: Tenant, professionals, insurance):
    from secretaria.workers.tasks import _flow_tenant_snapshot

    return _flow_tenant_snapshot(tenant, professionals, None, insurance)


async def _book_to_insurance_or_professional(snapshot, professionals):
    """MENU entry -> pra-quem "Sim" -> whatever comes next (skip or convênio)."""
    from types import SimpleNamespace

    from secretaria.models import FlowState

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
    assert asked.flow_step == STEP_AWAITING_ATTENDEE_CHOICE
    after = await route(
        _conv(asked), snapshot, None, LABEL_ATTENDEE_SELF, professionals=professionals
    )
    return after, _conv


async def _book_to_the_convenio(snapshot, professionals, doctor=None, service="Consulta"):
    """TASK-088 order: pra-quem "Sim" -> the SERVICE -> [the doctor] (no card in between).

    Returns (result, conv): the result after the service / doctor tap - the convênio list when
    the doctor accepts any plan, else (Particular direct) the day step - and the conversation
    builder."""
    from secretaria.services.flow_router import STEP_AWAITING_SERVICE_PROFESSIONAL

    after, conv = await _book_to_insurance_or_professional(snapshot, professionals)
    row = next(r for r in after.bubbles[0].rows if r[0] == f"svc|{service}")
    result = await route(
        conv(after), snapshot, None, inbound_routing_text(row[1], row[0]),
        professionals=professionals,
    )
    if result.flow_step == STEP_AWAITING_SERVICE_PROFESSIONAL:
        row = next(r for r in result.bubbles[0].rows if r[0] == f"prof|{doctor.id}")
        result = await route(
            conv(result), snapshot, None, inbound_routing_text(row[1], row[0]),
            professionals=professionals,
        )
    return result, conv


def _plan_titles(result) -> list[str]:
    """The plans a convênio list offers (the two fixed rows left out)."""
    assert result.flow_step == STEP_AWAITING_INSURANCE, result.flow_step
    rows = [row for row in result.bubbles[0].rows if not row[0].startswith("dayback|")]
    return [row[1] for row in rows[:-2]]


# --------------------------------------------------------------------------
# 1. `insurance_mode` x catalog x professionals - representative matrix
# --------------------------------------------------------------------------


async def test_mode_none_skips_regardless_of_catalog(db):
    """`insurance_mode is None`: skipped even with `collect_insurance=True` and
    a non-empty clinic catalog - the owner's decision overrides everything
    else (SPEC §2)."""
    async with db() as session:
        tenant = await _tenant(
            session,
            insurance_mode=None,
            collect_insurance=True,
            initial_flows=_FLOWS_ON,
            appointment_types=_SERVICES,
            business_hours=_HOURS,
        )
        solo = await _professional(session, tenant, "Dra. Única")
        await set_tenant_plans(session, tenant, [UNIMED])
        await session.commit()
        insurance = await load_tenant_insurance(session, tenant.id)
        assert _insurance_step_skip_reason(tenant) == INSURANCE_SKIP_NO_MODE

    snapshot = _flow_snapshot(tenant, [solo], insurance)
    after, _conv = await _book_to_insurance_or_professional(snapshot, [solo])
    # No convênio question: a single doctor's own service list comes next.
    assert after.flow_step == STEP_AWAITING_SERVICE


async def test_mode_shared_lists_the_plan_for_every_active_professional_no_rows_needed(db):
    """`shared`: nobody has (or needs) a `professional_insurance_plans` row -
    the mode alone makes every active doctor accept the clinic's plans."""
    async with db() as session:
        tenant = await _tenant(
            session,
            insurance_mode="shared",
            collect_insurance=True,
            initial_flows=_FLOWS_ON,
            appointment_types=_SERVICES,
            business_hours=_HOURS,
        )
        ana = await _professional(session, tenant, "Dra. Ana")
        bruno = await _professional(session, tenant, "Dr. Bruno")
        await set_tenant_plans(session, tenant, [UNIMED])
        await session.commit()
        assert await _count(session, ProfessionalInsurancePlan) == 0
        insurance = await load_tenant_insurance(session, tenant.id)

    professionals = [ana, bruno]
    snapshot = _flow_snapshot(tenant, professionals, insurance)
    for professional in professionals:
        asked, _conv = await _book_to_the_convenio(snapshot, professionals, doctor=professional)
        assert _plan_titles(asked) == ["Unimed"]


async def test_mode_clinic_with_exceptions_inherit_all_default(db):
    """`clinic_with_exceptions`: never-customized inherits ALL clinic plans;
    an explicit empty customization accepts NONE - the two are distinguishable
    (`Professional.insurance_plans_customized`)."""
    async with db() as session:
        tenant = await _tenant(
            session,
            insurance_mode="clinic_with_exceptions",
            collect_insurance=True,
            initial_flows=_FLOWS_ON,
            appointment_types=_SERVICES,
            business_hours=_HOURS,
        )
        never_customized = await _professional(session, tenant, "Dr. Nunca Mexeu")
        explicitly_none = await _professional(session, tenant, "Dra. Recusou Tudo")
        await set_tenant_plans(session, tenant, [UNIMED])
        unimed_plan_id = await _plan_id(session, tenant.id, UNIMED)
        await set_professional_clinic_plans(session, explicitly_none, [])
        await session.commit()
        insurance = await load_tenant_insurance(session, tenant.id)
        assert insurance.accepted_by[str(explicitly_none.id)] == frozenset()
        assert str(never_customized.id) not in insurance.accepted_by

    professionals = [never_customized, explicitly_none]
    snapshot = _flow_snapshot(tenant, professionals, insurance)
    inherits, _conv = await _book_to_the_convenio(snapshot, professionals, doctor=never_customized)
    assert _plan_titles(inherits) == ["Unimed"]
    # TASK-088: the doctor who accepts NONE is not asked at all - Particular direct.
    refuses, _conv = await _book_to_the_convenio(snapshot, professionals, doctor=explicitly_none)
    assert refuses.flow_step == STEP_AWAITING_DAY
    assert refuses.flow_selected_insurance == "Particular"
    assert unimed_plan_id  # sanity: the plan row really exists


async def test_mode_independent_union_of_doctors_no_clinic_inheritance(db):
    """`independent`: the offered list is the UNION of doctors' own picks;
    the CLINIC's own `tenant_insurance_plans` (even if populated) is ignored."""
    async with db() as session:
        tenant = await _tenant(
            session,
            insurance_mode="independent",
            collect_insurance=True,
            initial_flows=_FLOWS_ON,
            appointment_types=_SERVICES,
            business_hours=_HOURS,
        )
        ana = await _professional(session, tenant, "Dra. Ana")
        bruno = await _professional(session, tenant, "Dr. Bruno")
        # A clinic-level plan that NO mode-independent doctor ever reads.
        await set_tenant_plans(session, tenant, [UNIMED])
        session.add(
            ProfessionalInsurancePlan(professional_id=ana.id, tenant_id=tenant.id, catalog_id=AMIL)
        )
        await session.commit()
        insurance = await load_tenant_insurance(session, tenant.id)
        assert [plan["name"] for plan in insurance.plans] == ["Amil"]

    professionals = [ana, bruno]
    snapshot = _flow_snapshot(tenant, professionals, insurance)
    # Ana's own pick is the whole offer; Bruno picked nothing, so for him nothing is accepted
    # (the clinic's own list is ignored): Particular direct.
    asked, _conv = await _book_to_the_convenio(snapshot, professionals, doctor=ana)
    assert _plan_titles(asked) == ["Amil"]
    none, _conv = await _book_to_the_convenio(snapshot, professionals, doctor=bruno)
    assert none.flow_step == STEP_AWAITING_DAY
    assert none.flow_selected_insurance == "Particular"


async def test_empty_catalog_skips_even_with_mode_chosen(db):
    async with db() as session:
        tenant = await _tenant(
            session, insurance_mode="shared", collect_insurance=True,
        )
        assert _insurance_step_skip_reason(tenant) == INSURANCE_SKIP_EMPTY_CATALOG


async def _count(session: AsyncSession, model, *where) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(*where))


# --------------------------------------------------------------------------
# 2. enter_guided_booking - same resolution, dedicated mode-focused check
# --------------------------------------------------------------------------


async def test_enter_guided_booking_skips_when_mode_is_none(db):
    async with db() as session:
        tenant = await _tenant(
            session,
            insurance_mode=None,
            collect_insurance=True,
            appointment_types=_SERVICES,
            business_hours=_HOURS,
        )
        solo = await _professional(session, tenant, "Dra. Única")
        await set_tenant_plans(session, tenant, [UNIMED])
        await session.commit()
        insurance = await load_tenant_insurance(session, tenant.id)

    snapshot = _flow_snapshot(tenant, [solo], insurance)

    class _FakeCalendar:
        tzinfo = None

        async def list_available_days(self, start_day, days, slot_minutes=None):
            return [start_day]

    result = await enter_guided_booking(
        snapshot,
        _FakeCalendar(),
        "Consulta",
        conversation_id=uuid4(),
        professionals=[solo],
    )
    # Skipped: straight to the days.
    assert result.flow_step == STEP_AWAITING_DAY


# --------------------------------------------------------------------------
# 3. Reconciliation
# --------------------------------------------------------------------------


async def test_reconciliation_resolves_legacy_unmatched_when_catalog_grows(db):
    async with db() as session:
        tenant = await _tenant(session)
        await record_unmatched(session, tenant.id, "GEAP Saúde")
        await session.commit()

    async with db() as session:
        pending = list(
            await session.scalars(
                select(InsuranceCatalogUnmatched).where(
                    InsuranceCatalogUnmatched.resolved_at.is_(None)
                )
            )
        )
        assert len(pending) == 1

        entry = await create_catalog_entry(
            session, name="GEAP", aliases=["GEAP Saúde"], mechanism="desconhecido", note=None
        )
        resolved_count = await reconcile_unmatched_insurances(session, entry)
        await session.commit()
        assert resolved_count == 1

    async with db() as session:
        row = await session.get(InsuranceCatalogUnmatched, pending[0].id)
        assert row.resolved_at is not None
        assert row.resolved_insurance_catalog_id == entry.id
        plan = await session.scalar(
            select(TenantInsurancePlan).where(
                TenantInsurancePlan.tenant_id == tenant.id,
                TenantInsurancePlan.catalog_id == entry.id,
            )
        )
        assert plan is not None


async def test_reconciliation_idempotent_record_unmatched(db):
    async with db() as session:
        tenant = await _tenant(session)
        await record_unmatched(session, tenant.id, "geap")
        await record_unmatched(session, tenant.id, "  GEAP  ")  # same normalized text
        await session.commit()
        assert await _count(session, InsuranceCatalogUnmatched) == 1


async def test_admin_http_creates_catalog_entry_and_reconciles(client: AsyncClient, db):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[require_admin] = lambda: None
    try:
        async with db() as session:
            tenant = await _tenant(session)
            await record_unmatched(session, tenant.id, "Cabesp")
            await session.commit()

        response = await client.post(
            "/admin/insurance-catalog",
            json={"name": "Cabesp", "aliases": [], "mechanism": "desconhecido", "note": None},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["reconciled_count"] == 1

        async with db() as session:
            insurance = await load_tenant_insurance(session, tenant.id)
            assert insurance.plans and insurance.plans[0]["name"] == "Cabesp"
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(require_admin, None)


# --------------------------------------------------------------------------
# 4. Convênio "Outro"
# --------------------------------------------------------------------------


async def test_clinic_custom_plan_appears_in_patient_list_and_can_toggle_deposit(db):
    async with db() as session:
        tenant = await _tenant(
            session,
            insurance_mode="shared",
            collect_insurance=True,
            initial_flows=_FLOWS_ON,
            appointment_types=_SERVICES,
            business_hours=_HOURS,
        )
        solo = await _professional(session, tenant, "Dra. Única")
        plan = await create_tenant_custom_plan(
            session, tenant, custom_name="Convênio da Prefeitura"
        )
        await session.commit()
        plan_id = str(plan.id)
        insurance = await load_tenant_insurance(session, tenant.id)
        assert insurance.plans == [
            {
                "id": plan_id,
                "name": "Convênio da Prefeitura",
                "mechanism": "desconhecido",  # custom plans have no catalog entry
            }
        ]

    snapshot = _flow_snapshot(tenant, [solo], insurance)
    after, conv = await _book_to_the_convenio(snapshot, [solo])
    assert _plan_titles(after) == ["Convênio da Prefeitura"]
    row = next(row for row in after.bubbles[0].rows if row[1] == "Convênio da Prefeitura")
    answered = await route(
        conv(after), snapshot, None, inbound_routing_text(row[1], row[0]), professionals=[solo]
    )
    assert str(answered.flow_selected_insurance) == "Convênio da Prefeitura"


async def test_professional_custom_plan_independent_mode(db):
    async with db() as session:
        tenant = await _tenant(session, insurance_mode="independent")
        ana = await _professional(session, tenant, "Dra. Ana")
        plan = await create_professional_custom_plan(session, ana, custom_name="Plano da Dra. Ana")
        await session.commit()
        pairs = await list_professional_plans(session, ana.id)
        assert len(pairs) == 1
        assert pairs[0][0].id == plan.id
        assert pairs[0][1] is None  # no catalog entry - it's custom


async def test_the_deposit_ask_never_carries_a_convenio_payment_block():
    """TASK-090 (owner, 2026-11-22): the "Outro" convênio has no payment text any more, so the
    Pix ask never shows "Sobre o pagamento do convênio" (it used to repeat the note there)."""
    text = deposit_lifecycle._deposit_request_text(
        tenant=type("T", (), {"clinic_name": "Clinica"})(),
        appointment=type("A", (), {"appointment_type": "Consulta"})(),
        amount_cents=6000,
        copy_paste="00020126...",
    )
    assert "Sobre o pagamento do convênio" not in text
    assert "💳" not in text


async def test_custom_plan_never_exempts_the_deposit(db, monkeypatch):
    """TASK-085 R D-R12: a custom plan whose legacy flag says "não cobra sinal" is charged like
    any other booking - it goes past where it used to stop, up to the next guard (no account)."""
    reasons: list[str] = []
    monkeypatch.setattr(
        deposit_lifecycle, "_log_deposit_skip", lambda _t, _a, reason: reasons.append(reason)
    )
    async with db() as session:
        tenant = await _tenant(
            session,
            is_active=True,
            pix_deposit_enabled=True,
            insurance_mode="shared",
            appointment_types=[
                {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 200,00"}
            ],
        )
        plan = await create_tenant_custom_plan(session, tenant, custom_name="Outro")
        await session.flush()
        # A row saved while the option existed.
        await set_legacy_flag(session, "tenant_insurance_plans", plan.id, False)
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
            insurance="Outro",
            insurance_plan_id=plan.id,
        )
        session.add(appointment)
        await session.commit()

        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="tok"
        )
        assert deposit is None
        assert await _count(session, PixDeposit) == 0
    assert reasons == [deposit_lifecycle.DEPOSIT_SKIP_NO_API_KEY]


async def test_resolve_booking_plan_ids_independent_mode_uses_professional_row(db):
    async with db() as session:
        tenant = await _tenant(session, insurance_mode="independent")
        ana = await _professional(session, tenant, "Dra. Ana")
        bruno = await _professional(session, tenant, "Dr. Bruno")
        session.add(
            ProfessionalInsurancePlan(
                professional_id=ana.id, tenant_id=tenant.id, catalog_id=UNIMED
            )
        )
        await session.commit()

        tenant_plan_id, professional_plan_id = await resolve_booking_plan_ids(
            session, tenant.id, "Unimed", ana.id
        )
        assert tenant_plan_id is None
        assert professional_plan_id is not None

        # Bruno has no such row: unmatched for HIM even though Ana has it.
        none_tenant, none_prof = await resolve_booking_plan_ids(
            session, tenant.id, "Unimed", bruno.id
        )
        assert none_tenant is None
        assert none_prof is None
