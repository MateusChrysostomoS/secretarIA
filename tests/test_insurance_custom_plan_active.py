"""TASK-090 Adendo 1, contract C7 - the ON/OFF switch of a convênio the clinic typed by hand.

Owner decision (2026-11-24): an "Outro convênio" (a custom row: `custom_name`, no catalog id) can
be switched OFF in both modes - the clinic's row (`shared`) and a doctor's own row
(`independent`). OFF keeps the row, its name and its payment rule; the convênio then reaches no
patient and no AI tool (it behaves like an unknown convênio). ON brings it back intact. Catalog
convênios keep "OFF deletes the row".

Covered: the migration (up/down), both PUT .../active routes (roles, 404 cross-tenant, 422 catalog
line, 409 wrong mode, the convênio cap on switching ON, idempotency), and every reader the switch
must reach - the patient's list (the flow snapshot), the AI (clinic features, the payment-rule
tool), the acceptance matrix, the cap count, booking resolution - plus a catalog replace keeping
the switched-OFF row and an existing appointment with that convênio still being editable.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.operations import Operations  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.api.hub import deps as hub_deps  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.catalog_limits import INSURANCE_PLANS_PER_CLINIC  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    InsuranceAcceptanceException,
    Professional,
    Service,
    Tenant,
)
from secretaria.models.insurance import (  # noqa: E402
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.services import (
    appointment_edit_write as writer,  # noqa: E402
    flow_router as fr,  # noqa: E402
    insurance_payment_note as ipn,  # noqa: E402
)
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    ensure_catalog_seeded,
    load_appointment_plans,
    load_tenant_insurance,
    resolve_booking_plan_ids,
    set_professional_independent_plans,
    set_tenant_plans,
)
from secretaria.services.llm_context import build_clinic_features  # noqa: E402
from secretaria.services.service_catalog import normalize  # noqa: E402
from tests._reminders_v2 import seed_world  # noqa: E402
from tests.payments_support import make_sessionmaker  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REVISION = "c7e3a9f1d5b8"
HEADERS = {"authorization": "Bearer c7-test-token"}
UNIMED = catalog_id_for_slug("unimed")
AMIL = catalog_id_for_slug("amil")
RULE = "Reembolso em até 30 dias."


# ------------------------------------------------------------------------------ migration


def _migration():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [REVISION]
    return scripts.get_revision(REVISION).module


def test_the_migration_sits_on_the_previous_head_and_carries_the_deploy_order():
    migration = _migration()
    assert migration.down_revision == "a3c9e5d7f1b4"
    assert "DEPLOY ORDER" in (migration.__doc__ or "")
    assert "Rollback" in (migration.__doc__ or "")


def test_the_migration_adds_and_drops_a_true_by_default_column_on_both_tables(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'c7.db'}")
    with engine.begin() as conn:
        for table in ("tenant_insurance_plans", "professional_insurance_plans"):
            conn.execute(sa.text(f"CREATE TABLE {table} (id CHAR(32) PRIMARY KEY, name TEXT)"))
            conn.execute(sa.text(f"INSERT INTO {table} VALUES ('old', 'GEAP')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            for table in ("tenant_insurance_plans", "professional_insurance_plans"):
                column = {c["name"]: c for c in sa.inspect(conn).get_columns(table)}["is_active"]
                assert column["nullable"] is False
                # The existing row reads ON: today's behaviour.
                assert conn.execute(sa.text(f"SELECT is_active FROM {table}")).scalar() in (1, True)
            migration.downgrade()
            for table in ("tenant_insurance_plans", "professional_insurance_plans"):
                names = {c["name"] for c in sa.inspect(conn).get_columns(table)}
                assert names == {"id", "name"}


# -------------------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def hub(db, monkeypatch):
    """Clinic "Mine" (shared): Unimed (catalog) + its own "GEAP" (with a rule). Clinic "Other"
    has its own "Cassi". The staff claim's role is editable per test."""
    from secretaria.main import app

    async with db() as session:
        mine = Tenant(id=uuid4(), clinic_name="Mine", phone_number_id=None, insurance_mode="shared")
        other = Tenant(
            id=uuid4(), clinic_name="Other", phone_number_id=None, insurance_mode="shared"
        )
        session.add_all([mine, other])
        await session.flush()
        unimed = TenantInsurancePlan(tenant_id=mine.id, catalog_id=UNIMED)
        geap = TenantInsurancePlan(tenant_id=mine.id, custom_name="GEAP", custom_payment_note=RULE)
        foreign = TenantInsurancePlan(tenant_id=other.id, custom_name="Cassi")
        session.add_all([unimed, geap, foreign])
        await session.commit()

    claim = {"role": "manager", "is_owner": False, "is_manager": True, "tenant_id": mine.id}

    async def _session():
        async with db() as session:
            yield session

    async def _verify(token, *, fresh=False):
        return SubscriptionClaim(
            tenant_id=claim["tenant_id"],
            active=True,
            role=claim["role"],
            is_owner=claim["is_owner"],
            is_manager=claim["is_manager"],
            professional_id=None,
        )

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    monkeypatch.setattr(core_database, "async_session_factory", db)
    yield SimpleNamespace(
        db=db, mine=mine, other=other, unimed=unimed, geap=geap, foreign=foreign, claim=claim
    )
    app.dependency_overrides.pop(get_session, None)


