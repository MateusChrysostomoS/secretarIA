# ruff: noqa: F811
"""Junção TASK-090 + TASK-091: WHO may save WHAT - the more restrictive rule wins.

Owner, 2026-11-25 (integration prompt): where TASK-090 and TASK-091 both decide who may save
something, the MORE RESTRICTIVE rule wins, with one test per pair that conflicted.

TASK-090 (Adendo 1) added hub writes - the payment rule of a convênio line, the "Outro" ON/OFF,
the convênio order of a doctor's matrix, deleting a service - some of them role-blind on purpose
("same gate as creating an Outro: any staff role"). TASK-091 T4 closed every write of the CLINIC
to the manager and every write of `/professionals/{id}/...` to the manager or that doctor himself.
Together:

* a clinic line (the clinic's "Outro" ON/OFF, a clinic line's payment rule, the clinic's convênio
  order - also when it is reached through a doctor's matrix, because that order is the clinic's
  ONE order) -> the manager only;
* a doctor's own line (his "Outro" ON/OFF, his own payment rule, C8) -> the manager or that doctor;
* everyone else (another doctor, a secretary) -> 403 `forbidden_role`, before any lookup.

Reuses TASK-091's `world` (real `get_current_tenant`, only brain-api's introspection faked).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.api.hub import deps as hub_deps  # noqa: E402
from secretaria.models import ProfessionalUnit, Service, Unit  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401
from tests.test_t091_role_guards import _forbidden, world  # noqa: E402,F401


def _passed_the_gate(response) -> bool:
    """The role gate let the caller through (the route may still 404/409 on the data)."""
    return response.status_code != 403


async def _call(world, method: str, path: str, body: dict | None = None):
    return await world["client"].request(
        method, path, json=body, headers={"authorization": "Bearer t091-role-token"}
    )


# -- the clinic's lines: the manager only -------------------------------------------------------

CLINIC_LINE_WRITES = [
    ("PUT", "/tenants/me/insurance-plans/{plan}/active", {"is_active": False}),
    ("PUT", "/tenants/me/insurance-plans/{plan}/payment-note", {"custom_payment_note": "x"}),
]


@pytest.mark.parametrize(("method", "path", "body"), CLINIC_LINE_WRITES)
async def test_a_clinic_convenio_line_is_written_by_the_manager_only(world, method, path, body):
    url = path.format(plan=uuid4())
    world["as_self_only"](world["a"].id)
    assert _forbidden(await _call(world, method, url, body))
    world["as_role"]("secretary")
    assert _forbidden(await _call(world, method, url, body))
    world["as_manager"]()
    assert _passed_the_gate(await _call(world, method, url, body))


async def test_the_clinic_convenio_order_through_a_doctors_matrix_is_the_managers(world):
    """TASK-090 C3 writes the clinic's ONE convênio order from a doctor's matrix: clinic data,
    so not even the doctor himself writes it (TASK-091: a self-only doctor writes nothing of
    the clinic)."""
    body = {"plan_ids": []}
    own = f"/tenants/me/professionals/{world['a'].id}/insurance-acceptance/plan-order"
    world["as_self_only"](world["a"].id)
    assert _forbidden(await _call(world, "PUT", own, body))
    world["as_role"]("secretary")
    assert _forbidden(await _call(world, "PUT", own, body))
    world["as_manager"]()
    assert _passed_the_gate(await _call(world, "PUT", own, body))


# -- a doctor's own lines: the manager or that doctor himself -----------------------------------

DOCTOR_LINE_WRITES = [
    ("PUT", "/tenants/me/professionals/{p}/insurance-plans/{plan}/active", {"is_active": False}),
    (
        "PUT",
        "/tenants/me/professionals/{p}/insurance-plans/{plan}/payment-note",
        {"custom_payment_note": "paga e pede reembolso"},
    ),
]


@pytest.mark.parametrize(("method", "path", "body"), DOCTOR_LINE_WRITES)
async def test_a_doctors_own_convenio_line_is_his_or_the_managers(world, method, path, body):
    on_b = path.format(p=world["b"].id, plan=uuid4())
    world["as_self_only"](world["a"].id)  # another doctor
    assert _forbidden(await _call(world, method, on_b, body))
    world["as_role"]("secretary")
    assert _forbidden(await _call(world, method, on_b, body))
    world["as_self_only"](world["b"].id)  # the doctor himself
    assert _passed_the_gate(await _call(world, method, on_b, body))
    world["as_manager"]()
    assert _passed_the_gate(await _call(world, method, on_b, body))


async def test_a_malformed_professional_id_reaches_nobody_for_an_unlinked_doctor(world):
    """deps pair: TASK-090 made "manager or own professional" fail closed for a None id, which
    is what TASK-091's `professional_writer` hands it for a malformed path id."""
    world["as_self_only"](None)
    response = await _call(
        world, "PUT", "/tenants/me/professionals/not-a-uuid/config", {"specialty": "Retina"}
    )
    assert _forbidden(response)


