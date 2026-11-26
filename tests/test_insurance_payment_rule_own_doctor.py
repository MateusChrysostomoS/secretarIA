"""TASK-090 Adendo 1, contract C8 - who writes a DOCTOR's payment rule (owner, 2026-11-24).

In the per-doctor mode (`independent`) a doctor's payment rule may be written by the clinic
manager (any doctor) OR by that doctor himself - the `doctor` user whose linked professional
(`claim.professional_id`, from brain-api) is the path's - and ONLY his own. Secretaries, other
doctors and a doctor with no linked professional: 403 (decided before any lookup). Kept: 404
cross-tenant, 409 outside `independent`, 422 above the limit. The clinic-level rule
(`PUT /tenants/me/insurance-plans/{plan_id}/payment-note`) stays manager-only.
Same gate for a professional custom POST that carries a rule; without a rule it is unchanged.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from secretaria.api.hub import deps as hub_deps  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.models.insurance import (  # noqa: E402
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    ensure_catalog_seeded,
)
from tests.payments_support import make_sessionmaker  # noqa: E402

HEADERS = {"authorization": "Bearer c8-test-token"}
UNIMED = catalog_id_for_slug("unimed")
RULE = "Reembolso em até 30 dias."


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def world(db, monkeypatch):
    """Clinic "Mine" (independent): Dra. Ana and Dr. Beto, each accepting Unimed and each with
    an own "Outro". Clinic "Other" has Dr. Zé. The claim is editable per test."""
    from secretaria.main import app

    async with db() as session:
        mine = Tenant(
            id=uuid4(), clinic_name="Mine", phone_number_id=None, insurance_mode="independent"
        )
        other = Tenant(
            id=uuid4(), clinic_name="Other", phone_number_id=None, insurance_mode="independent"
        )
        session.add_all([mine, other])
        await session.flush()
        ana = Professional(id=uuid4(), tenant_id=mine.id, name="Dra. Ana", is_active=True)
        beto = Professional(id=uuid4(), tenant_id=mine.id, name="Dr. Beto", is_active=True)
        ze = Professional(id=uuid4(), tenant_id=other.id, name="Dr. Zé", is_active=True)
        session.add_all([ana, beto, ze])
        await session.flush()
        rows = {}
        for doctor in (ana, beto, ze):
            session.add(
                ProfessionalInsurancePlan(
                    professional_id=doctor.id, tenant_id=doctor.tenant_id, catalog_id=UNIMED
                )
            )
            own = ProfessionalInsurancePlan(
                professional_id=doctor.id, tenant_id=doctor.tenant_id, custom_name="Próprio"
            )
            session.add(own)
            rows[doctor.id] = own
        clinic_line = TenantInsurancePlan(tenant_id=mine.id, catalog_id=UNIMED)
        session.add(clinic_line)
        await session.commit()

    claim = {
        "role": "doctor",
        "is_owner": False,
        "is_manager": False,
        "tenant_id": mine.id,
        "professional_id": ana.id,
    }

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
            professional_id=claim["professional_id"],
        )

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    yield SimpleNamespace(
        db=db,
        mine=mine,
        other=other,
        ana=ana,
        beto=beto,
        ze=ze,
        own=rows,
        clinic_line=clinic_line,
        claim=claim,
    )
    app.dependency_overrides.pop(get_session, None)


def _note_url(doctor_id, plan_id) -> str:
    return f"/tenants/me/professionals/{doctor_id}/insurance-plans/{plan_id}/payment-note"


def _custom_url(doctor_id) -> str:
    return f"/tenants/me/professionals/{doctor_id}/insurance-plans/custom"


async def _note_of(db, model, row_id):
    async with db() as session:
        return (await session.get(model, row_id)).custom_payment_note


async def _put(client, doctor_id, plan_id, note=RULE):
    return await client.put(
        _note_url(doctor_id, plan_id), json={"custom_payment_note": note}, headers=HEADERS
    )


# ------------------------------------------------------------------------------ the PUT


async def test_the_doctor_writes_his_own_rule_on_a_catalog_pick_and_on_his_outro(client, world):
    w = world
    catalog = await _put(client, w.ana.id, UNIMED)
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["custom_payment_note"] == RULE
    own = await _put(client, w.ana.id, w.own[w.ana.id].id, "Só reembolso.")
    assert own.status_code == 200 and own.json()["custom_payment_note"] == "Só reembolso."
    cleared = await _put(client, w.ana.id, w.own[w.ana.id].id, None)
    assert cleared.status_code == 200 and cleared.json()["custom_payment_note"] is None


async def test_a_manager_writes_any_doctors_rule(client, world):
    w = world
    w.claim.update(role="manager", professional_id=None)
    for doctor in (w.ana, w.beto):
        resp = await _put(client, doctor.id, w.own[doctor.id].id)
        assert resp.status_code == 200, resp.text
        assert await _note_of(w.db, ProfessionalInsurancePlan, w.own[doctor.id].id) == RULE
    # A doctor flagged manager is a manager too (any doctor).
    w.claim.update(role="doctor", is_manager=True, professional_id=w.ana.id)
    assert (await _put(client, w.beto.id, UNIMED)).status_code == 200


@pytest.mark.parametrize(
    "who",
    ["other_doctor", "secretary", "secretary_linked", "doctor_without_link", "no_role"],
)
async def test_everyone_else_is_403_before_any_lookup(client, world, who):
    w = world
    claim = {
        "other_doctor": {"role": "doctor", "professional_id": w.beto.id},
        "secretary": {"role": "secretary", "professional_id": None},
        # A secretary whose account happens to carry the professional id is still refused.
        "secretary_linked": {"role": "secretary", "professional_id": w.ana.id},
        "doctor_without_link": {"role": "doctor", "professional_id": None},
        "no_role": {"role": None, "professional_id": w.ana.id},
    }[who]
    w.claim.update(**claim)
    resp = await _put(client, w.ana.id, w.own[w.ana.id].id)
    assert resp.status_code == 403
    code = resp.json()["detail"]["code"]
    assert code == ("role_claim_missing" if who == "no_role" else "forbidden_role")
    # 403 also for an id that does not exist (no probing) and for a malformed one.
    if who != "no_role":
        assert (await _put(client, uuid4(), UNIMED)).status_code == 403
        assert (await _put(client, "not-a-uuid", UNIMED)).status_code == 403
    assert await _note_of(w.db, ProfessionalInsurancePlan, w.own[w.ana.id].id) is None


async def test_own_doctor_still_gets_404_and_409_and_422(client, world):
    w = world
    # A line he does not have: 404.
    assert (await _put(client, w.ana.id, uuid4())).status_code == 404
    # Above the limit: 422.
    too_long = await _put(client, w.ana.id, UNIMED, "a" * 1001)
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["code"] == "payment_note_too_long"
    # His own professional in shared mode: 409, as today.
    async with w.db() as session:
        (await session.get(Tenant, w.mine.id)).insurance_mode = "shared"
        await session.commit()
    wrong_mode = await _put(client, w.ana.id, UNIMED)
    assert wrong_mode.status_code == 409
    assert wrong_mode.json()["detail"]["code"] == "insurance_mode_not_applicable"


async def test_another_clinics_doctor_is_404_for_a_manager(client, world):
    w = world
    w.claim.update(role="manager", professional_id=None)
    assert (await _put(client, w.ze.id, UNIMED)).status_code == 404
    assert (await _put(client, "not-a-uuid", UNIMED)).status_code == 404
    assert await _note_of(w.db, ProfessionalInsurancePlan, w.own[w.ze.id].id) is None


async def test_the_clinic_level_rule_stays_manager_only(client, world):
    w = world
    resp = await client.put(
        f"/tenants/me/insurance-plans/{w.clinic_line.id}/payment-note",
        json={"custom_payment_note": RULE},
        headers=HEADERS,
    )
    assert resp.status_code == 403
    assert await _note_of(w.db, TenantInsurancePlan, w.clinic_line.id) is None


# ------------------------------------------------------------------- the custom POST


async def test_custom_post_with_a_rule_by_the_doctor_himself(client, world):
    w = world
    resp = await client.post(
        _custom_url(w.ana.id),
        json={"custom_name": "Plano novo", "custom_payment_note": RULE},
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["custom_payment_note"] == RULE


@pytest.mark.parametrize(
    "who", ["other_doctor", "secretary", "doctor_without_link"]
)
async def test_custom_post_with_a_rule_by_anyone_else_is_403(client, world, who):
    w = world
    w.claim.update(
        **{
            "other_doctor": {"role": "doctor", "professional_id": w.beto.id},
            "secretary": {"role": "secretary", "professional_id": None},
            "doctor_without_link": {"role": "doctor", "professional_id": None},
        }[who]
    )
    with_rule = await client.post(
        _custom_url(w.ana.id),
        json={"custom_name": "Plano novo", "custom_payment_note": RULE},
        headers=HEADERS,
    )
    assert with_rule.status_code == 403
    # Without a rule a doctor's "Outro" is still his or the manager's (TASK-091 T4; junção
    # 090+091: the more restrictive rule wins).
    plain = await client.post(
        _custom_url(w.ana.id), json={"custom_name": "Plano sem regra"}, headers=HEADERS
    )
    assert plain.status_code == 403
