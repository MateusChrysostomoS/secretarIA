# ruff: noqa: F811
"""Hub API for price/duration per professional x service x unit (TASK-085 T3).

`unit_terms` rides in each entry of the professional's service list (read and write, same save),
`apply_to_all` is rule A (writing the base erases the exceptions), and PUT .../units prunes the
exceptions of a unit the professional stopped attending (contract: TASK-085 inventory, section 4).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.api.hub import deps as hub_deps  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import (  # noqa: E402
    Professional,
    ProfessionalServiceUnit,
    ProfessionalServiceUnitTerms,
    ProfessionalUnit,
    Service,
    Unit,
)
from tests._local_agenda import seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

HEADERS = {"authorization": "Bearer ut-test-token"}
CONFIGURATION = "/tenants/me/configuration"


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
        consulta = _service(tenant.id, "Consulta")
        retorno = _service(tenant.id, "Retorno")
        foreign_service = _service(other.id, "Consulta")
        session.add_all([consulta, retorno, foreign_service])
        await session.flush()
        doctor = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[
                {
                    "name": "Consulta",
                    "service_id": str(consulta.id),
                    "duration_min": 30,
                    "price": "R$ 200,00",
                    "is_active": True,
                },
                {
                    "name": "Retorno",
                    "service_id": str(retorno.id),
                    "duration_min": 20,
                    "price": "R$ 100,00",
                    "is_active": True,
                },
            ],
        )
        foreign_doctor = Professional(id=uuid4(), tenant_id=other.id, name="Dr. B", is_active=True)
        units = {
            name: Unit(id=uuid4(), tenant_id=tenant.id, name=name, address=f"Rua {name}")
            for name in ("Sede", "Centro", "Norte")
        }
        inactive = Unit(
            id=uuid4(), tenant_id=tenant.id, name="Velha", address="Rua Velha", is_active=False
        )
        foreign_unit = Unit(id=uuid4(), tenant_id=other.id, name="Alheia", address="Rua Alheia")
        session.add_all([doctor, foreign_doctor, foreign_unit, inactive, *units.values()])
        await session.flush()
        for position, name in enumerate(("Sede", "Centro")):  # Norte is NOT linked
            session.add(
                ProfessionalUnit(
                    professional_id=doctor.id,
                    unit_id=units[name].id,
                    business_hours=None,
                    is_active=True,
                    sort_order=position,
                )
            )
        await session.commit()

    claim = {"tenant_id": tenant.id}

    async def _session():
        async with db() as session:
            yield session

    async def _verify(token, *, fresh=False):
        return SubscriptionClaim(
            tenant_id=claim["tenant_id"],
            active=True,
            role="manager",
            is_owner=False,
            is_manager=False,
            professional_id=None,
        )

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    yield {
        "db": db,
        "tenant": tenant,
        "other": other,
        "doctor": doctor,
        "foreign_doctor": foreign_doctor,
        "consulta": consulta,
        "retorno": retorno,
        "foreign_service": foreign_service,
        "sede": units["Sede"],
        "centro": units["Centro"],
        "norte": units["Norte"],
        "inactive": inactive,
        "foreign_unit": foreign_unit,
        "claim": claim,
    }
    app.dependency_overrides.pop(get_session, None)


def _entry(w, key: str, *, price=None, duration=None, **extra) -> dict:
    base = {
        "consulta": {"name": "Consulta", "duration_min": 30, "price": "R$ 200,00"},
        "retorno": {"name": "Retorno", "duration_min": 20, "price": "R$ 100,00"},
    }[key]
    entry = {**base, "service_id": str(w[key].id), "is_active": True, **extra}
    if price is not None:
        entry["price"] = price
    if duration is not None:
        entry["duration_min"] = duration
    return entry


def _term(unit, price=None, duration=None) -> dict:
    return {"unit_id": str(unit.id), "price": price, "duration_min": duration}


async def _put(client, w, entries):
    return await client.put(
        CONFIGURATION,
        headers=HEADERS,
        json={
            "professional_id": str(w["doctor"].id),
            "professional": {"appointment_types": entries},
        },
    )


async def _rows(w) -> dict:
    """{(service_key, unit_key): (price, duration)} straight from the database."""
    names = {w["consulta"].id: "consulta", w["retorno"].id: "retorno"}
    unit_names = {w[k].id: k for k in ("sede", "centro", "norte", "inactive")}
    async with w["db"]() as session:
        rows = await session.scalars(select(ProfessionalServiceUnitTerms))
        return {
            (names[r.service_id], unit_names.get(r.unit_id, "?")): (r.price, r.duration_min)
            for r in rows
        }


async def _seed_terms(w, *items):
    """items: (service_key, unit_key, price, duration)."""
    async with w["db"]() as session:
        for service_key, unit_key, price, duration in items:
            session.add(
                ProfessionalServiceUnitTerms(
                    professional_id=w["doctor"].id,
                    service_id=w[service_key].id,
                    unit_id=w[unit_key].id,
                    tenant_id=w["tenant"].id,
                    price=price,
                    duration_min=duration,
                )
            )
        await session.commit()


def _by_name(payload) -> dict:
    return {e["name"]: e for e in payload["appointment_types"]}


async def _list_doctor(client, w) -> dict:
    response = await client.get("/tenants/me/professionals", headers=HEADERS)
    assert response.status_code == 200, response.text
    return next(p for p in response.json() if p["id"] == str(w["doctor"].id))


# ---------------------------------------------------------------- read


async def test_read_has_an_empty_unit_terms_on_every_entry_without_exceptions(client, world):
    doctor = await _list_doctor(client, world)
    assert all(e["unit_terms"] == [] for e in doctor["appointment_types"])
    assert [e["price"] for e in doctor["appointment_types"]] == ["R$ 200,00", "R$ 100,00"]


async def test_read_returns_the_exceptions_in_link_order(client, world):
    w = world
    await _seed_terms(w, ("consulta", "centro", "R$ 250,00", None), ("consulta", "sede", None, 40))
    doctor = await _list_doctor(client, w)
    assert _by_name(doctor)["Consulta"]["unit_terms"] == [
        {"unit_id": str(w["sede"].id), "price": None, "duration_min": 40, "blocked_weekdays": None},
        {
            "unit_id": str(w["centro"].id),
            "price": "R$ 250,00",
            "duration_min": None,
            "blocked_weekdays": None,
        },
    ]
    assert _by_name(doctor)["Retorno"]["unit_terms"] == []


async def test_read_hides_exceptions_of_a_unit_the_doctor_no_longer_attends(client, world):
    w = world
    await _seed_terms(w, ("consulta", "norte", "R$ 999,00", None))  # Norte: not linked
    doctor = await _list_doctor(client, w)
    assert _by_name(doctor)["Consulta"]["unit_terms"] == []
    assert await _rows(w) == {("consulta", "norte"): ("R$ 999,00", None)}  # kept, just hidden


# ---------------------------------------------------------------- write (/configuration)


async def test_write_saves_exceptions_in_the_same_save_and_echoes_them(client, world):
    w = world
    response = await _put(
        client,
        w,
        [
            _entry(
                w,
                "consulta",
                unit_terms=[_term(w["centro"], price="R$ 250,00"), _term(w["sede"], duration=45)],
            ),
            _entry(w, "retorno"),
        ],
    )
    assert response.status_code == 200, response.text
    echoed = _by_name(response.json()["professional"])["Consulta"]
    assert echoed["price"] == "R$ 200,00" and echoed["duration_min"] == 30  # base untouched
    assert [t["unit_id"] for t in echoed["unit_terms"]] == [str(w["sede"].id), str(w["centro"].id)]
    assert await _rows(w) == {
        ("consulta", "centro"): ("R$ 250,00", None),
        ("consulta", "sede"): (None, 45),
    }


async def test_unit_terms_never_land_in_the_professional_json(client, world):
    w = world
    response = await _put(
        client,
        w,
        [
            _entry(w, "consulta", unit_terms=[_term(w["sede"], price="R$ 1,00")]),
            _entry(w, "retorno"),
        ],
    )
    assert response.status_code == 200, response.text
    async with w["db"]() as session:
        stored = (await session.get(Professional, w["doctor"].id)).appointment_types
    assert all("unit_terms" not in e and "apply_to_all" not in e for e in stored)


async def test_price_is_stripped_and_blank_means_none(client, world):
    w = world
    response = await _put(
        client,
        w,
        [
            _entry(w, "consulta", unit_terms=[_term(w["sede"], price="  R$ 250,00 ", duration=35)]),
            _entry(w, "retorno", unit_terms=[_term(w["sede"], price="   ", duration=15)]),
        ],
    )
    assert response.status_code == 200, response.text
    assert await _rows(w) == {
        ("consulta", "sede"): ("R$ 250,00", 35),
        ("retorno", "sede"): (None, 15),
    }


async def test_an_array_replaces_the_set_and_an_empty_array_clears_it(client, world):
    w = world
    await _seed_terms(w, ("consulta", "sede", "R$ 1,00", None), ("consulta", "centro", None, 50))
    response = await _put(
        client,
        w,
        [
            _entry(w, "consulta", unit_terms=[_term(w["centro"], price="R$ 9,00")]),
            _entry(w, "retorno"),
        ],
    )
    assert response.status_code == 200, response.text
    assert await _rows(w) == {("consulta", "centro"): ("R$ 9,00", None)}
    response = await _put(client, w, [_entry(w, "consulta", unit_terms=[]), _entry(w, "retorno")])
    assert response.status_code == 200, response.text
    assert await _rows(w) == {}


async def test_absent_or_null_unit_terms_leave_the_exceptions_alone(client, world):
    w = world
    await _seed_terms(w, ("consulta", "sede", "R$ 1,00", None))
    for entries in (
        [_entry(w, "consulta"), _entry(w, "retorno")],
        [_entry(w, "consulta", unit_terms=None), _entry(w, "retorno")],
    ):
        response = await _put(client, w, entries)
        assert response.status_code == 200, response.text
        assert await _rows(w) == {("consulta", "sede"): ("R$ 1,00", None)}


async def test_apply_to_all_writes_the_base_and_erases_every_exception(client, world):
    w = world
    await _seed_terms(
        w,
        ("consulta", "sede", "R$ 1,00", None),
        ("consulta", "centro", None, 50),
        ("consulta", "inactive", "R$ 2,00", None),  # "Todas" is all, even a deactivated unit
        ("retorno", "sede", "R$ 7,00", None),  # another service: untouched
    )
    response = await _put(
        client,
        w,
        [
            _entry(w, "consulta", price="R$ 300,00", duration=40, apply_to_all=True),
            _entry(w, "retorno"),
        ],
    )
    assert response.status_code == 200, response.text
    echoed = _by_name(response.json()["professional"])["Consulta"]
    assert (echoed["price"], echoed["duration_min"], echoed["unit_terms"]) == ("R$ 300,00", 40, [])
    assert await _rows(w) == {("retorno", "sede"): ("R$ 7,00", None)}


async def test_apply_to_all_with_exceptions_erases_first_then_writes_them(client, world):
    # TASK-087 relaxed the 085 refusal: "Todas as unidades" is per subject and may travel with
    # `unit_terms` (the erase runs first, then the list is applied).
    w = world
    await _seed_terms(w, ("consulta", "centro", "R$ 9,00", None))
    response = await _put(
        client,
        w,
        [_entry(w, "consulta", apply_to_all=True, unit_terms=[_term(w["sede"], price="R$ 1,00")])],
    )
    assert response.status_code == 200, response.text
    assert await _rows(w) == {("consulta", "sede"): ("R$ 1,00", None)}


async def test_apply_to_all_with_an_empty_unit_terms_is_fine(client, world):
    w = world
    await _seed_terms(w, ("consulta", "sede", "R$ 1,00", None))
    response = await _put(client, w, [_entry(w, "consulta", apply_to_all=True, unit_terms=[])])
    assert response.status_code == 200, response.text
    assert await _rows(w) == {}


async def test_removing_an_entry_removes_its_exceptions_but_switching_it_off_keeps_them(
    client, world
):
    w = world
    await _seed_terms(
        w, ("consulta", "sede", "R$ 1,00", None), ("retorno", "sede", "R$ 2,00", None)
    )
    # Retorno switched off: kept.
    response = await _put(
        client, w, [_entry(w, "consulta"), {**_entry(w, "retorno"), "is_active": False}]
    )
    assert response.status_code == 200, response.text
    assert len(await _rows(w)) == 2
    # Retorno gone from the list: its exceptions go with it.
    response = await _put(client, w, [_entry(w, "consulta")])
    assert response.status_code == 200, response.text
    assert await _rows(w) == {("consulta", "sede"): ("R$ 1,00", None)}


async def test_a_name_matched_entry_without_service_id_keeps_its_exceptions(client, world):
    w = world
    await _seed_terms(w, ("consulta", "sede", "R$ 1,00", None))
    legacy = {"name": "Consulta", "duration_min": 30, "price": "R$ 200,00", "is_active": True}
    response = await _put(client, w, [legacy])
    assert response.status_code == 200, response.text
    assert await _rows(w) == {("consulta", "sede"): ("R$ 1,00", None)}


async def test_inactive_unit_in_the_body_is_ignored(client, world):
    w = world
    response = await _put(
        client,
        w,
        [
            _entry(
                w,
                "consulta",
                unit_terms=[
                    _term(w["inactive"], price="R$ 5,00"),
                    _term(w["sede"], price="R$ 6,00"),
                ],
            )
        ],
    )
    assert response.status_code == 200, response.text
    assert await _rows(w) == {("consulta", "sede"): ("R$ 6,00", None)}


# ---------------------------------------------------------------- validation (422)


async def _assert_refused(client, w, entries, status=422):
    before = await _rows(w)
    response = await _put(client, w, entries)
    assert response.status_code == status, response.text
    assert await _rows(w) == before
    async with w["db"]() as session:  # atomic: the base was not saved either
        stored = (await session.get(Professional, w["doctor"].id)).appointment_types
    assert [e["price"] for e in stored] == ["R$ 200,00", "R$ 100,00"]
    return response.json()


async def test_an_exception_with_neither_price_nor_duration_is_refused(client, world):
    w = world
    for term in (_term(w["sede"]), _term(w["sede"], price="  ")):
        await _assert_refused(
            client, w, [_entry(w, "consulta", price="R$ 999,00", unit_terms=[term])]
        )


async def test_duration_and_price_limits_match_the_base(client, world):
    w = world
    for term in (
        _term(w["sede"], duration=0),
        _term(w["sede"], duration=601),
        _term(w["sede"], price="x" * 41),
    ):
        await _assert_refused(
            client, w, [_entry(w, "consulta", price="R$ 999,00", unit_terms=[term])]
        )


async def test_a_repeated_unit_is_refused(client, world):
    w = world
    await _assert_refused(
        client,
        w,
        [
            _entry(
                w,
                "consulta",
                price="R$ 999,00",
                unit_terms=[_term(w["sede"], price="R$ 1,00"), _term(w["sede"], duration=10)],
            )
        ],
    )


async def test_unit_terms_on_an_entry_without_service_id_is_refused(client, world):
    w = world
    body = await _assert_refused(
        client,
        w,
        [
            {
                "name": "Avulso",
                "duration_min": 10,
                "price": "R$ 999,00",
                "unit_terms": [_term(w["sede"], price="R$ 1,00")],
            }
        ],
    )
    assert body["detail"]["code"] == "unit_terms_without_service"
    assert body["detail"]["message"]


async def test_apply_to_all_on_an_entry_without_service_id_is_refused(client, world):
    w = world
    body = await _assert_refused(
        client,
        w,
        [{"name": "Avulso", "duration_min": 10, "price": "R$ 999,00", "apply_to_all": True}],
    )
    assert body["detail"]["code"] == "unit_terms_without_service"


async def test_unknown_malformed_or_foreign_units_are_all_the_same_422(client, world):
    w = world
    for bad in (str(uuid4()), "not-a-uuid", str(w["foreign_unit"].id)):
        body = await _assert_refused(
            client,
            w,
            [
                _entry(
                    w,
                    "consulta",
                    price="R$ 999,00",
                    unit_terms=[{"unit_id": bad, "price": "R$ 1,00", "duration_min": None}],
                )
            ],
        )
        assert body["detail"]["code"] == "unknown_unit_ids"
        assert body["detail"]["unit_ids"] == [bad]


async def test_a_unit_the_doctor_does_not_attend_is_refused(client, world):
    w = world
    body = await _assert_refused(
        client,
        w,
        [_entry(w, "consulta", price="R$ 999,00", unit_terms=[_term(w["norte"], price="R$ 1,00")])],
    )
    detail = body["detail"]
    assert detail["code"] == "unit_not_linked"
    assert detail["unit_ids"] == [str(w["norte"].id)]
    assert detail["service_ids"] == [str(w["consulta"].id)]


async def test_a_service_restricted_to_other_units_is_refused(client, world):
    w = world
    async with w["db"]() as session:  # Consulta is done only at Sede
        session.add(
            ProfessionalServiceUnit(
                professional_id=w["doctor"].id, service_id=w["consulta"].id, unit_id=w["sede"].id
            )
        )
        await session.commit()
    body = await _assert_refused(
        client,
        w,
        [
            _entry(
                w, "consulta", price="R$ 999,00", unit_terms=[_term(w["centro"], price="R$ 1,00")]
            )
        ],
    )
    assert body["detail"]["code"] == "service_not_at_unit"
    assert body["detail"]["unit_ids"] == [str(w["centro"].id)]
    # ...but Sede, where it is done, is fine.
    response = await _put(
        client, w, [_entry(w, "consulta", unit_terms=[_term(w["sede"], price="R$ 1,00")])]
    )
    assert response.status_code == 200, response.text


async def test_a_service_of_another_clinic_is_refused_as_before(client, world):
    w = world
    response = await _put(
        client, w, [{**_entry(w, "consulta"), "service_id": str(w["foreign_service"].id)}]
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_service_ids"


# ---------------------------------------------------------------- legacy route


async def test_legacy_professional_config_route_writes_and_reads_the_same_way(client, world):
    w = world
    url = f"/tenants/me/professionals/{w['doctor'].id}/config"
    response = await client.put(
        url,
        headers=HEADERS,
        json={
            "appointment_types": [
                _entry(w, "consulta", unit_terms=[_term(w["centro"], price="R$ 250,00")]),
                _entry(w, "retorno"),
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert _by_name(response.json())["Consulta"]["unit_terms"] == [
        {
            "unit_id": str(w["centro"].id),
            "price": "R$ 250,00",
            "duration_min": None,
            "blocked_weekdays": None,
        }
    ]
    response = await client.put(
        url,
        headers=HEADERS,
        json={"appointment_types": [_entry(w, "consulta", price="R$ 300,00", apply_to_all=True)]},
    )
    assert response.status_code == 200, response.text
    assert await _rows(w) == {}
    # Validation holds there too, and nothing is written.
    response = await client.put(
        url,
        headers=HEADERS,
        json={
            "appointment_types": [
                _entry(w, "consulta", unit_terms=[_term(w["norte"], price="R$ 1,00")])
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unit_not_linked"
    assert await _rows(w) == {}


# ---------------------------------------------------------------- PUT /professionals/{id}/units


def _units_url(w) -> str:
    return f"/tenants/me/professionals/{w['doctor'].id}/units"


def _link(unit) -> dict:
    return {"unit_id": str(unit.id), "business_hours": None}


async def test_unlinking_a_unit_erases_its_exceptions_only(client, world):
    w = world
    await _seed_terms(
        w,
        ("consulta", "sede", "R$ 1,00", None),
        ("consulta", "centro", "R$ 2,00", None),
        ("retorno", "centro", None, 15),
    )
    response = await client.put(_units_url(w), headers=HEADERS, json={"links": [_link(w["sede"])]})
    assert response.status_code == 200, response.text
    assert await _rows(w) == {("consulta", "sede"): ("R$ 1,00", None)}


async def test_keeping_the_links_keeps_the_exceptions(client, world):
    w = world
    await _seed_terms(w, ("consulta", "centro", "R$ 2,00", None))
    response = await client.put(
        _units_url(w), headers=HEADERS, json={"links": [_link(w["sede"]), _link(w["centro"])]}
    )
    assert response.status_code == 200, response.text
    assert await _rows(w) == {("consulta", "centro"): ("R$ 2,00", None)}


async def test_a_mark_that_removes_the_service_from_a_unit_erases_that_exception(client, world):
    w = world
    await _seed_terms(
        w, ("consulta", "sede", "R$ 1,00", None), ("consulta", "centro", "R$ 2,00", None)
    )
    response = await client.put(
        _units_url(w),
        headers=HEADERS,
        json={
            "links": [_link(w["sede"]), _link(w["centro"])],
            "service_marks": [
                {"service_id": str(w["consulta"].id), "unit_ids": [str(w["sede"].id)]}
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert await _rows(w) == {("consulta", "sede"): ("R$ 1,00", None)}


async def test_a_refused_units_save_erases_nothing(client, world):
    w = world
    await _seed_terms(w, ("consulta", "centro", "R$ 2,00", None))
    response = await client.put(
        _units_url(w),
        headers=HEADERS,
        json={
            "links": [_link(w["sede"])],
            "service_marks": [
                {"service_id": str(w["consulta"].id), "unit_ids": [str(w["centro"].id)]}
            ],
        },
    )
    assert response.status_code == 422  # unit_not_linked
    assert await _rows(w) == {("consulta", "centro"): ("R$ 2,00", None)}


# ---------------------------------------------------------------- tenant isolation


async def test_another_clinic_cannot_write_or_read_these_exceptions(client, world):
    w = world
    await _seed_terms(w, ("consulta", "sede", "R$ 1,00", None))
    w["claim"]["tenant_id"] = w["other"].id  # now acting as clinic B
    # Writing through A's professional id: 404, nothing changes.
    response = await client.put(
        CONFIGURATION,
        headers=HEADERS,
        json={
            "professional_id": str(w["doctor"].id),
            "professional": {
                "appointment_types": [
                    _entry(w, "consulta", unit_terms=[_term(w["sede"], price="R$ 0,01")])
                ]
            },
        },
    )
    assert response.status_code == 404
    response = await client.put(_units_url(w), headers=HEADERS, json={"links": []})
    assert response.status_code == 404
    assert await _rows(w) == {("consulta", "sede"): ("R$ 1,00", None)}
    # B's own list never carries A's exceptions.
    listing = await client.get("/tenants/me/professionals", headers=HEADERS)
    assert listing.status_code == 200
    assert [p["id"] for p in listing.json()] == [str(w["foreign_doctor"].id)]
    assert "R$ 1,00" not in listing.text


async def test_a_clinic_cannot_write_exceptions_on_another_clinics_unit(client, world):
    w = world
    w["claim"]["tenant_id"] = w["other"].id
    response = await client.put(
        CONFIGURATION,
        headers=HEADERS,
        json={
            "professional_id": str(w["foreign_doctor"].id),
            "professional": {
                "appointment_types": [
                    {
                        "name": "Consulta",
                        "service_id": str(w["foreign_service"].id),
                        "duration_min": 30,
                        "unit_terms": [_term(w["sede"], price="R$ 1,00")],  # A's unit
                    }
                ]
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_unit_ids"
    assert await _rows(w) == {}