# -- services and units pairs -------------------------------------------------------------------


async def test_deleting_a_service_is_the_managers_only(world):
    path = f"/tenants/me/services/{world['consulta'].id}"
    world["as_self_only"](world["a"].id)
    assert _forbidden(await _call(world, "DELETE", path))
    world["as_role"]("secretary")
    assert _forbidden(await _call(world, "DELETE", path))
    async with world["db"]() as session:
        assert await session.get(Service, world["consulta"].id) is not None
    world["as_manager"]()
    assert (await _call(world, "DELETE", path)).status_code == 204


async def test_units_a_doctor_reads_only_his_own_and_the_manager_links_two_without_an_addon(
    world,
):
    """professional_units pair: TASK-091 - doctor A never reads doctor B's units; TASK-090 -
    units are always available, so linking a doctor to 2 units needs no multi-unit add-on."""
    world["as_self_only"](world["a"].id)
    assert _forbidden(await _call(world, "GET", f"/tenants/me/professionals/{world['b'].id}/units"))
    async with world["db"]() as session:
        sul = Unit(id=uuid4(), tenant_id=world["tenant"].id, name="Sul", address="Rua B")
        session.add(sul)
        await session.commit()
    world["as_manager"]()
    links = {"links": [{"unit_id": str(world["centro"].id)}, {"unit_id": str(sul.id)}]}
    path = f"/tenants/me/professionals/{world['a'].id}/units"
    response = await _call(world, "PUT", path, links)
    assert response.status_code == 200, response.text
    async with world["db"]() as session:
        linked = set(
            await session.scalars(
                select(ProfessionalUnit.unit_id).where(
                    ProfessionalUnit.professional_id == world["a"].id
                )
            )
        )
    assert linked == {world["centro"].id, sul.id}


# -- structural guard: no hub write route without a role gate ----------------------------------

GATES = {
    hub_deps.manager_only,
    hub_deps.professional_writer,
    hub_deps.require_clinic_manager,
    hub_deps.require_clinic_manager_or_own_professional,
    hub_deps.require_clinic_clinician,
}

# Write routes that decide the role INSIDE the handler, or follow a model of their own that is
# not "who configures the clinic" (the agenda by agenda scope, conversations, the simulator...).
# A new write route must declare a gate or be listed here WITH its reason.
DECIDED_ELSEWHERE = {
    ("PUT", "/tenants/me/configuration"): "_require_configuration_roles: clinic half = manager, "
    "professional half = manager or self (TASK-091 T4)",
    ("PUT", "/tenants/me/professionals/{professional_id}/units"): "handler: "
    "ensure_clinic_manager_or_own_professional",
    ("POST", "/tenants/me/simulation/turn"): "the doctor's 'Testar': no side effect on any patient",
}
DECIDED_ELSEWHERE_PREFIXES = {
    "/tenants/me/calendar/": "the agenda: every role books, scope from brain-api (TASK-044 R7)",
    "/tenants/me/conversations/": "the console: every staff role answers patients",
    "/tenants/me/release-offers/": "released slots: every role in the agenda scope (TASK-052)",
}


def _dependency_calls(dependant):
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from _dependency_calls(dependency)


def test_every_hub_write_route_declares_a_role_gate():
    from secretaria.main import app

    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/tenants/me"):
            continue
        for method in sorted(route.methods & {"POST", "PUT", "PATCH", "DELETE"}):
            if (method, route.path) in DECIDED_ELSEWHERE or any(
                route.path.startswith(prefix) for prefix in DECIDED_ELSEWHERE_PREFIXES
            ):
                continue
            if not GATES & set(_dependency_calls(route.dependant)):
                missing.append((method, route.path))
    assert missing == []
