# ruff: noqa: F811
"""The clinic's ONE convênio order in the per-doctor mode (TASK-090 SPEC §4.6, contract C3).

`PUT /tenants/me/professionals/{id}/insurance-acceptance/plan-order` writes a clinic-wide order
(catalog plans share a place across doctors, each "Outro" has its own); the doctor matrix's columns
and the patient's list (`load_tenant_insurance` -> `_load_independent_insurance`) follow it for
every doctor. Reordering the subset one doctor sees never erases the places of the plans he does
not see. Real in-memory SQLite; the hub token is faked like tests/test_hub_insurance_acceptance_api.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from pathlib import Path  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest_asyncio  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.operations import Operations  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from secretaria.api.hub import deps as hub_deps  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import InsurancePlanOrder, Professional, Tenant  # noqa: E402
from secretaria.models.insurance import (  # noqa: E402
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    ensure_catalog_seeded,
    load_tenant_insurance,
)
from secretaria.services.insurance_plan_order import (  # noqa: E402
    apply_subset_order,
    plan_key_for_catalog,
    plan_key_for_custom,
)
from tests._local_agenda import seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

HEADERS = {"authorization": "Bearer plan-order-test-token"}
ROOT = Path(__file__).resolve().parents[1]
REVISION = "a3c9e5d7f1b4"


# ---------------------------------------------------------------- pure algorithm


def test_apply_subset_order_preserves_others():
    assert apply_subset_order(["A", "B", "C", "D"], ["D", "B"]) == ["A", "D", "C", "B"]


def test_apply_subset_order_appends_unplaced():
    # Never-placed keys join the end, then the subset's slots take its new order.
    assert apply_subset_order(["A", "B"], ["C", "A"]) == ["C", "B", "A"]
    assert apply_subset_order([], ["B", "A"]) == ["B", "A"]
    assert apply_subset_order(["A", "B"], ["C", "D"]) == ["A", "B", "C", "D"]
    # Duplicates in the request count once; an empty request changes nothing.
    assert apply_subset_order(["A", "B", "C"], ["C", "C", "A"]) == ["C", "B", "A"]
    assert apply_subset_order(["A", "B"], []) == ["A", "B"]


# ---------------------------------------------------------------- fixtures


async def _doctor(session, tenant_id, name: str) -> Professional:
    doctor = Professional(id=uuid4(), tenant_id=tenant_id, name=name, is_active=True)
    session.add(doctor)
    await session.flush()
    return doctor


def _catalog_plan(tenant_id, doctor, slug: str) -> ProfessionalInsurancePlan:
    return ProfessionalInsurancePlan(
        id=uuid4(),
        tenant_id=tenant_id,
        professional_id=doctor.id,
        catalog_id=catalog_id_for_slug(slug),
    )


def _custom_plan(tenant_id, doctor, name: str) -> ProfessionalInsurancePlan:
    return ProfessionalInsurancePlan(
        id=uuid4(), tenant_id=tenant_id, professional_id=doctor.id, custom_name=name
    )


async def _build_clinic(db, name: str, mode: str | None) -> dict:
    """Ana takes Amil, Bradesco Saúde, Unimed + her own "GEAP"; Beto takes Unimed, SulAmérica
    + his own "GEAP" (two "Outro" with the same name, never deduplicated)."""
    tenant = await seed_tenant(db, name=name)
    async with db() as session:
        await ensure_catalog_seeded(session)
        (await session.get(Tenant, tenant.id)).insurance_mode = mode
        ana = await _doctor(session, tenant.id, "Dra. Ana")
        beto = await _doctor(session, tenant.id, "Dr. Beto")
        ana_plans = {
            "amil": _catalog_plan(tenant.id, ana, "amil"),
            "bradesco": _catalog_plan(tenant.id, ana, "bradesco-saude"),
            "unimed": _catalog_plan(tenant.id, ana, "unimed"),
            "geap": _custom_plan(tenant.id, ana, "GEAP"),
        }
        beto_plans = {
            "unimed": _catalog_plan(tenant.id, beto, "unimed"),
            "sul": _catalog_plan(tenant.id, beto, "sulamerica"),
            "geap": _custom_plan(tenant.id, beto, "GEAP"),
        }
        session.add_all([*ana_plans.values(), *beto_plans.values()])
        await session.commit()
    return {"tenant": tenant, "ana": ana, "beto": beto, "ap": ana_plans, "bp": beto_plans}


@pytest_asyncio.fixture
async def world(db, monkeypatch):
    from secretaria.main import app

    a = await _build_clinic(db, "Clinica A", "independent")
    b = await _build_clinic(db, "Clinica B", "independent")
    claim = {"tenant_id": a["tenant"].id}

    async def _session():
        async with db() as session:
            yield session

    async def _verify(token, *, fresh=False):
        return SubscriptionClaim(
            tenant_id=claim["tenant_id"],
            active=True,
            role="manager",
            is_owner=False,
            is_manager=True,
            professional_id=None,
        )

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    yield {"db": db, "a": a, "b": b, "claim": claim}
    app.dependency_overrides.pop(get_session, None)


def _order_url(doctor) -> str:
    return f"/tenants/me/professionals/{doctor.id}/insurance-acceptance/plan-order"


def _matrix_url(doctor) -> str:
    return f"/tenants/me/professionals/{doctor.id}/insurance-acceptance"


async def _put_order(client, doctor, plans):
    return await client.put(
        _order_url(doctor), json={"plan_ids": [str(p.id) for p in plans]}, headers=HEADERS
    )


async def _matrix_names(client, doctor) -> list[str]:
    resp = await client.get(_matrix_url(doctor), headers=HEADERS)
    assert resp.status_code == 200, resp.text
    return [p["name"] for p in resp.json()["plans"]]


async def _patient_names(db, tenant, doctor=None) -> list[str]:
    """The patient's convênio list (all doctors, or the ones `doctor` accepts), in order."""
    async with db() as session:
        insurance = await load_tenant_insurance(session, tenant.id)
    if doctor is None:
        return [p["name"] for p in insurance.plans]
    accepted = insurance.accepted_by[str(doctor.id)]
    return [p["name"] for p in insurance.plans if p["id"] in accepted]


