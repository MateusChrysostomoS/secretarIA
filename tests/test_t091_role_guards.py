# ruff: noqa: F811
"""TASK-091 T4: the hub's server-side role gates for a doctor who is NOT a manager.

A `doctor` without `is_owner`/`is_manager` ("self-only doctor") reads and writes ONLY the
professional his account is linked to (`claim.professional_id`): config, units, insurance
acceptance, service x unit marks and the per-unit price/duration/days terms. `GET
/professionals` lists only him. He READS the clinic's catalog (services, units, the sinal
percentage) but WRITES nothing of the clinic: catalog, units, clinic data, messages, reminders,
pix, post-consult, clinic insurance -> 403 `forbidden_role`. A manager is unchanged.

The real `get_current_tenant` runs: only brain-api's token introspection is faked, so the
tenant ALWAYS comes from the claim, never from the request.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.api.hub import deps as hub_deps  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import (  # noqa: E402
    Professional,
    ProfessionalServiceUnitTerms,
    ProfessionalUnit,
    Service,
    Tenant,
    Unit,
)
from secretaria.services.payments import price_change  # noqa: E402
from tests._local_agenda import seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

HEADERS = {"authorization": "Bearer t091-role-token"}


class _FakePool:
    """Stands in for the arq pool: records what the price sweep enqueues."""

    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name: str, *args, **kwargs) -> None:
        self.jobs.append((name, args))


@pytest_asyncio.fixture
async def world(db, monkeypatch):
    from secretaria.main import app

    tenant = await seed_tenant(db, name="Clinica T4")
    other = await seed_tenant(db, name="Outra Clinica")
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.pix_deposit_enabled = True
        consulta = Service(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Consulta",
            normalized_name="consulta",
            is_active=True,
        )
        centro = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro", address="Rua A")
        session.add_all([consulta, centro])
        await session.flush()
        entry = {
            "name": "Consulta",
            "service_id": str(consulta.id),
            "duration_min": 30,
            "price": "200",
            "is_active": True,
        }
        doctor_a = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[dict(entry)],
        )
        doctor_b = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dr. Bruno",
            is_active=True,
            appointment_types=[dict(entry)],
        )
        foreign = Professional(id=uuid4(), tenant_id=other.id, name="Dr. Alheio", is_active=True)
        session.add_all([doctor_a, doctor_b, foreign])
        await session.flush()
        session.add_all(
            [
                ProfessionalUnit(professional_id=doctor_a.id, unit_id=centro.id),
                ProfessionalUnit(professional_id=doctor_b.id, unit_id=centro.id),
            ]
        )
        await session.commit()

    claim: dict = {}

    def as_self_only(professional_id: UUID | None) -> None:
        claim.update(
            role="doctor",
            is_owner=False,
            is_manager=False,
            professional_id=professional_id,
            tenant_id=tenant.id,
        )

    def as_manager(**flags) -> None:
        claim.update(
            role="manager",
            is_owner=False,
            is_manager=False,
            professional_id=None,
            tenant_id=tenant.id,
        )
        claim.update(flags)

    def as_role(role: str | None, **flags) -> None:
        claim.update(
            role=role, is_owner=False, is_manager=False, professional_id=None, tenant_id=tenant.id
        )
        claim.update(flags)

    async def _verify(token, *, fresh=False):
        return SubscriptionClaim(
            tenant_id=claim["tenant_id"],
            active=True,
            role=claim["role"],
            is_owner=claim["is_owner"],
            is_manager=claim["is_manager"],
            professional_id=claim["professional_id"],
        )

    async def _session():
        async with db() as session:
            yield session

    async def _no_calendar(*_args, **_kwargs):
        return None

    pool = _FakePool()
    previous_pool = getattr(app.state, "arq_pool", None)
    app.state.arq_pool = pool
    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    from secretaria.api.hub import professionals as professionals_api

    monkeypatch.setattr(professionals_api, "prepare_shared_professional_calendar", _no_calendar)
    as_self_only(doctor_a.id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield {
            "client": client,
            "db": db,
            "tenant": tenant,
            "a": doctor_a,
            "b": doctor_b,
            "foreign": foreign,
            "consulta": consulta,
            "centro": centro,
            "pool": pool,
            "as_self_only": as_self_only,
            "as_manager": as_manager,
            "as_role": as_role,
        }
    app.state.arq_pool = previous_pool
    app.dependency_overrides.pop(get_session, None)


def _forbidden(response) -> bool:
    return response.status_code == 403 and response.json()["detail"]["code"] == "forbidden_role"


# Every hub route that names ONE professional. `{p}` is the professional in the path.
PROFESSIONAL_ROUTES: list[tuple[str, str, dict | None]] = [
    ("GET", "/tenants/me/professionals/{p}/units", None),
    ("PUT", "/tenants/me/professionals/{p}/units", {"links": []}),
    ("PUT", "/tenants/me/professionals/{p}/config", {"specialty": "Retina"}),
    ("PATCH", "/tenants/me/professionals/{p}", {"name": "Outro Nome"}),
    ("POST", "/tenants/me/professionals/{p}/calendar", None),
    ("GET", "/tenants/me/professionals/{p}/insurance-plans", None),
    ("PUT", "/tenants/me/professionals/{p}/insurance-plans", {"plan_ids": []}),
    (
        "POST",
        "/tenants/me/professionals/{p}/insurance-plans/custom",
        {"custom_name": "Plano X", "custom_payment_note": "paga e pede reembolso"},
    ),
    ("GET", "/tenants/me/professionals/{p}/insurance-acceptance", None),
    ("PUT", "/tenants/me/professionals/{p}/insurance-acceptance/units/none", {"cells": []}),
    ("POST", "/tenants/me/professionals/{p}/insurance-acceptance/copy", {}),
    ("GET", "/tenants/me/professionals/{p}/agenda", None),
    ("GET", "/tenants/me/professionals/{p}/calendar/oauth/start", None),
    ("POST", "/tenants/me/professionals/{p}/calendar/disconnect", None),
]


@pytest.mark.parametrize(("method", "path", "body"), PROFESSIONAL_ROUTES)
async def test_self_only_doctor_cannot_touch_another_professional(world, method, path, body):
    response = await world["client"].request(
        method, path.format(p=world["b"].id), headers=HEADERS, json=body
    )
    assert _forbidden(response), (method, path, response.status_code, response.text)


async def test_self_only_doctor_cannot_save_another_professional_through_configuration(world):
    response = await world["client"].put(
        "/tenants/me/configuration",
        headers=HEADERS,
        json={"professional_id": str(world["b"].id), "professional": {"specialty": "Retina"}},
    )
    assert _forbidden(response), response.text


async def test_cross_professional_write_leaves_the_other_untouched(world):
    await world["client"].put(
        f"/tenants/me/professionals/{world['b'].id}/config",
        headers=HEADERS,
        json={"specialty": "Invadido"},
    )
    async with world["db"]() as session:
        b = await session.get(Professional, world["b"].id)
        assert b.specialty != "Invadido"


async def test_self_only_doctor_without_linked_professional_reaches_nobody(world):
    world["as_self_only"](None)
    response = await world["client"].put(
        f"/tenants/me/professionals/{world['a'].id}/config",
        headers=HEADERS,
        json={"specialty": "Retina"},
    )
    assert _forbidden(response)
    listing = await world["client"].get("/tenants/me/professionals", headers=HEADERS)
    assert listing.status_code == 200
    assert listing.json() == []


# --- own professional: allowed ------------------------------------------------------------


async def test_self_only_doctor_edits_own_config(world):
    response = await world["client"].put(
        f"/tenants/me/professionals/{world['a'].id}/config",
        headers=HEADERS,
        json={"specialty": "Retina", "about": "Sobre mim"},
    )
    assert response.status_code == 200, response.text
    async with world["db"]() as session:
        assert (await session.get(Professional, world["a"].id)).specialty == "Retina"


async def test_self_only_doctor_saves_own_professional_through_configuration(world):
    response = await world["client"].put(
        "/tenants/me/configuration",
        headers=HEADERS,
        json={"professional_id": str(world["a"].id), "professional": {"specialty": "Retina"}},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "path",
    [
        "/tenants/me/professionals/{p}/units",
        "/tenants/me/professionals/{p}/insurance-plans",
        "/tenants/me/professionals/{p}/insurance-acceptance",
        "/tenants/me/professionals/{p}/agenda",
    ],
)
async def test_self_only_doctor_reads_own_professional(world, path):
    response = await world["client"].get(path.format(p=world["a"].id), headers=HEADERS)
    # Not refused by role (the insurance views answer 409 while the clinic has no insurance
    # mode - that is the clinic's setup, not a permission).
    assert response.status_code not in (401, 403), (path, response.text)


async def test_list_shows_only_the_self_only_doctor(world):
    response = await world["client"].get("/tenants/me/professionals", headers=HEADERS)
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [str(world["a"].id)]


async def test_self_only_doctor_cannot_deactivate_himself(world):
    """`is_active` spends a seat of the clinic's plan: only the manager turns it on or off."""
    response = await world["client"].patch(
        f"/tenants/me/professionals/{world['a'].id}",
        headers=HEADERS,
        json={"is_active": False},
    )
    assert _forbidden(response)


