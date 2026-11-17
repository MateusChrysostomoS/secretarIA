# ruff: noqa: F811
"""Hub API of the convênio acceptance matrix (TASK-086 T3).

GET/PUT/copy/summary for mode 1 (`shared`, the clinic matrix) and mode 2 (`independent`, one matrix
per doctor), both per unit. Real in-memory SQLite; the hub token is introspected through the same
fake as tests/test_hub_professional_service_units.py.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from secretaria.api.hub import deps as hub_deps  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import (  # noqa: E402
    Professional,
    ProfessionalServiceUnit,
    ProfessionalUnit,
    Service,
    Tenant,
    Unit,
)
from secretaria.models.insurance import (  # noqa: E402
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.models.insurance_acceptance_exception import (  # noqa: E402
    InsuranceAcceptanceException,
)
from tests._local_agenda import seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

HEADERS = {"authorization": "Bearer matrix-test-token"}


def _service(tenant_id, name: str, order: int = 0) -> Service:
    return Service(
        id=uuid4(),
        tenant_id=tenant_id,
        name=name,
        normalized_name=name.casefold(),
        is_active=True,
        sort_order=order,
    )


async def _build_clinic(db, name: str, mode: str | None):
    """One clinic: 3 services, 2 units, doctor Ana (all services, both units), doctor Beto
    (no unit link), 2 clinic plans and 2 plans of Ana's own."""
    tenant = await seed_tenant(db, name=name)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.insurance_mode = mode
        consulta = _service(tenant.id, "Consulta", 0)
        retorno = _service(tenant.id, "Retorno", 1)
        eco = _service(tenant.id, "Ecocardiograma", 2)
        session.add_all([consulta, retorno, eco])
        await session.flush()
        entries = [
            {"name": s.name, "service_id": str(s.id), "is_active": True}
            for s in (consulta, retorno, eco)
        ]
        ana = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=entries,
        )
        beto = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dr. Beto",
            is_active=True,
            appointment_types=[entries[0]],
        )
        barra = Unit(id=uuid4(), tenant_id=tenant.id, name="Barra", address="Rua A")
        centro = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro", address="Rua B")
        session.add_all([ana, beto, barra, centro])
        await session.flush()
        hours = {"monday": [{"start": "08:00", "end": "12:00"}]}
        for i, unit in enumerate((barra, centro)):
            session.add(
                ProfessionalUnit(
                    professional_id=ana.id,
                    unit_id=unit.id,
                    is_active=True,
                    sort_order=i,
                    business_hours=hours,
                )
            )
        tplans = [
            TenantInsurancePlan(id=uuid4(), tenant_id=tenant.id, custom_name=n)
            for n in ("Bradesco", "Unimed")
        ]
        pplans = [
            ProfessionalInsurancePlan(
                id=uuid4(), tenant_id=tenant.id, professional_id=ana.id, custom_name=n
            )
            for n in ("Amil", "SulAmerica")
        ]
        session.add_all([*tplans, *pplans])
        await session.commit()
    return {
        "tenant": tenant,
        "services": {"consulta": consulta, "retorno": retorno, "eco": eco},
        "ana": ana,
        "beto": beto,
        "barra": barra,
        "centro": centro,
        "tplans": {"bradesco": tplans[0], "unimed": tplans[1]},
        "pplans": {"amil": pplans[0], "sul": pplans[1]},
    }


@pytest_asyncio.fixture
async def world(db, monkeypatch):
    from secretaria.main import app

    a = await _build_clinic(db, "Clinica A", "shared")
    b = await _build_clinic(db, "Clinica B", "shared")
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


async def _set_mode(db, tenant, mode):
    async with db() as session:
        (await session.get(Tenant, tenant.id)).insurance_mode = mode
        await session.commit()


def _cell(service, plan) -> dict:
    return {"service_id": str(service.id), "plan_id": str(plan.id)}


def _clinic_url(unit="none") -> str:
    return f"/tenants/me/insurance-acceptance/clinic/units/{unit}"


def _prof_url(prof, unit=None, tail="") -> str:
    base = f"/tenants/me/professionals/{prof.id}/insurance-acceptance"
    return f"{base}/units/{unit}" if unit is not None else base + tail