def _url(plan_id) -> str:
    return f"/tenants/me/insurance-plans/{plan_id}/active"


def _doctor_url(doctor_id, plan_id) -> str:
    return f"/tenants/me/professionals/{doctor_id}/insurance-plans/{plan_id}/active"


async def _row(db, model, plan_id):
    async with db() as session:
        return await session.get(model, plan_id)


async def _patient_names(db, tenant_id) -> list[str]:
    async with db() as session:
        return [plan["name"] for plan in (await load_tenant_insurance(session, tenant_id)).plans]


async def _flow_snapshot(db, tenant_id):
    """The router's tenant snapshot exactly as the worker builds it (insurance part)."""
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        insurance = await load_tenant_insurance(session, tenant_id)
    return SimpleNamespace(
        insurance_mode=tenant.insurance_mode,
        insurances=tenant.insurances,
        insurance_plans=insurance.plans,
        insurance_accepted_by=insurance.accepted_by,
        insurance_refused=insurance.refused,
        pix_deposit_enabled=False,
        initial_flows=None,
    )


async def _ask_rule(tenant_id, insurance: str) -> str:
    token = ai_tools._tenant_id_ctx.set(tenant_id)
    try:
        return await ai_tools.get_insurance_payment_rule.ainvoke({"insurance": insurance})
    finally:
        ai_tools._tenant_id_ctx.reset(token)


async def _independent_doctor(db, tenant_id, name="Dra. Ana") -> Professional:
    async with db() as session:
        clinic = await session.get(Tenant, tenant_id)
        clinic.insurance_mode = "independent"
        doctor = Professional(id=uuid4(), tenant_id=tenant_id, name=name, is_active=True)
        session.add(doctor)
        await session.commit()
        return doctor


# ----------------------------------------------------------------------- clinic route (shared)


async def test_manager_switches_the_clinic_outro_off_and_on_keeping_name_and_rule(client, hub):
    w = hub
    off = await client.put(_url(w.geap.id), json={"is_active": False}, headers=HEADERS)
    assert off.status_code == 200, off.text
    assert off.json() == {
        "id": str(w.geap.id),
        "catalog_id": None,
        "name": "GEAP",
        "is_custom": True,
        "custom_payment_note": RULE,
        "sort_order": None,
        "is_active": False,
    }
    stored = await _row(w.db, TenantInsurancePlan, w.geap.id)
    assert (stored.custom_name, stored.custom_payment_note, stored.is_active) == (
        "GEAP",
        RULE,
        False,
    )
    # The manager's own list keeps the row, flagged OFF; the catalog line reads ON.
    listed = await client.get("/tenants/me/insurance-plans", headers=HEADERS)
    assert {p["name"]: p["is_active"] for p in listed.json()} == {"GEAP": False, "Unimed": True}

    on = await client.put(_url(w.geap.id), json={"is_active": True}, headers=HEADERS)
    assert on.status_code == 200 and on.json()["is_active"] is True
    assert on.json()["name"] == "GEAP" and on.json()["custom_payment_note"] == RULE
    assert "GEAP" in await _patient_names(w.db, w.mine.id)