async def _stored(db, tenant) -> dict[str, int]:
    async with db() as session:
        rows = await session.execute(
            select(InsurancePlanOrder.plan_key, InsurancePlanOrder.position).where(
                InsurancePlanOrder.tenant_id == tenant.id
            )
        )
        return dict(rows.all())


async def _order_rows(db) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(InsurancePlanOrder))


# ---------------------------------------------------------------- matrix + patient list


async def test_professional_matrix_columns_follow_clinic_order(client, world):
    a = world["a"]
    ap = a["ap"]
    # Before any order: alphabetical, exactly as before TASK-090.
    assert await _matrix_names(client, a["ana"]) == ["Amil", "Bradesco Saúde", "GEAP", "Unimed"]

    new_order = [ap["geap"], ap["unimed"], ap["amil"], ap["bradesco"]]
    resp = await _put_order(client, a["ana"], new_order)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "professional" and body["professional_id"] == str(a["ana"].id)
    expected = ["GEAP", "Unimed", "Amil", "Bradesco Saúde"]
    assert [p["name"] for p in body["plans"]] == expected
    assert [p["id"] for p in body["plans"]] == [
        str(ap[k].id) for k in ("geap", "unimed", "amil", "bradesco")
    ]
    # Persisted: a fresh GET returns the same columns.
    assert await _matrix_names(client, a["ana"]) == expected


async def test_order_saved_in_doctor_a_shows_in_doctor_b(client, world):
    a = world["a"]
    db = world["db"]
    # Ana puts Unimed before Amil (alphabetical would be the other way round).
    resp = await _put_order(client, a["ana"], [a["ap"]["unimed"], a["ap"]["amil"]])
    assert resp.status_code == 200, resp.text
    # Beto shares Unimed (catalog plan): it comes before his never-placed plans.
    assert await _matrix_names(client, a["beto"]) == ["Unimed", "GEAP", "SulAmérica"]
    # The patient's list, for every doctor, follows the same clinic order.
    assert await _patient_names(db, a["tenant"]) == [
        "Unimed",
        "Amil",
        "Bradesco Saúde",
        "GEAP",
        "GEAP",
        "SulAmérica",
    ]
    assert await _patient_names(db, a["tenant"], a["ana"]) == [
        "Unimed",
        "Amil",
        "Bradesco Saúde",
        "GEAP",
    ]
    assert await _patient_names(db, a["tenant"], a["beto"]) == ["Unimed", "GEAP", "SulAmérica"]


