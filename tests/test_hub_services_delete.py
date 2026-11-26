# ruff: noqa: F811
"""DELETE /tenants/me/services/{id} (TASK-090 B7, contract C2).

A manager deletes a catalog service for the whole clinic: the row goes, so do the rows that hang
off its id (unit restrictions, unit terms, convenio-matrix exceptions) and its entries in every
professional's JSON list and in the clinic's legacy list. Convenio plans and past appointments
are untouched. Another clinic's id is a 404; a doctor or secretary gets a 403.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from secretaria.api.hub import deps as hub_deps  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    InsuranceAcceptanceException,
    Professional,
    ProfessionalServiceUnit,
    ProfessionalServiceUnitTerms,
    Service,
    Tenant,
    TenantInsurancePlan,
    Unit,
)
from tests._local_agenda import seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

HEADERS = {"authorization": "Bearer ut-test-token"}


def _service(tenant_id, name: str, sort_order: int) -> Service:
    return Service(
        id=uuid4(),
        tenant_id=tenant_id,
        name=name,
        normalized_name=name.casefold(),
        is_active=True,
        sort_order=sort_order,
    )


def _entry(service: Service | None, name: str) -> dict:
    entry = {"name": name, "duration_min": 30, "price": "R$ 100,00", "is_active": True}
    if service is not None:
        entry["service_id"] = str(service.id)
    return entry


@pytest_asyncio.fixture
async def world(db, monkeypatch):
    from secretaria.main import app

    tenant = await seed_tenant(db, name="Clinica A")
    other = await seed_tenant(db, name="Clinica B")
    async with db() as session:
        consulta = _service(tenant.id, "Consulta", 0)
        retorno = _service(tenant.id, "Retorno", 1)
        exame = _service(tenant.id, "Exame", 2)
        foreign = _service(other.id, "Consulta", 0)
        session.add_all([consulta, retorno, exame, foreign])
        await session.flush()
        # Entry linked by id, and an entry that only has the name (case/spaces differ).
        doc_a = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[_entry(consulta, "Consulta"), _entry(retorno, "Retorno")],
        )
        doc_b = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dr. Bruno",
            is_active=False,
            appointment_types=[_entry(None, " CONSULTA "), _entry(exame, "Exame")],
        )
        # NULL list: inherits the clinic's legacy list kept on the tenant.
        doc_inherit = Professional(
            id=uuid4(), tenant_id=tenant.id, name="Dra. Clara", is_active=True
        )
        foreign_doc = Professional(
            id=uuid4(),
            tenant_id=other.id,
            name="Dr. Alheio",
            is_active=True,
            appointment_types=[_entry(foreign, "Consulta")],
        )
        unit = Unit(id=uuid4(), tenant_id=tenant.id, name="Sede", address="Rua Sede")
        foreign_unit = Unit(id=uuid4(), tenant_id=other.id, name="Alheia", address="Rua X")
        session.add_all([doc_a, doc_b, doc_inherit, foreign_doc, unit, foreign_unit])
        row = await session.get(Tenant, tenant.id)
        row.appointment_types = [
            _entry(consulta, "Consulta"),
            _entry(None, "Retorno"),
            _entry(exame, "Exame"),
        ]
        other_row = await session.get(Tenant, other.id)
        other_row.appointment_types = [_entry(foreign, "Consulta")]
        await session.flush()
        plan = TenantInsurancePlan(id=uuid4(), tenant_id=tenant.id, custom_name="Plano X")
        foreign_plan = TenantInsurancePlan(id=uuid4(), tenant_id=other.id, custom_name="Plano Y")
        session.add_all([plan, foreign_plan])
        await session.flush()
        session.add_all(
            [
                ProfessionalServiceUnit(
                    professional_id=doc_a.id, service_id=consulta.id, unit_id=unit.id
                ),
                ProfessionalServiceUnit(
                    professional_id=doc_a.id, service_id=retorno.id, unit_id=unit.id
                ),
                ProfessionalServiceUnitTerms(
                    professional_id=doc_a.id,
                    service_id=consulta.id,
                    unit_id=unit.id,
                    tenant_id=tenant.id,
                    price="R$ 300,00",
                ),
                ProfessionalServiceUnitTerms(
                    professional_id=doc_a.id,
                    service_id=retorno.id,
                    unit_id=unit.id,
                    tenant_id=tenant.id,
                    price="R$ 150,00",
                ),
                InsuranceAcceptanceException(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    service_id=consulta.id,
                    unit_id=unit.id,
                    tenant_plan_id=plan.id,
                ),
                InsuranceAcceptanceException(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    service_id=retorno.id,
                    unit_id=unit.id,
                    tenant_plan_id=plan.id,
                ),
                InsuranceAcceptanceException(
                    id=uuid4(),
                    tenant_id=other.id,
                    service_id=foreign.id,
                    unit_id=foreign_unit.id,
                    tenant_plan_id=foreign_plan.id,
                ),
                Appointment(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    google_event_id="evt-1",
                    appointment_type="Consulta",
                    professional_id=doc_a.id,
                ),
            ]
        )
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

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    yield {
        "db": db,
        "tenant": tenant,
        "other": other,
        "consulta": consulta,
        "retorno": retorno,
        "exame": exame,
        "foreign": foreign,
        "doc_a": doc_a,
        "doc_b": doc_b,
        "doc_inherit": doc_inherit,
        "foreign_doc": foreign_doc,
        "plan": plan,
        "claim": claim,
    }
    app.dependency_overrides.pop(get_session, None)


def _url(service: Service) -> str:
    return f"/tenants/me/services/{service.id}"


async def _names(w, model, pk) -> list[str]:
    async with w["db"]() as session:
        row = await session.get(model, pk)
        return [e["name"] for e in (row.appointment_types or [])]


async def _count(w, model, *where) -> int:
    async with w["db"]() as session:
        return len(list(await session.scalars(select(model).where(*where))))


async def test_manager_deletes_service_204(client, world):
    w = world
    response = await client.delete(_url(w["consulta"]), headers=HEADERS)
    assert response.status_code == 204
    assert response.content == b""
    async with w["db"]() as session:
        assert await session.get(Service, w["consulta"].id) is None
        assert await session.get(Service, w["retorno"].id) is not None
    again = await client.delete(_url(w["consulta"]), headers=HEADERS)
    assert again.status_code == 404


@pytest.mark.parametrize(
    ("role", "is_owner", "is_manager", "expected"),
    [
        ("doctor", False, False, 403),
        ("secretary", False, False, 403),
        ("secretary", True, True, 403),
        ("doctor", True, False, 204),
    ],
)
async def test_doctor_and_secretary_get_403(client, world, role, is_owner, is_manager, expected):
    w = world
    w["claim"].update(role=role, is_owner=is_owner, is_manager=is_manager)
    response = await client.delete(_url(w["consulta"]), headers=HEADERS)
    assert response.status_code == expected
    async with w["db"]() as session:
        still_there = await session.get(Service, w["consulta"].id) is not None
    assert still_there is (expected == 403)
    if expected == 403:
        assert await _names(w, Professional, w["doc_a"].id) == ["Consulta", "Retorno"]


async def test_foreign_tenant_service_404_and_untouched(client, world):
    w = world
    response = await client.delete(_url(w["foreign"]), headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "service_not_found"
    bad = await client.delete("/tenants/me/services/not-a-uuid", headers=HEADERS)
    assert bad.status_code == 404
    unknown = await client.delete(f"/tenants/me/services/{uuid4()}", headers=HEADERS)
    assert unknown.status_code == 404
    async with w["db"]() as session:
        assert await session.get(Service, w["foreign"].id) is not None
    assert await _names(w, Professional, w["foreign_doc"].id) == ["Consulta"]
    assert await _names(w, Tenant, w["other"].id) == ["Consulta"]
    assert await _count(w, InsuranceAcceptanceException) == 3


async def test_cascades_unit_terms_and_matrix_exceptions(client, world):
    w = world
    response = await client.delete(_url(w["consulta"]), headers=HEADERS)
    assert response.status_code == 204
    cid, rid = w["consulta"].id, w["retorno"].id
    psu, terms = ProfessionalServiceUnit, ProfessionalServiceUnitTerms
    exceptions = InsuranceAcceptanceException
    assert await _count(w, psu, psu.service_id == cid) == 0
    assert await _count(w, terms, terms.service_id == cid) == 0
    assert await _count(w, exceptions, exceptions.service_id == cid) == 0
    # The other service's rows, and the convenio plan itself, are still there.
    assert await _count(w, psu, psu.service_id == rid) == 1
    assert await _count(w, terms, terms.service_id == rid) == 1
    assert await _count(w, exceptions, exceptions.service_id == rid) == 1
    async with w["db"]() as session:
        assert await session.get(TenantInsurancePlan, w["plan"].id) is not None
    # The other clinic's exception is not touched.
    assert await _count(w, exceptions) == 2


async def test_strips_professional_and_tenant_json_by_id_and_by_name(client, world):
    w = world
    response = await client.delete(_url(w["consulta"]), headers=HEADERS)
    assert response.status_code == 204
    # By id (doc_a), by name only with other case/spaces (doc_b, an inactive professional too).
    assert await _names(w, Professional, w["doc_a"].id) == ["Retorno"]
    assert await _names(w, Professional, w["doc_b"].id) == ["Exame"]
    # The clinic's legacy list, which doc_inherit (NULL list) falls back on.
    assert await _names(w, Tenant, w["tenant"].id) == ["Retorno", "Exame"]
    async with w["db"]() as session:
        inheriting = await session.get(Professional, w["doc_inherit"].id)
        assert inheriting.appointment_types is None
    # Another clinic's service with the same name keeps its entries.
    assert await _names(w, Professional, w["foreign_doc"].id) == ["Consulta"]
    assert await _names(w, Tenant, w["other"].id) == ["Consulta"]


async def test_entry_with_another_services_id_is_not_taken_for_the_deleted_one(client, world):
    w = world
    async with w["db"]() as session:
        doc = await session.get(Professional, w["doc_a"].id)
        doc.appointment_types = [
            _entry(w["retorno"], "Consulta"),  # id of Retorno, label says Consulta
            _entry(w["consulta"], "Consulta"),
        ]
        await session.commit()
    assert (await client.delete(_url(w["consulta"]), headers=HEADERS)).status_code == 204
    async with w["db"]() as session:
        doc = await session.get(Professional, w["doc_a"].id)
        assert [e["service_id"] for e in doc.appointment_types] == [str(w["retorno"].id)]


async def test_sort_order_repacked(client, world):
    w = world
    assert (await client.delete(_url(w["retorno"]), headers=HEADERS)).status_code == 204
    response = await client.get("/tenants/me/services", headers=HEADERS)
    assert response.status_code == 200
    assert [(row["name"], row["sort_order"]) for row in response.json()] == [
        ("Consulta", 0),
        ("Exame", 1),
    ]


async def test_past_appointments_keep_appointment_type_text(client, world):
    w = world
    assert (await client.delete(_url(w["consulta"]), headers=HEADERS)).status_code == 204
    async with w["db"]() as session:
        appointment = await session.scalar(
            select(Appointment).where(Appointment.google_event_id == "evt-1")
        )
        assert appointment.appointment_type == "Consulta"
        assert appointment.professional_id == w["doc_a"].id


async def _stale_copy(w, service: Service) -> Service:
    """The service as a concurrent request still holds it: loaded, then deleted by another."""
    async with w["db"]() as session:
        stale = await session.get(Service, service.id)
        session.expunge(stale)
    async with w["db"]() as session:
        await session.execute(delete(Service).where(Service.id == service.id))
        await session.commit()
    return stale


async def test_second_concurrent_delete_of_the_same_service_is_404_not_500(
    client, world, monkeypatch
):
    from secretaria.api.hub import services as services_api

    w = world
    stale = await _stale_copy(w, w["consulta"])

    async def _resolve_stale(session, tenant, service_id):
        return stale

    monkeypatch.setattr(services_api, "_resolve", _resolve_stale)
    response = await client.delete(_url(w["consulta"]), headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "service_not_found"
    # Nothing else was touched by the losing request.
    assert await _names(w, Professional, w["doc_a"].id) == ["Consulta", "Retorno"]


async def test_helper_raises_when_the_row_is_already_gone(world):
    from secretaria.services.service_catalog import (
        ServiceAlreadyDeleted,
        delete_service_everywhere,
    )

    w = world
    stale = await _stale_copy(w, w["consulta"])
    async with w["db"]() as session:
        tenant = await session.get(Tenant, w["tenant"].id)
        with pytest.raises(ServiceAlreadyDeleted):
            await delete_service_everywhere(session, tenant, stale)
