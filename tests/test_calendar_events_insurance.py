"""Agenda read model: the convênio of each appointment (TASK-014, spec TASK E).

Ground truth: api/hub/calendar.py::list_events, schemas/calendar.py::CalendarEventRead,
services/insurance_catalog.py::load_appointment_plans. Contract for the consumer:
docs/CHECKPOINT_convenio_catalogo.md section 11.

Fixture shape mirrors tests/test_cancellation_notice.py (fake Calendar, in-memory
SQLite) so no Google or Redis call is ever attempted.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub import calendar as hub_calendar  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import (
    Base,  # noqa: E402
    get_session,  # noqa: E402
)
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Professional,
    Tenant,
)
from secretaria.models.insurance import (  # noqa: E402
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.models.pix_deposit import PixDeposit, PixDepositStatus  # noqa: E402
from secretaria.schemas.calendar import (  # noqa: E402
    CalendarDepositRead,
    CalendarEventRead,
    CalendarInsurancePlanRead,
)
from secretaria.services.insurance_catalog import (  # noqa: E402
    AppointmentPlan,
    AppointmentPlanLookup,
    catalog_id_for_slug,
    ensure_catalog_seeded,
    load_appointment_plans,
)
from secretaria.services.payments.deposit_lifecycle import (  # noqa: E402
    AppointmentDepositView,
    load_deposit_views,
)
from secretaria.services.tenant_config import set_google_refresh_token  # noqa: E402

CALENDAR = "/tenants/me/calendar"
UNIMED = catalog_id_for_slug("unimed")
AMIL = catalog_id_for_slug("amil")
LEGACY_EVENT_KEYS = {"id", "summary", "start", "end", "appointment_id"}

LEGACY_EVENT_KEYS = {"id", "summary", "start", "end", "appointment_id"}


# ---------------------------------------------------------------------------
# Schema — additive; plan never wider than {id, name, charge_deposit},
# deposit never wider than {status, amount_cents}
# ---------------------------------------------------------------------------


NEW_EVENT_KEYS = {"status", "confirmation_count", "display_state", "attention", "reminders"}


def test_a_legacy_event_gains_three_null_fields_and_loses_nothing():
    event = CalendarEventRead(id="g1", summary="Consulta", start="s", end="e")

    dumped = event.model_dump()

    assert set(dumped) == LEGACY_EVENT_KEYS | {"insurance", "insurance_plan", "deposit"} | NEW_EVENT_KEYS
    assert dumped["insurance"] is None
    assert dumped["insurance_plan"] is None
    assert dumped["deposit"] is None
    assert dumped["appointment_id"] is None
    assert dumped["status"] is None and dumped["reminders"] is None


def test_the_plan_wire_carries_exactly_three_keys():
    """custom_payment_note / mechanism / note must never be widened into this
    shape by accident: the agenda shows a name and a flag, nothing else."""
    assert set(CalendarInsurancePlanRead.model_fields) == {"id", "name", "charge_deposit"}


def test_the_deposit_wire_carries_exactly_two_keys():
    """The Pix copy-paste payload, the Asaas payment id and the patient stay
    server-side: the agenda shows the STATE of the deposit and its amount.

    This is the wire of TODAY. Spec F (section 10A.5) deliberately adds a third
    key, `needs_attention`, in TASK-017, which then updates THIS assertion on
    purpose; nothing else may widen it (never the provider charge id, the
    fulfillment_status column itself, the booking snapshot or the payer)."""
    assert set(CalendarDepositRead.model_fields) == {"status", "amount_cents"}


# ---------------------------------------------------------------------------
# Fixtures + builders
# ---------------------------------------------------------------------------


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
    maker.probe_engine = engine.sync_engine  # statement counter below
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    async with db() as session:
        t = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=None, language="pt-BR")
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest_asyncio.fixture
async def other_tenant(db) -> Tenant:
    async with db() as session:
        t = Tenant(id=uuid4(), clinic_name="Other clinic", phone_number_id=None)
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


class _Sql:
    """Collects every statement the engine executes inside the `with` block."""

    def __init__(self, engine) -> None:
        self._engine = engine
        self.statements: list[str] = []

    def _on(self, conn, cursor, statement, params, context, executemany) -> None:
        self.statements.append(statement)

    def __enter__(self) -> "_Sql":
        event.listen(self._engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self._engine, "before_cursor_execute", self._on)

    def count(self, needle: str) -> int:
        return sum(needle in s for s in self.statements)


async def _tenant_plan(
    db,
    tenant_id: UUID,
    *,
    catalog_id: UUID | None = None,
    custom_name: str | None = None,
    note: str | None = None,
    charge_deposit: bool = True,
) -> UUID:
    async with db() as session:
        plan = TenantInsurancePlan(
            id=uuid4(),
            tenant_id=tenant_id,
            catalog_id=catalog_id,
            custom_name=custom_name,
            custom_payment_note=note,
            charge_deposit=charge_deposit,
        )
        session.add(plan)
        await session.commit()
        return plan.id


async def _professional_plan(
    db,
    tenant_id: UUID,
    *,
    catalog_id: UUID | None = None,
    custom_name: str | None = None,
    note: str | None = None,
    tenant_plan_id: UUID | None = None,
    charge_deposit: bool = True,
) -> UUID:
    async with db() as session:
        professional = Professional(id=uuid4(), tenant_id=tenant_id, name="Dra. Ana")
        session.add(professional)
        await session.flush()
        plan = ProfessionalInsurancePlan(
            id=uuid4(),
            professional_id=professional.id,
            tenant_id=tenant_id,
            tenant_plan_id=tenant_plan_id,
            catalog_id=catalog_id,
            custom_name=custom_name,
            custom_payment_note=note,
            charge_deposit=charge_deposit,
        )
        session.add(plan)
        await session.commit()
        return plan.id


async def _deposit(
    db,
    tenant_id: UUID,
    *,
    status: PixDepositStatus = PixDepositStatus.AWAITING,
    amount_cents: int = 3000,
    appointment_id: UUID | None = None,
) -> UUID:
    """One pix_deposits row; creates a bare appointment when none is given.
    Returns the APPOINTMENT id (the key the agenda looks deposits up by)."""
    async with db() as session:
        if appointment_id is None:
            appointment_id = uuid4()
            session.add(
                Appointment(
                    id=appointment_id,
                    tenant_id=tenant_id,
                    google_event_id=f"evt-{appointment_id}",
                    appointment_type="Consulta",
                    start_at=datetime.now(UTC) + timedelta(days=1),
                    end_at=datetime.now(UTC) + timedelta(days=1, minutes=30),
                    status=AppointmentStatus.SCHEDULED,
                )
            )
            await session.flush()
        session.add(
            PixDeposit(
                id=uuid4(),
                tenant_id=tenant_id,
                appointment_id=appointment_id,
                asaas_payment_id=f"pay_{uuid4().hex}",
                amount_cents=amount_cents,
                percent_applied=30,
                pix_copy_paste="00020126-PIX-COPIA-E-COLA-NAO-VAZAR",
                status=status,
            )
        )
        await session.commit()
        return appointment_id


# ---------------------------------------------------------------------------
# Service — batch lookup, tenant-scoped, mirrors the deposit guard's precedence
# ---------------------------------------------------------------------------


async def test_a_catalog_backed_clinic_plan_resolves_to_its_catalog_name_and_flag(db, tenant):
    keeps = await _tenant_plan(db, tenant.id, catalog_id=UNIMED, charge_deposit=True)
    waives = await _tenant_plan(db, tenant.id, catalog_id=AMIL, charge_deposit=False)

    async with db() as session:
        lookup = await load_appointment_plans(
            session, tenant.id, tenant_plan_ids=[keeps, waives], professional_plan_ids=[]
        )

    assert lookup.resolve(keeps, None) == AppointmentPlan(keeps, "Unimed", True)
    assert lookup.resolve(waives, None) == AppointmentPlan(waives, "Amil", False)


async def test_a_clinic_outro_resolves_to_its_custom_name(db, tenant):
    plan = await _tenant_plan(db, tenant.id, custom_name="Cabesp", note="Paga na recepcao")

    async with db() as session:
        lookup = await load_appointment_plans(
            session, tenant.id, tenant_plan_ids=[plan], professional_plan_ids=[]
        )

    resolved = lookup.resolve(plan, None)
    assert resolved == AppointmentPlan(plan, "Cabesp", True)
    assert not hasattr(resolved, "custom_payment_note")


async def test_an_independent_doctors_rows_resolve_for_catalog_and_outro(db, tenant):
    from_catalog = await _professional_plan(db, tenant.id, catalog_id=UNIMED, charge_deposit=False)
    own_outro = await _professional_plan(db, tenant.id, custom_name="Geap")

    async with db() as session:
        lookup = await load_appointment_plans(
            session,
            tenant.id,
            tenant_plan_ids=[],
            professional_plan_ids=[from_catalog, own_outro],
        )

    assert lookup.resolve(None, from_catalog) == AppointmentPlan(from_catalog, "Unimed", False)
    assert lookup.resolve(None, own_outro) == AppointmentPlan(own_outro, "Geap", True)


async def test_another_clinics_plan_ids_never_resolve(db, tenant, other_tenant):
    """Isolation invariant: an id forged into THIS clinic's appointment must not
    hand back a name that belongs to another clinic."""
    foreign_clinic_plan = await _tenant_plan(db, other_tenant.id, catalog_id=UNIMED)
    foreign_doctor_plan = await _professional_plan(db, other_tenant.id, catalog_id=AMIL)

    async with db() as session:
        lookup = await load_appointment_plans(
            session,
            tenant.id,
            tenant_plan_ids=[foreign_clinic_plan],
            professional_plan_ids=[foreign_doctor_plan],
        )

    assert lookup.resolve(foreign_clinic_plan, None) is None
    assert lookup.resolve(None, foreign_doctor_plan) is None


async def test_a_doctor_row_that_points_at_a_clinic_row_has_no_name_of_its_own(db, tenant):
    """The shared / clinic_with_exceptions shape (tenant_plan_id) carries no name
    or flag on the doctor row; the agenda never invents one."""
    clinic_row = await _tenant_plan(db, tenant.id, catalog_id=UNIMED)
    doctor_row = await _professional_plan(db, tenant.id, tenant_plan_id=clinic_row)

    async with db() as session:
        lookup = await load_appointment_plans(
            session, tenant.id, tenant_plan_ids=[], professional_plan_ids=[doctor_row]
        )

    assert lookup.resolve(None, doctor_row) is None


def test_the_professional_row_wins_and_never_falls_back_to_the_clinic_row():
    """Same precedence as deposit_lifecycle._insurance_charges_deposit."""
    clinic_id, doctor_id = uuid4(), uuid4()
    clinic_plan = AppointmentPlan(clinic_id, "Clinic Unimed", True)
    doctor_plan = AppointmentPlan(doctor_id, "Doctor Amil", False)

    both = AppointmentPlanLookup(
        tenant={clinic_id: clinic_plan}, professional={doctor_id: doctor_plan}
    )
    assert both.resolve(clinic_id, doctor_id) == doctor_plan

    unresolved_doctor = AppointmentPlanLookup(tenant={clinic_id: clinic_plan}, professional={})
    assert unresolved_doctor.resolve(clinic_id, doctor_id) is None
    assert unresolved_doctor.resolve(None, None) is None


async def test_no_plan_ids_means_no_plan_query_at_all(db, tenant):
    with _Sql(db.probe_engine) as sql:
        async with db() as session:
            lookup = await load_appointment_plans(
                session, tenant.id, tenant_plan_ids=[None, None], professional_plan_ids=[]
            )

    assert lookup.tenant == {} and lookup.professional == {}
    assert sql.count("tenant_insurance_plans") == 0
    assert sql.count("professional_insurance_plans") == 0


async def test_many_ids_cost_one_query_per_table(db, tenant):
    clinic_ids = [await _tenant_plan(db, tenant.id, custom_name=f"Outro {n}") for n in range(6)]
    doctor_ids = [await _professional_plan(db, tenant.id, custom_name=f"Doc {n}") for n in range(6)]

    with _Sql(db.probe_engine) as sql:
        async with db() as session:
            await load_appointment_plans(
                session,
                tenant.id,
                tenant_plan_ids=clinic_ids + clinic_ids,  # duplicates collapse
                professional_plan_ids=doctor_ids,
            )

    assert sql.count("FROM tenant_insurance_plans") == 1
    assert sql.count("FROM professional_insurance_plans") == 1


# ---------------------------------------------------------------------------
# Service — deposit state, read-only, tenant-scoped, one query
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", list(PixDepositStatus))
async def test_every_real_deposit_status_comes_back_as_stored(db, tenant, status):
    appointment_id = await _deposit(db, tenant.id, status=status, amount_cents=4550)

    async with db() as session:
        views = await load_deposit_views(session, tenant.id, [appointment_id])

    assert views == {appointment_id: AppointmentDepositView(status, 4550)}


async def test_an_appointment_without_a_deposit_is_simply_absent(db, tenant):
    paid = await _deposit(db, tenant.id, status=PixDepositStatus.PAID)
    no_deposit = uuid4()

    async with db() as session:
        views = await load_deposit_views(session, tenant.id, [paid, no_deposit])

    assert set(views) == {paid}


async def test_another_clinics_deposit_is_never_returned(db, tenant, other_tenant):
    """Isolation invariant: a deposit row of clinic B whose appointment id shows
    up in clinic A's page must not surface as A's money state."""
    foreign = await _deposit(db, other_tenant.id, status=PixDepositStatus.PAID)

    async with db() as session:
        views = await load_deposit_views(session, tenant.id, [foreign])

    assert views == {}


async def test_no_appointment_ids_means_no_deposit_query_at_all(db, tenant):
    with _Sql(db.probe_engine) as sql:
        async with db() as session:
            views = await load_deposit_views(session, tenant.id, [])

    assert views == {}
    assert sql.count("pix_deposits") == 0


async def test_a_none_id_is_dropped_and_never_matches_a_charge_without_an_appointment(db, tenant):
    """Spec F (TASK-016) makes pix_deposits.appointment_id NULLABLE: a `before`-mode
    charge exists with no appointment yet. A None in the ids must not reach the
    query, so such a row can never surface as some event's deposit. (Today's
    NOT NULL column cannot hold that row in SQLite; the F1 executor adds the
    row-level twin of this test when the column widens.)"""
    paid = await _deposit(db, tenant.id, status=PixDepositStatus.PAID)

    with _Sql(db.probe_engine) as sql:
        async with db() as session:
            only_none = await load_deposit_views(session, tenant.id, [None])
            mixed = await load_deposit_views(session, tenant.id, [None, paid])

    assert only_none == {}
    assert set(mixed) == {paid}
    assert sql.count("FROM pix_deposits") == 1  # the [None]-only call issued no query


async def test_many_appointments_cost_one_deposit_query(db, tenant):
    ids = [await _deposit(db, tenant.id, amount_cents=1000 + n) for n in range(8)]

    with _Sql(db.probe_engine) as sql:
        async with db() as session:
            views = await load_deposit_views(session, tenant.id, ids + ids)

    assert len(views) == 8
    assert sql.count("FROM pix_deposits") == 1
    # The payload column is never selected: the read model has no use for it.
    assert not any("pix_copy_paste" in s for s in sql.statements)


# ---------------------------------------------------------------------------
# HTTP — GET /events carries the convênio
# ---------------------------------------------------------------------------


class _FakeCalendarService:
    """No Google call is ever attempted; `next_events` is what /events sees."""

    next_events: list[dict] = []

    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    async def check_availability(self, start, end):
        return _FakeCalendarService.next_events


@pytest.fixture(autouse=True)
def _override(db, tenant, monkeypatch: pytest.MonkeyPatch):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    monkeypatch.setattr(hub_calendar, "CalendarService", _FakeCalendarService)
    _FakeCalendarService.next_events = []
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _connect_calendar(db, tenant) -> None:
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "fake-refresh-token")
        await session.commit()


def _wire_event(event_id: str) -> dict:
    return {
        "id": event_id,
        "summary": "Consulta",
        "start": "2026-08-19T10:00:00Z",
        "end": "2026-08-19T10:30:00Z",
    }


async def _appointment(
    db,
    tenant_id: UUID,
    google_event_id: str,
    *,
    insurance: str | None = None,
    plan_id: UUID | None = None,
    professional_plan_id: UUID | None = None,
) -> Appointment:
    async with db() as session:
        appt = Appointment(
            id=uuid4(),
            tenant_id=tenant_id,
            google_event_id=google_event_id,
            appointment_type="Consulta",
            start_at=datetime.now(UTC) + timedelta(days=1),
            end_at=datetime.now(UTC) + timedelta(days=1, minutes=30),
            status=AppointmentStatus.SCHEDULED,
            insurance=insurance,
            insurance_plan_id=plan_id,
            insurance_professional_plan_id=professional_plan_id,
        )
        session.add(appt)
        await session.commit()
        return appt


async def _events(client: AsyncClient):
    return await client.get(
        f"{CALENDAR}/events",
        params={"start": "2026-08-18T00:00:00Z", "end": "2026-08-20T00:00:00Z"},
    )


async def test_the_clinic_path_carries_label_plan_name_and_flag(client: AsyncClient, db, tenant):
    await _connect_calendar(db, tenant)
    plan = await _tenant_plan(db, tenant.id, catalog_id=UNIMED, charge_deposit=False)
    await _appointment(db, tenant.id, "evt-1", insurance="Unimed", plan_id=plan)
    _FakeCalendarService.next_events = [_wire_event("evt-1")]

    resp = await _events(client)

    assert resp.status_code == 200
    body = resp.json()[0]
    assert body["insurance"] == "Unimed"
    assert body["insurance_plan"] == {
        "id": str(plan),
        "name": "Unimed",
        "charge_deposit": False,
    }


async def test_the_independent_path_resolves_through_the_professional_row(
    client: AsyncClient, db, tenant
):
    await _connect_calendar(db, tenant)
    doctor_plan = await _professional_plan(db, tenant.id, catalog_id=AMIL, charge_deposit=True)
    await _appointment(db, tenant.id, "evt-1", insurance="Amil", professional_plan_id=doctor_plan)
    _FakeCalendarService.next_events = [_wire_event("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["insurance"] == "Amil"
    assert body["insurance_plan"] == {
        "id": str(doctor_plan),
        "name": "Amil",
        "charge_deposit": True,
    }


async def test_a_clinic_outro_shows_its_name_and_never_its_payment_note(
    client: AsyncClient, db, tenant
):
    await _connect_calendar(db, tenant)
    plan = await _tenant_plan(
        db, tenant.id, custom_name="Cabesp", note="NOTA-INTERNA-NAO-VAZAR paga na recepcao"
    )
    await _appointment(db, tenant.id, "evt-1", insurance="Cabesp", plan_id=plan)
    _FakeCalendarService.next_events = [_wire_event("evt-1")]

    resp = await _events(client)

    body = resp.json()[0]
    assert body["insurance_plan"]["name"] == "Cabesp"
    assert set(body["insurance_plan"]) == {"id", "name", "charge_deposit"}
    assert "NOTA-INTERNA-NAO-VAZAR" not in resp.text


async def test_a_label_without_a_resolved_plan_keeps_the_label_and_no_plan(
    client: AsyncClient, db, tenant
):
    """ "Particular" / a typed "Outro convênio": the patient's own answer is still
    the truth, there is just no plan row behind it."""
    await _connect_calendar(db, tenant)
    await _appointment(db, tenant.id, "evt-1", insurance="Particular")
    _FakeCalendarService.next_events = [_wire_event("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["insurance"] == "Particular"
    assert body["insurance_plan"] is None


@pytest.mark.parametrize("blank", [None, "", "   "])
async def test_a_blank_label_is_null_not_an_empty_string(client: AsyncClient, db, tenant, blank):
    await _connect_calendar(db, tenant)
    await _appointment(db, tenant.id, "evt-1", insurance=blank)
    _FakeCalendarService.next_events = [_wire_event("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["insurance"] is None
    assert body["insurance_plan"] is None


async def test_a_google_only_event_has_no_convenio_and_costs_no_plan_query(
    client: AsyncClient, db, tenant
):
    await _connect_calendar(db, tenant)
    _FakeCalendarService.next_events = [_wire_event("evt-typed-in-google")]

    with _Sql(db.probe_engine) as sql:
        body = (await _events(client)).json()[0]

    assert body["appointment_id"] is None
    assert body["insurance"] is None and body["insurance_plan"] is None
    assert body["deposit"] is None
    assert sql.count("tenant_insurance_plans") == 0
    assert sql.count("professional_insurance_plans") == 0
    assert sql.count("pix_deposits") == 0


async def test_a_forged_plan_id_from_another_clinic_never_resolves(
    client: AsyncClient, db, tenant, other_tenant
):
    """Isolation invariant at the HTTP boundary: this clinic's appointment row
    points at plan rows that belong to ANOTHER clinic."""
    await _connect_calendar(db, tenant)
    foreign_clinic_plan = await _tenant_plan(db, other_tenant.id, catalog_id=UNIMED)
    foreign_doctor_plan = await _professional_plan(db, other_tenant.id, custom_name="Segredo Outra")
    await _appointment(db, tenant.id, "evt-clinic", insurance="Unimed", plan_id=foreign_clinic_plan)
    await _appointment(
        db, tenant.id, "evt-doctor", insurance="Geap", professional_plan_id=foreign_doctor_plan
    )
    _FakeCalendarService.next_events = [_wire_event("evt-clinic"), _wire_event("evt-doctor")]

    resp = await _events(client)

    first, second = resp.json()
    assert first["insurance"] == "Unimed" and first["insurance_plan"] is None
    assert second["insurance"] == "Geap" and second["insurance_plan"] is None
    assert "Segredo Outra" not in resp.text


async def test_another_clinics_appointment_never_lends_its_convenio(
    client: AsyncClient, db, tenant, other_tenant
):
    """A shared or mis-configured Google account can list an event whose id
    matches another clinic's appointment: neither its id nor its convênio leak."""
    await _connect_calendar(db, tenant)
    foreign_plan = await _tenant_plan(db, other_tenant.id, catalog_id=UNIMED)
    await _appointment(db, other_tenant.id, "evt-foreign", insurance="Unimed", plan_id=foreign_plan)
    _FakeCalendarService.next_events = [_wire_event("evt-foreign")]

    body = (await _events(client)).json()[0]

    assert body["appointment_id"] is None
    assert body["insurance"] is None and body["insurance_plan"] is None


