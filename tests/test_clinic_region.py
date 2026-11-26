"""Tests for services/clinic_region.py - the place the greeting names (TASK-090 B3)."""

import os
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Tenant, Unit  # noqa: E402
from secretaria.services.clinic_region import (  # noqa: E402
    Region,
    load_clinic_region,
    region_of_units,
)
from secretaria.services.unit_address import (  # noqa: E402
    BR_STATE_NAMES,
    BR_STATE_PREPOSITIONS,
)
from secretaria.workers.shared.greeting import greeting_place  # noqa: E402


def _unit(city: str | None, state: str | None = "SP", active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(city=city, state=state, is_active=active)


def test_one_unit_gives_city() -> None:
    assert region_of_units([_unit("Campinas")]) == Region("city", "Campinas", "em")


def test_same_city_accents_and_case_equal() -> None:
    region = region_of_units([_unit("São Paulo"), _unit(" sao   paulo ")])
    assert region == Region("city", "São Paulo", "em")


def test_same_state_different_cities_gives_state_name() -> None:
    region = region_of_units([_unit("Belo Horizonte", "MG"), _unit("Uberlândia", "mg")])
    assert region == Region("state", "Minas Gerais", "em")


EXPECTED_STATE_PHRASES = {
    "AC": "no Acre",
    "AL": "em Alagoas",
    "AP": "no Amapá",
    "AM": "no Amazonas",
    "BA": "na Bahia",
    "CE": "no Ceará",
    "DF": "no Distrito Federal",
    "ES": "no Espírito Santo",
    "GO": "em Goiás",
    "MA": "no Maranhão",
    "MT": "no Mato Grosso",
    "MS": "no Mato Grosso do Sul",
    "MG": "em Minas Gerais",
    "PA": "no Pará",
    "PB": "na Paraíba",
    "PR": "no Paraná",
    "PE": "em Pernambuco",
    "PI": "no Piauí",
    "RJ": "no Rio de Janeiro",
    "RN": "no Rio Grande do Norte",
    "RS": "no Rio Grande do Sul",
    "RO": "em Rondônia",
    "RR": "em Roraima",
    "SC": "em Santa Catarina",
    "SP": "em São Paulo",
    "SE": "em Sergipe",
    "TO": "no Tocantins",
}


def test_every_state_has_a_preposition() -> None:
    assert set(EXPECTED_STATE_PHRASES) == set(BR_STATE_NAMES) == set(BR_STATE_PREPOSITIONS)


@pytest.mark.parametrize("uf", sorted(EXPECTED_STATE_PHRASES))
def test_state_region_carries_the_states_own_preposition(uf: str) -> None:
    region = region_of_units([_unit("Cidade Um", uf), _unit("Cidade Dois", uf.lower())])
    assert region is not None and region.level == "state"
    assert f"{region.preposition} {region.name}" == EXPECTED_STATE_PHRASES[uf]


def test_different_states_gives_brasil() -> None:
    region = region_of_units([_unit("Campinas", "SP"), _unit("Uberlândia", "MG")])
    assert region == Region("country", "Brasil", "no")


def test_same_city_name_in_different_states_is_still_one_city() -> None:
    # Equality is by city name only (the brief); two "Santa Cruz" in different UFs read as one.
    region = region_of_units([_unit("Santa Cruz", "RN"), _unit("Santa Cruz", "PE")])
    assert region.level == "city"


def test_unit_without_city_is_ignored() -> None:
    region = region_of_units([_unit("Recife", "PE"), _unit(None, None), _unit("  ", None)])
    assert region == Region("city", "Recife", "em")


def test_inactive_unit_is_ignored() -> None:
    region = region_of_units([_unit("Recife", "PE"), _unit("Curitiba", "PR", active=False)])
    assert region == Region("city", "Recife", "em")


def test_no_unit_city_falls_back_to_tenant_address() -> None:
    region = region_of_units([_unit(None, None)], {"line": "Rua A", "city": " Recife "})
    assert region == Region("city", "Recife", "em")


def test_unit_city_wins_over_tenant_address() -> None:
    region = region_of_units([_unit("Campinas")], {"city": "Recife"})
    assert region == Region("city", "Campinas", "em")


@pytest.mark.parametrize("legacy", [None, {}, {"city": ""}, "Rua A, Recife", 42])
def test_no_region_returns_none(legacy) -> None:
    assert region_of_units([], legacy) is None
    assert region_of_units([_unit(None, None, active=True)], legacy) is None


def test_greeting_place_prefers_region_then_legacy_then_nothing() -> None:
    tenant = SimpleNamespace(address={"city": "Recife"})
    assert greeting_place(tenant, Region("country", "Brasil", "no")) == ("Brasil", "no")
    assert greeting_place(tenant, None) == ("Recife", "em")
    assert greeting_place(SimpleNamespace(address=None), None) == ("", "em")


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def test_load_only_reads_this_tenants_active_units(db) -> None:
    async with db() as session:
        mine = Tenant(id=uuid4(), clinic_name="Mine", phone_number_id=None)
        other = Tenant(id=uuid4(), clinic_name="Other", phone_number_id=None)
        session.add_all([mine, other])
        await session.flush()
        session.add_all(
            [
                Unit(tenant_id=mine.id, name="Sede", city="Recife", state="PE"),
                Unit(tenant_id=mine.id, name="Velha", city="Curitiba", state="PR", is_active=False),
                Unit(tenant_id=other.id, name="Outra", city="Manaus", state="AM"),
            ]
        )
        await session.commit()

        assert await load_clinic_region(session, mine) == Region("city", "Recife", "em")
        assert await load_clinic_region(session, other) == Region("city", "Manaus", "em")


async def test_load_falls_back_to_legacy_address_without_units(db) -> None:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Legacy",
            phone_number_id=None,
            address={"line": "Rua A", "city": "Olinda"},
        )
        session.add(tenant)
        await session.commit()
        assert await load_clinic_region(session, tenant) == Region("city", "Olinda", "em")

        bare = Tenant(id=uuid4(), clinic_name="Bare", phone_number_id=None)
        session.add(bare)
        await session.commit()
        assert await load_clinic_region(session, bare) is None


