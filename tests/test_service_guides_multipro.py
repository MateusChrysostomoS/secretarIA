"""Service orientations in a clinic with 2+ active professionals (TASK-025 production fix).

The bug: `get_service_info` and the prompt marker " (há orientações: use get_service_info)" read
ONLY `TenantRuntimeConfig.appointment_types`, which `load_tenant_config` resolves from the TENANT
list (or the one professional's list when there is exactly one). The hub writes a new service to
the canonical catalog (`services`) and to each professional's own list, never to the tenant list,
so with 2+ active professionals a catalog-only service was invisible to the agent even though its
orientations ("Jejum de 8 horas") sit in the catalog. The fix adds `service_guides`: the clinic-wide
orientations, read from the catalog `load_tenant_config` already loads.

In-memory sqlite (same pattern as tests/test_service_catalog.py). No network.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.ai import prompts, tools as ai_tools
from secretaria.core.database import Base
from secretaria.models import Professional, Tenant
from secretaria.models.service import Service
from secretaria.services import tenant_config as cfg
from secretaria.services.service_catalog import normalize

SVC = "Cirurgia de Catarata"
REQS = ["Jejum de 8 horas antes da cirurgia", "Vir com acompanhante maior de 18 anos"]
LONG = "Cirurgia para remover a catarata."
# The line the prompt gains for a service whose orientations only the catalog knows.
GUIDES_LINE = "- Serviços com orientações (use get_service_info): "
MARKER = "(há orientações: use get_service_info)"

CATARATA = {"name": SVC, "requirements": REQS, "long_description": LONG}
RESSONANCIA = {
    "name": "Ressonância Magnética",
    "requirements": ["Retirar objetos metálicos"],
    "long_description": "Exame de imagem sem radiação.",
    "description": "Imagem do olho",
}


class _FixedDate(date):
    @classmethod
    def today(cls) -> date:  # type: ignore[override]
        return date(2026, 1, 15)


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompts, "date", _FixedDate)


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _clinic(
    maker,
    *,
    professionals: int,
    catalog: list[dict],
    tenant_extra: tuple[str, ...] = (),
    clinic_name: str = "Clínica Teste",
    offered_by: tuple[bool, ...] | None = None,
    entry_active: bool = True,
) -> cfg.TenantRuntimeConfig:
    """Seed one clinic the way the hub does and return its loaded runtime config.

    `catalog` rows go to the canonical `services` table AND to each professional's own list (with
    `service_id`), exactly what the hub's service POST/PATCH + professional PUT write. The TENANT
    list only gets "Consulta", plus the names in `tenant_extra` (legacy tenant-level services).

    `offered_by` says, per professional, whether their own list carries the catalog services at
    all (default: everyone does). A service the hub just created is in the catalog only, until a
    professional ticks it, so `False` is the DEFAULT state of a new service. `entry_active=False`
    keeps the entry but marks it not offered (a professional who unticked the service).
    """
    async with maker() as session:
        tenant_types = [{"name": "Consulta", "duration_min": 40, "is_active": True}]
        tenant_types += [
            {"name": n, "duration_min": 90, "is_active": True} for n in tenant_extra
        ]
        tenant = Tenant(
            id=uuid4(),
            clinic_name=clinic_name,
            phone_number_id=str(uuid4())[:12],
            appointment_types=tenant_types,
        )
        session.add(tenant)
        await session.flush()
        rows: list[Service] = []
        for order, spec in enumerate(catalog, start=1):
            row = Service(
                id=uuid4(),
                tenant_id=tenant.id,
                name=spec["name"],
                normalized_name=normalize(spec["name"]),
                description=spec.get("description"),
                long_description=spec.get("long_description"),
                requirements=spec.get("requirements"),
                is_active=spec.get("is_active", True),
                sort_order=order,
            )
            session.add(row)
            rows.append(row)
        await session.flush()
        doctors = ("Dr. Diogo Raposo", "Dr. Rafael Teixeira")[:professionals]
        for index, doctor in enumerate(doctors):
            carries = True if offered_by is None else offered_by[index]
            own = [{"name": "Consulta", "duration_min": 40, "is_active": True}]
            own += [
                {
                    "service_id": str(row.id),
                    "name": row.name,
                    "duration_min": 90,
                    "price": "R$ 3.000,00",
                    "is_active": entry_active,
                    "sort_order": row.sort_order,
                }
                for row in (rows if carries else [])
            ]
            session.add(
                Professional(
                    tenant_id=tenant.id, name=doctor, is_active=True, appointment_types=own
                )
            )
        await session.commit()
        await session.refresh(tenant)
        return await cfg.load_tenant_config(session, tenant)


async def _tool(config, name: str) -> dict:
    token = ai_tools._tenant_config_ctx.set(config)
    try:
        return await ai_tools.get_service_info.ainvoke({"service_name": name})
    finally:
        ai_tools._tenant_config_ctx.reset(token)


def _types_block(prompt: str) -> str:
    """The "Tipos de consulta disponíveis" part of the prompt, up to the next blank line."""
    return prompt.split("Tipos de consulta disponíveis:\n", 1)[1].split("\n\n", 1)[0]


def _without_guides(config: cfg.TenantRuntimeConfig) -> str:
    """The prompt the SAME config renders when it carries no `service_guides`."""
    return prompts.secretary_system_prompt(replace(config, service_guides=[]))


def _direct(types: list, guides: list | None = None) -> cfg.TenantRuntimeConfig:
    fields = dict(
        tenant_id=UUID(int=1),
        clinic_name="C",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=types,
        business_hours={},
        google_calendar_id="primary",
        google_refresh_token=None,
    )
    if guides is not None:
        fields["service_guides"] = guides
    return cfg.TenantRuntimeConfig(**fields)


def _type(name: str, **kw) -> cfg.RuntimeAppointmentType:
    return cfg.RuntimeAppointmentType(
        name=name,
        description=kw.get("description"),
        duration_min=kw.get("duration", 30),
        price=kw.get("price"),
        long_description=kw.get("long_description"),
        requirements=kw.get("requirements", []),
    )


def _guide(name: str, **kw) -> cfg.RuntimeServiceGuide:
    return cfg.RuntimeServiceGuide(
        name=name,
        description=kw.get("description"),
        long_description=kw.get("long_description"),
        requirements=kw.get("requirements", []),
    )


# ---------------------------------------------------------------- (A) the bug, 2 professionals


async def test_two_professionals_a_catalog_only_service_is_visible_to_the_agent(maker) -> None:
    config = await _clinic(maker, professionals=2, catalog=[CATARATA])

    # The root cause stays what it was: the service is NOT in the effective catalog...
    assert [t.name for t in config.appointment_types] == ["Consulta"]
    # ...and the fix carries its clinic-wide orientations separately.
    assert config.service_guides == [
        cfg.RuntimeServiceGuide(
            name=SVC, description=None, long_description=LONG, requirements=REQS
        )
    ]

    result = await _tool(config, SVC)
    assert result == {
        "servico": SVC,
        "duracao_min": None,  # varies per professional, so it is not invented here
        "preco": None,
        "descricao": None,
        "descricao_completa": LONG,
        "orientacoes": REQS,
    }

    block = _types_block(prompts.secretary_system_prompt(config))
    assert f"{GUIDES_LINE}{SVC}" in block
    assert "- Consulta (40 min)\n" in block  # the bookable list itself is untouched


async def test_the_guides_line_sits_inside_the_operational_context_after_the_types(maker) -> None:
    config = await _clinic(maker, professionals=2, catalog=[CATARATA])
    prompt = prompts.secretary_system_prompt(config)
    assert prompt.index("Tipos de consulta disponíveis") < prompt.index(GUIDES_LINE)
    assert prompt.index(GUIDES_LINE) < prompt.index("COMO ESCREVER NO WHATSAPP")


async def test_a_guide_only_service_answers_with_the_same_keys_as_a_catalog_one(maker) -> None:
    catalog_cfg = _direct([_type("Hemograma", requirements=["Jejum"])])
    guide_cfg = await _clinic(maker, professionals=2, catalog=[RESSONANCIA])
    assert set(await _tool(guide_cfg, "Ressonância Magnética")) == set(
        await _tool(catalog_cfg, "Hemograma")
    )
    result = await _tool(guide_cfg, "Ressonância Magnética")
    assert result["descricao"] == "Imagem do olho"


async def test_guide_with_only_a_long_description_still_counts(maker) -> None:
    only_long = {"name": "Mapeamento de Retina", "long_description": "Exame com dilatação."}
    config = await _clinic(maker, professionals=2, catalog=[only_long])
    assert [g.name for g in config.service_guides] == ["Mapeamento de Retina"]
    result = await _tool(config, "Mapeamento de Retina")
    assert result["descricao_completa"] == "Exame com dilatação."
    assert result["orientacoes"] == []


async def test_guides_follow_the_catalog_order_and_are_listed_together(maker) -> None:
    config = await _clinic(maker, professionals=2, catalog=[CATARATA, RESSONANCIA])
    assert [g.name for g in config.service_guides] == [SVC, "Ressonância Magnética"]
    block = _types_block(prompts.secretary_system_prompt(config))
    assert f"{GUIDES_LINE}{SVC}, Ressonância Magnética" in block


# ------------------------------------------------------------ (B) exactly 1 professional: unchanged


async def test_one_professional_the_behaviour_is_unchanged(maker) -> None:
    config = await _clinic(maker, professionals=1, catalog=[CATARATA])

    assert [t.name for t in config.appointment_types] == ["Consulta", SVC]
    prompt = prompts.secretary_system_prompt(config)
    assert f"- {SVC} (90 min) - R$ 3.000,00 {MARKER}" in prompt  # the marker on the service line
    assert GUIDES_LINE not in prompt  # and NO extra block
    assert prompt == _without_guides(config)  # byte-identical to a config with no guides at all

    assert await _tool(config, SVC) == {
        "servico": SVC,
        "duracao_min": 90,
        "preco": "R$ 3.000,00",
        "descricao": None,
        "descricao_completa": LONG,
        "orientacoes": REQS,
    }


# ------------------------------------------- (C) 2 professionals, service also in the tenant list


async def test_two_professionals_with_the_service_in_the_tenant_list_has_no_duplicate(
    maker,
) -> None:
    config = await _clinic(maker, professionals=2, catalog=[CATARATA], tenant_extra=(SVC,))

    # Tenant-level entries carry no sort_order, so they sort by name.
    assert [t.name for t in config.appointment_types] == [SVC, "Consulta"]
    prompt = prompts.secretary_system_prompt(config)
    assert f"- {SVC} (90 min) {MARKER}" in prompt
    assert GUIDES_LINE not in prompt
    assert prompt == _without_guides(config)

    result = await _tool(config, SVC)
    assert result["duracao_min"] == 90  # the effective catalog still wins
    assert result["preco"] is None
    assert result["orientacoes"] == REQS
    assert result["descricao_completa"] == LONG


async def test_only_the_guides_missing_from_the_types_are_listed(maker) -> None:
    config = await _clinic(
        maker, professionals=2, catalog=[CATARATA, RESSONANCIA], tenant_extra=(SVC,)
    )
    block = _types_block(prompts.secretary_system_prompt(config))
    # Exactly the one missing from the types: the catarata guide is already a type, no repeat.
    assert block.split(GUIDES_LINE, 1)[1] == "Ressonância Magnética"


def test_a_guide_already_among_the_types_is_not_repeated_whatever_the_spelling() -> None:
    config = _direct(
        [_type("Cirurgia de Catarata", requirements=["Jejum"])],
        [_guide("cirurgia  de CATARATA", requirements=["Jejum"])],
    )
    assert GUIDES_LINE not in prompts.secretary_system_prompt(config)


# --------------------------------------------------------------- (D) what never becomes a guide


@pytest.mark.parametrize(
    "spec",
    [
        {**CATARATA, "is_active": False},
        {"name": SVC, "requirements": None, "long_description": None},
        {"name": SVC, "requirements": [], "long_description": None},
    ],
    ids=["inactive", "no-orientation-none", "no-orientation-empty"],
)
async def test_inactive_or_empty_services_are_no_guides_and_nothing_is_added(maker, spec) -> None:
    config = await _clinic(maker, professionals=2, catalog=[spec])

    assert config.service_guides == []
    prompt = prompts.secretary_system_prompt(config)
    assert GUIDES_LINE not in prompt
    assert prompt == _without_guides(config)
    assert await _tool(config, SVC) == {
        "error": f"Serviço '{SVC}' não existe nesta clínica. Serviços disponíveis: Consulta."
    }


async def test_only_the_valid_guides_survive_a_mixed_catalog(maker) -> None:
    mixed = [
        {**CATARATA, "name": "Inativa", "is_active": False},
        {"name": "Sem orientação"},
        RESSONANCIA,
    ]
    config = await _clinic(maker, professionals=2, catalog=mixed)
    assert [g.name for g in config.service_guides] == ["Ressonância Magnética"]


# ----------------------------------------------------------------------- (E) tenant isolation


async def test_a_guide_of_one_clinic_never_reaches_another(maker) -> None:
    mine = await _clinic(maker, professionals=2, catalog=[CATARATA], clinic_name="Clínica Um")
    theirs = await _clinic(
        maker, professionals=2, catalog=[RESSONANCIA], clinic_name="Clínica Dois"
    )

    assert [g.name for g in mine.service_guides] == [SVC]
    assert [g.name for g in theirs.service_guides] == ["Ressonância Magnética"]

    assert (await _tool(mine, SVC))["orientacoes"] == REQS
    refused = await _tool(theirs, SVC)
    assert "error" in refused
    assert SVC not in refused["error"].split("Serviços disponíveis:", 1)[1]
    assert SVC not in prompts.secretary_system_prompt(theirs)
    assert (await _tool(theirs, "Ressonância Magnética"))["orientacoes"] == [
        "Retirar objetos metálicos"
    ]


# ------------------------------------------------------------------- (F) the not-found message


async def test_unknown_service_lists_the_union_of_both_sources_without_duplicates() -> None:
    config = _direct(
        [_type("Consulta"), _type("Retorno")],
        [_guide("retorno", requirements=["x"]), _guide(SVC, requirements=REQS)],
    )
    assert await _tool(config, "Raio-X") == {
        "error": (
            "Serviço 'Raio-X' não existe nesta clínica. "
            "Serviços disponíveis: Consulta, Retorno, Cirurgia de Catarata."
        )
    }


async def test_unknown_service_with_only_guides_still_names_them() -> None:
    config = _direct([], [_guide(SVC, requirements=REQS)])
    result = await _tool(config, "Raio-X")
    assert result["error"].endswith(f"Serviços disponíveis: {SVC}.")


async def test_unknown_service_without_any_source_keeps_the_current_wording() -> None:
    result = await _tool(_direct([]), "Raio-X")
    assert result == {
        "error": "Serviço 'Raio-X' não existe nesta clínica. Serviços disponíveis: nenhum."
    }


# ----------------------------------------------------------------------- (G) name matching


@pytest.mark.parametrize("asked", ["cirurgia de catarata", "  CIRURGIA  DE CATARATA ", SVC])
async def test_the_name_is_matched_ignoring_case_and_spacing(maker, asked) -> None:
    config = await _clinic(maker, professionals=2, catalog=[CATARATA])
    result = await _tool(config, asked)
    assert result["servico"] == SVC and result["orientacoes"] == REQS


async def test_the_name_is_matched_ignoring_accents_on_a_guide(maker) -> None:
    config = await _clinic(maker, professionals=2, catalog=[RESSONANCIA])
    result = await _tool(config, "ressonancia magnetica")
    assert result["servico"] == "Ressonância Magnética"  # the clinic's own spelling comes back


async def test_the_name_is_matched_ignoring_accents_on_the_effective_catalog_too() -> None:
    config = _direct([_type("Avaliação", requirements=["Trazer exames"])])
    result = await _tool(config, "avaliacao")
    assert result["servico"] == "Avaliação" and result["orientacoes"] == ["Trazer exames"]


# ------------------------------------------------------------------ (H) zero professionals


async def test_a_clinic_with_no_professional_still_loads_and_offers_only_its_tenant_list(
    maker,
) -> None:
    # Nobody can book a catalog-only service when there is no professional: no guide for it.
    config = await _clinic(maker, professionals=0, catalog=[CATARATA])
    assert config.professional_id is None
    assert [t.name for t in config.appointment_types] == ["Consulta"]
    assert config.service_guides == []
    assert GUIDES_LINE not in prompts.secretary_system_prompt(config)

    # What the TENANT list offers is bookable, so its orientations are known (and flagged on the
    # service line, not repeated in the extra line).
    config = await _clinic(maker, professionals=0, catalog=[CATARATA], tenant_extra=(SVC,))
    assert [g.name for g in config.service_guides] == [SVC]
    prompt = prompts.secretary_system_prompt(config)
    assert MARKER in prompt and GUIDES_LINE not in prompt
    assert (await _tool(config, SVC))["orientacoes"] == REQS


# ----------------------------------------------- a guide needs someone who OFFERS the service
# `POST /tenants/me/services` writes a catalog row only; a service reaches a professional through
# a separate edit of that professional's own list. So "in the catalog, offered by nobody" is the
# default state of every new service (and of one a professional just unticked): it is not
# bookable, so neither the prompt nor the tool may bring it up.

def _nothing_about_the_service(config) -> None:
    assert config.service_guides == []
    prompt = prompts.secretary_system_prompt(config)
    assert GUIDES_LINE not in prompt and SVC not in prompt
    assert prompt == _without_guides(config)


@pytest.mark.parametrize(
    "professionals, offered_by, entry_active",
    [
        (1, (False,), True),  # the sole professional's list is only "Consulta"
        (2, (False, False), True),  # neither of two offers it
        (1, (True,), False),  # the sole professional unticked it (entry kept, not offered)
        (2, (True, True), False),  # both unticked it
    ],
    ids=["sole-professional-lacks-it", "no-professional-has-it", "sole-unticked", "both-unticked"],
)
async def test_a_catalog_service_nobody_offers_is_not_a_guide(
    maker, professionals, offered_by, entry_active
) -> None:
    config = await _clinic(
        maker,
        professionals=professionals,
        catalog=[CATARATA],
        offered_by=offered_by,
        entry_active=entry_active,
    )
    _nothing_about_the_service(config)
    assert await _tool(config, SVC) == {
        "error": f"Serviço '{SVC}' não existe nesta clínica. Serviços disponíveis: Consulta."
    }


async def test_a_service_only_one_of_two_professionals_offers_keeps_its_guide(maker) -> None:
    # The production scenario: it must keep working after the offered-by-someone limit.
    config = await _clinic(
        maker, professionals=2, catalog=[CATARATA], offered_by=(True, False)
    )
    assert [g.name for g in config.service_guides] == [SVC]
    assert f"{GUIDES_LINE}{SVC}" in prompts.secretary_system_prompt(config)
    assert (await _tool(config, SVC))["orientacoes"] == REQS


async def test_only_the_offered_services_of_a_mixed_catalog_become_guides(maker) -> None:
    # Two catalog services with orientations. The tenant list offers the catarata; no
    # professional carries either, and nobody offers the ressonância at all.
    config = await _clinic(
        maker,
        professionals=2,
        catalog=[CATARATA, RESSONANCIA],
        tenant_extra=(SVC,),
        offered_by=(False, False),
    )
    assert [g.name for g in config.service_guides] == [SVC]
    prompt = prompts.secretary_system_prompt(config)
    assert "Ressonância" not in prompt and GUIDES_LINE not in prompt  # catarata is a type already
    assert "error" in await _tool(config, "Ressonância Magnética")


# ------------------------------------------------------------ defaults keep old callers valid


def test_a_config_built_without_guides_has_none_and_renders_no_line() -> None:
    config = _direct([_type("Consulta")])
    assert config.service_guides == []
    assert GUIDES_LINE not in prompts.secretary_system_prompt(config)