async def test_switching_to_the_value_it_has_is_a_200_no_op(client, hub):
    w = hub
    for _ in range(2):
        same = await client.put(_url(w.geap.id), json={"is_active": True}, headers=HEADERS)
        assert same.status_code == 200 and same.json()["is_active"] is True
    for _ in range(2):
        off = await client.put(_url(w.geap.id), json={"is_active": False}, headers=HEADERS)
        assert off.status_code == 200 and off.json()["is_active"] is False


@pytest.mark.parametrize(
    ("role", "is_owner", "is_manager"),
    [("doctor", False, False), ("secretary", False, False)],
)
async def test_any_staff_role_that_may_create_an_outro_may_switch_it(
    client, hub, role, is_owner, is_manager
):
    """Same gate as `POST /insurance-plans/custom` (role-blind `get_current_tenant`)."""
    hub.claim.update(role=role, is_owner=is_owner, is_manager=is_manager)
    created = await client.post(
        "/tenants/me/insurance-plans/custom", json={"custom_name": "Saúde Caixa"}, headers=HEADERS
    )
    assert created.status_code == 201
    switched = await client.put(_url(hub.geap.id), json={"is_active": False}, headers=HEADERS)
    assert switched.status_code == 200


async def test_without_a_staff_token_the_switch_is_refused(client, hub):
    resp = await client.put(_url(hub.geap.id), json={"is_active": False})
    assert resp.status_code in (401, 403)
    assert (await _row(hub.db, TenantInsurancePlan, hub.geap.id)).is_active is True


@pytest.mark.parametrize("which", ["foreign", "unknown", "garbage"])
async def test_another_clinics_or_an_unknown_line_is_404(client, hub, which):
    w = hub
    plan_id = {"foreign": w.foreign.id, "unknown": uuid4(), "garbage": "not-a-uuid"}[which]
    resp = await client.put(_url(plan_id), json={"is_active": False}, headers=HEADERS)
    assert resp.status_code == 404
    assert (await _row(w.db, TenantInsurancePlan, w.foreign.id)).is_active is True


async def test_a_catalog_line_has_no_switch(client, hub):
    resp = await client.put(_url(hub.unimed.id), json={"is_active": False}, headers=HEADERS)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "not_custom_plan"
    assert (await _row(hub.db, TenantInsurancePlan, hub.unimed.id)).is_active is True


async def test_the_body_is_strict(client, hub):
    for body in ({}, {"is_active": True, "extra": 1}):
        resp = await client.put(_url(hub.geap.id), json=body, headers=HEADERS)
        assert resp.status_code == 422


# ------------------------------------------------------------------------------------- cap


async def _fill_to_the_cap(db, tenant_id) -> None:
    """Active convênios up to exactly the cap (Unimed + GEAP already count as 2)."""
    async with db() as session:
        for i in range(INSURANCE_PLANS_PER_CLINIC - 2):
            session.add(TenantInsurancePlan(tenant_id=tenant_id, custom_name=f"Plano {i}"))
        await session.commit()


async def test_switching_on_respects_the_cap_counting_only_active_lines(client, hub):
    w = hub
    off = await client.put(_url(w.geap.id), json={"is_active": False}, headers=HEADERS)
    assert off.status_code == 200
    await _fill_to_the_cap(w.db, w.mine.id)  # 59 active + GEAP switched OFF
    assert len(await _patient_names(w.db, w.mine.id)) == INSURANCE_PLANS_PER_CLINIC - 1

    # One free slot: a NEW "Outro" fits (the OFF line is not counted)...
    added = await client.post(
        "/tenants/me/insurance-plans/custom", json={"custom_name": "Novo"}, headers=HEADERS
    )
    assert added.status_code == 201
    # ...and then switching GEAP back ON would make 61: refused, nothing written.
    refused = await client.put(_url(w.geap.id), json={"is_active": True}, headers=HEADERS)
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["code"] == "catalog_limit_exceeded"
    assert (detail["limit"], detail["count"]) == (INSURANCE_PLANS_PER_CLINIC, 61)
    assert (await _row(w.db, TenantInsurancePlan, w.geap.id)).is_active is False
    # Switching OFF never fails on the cap, and frees the slot for GEAP.
    novo = next(
        p["id"]
        for p in (await client.get("/tenants/me/insurance-plans", headers=HEADERS)).json()
        if p["name"] == "Novo"
    )
    assert (
        await client.put(_url(novo), json={"is_active": False}, headers=HEADERS)
    ).status_code == 200
    back = await client.put(_url(w.geap.id), json={"is_active": True}, headers=HEADERS)
    assert back.status_code == 200 and back.json()["is_active"] is True


