# ruff: noqa: F811
"""TASK-085 R / UB1 - D-R14: what starts the price sweep.

After the COMMIT of a hub save that can change a price (`appointment_types` of the clinic or of a
professional - the per-unit exceptions ride in it - or a units save that erased an exception),
`price_change.enqueue_sweep` puts ONE `sweep_price_changes` job per clinic per 5-minute window on
the arq pool. A save that cannot move a price, a refused save, or a clinic without sinal enqueues
nothing; no pool (or a broken one) never fails the save. Contract: mapa-u.md §2.2, §4.5.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from secretaria.api.hub import (  # noqa: E402
    deps as hub_deps,
    professional_units as pu_api,
)
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
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.payments import price_change  # noqa: E402
from secretaria.workers import arq_worker, price_change as price_change_job  # noqa: E402
from tests._local_agenda import seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401
from tests.payments_support import FakeArqPool  # noqa: E402

HEADERS = {"authorization": "Bearer ub1-test-token"}
CONFIGURATION = "/tenants/me/configuration"
LEGACY_CONFIG = "/tenants/me/config"


def _consulta(service: Service, price: str = "R$ 200,00") -> dict:
    return {
        "name": "Consulta",
        "service_id": str(service.id),
        "duration_min": 30,
        "price": price,
        "is_active": True,
    }


@pytest_asyncio.fixture
async def world(db, monkeypatch):
    from secretaria.main import app

    tenant = await seed_tenant(db, name="Clinica A")
    async with db() as session:
        stored = await session.get(Tenant, tenant.id)
        stored.pix_deposit_enabled = True
        consulta = Service(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Consulta",
            normalized_name="consulta",
            is_active=True,
        )
        session.add(consulta)
        await session.flush()
        doctor = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[_consulta(consulta)],
        )
        units = {
            name: Unit(id=uuid4(), tenant_id=tenant.id, name=name, address=f"Rua {name}")
            for name in ("Sede", "Centro")
        }
        session.add_all([doctor, *units.values()])
        await session.flush()
        for position, unit in enumerate(units.values()):
            session.add(
                ProfessionalUnit(
                    professional_id=doctor.id,
                    unit_id=unit.id,
                    business_hours=None,
                    is_active=True,
                    sort_order=position,
                )
            )
        await session.commit()

    async def _session():
        async with db() as session:
            yield session

    async def _verify(token, *, fresh=False):
        return SubscriptionClaim(
            tenant_id=tenant.id,
            active=True,
            role="manager",
            is_owner=False,
            is_manager=True,
            professional_id=None,
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

    pool = FakeArqPool()
    app.dependency_overrides[get_session] = _session
    app.state.arq_pool = pool
    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    monkeypatch.setattr(pu_api, "get_entitlements", _entitled)
    yield {
        "db": db,
        "tenant": tenant,
        "doctor": doctor,
        "consulta": consulta,
        "sede": units["Sede"],
        "centro": units["Centro"],
        "pool": pool,
    }
    app.dependency_overrides.pop(get_session, None)
    app.state.arq_pool = None


def _sweeps(pool: FakeArqPool) -> list[tuple]:
    return [job for job in pool.jobs if job[0] == price_change.SWEEP_JOB]


def _assert_one_sweep(pool: FakeArqPool, tenant_id: UUID) -> None:
    [(name, args, kwargs)] = _sweeps(pool)
    assert name == "sweep_price_changes"
    assert args[0] == str(tenant_id)
    changed_at = datetime.fromisoformat(args[1])
    assert changed_at.tzinfo is not None
    assert abs(datetime.now(UTC) - changed_at) < timedelta(minutes=1)
    assert kwargs["_job_id"] == price_change.sweep_job_id(tenant_id, changed_at)


async def _set_sinal(w, enabled: bool) -> None:
    async with w["db"]() as session:
        stored = await session.get(Tenant, w["tenant"].id)
        stored.pix_deposit_enabled = enabled
        await session.commit()


# ------------------------------------------------------------------ enqueue_sweep


async def test_enqueue_sweep_folds_a_window_into_one_job_that_runs_after_it_closes():
    pool = FakeArqPool()
    tenant_id = uuid4()
    changed_at = datetime(2026, 11, 18, 12, 1, 30, tzinfo=UTC)  # window 12:00-12:05

    await price_change.enqueue_sweep(pool, tenant_id, changed_at=changed_at)

    [(name, args, kwargs)] = pool.jobs
    assert name == price_change.SWEEP_JOB
    assert args == (str(tenant_id), changed_at.isoformat())
    assert kwargs == {
        "_job_id": price_change.sweep_job_id(tenant_id, changed_at),
        # The job runs after the window closed (+ SWEEP_DEFER): an edit later in the same window,
        # refused by arq as a duplicate id, is still covered by the prices the job reads.
        "_defer_until": datetime(2026, 11, 18, 12, 5, tzinfo=UTC) + price_change.SWEEP_DEFER,
    }


async def test_enqueue_sweep_never_raises():
    changed_at = datetime.now(UTC)
    await price_change.enqueue_sweep(None, uuid4(), changed_at=changed_at)  # no pool: a log
    await price_change.enqueue_sweep(FakeArqPool(fail_enqueue=True), uuid4(), changed_at=changed_at)
    await price_change.enqueue_sweep(FakeArqPool(), uuid4(), changed_at=datetime(2026, 1, 1))


# ------------------------------------------------------------------ the job


async def test_the_job_parses_its_arguments_and_hands_the_worker_redis(monkeypatch):
    seen: dict = {}

    async def _service(tenant_id, *, changed_at, now, redis):
        seen.update(tenant_id=tenant_id, changed_at=changed_at, now=now, redis=redis)
        return 2

    monkeypatch.setattr(price_change, "sweep_price_changes", _service)
    tenant_id = uuid4()
    changed_at = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    redis = object()

    result = await price_change_job.sweep_price_changes(
        {"redis": redis}, str(tenant_id), changed_at.isoformat()
    )

    assert result == {"sent": 2}
    assert seen["tenant_id"] == tenant_id and seen["changed_at"] == changed_at
    assert seen["redis"] is redis and seen["now"].tzinfo is not None


@pytest.mark.parametrize(
    ("tenant_id", "changed_at"),
    [
        ("not-a-uuid", "2026-11-18T12:00:00+00:00"),
        (str(uuid4()), "ontem"),
        (str(uuid4()), "2026-11-18T12:00:00"),
    ],
)
async def test_the_job_refuses_bad_arguments_quietly(monkeypatch, tenant_id, changed_at):
    async def _service(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("bad arguments must not reach the service")

    monkeypatch.setattr(price_change, "sweep_price_changes", _service)
    assert await price_change_job.sweep_price_changes({}, tenant_id, changed_at) == {
        "sent": 0,
        "error": "bad_arguments",
    }


def test_the_job_is_registered_under_its_name_with_a_timeout_above_the_sweep_budget():
    assert price_change.SWEEP_JOB in arq_worker.registered_function_names()
    [job] = [
        f
        for f in arq_worker.WorkerSettings.functions
        if getattr(f, "name", None) == price_change.SWEEP_JOB
    ]
    assert job.coroutine is price_change_job.sweep_price_changes
    # arq's default (300 s) would kill a large clinic's sweep with no retry; the sweep stops
    # itself at `_SWEEP_BUDGET` and resumes in a follow-up job, well inside the job timeout.
    assert job.timeout_s == price_change_job.JOB_TIMEOUT.total_seconds()
    assert price_change._SWEEP_BUDGET + timedelta(minutes=4) <= price_change_job.JOB_TIMEOUT


# ------------------------------------------------------------------ hub triggers


async def test_configuration_save_with_a_professional_price_enqueues_one_sweep(client, world):
    w = world
    response = await client.put(
        CONFIGURATION,
        headers=HEADERS,
        json={
            "professional_id": str(w["doctor"].id),
            "professional": {"appointment_types": [_consulta(w["consulta"], "R$ 250,00")]},
        },
    )
    assert response.status_code == 200, response.text
    _assert_one_sweep(w["pool"], w["tenant"].id)


async def test_configuration_save_with_the_clinic_catalog_enqueues_one_sweep(client, world):
    w = world
    response = await client.put(
        CONFIGURATION,
        headers=HEADERS,
        json={"tenant": {"appointment_types": [_consulta(w["consulta"], "R$ 250,00")]}},
    )
    assert response.status_code == 200, response.text
    _assert_one_sweep(w["pool"], w["tenant"].id)


async def test_a_save_that_cannot_move_a_price_enqueues_nothing(client, world):
    w = world
    response = await client.put(
        CONFIGURATION,
        headers=HEADERS,
        json={
            "tenant": {"clinic_description": "nova"},
            "professional_id": str(w["doctor"].id),
            "professional": {"specialty": "Pediatria"},
        },
    )
    assert response.status_code == 200, response.text
    assert _sweeps(w["pool"]) == []


async def test_a_refused_save_enqueues_nothing(client, world):
    w = world
    foreign = {**_consulta(w["consulta"]), "service_id": str(uuid4())}
    response = await client.put(
        CONFIGURATION,
        headers=HEADERS,
        json={
            "professional_id": str(w["doctor"].id),
            "professional": {"appointment_types": [foreign]},
        },
    )
    assert response.status_code == 422
    assert _sweeps(w["pool"]) == []


async def test_a_clinic_without_sinal_enqueues_nothing(client, world):
    w = world
    await _set_sinal(w, False)
    response = await client.put(
        CONFIGURATION,
        headers=HEADERS,
        json={
            "professional_id": str(w["doctor"].id),
            "professional": {"appointment_types": [_consulta(w["consulta"], "R$ 250,00")]},
        },
    )
    assert response.status_code == 200, response.text
    assert _sweeps(w["pool"]) == []


async def test_no_pool_never_fails_the_save(client, world):
    from secretaria.main import app

    w = world
    app.state.arq_pool = FakeArqPool(fail_enqueue=True)
    response = await client.put(
        CONFIGURATION,
        headers=HEADERS,
        json={
            "professional_id": str(w["doctor"].id),
            "professional": {"appointment_types": [_consulta(w["consulta"], "R$ 250,00")]},
        },
    )
    assert response.status_code == 200, response.text
    app.state.arq_pool = None
    response = await client.put(
        CONFIGURATION,
        headers=HEADERS,
        json={"tenant": {"appointment_types": [_consulta(w["consulta"], "R$ 260,00")]}},
    )
    assert response.status_code == 200, response.text


async def test_legacy_clinic_config_save_with_prices_enqueues_one_sweep(client, world):
    w = world
    response = await client.put(
        LEGACY_CONFIG,
        headers=HEADERS,
        json={"appointment_types": [_consulta(w["consulta"], "R$ 250,00")]},
    )
    assert response.status_code == 200, response.text
    _assert_one_sweep(w["pool"], w["tenant"].id)


async def test_legacy_clinic_config_save_without_prices_enqueues_nothing(client, world):
    w = world
    response = await client.put(LEGACY_CONFIG, headers=HEADERS, json={"clinic_description": "nova"})
    assert response.status_code == 200, response.text
    assert _sweeps(w["pool"]) == []


async def test_professional_config_save_with_prices_enqueues_one_sweep(client, world):
    w = world
    url = f"/tenants/me/professionals/{w['doctor'].id}/config"
    response = await client.put(
        url,
        headers=HEADERS,
        json={"appointment_types": [_consulta(w["consulta"], "R$ 250,00")]},
    )
    assert response.status_code == 200, response.text
    _assert_one_sweep(w["pool"], w["tenant"].id)

    w["pool"].jobs.clear()
    response = await client.put(url, headers=HEADERS, json={"specialty": "Pediatria"})
    assert response.status_code == 200, response.text
    assert _sweeps(w["pool"]) == []


def _units_url(w) -> str:
    return f"/tenants/me/professionals/{w['doctor'].id}/units"


def _link(unit) -> dict:
    return {"unit_id": str(unit.id), "business_hours": None}


async def _seed_exception(w, unit) -> None:
    async with w["db"]() as session:
        session.add(
            ProfessionalServiceUnitTerms(
                professional_id=w["doctor"].id,
                service_id=w["consulta"].id,
                unit_id=unit.id,
                tenant_id=w["tenant"].id,
                price="R$ 300,00",
                duration_min=None,
            )
        )
        await session.commit()


async def test_a_units_save_that_erases_a_price_exception_enqueues_one_sweep(client, world):
    w = world
    await _seed_exception(w, w["centro"])
    response = await client.put(_units_url(w), headers=HEADERS, json={"links": [_link(w["sede"])]})
    assert response.status_code == 200, response.text
    _assert_one_sweep(w["pool"], w["tenant"].id)


async def test_a_units_save_that_keeps_every_exception_enqueues_nothing(client, world):
    w = world
    await _seed_exception(w, w["centro"])
    response = await client.put(
        _units_url(w), headers=HEADERS, json={"links": [_link(w["sede"]), _link(w["centro"])]}
    )
    assert response.status_code == 200, response.text
    assert _sweeps(w["pool"]) == []

    # Dropping a unit WITHOUT an exception (Sede) changes no price either.
    response = await client.put(
        _units_url(w), headers=HEADERS, json={"links": [_link(w["centro"])]}
    )
    assert response.status_code == 200, response.text
    assert _sweeps(w["pool"]) == []  # Centro's exception survived: nothing was erased