async def test_self_only_doctor_renames_himself(world):
    response = await world["client"].patch(
        f"/tenants/me/professionals/{world['a'].id}",
        headers=HEADERS,
        json={"name": "Dra. Ana Maria"},
    )
    assert response.status_code == 200, response.text


# --- the clinic: read yes, write no -------------------------------------------------------


CLINIC_READS = [
    "/tenants/me/services",
    "/tenants/me/units",
    "/tenants/me/config",
    "/tenants/me/insurance-mode",
    "/tenants/me/insurance-plans",
    "/tenants/me/insurance-acceptance/clinic",
]


@pytest.mark.parametrize("path", CLINIC_READS)
async def test_self_only_doctor_reads_the_clinic_catalog(world, path):
    response = await world["client"].get(path, headers=HEADERS)
    assert response.status_code not in (401, 403), (path, response.text)


async def test_self_only_doctor_sees_the_sinal_percentage(world):
    response = await world["client"].get("/tenants/me/config", headers=HEADERS)
    assert "pix_deposit_percent" in response.json()


CLINIC_WRITES: list[tuple[str, str, dict | None]] = [
    ("POST", "/tenants/me/professionals", {"name": "Novo Medico"}),
    ("POST", "/tenants/me/professionals/calendars", None),
    ("PUT", "/tenants/me/config", {"clinic_description": "outra"}),
    ("PUT", "/tenants/me/config", {"pix_deposit_percent": 50}),
    ("PUT", "/tenants/me/configuration", {"tenant": {"clinic_description": "outra"}}),
    ("PUT", "/tenants/me/configuration", {"tenant": {"post_consult_message": "obrigado"}}),
    ("PUT", "/tenants/me/configuration", {"tenant": {"pix_deposit_percent": 50}}),
    ("PATCH", "/tenants/me/contact-email", {"contact_email": "x@example.com"}),
    ("PATCH", "/tenants/me/operating-channel", {"operating_channel": "portal"}),
    ("POST", "/tenants/me/context-sources", {"kind": "url", "url": "https://example.com"}),
    ("DELETE", "/tenants/me/context-sources/" + str(uuid4()), None),
    ("PUT", "/tenants/me/insurance-mode", {"mode": "shared"}),
    ("PUT", "/tenants/me/insurance-plans", {"plans": []}),
    (
        "POST",
        "/tenants/me/insurance-plans/custom",
        {"custom_name": "Plano X", "custom_payment_note": "reembolso"},
    ),
    ("PUT", "/tenants/me/insurance-plans/order", {"plan_ids": []}),
    ("PUT", "/tenants/me/insurance-acceptance/clinic/units/none", {"cells": []}),
    ("POST", "/tenants/me/insurance-acceptance/clinic/copy", {}),
    ("POST", "/tenants/me/services", {"name": "Exame", "duration_min": 20}),
    ("PUT", "/tenants/me/services/order", {"service_ids": []}),
    ("PATCH", "/tenants/me/services/{service}", {"name": "Renomeado"}),
    ("POST", "/tenants/me/units", {"name": "Filial"}),
    ("PATCH", "/tenants/me/units/{unit}", {"name": "Renomeada"}),
    ("POST", "/tenants/me/agendas/sync", None),
    ("GET", "/tenants/me/calendar/oauth/start", None),
    ("POST", "/tenants/me/calendar/disconnect", None),
]


