# ruff: noqa: F811
"""Units per professional (TASK-083 Task 6): service + hub API.

  GET /tenants/me/professionals/{pid}/units   - any staff member of the tenant
  PUT /tenants/me/professionals/{pid}/units   - manager, or the doctor themself
  GET /tenants/me/units                       - gains `professionals_count`

Without any link nothing changes anywhere (spec §3).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.api.hub import (
    deps as hub_deps,  # noqa: E402
    professional_units as pu_api,  # noqa: E402
)
from secretaria.core.database import get_session  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import Professional, ProfessionalUnit, Unit  # noqa: E402
from secretaria.services import professional_units as pu_service  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from tests._local_agenda import WEEK_HOURS, seed_professional, seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

HEADERS = {"authorization": "Bearer pu-test-token"}
MORNING = {"monday": [{"start": "08:00", "end": "12:00"}]}
EVENING = {"tuesday": [{"start": "18:00", "end": "21:00"}]}


def _summary(*, entitled: bool, limits: dict | None = None) -> EntitlementSummary:
    return EntitlementSummary(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons={"multi_unit": entitled},
        limits=limits or {},
    )


def _entitlements(monkeypatch, *, entitled=True, limits=None, fail=False) -> list:
    calls: list = []

    async def _fake(tenant_id, redis=None):
        calls.append(tenant_id)
        return None if fail else _summary(entitled=entitled, limits=limits)

    monkeypatch.setattr(pu_api, "get_entitlements", _fake)
    return calls


@pytest_asyncio.fixture
async def world(db, monkeypatch):
    from secretaria.main import app

    tenant = await seed_tenant(db, name="Clínica A")
    other_tenant = await seed_tenant(db, name="Clínica B")
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    colleague = await seed_professional(db, tenant, "Dr. Beto")
    foreign = await seed_professional(db, other_tenant, "Dr. Fora")

    async def _unit(owner, name, **kw):
        async with db() as session:
            unit = Unit(tenant_id=owner.id, name=name, address=f"Rua {name}", **kw)
            session.add(unit)
            await session.commit()
            await session.refresh(unit)
            return unit

    centro = await _unit(tenant, "Centro")
    norte = await _unit(tenant, "Norte")
    sul = await _unit(tenant, "Sul")
    closed = await _unit(tenant, "Antiga", is_active=False)
    foreign_unit = await _unit(other_tenant, "Alheia")

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

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    _entitlements(monkeypatch, entitled=True)
    yield {
        "db": db,
        "tenant": tenant,
        "other_tenant": other_tenant,
        "doctor": doctor,
        "colleague": colleague,
        "foreign": foreign,
        "centro": centro,
        "norte": norte,
        "sul": sul,
        "closed": closed,
        "foreign_unit": foreign_unit,
        "claim": claim,
    }
    app.dependency_overrides.pop(get_session, None)


def _url(professional) -> str:
    return f"/tenants/me/professionals/{professional.id}/units"


def _link(unit, hours=None) -> dict:
    return {"unit_id": str(unit.id), "business_hours": hours}


async def _put(client, professional, *links):
    return await client.put(_url(professional), headers=HEADERS, json={"links": list(links)})


async def _db_links(w, professional) -> dict:
    async with w["db"]() as session:
        rows = await session.scalars(
            select(ProfessionalUnit).where(ProfessionalUnit.professional_id == professional.id)
        )
        return {str(r.unit_id): r for r in rows}


async def _seed_link(w, professional, unit, *, hours=None, is_active=True, sort_order=0):
    async with w["db"]() as session:
        session.add(
            ProfessionalUnit(
                professional_id=professional.id,
                unit_id=unit.id,
                business_hours=hours,
                is_active=is_active,
                sort_order=sort_order,
            )
        )
        await session.commit()


def _by_unit(payload) -> dict:
    return {row["unit_id"]: row for row in payload["units"]}


# ---------------------------------------------------------------- GET


async def test_get_lists_every_active_unit_with_linked_flag(client, world):
    w = world
    await _seed_link(w, w["doctor"], w["centro"], hours=MORNING)
    await _seed_link(w, w["doctor"], w["closed"])  # inactive unit: never listed
    response = await client.get(_url(w["doctor"]), headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"units", "services", "service_marks"}  # T2: marks ride along
    rows = _by_unit(body)
    assert set(rows) == {str(w["centro"].id), str(w["norte"].id), str(w["sul"].id)}
    centro = rows[str(w["centro"].id)]
    assert set(centro) == {"unit_id", "name", "address", "is_active", "linked", "business_hours"}
    assert centro["linked"] is True
    assert centro["business_hours"] == MORNING
    assert centro["name"] == "Centro" and centro["address"] == "Rua Centro"
    assert rows[str(w["norte"].id)]["linked"] is False
    assert rows[str(w["norte"].id)]["business_hours"] is None


async def test_get_inherited_link_is_linked_with_null_hours(client, world):
    w = world
    await _seed_link(w, w["doctor"], w["centro"], hours=None)
    rows = _by_unit((await client.get(_url(w["doctor"]), headers=HEADERS)).json())
    assert rows[str(w["centro"].id)]["linked"] is True
    assert rows[str(w["centro"].id)]["business_hours"] is None


async def test_get_without_links_has_nothing_linked(client, world):
    rows = (await client.get(_url(world["doctor"]), headers=HEADERS)).json()["units"]
    assert rows and not any(r["linked"] for r in rows)


async def test_get_never_calls_entitlements(client, world, monkeypatch):
    calls = _entitlements(monkeypatch, entitled=False)
    response = await client.get(_url(world["doctor"]), headers=HEADERS)
    assert response.status_code == 200
    assert calls == []


@pytest.mark.parametrize("role", ["secretary", "manager"])
async def test_get_is_readable_by_any_staff_member(client, world, role):
    world["claim"]["role"] = role
    assert (await client.get(_url(world["doctor"]), headers=HEADERS)).status_code == 200


async def test_a_doctor_who_is_not_the_manager_reads_only_himself(client, world):
    """TASK-091 T4: the desk reads every doctor's units to book; a non-manager doctor, only his."""
    world["claim"]["role"] = "doctor"
    world["claim"]["professional_id"] = None
    other = await client.get(_url(world["doctor"]), headers=HEADERS)
    assert other.status_code == 403
    assert other.json()["detail"]["code"] == "forbidden_role"
    world["claim"]["professional_id"] = world["doctor"].id
    assert (await client.get(_url(world["doctor"]), headers=HEADERS)).status_code == 200


