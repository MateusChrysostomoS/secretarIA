"""What a PATIENT may see of the clinic's convenios (TASK-053, owner decision 2026-10-12).

`GET /internal/brain-message/insurance-overview?tenant_id=` feeds the Portal's "Convenios e
pagamento" card through brain-api. What is worth pinning, and the failure each test catches:
- the list is the SAME one the booking flow offers (mode-aware);
- nothing internal leaks: no catalog note, no doctor, no row id;
- TASK-085 R D-R12 (owner, 2026-11-18): no per-plan payment text and no per-plan deposit - a
  convenio never exempts the sinal, which is the clinic's one rule (`deposit`);
- TASK-085 R U1b: only the convenios WITH the clinic's own note are listed (the sheet exists to
  show those notes; the Portal offers it only when one exists);
- TASK-090 (owner, 2026-11-22): the "Outro" convênio has no payment text any more, so NO plan is
  ever listed - `plans` stays (wire shape) and is always empty; a patient who asks how payment
  works with a convênio is handed off to a person;
- one clinic never sees another's rule; the route needs the internal key.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Professional,
    ProfessionalInsurancePlan,
    Tenant,
    TenantInsurancePlan,
)
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    ensure_catalog_seeded,
)

KEY = {"X-Internal-Api-Key": "test-internal-key"}
PATH = "/internal/brain-message/insurance-overview"
UNIMED = catalog_id_for_slug("unimed")
AMIL = catalog_id_for_slug("amil")


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db):
    from secretaria.config import get_settings
    from secretaria.core.database import get_session

    get_settings.cache_clear()
    from secretaria.main import app

    async def _override_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


async def _tenant(db, **fields) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12], **fields
        )
        session.add(tenant)
        await session.commit()
        return tenant


async def _shared_clinic(db, *, deposit: bool = True) -> Tenant:
    """Shared mode: Unimed (catalog) + the clinic's own "GEAP" ("Outro")."""
    tenant = await _tenant(
        db, insurance_mode="shared", pix_deposit_enabled=deposit, pix_deposit_percent=30
    )
    async with db() as session:
        session.add(TenantInsurancePlan(tenant_id=tenant.id, catalog_id=UNIMED))
        session.add(TenantInsurancePlan(tenant_id=tenant.id, custom_name="GEAP"))
        await session.commit()
    return tenant


async def _get(client, tenant_id) -> httpx.Response:
    return await client.get(PATH, params={"tenant_id": str(tenant_id)}, headers=KEY)


def _row(professional, tenant, catalog_id) -> ProfessionalInsurancePlan:
    return ProfessionalInsurancePlan(
        professional_id=professional.id, tenant_id=tenant.id, catalog_id=catalog_id
    )


async def test_the_patient_sees_the_one_sinal_and_no_convenio_payment_text(client, db):
    tenant = await _shared_clinic(db)
    resp = await _get(client, tenant.id)
    assert resp.status_code == 200, resp.text
    # TASK-090: neither the catalog plan nor the clinic's "Outro" carries a payment text.
    assert resp.json() == {"deposit": {"enabled": True, "percent": 30}, "plans": []}
    assert "Varia por plano" not in resp.text and "Pagamento: confirme" not in resp.text


async def test_nothing_internal_leaks_no_catalog_note_no_row_id(client, db):
    tenant = await _shared_clinic(db)
    text = (await _get(client, tenant.id)).text
    assert "Cooperativa" not in text  # Unimed's catalog note (internal metadata)
    assert str(UNIMED) not in text
    internal = (
        "id",
        "catalog_id",
        "note",
        "mechanism",
        "accepted_by",
        "charge_deposit",
        "payment_text",
    )
    for key in (f'"{name}"' for name in internal):
        assert key not in text


async def test_with_the_deposit_off_the_clinic_rule_says_off_and_no_percent(client, db):
    tenant = await _shared_clinic(db, deposit=False)
    body = (await _get(client, tenant.id)).json()
    assert body["deposit"] == {"enabled": False, "percent": None}
    assert body["plans"] == []


async def test_independent_mode_lists_no_plan_and_never_names_a_doctor(client, db):
    tenant = await _tenant(
        db, insurance_mode="independent", pix_deposit_enabled=True, pix_deposit_percent=40
    )
    async with db() as session:
        ana = Professional(tenant_id=tenant.id, name="Dra. Ana Sigilo", is_active=True)
        beto = Professional(tenant_id=tenant.id, name="Dr. Beto Sigilo", is_active=True)
        gone = Professional(tenant_id=tenant.id, name="Dr. Inativo Sigilo", is_active=False)
        session.add_all([ana, beto, gone])
        await session.flush()
        session.add_all(
            [
                _row(ana, tenant, UNIMED),
                _row(beto, tenant, UNIMED),
                _row(beto, tenant, AMIL),
                _row(gone, tenant, AMIL),
                ProfessionalInsurancePlan(
                    professional_id=beto.id,
                    tenant_id=tenant.id,
                    custom_name="Plano do Consultorio",
                ),
                ProfessionalInsurancePlan(
                    professional_id=gone.id,
                    tenant_id=tenant.id,
                    custom_name="Plano Inativo",
                ),
            ]
        )
        await session.commit()
    resp = await _get(client, tenant.id)
    body = resp.json()
    assert body["deposit"] == {"enabled": True, "percent": 40}
    # TASK-090: a doctor's own "Outro" has no payment text either.
    assert body["plans"] == []
    assert "Sigilo" not in resp.text and "Plano Inativo" not in resp.text


async def test_a_legacy_name_without_a_row_has_no_note_so_it_is_not_listed(client, db):
    """A free-text convenio from before the catalog never carries a clinic note (U1b)."""
    tenant = await _tenant(
        db,
        insurance_mode="shared",
        pix_deposit_enabled=True,
        pix_deposit_percent=30,
        insurances=["Plano Antigo Sintetico"],
    )
    body = (await _get(client, tenant.id)).json()
    assert body["plans"] == []


async def test_one_clinic_never_sees_another_clinics_rule(client, db):
    mine = await _shared_clinic(db)
    other = await _tenant(
        db, insurance_mode="shared", pix_deposit_enabled=True, pix_deposit_percent=55
    )
    async with db() as session:
        session.add(TenantInsurancePlan(tenant_id=other.id, custom_name="Plano Vizinho"))
        await session.commit()
    resp = await _get(client, mine.id)
    assert resp.json()["deposit"] == {"enabled": True, "percent": 30}
    assert "Plano Vizinho" not in resp.text
    assert (await _get(client, other.id)).json()["deposit"] == {"enabled": True, "percent": 55}


async def test_an_unknown_clinic_is_404_and_the_route_needs_the_internal_key(client, db):
    resp = await _get(client, uuid4())
    assert resp.status_code == 404
    assert resp.json()["detail"] == {"code": "tenant_not_found"}
    tenant = await _shared_clinic(db)
    no_key = await client.get(PATH, params={"tenant_id": str(tenant.id)})
    assert no_key.status_code == 401
    wrong = await client.get(
        PATH, params={"tenant_id": str(tenant.id)}, headers={"X-Internal-Api-Key": "nope"}
    )
    assert wrong.status_code == 401 and "GEAP" not in wrong.text