# -------------------------------------------------------------- doctor route (independent)


async def test_doctor_outro_switches_off_and_on_in_independent_mode(client, hub):
    w = hub
    doctor = await _independent_doctor(w.db, w.mine.id)
    own = await client.post(
        f"/tenants/me/professionals/{doctor.id}/insurance-plans/custom",
        json={"custom_name": "Plano da Ana"},
        headers=HEADERS,
    )
    assert own.status_code == 201 and own.json()["is_active"] is True
    plan_id = own.json()["id"]
    note = await client.put(
        f"/tenants/me/professionals/{doctor.id}/insurance-plans/{plan_id}/payment-note",
        json={"custom_payment_note": RULE},
        headers=HEADERS,
    )
    assert note.status_code == 200
    assert "Plano da Ana" in await _patient_names(w.db, w.mine.id)

    off = await client.put(
        _doctor_url(doctor.id, plan_id), json={"is_active": False}, headers=HEADERS
    )
    assert off.status_code == 200, off.text
    assert off.json()["is_active"] is False and off.json()["custom_payment_note"] == RULE
    assert "Plano da Ana" not in await _patient_names(w.db, w.mine.id)
    # The picker (manager view) still shows the row, flagged OFF; catalog rows read ON.
    got = await client.get(
        f"/tenants/me/professionals/{doctor.id}/insurance-plans", headers=HEADERS
    )
    flags = {p["name"]: p["is_active"] for p in got.json()["selectable"]}
    assert flags["Plano da Ana"] is False and flags["Unimed"] is True
    assert plan_id in got.json()["accepted_plan_ids"]
    # Idempotent.
    again = await client.put(
        _doctor_url(doctor.id, plan_id), json={"is_active": False}, headers=HEADERS
    )
    assert again.status_code == 200

    on = await client.put(
        _doctor_url(doctor.id, plan_id), json={"is_active": True}, headers=HEADERS
    )
    assert on.status_code == 200 and on.json()["is_active"] is True
    assert on.json()["name"] == "Plano da Ana" and on.json()["custom_payment_note"] == RULE
    assert "Plano da Ana" in await _patient_names(w.db, w.mine.id)


async def test_doctor_route_refusals(client, hub):
    w = hub
    doctor = await _independent_doctor(w.db, w.mine.id)
    picked = await client.put(
        f"/tenants/me/professionals/{doctor.id}/insurance-plans",
        json={"catalog_ids": [str(UNIMED)]},
        headers=HEADERS,
    )
    assert picked.status_code == 200
    # A catalog pick he accepts: 422 not_custom_plan.
    catalog = await client.put(
        _doctor_url(doctor.id, UNIMED), json={"is_active": False}, headers=HEADERS
    )
    assert catalog.status_code == 422 and catalog.json()["detail"]["code"] == "not_custom_plan"
    # A catalog entry he does not have / an unknown id: 404.
    for missing in (AMIL, uuid4(), "garbage"):
        resp = await client.put(
            _doctor_url(doctor.id, missing), json={"is_active": False}, headers=HEADERS
        )
        assert resp.status_code == 404
    # Another doctor's "Outro" (same clinic) is not his: 404.
    async with w.db() as session:
        bia = Professional(id=uuid4(), tenant_id=w.mine.id, name="Dra. Bia", is_active=True)
        session.add(bia)
        await session.flush()
        bias = ProfessionalInsurancePlan(
            professional_id=bia.id, tenant_id=w.mine.id, custom_name="Plano da Bia"
        )
        session.add(bias)
        await session.commit()
    not_his = await client.put(
        _doctor_url(doctor.id, bias.id), json={"is_active": False}, headers=HEADERS
    )
    assert not_his.status_code == 404
    # Another clinic's doctor: 404.
    async with w.db() as session:
        stranger = Professional(id=uuid4(), tenant_id=w.other.id, name="X", is_active=True)
        session.add(stranger)
        await session.flush()
        theirs = ProfessionalInsurancePlan(
            professional_id=stranger.id, tenant_id=w.other.id, custom_name="Deles"
        )
        session.add(theirs)
        await session.commit()
    foreign = await client.put(
        _doctor_url(stranger.id, theirs.id), json={"is_active": False}, headers=HEADERS
    )
    assert foreign.status_code == 404
    assert (await _row(w.db, ProfessionalInsurancePlan, theirs.id)).is_active is True
    assert (await _row(w.db, ProfessionalInsurancePlan, bias.id)).is_active is True
    # Outside `independent`: 409, like the payment-note route.
    async with w.db() as session:
        (await session.get(Tenant, w.mine.id)).insurance_mode = "shared"
        await session.commit()
    wrong_mode = await client.put(
        _doctor_url(doctor.id, bias.id), json={"is_active": False}, headers=HEADERS
    )
    assert wrong_mode.status_code == 409
    assert wrong_mode.json()["detail"]["code"] == "insurance_mode_not_applicable"