async def test_get_other_tenants_professional_is_404(client, world):
    assert (await client.get(_url(world["foreign"]), headers=HEADERS)).status_code == 404


async def test_get_malformed_professional_id_is_404(client, world):
    response = await client.get("/tenants/me/professionals/not-a-uuid/units", headers=HEADERS)
    assert response.status_code == 404


async def test_get_without_token_is_401(client, world):
    assert (await client.get(_url(world["doctor"]))).status_code == 401


# ---------------------------------------------------------------- PUT: entitlement / limit


async def test_put_two_links_not_entitled_is_403(client, world, monkeypatch):
    _entitlements(monkeypatch, entitled=False)
    response = await _put(client, world["doctor"], _link(world["centro"]), _link(world["norte"]))
    assert response.status_code == 403
    assert response.json()["detail"] == "multi_unit_not_entitled"
    assert await _db_links(world, world["doctor"]) == {}


async def test_put_one_link_not_entitled_is_allowed_without_asking(client, world, monkeypatch):
    calls = _entitlements(monkeypatch, entitled=False)
    response = await _put(client, world["doctor"], _link(world["centro"]))
    assert response.status_code == 200, response.text
    assert calls == []
    assert set(await _db_links(world, world["doctor"])) == {str(world["centro"].id)}


async def test_put_empty_set_not_entitled_clears_links(client, world, monkeypatch):
    await _seed_link(world, world["doctor"], world["centro"])
    _entitlements(monkeypatch, entitled=False)
    response = await _put(client, world["doctor"])
    assert response.status_code == 200
    assert await _db_links(world, world["doctor"]) == {}
    assert not any(r["linked"] for r in response.json()["units"])


async def test_put_reducing_but_still_over_one_without_entitlement_is_403(
    client, world, monkeypatch
):
    """Final wave T6: the RESULTING set decides - three links reduced to two is still more than
    one unit without the add-on (403, nothing written); reduced to one is allowed."""
    w = world
    for position, unit in enumerate((w["centro"], w["norte"], w["sul"])):
        await _seed_link(w, w["doctor"], unit, sort_order=position)
    _entitlements(monkeypatch, entitled=False)

    still_two = await _put(client, w["doctor"], _link(w["centro"]), _link(w["norte"]))
    assert still_two.status_code == 403
    assert still_two.json()["detail"] == "multi_unit_not_entitled"
    assert len(await _db_links(w, w["doctor"])) == 3

    one = await _put(client, w["doctor"], _link(w["centro"]))
    assert one.status_code == 200, one.text
    assert set(await _db_links(w, w["doctor"])) == {str(w["centro"].id)}