async def test_reorder_subset_keeps_unseen_plans_positions(client, world):
    a = world["a"]
    ap, bp = a["ap"], a["bp"]
    resp = await _put_order(client, a["ana"], [ap["unimed"], ap["bradesco"], ap["amil"]])
    assert resp.status_code == 200, resp.text
    unimed = plan_key_for_catalog(catalog_id_for_slug("unimed"))
    amil = plan_key_for_catalog(catalog_id_for_slug("amil"))
    bradesco = plan_key_for_catalog(catalog_id_for_slug("bradesco-saude"))
    sul = plan_key_for_catalog(catalog_id_for_slug("sulamerica"))
    assert await _stored(world["db"], a["tenant"]) == {unimed: 0, bradesco: 1, amil: 2}

    # Beto sees only Unimed + SulAmérica (+ his GEAP): he puts SulAmérica first.
    resp = await _put_order(client, a["beto"], [bp["sul"], bp["unimed"]])
    assert resp.status_code == 200, resp.text
    assert [p["name"] for p in resp.json()["plans"]] == ["SulAmérica", "Unimed", "GEAP"]
    # Bradesco and Amil (which Beto does not see) keep their places.
    assert await _stored(world["db"], a["tenant"]) == {sul: 0, bradesco: 1, amil: 2, unimed: 3}
    assert await _matrix_names(client, a["ana"]) == ["Bradesco Saúde", "Amil", "Unimed", "GEAP"]


async def test_unplaced_plans_alphabetical_after_placed(client, world):
    a = world["a"]
    resp = await _put_order(client, a["ana"], [a["ap"]["unimed"]])
    assert resp.status_code == 200, resp.text
    assert [p["name"] for p in resp.json()["plans"]] == [
        "Unimed",
        "Amil",
        "Bradesco Saúde",
        "GEAP",
    ]
    assert await _patient_names(world["db"], a["tenant"], a["ana"]) == [
        "Unimed",
        "Amil",
        "Bradesco Saúde",
        "GEAP",
    ]


async def test_custom_outro_of_two_doctors_ordered_independently(client, world):
    a = world["a"]
    resp = await _put_order(client, a["ana"], [a["ap"]["geap"], a["ap"]["amil"]])
    assert resp.status_code == 200, resp.text
    # Only Ana's "Outro" was placed; Beto's same-named one is a different plan (its own row).
    stored = await _stored(world["db"], a["tenant"])
    assert stored == {
        plan_key_for_custom(a["ap"]["geap"].id): 0,
        plan_key_for_catalog(catalog_id_for_slug("amil")): 1,
    }
    assert await _matrix_names(client, a["beto"]) == ["GEAP", "SulAmérica", "Unimed"]
    async with world["db"]() as session:
        plans = (await load_tenant_insurance(session, a["tenant"].id)).plans
    ids = [p["id"] for p in plans]
    # Ana's GEAP is first; Beto's GEAP follows the placed plans, alphabetically.
    assert ids[0] == str(a["ap"]["geap"].id)
    assert [p["name"] for p in plans] == [
        "GEAP",
        "Amil",
        "Bradesco Saúde",
        "GEAP",
        "SulAmérica",
        "Unimed",
    ]
    assert ids[3] == str(a["bp"]["geap"].id)

    # Beto now places his own GEAP last of his list: Ana's GEAP does not move.
    resp = await _put_order(client, a["beto"], [a["bp"]["unimed"], a["bp"]["geap"]])
    assert resp.status_code == 200, resp.text
    async with world["db"]() as session:
        plans = (await load_tenant_insurance(session, a["tenant"].id)).plans
    assert plans[0]["id"] == str(a["ap"]["geap"].id)


async def test_put_order_404_foreign_professional_and_unknown_plan(client, world):
    a, b = world["a"], world["b"]
    # Another clinic's doctor: 404, never touched.
    resp = await _put_order(client, b["ana"], [b["ap"]["unimed"]])
    assert resp.status_code == 404 and resp.json()["detail"] == "professional_not_found"
    resp = await client.put(
        "/tenants/me/professionals/not-a-uuid/insurance-acceptance/plan-order",
        json={"plan_ids": []},
        headers=HEADERS,
    )
    assert resp.status_code == 404 and resp.json()["detail"] == "professional_not_found"
    # A plan of another doctor (same clinic), of another clinic, or unknown: 404, nothing written.
    for foreign in (a["bp"]["sul"], b["ap"]["amil"]):
        resp = await _put_order(client, a["ana"], [a["ap"]["unimed"], foreign])
        assert resp.status_code == 404 and resp.json()["detail"] == "plan_not_found"
    resp = await client.put(
        _order_url(a["ana"]), json={"plan_ids": [str(uuid4())]}, headers=HEADERS
    )
    assert resp.status_code == 404 and resp.json()["detail"] == "plan_not_found"
    # An id that is not even a UUID is just an unknown plan (contract C3), never a 422.
    for junk in ("not-a-uuid", "", "123"):
        resp = await client.put(
            _order_url(a["ana"]),
            json={"plan_ids": [str(a["ap"]["unimed"].id), junk]},
            headers=HEADERS,
        )
        assert resp.status_code == 404 and resp.json()["detail"] == "plan_not_found", junk
    assert await _order_rows(world["db"]) == 0

    # Tenant isolation: clinic A's order never reaches clinic B (same catalog plans).
    resp = await _put_order(client, a["ana"], [a["ap"]["unimed"], a["ap"]["amil"]])
    assert resp.status_code == 200, resp.text
    assert await _stored(world["db"], b["tenant"]) == {}
    assert await _patient_names(world["db"], b["tenant"], b["ana"]) == [
        "Amil",
        "Bradesco Saúde",
        "GEAP",
        "Unimed",
    ]


