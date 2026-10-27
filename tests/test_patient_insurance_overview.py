"""What a PATIENT may see of the clinic's convenios (TASK-053, owner decision 2026-10-12).

`GET /internal/brain-message/insurance-overview?tenant_id=` feeds the Portal's "Convenios e
pagamento" card through brain-api. What is worth pinning, and the failure each test catches:
- the list is the SAME one the booking flow offers (mode-aware), with the SAME fixed payment text;
- nothing internal leaks: no catalog note, no doctor, no row id;
- the deposit rule mirrors deposit_lifecycle (only an explicit charge_deposit=False skips it),
  "varies" when the active doctors disagree, and all "not_charged" with the deposit off;
- one clinic never sees another's list; the route needs the internal key.
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
VARIES_TEXT = "Varia por plano: confirme cobertura e pagamento com a operadora."
UNKNOWN_TEXT = "Pagamento: confirme as condições com a clínica."


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
    """Shared mode: Unimed (charges) + the clinic's own "GEAP" (no deposit, with a note)."""
    tenant = await _tenant(
        db, insurance_mode="shared", pix_deposit_enabled=deposit, pix_deposit_percent=30
    )
    async with db() as session:
        session.add(
            TenantInsurancePlan(tenant_id=tenant.id, catalog_id=UNIMED, charge_deposit=True)
        )
        session.add(
            TenantInsurancePlan(
                tenant_id=tenant.id,
                custom_name="GEAP",
                custom_payment_note="Pagamento na recepção.",
                charge_deposit=False,
            )
        )
        await session.commit()
    return tenant


async def _get(client, tenant_id) -> httpx.Response:
    return await client.get(PATH, params={"tenant_id": str(tenant_id)}, headers=KEY)


def _row(professional, tenant, catalog_id, charge) -> ProfessionalInsurancePlan:
    return ProfessionalInsurancePlan(
        professional_id=professional.id,
        tenant_id=tenant.id,
        catalog_id=catalog_id,
        charge_deposit=charge,
    )


async def test_the_patient_sees_each_convenio_its_payment_text_its_own_note_and_its_deposit(
    client, db
):
    tenant = await _shared_clinic(db)
    resp = await _get(client, tenant.id)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "deposit": {"enabled": True, "percent": 30},
        "plans": [
            {
                "name": "GEAP",
                "payment_text": UNKNOWN_TEXT,
                "clinic_note": "Pagamento na recepção.",
                "deposit": "not_charged",
            },
            {
                "name": "Unimed",
                "payment_text": VARIES_TEXT,
                "clinic_note": None,
                "deposit": "charged",
            },
        ],
    }


async def test_nothing_internal_leaks_no_catalog_note_no_row_id(client, db):
    tenant = await _shared_clinic(db)
    text = (await _get(client, tenant.id)).text
    assert "Cooperativa" not in text  # Unimed's catalog note (internal metadata)
    assert str(UNIMED) not in text
    internal = ("id", "catalog_id", "note", "mechanism", "accepted_by", "charge_deposit")
    for key in (f'"{name}"' for name in internal):
        assert key not in text


async def test_with_the_deposit_off_every_convenio_is_without_deposit_and_no_percent(client, db):
    tenant = await _shared_clinic(db, deposit=False)
    body = (await _get(client, tenant.id)).json()
    assert body["deposit"] == {"enabled": False, "percent": None}
    assert {plan["deposit"] for plan in body["plans"]} == {"not_charged"}


async def test_independent_mode_unites_active_doctors_says_varies_and_never_names_them(
    client, db
):
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
                _row(ana, tenant, UNIMED, True),
                _row(beto, tenant, UNIMED, False),
                _row(beto, tenant, AMIL, False),
                _row(gone, tenant, AMIL, True),
            ]
        )
        await session.commit()
    resp = await _get(client, tenant.id)
    body = resp.json()
    assert body["deposit"] == {"enabled": True, "percent": 40}
    assert [(p["name"], p["deposit"]) for p in body["plans"]] == [
        ("Amil", "not_charged"),  # the inactive doctor's "charges" does not count
        ("Unimed", "varies"),
    ]
    assert "Sigilo" not in resp.text


async def test_a_legacy_name_without_a_row_is_shown_and_charged(client, db):
    """A free-text convenio from before the catalog: the booking carries no plan id, so the
    deposit is charged normally (deposit_lifecycle._insurance_charges_deposit)."""
    tenant = await _tenant(
        db,
        insurance_mode="shared",
        pix_deposit_enabled=True,
        pix_deposit_percent=30,
        insurances=["Plano Antigo Sintetico"],
    )
    body = (await _get(client, tenant.id)).json()
    assert body["plans"] == [
        {
            "name": "Plano Antigo Sintetico",
            "payment_text": UNKNOWN_TEXT,
            "clinic_note": None,
            "deposit": "charged",
        }
    ]


async def test_one_clinic_never_sees_another_clinics_list(client, db):
    mine = await _shared_clinic(db)
    other = await _tenant(db, insurance_mode="shared", pix_deposit_enabled=True)
    async with db() as session:
        session.add(TenantInsurancePlan(tenant_id=other.id, custom_name="Plano Vizinho"))
        await session.commit()
    names = [p["name"] for p in (await _get(client, mine.id)).json()["plans"]]
    assert "Plano Vizinho" not in names
    assert [p["name"] for p in (await _get(client, other.id)).json()["plans"]] == ["Plano Vizinho"]


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