async def test_put_a_concurrent_save_is_a_409_not_a_500(client, world, monkeypatch):
    """Final wave T6: two saves at once may collide on the (professional, unit) key; the loser
    gets a plain 409 and nothing half-written, never an unhandled 500."""
    from sqlalchemy.exc import IntegrityError

    async def colliding(*_args, **_kwargs):
        raise IntegrityError("INSERT professional_units", {}, Exception("duplicate key"))

    monkeypatch.setattr(pu_api.pu, "replace_links", colliding)
    response = await _put(client, world["doctor"], _link(world["centro"]))
    assert response.status_code == 409
    assert response.json()["detail"] == "professional_units_changed"


async def test_replace_locks_the_professional_row_first():
    """Final wave T6: the replace starts with SELECT ... FOR UPDATE on the professional (a
    no-op on SQLite), so two saves of one professional run one after the other on Postgres."""
    from types import SimpleNamespace

    seen: list[str] = []

    class _Session:
        async def execute(self, statement, *args, **kwargs):
            seen.append(str(statement.compile()))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))

        async def scalars(self, statement, *args, **kwargs):
            seen.append(str(statement.compile()))
            return []

        def add(self, row):
            pass

        async def flush(self):
            pass

    professional = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    await pu_service.replace_links(_Session(), professional, [])
    assert seen and seen[0].startswith("SELECT professionals.id")
    assert "FOR UPDATE" in seen[0]


async def test_put_two_links_entitled_within_limit(client, world, monkeypatch):
    _entitlements(monkeypatch, entitled=True, limits={"units": 2})
    response = await _put(client, world["doctor"], _link(world["centro"]), _link(world["norte"]))
    assert response.status_code == 200, response.text