async def test_a_removed_plan_keeps_the_label_and_a_renamed_plan_shows_its_current_name(
    client: AsyncClient, db, tenant
):
    await _connect_calendar(db, tenant)
    renamed = await _tenant_plan(db, tenant.id, custom_name="Nome antigo")
    await _appointment(db, tenant.id, "evt-renamed", insurance="Nome antigo", plan_id=renamed)
    # A plan the clinic removed: the FK is SET NULL, so the appointment keeps only the label.
    await _appointment(db, tenant.id, "evt-removed", insurance="Unimed", plan_id=None)
    async with db() as session:
        row = await session.get(TenantInsurancePlan, renamed)
        row.custom_name = "Nome novo"
        await session.commit()
    _FakeCalendarService.next_events = [_wire_event("evt-renamed"), _wire_event("evt-removed")]

    renamed_body, removed_body = (await _events(client)).json()

    assert renamed_body["insurance"] == "Nome antigo"
    assert renamed_body["insurance_plan"]["name"] == "Nome novo"
    assert removed_body["insurance"] == "Unimed"
    assert removed_body["insurance_plan"] is None


async def test_a_mixed_page_costs_one_query_per_table_not_one_per_event(
    client: AsyncClient, db, tenant
):
    """A month of a busy clinic is hundreds of events; N+1 here would put that
    many round trips behind a screen the doctor opens constantly."""
    await _connect_calendar(db, tenant)
    events = []
    for n in range(12):
        gid = f"evt-{n}"
        events.append(_wire_event(gid))
        if n % 4 == 0:
            plan = await _tenant_plan(db, tenant.id, custom_name=f"Clinica {n}")
            appt = await _appointment(db, tenant.id, gid, insurance=f"Clinica {n}", plan_id=plan)
        elif n % 4 == 1:
            plan = await _professional_plan(db, tenant.id, custom_name=f"Medico {n}")
            appt = await _appointment(
                db, tenant.id, gid, insurance=f"Medico {n}", professional_plan_id=plan
            )
        elif n % 4 == 2:
            appt = await _appointment(db, tenant.id, gid, insurance="Particular")
        else:
            appt = await _appointment(db, tenant.id, gid)
        if n % 3 == 0:
            await _deposit(db, tenant.id, appointment_id=appt.id, status=PixDepositStatus.PAID)
    _FakeCalendarService.next_events = events

    with _Sql(db.probe_engine) as sql:
        resp = await _events(client)

    assert resp.status_code == 200
    assert len([e for e in resp.json() if e["insurance_plan"]]) == 6
    assert len([e for e in resp.json() if e["deposit"]]) == 4
    assert sql.count("FROM appointments") == 1
    assert sql.count("FROM tenant_insurance_plans") == 1
    assert sql.count("FROM professional_insurance_plans") == 1
    assert sql.count("FROM pix_deposits") == 1


