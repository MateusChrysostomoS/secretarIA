# ruff: noqa: F811
"""Hub API for the weekdays a service is blocked on (TASK-087, Task 2).

The wire contract the Portal was written against:

* an entry of the professional's `appointment_types` carries `blocked_weekdays` (the BASE);
* each item of `unit_terms` carries `blocked_weekdays` too (`null` = inherit the base, `[]` = own,
  nothing blocked, a list = own);
* a unit item is valid with ANY of price / duration / blocked_weekdays set;
* `apply_to_all` is per subject: it wipes price/duration everywhere and keeps the days, and it may
  now travel together with `unit_terms`;
* 422 `blocked_weekdays_invalid` for an unknown, repeated or all-seven set of days.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from sqlalchemy import select  # noqa: E402

from secretaria.models import Professional, ProfessionalServiceUnitTerms, Service  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401
from tests.test_hub_unit_terms import (  # noqa: E402,F401
    HEADERS,
    _by_name,
    _entry,
    _list_doctor,
    _put,
    _seed_terms,
    _term,
    world,
)

ALL_DAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def _days_term(unit, price=None, duration=None, blocked=None) -> dict:
    return {**_term(unit, price=price, duration=duration), "blocked_weekdays": blocked}


async def _rows_full(w) -> dict:
    """{(service_key, unit_key): (price, duration, blocked)} straight from the database."""
    names = {w["consulta"].id: "consulta", w["retorno"].id: "retorno"}
    unit_names = {w[k].id: k for k in ("sede", "centro", "norte", "inactive")}
    async with w["db"]() as session:
        rows = await session.scalars(select(ProfessionalServiceUnitTerms))
        return {
            (names[r.service_id], unit_names.get(r.unit_id, "?")): (
                r.price,
                r.duration_min,
                r.blocked_weekdays,
            )
            for r in rows
        }


async def _stored_entry(w, key: str) -> dict:
    async with w["db"]() as session:
        doctor = await session.get(Professional, w["doctor"].id)
        service_id = str(w[key].id)
        return next(e for e in doctor.appointment_types if e.get("service_id") == service_id)


# ---------------------------------------------------------------- base days


async def test_put_base_days_persist_and_read_back(client, world):
    w = world
    response = await _put(
        client,
        w,
        [_entry(w, "consulta", blocked_weekdays=["monday"]), _entry(w, "retorno")],
    )
    assert response.status_code == 200, response.text
    assert _by_name(response.json()["professional"])["Consulta"]["blocked_weekdays"] == ["monday"]
    doctor = await _list_doctor(client, w)
    assert _by_name(doctor)["Consulta"]["blocked_weekdays"] == ["monday"]
    assert "blocked_weekdays" not in _by_name(doctor)["Retorno"]  # absent = nothing blocked


async def test_base_days_are_stored_sorted_monday_to_sunday_and_null_is_empty(client, world):
    w = world
    response = await _put(
        client,
        w,
        [
            _entry(w, "consulta", blocked_weekdays=["sunday", "monday", "friday"]),
            _entry(w, "retorno", blocked_weekdays=None),
        ],
    )
    assert response.status_code == 200, response.text
    assert (await _stored_entry(w, "consulta"))["blocked_weekdays"] == [
        "monday",
        "friday",
        "sunday",
    ]
    assert (await _stored_entry(w, "retorno"))["blocked_weekdays"] == []


# ---------------------------------------------------------------- per unit


async def test_put_unit_days_only_row(client, world):
    w = world
    response = await _put(
        client,
        w,
        [_entry(w, "consulta", unit_terms=[_days_term(w["sede"], blocked=["friday"])])],
    )
    assert response.status_code == 200, response.text
    assert await _rows_full(w) == {("consulta", "sede"): (None, None, ["friday"])}
    item = _by_name(response.json()["professional"])["Consulta"]["unit_terms"][0]
    assert item == {
        "unit_id": str(w["sede"].id),
        "price": None,
        "duration_min": None,
        "blocked_weekdays": ["friday"],
    }
    # the entry stays free of wire-only fields
    assert "unit_terms" not in await _stored_entry(w, "consulta")


async def test_empty_list_is_an_own_row_that_unblocks(client, world):
    w = world
    response = await _put(
        client,
        w,
        [
            _entry(
                w,
                "consulta",
                blocked_weekdays=["monday"],
                unit_terms=[_days_term(w["sede"], blocked=[])],
            )
        ],
    )
    assert response.status_code == 200, response.text
    assert await _rows_full(w) == {("consulta", "sede"): (None, None, [])}
    item = _by_name(response.json()["professional"])["Consulta"]["unit_terms"][0]
    assert item["blocked_weekdays"] == []


async def test_item_without_the_key_reads_back_as_null(client, world):
    w = world
    response = await _put(
        client,
        w,
        [_entry(w, "consulta", unit_terms=[_term(w["sede"], price="R$ 250,00")])],
    )
    assert response.status_code == 200, response.text
    item = _by_name(response.json()["professional"])["Consulta"]["unit_terms"][0]
    assert item["blocked_weekdays"] is None
    assert await _rows_full(w) == {("consulta", "sede"): ("R$ 250,00", None, None)}


async def test_item_with_nothing_set_is_still_refused(client, world):
    w = world
    response = await _put(client, w, [_entry(w, "consulta", unit_terms=[_term(w["sede"])])])
    assert response.status_code == 422
    assert await _rows_full(w) == {}


async def test_days_save_keeps_unit_price(client, world):
    w = world
    await _seed_terms(w, ("consulta", "sede", "R$ 250,00", None))
    response = await _put(
        client,
        w,
        [
            _entry(
                w,
                "consulta",
                unit_terms=[_days_term(w["sede"], price="R$ 250,00", blocked=["friday"])],
            )
        ],
    )
    assert response.status_code == 200, response.text
    assert await _rows_full(w) == {("consulta", "sede"): ("R$ 250,00", None, ["friday"])}


async def test_unit_days_are_stored_sorted(client, world):
    w = world
    response = await _put(
        client,
        w,
        [
            _entry(
                w,
                "consulta",
                unit_terms=[_days_term(w["sede"], blocked=["sunday", "tuesday"])],
            )
        ],
    )
    assert response.status_code == 200, response.text
    assert await _rows_full(w) == {("consulta", "sede"): (None, None, ["tuesday", "sunday"])}


# ---------------------------------------------------------------- apply_to_all (per subject)


async def test_apply_to_all_keeps_days(client, world):
    w = world
    await _seed_terms(
        w,
        ("consulta", "sede", "R$ 250,00", None),
        ("consulta", "centro", "R$ 300,00", None),
    )
    async with w["db"]() as session:
        sede = await session.scalar(
            select(ProfessionalServiceUnitTerms).where(
                ProfessionalServiceUnitTerms.unit_id == w["sede"].id
            )
        )
        sede.blocked_weekdays = ["friday"]
        await session.commit()

    response = await _put(client, w, [_entry(w, "consulta", apply_to_all=True)])
    assert response.status_code == 200, response.text
    # Sede keeps its own days but loses the price; Centro had nothing but a price: gone.
    assert await _rows_full(w) == {("consulta", "sede"): (None, None, ["friday"])}


async def test_apply_to_all_erases_price_of_deactivated_units_too_but_keeps_their_days(
    client, world
):
    w = world
    async with w["db"]() as session:
        session.add(
            ProfessionalServiceUnitTerms(
                professional_id=w["doctor"].id,
                service_id=w["consulta"].id,
                unit_id=w["inactive"].id,
                tenant_id=w["tenant"].id,
                price="R$ 2,00",
                duration_min=45,
                blocked_weekdays=["monday"],
            )
        )
        await session.commit()
    response = await _put(client, w, [_entry(w, "consulta", apply_to_all=True)])
    assert response.status_code == 200, response.text
    assert await _rows_full(w) == {("consulta", "inactive"): (None, None, ["monday"])}


async def test_apply_to_all_does_not_touch_another_service(client, world):
    w = world
    await _seed_terms(w, ("retorno", "sede", "R$ 7,00", None))
    response = await _put(
        client, w, [_entry(w, "consulta", apply_to_all=True), _entry(w, "retorno")]
    )
    assert response.status_code == 200, response.text
    assert await _rows_full(w) == {("retorno", "sede"): ("R$ 7,00", None, None)}


async def test_apply_to_all_with_unit_terms(client, world):
    w = world
    response = await _put(
        client,
        w,
        [
            _entry(
                w,
                "consulta",
                apply_to_all=True,
                unit_terms=[_days_term(w["centro"], blocked=["monday"])],
            )
        ],
    )
    assert response.status_code == 200, response.text
    assert await _rows_full(w) == {("consulta", "centro"): (None, None, ["monday"])}


async def test_apply_to_all_with_unit_terms_repeating_a_unit_is_still_refused(client, world):
    w = world
    response = await _put(
        client,
        w,
        [
            _entry(
                w,
                "consulta",
                apply_to_all=True,
                unit_terms=[
                    _days_term(w["sede"], blocked=["monday"]),
                    _days_term(w["sede"], blocked=["friday"]),
                ],
            )
        ],
    )
    assert response.status_code == 422
    assert await _rows_full(w) == {}


# ---------------------------------------------------------------- validation (422)


def _assert_invalid(response) -> None:
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "blocked_weekdays_invalid"
    assert detail["message"]


async def test_invalid_base_days_are_422_with_a_stable_code(client, world):
    w = world
    for bad in (["funday"], ["monday", "monday"], list(ALL_DAYS)):
        response = await _put(client, w, [_entry(w, "consulta", blocked_weekdays=bad)])
        _assert_invalid(response)
    # nothing was saved
    assert "blocked_weekdays" not in await _stored_entry(w, "consulta")


async def test_invalid_unit_days_are_422_with_a_stable_code(client, world):
    w = world
    for bad in (["funday"], ["monday", "monday"], list(ALL_DAYS)):
        response = await _put(
            client,
            w,
            [_entry(w, "consulta", unit_terms=[_days_term(w["sede"], blocked=bad)])],
        )
        _assert_invalid(response)
    assert await _rows_full(w) == {}


async def test_six_days_are_fine(client, world):
    w = world
    response = await _put(client, w, [_entry(w, "consulta", blocked_weekdays=ALL_DAYS[:6])])
    assert response.status_code == 200, response.text


async def test_invalid_days_on_the_legacy_professional_endpoint_too(client, world):
    w = world
    url = f"/tenants/me/professionals/{w['doctor'].id}/config"
    response = await client.put(
        url,
        headers=HEADERS,
        json={"appointment_types": [_entry(w, "consulta", blocked_weekdays=["funday"])]},
    )
    _assert_invalid(response)
    ok = await client.put(
        url,
        headers=HEADERS,
        json={"appointment_types": [_entry(w, "consulta", blocked_weekdays=["monday"])]},
    )
    assert ok.status_code == 200, ok.text
    assert _by_name(ok.json())["Consulta"]["blocked_weekdays"] == ["monday"]


async def test_other_tenant_unit_still_refused(client, world):
    w = world
    response = await _put(
        client,
        w,
        [_entry(w, "consulta", unit_terms=[_days_term(w["foreign_unit"], blocked=["monday"])])],
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_unit_ids"
    assert await _rows_full(w) == {}


# ---------------------------------------------------------------- create service with `offer`


async def _create_service(client, w, name: str, **offer_extra):
    return await client.post(
        "/tenants/me/services",
        headers=HEADERS,
        json={
            "name": name,
            "offer": {
                "professional_id": str(w["doctor"].id),
                "duration_min": 60,
                "price": "R$ 900,00",
                **offer_extra,
            },
        },
    )


async def test_create_service_offer_with_days(client, world):
    w = world
    response = await _create_service(client, w, "Cirurgia", blocked_weekdays=["saturday", "monday"])
    assert response.status_code == 201, response.text
    entries = response.json()["professional"]["appointment_types"]
    created = next(e for e in entries if e["name"] == "Cirurgia")
    assert created["blocked_weekdays"] == ["monday", "saturday"]
    async with w["db"]() as session:
        doctor = await session.get(Professional, w["doctor"].id)
        stored = next(e for e in doctor.appointment_types if e["name"] == "Cirurgia")
    assert stored["blocked_weekdays"] == ["monday", "saturday"]


async def test_create_service_offer_without_days_is_unchanged(client, world):
    w = world
    response = await _create_service(client, w, "Exame")
    assert response.status_code == 201, response.text
    created = next(
        e for e in response.json()["professional"]["appointment_types"] if e["name"] == "Exame"
    )
    assert not created.get("blocked_weekdays")


async def test_create_service_offer_with_invalid_days_creates_nothing(client, world):
    w = world
    for bad in (["funday"], ["monday", "monday"], list(ALL_DAYS)):
        response = await _create_service(client, w, "Cirurgia", blocked_weekdays=bad)
        _assert_invalid(response)
    async with w["db"]() as session:
        assert (
            await session.scalar(
                select(Service).where(
                    Service.tenant_id == w["tenant"].id, Service.name == "Cirurgia"
                )
            )
            is None
        )


# ---------------------------------------------------------------- D1: absent key = keep


async def _put_legacy(client, w, entries):
    return await client.put(
        f"/tenants/me/professionals/{w['doctor'].id}/config",
        headers=HEADERS,
        json={"appointment_types": entries},
    )


async def test_put_without_the_key_keeps_the_stored_base_days(client, world):
    w = world
    first = [_entry(w, "consulta", blocked_weekdays=["monday"]), _entry(w, "retorno")]
    assert (await _put(client, w, first)).status_code == 200
    # a client that does not know the field (secretarIA-frontend, stale Portal tab)
    stale = [_entry(w, "consulta", price="R$ 210,00"), _entry(w, "retorno")]
    response = await _put(client, w, stale)
    assert response.status_code == 200, response.text
    assert (await _stored_entry(w, "consulta"))["blocked_weekdays"] == ["monday"]
    assert _by_name(await _list_doctor(client, w))["Consulta"]["blocked_weekdays"] == ["monday"]
    assert "blocked_weekdays" not in await _stored_entry(w, "retorno")
    # explicit null clears, a list replaces
    assert (
        await _put(client, w, [_entry(w, "consulta", blocked_weekdays=None), _entry(w, "retorno")])
    ).status_code == 200
    assert (await _stored_entry(w, "consulta"))["blocked_weekdays"] == []


async def test_put_without_the_key_keeps_days_on_the_legacy_route_too(client, world):
    w = world
    assert (
        await _put_legacy(client, w, [_entry(w, "consulta", blocked_weekdays=["friday"])])
    ).status_code == 200
    assert (await _put_legacy(client, w, [_entry(w, "consulta")])).status_code == 200
    assert (await _stored_entry(w, "consulta"))["blocked_weekdays"] == ["friday"]
    assert (
        await _put_legacy(client, w, [_entry(w, "consulta", blocked_weekdays=["monday"])])
    ).status_code == 200
    assert (await _stored_entry(w, "consulta"))["blocked_weekdays"] == ["monday"]


async def test_put_without_the_key_matches_by_name_when_there_is_no_service_id(client, world):
    w = world
    assert (
        await _put(client, w, [_entry(w, "consulta", blocked_weekdays=["monday"])])
    ).status_code == 200
    bare = _entry(w, "consulta")
    bare.pop("service_id")
    bare["name"] = "  consulta "
    # the entry now has no link, but the stored entry of the same name keeps its days
    async with w["db"]() as session:
        doctor = await session.get(Professional, w["doctor"].id)
        stored = [dict(e) for e in doctor.appointment_types]
    assert stored[0]["blocked_weekdays"] == ["monday"]
    from secretaria.services import hub_configuration as hubcfg

    class _P:
        appointment_types = stored

    hubcfg.apply_professional_config(_P, {"appointment_types": [bare]})
    assert _P.appointment_types[0]["blocked_weekdays"] == ["monday"]


async def test_inheriting_professional_saving_without_the_key_is_fine(client, world):
    w = world
    async with w["db"]() as session:
        doctor = await session.get(Professional, w["doctor"].id)
        doctor.appointment_types = None
        await session.commit()
    response = await _put(client, w, [_entry(w, "consulta")])
    assert response.status_code == 200, response.text
    assert "blocked_weekdays" not in await _stored_entry(w, "consulta")


# ---------------------------------------------------------------- F1: apply_to_all + unit_terms: []


async def test_apply_to_all_with_empty_unit_terms_clears_every_row(client, world):
    w = world
    await _seed_terms(w, ("consulta", "norte", "R$ 700,00", None))
    async with w["db"]() as session:
        session.add_all(
            [
                ProfessionalServiceUnitTerms(
                    professional_id=w["doctor"].id,
                    service_id=w["consulta"].id,
                    unit_id=w["centro"].id,
                    tenant_id=w["tenant"].id,
                    price=None,
                    duration_min=None,
                    blocked_weekdays=["friday"],
                ),
                ProfessionalServiceUnitTerms(
                    professional_id=w["doctor"].id,
                    service_id=w["consulta"].id,
                    unit_id=w["inactive"].id,
                    tenant_id=w["tenant"].id,
                    price="R$ 2,00",
                    duration_min=None,
                    blocked_weekdays=["monday"],
                ),
            ]
        )
        await session.commit()
    response = await _put(client, w, [_entry(w, "consulta", apply_to_all=True, unit_terms=[])])
    assert response.status_code == 200, response.text
    assert await _rows_full(w) == {("consulta", "inactive"): (None, None, ["monday"])}


async def test_apply_to_all_with_unit_terms_replaces_an_existing_row(client, world):
    w = world
    async with w["db"]() as session:
        session.add(
            ProfessionalServiceUnitTerms(
                professional_id=w["doctor"].id,
                service_id=w["consulta"].id,
                unit_id=w["centro"].id,
                tenant_id=w["tenant"].id,
                price="R$ 250,00",
                duration_min=None,
                blocked_weekdays=["friday"],
            )
        )
        await session.commit()
    response = await _put(
        client,
        w,
        [
            _entry(
                w,
                "consulta",
                apply_to_all=True,
                unit_terms=[_days_term(w["centro"], blocked=["monday"])],
            )
        ],
    )
    assert response.status_code == 200, response.text
    assert await _rows_full(w) == {("consulta", "centro"): (None, None, ["monday"])}


def test_keep_stored_days_name_fallback_only_without_service_id():
    """Re-review of D1: an incoming entry WITH a service_id never borrows, by name, the days of a
    DIFFERENT stored entry; the name fallback is only for entries without a service_id."""
    from uuid import uuid4

    from secretaria.services.hub_unit_terms import keep_stored_blocked_weekdays

    s1, s2 = str(uuid4()), str(uuid4())
    stored = [
        {"name": "Consulta", "service_id": s1},  # no days of its own
        {"name": "Consulta", "service_id": s2, "blocked_weekdays": ["monday"]},
    ]
    out = keep_stored_blocked_weekdays([{"name": "Consulta", "service_id": s1}], stored)
    assert "blocked_weekdays" not in out[0]

    legacy = keep_stored_blocked_weekdays(
        [{"name": " consulta "}], [{"name": "Consulta", "blocked_weekdays": ["friday"]}]
    )
    assert legacy[0]["blocked_weekdays"] == ["friday"]