async def test_put_over_unit_limit_is_409(client, world, monkeypatch):
    _entitlements(monkeypatch, entitled=True, limits={"units": 2})
    response = await _put(
        client,
        world["doctor"],
        _link(world["centro"]),
        _link(world["norte"]),
        _link(world["sul"]),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "unit_limit_reached"
    assert await _db_links(world, world["doctor"]) == {}


async def test_put_entitlement_failure_fails_closed_503(client, world, monkeypatch):
    _entitlements(monkeypatch, fail=True)
    response = await _put(client, world["doctor"], _link(world["centro"]), _link(world["norte"]))
    assert response.status_code == 503
    assert await _db_links(world, world["doctor"]) == {}


# ---------------------------------------------------------------- PUT: tenant isolation


async def test_put_unit_of_another_tenant_is_404_and_writes_nothing(client, world):
    response = await _put(
        client, world["doctor"], _link(world["centro"]), _link(world["foreign_unit"])
    )
    assert response.status_code == 404
    assert await _db_links(world, world["doctor"]) == {}


async def test_put_unknown_or_malformed_unit_id_is_404(client, world):
    assert (await _put(client, world["doctor"], {"unit_id": str(uuid4())})).status_code == 404
    assert (await _put(client, world["doctor"], {"unit_id": "nope"})).status_code == 404


async def test_put_other_tenants_professional_is_404(client, world):
    response = await _put(client, world["foreign"], _link(world["centro"]))
    assert response.status_code == 404
    assert await _db_links(world, world["foreign"]) == {}


# ---------------------------------------------------------------- PUT: replace-set semantics


async def test_put_replaces_the_set_and_answers_like_get(client, world):
    w = world
    await _seed_link(w, w["doctor"], w["centro"], hours=MORNING)
    await _seed_link(w, w["doctor"], w["norte"])
    response = await _put(client, w["doctor"], _link(w["norte"], EVENING), _link(w["sul"]))
    assert response.status_code == 200, response.text
    after = await client.get(_url(w["doctor"]), headers=HEADERS)
    assert response.json() == after.json()
    rows = _by_unit(response.json())
    assert rows[str(w["centro"].id)]["linked"] is False
    assert rows[str(w["norte"].id)]["linked"] is True
    assert rows[str(w["norte"].id)]["business_hours"] == EVENING
    assert rows[str(w["sul"].id)]["linked"] is True
    assert rows[str(w["sul"].id)]["business_hours"] is None
    assert set(await _db_links(w, w["doctor"])) == {str(w["norte"].id), str(w["sul"].id)}


async def test_put_does_not_touch_another_professionals_links(client, world):
    await _seed_link(world, world["colleague"], world["centro"], hours=MORNING)
    await _put(client, world["doctor"], _link(world["centro"]))
    other = await _db_links(world, world["colleague"])
    assert other[str(world["centro"].id)].business_hours == MORNING


async def test_put_preserves_links_and_hours_of_deactivated_units(client, world):
    w = world
    await _seed_link(w, w["doctor"], w["closed"], hours=EVENING, sort_order=7)
    response = await _put(client, w["doctor"], _link(w["centro"]))
    assert response.status_code == 200
    links = await _db_links(w, w["doctor"])
    kept = links[str(w["closed"].id)]
    assert kept.business_hours == EVENING and kept.is_active is True and kept.sort_order == 7
    assert set(links) == {str(w["closed"].id), str(w["centro"].id)}
    # the hidden link is not part of the answer
    assert str(w["closed"].id) not in _by_unit(response.json())
    # an empty PUT clears the active links but still keeps the deactivated one
    await _put(client, w["doctor"])
    assert set(await _db_links(w, w["doctor"])) == {str(w["closed"].id)}


async def test_put_ignores_an_inactive_unit_id_in_the_body(client, world):
    w = world
    response = await _put(client, w["doctor"], _link(w["centro"]), _link(w["closed"], MORNING))
    assert response.status_code == 200, response.text
    assert set(await _db_links(w, w["doctor"])) == {str(w["centro"].id)}


async def test_put_inactive_unit_id_does_not_count_for_entitlement(client, world, monkeypatch):
    _entitlements(monkeypatch, entitled=False)
    response = await _put(client, world["doctor"], _link(world["centro"]), _link(world["closed"]))
    assert response.status_code == 200, response.text


async def test_put_duplicate_unit_id_is_422(client, world):
    response = await _put(client, world["doctor"], _link(world["centro"]), _link(world["centro"]))
    assert response.status_code == 422


async def test_put_stores_the_array_order_as_sort_order(client, world):
    w = world
    await _put(client, w["doctor"], _link(w["sul"]), _link(w["centro"]), _link(w["norte"]))
    links = await _db_links(w, w["doctor"])
    assert links[str(w["sul"].id)].sort_order == 0
    assert links[str(w["centro"].id)].sort_order == 1
    assert links[str(w["norte"].id)].sort_order == 2


# ---------------------------------------------------------------- PUT: payload validation


@pytest.mark.parametrize(
    "link_extra",
    [{"agenda_id": str(uuid4())}, {"person_key": "x"}, {"is_active": False}, {"sort_order": 3}],
)
async def test_put_link_rejects_extra_fields(client, world, link_extra):
    response = await client.put(
        _url(world["doctor"]),
        headers=HEADERS,
        json={"links": [{**_link(world["centro"]), **link_extra}]},
    )
    assert response.status_code == 422
    assert await _db_links(world, world["doctor"]) == {}


async def test_put_body_rejects_extra_fields(client, world):
    response = await client.put(
        _url(world["doctor"]),
        headers=HEADERS,
        json={"links": [], "agenda_id": str(uuid4())},
    )
    assert response.status_code == 422


async def test_put_requires_the_links_key(client, world):
    assert (await client.put(_url(world["doctor"]), headers=HEADERS, json={})).status_code == 422


@pytest.mark.parametrize(
    "hours",
    [
        {"funday": [{"start": "08:00", "end": "12:00"}]},
        {"monday": [{"start": "08:00", "end": "12:00"}, {"start": "11:00", "end": "13:00"}]},
        {"monday": [{"start": "12:00", "end": "08:00"}]},
        {"monday": [{"start": "8am", "end": "12:00"}]},
    ],
)
async def test_put_hours_use_the_professional_hours_validation(client, world, hours):
    response = await _put(client, world["doctor"], _link(world["centro"], hours))
    assert response.status_code == 422


async def test_put_absent_hours_means_inherit(client, world):
    response = await client.put(
        _url(world["doctor"]),
        headers=HEADERS,
        json={"links": [{"unit_id": str(world["centro"].id)}]},
    )
    assert response.status_code == 200
    stored = await _db_links(world, world["doctor"])
    assert stored[str(world["centro"].id)].business_hours is None


async def test_put_empty_hours_means_closed_all_week_not_inherit(client, world):
    response = await _put(client, world["doctor"], _link(world["centro"], {}))
    assert response.status_code == 200
    stored = (await _db_links(world, world["doctor"]))[str(world["centro"].id)]
    assert stored.business_hours == {}
    assert _by_unit(response.json())[str(world["centro"].id)]["business_hours"] == {}


# ---------------------------------------------------------------- PUT: role matrix


async def test_put_secretary_is_forbidden(client, world):
    world["claim"]["role"] = "secretary"
    response = await _put(client, world["doctor"], _link(world["centro"]))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden_role"
    assert await _db_links(world, world["doctor"]) == {}


async def test_put_secretary_flagged_manager_is_still_forbidden(client, world):
    world["claim"].update(role="secretary", is_manager=True, is_owner=True)
    assert (await _put(client, world["doctor"], _link(world["centro"]))).status_code == 403


async def test_put_doctor_can_edit_only_their_own(client, world):
    world["claim"].update(role="doctor", professional_id=world["doctor"].id)
    assert (await _put(client, world["doctor"], _link(world["centro"]))).status_code == 200
    other = await _put(client, world["colleague"], _link(world["centro"]))
    assert other.status_code == 403
    assert other.json()["detail"]["code"] == "forbidden_role"
    assert await _db_links(world, world["colleague"]) == {}


async def test_put_doctor_without_professional_claim_is_forbidden(client, world):
    world["claim"].update(role="doctor", professional_id=None)
    assert (await _put(client, world["doctor"], _link(world["centro"]))).status_code == 403


async def test_put_flagged_doctor_manager_can_edit_any(client, world):
    world["claim"].update(role="doctor", is_manager=True, professional_id=world["doctor"].id)
    assert (await _put(client, world["colleague"], _link(world["centro"]))).status_code == 200


async def test_put_manager_can_edit_any(client, world):
    assert (await _put(client, world["colleague"], _link(world["centro"]))).status_code == 200


async def test_put_without_role_claim_fails_closed(client, world):
    world["claim"]["role"] = None
    response = await _put(client, world["doctor"], _link(world["centro"]))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "role_claim_missing"


async def test_put_without_token_is_401(client, world):
    response = await client.put(_url(world["doctor"]), json={"links": []})
    assert response.status_code == 401


# ---------------------------------------------------------------- service: hours / availability


async def test_unit_hours_inherit_the_professionals_resolved_hours(world):
    w = world
    await _seed_link(w, w["doctor"], w["centro"], hours=None)
    async with w["db"]() as session:
        prof = await session.get(Professional, w["doctor"].id)
        unit = await session.get(Unit, w["centro"].id)
        # professional has no hours of their own -> the tenant's (WEEK_HOURS)
        assert await pu_service.unit_hours(session, prof, unit) == WEEK_HOURS
        prof.business_hours = MORNING  # now an own value -> that one
        assert await pu_service.unit_hours(session, prof, unit) == MORNING
        prof.business_hours = {}  # own empty = closed all week, inherited by the unit link
        assert await pu_service.unit_hours(session, prof, unit) == {}


async def test_unit_hours_own_value_wins_over_the_professionals(world):
    w = world
    await _seed_link(w, w["doctor"], w["centro"], hours=EVENING)
    async with w["db"]() as session:
        prof = await session.get(Professional, w["doctor"].id)
        prof.business_hours = MORNING
        unit = await session.get(Unit, w["centro"].id)
        assert await pu_service.unit_hours(session, prof, unit) == EVENING


async def test_unit_hours_empty_dict_is_closed_and_never_inherits(world):
    w = world
    await _seed_link(w, w["doctor"], w["centro"], hours={})
    async with w["db"]() as session:
        prof = await session.get(Professional, w["doctor"].id)
        prof.business_hours = MORNING
        unit = await session.get(Unit, w["centro"].id)
        assert await pu_service.unit_hours(session, prof, unit) == {}


async def test_unit_hours_drop_closed_weekdays_like_the_professional_resolver(world):
    w = world
    await _seed_link(w, w["doctor"], w["centro"], hours={**MORNING, "friday": []})
    async with w["db"]() as session:
        prof = await session.get(Professional, w["doctor"].id)
        unit = await session.get(Unit, w["centro"].id)
        assert await pu_service.unit_hours(session, prof, unit) == MORNING


async def test_unit_hours_is_none_when_the_unit_is_not_offered(world):
    w = world
    await _seed_link(w, w["doctor"], w["centro"])
    await _seed_link(w, w["doctor"], w["closed"])
    await _seed_link(w, w["doctor"], w["sul"], is_active=False)
    async with w["db"]() as session:
        prof = await session.get(Professional, w["doctor"].id)
        for key in ("norte", "closed", "sul"):  # unlinked / deactivated unit / inactive link
            unit = await session.get(Unit, w[key].id)
            assert await pu_service.unit_hours(session, prof, unit) is None
        centro = await session.get(Unit, w["centro"].id)
        assert await pu_service.unit_hours(session, prof, centro) == WEEK_HOURS


async def test_available_unit_ids_are_active_units_with_active_links(world):
    w = world
    await _seed_link(w, w["doctor"], w["norte"], sort_order=1)
    await _seed_link(w, w["doctor"], w["centro"], sort_order=0)
    await _seed_link(w, w["doctor"], w["sul"], is_active=False)
    await _seed_link(w, w["doctor"], w["closed"])
    await _seed_link(w, w["colleague"], w["sul"])
    async with w["db"]() as session:
        prof = await session.get(Professional, w["doctor"].id)
        assert await pu_service.available_unit_ids(session, prof) == [w["centro"].id, w["norte"].id]


async def test_available_unit_ids_is_empty_without_links(world):
    async with world["db"]() as session:
        prof = await session.get(Professional, world["doctor"].id)
        assert await pu_service.available_unit_ids(session, prof) == []


async def test_deactivating_a_unit_hides_it_but_keeps_the_link(client, world):
    w = world
    await _put(client, w["doctor"], _link(w["centro"], MORNING), _link(w["norte"]))
    async with w["db"]() as session:
        unit = await session.get(Unit, w["centro"].id)
        unit.is_active = False
        await session.commit()
    async with w["db"]() as session:
        prof = await session.get(Professional, w["doctor"].id)
        assert await pu_service.available_unit_ids(session, prof) == [w["norte"].id]
    links = await _db_links(w, w["doctor"])
    assert str(w["centro"].id) in links and links[str(w["centro"].id)].business_hours == MORNING
    # reactivating brings it back, hours intact
    async with w["db"]() as session:
        unit = await session.get(Unit, w["centro"].id)
        unit.is_active = True
        await session.commit()
    async with w["db"]() as session:
        prof = await session.get(Professional, w["doctor"].id)
        assert set(await pu_service.available_unit_ids(session, prof)) == {
            w["centro"].id,
            w["norte"].id,
        }


# ---------------------------------------------------------------- GET /tenants/me/units


async def test_units_list_has_professionals_count(client, world):
    w = world
    await _seed_link(w, w["doctor"], w["centro"])
    await _seed_link(w, w["colleague"], w["centro"])
    await _seed_link(w, w["doctor"], w["norte"])
    await _seed_link(w, w["colleague"], w["sul"], is_active=False)  # inactive link
    async with w["db"]() as session:
        gone = await session.get(Professional, w["colleague"].id)
        gone.is_active = False  # inactive professional
        await session.commit()
    response = await client.get("/tenants/me/units", headers=HEADERS)
    assert response.status_code == 200
    counts = {row["id"]: row["professionals_count"] for row in response.json()}
    assert counts == {
        str(w["centro"].id): 1,  # colleague is inactive
        str(w["norte"].id): 1,
        str(w["sul"].id): 0,  # only an inactive link
        str(w["closed"].id): 0,
    }


async def test_units_list_count_ignores_other_tenants_professionals(world):
    w = world
    async with w["db"]() as session:
        counts = await pu_service.count_professionals_by_unit(session, w["tenant"].id)
    assert counts == {}
    await _seed_link(w, w["foreign"], w["foreign_unit"])
    async with w["db"]() as session:
        assert await pu_service.count_professionals_by_unit(session, w["tenant"].id) == {}
        other = await pu_service.count_professionals_by_unit(session, w["other_tenant"].id)
    assert other == {w["foreign_unit"].id: 1}