async def test_doctor_route_role_gate_is_the_custom_posts(client, hub):
    w = hub
    doctor = await _independent_doctor(w.db, w.mine.id)
    async with w.db() as session:
        own = ProfessionalInsurancePlan(
            professional_id=doctor.id, tenant_id=w.mine.id, custom_name="Plano da Ana"
        )
        session.add(own)
        await session.commit()
    w.claim.update(role="secretary", is_owner=False, is_manager=False)
    resp = await client.put(
        _doctor_url(doctor.id, own.id), json={"is_active": False}, headers=HEADERS
    )
    assert resp.status_code == 200


async def test_doctor_switch_on_respects_the_cap(client, hub):
    w = hub
    doctor = await _independent_doctor(w.db, w.mine.id)
    async with w.db() as session:
        off = ProfessionalInsurancePlan(
            professional_id=doctor.id, tenant_id=w.mine.id, custom_name="Desligado", is_active=False
        )
        session.add(off)
        for i in range(INSURANCE_PLANS_PER_CLINIC):
            session.add(
                ProfessionalInsurancePlan(
                    professional_id=doctor.id, tenant_id=w.mine.id, custom_name=f"P{i}"
                )
            )
        await session.commit()
    assert len(await _patient_names(w.db, w.mine.id)) == INSURANCE_PLANS_PER_CLINIC
    refused = await client.put(
        _doctor_url(doctor.id, off.id), json={"is_active": True}, headers=HEADERS
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "catalog_limit_exceeded"
    assert (await _row(w.db, ProfessionalInsurancePlan, off.id)).is_active is False


# ------------------------------------------------------- an OFF line reaches no patient / AI


async def test_an_off_clinic_outro_is_gone_from_the_patient_flow_and_the_ai(client, hub):
    w = hub
    before = await _flow_snapshot(w.db, w.mine.id)
    assert fr.insurance_plan_names(before) == ["GEAP", "Unimed"]
    assert build_clinic_features(before).insurance_plan_count == 2
    assert ipn.FENCE_OPEN in await _ask_rule(w.mine.id, "GEAP")

    await client.put(_url(w.geap.id), json={"is_active": False}, headers=HEADERS)

    after = await _flow_snapshot(w.db, w.mine.id)
    # The patient's list (and the AI's view of it) no longer has it...
    assert fr.insurance_plan_names(after) == ["Unimed"]
    assert build_clinic_features(after).insurance_plan_count == 1
    assert all(plan["id"] != str(w.geap.id) for plan in after.insurance_plans)
    # ...the patient typing its name matches nothing (the "unknown convênio" path)...
    assert fr.match_plan(fr._tenant_insurance_plans(after), "GEAP") is None
    # ...and the AI never reads its rule: an unknown convênio -> the Sim/Não offer.
    with pytest.raises(ai_tools.HumanHandoffRequested) as handed:
        await _ask_rule(w.mine.id, "geap")
    assert handed.value.reason == "insurance_payment_question"
    # A NEW booking naming it records no plan id (like a typed unknown convênio).
    async with w.db() as session:
        assert await resolve_booking_plan_ids(session, w.mine.id, "GEAP") == (None, None)

    # ON again: back in the list, rule intact.
    await client.put(_url(w.geap.id), json={"is_active": True}, headers=HEADERS)
    assert fr.insurance_plan_names(await _flow_snapshot(w.db, w.mine.id)) == ["GEAP", "Unimed"]
    assert RULE in await _ask_rule(w.mine.id, "GEAP")


async def test_a_legacy_string_of_the_same_name_does_not_bring_it_back(client, hub):
    w = hub
    async with w.db() as session:
        (await session.get(Tenant, w.mine.id)).insurances = ["Unimed", "GEAP", "Cabesp"]
        await session.commit()
    await client.put(_url(w.geap.id), json={"is_active": False}, headers=HEADERS)
    names = await _patient_names(w.db, w.mine.id)
    assert "GEAP" not in names and "Cabesp" in names


async def test_an_off_doctor_outro_is_gone_from_the_list_the_matrix_and_the_ai(client, hub):
    w = hub
    doctor = await _independent_doctor(w.db, w.mine.id)
    async with w.db() as session:
        own = ProfessionalInsurancePlan(
            professional_id=doctor.id,
            tenant_id=w.mine.id,
            custom_name="Plano da Ana",
            custom_payment_note=RULE,
        )
        session.add(own)
        await session.commit()
    matrix_url = f"/tenants/me/professionals/{doctor.id}/insurance-acceptance"
    cols = await client.get(matrix_url, headers=HEADERS)
    assert [p["name"] for p in cols.json()["plans"]] == ["Plano da Ana"]

    await client.put(_doctor_url(doctor.id, own.id), json={"is_active": False}, headers=HEADERS)

    async with w.db() as session:
        insurance = await load_tenant_insurance(session, w.mine.id)
    assert insurance.plans == [] and str(own.id) not in insurance.accepted_by.get(
        str(doctor.id), frozenset()
    )
    assert (await client.get(matrix_url, headers=HEADERS)).json()["plans"] == []
    with pytest.raises(ai_tools.HumanHandoffRequested):
        await _ask_rule(w.mine.id, "Plano da Ana")
    async with w.db() as session:
        assert await resolve_booking_plan_ids(session, w.mine.id, "Plano da Ana", doctor.id) == (
            None,
            None,
        )


async def test_the_clinic_matrix_hides_the_off_column_and_keeps_its_cells(client, hub):
    w = hub
    async with w.db() as session:
        service = Service(
            id=uuid4(), tenant_id=w.mine.id, name="Consulta", normalized_name=normalize("Consulta")
        )
        session.add(service)
        await session.flush()
        session.add(
            InsuranceAcceptanceException(
                tenant_id=w.mine.id, service_id=service.id, tenant_plan_id=w.geap.id
            )
        )
        await session.commit()
    url = "/tenants/me/insurance-acceptance/clinic"
    read = await client.get(url, headers=HEADERS)
    assert read.status_code == 200, read.text
    assert {p["name"] for p in read.json()["plans"]} == {"GEAP", "Unimed"}

    await client.put(_url(w.geap.id), json={"is_active": False}, headers=HEADERS)
    hidden = await client.get(url, headers=HEADERS)
    assert [p["name"] for p in hidden.json()["plans"]] == ["Unimed"]
    assert all(not s["exceptions"] for s in hidden.json()["slices"])
    # Saving the slice (the OFF column is not on screen) keeps the OFF line's refusal...
    saved = await client.put(
        f"{url}/units/none",
        json={"exceptions": [{"service_id": str(service.id), "plan_id": str(w.unimed.id)}]},
        headers=HEADERS,
    )
    assert saved.status_code == 200, saved.text
    # ...and a cell naming the OFF line is not accepted (not a column).
    refused = await client.put(
        f"{url}/units/none",
        json={"exceptions": [{"service_id": str(service.id), "plan_id": str(w.geap.id)}]},
        headers=HEADERS,
    )
    assert refused.status_code == 422
    # ON again: the column and its refusal are back.
    await client.put(_url(w.geap.id), json={"is_active": True}, headers=HEADERS)
    back = await client.get(url, headers=HEADERS)
    cells = {c["plan_id"] for s in back.json()["slices"] for c in s["exceptions"]}
    assert cells == {str(w.geap.id), str(w.unimed.id)}


async def test_a_catalog_replace_keeps_the_off_custom_rows(hub):
    w = hub
    async with w.db() as session:
        geap = await session.get(TenantInsurancePlan, w.geap.id)
        geap.is_active = False
        await session.commit()
    async with w.db() as session:
        tenant = await session.get(Tenant, w.mine.id)
        await set_tenant_plans(session, tenant, [AMIL])
        await session.commit()
    row = await _row(w.db, TenantInsurancePlan, w.geap.id)
    assert (row.custom_name, row.custom_payment_note, row.is_active) == ("GEAP", RULE, False)
    # The doctor's catalog replace keeps his OFF "Outro" too.
    doctor = await _independent_doctor(w.db, w.mine.id)
    async with w.db() as session:
        own = ProfessionalInsurancePlan(
            professional_id=doctor.id, tenant_id=w.mine.id, custom_name="Plano", is_active=False
        )
        session.add(own)
        await session.commit()
    async with w.db() as session:
        professional = await session.get(Professional, doctor.id)
        await set_professional_independent_plans(session, professional, [str(UNIMED)])
        await session.commit()
    kept = await _row(w.db, ProfessionalInsurancePlan, own.id)
    assert (kept.custom_name, kept.is_active) == ("Plano", False)


# ------------------------------------------------- an existing appointment keeps working


async def test_an_existing_appointment_with_an_off_convenio_can_still_be_edited(db):
    world = await seed_world(db)
    async with db() as session:
        plan = TenantInsurancePlan(tenant_id=world.tenant.id, custom_name="GEAP")
        session.add(plan)
        await session.flush()
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.insurance = "GEAP"
        appointment.insurance_plan_id = plan.id
        plan.is_active = False  # the clinic switched it OFF after the booking
        await session.commit()

    async def _edit(**overrides):
        async with db() as session:
            row = await session.get(Appointment, world.appointment.id)
            new = world.start_at + timedelta(hours=1)
            edit = {
                "appointment_type": "Consulta",
                "professional_id": row.professional_id,
                "insurance": "GEAP",
                "attendee_name": None,
                "google_event_id": row.google_event_id,
                "google_event_link": None,
                "start_at": new,
                "end_at": new + timedelta(minutes=30),
                "time_changed": True,
                "doctor_changed": False,
                **overrides,
            }
            fields = await writer.write_appointment_edit(
                session,
                row,
                tenant_id=world.tenant.id,
                timezone="America/Sao_Paulo",
                edit=edit,
                source="hub",
                idempotency_key="c7-edit",
            )
            await session.commit()
            return fields, row

    # Rescheduling keeps the convênio AND its plan row; the agenda still names it.
    fields, row = await _edit()
    assert fields == ["horário"]
    assert (row.insurance, row.insurance_plan_id) == ("GEAP", plan.id)
    async with db() as session:
        lookup = await load_appointment_plans(
            session, world.tenant.id, tenant_plan_ids=[plan.id], professional_plan_ids=[]
        )
    assert lookup.resolve(plan.id, None).name == "GEAP"

    # Choosing it AGAIN after switching to another convênio is a new choice: no plan id.
    _fields, particular = await _edit(insurance="Particular", time_changed=False)
    assert particular.insurance_plan_id is None
    _fields, again = await _edit(insurance="GEAP", time_changed=False)
    assert again.insurance_plan_id is None


async def test_the_flow_snapshot_built_by_the_worker_skips_the_off_line(hub):
    """The worker's own snapshot builder (greeting._flow_tenant_snapshot) over the same read."""
    from secretaria.workers.shared.greeting import _flow_tenant_snapshot

    w = hub
    async with w.db() as session:
        (await session.get(TenantInsurancePlan, w.geap.id)).is_active = False
        await session.commit()
    async with w.db() as session:
        tenant = await session.get(Tenant, w.mine.id)
        insurance = await load_tenant_insurance(session, w.mine.id)
    snapshot = _flow_tenant_snapshot(tenant, [], [], insurance)
    assert [p["name"] for p in snapshot.insurance_plans] == ["Unimed"]


async def test_payment_rules_for_skips_off_lines_directly(hub):
    w = hub
    async with w.db() as session:
        (await session.get(TenantInsurancePlan, w.geap.id)).is_active = False
        await session.commit()
    async with w.db() as session:
        tenant = await session.get(Tenant, w.mine.id)
        assert await ipn.payment_rules_for(session, tenant, "GEAP") == []
    rows = await _all_rows(w.db)
    assert rows["GEAP"] is False


async def _all_rows(db) -> dict[str, bool]:
    async with db() as session:
        return {
            plan.custom_name: plan.is_active
            for plan in await session.scalars(
                select(TenantInsurancePlan).where(TenantInsurancePlan.custom_name.is_not(None))
            )
        }
