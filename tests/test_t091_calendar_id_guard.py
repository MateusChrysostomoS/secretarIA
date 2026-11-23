# ruff: noqa: F811
"""TASK-091 review (M1): a doctor can never point his agenda at another doctor's calendar.

The Portal never sends `google_calendar_id`: the system writes it (the secondary calendar it
creates in shared_account mode). So:

* a doctor who is not the manager may NOT send `google_calendar_id` at all, on any of the three
  writes that accept it (PATCH, PUT config, PUT /configuration) -> 403 `forbidden_role`, nothing
  saved;
* nobody - the manager included - may give a professional a calendar id another professional of
  the same clinic already uses -> 409 `calendar_in_use`, nothing saved.
"""

import pytest  # noqa: E402

from secretaria.models import Professional  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401
from tests.test_t091_role_guards import _forbidden, world  # noqa: E402,F401

pytestmark = pytest.mark.asyncio

HEADERS = {"authorization": "Bearer t091-role-token"}


async def _calendar_of(world, professional) -> str | None:
    async with world["db"]() as session:
        return (await session.get(Professional, professional.id)).google_calendar_id


async def _give_calendar(world, professional, calendar_id: str) -> None:
    async with world["db"]() as session:
        row = await session.get(Professional, professional.id)
        row.google_calendar_id = calendar_id
        await session.commit()


async def test_self_only_doctor_cannot_patch_own_calendar_id(world):
    await _give_calendar(world, world["b"], "bruno-cal@group.calendar.google.com")
    response = await world["client"].patch(
        f"/tenants/me/professionals/{world['a'].id}",
        json={"google_calendar_id": "bruno-cal@group.calendar.google.com"},
        headers=HEADERS,
    )
    assert _forbidden(response)
    assert await _calendar_of(world, world["a"]) is None


async def test_self_only_doctor_cannot_put_config_calendar_id(world):
    response = await world["client"].put(
        f"/tenants/me/professionals/{world['a'].id}/config",
        json={"google_calendar_id": "primary"},
        headers=HEADERS,
    )
    assert _forbidden(response)
    assert await _calendar_of(world, world["a"]) is None


async def test_self_only_doctor_cannot_send_calendar_id_in_configuration(world):
    response = await world["client"].put(
        "/tenants/me/configuration",
        json={
            "professional_id": str(world["a"].id),
            "professional": {"google_calendar_id": "x@group.calendar.google.com"},
        },
        headers=HEADERS,
    )
    assert _forbidden(response)
    assert await _calendar_of(world, world["a"]) is None


async def test_self_only_doctor_still_saves_his_other_fields(world):
    response = await world["client"].put(
        f"/tenants/me/professionals/{world['a'].id}/config",
        json={"specialty": "Retina"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text


async def test_manager_cannot_reuse_another_professionals_calendar(world):
    world["as_manager"]()
    await _give_calendar(world, world["b"], "bruno-cal@group.calendar.google.com")
    for method, path, body in (
        (
            "PATCH",
            f"/tenants/me/professionals/{world['a'].id}",
            {"google_calendar_id": "bruno-cal@group.calendar.google.com"},
        ),
        (
            "PUT",
            f"/tenants/me/professionals/{world['a'].id}/config",
            {"google_calendar_id": " Bruno-Cal@group.calendar.google.com "},
        ),
        (
            "PUT",
            "/tenants/me/configuration",
            {
                "professional_id": str(world["a"].id),
                "professional": {"google_calendar_id": "bruno-cal@group.calendar.google.com"},
            },
        ),
    ):
        response = await world["client"].request(method, path, json=body, headers=HEADERS)
        assert response.status_code == 409, (path, response.text)
        assert response.json()["detail"]["code"] == "calendar_in_use"
    assert await _calendar_of(world, world["a"]) is None


async def test_manager_sets_a_free_calendar_and_keeps_his_own(world):
    world["as_manager"]()
    await _give_calendar(world, world["a"], "ana-cal@group.calendar.google.com")
    # Re-saving the SAME professional's own calendar is not "in use by another".
    same = await world["client"].patch(
        f"/tenants/me/professionals/{world['a'].id}",
        json={"google_calendar_id": "ana-cal@group.calendar.google.com"},
        headers=HEADERS,
    )
    assert same.status_code == 200, same.text
    free = await world["client"].patch(
        f"/tenants/me/professionals/{world['b'].id}",
        json={"google_calendar_id": "bruno-novo@group.calendar.google.com"},
        headers=HEADERS,
    )
    assert free.status_code == 200, free.text
    assert await _calendar_of(world, world["b"]) == "bruno-novo@group.calendar.google.com"


async def test_another_clinics_calendar_is_not_in_use_here(world):
    world["as_manager"]()
    await _give_calendar(world, world["foreign"], "alheio@group.calendar.google.com")
    response = await world["client"].patch(
        f"/tenants/me/professionals/{world['a'].id}",
        json={"google_calendar_id": "alheio@group.calendar.google.com"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
