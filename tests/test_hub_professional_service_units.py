# ruff: noqa: F811
"""Service marks in GET/PUT /tenants/me/professionals/{id}/units (TASK-083 follow-up T2).

The hub reads and writes "which services this professional does at which unit" together with the
unit links and hours, in ONE atomic save. No row for a service = every unit of the professional.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.api.hub import (  # noqa: E402
    deps as hub_deps,
    professional_units as pu_api,
)
from secretaria.core.database import get_session  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import (  # noqa: E402
    Professional,
    ProfessionalServiceUnit,
    ProfessionalUnit,
    Service,
    Unit,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from tests._local_agenda import seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

HEADERS = {"authorization": "Bearer pu-test-token"}


def _service(tenant_id, name: str) -> Service:
    return Service(
        id=uuid4(),
        tenant_id=tenant_id,
        name=name,
        normalized_name=name.casefold(),
        is_active=True,
    )


@pytest_asyncio.fixture
async def world(db, monkeypatch):
    from secretaria.main import app

    tenant = await seed_tenant(db, name="Clinica A")
    other = await seed_tenant(db, name="Clinica B")
    async with db() as session:
        limpeza = _service(tenant.id, "Limpeza")
        clareamento = _service(tenant.id, "Clareamento")
        extracao = _service(tenant.id, "Extracao")  # in the catalog, NOT offered by the doctor
        session.add_all([limpeza, clareamento, extracao])
        await session.flush()
        doctor = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[
                {"name": "Limpeza", "service_id": str(limpeza.id), "is_active": True},
                {"name": "Clareamento", "service_id": str(clareamento.id), "is_active": True},
                {"name": "Consulta", "is_active": True},  # no service_id: never restricted
            ],
        )
        units = {
            name: Unit(id=uuid4(), tenant_id=tenant.id, name=name, address=f"Rua {name}")
            for name in ("Centro", "Norte", "Sul")
        }
        foreign_unit = Unit(id=uuid4(), tenant_id=other.id, name="Alheia", address="Rua Alheia")
        session.add_all([doctor, foreign_unit, *units.values()])
        await session.commit()

    claim = {
        "role": "manager",
        "is_owner": False,
        "is_manager": False,
        "professional_id": None,
        "tenant_id": tenant.id,
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

    async def _entitled(tenant_id, redis=None):
        return EntitlementSummary(
            tenant_id=str(tenant_id),
            status="active",
            active=True,
            secretaria_enabled=True,
            plan="bronze",
            secretaria_tier="basico",
            addons={"multi_unit": True},
            limits={},
        )

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    monkeypatch.setattr(pu_api, "get_entitlements", _entitled)
    yield {
        "db": db,
        "tenant": tenant,
        "doctor": doctor,
        "limpeza": limpeza,
        "clareamento": clareamento,
        "extracao": extracao,
        "centro": units["Centro"],
        "norte": units["Norte"],
        "sul": units["Sul"],
        "foreign_unit": foreign_unit,
        "claim": claim,
    }
    app.dependency_overrides.pop(get_session, None)


def _url(professional) -> str:
    return f"/tenants/me/professionals/{professional.id}/units"


def _link(unit, hours=None) -> dict:
    return {"unit_id": str(unit.id), "business_hours": hours}


def _mark(service, *units) -> dict:
    return {"service_id": str(service.id), "unit_ids": [str(u.id) for u in units]}


async def _put(client, professional, links, marks="omit"):
    body: dict = {"links": links}
    if marks != "omit":
        body["service_marks"] = marks
    return await client.put(_url(professional), headers=HEADERS, json=body)


async def _seed_link(w, unit, *, hours=None):
    async with w["db"]() as session:
        session.add(
            ProfessionalUnit(
                professional_id=w["doctor"].id,
                unit_id=unit.id,
                business_hours=hours,
                is_active=True,
                sort_order=0,
            )
        )
        await session.commit()


async def _seed_marks(w, service, *units):
    async with w["db"]() as session:
        for unit in units:
            session.add(
                ProfessionalServiceUnit(
                    professional_id=w["doctor"].id, service_id=service.id, unit_id=unit.id
                )
            )
        await session.commit()


async def _db_marks(w) -> dict:
    async with w["db"]() as session:
        rows = await session.execute(
            select(ProfessionalServiceUnit.service_id, ProfessionalServiceUnit.unit_id).where(
                ProfessionalServiceUnit.professional_id == w["doctor"].id
            )
        )
        out: dict = {}
        for service_id, unit_id in rows:
            out.setdefault(str(service_id), set()).add(str(unit_id))
        return out


async def _db_link_ids(w) -> set[str]:
    async with w["db"]() as session:
        rows = await session.scalars(
            select(ProfessionalUnit.unit_id).where(
                ProfessionalUnit.professional_id == w["doctor"].id
            )
        )
        return {str(u) for u in rows}


def _marks_by_service(payload) -> dict:
    return {m["service_id"]: set(m["unit_ids"]) for m in payload["service_marks"]}


# ---------------------------------------------------------------- GET


async def test_get_returns_services_and_marks(client, world):
    w = world
    await _seed_link(w, w["centro"])
    await _seed_link(w, w["norte"])
    await _seed_marks(w, w["limpeza"], w["centro"])
    response = await client.get(_url(w["doctor"]), headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"units", "services", "service_marks"}
    # The doctor's active catalog services (entries with a service_id), by the clinic's name.
    assert {s["service_id"]: s["name"] for s in body["services"]} == {
        str(w["limpeza"].id): "Limpeza",
        str(w["clareamento"].id): "Clareamento",
    }
    assert all(set(s) == {"service_id", "name"} for s in body["services"])
    # Only RESTRICTED services appear in service_marks; the others mean "every unit".
    assert _marks_by_service(body) == {str(w["limpeza"].id): {str(w["centro"].id)}}


async def test_get_without_marks_has_empty_service_marks(client, world):
    body = (await client.get(_url(world["doctor"]), headers=HEADERS)).json()
    assert body["service_marks"] == []
    assert len(body["services"]) == 2


async def test_get_hides_marks_of_services_the_doctor_no_longer_offers(client, world):
    """Final review (finding 3): a stale mark (the service left the doctor's catalog) is not
    shown, and a PUT that does not send it succeeds and removes the stale row."""
    w = world
    await _seed_link(w, w["centro"])
    await _seed_marks(w, w["limpeza"], w["centro"])
    await _seed_marks(w, w["extracao"], w["centro"])  # in the catalog, NOT offered by the doctor
    body = (await client.get(_url(w["doctor"]), headers=HEADERS)).json()
    offered = {s["service_id"] for s in body["services"]}
    assert str(w["extracao"].id) not in offered
    assert _marks_by_service(body) == {str(w["limpeza"].id): {str(w["centro"].id)}}

    response = await _put(
        client, w["doctor"], [_link(w["centro"])], [_mark(w["limpeza"], w["centro"])]
    )
    assert response.status_code == 200, response.text
    assert await _db_marks(w) == {str(w["limpeza"].id): {str(w["centro"].id)}}


# ---------------------------------------------------------------- PUT


async def test_put_links_hours_and_marks_in_one_commit(client, world):
    w = world
    hours = {"monday": [{"start": "08:00", "end": "12:00"}]}
    response = await _put(
        client,
        w["doctor"],
        [_link(w["centro"], hours), _link(w["norte"])],
        [_mark(w["limpeza"], w["centro"]), _mark(w["clareamento"], w["centro"], w["norte"])],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert _marks_by_service(body) == {
        str(w["limpeza"].id): {str(w["centro"].id)},
        str(w["clareamento"].id): {str(w["centro"].id), str(w["norte"].id)},
    }
    rows = {r["unit_id"]: r for r in body["units"]}
    assert rows[str(w["centro"].id)]["linked"] is True
    assert rows[str(w["centro"].id)]["business_hours"] == hours
    # And it is really stored, links and marks together.
    assert await _db_link_ids(w) == {str(w["centro"].id), str(w["norte"].id)}
    assert await _db_marks(w) == {
        str(w["limpeza"].id): {str(w["centro"].id)},
        str(w["clareamento"].id): {str(w["centro"].id), str(w["norte"].id)},
    }


async def test_put_without_service_marks_leaves_marks_untouched(client, world):
    w = world
    await _seed_link(w, w["centro"])
    await _seed_link(w, w["norte"])
    await _seed_marks(w, w["limpeza"], w["centro"], w["norte"])
    # Only the links change (norte kept, centro kept); no `service_marks` key at all.
    response = await _put(client, w["doctor"], [_link(w["centro"]), _link(w["norte"])])
    assert response.status_code == 200, response.text
    assert _marks_by_service(response.json()) == {
        str(w["limpeza"].id): {str(w["centro"].id), str(w["norte"].id)}
    }
    assert await _db_marks(w) == {str(w["limpeza"].id): {str(w["centro"].id), str(w["norte"].id)}}


async def test_put_without_service_marks_drops_marks_of_unlinked_units(client, world):
    w = world
    await _seed_link(w, w["centro"])
    await _seed_link(w, w["norte"])
    await _seed_marks(w, w["limpeza"], w["centro"], w["norte"])
    response = await _put(client, w["doctor"], [_link(w["centro"])])
    assert response.status_code == 200, response.text
    assert await _db_marks(w) == {str(w["limpeza"].id): {str(w["centro"].id)}}


async def test_put_empty_service_marks_list_clears_every_restriction(client, world):
    w = world
    await _seed_link(w, w["centro"])
    await _seed_link(w, w["norte"])
    await _seed_marks(w, w["limpeza"], w["centro"])
    response = await _put(client, w["doctor"], [_link(w["centro"]), _link(w["norte"])], [])
    assert response.status_code == 200, response.text
    assert response.json()["service_marks"] == []
    assert await _db_marks(w) == {}


async def test_put_unlinking_only_unit_of_restricted_service_is_422_and_nothing_saved(
    client, world
):
    w = world
    await _seed_link(w, w["centro"], hours={"monday": [{"start": "08:00", "end": "12:00"}]})
    await _seed_link(w, w["norte"])
    await _seed_marks(w, w["limpeza"], w["norte"])
    before = (await client.get(_url(w["doctor"]), headers=HEADERS)).json()

    # Drop norte (the ONLY unit of Limpeza) and change centro's hours: refused, nothing kept.
    response = await _put(
        client,
        w["doctor"],
        [_link(w["centro"], {"tuesday": [{"start": "14:00", "end": "18:00"}]})],
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "code": "service_without_unit",
        "service_ids": [str(w["limpeza"].id)],
    }
    after = (await client.get(_url(w["doctor"]), headers=HEADERS)).json()
    assert after == before
    assert await _db_link_ids(w) == {str(w["centro"].id), str(w["norte"].id)}
    assert await _db_marks(w) == {str(w["limpeza"].id): {str(w["norte"].id)}}


async def test_put_marks_for_service_not_offered_is_422(client, world):
    w = world
    response = await _put(
        client, w["doctor"], [_link(w["centro"])], [_mark(w["extracao"], w["centro"])]
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "code": "service_not_offered",
        "service_ids": [str(w["extracao"].id)],
    }
    assert await _db_link_ids(w) == set()  # the links were not saved either
    assert await _db_marks(w) == {}


async def test_put_marks_unit_not_linked_is_422(client, world):
    w = world
    response = await _put(
        client, w["doctor"], [_link(w["centro"])], [_mark(w["limpeza"], w["norte"])]
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "code": "unit_not_linked",
        "service_ids": [str(w["limpeza"].id)],
    }
    assert await _db_link_ids(w) == set()


async def test_put_marks_unit_of_other_tenant_is_404(client, world):
    w = world
    response = await _put(
        client, w["doctor"], [_link(w["centro"])], [_mark(w["limpeza"], w["foreign_unit"])]
    )
    assert response.status_code == 404, response.text
    assert await _db_link_ids(w) == set()
    assert await _db_marks(w) == {}


async def test_put_marks_unknown_or_malformed_unit_is_404(client, world):
    w = world
    for bad in (str(uuid4()), "nope"):
        response = await _put(
            client,
            w["doctor"],
            [_link(w["centro"])],
            [{"service_id": str(w["limpeza"].id), "unit_ids": [bad]}],
        )
        assert response.status_code == 404, response.text


async def test_put_mark_with_no_units_is_422_by_schema(client, world):
    w = world
    response = await _put(
        client,
        w["doctor"],
        [_link(w["centro"])],
        [{"service_id": str(w["limpeza"].id), "unit_ids": []}],
    )
    assert response.status_code == 422
    assert await _db_marks(w) == {}


async def test_put_marks_on_a_kept_inactive_link_counts_as_linked(client, world):
    """R6: links to deactivated units are preserved by the replace and still count as links."""
    w = world
    async with w["db"]() as session:
        old = Unit(id=uuid4(), tenant_id=w["tenant"].id, name="Antiga", is_active=False)
        session.add(old)
        await session.flush()
        session.add(ProfessionalUnit(professional_id=w["doctor"].id, unit_id=old.id))
        await session.commit()
    await _seed_marks(w, w["limpeza"], old)
    # Linking centro too and re-sending the (stored) restriction: accepted, no 422.
    response = await _put(
        client,
        w["doctor"],
        [_link(w["centro"])],
        [{"service_id": str(w["limpeza"].id), "unit_ids": [str(old.id)]}],
    )
    assert response.status_code == 200, response.text
    assert await _db_marks(w) == {str(w["limpeza"].id): {str(old.id)}}


async def test_put_requires_manager_or_own_professional(client, world):
    w = world
    body_marks = [_mark(w["limpeza"], w["centro"])]
    w["claim"]["role"] = "secretary"
    denied = await _put(client, w["doctor"], [_link(w["centro"])], body_marks)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "forbidden_role"
    assert await _db_marks(w) == {}

    w["claim"].update(role="doctor", professional_id=w["doctor"].id)
    own = await _put(client, w["doctor"], [_link(w["centro"])], body_marks)
    assert own.status_code == 200, own.text
    assert await _db_marks(w) == {str(w["limpeza"].id): {str(w["centro"].id)}}


async def test_two_concurrent_puts_one_wins_409(client, world, monkeypatch):
    """The loser of two simultaneous saves collides in the DB (IntegrityError) and gets a plain
    409 with NOTHING kept (no orphaned marks). A real concurrent race is not feasible on SQLite
    (no row locks), so - like the existing suite - the collision is injected, here at the marks
    write, AFTER the links were written in the same transaction."""
    from sqlalchemy.exc import IntegrityError

    w = world
    await _seed_link(w, w["centro"])
    await _seed_marks(w, w["limpeza"], w["centro"])

    async def colliding(*_args, **_kwargs):
        raise IntegrityError("INSERT professional_service_units", {}, Exception("duplicate key"))

    monkeypatch.setattr(pu_api.psu, "apply_marks", colliding)
    response = await _put(
        client,
        w["doctor"],
        [_link(w["norte"])],
        [_mark(w["clareamento"], w["norte"])],
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "professional_units_changed"
    # Links (replace_links ran before the collision) were rolled back with it.
    assert await _db_link_ids(w) == {str(w["centro"].id)}
    assert await _db_marks(w) == {str(w["limpeza"].id): {str(w["centro"].id)}}