@pytest.mark.parametrize(("method", "path", "body"), CLINIC_WRITES)
async def test_self_only_doctor_cannot_write_the_clinic(world, method, path, body):
    path = path.format(service=world["consulta"].id, unit=world["centro"].id)
    response = await world["client"].request(method, path, headers=HEADERS, json=body)
    assert _forbidden(response), (method, path, response.status_code, response.text)


async def test_refused_clinic_write_changes_nothing(world):
    await world["client"].patch(
        f"/tenants/me/services/{world['consulta'].id}", headers=HEADERS, json={"name": "Renomeado"}
    )
    await world["client"].put(
        "/tenants/me/configuration",
        headers=HEADERS,
        json={
            "tenant": {"clinic_description": "invadida"},
            "professional_id": str(world["a"].id),
            "professional": {"specialty": "Retina"},
        },
    )
    async with world["db"]() as session:
        assert (await session.get(Service, world["consulta"].id)).name == "Consulta"
        tenant = await session.get(Tenant, world["tenant"].id)
        assert tenant.clinic_description != "invadida"
        # The whole save is refused: the own half did not go through either.
        assert (await session.get(Professional, world["a"].id)).specialty != "Retina"


# --- the manager is unchanged -------------------------------------------------------------


async def test_manager_edits_any_professional_and_the_clinic(world):
    world["as_manager"]()
    client = world["client"]
    r1 = await client.put(
        f"/tenants/me/professionals/{world['b'].id}/config",
        headers=HEADERS,
        json={"specialty": "Glaucoma"},
    )
    r2 = await client.patch(
        f"/tenants/me/services/{world['consulta'].id}", headers=HEADERS, json={"name": "Consulta 2"}
    )
    r3 = await client.put(
        "/tenants/me/configuration",
        headers=HEADERS,
        json={
            "tenant": {"clinic_description": "nova"},
            "professional_id": str(world["b"].id),
            "professional": {"about": "x"},
        },
    )
    listing = await client.get("/tenants/me/professionals", headers=HEADERS)
    assert (r1.status_code, r2.status_code, r3.status_code) == (200, 200, 200), (
        r1.text,
        r2.text,
        r3.text,
    )
    assert {row["id"] for row in listing.json()} == {str(world["a"].id), str(world["b"].id)}