def _tenant_ns(**kw) -> SimpleNamespace:
    base = dict(
        id=uuid4(),
        clinic_name="Clinic",
        clinic_description="",
        address=None,
        returning_greeting_message=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_first_contact_greeting_names_the_region_with_its_preposition() -> None:
    from secretaria.workers.shared.greeting import _first_contact_reply

    reply = _first_contact_reply(
        tenant=_tenant_ns(address={"city": "Legacy"}),
        conversation_id=uuid4(),
        patient_ref="x",
        channel="whatsapp",
        region=Region("country", "Brasil", "no"),
    )
    assert ", no Brasil." in reply.greeting_override
    assert "Legacy" not in reply.greeting_override


def test_first_contact_greeting_without_region_keeps_the_legacy_city() -> None:
    from secretaria.workers.shared.greeting import _first_contact_reply

    reply = _first_contact_reply(
        tenant=_tenant_ns(address={"city": "Legacy"}),
        conversation_id=uuid4(),
        patient_ref="x",
        channel="whatsapp",
    )
    assert ", em Legacy." in reply.greeting_override


def test_description_is_cut_to_the_budget_of_the_rendered_region() -> None:
    from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS
    from secretaria.workers.shared.greeting import _first_contact_reply

    tenant = _tenant_ns(
        clinic_name="Instituto de Oftalmologia Avancada", clinic_description="d" * 2000
    )
    reply = _first_contact_reply(
        tenant=tenant,
        conversation_id=uuid4(),
        patient_ref="x",
        channel="whatsapp",
        region=Region("state", "Rio Grande do Norte", "no"),
    )
    assert ", no Rio Grande do Norte." in reply.greeting_override
    assert len(reply.greeting_override) <= MAX_INTERACTIVE_BODY_CHARS