async def _count(db) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(InsuranceAcceptanceException))


def _slice(body, unit):
    key = None if unit is None else str(unit.id)
    return next(s for s in body["slices"] if s["unit_id"] == key)


# ---------------------------------------------------------------- mode 1 (clinic)


async def test_clinic_get_shape_defaults_to_no_exceptions(client, world):
    a = world["a"]
    resp = await client.get("/tenants/me/insurance-acceptance/clinic", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "shared" and body["scope"] == "clinic"
    assert body["professional_id"] is None
    assert [s["name"] for s in body["services"]] == ["Consulta", "Retorno", "Ecocardiograma"]
    assert {p["name"] for p in body["plans"]} == {"Bradesco", "Unimed"}
    assert all(p["is_custom"] for p in body["plans"])
    # Ana attends both units; Beto has no unit link -> the "no unit" slice exists too.
    unit_ids = {s["unit_id"] for s in body["slices"]}
    assert unit_ids == {str(a["barra"].id), str(a["centro"].id), None}
    assert all(s["exceptions"] == [] for s in body["slices"])
    barra = _slice(body, a["barra"])
    assert barra["unit_name"] == "Barra"
    assert set(barra["offered_service_ids"]) == {str(s.id) for s in a["services"].values()}
    none_slice = _slice(body, None)  # only Beto's service lives there
    assert none_slice["offered_service_ids"] == [str(a["services"]["consulta"].id)]
    assert body["services_without_id"] == []


async def test_clinic_put_replaces_the_slice_and_returns_the_state(client, world):
    a = world["a"]
    s, p = a["services"], a["tplans"]
    first = await client.put(
        _clinic_url(a["barra"].id),
        headers=HEADERS,
        json={"exceptions": [_cell(s["retorno"], p["unimed"]), _cell(s["eco"], p["bradesco"])]},
    )
    assert first.status_code == 200, first.text
    assert len(_slice(first.json(), a["barra"])["exceptions"]) == 2
    # Replace, not merge: the second save keeps ONLY what it sends.
    second = await client.put(
        _clinic_url(a["barra"].id),
        headers=HEADERS,
        json={"exceptions": [_cell(s["eco"], p["bradesco"])]},
    )
    assert _slice(second.json(), a["barra"])["exceptions"] == [
        {"service_id": str(s["eco"].id), "plan_id": str(p["bradesco"].id)}
    ]
    assert await _count(world["db"]) == 1
    # Another slice is untouched.
    other = await client.get("/tenants/me/insurance-acceptance/clinic", headers=HEADERS)
    assert _slice(other.json(), a["centro"])["exceptions"] == []
    # Empty list clears.
    cleared = await client.put(_clinic_url(a["barra"].id), headers=HEADERS, json={"exceptions": []})
    assert cleared.status_code == 200 and await _count(world["db"]) == 0


async def test_duplicates_are_deduplicated(client, world):
    a = world["a"]
    cell = _cell(a["services"]["eco"], a["tplans"]["unimed"])
    resp = await client.put(
        _clinic_url(a["barra"].id), headers=HEADERS, json={"exceptions": [cell, cell]}
    )
    assert resp.status_code == 200 and await _count(world["db"]) == 1


async def test_clinic_none_slice_is_writable(client, world):
    a = world["a"]
    resp = await client.put(
        _clinic_url("none"),
        headers=HEADERS,
        json={"exceptions": [_cell(a["services"]["consulta"], a["tplans"]["unimed"])]},
    )
    assert resp.status_code == 200, resp.text
    assert len(_slice(resp.json(), None)["exceptions"]) == 1
    async with world["db"]() as session:
        row = (await session.scalars(select(InsuranceAcceptanceException))).one()
    assert row.unit_id is None and row.professional_id is None
    assert row.tenant_plan_id == a["tplans"]["unimed"].id and row.professional_plan_id is None


async def test_put_rejects_unknown_service_and_plan_without_writing(client, world):
    a = world["a"]
    good = _cell(a["services"]["eco"], a["tplans"]["unimed"])
    bad_service = await client.put(
        _clinic_url(a["barra"].id),
        headers=HEADERS,
        json={"exceptions": [good, {"service_id": str(uuid4()), "plan_id": good["plan_id"]}]},
    )
    assert bad_service.status_code == 422
    assert bad_service.json()["detail"]["code"] == "unknown_service_ids"
    bad_plan = await client.put(
        _clinic_url(a["barra"].id),
        headers=HEADERS,
        json={"exceptions": [good, {"service_id": good["service_id"], "plan_id": "not-a-uuid"}]},
    )
    assert bad_plan.status_code == 422
    assert bad_plan.json()["detail"]["code"] == "unknown_plan_ids"
    assert await _count(world["db"]) == 0


async def test_put_rejects_an_unknown_unit_and_a_slice_that_is_not_available(client, world):
    a = world["a"]
    body = {"exceptions": []}
    missing = await client.put(_clinic_url(uuid4()), headers=HEADERS, json=body)
    assert missing.status_code == 404 and missing.json()["detail"] == "unit_not_found"
    garbage = await client.put(_clinic_url("zzz"), headers=HEADERS, json=body)
    assert garbage.status_code == 404
    async with world["db"]() as session:
        (await session.get(Unit, a["centro"].id)).is_active = False
        await session.commit()
    inactive = await client.put(_clinic_url(a["centro"].id), headers=HEADERS, json=body)
    assert inactive.status_code == 404  # deactivated = not a slice (same as the units API)


async def test_put_rejects_extra_fields_and_oversized_body(client, world):
    a = world["a"]
    extra = await client.put(
        _clinic_url(a["barra"].id), headers=HEADERS, json={"exceptions": [], "tenant_id": "x"}
    )
    assert extra.status_code == 422
    cell = _cell(a["services"]["eco"], a["tplans"]["unimed"])
    cell["professional_id"] = str(a["ana"].id)
    cell_extra = await client.put(
        _clinic_url(a["barra"].id), headers=HEADERS, json={"exceptions": [cell]}
    )
    assert cell_extra.status_code == 422
    huge = await client.put(
        _clinic_url(a["barra"].id),
        headers=HEADERS,
        json={"exceptions": [{"service_id": "a", "plan_id": "b"}] * 7201},
    )
    assert huge.status_code == 422


async def test_clinic_routes_409_outside_shared_mode(client, world):
    a = world["a"]
    for mode in ("independent", "clinic_with_exceptions", None):
        await _set_mode(world["db"], a["tenant"], mode)
        for resp in (
            await client.get("/tenants/me/insurance-acceptance/clinic", headers=HEADERS),
            await client.put(_clinic_url(a["barra"].id), headers=HEADERS, json={"exceptions": []}),
            await client.post(
                "/tenants/me/insurance-acceptance/clinic/copy",
                headers=HEADERS,
                json={"from_unit_id": str(a["barra"].id), "to_unit_ids": [str(a["centro"].id)]},
            ),
        ):
            assert resp.status_code == 409, (mode, resp.text)
            assert resp.json()["detail"]["code"] == "insurance_matrix_not_in_mode"
            assert resp.json()["detail"]["mode"] == mode
    assert await _count(world["db"]) == 0


# ---------------------------------------------------------------- mode 2 (doctor)


async def test_professional_get_put_and_isolation_from_the_clinic_matrix(client, world):
    a = world["a"]
    await _set_mode(world["db"], a["tenant"], "independent")
    s, p = a["services"], a["pplans"]
    got = await client.get(_prof_url(a["ana"]), headers=HEADERS)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["mode"] == "independent" and body["scope"] == "professional"
    assert body["professional_id"] == str(a["ana"].id)
    assert {x["name"] for x in body["plans"]} == {"Amil", "SulAmerica"}
    assert {x["id"] for x in body["plans"]} == {str(x.id) for x in p.values()}
    assert {sl["unit_id"] for sl in body["slices"]} == {str(a["barra"].id), str(a["centro"].id)}
    put = await client.put(
        _prof_url(a["ana"], a["barra"].id),
        headers=HEADERS,
        json={"exceptions": [_cell(s["eco"], p["amil"])]},
    )
    assert put.status_code == 200, put.text
    assert len(_slice(put.json(), a["barra"])["exceptions"]) == 1
    async with world["db"]() as session:
        row = (await session.scalars(select(InsuranceAcceptanceException))).one()
    assert row.professional_id == a["ana"].id
    assert row.professional_plan_id == p["amil"].id and row.tenant_plan_id is None
    # Clinic-matrix routes are closed in this mode.
    clinic = await client.get("/tenants/me/insurance-acceptance/clinic", headers=HEADERS)
    assert clinic.status_code == 409


async def test_professional_routes_409_outside_independent_mode(client, world):
    a = world["a"]  # fixture mode = shared
    barra, centro = str(a["barra"].id), str(a["centro"].id)
    for resp in (
        await client.get(_prof_url(a["ana"]), headers=HEADERS),
        await client.put(
            _prof_url(a["ana"], a["barra"].id), headers=HEADERS, json={"exceptions": []}
        ),
        await client.post(
            _prof_url(a["ana"], tail="/copy"),
            headers=HEADERS,
            json={"from_unit_id": barra, "to_unit_ids": [centro]},
        ),
    ):
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "insurance_matrix_not_in_mode"
        assert resp.json()["detail"]["mode"] == "shared"


async def test_professional_without_units_has_only_the_none_slice(client, world):
    a = world["a"]
    await _set_mode(world["db"], a["tenant"], "independent")
    async with world["db"]() as session:
        session.add(
            ProfessionalInsurancePlan(
                tenant_id=a["tenant"].id, professional_id=a["beto"].id, custom_name="Porto"
            )
        )
        await session.commit()
    body = (await client.get(_prof_url(a["beto"]), headers=HEADERS)).json()
    assert [sl["unit_id"] for sl in body["slices"]] == [None]
    assert [x["name"] for x in body["plans"]] == ["Porto"]
    # An Ana unit is NOT a slice of Beto (he does not attend there): 422, nothing written.
    resp = await client.put(
        _prof_url(a["beto"], a["barra"].id), headers=HEADERS, json={"exceptions": []}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unit_slice_not_available"
    ok = await client.put(_prof_url(a["beto"], "none"), headers=HEADERS, json={"exceptions": []})
    assert ok.status_code == 200


async def test_professional_none_slice_unavailable_when_she_attends_units(client, world):
    a = world["a"]
    await _set_mode(world["db"], a["tenant"], "independent")
    resp = await client.put(_prof_url(a["ana"], "none"), headers=HEADERS, json={"exceptions": []})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unit_slice_not_available"


async def test_service_marks_drive_offered_service_ids(client, world):
    a = world["a"]
    await _set_mode(world["db"], a["tenant"], "independent")
    async with world["db"]() as session:  # Ecocardiograma only at Barra
        session.add(
            ProfessionalServiceUnit(
                professional_id=a["ana"].id,
                service_id=a["services"]["eco"].id,
                unit_id=a["barra"].id,
            )
        )
        await session.commit()
    body = (await client.get(_prof_url(a["ana"]), headers=HEADERS)).json()
    eco = str(a["services"]["eco"].id)
    assert eco in _slice(body, a["barra"])["offered_service_ids"]
    assert eco not in _slice(body, a["centro"])["offered_service_ids"]


async def test_entries_without_service_id_are_listed_not_editable(client, world):
    a = world["a"]
    await _set_mode(world["db"], a["tenant"], "independent")
    async with world["db"]() as session:
        ana = await session.get(Professional, a["ana"].id)
        ana.appointment_types = [
            *ana.appointment_types,
            {"name": "Check-up livre", "is_active": True},
        ]
        await session.commit()
    body = (await client.get(_prof_url(a["ana"]), headers=HEADERS)).json()
    assert body["services_without_id"] == ["Check-up livre"]
    assert len(body["services"]) == 3


async def test_professional_not_found_and_other_tenant_professional(client, world):
    a, b = world["a"], world["b"]
    await _set_mode(world["db"], a["tenant"], "independent")
    for pid in (uuid4(), "garbage", b["ana"].id):
        resp = await client.get(
            f"/tenants/me/professionals/{pid}/insurance-acceptance", headers=HEADERS
        )
        assert resp.status_code == 404, resp.text


async def test_professional_plan_of_another_doctor_is_refused(client, world):
    a = world["a"]
    await _set_mode(world["db"], a["tenant"], "independent")
    async with world["db"]() as session:
        other = ProfessionalInsurancePlan(
            id=uuid4(), tenant_id=a["tenant"].id, professional_id=a["beto"].id, custom_name="Porto"
        )
        session.add(other)
        await session.commit()
    resp = await client.put(
        _prof_url(a["ana"], a["barra"].id),
        headers=HEADERS,
        json={"exceptions": [_cell(a["services"]["eco"], other)]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_plan_ids"


# ---------------------------------------------------------------- tenant isolation (A x B)


async def test_other_tenants_ids_are_refused_everywhere(client, world):
    a, b = world["a"], world["b"]
    cell_b_plan = {
        "service_id": str(a["services"]["eco"].id),
        "plan_id": str(b["tplans"]["unimed"].id),
    }
    cell_b_service = {
        "service_id": str(b["services"]["eco"].id),
        "plan_id": str(a["tplans"]["unimed"].id),
    }
    r1 = await client.put(
        _clinic_url(a["barra"].id), headers=HEADERS, json={"exceptions": [cell_b_plan]}
    )
    r2 = await client.put(
        _clinic_url(a["barra"].id), headers=HEADERS, json={"exceptions": [cell_b_service]}
    )
    assert (r1.status_code, r1.json()["detail"]["code"]) == (422, "unknown_plan_ids")
    assert (r2.status_code, r2.json()["detail"]["code"]) == (422, "unknown_service_ids")
    # B's unit is not a slice of A.
    r3 = await client.put(_clinic_url(b["barra"].id), headers=HEADERS, json={"exceptions": []})
    assert r3.status_code == 404
    copy = await client.post(
        "/tenants/me/insurance-acceptance/clinic/copy",
        headers=HEADERS,
        json={"from_unit_id": str(a["barra"].id), "to_unit_ids": [str(b["barra"].id)]},
    )
    assert copy.status_code == 404
    assert await _count(world["db"]) == 0


async def test_matrices_of_two_clinics_never_mix(client, world):
    a, b = world["a"], world["b"]
    await client.put(
        _clinic_url(a["barra"].id),
        headers=HEADERS,
        json={"exceptions": [_cell(a["services"]["eco"], a["tplans"]["unimed"])]},
    )
    world["claim"]["tenant_id"] = b["tenant"].id  # now clinic B is calling
    body = (await client.get("/tenants/me/insurance-acceptance/clinic", headers=HEADERS)).json()
    assert all(sl["exceptions"] == [] for sl in body["slices"])
    summary = (await client.get("/tenants/me/insurance-acceptance/summary", headers=HEADERS)).json()
    assert summary["clinic"]["exception_count"] == 0
    # B saving its own slice leaves A's row alone.
    await client.put(_clinic_url(b["barra"].id), headers=HEADERS, json={"exceptions": []})
    assert await _count(world["db"]) == 1


# ---------------------------------------------------------------- copy


async def test_copy_replaces_each_target_slice_atomically(client, world):
    a = world["a"]
    s, p = a["services"], a["tplans"]
    await client.put(
        _clinic_url(a["barra"].id),
        headers=HEADERS,
        json={"exceptions": [_cell(s["eco"], p["unimed"])]},
    )
    await client.put(
        _clinic_url(a["centro"].id),
        headers=HEADERS,
        json={"exceptions": [_cell(s["retorno"], p["bradesco"])]},
    )
    resp = await client.post(
        "/tenants/me/insurance-acceptance/clinic/copy",
        headers=HEADERS,
        json={"from_unit_id": str(a["barra"].id), "to_unit_ids": [str(a["centro"].id)]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert _slice(body, a["centro"])["exceptions"] == _slice(body, a["barra"])["exceptions"]
    assert len(_slice(body, a["centro"])["exceptions"]) == 1
    assert await _count(world["db"]) == 2


async def test_copy_validation(client, world):
    a = world["a"]
    url = "/tenants/me/insurance-acceptance/clinic/copy"
    barra, centro = str(a["barra"].id), str(a["centro"].id)
    for to in ([], [barra], [centro, centro]):
        resp = await client.post(
            url, headers=HEADERS, json={"from_unit_id": barra, "to_unit_ids": to}
        )
        assert resp.status_code == 422, (to, resp.text)
        assert resp.json()["detail"]["code"] == "copy_target_invalid"
    none_src = await client.post(
        url, headers=HEADERS, json={"from_unit_id": "none", "to_unit_ids": [centro]}
    )
    assert none_src.status_code in (404, 422)
    missing = await client.post(
        url, headers=HEADERS, json={"from_unit_id": barra, "to_unit_ids": [str(uuid4())]}
    )
    assert missing.status_code == 404
    assert await _count(world["db"]) == 0


async def test_professional_copy_only_between_her_units(client, world):
    a = world["a"]
    await _set_mode(world["db"], a["tenant"], "independent")
    await client.put(
        _prof_url(a["ana"], a["barra"].id),
        headers=HEADERS,
        json={"exceptions": [_cell(a["services"]["eco"], a["pplans"]["amil"])]},
    )
    ok = await client.post(
        _prof_url(a["ana"], tail="/copy"),
        headers=HEADERS,
        json={"from_unit_id": str(a["barra"].id), "to_unit_ids": [str(a["centro"].id)]},
    )
    assert ok.status_code == 200, ok.text
    assert len(_slice(ok.json(), a["centro"])["exceptions"]) == 1
    async with world["db"]() as session:
        extra = Unit(id=uuid4(), tenant_id=a["tenant"].id, name="Norte", address="Rua C")
        session.add(extra)
        await session.commit()
    not_hers = await client.post(
        _prof_url(a["ana"], tail="/copy"),
        headers=HEADERS,
        json={"from_unit_id": str(a["barra"].id), "to_unit_ids": [str(extra.id)]},
    )
    assert not_hers.status_code == 422
    assert not_hers.json()["detail"]["code"] == "unit_slice_not_available"


# ---------------------------------------------------------------- summary


async def test_summary_shared_and_independent(client, world):
    a = world["a"]
    s = a["services"]
    await client.put(
        _clinic_url(a["barra"].id),
        headers=HEADERS,
        json={
            "exceptions": [
                _cell(s["eco"], a["tplans"]["unimed"]),
                _cell(s["retorno"], a["tplans"]["unimed"]),
            ]
        },
    )
    shared = (await client.get("/tenants/me/insurance-acceptance/summary", headers=HEADERS)).json()
    assert shared["mode"] == "shared" and shared["professionals"] == []
    assert shared["clinic"]["exception_count"] == 2
    assert shared["clinic"]["by_unit"] == [{"unit_id": str(a["barra"].id), "exception_count": 2}]

    await _set_mode(world["db"], a["tenant"], "independent")
    await client.put(
        _prof_url(a["ana"], a["centro"].id),
        headers=HEADERS,
        json={"exceptions": [_cell(s["eco"], a["pplans"]["amil"])]},
    )
    ind = (await client.get("/tenants/me/insurance-acceptance/summary", headers=HEADERS)).json()
    assert ind["mode"] == "independent" and ind["clinic"] is None
    assert [p["professional_id"] for p in ind["professionals"]] == [str(a["ana"].id)]
    assert ind["professionals"][0]["exception_count"] == 1


@pytest.mark.parametrize("mode", ["clinic_with_exceptions", None])
async def test_summary_409_without_a_matrix_mode(client, world, mode):
    await _set_mode(world["db"], world["a"]["tenant"], mode)
    resp = await client.get("/tenants/me/insurance-acceptance/summary", headers=HEADERS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "insurance_matrix_not_in_mode"


# ---------------------------------------------------------------- auth


async def test_requires_a_valid_hub_token(client, world):
    resp = await client.get("/tenants/me/insurance-acceptance/clinic")
    assert resp.status_code == 401