@pytest.mark.parametrize("status", list(PixDepositStatus))
async def test_the_deposit_carries_its_real_status_value_and_amount(
    client: AsyncClient, db, tenant, status
):
    await _connect_calendar(db, tenant)
    appt = await _appointment(db, tenant.id, "evt-1")
    await _deposit(db, tenant.id, appointment_id=appt.id, status=status, amount_cents=4550)
    _FakeCalendarService.next_events = [_wire_event("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["deposit"] == {"status": status.value, "amount_cents": 4550}


async def test_a_paid_deposit_says_nothing_about_the_appointment_and_leaks_no_payment_data(
    client: AsyncClient, db, tenant
):
    """Today a paid deposit leaves Appointment.status at SCHEDULED, so this wire
    must not pretend otherwise; and the Pix payload / Asaas id stay server-side."""
    await _connect_calendar(db, tenant)
    appt = await _appointment(db, tenant.id, "evt-1")
    await _deposit(db, tenant.id, appointment_id=appt.id, status=PixDepositStatus.PAID)
    _FakeCalendarService.next_events = [_wire_event("evt-1")]

    resp = await _events(client)

    body = resp.json()[0]
    assert set(body["deposit"]) == {"status", "amount_cents"}
    assert body["status"] == "scheduled"
    assert body["confirmation_count"] == 0
    assert "NAO-VAZAR" not in resp.text
    assert "pay_" not in resp.text


async def test_an_appointment_with_no_deposit_row_has_a_null_deposit(
    client: AsyncClient, db, tenant
):
    """No deposit = null. The backend cannot tell "clinic charges none", "manual
    booking" and "charge failed" apart, so it never invents a state for them."""
    await _connect_calendar(db, tenant)
    await _appointment(db, tenant.id, "evt-1")
    _FakeCalendarService.next_events = [_wire_event("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["deposit"] is None


async def test_another_clinics_deposit_row_never_lends_its_state(
    client: AsyncClient, db, tenant, other_tenant
):
    """Isolation invariant at the HTTP boundary: a pix_deposits row of ANOTHER
    clinic that points at this clinic's appointment id must not show as PAID."""
    await _connect_calendar(db, tenant)
    appt = await _appointment(db, tenant.id, "evt-1")
    await _deposit(db, other_tenant.id, appointment_id=appt.id, status=PixDepositStatus.PAID)
    _FakeCalendarService.next_events = [_wire_event("evt-1")]

    body = (await _events(client)).json()[0]

    assert body["deposit"] is None


async def test_the_event_wire_only_grew_by_three_optional_keys(client: AsyncClient, db, tenant):
    """Existing consumers (the secretarIA-frontend agenda) read by key and ignore
    the rest; the old keys and their values are untouched."""
    await _connect_calendar(db, tenant)
    appt = await _appointment(db, tenant.id, "evt-1")
    _FakeCalendarService.next_events = [_wire_event("evt-1")]

    body = (await _events(client)).json()[0]

    assert set(body) == LEGACY_EVENT_KEYS | {"insurance", "insurance_plan", "deposit"} | NEW_EVENT_KEYS
    assert body["id"] == "evt-1"
    assert body["summary"] == "Consulta"
    assert body["appointment_id"] == str(appt.id)