async def test_put_order_409_when_mode_not_independent(client, world):
    a = world["a"]
    for mode in ("shared", "clinic_with_exceptions", None):
        async with world["db"]() as session:
            (await session.get(Tenant, a["tenant"].id)).insurance_mode = mode
            await session.commit()
        resp = await _put_order(client, a["ana"], [a["ap"]["unimed"]])
        assert resp.status_code == 409, (mode, resp.text)
        assert resp.json()["detail"]["code"] == "insurance_mode_not_applicable"
    assert await _order_rows(world["db"]) == 0


async def test_put_order_rejects_extra_fields_and_empty_list_is_noop(client, world):
    a = world["a"]
    resp = await client.put(
        _order_url(a["ana"]), json={"plan_ids": [], "tenant_id": "x"}, headers=HEADERS
    )
    assert resp.status_code == 422
    resp = await client.put(_order_url(a["ana"]), json={"plan_ids": []}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    assert [p["name"] for p in resp.json()["plans"]] == ["Amil", "Bradesco Saúde", "GEAP", "Unimed"]
    assert await _order_rows(world["db"]) == 0


# ---------------------------------------------------------------- clinic-scope path unchanged


async def test_shared_mode_list_still_follows_tenant_sort_order(client, world):
    """Modes shared/clinic_with_exceptions: the existing PUT /insurance-plans/order still drives
    the patient's list, and the per-doctor-mode order table never applies to them."""
    a = world["a"]
    db = world["db"]
    resp = await _put_order(client, a["ana"], [a["ap"]["amil"], a["ap"]["unimed"]])
    assert resp.status_code == 200, resp.text
    async with db() as session:
        (await session.get(Tenant, a["tenant"].id)).insurance_mode = "shared"
        tplans = [
            TenantInsurancePlan(
                id=uuid4(), tenant_id=a["tenant"].id, catalog_id=catalog_id_for_slug(slug)
            )
            for slug in ("amil", "unimed", "omint")
        ]
        session.add_all(tplans)
        await session.commit()
    assert await _patient_names(db, a["tenant"]) == ["Amil", "Omint", "Unimed"]
    resp = await client.put(
        "/tenants/me/insurance-plans/order",
        json={"plan_ids": [str(tplans[1].id), str(tplans[2].id)]},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert [p["name"] for p in resp.json()] == ["Unimed", "Omint", "Amil"]
    for mode in ("shared", "clinic_with_exceptions"):
        async with db() as session:
            (await session.get(Tenant, a["tenant"].id)).insurance_mode = mode
            await session.commit()
        assert await _patient_names(db, a["tenant"]) == ["Unimed", "Omint", "Amil"]


# ---------------------------------------------------------------- migration


def test_migration_creates_and_drops_the_order_table(tmp_path):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    migration = scripts.get_revision(REVISION).module

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'order.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE tenants (id CHAR(32) PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            inspector = sa.inspect(conn)
            columns = {col["name"]: col for col in inspector.get_columns("insurance_plan_order")}
            assert set(columns) == {"tenant_id", "plan_key", "position"}
            assert columns["position"]["nullable"] is False
            assert inspector.get_pk_constraint("insurance_plan_order")["constrained_columns"] == [
                "tenant_id",
                "plan_key",
            ]
            fks = inspector.get_foreign_keys("insurance_plan_order")
            assert fks[0]["referred_table"] == "tenants"
            assert fks[0]["options"].get("ondelete") == "CASCADE"
            migration.downgrade()
        assert "insurance_plan_order" not in sa.inspect(conn).get_table_names()