async def test_doctor_flagged_owner_is_a_manager(world):
    world["as_manager"](role="doctor", is_owner=True, professional_id=world["a"].id)
    response = await world["client"].put(
        f"/tenants/me/professionals/{world['b'].id}/config",
        headers=HEADERS,
        json={"specialty": "Glaucoma"},
    )
    assert response.status_code == 200, response.text


async def test_secretary_still_reads_the_whole_roster(world):
    """The desk books for every doctor: reads stay open to it (writes were never its)."""
    world["as_role"]("secretary")
    listing = await world["client"].get("/tenants/me/professionals", headers=HEADERS)
    units = await world["client"].get(
        f"/tenants/me/professionals/{world['b'].id}/units", headers=HEADERS
    )
    assert len(listing.json()) == 2
    assert units.status_code == 200


async def test_tenant_always_comes_from_the_claim(world):
    """A professional of ANOTHER clinic is "not found", even for a manager."""
    world["as_manager"]()
    response = await world["client"].put(
        f"/tenants/me/professionals/{world['foreign'].id}/config",
        headers=HEADERS,
        json={"specialty": "x"},
    )
    assert response.status_code == 404


# --- P1: the doctor's own price runs the same price-change flow as the manager's -----------


def _own_price_body(world, price: str) -> dict:
    return {
        "appointment_types": [
            {
                "name": "Consulta",
                "service_id": str(world["consulta"].id),
                "duration_min": 30,
                "price": "200",
                "is_active": True,
                "unit_terms": [
                    {"unit_id": str(world["centro"].id), "price": price, "duration_min": 40}
                ],
            }
        ]
    }


async def _terms(world, professional_id) -> list[ProfessionalServiceUnitTerms]:
    async with world["db"]() as session:
        return list(
            await session.scalars(
                select(ProfessionalServiceUnitTerms).where(
                    ProfessionalServiceUnitTerms.professional_id == professional_id
                )
            )
        )


async def test_self_only_doctor_own_price_triggers_the_price_change_flow(world):
    response = await world["client"].put(
        f"/tenants/me/professionals/{world['a'].id}/config",
        headers=HEADERS,
        json=_own_price_body(world, "250"),
    )
    assert response.status_code == 200, response.text
    terms = await _terms(world, world["a"].id)
    assert [(t.price, t.duration_min) for t in terms] == [("250", 40)]
    assert [job[0] for job in world["pool"].jobs] == [price_change.SWEEP_JOB]
    assert world["pool"].jobs[0][1][0] == str(world["tenant"].id)


async def test_self_only_doctor_own_price_through_configuration_triggers_the_flow(world):
    response = await world["client"].put(
        "/tenants/me/configuration",
        headers=HEADERS,
        json={"professional_id": str(world["a"].id), "professional": _own_price_body(world, "260")},
    )
    assert response.status_code == 200, response.text
    assert [job[0] for job in world["pool"].jobs] == [price_change.SWEEP_JOB]


async def test_doctor_and_manager_price_edits_enqueue_the_same_job(world):
    await world["client"].put(
        f"/tenants/me/professionals/{world['a'].id}/config",
        headers=HEADERS,
        json=_own_price_body(world, "250"),
    )
    world["as_manager"]()
    await world["client"].put(
        f"/tenants/me/professionals/{world['b'].id}/config",
        headers=HEADERS,
        json=_own_price_body(world, "250"),
    )
    doctor_job, manager_job = world["pool"].jobs
    assert doctor_job[0] == manager_job[0] == price_change.SWEEP_JOB
    assert doctor_job[1][0] == manager_job[1][0] == str(world["tenant"].id)


async def test_self_only_doctor_cannot_price_another_professional(world):
    response = await world["client"].put(
        f"/tenants/me/professionals/{world['b'].id}/config",
        headers=HEADERS,
        json=_own_price_body(world, "999"),
    )
    assert _forbidden(response)
    assert await _terms(world, world["b"].id) == []
    assert world["pool"].jobs == []
