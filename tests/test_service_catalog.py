"""The canonical service catalog: identity, resolution, backfill and the hub API.

A service used to be a free string repeated once per professional, so nothing
could tell two doctors' "Limpeza" apart from each other — or from "limpeza
dental". These tests pin the four things that changes:

  1. IDENTITY — one normalized name per clinic, enforced by the DB.
  2. RESOLUTION — the clinic owns the name and the copy; the professional owns
     price, duration and whether they offer it. Every existing consumer keeps
     receiving the dict shape it already reads.
  3. BACKFILL — equivalent spellings merge, look-alikes never do, and nothing
     in `appointments` is ever rewritten.
  4. THE HUB — one place to edit, a rename that propagates by id, and a
     near-duplicate that is refused with the suggestion attached.

In-memory sqlite (the pattern from tests/test_hub_units.py and
tests/test_multi_professional_plugin.py). No network anywhere.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Appointment, Professional, Tenant  # noqa: E402
from secretaria.models.service import Service  # noqa: E402
from secretaria.services import tenant_config as cfg  # noqa: E402
from secretaria.services.payments import deposit_lifecycle  # noqa: E402
from secretaria.services.service_catalog import (  # noqa: E402
    SERVICE_ID_KEY,
    find_near_duplicates,
    load_service_catalog,
    normalize,
    professionals_offering,
    resolve_entries,
)

ENDPOINT = "/tenants/me/services"


def _entry(name, **overrides):
    """One stored appointment_types entry, the shape that exists today."""
    base = {
        "name": name,
        "duration_min": 30,
        "price": "R$ 200,00",
        "is_active": True,
        "sort_order": 0,
    }
    base.update(overrides)
    return base


def _service(name, **overrides):
    """A detached Service row (no session needed for the pure tests)."""
    fields = dict(
        id=uuid4(),
        tenant_id=uuid4(),
        name=name,
        normalized_name=normalize(name),
        description=None,
        long_description=None,
        requirements=[],
        is_active=True,
        sort_order=0,
    )
    fields.update(overrides)
    return Service(**fields)


# --------------------------------------------------------------------------
# 1. Identity
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    ["Limpeza Dental", "limpeza dental", "  LIMPEZA   DENTAL  ", "Limpeza\tDental"],
)
def test_equivalent_spellings_share_one_identity(spelling):
    assert normalize(spelling) == "limpeza dental"


def test_accents_do_not_create_a_second_service():
    assert normalize("Avaliação") == normalize("Avaliacao") == "avaliacao"


def test_different_services_keep_different_identities():
    assert normalize("Limpeza") != normalize("Limpeza Dental")


def test_blank_names_have_no_identity():
    assert normalize(None) == normalize("") == normalize("   ") == ""


# --------------------------------------------------------------------------
# 2. Look-alikes are flagged, never merged
# --------------------------------------------------------------------------


def test_near_duplicates_are_reported():
    assert find_near_duplicates("Limpeza", ["Limpeza Dental", "Extração"]) == ["Limpeza Dental"]


def test_the_same_service_is_not_a_near_duplicate_of_itself():
    assert find_near_duplicates("Limpeza", ["limpeza", "LIMPEZA"]) == []


def test_unrelated_names_are_not_near_duplicates():
    assert find_near_duplicates("Limpeza", ["Extração", "Clareamento"]) == []


# --------------------------------------------------------------------------
# 3. Resolution — clinic owns the name, professional owns the money
# --------------------------------------------------------------------------


def test_entries_resolve_to_the_clinics_spelling():
    service = _service("Limpeza Dental")
    [resolved] = resolve_entries([_entry("  limpeza dental ")], [service])
    assert resolved["name"] == "Limpeza Dental"
    assert resolved[SERVICE_ID_KEY] == str(service.id)


def test_price_and_duration_stay_per_professional():
    service = _service("Limpeza Dental")
    entries = [_entry("Limpeza Dental", price="R$ 150,00", duration_min=20)]
    [resolved] = resolve_entries(entries, [service])
    assert resolved["price"] == "R$ 150,00"
    assert resolved["duration_min"] == 20


def test_description_comes_from_the_catalog_and_cannot_be_blanked():
    """FIX_08's structural fix: the copy has ONE owner, so a professional
    payload that omits it can no longer zero it."""
    service = _service(
        "Limpeza Dental",
        description="Profilaxia completa",
        long_description="Remoção de tártaro e polimento.",
        requirements=["Escovar os dentes antes"],
    )
    # The professional's entry carries no copy at all — the old shape that
    # used to overwrite the description with nothing.
    [resolved] = resolve_entries([_entry("Limpeza Dental")], [service])
    assert resolved["description"] == "Profilaxia completa"
    assert resolved["long_description"] == "Remoção de tártaro e polimento."
    assert resolved["requirements"] == ["Escovar os dentes antes"]


def test_a_retired_service_is_offered_by_nobody():
    service = _service("Limpeza Dental", is_active=False)
    assert resolve_entries([_entry("Limpeza Dental")], [service]) == []


def test_entries_the_catalog_does_not_know_pass_through_untouched():
    service = _service("Limpeza Dental")
    entry = _entry("Extração")
    [resolved] = resolve_entries([entry], [service])
    assert resolved == entry
    assert SERVICE_ID_KEY not in resolved


def test_no_catalog_means_exactly_todays_behaviour():
    entries = [_entry("Limpeza Dental"), _entry("Extração")]
    assert resolve_entries(entries, []) == entries
    assert resolve_entries(entries, None) == entries


def test_a_linked_entry_resolves_by_id_even_after_a_rename():
    service = _service("Limpeza Dental Completa")
    # The entry still carries the OLD name; the id is what binds them.
    entry = _entry("Limpeza Dental", **{SERVICE_ID_KEY: str(service.id)})
    [resolved] = resolve_entries([entry], [service])
    assert resolved["name"] == "Limpeza Dental Completa"


# --------------------------------------------------------------------------
# 4. "Who offers this?" — the FEAT_34 filter
# --------------------------------------------------------------------------


def _tenant_ns(entries=None):
    return SimpleNamespace(appointment_types=entries or [])


def test_two_professionals_offering_one_service_resolve_to_the_same_id():
    service = _service("Limpeza Dental")
    ana = SimpleNamespace(id=uuid4(), name="Dra. Ana", appointment_types=[_entry("Limpeza")])
    bruno = SimpleNamespace(
        id=uuid4(), name="Dr. Bruno", appointment_types=[_entry("  limpeza dental ")]
    )
    # Ana's "Limpeza" is a DIFFERENT service; Bruno's spelling is the same one.
    ana_ids = [e.get(SERVICE_ID_KEY) for e in resolve_entries(ana.appointment_types, [service])]
    bruno_ids = [
        e.get(SERVICE_ID_KEY) for e in resolve_entries(bruno.appointment_types, [service])
    ]
    assert ana_ids == [None]
    assert bruno_ids == [str(service.id)]


def test_filter_returns_every_professional_offering_the_service():
    service = _service("Limpeza Dental")
    ana = SimpleNamespace(id=uuid4(), name="Ana", appointment_types=[_entry("limpeza dental")])
    bruno = SimpleNamespace(id=uuid4(), name="Bruno", appointment_types=[_entry("Extração")])
    caio = SimpleNamespace(id=uuid4(), name="Caio", appointment_types=[_entry("LIMPEZA DENTAL")])
    found = professionals_offering(service, [ana, bruno, caio], _tenant_ns(), [service])
    assert found == [ana, caio]


def test_filter_returns_empty_when_nobody_else_offers_it():
    """The 'no other doctor does this' answer FEAT_34 has to handle."""
    service = _service("Implante")
    ana = SimpleNamespace(id=uuid4(), name="Ana", appointment_types=[_entry("Limpeza Dental")])
    assert professionals_offering(service, [ana], _tenant_ns(), [service]) == []


def test_filter_ignores_a_professional_who_switched_the_service_off():
    service = _service("Limpeza Dental")
    ana = SimpleNamespace(
        id=uuid4(), name="Ana", appointment_types=[_entry("Limpeza Dental", is_active=False)]
    )
    assert professionals_offering(service, [ana], _tenant_ns(), [service]) == []


def test_filter_falls_back_to_the_tenant_list_for_a_professional_without_services():
    service = _service("Limpeza Dental")
    tenant_ns = _tenant_ns([_entry("Limpeza Dental")])
    ana = SimpleNamespace(id=uuid4(), name="Ana", appointment_types=None)
    assert professionals_offering(service, [ana], tenant_ns, [service]) == [ana]


# --------------------------------------------------------------------------
# DB fixtures
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    async with db() as session:
        row = Tenant(
            id=uuid4(),
            clinic_name="Clinica Teste",
            phone_number_id=str(uuid4())[:12],
            appointment_types=[],
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


@pytest.fixture(autouse=True)
def _override(db, tenant):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant():
        return tenant

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


# --------------------------------------------------------------------------
# 5. The database enforces identity
# --------------------------------------------------------------------------


async def test_two_spellings_of_one_service_cannot_coexist(db, tenant):
    async with db() as session:
        session.add(
            Service(
                id=uuid4(),
                tenant_id=tenant.id,
                name="Limpeza Dental",
                normalized_name=normalize("Limpeza Dental"),
            )
        )
        await session.commit()
    with pytest.raises(IntegrityError):
        async with db() as session:
            session.add(
                Service(
                    id=uuid4(),
                    tenant_id=tenant.id,
                    name="  limpeza   DENTAL ",
                    normalized_name=normalize("  limpeza   DENTAL "),
                )
            )
            await session.commit()


async def test_two_clinics_may_both_have_the_same_service(db, tenant):
    async with db() as session:
        other = Tenant(id=uuid4(), clinic_name="Outra", phone_number_id=str(uuid4())[:12])
        session.add(other)
        await session.flush()
        for owner in (tenant.id, other.id):
            session.add(
                Service(
                    id=uuid4(),
                    tenant_id=owner,
                    name="Limpeza Dental",
                    normalized_name=normalize("Limpeza Dental"),
                )
            )
        await session.commit()
        rows = (await session.scalars(select(Service))).all()
    assert len(rows) == 2


# --------------------------------------------------------------------------
# 6. Backfill
# --------------------------------------------------------------------------


async def _seed_for_backfill(db, tenant):
    """Tenant + two professionals spelling one service three ways."""
    async with db() as session:
        stored = await session.get(Tenant, tenant.id)
        stored.appointment_types = [
            _entry("Limpeza Dental", description="Profilaxia completa"),
            _entry("Extração"),
        ]
        ana = Professional(
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[_entry("limpeza dental", price="R$ 150,00")],
        )
        bruno = Professional(
            tenant_id=tenant.id,
            name="Dr. Bruno",
            is_active=True,
            appointment_types=[_entry("Limpeza Dental"), _entry("Limpeza")],
        )
        session.add_all([ana, bruno])
        await session.commit()
        return ana, bruno


async def _groups_for(session, tenant):
    from scripts.backfill_service_catalog import collect_groups

    stored = await session.get(Tenant, tenant.id)
    professionals = list(
        await session.scalars(select(Professional).where(Professional.tenant_id == tenant.id))
    )
    return stored, collect_groups(stored, professionals)


async def test_backfill_groups_equivalent_spellings_and_keeps_the_frequent_one(db, tenant):
    from scripts.backfill_service_catalog import apply_groups

    await _seed_for_backfill(db, tenant)
    async with db() as session:
        stored, groups = await _groups_for(session, tenant)
        # "Limpeza Dental" x2 vs "limpeza dental" x1 -> the frequent spelling wins.
        assert groups[normalize("Limpeza Dental")].canonical_name == "Limpeza Dental"
        assert groups[normalize("Limpeza Dental")].has_variants is True
        # "Limpeza" is a DIFFERENT service and stays one.
        assert groups[normalize("Limpeza")].has_variants is False

        await apply_groups(session, stored, groups)
        await session.commit()

    async with db() as session:
        rows = (await session.scalars(select(Service))).all()
    # Three canonical services, NOT four and NOT two: the two spellings of
    # "Limpeza Dental" merged, and "Limpeza" was never folded into it.
    assert sorted(row.name for row in rows) == ["Extração", "Limpeza", "Limpeza Dental"]


async def test_backfill_never_merges_look_alikes(db, tenant):
    from scripts.backfill_service_catalog import look_alike_pairs

    await _seed_for_backfill(db, tenant)
    async with db() as session:
        _stored, groups = await _groups_for(session, tenant)
    pairs = {frozenset(pair) for pair in look_alike_pairs(groups)}
    # Reported for a human, and kept as two distinct groups.
    assert frozenset({"Limpeza", "Limpeza Dental"}) in pairs
    assert normalize("Limpeza") in groups
    assert normalize("Limpeza Dental") in groups


async def test_backfill_report_names_every_variant_spelling(db, tenant):
    """The ambiguity report a human reviews before consolidating."""
    await _seed_for_backfill(db, tenant)
    async with db() as session:
        _stored, groups = await _groups_for(session, tenant)
    group = groups[normalize("Limpeza Dental")]
    assert set(group.spellings) == {"Limpeza Dental", "limpeza dental"}
    assert sum(group.spellings.values()) == 3


async def test_backfill_preserves_every_description(db, tenant):
    from scripts.backfill_service_catalog import apply_groups

    await _seed_for_backfill(db, tenant)
    async with db() as session:
        stored, groups = await _groups_for(session, tenant)
        await apply_groups(session, stored, groups)
        await session.commit()

    async with db() as session:
        service = await session.scalar(
            select(Service).where(Service.normalized_name == normalize("Limpeza Dental"))
        )
    assert service.description == "Profilaxia completa"


async def test_backfill_stamps_the_id_onto_every_entry_and_is_idempotent(db, tenant):
    from scripts.backfill_service_catalog import apply_groups

    ana, _bruno = await _seed_for_backfill(db, tenant)
    for _run in range(2):
        async with db() as session:
            stored, groups = await _groups_for(session, tenant)
            await apply_groups(session, stored, groups)
            await session.commit()

    async with db() as session:
        rows = (await session.scalars(select(Service))).all()
        stored_ana = await session.get(Professional, ana.id)
    assert len(rows) == 3  # the second run created nothing new
    assert stored_ana.appointment_types[0][SERVICE_ID_KEY]


async def test_backfill_never_touches_appointment_rows(db, tenant):
    from scripts.backfill_service_catalog import apply_groups

    await _seed_for_backfill(db, tenant)
    async with db() as session:
        session.add(
            Appointment(
                tenant_id=tenant.id,
                google_event_id="evt-history",
                appointment_type="limpeza dental ",  # historical free text
                start_at=datetime(2026, 9, 1, 14, 0, tzinfo=UTC),
                end_at=datetime(2026, 9, 1, 14, 30, tzinfo=UTC),
            )
        )
        await session.commit()

    async with db() as session:
        stored, groups = await _groups_for(session, tenant)
        await apply_groups(session, stored, groups)
        await session.commit()

    async with db() as session:
        appointment = await session.scalar(select(Appointment))
    assert appointment.appointment_type == "limpeza dental "  # verbatim, untouched


# --------------------------------------------------------------------------
# 7. Pix — price by identity, and history stays readable
# --------------------------------------------------------------------------


async def test_price_resolves_when_the_historical_spelling_diverges(db, tenant):
    async with db() as session:
        service = Service(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Limpeza Dental",
            normalized_name=normalize("Limpeza Dental"),
        )
        session.add(service)
        await session.flush()
        professional = Professional(
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[
                _entry("Limpeza Dental", price="R$ 250,00", **{SERVICE_ID_KEY: str(service.id)})
            ],
        )
        session.add(professional)
        await session.flush()
        appointment = Appointment(
            tenant_id=tenant.id,
            professional_id=professional.id,
            google_event_id="evt-old",
            # Booked long ago, under a spelling nobody uses now.
            appointment_type="  LIMPEZA   dental  ",
            start_at=datetime(2026, 9, 1, 14, 0, tzinfo=UTC),
            end_at=datetime(2026, 9, 1, 14, 30, tzinfo=UTC),
        )
        session.add(appointment)
        await session.flush()
        stored_tenant = await session.get(Tenant, tenant.id)
        name, price = await deposit_lifecycle._resolve_service_and_price(
            session, stored_tenant, appointment
        )
    assert name == "Limpeza Dental"
    assert price == "R$ 250,00"


async def test_historical_appointment_without_a_catalog_still_reads(db, tenant):
    """A tenant not backfilled yet keeps the pre-catalog exact-name behaviour."""
    async with db() as session:
        stored = await session.get(Tenant, tenant.id)
        stored.appointment_types = [_entry("Consulta", price="R$ 100,00")]
        appointment = Appointment(
            tenant_id=tenant.id,
            google_event_id="evt-legacy",
            appointment_type="Consulta",
            start_at=datetime(2026, 9, 1, 14, 0, tzinfo=UTC),
            end_at=datetime(2026, 9, 1, 14, 30, tzinfo=UTC),
        )
        session.add(appointment)
        await session.flush()
        name, price = await deposit_lifecycle._resolve_service_and_price(
            session, stored, appointment
        )
    assert (name, price) == ("Consulta", "R$ 100,00")


# --------------------------------------------------------------------------
# 8. The hub API
# --------------------------------------------------------------------------


async def test_create_returns_the_catalog_row(client: AsyncClient):
    response = await client.post(ENDPOINT, json={"name": "Limpeza Dental"})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Limpeza Dental"
    assert set(body) == {
        "id",
        "name",
        "description",
        "long_description",
        "requirements",
        "is_active",
        "sort_order",
        "created_at",
        # Who offers it — the hub renders "também oferecido por" from this and
        # warns before a rename that would change it for other doctors too.
        "professional_ids",
    }
    # The internal identity key is never exposed as if it were editable.
    assert "normalized_name" not in body


async def test_an_equivalent_spelling_is_refused_with_the_existing_service(client: AsyncClient):
    await client.post(ENDPOINT, json={"name": "Limpeza Dental"})
    response = await client.post(ENDPOINT, json={"name": "  limpeza   DENTAL "})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "service_already_exists"
    assert detail["service"]["name"] == "Limpeza Dental"


async def test_a_near_duplicate_warns_and_suggests_the_existing_one(client: AsyncClient):
    await client.post(ENDPOINT, json={"name": "Limpeza Dental"})
    response = await client.post(ENDPOINT, json={"name": "Limpeza"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "similar_service_exists"
    assert detail["similar"] == ["Limpeza Dental"]


async def test_a_near_duplicate_can_be_created_deliberately(client: AsyncClient):
    await client.post(ENDPOINT, json={"name": "Limpeza Dental"})
    response = await client.post(f"{ENDPOINT}?force=true", json={"name": "Limpeza"})
    assert response.status_code == 201
    listed = (await client.get(ENDPOINT)).json()
    assert sorted(row["name"] for row in listed) == ["Limpeza", "Limpeza Dental"]


async def test_force_never_creates_an_exact_duplicate(client: AsyncClient):
    await client.post(ENDPOINT, json={"name": "Limpeza Dental"})
    response = await client.post(f"{ENDPOINT}?force=true", json={"name": "limpeza dental"})
    assert response.status_code == 409


async def test_renaming_the_service_renames_it_for_every_professional(
    client: AsyncClient, db, tenant
):
    created = (await client.post(ENDPOINT, json={"name": "Limpeza Dental"})).json()
    async with db() as session:
        for name in ("Dra. Ana", "Dr. Bruno"):
            session.add(
                Professional(
                    tenant_id=tenant.id,
                    name=name,
                    is_active=True,
                    appointment_types=[
                        _entry("Limpeza Dental", **{SERVICE_ID_KEY: created["id"]})
                    ],
                )
            )
        await session.commit()

    response = await client.patch(
        f"{ENDPOINT}/{created['id']}", json={"name": "Profilaxia Dental"}
    )
    assert response.status_code == 200

    async with db() as session:
        stored_tenant = await session.get(Tenant, tenant.id)
        services = await load_service_catalog(session, tenant.id)
        professionals = list(
            await session.scalars(select(Professional).where(Professional.tenant_id == tenant.id))
        )
        # No fan-out write happened — every professional resolves to the new
        # name purely because they reference the id.
        resolved = [
            cfg.professional_appointment_types(p, stored_tenant, services)[0]["name"]
            for p in professionals
        ]
    assert resolved == ["Profilaxia Dental", "Profilaxia Dental"]


async def test_renaming_onto_an_existing_service_is_refused(client: AsyncClient):
    await client.post(ENDPOINT, json={"name": "Limpeza Dental"})
    other = (await client.post(f"{ENDPOINT}?force=true", json={"name": "Extração"})).json()
    response = await client.patch(f"{ENDPOINT}/{other['id']}", json={"name": "limpeza dental"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "service_already_exists"


async def test_retiring_a_service_removes_it_from_every_booking_surface(
    client: AsyncClient, db, tenant
):
    created = (await client.post(ENDPOINT, json={"name": "Limpeza Dental"})).json()
    async with db() as session:
        session.add(
            Professional(
                tenant_id=tenant.id,
                name="Dra. Ana",
                is_active=True,
                appointment_types=[_entry("Limpeza Dental", **{SERVICE_ID_KEY: created["id"]})],
            )
        )
        await session.commit()

    await client.patch(f"{ENDPOINT}/{created['id']}", json={"is_active": False})

    async with db() as session:
        stored_tenant = await session.get(Tenant, tenant.id)
        services = await load_service_catalog(session, tenant.id)
        professional = await session.scalar(select(Professional))
        assert cfg.professional_appointment_types(professional, stored_tenant, services) == []
    # ... but the hub still sees it, so the clinic can bring it back.
    assert [row["name"] for row in (await client.get(ENDPOINT)).json()] == ["Limpeza Dental"]


async def test_another_clinics_service_is_not_reachable(client: AsyncClient, db):
    async with db() as session:
        other = Tenant(id=uuid4(), clinic_name="Outra", phone_number_id=str(uuid4())[:12])
        session.add(other)
        await session.flush()
        foreign = Service(
            id=uuid4(),
            tenant_id=other.id,
            name="Limpeza Dental",
            normalized_name=normalize("Limpeza Dental"),
        )
        session.add(foreign)
        await session.commit()
        foreign_id = str(foreign.id)

    assert (await client.get(ENDPOINT)).json() == []
    response = await client.patch(f"{ENDPOINT}/{foreign_id}", json={"name": "Sequestrado"})
    assert response.status_code == 404


async def test_an_unparseable_id_is_a_plain_404(client: AsyncClient):
    response = await client.patch(f"{ENDPOINT}/not-a-uuid", json={"name": "X"})
    assert response.status_code == 404


# --------------------------------------------------------------------------
# 9. Entrega 2 wiring - the link the hub writes, and who offers what.
#
# Until `service_id` survived validation, the catalog was unreachable from the
# hub: Pydantic dropped the key (extra="ignore"), so every save fell back to
# name matching and the link was never actually written.
# --------------------------------------------------------------------------


async def _catalog_row(client: AsyncClient, name: str) -> dict:
    response = await client.post(ENDPOINT, json={"name": name}, params={"force": "true"})
    assert response.status_code == 201
    return response.json()


async def test_a_professional_save_persists_the_service_id(client: AsyncClient, db, tenant):
    """The whole point of entrega 2: picking from the catalog writes the link."""
    service = await _catalog_row(client, "Limpeza")
    async with db() as session:
        prof = Professional(tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        session.add(prof)
        await session.commit()
        await session.refresh(prof)

    response = await client.put(
        f"/tenants/me/professionals/{prof.id}/config",
        json={
            "appointment_types": [
                _entry("qualquer coisa", service_id=service["id"], duration_min=45)
            ]
        },
    )
    assert response.status_code == 200

    async with db() as session:
        stored = await session.get(Professional, prof.id)
        assert stored.appointment_types[0]["service_id"] == service["id"]
        # Price/duration stay the PROFESSIONAL's.
        assert stored.appointment_types[0]["duration_min"] == 45

    # ...and the catalog's spelling is what the resolver hands downstream,
    # whatever the client happened to type in `name`.
    async with db() as session:
        catalog = await load_service_catalog(session, tenant.id)
        stored = await session.get(Professional, prof.id)
        assert resolve_entries(stored.appointment_types, catalog)[0]["name"] == "Limpeza"


async def test_a_service_id_from_another_clinic_is_refused(client: AsyncClient, db, tenant):
    """A dangling id would silently degrade to name matching - the exact state
    the catalog exists to end - so it is rejected instead of stored."""
    async with db() as session:
        other = Tenant(id=uuid4(), clinic_name="Outra", phone_number_id=str(uuid4())[:12])
        session.add(other)
        await session.commit()
        foreign = Service(
            id=uuid4(),
            tenant_id=other.id,
            name="Limpeza",
            normalized_name=normalize("Limpeza"),
        )
        session.add(foreign)
        await session.commit()
        prof = Professional(tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        session.add(prof)
        await session.commit()
        await session.refresh(prof)
        foreign_id = str(foreign.id)

    response = await client.put(
        f"/tenants/me/professionals/{prof.id}/config",
        json={"appointment_types": [_entry("Limpeza", service_id=foreign_id)]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_service_ids"
    assert response.json()["detail"]["service_ids"] == [foreign_id]

    async with db() as session:
        stored = await session.get(Professional, prof.id)
        assert stored.appointment_types is None  # nothing was written


async def test_entries_without_a_service_id_still_save(client: AsyncClient, db, tenant):
    """Pre-catalog payloads keep working - that is what lets this ship before
    the backfill."""
    async with db() as session:
        prof = Professional(tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        session.add(prof)
        await session.commit()
        await session.refresh(prof)

    response = await client.put(
        f"/tenants/me/professionals/{prof.id}/config",
        json={"appointment_types": [_entry("Limpeza")]},
    )
    assert response.status_code == 200


async def test_the_catalog_reports_which_professionals_offer_each_service(
    client: AsyncClient, db, tenant
):
    limpeza = await _catalog_row(client, "Limpeza")
    clareamento = await _catalog_row(client, "Clareamento")

    async with db() as session:
        ana = Professional(
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[_entry("Limpeza", service_id=limpeza["id"])],
        )
        bruno = Professional(
            tenant_id=tenant.id,
            name="Dr. Bruno",
            is_active=True,
            appointment_types=[
                _entry("Limpeza", service_id=limpeza["id"]),
                _entry("Clareamento", service_id=clareamento["id"]),
            ],
        )
        session.add_all([ana, bruno])
        await session.commit()
        await session.refresh(ana)
        await session.refresh(bruno)
        ana_id, bruno_id = str(ana.id), str(bruno.id)

    rows = {row["name"]: row for row in (await client.get(ENDPOINT)).json()}
    assert sorted(rows["Limpeza"]["professional_ids"]) == sorted([ana_id, bruno_id])
    assert rows["Clareamento"]["professional_ids"] == [bruno_id]


async def test_an_inactive_professional_is_not_counted_as_offering(
    client: AsyncClient, db, tenant
):
    """"Tambem oferecido por" must not name someone no patient can reach."""
    limpeza = await _catalog_row(client, "Limpeza")
    async with db() as session:
        session.add(
            Professional(
                tenant_id=tenant.id,
                name="Dr. Antigo",
                is_active=False,
                appointment_types=[_entry("Limpeza", service_id=limpeza["id"])],
            )
        )
        await session.commit()

    rows = (await client.get(ENDPOINT)).json()
    assert rows[0]["professional_ids"] == []


async def test_a_professional_who_turned_the_service_off_does_not_offer_it(
    client: AsyncClient, db, tenant
):
    limpeza = await _catalog_row(client, "Limpeza")
    async with db() as session:
        session.add(
            Professional(
                tenant_id=tenant.id,
                name="Dra. Ana",
                is_active=True,
                appointment_types=[
                    _entry("Limpeza", service_id=limpeza["id"], is_active=False)
                ],
            )
        )
        await session.commit()

    rows = (await client.get(ENDPOINT)).json()
    assert rows[0]["professional_ids"] == []


async def test_an_unlinked_legacy_entry_still_counts_as_offering(client: AsyncClient, db, tenant):
    """A clinic that has not been backfilled still gets a truthful answer -
    which is what makes the rename warning trustworthy before the backfill."""
    async with db() as session:
        ana = Professional(
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[_entry("  limpeza  ")],  # no service_id, odd spelling
        )
        session.add(ana)
        await session.commit()
        await session.refresh(ana)
        ana_id = str(ana.id)

    created = await _catalog_row(client, "Limpeza")
    # Reported the moment the catalog row exists, with no write to the
    # professional at all.
    assert created["professional_ids"] == [ana_id]
    assert (await client.get(ENDPOINT)).json()[0]["professional_ids"] == [ana_id]


async def test_renaming_keeps_reporting_the_same_professionals(client: AsyncClient, db, tenant):
    """A rename is a one-row write that changes what every linked doctor
    offers - so the response has to keep naming them."""
    limpeza = await _catalog_row(client, "Limpeza")
    async with db() as session:
        ana = Professional(
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=[_entry("Limpeza", service_id=limpeza["id"])],
        )
        session.add(ana)
        await session.commit()
        await session.refresh(ana)
        ana_id = str(ana.id)

    response = await client.patch(
        f"{ENDPOINT}/{limpeza['id']}", json={"name": "Limpeza Profunda"}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Limpeza Profunda"
    assert response.json()["professional_ids"] == [ana_id]
