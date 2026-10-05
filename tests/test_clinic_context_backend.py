"""ClinicFacts schema, `get_service_info` tool and the completeness checklist (TASK-025)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from secretaria.ai import graph as ai_graph, tools as ai_tools
from secretaria.schemas.clinic_facts import ClinicFacts
from secretaria.services.context_completeness import FAQ_TARGET, compute_completeness
from secretaria.services.tenant_config import RuntimeAppointmentType, TenantRuntimeConfig

# --------------------------------------------------------------------------- schema


def test_every_field_is_optional() -> None:
    facts = ClinicFacts()
    assert facts.parking is None and facts.payment_methods == [] and facts.faq == []


def test_blank_text_becomes_none_and_blank_list_items_are_dropped() -> None:
    facts = ClinicFacts(parking="   ", payment_methods=["Pix", "  ", ""], documents_to_bring=[" "])
    assert facts.parking is None
    assert facts.payment_methods == ["Pix"]
    assert facts.documents_to_bring == []


@pytest.mark.parametrize(
    "payload",
    [
        {"parking": "p" * 301},
        {"how_to_arrive": "h" * 401},
        {"cancellation_policy": "c" * 501},
        {"accessibility": "a" * 301},
        {"notes": "n" * 1001},
        {"payment_methods": ["Pix"] * 11},
        {"payment_methods": ["x" * 61]},
        {"documents_to_bring": ["d" * 121]},
        {"faq": [{"question": "q", "answer": "a"}] * 16},
        {"faq": [{"question": "q" * 151, "answer": "a"}]},
        {"faq": [{"question": "q", "answer": "a" * 401}]},
        {"faq": [{"question": "", "answer": "a"}]},
    ],
)
def test_limits_are_enforced_on_the_server(payload) -> None:
    with pytest.raises(ValidationError):
        ClinicFacts(**payload)


def test_exactly_at_the_limits_is_accepted() -> None:
    ClinicFacts(
        parking="p" * 300,
        how_to_arrive="h" * 400,
        payment_methods=["x" * 60] * 10,
        cancellation_policy="c" * 500,
        documents_to_bring=["d" * 120] * 10,
        accessibility="a" * 300,
        faq=[{"question": "q" * 150, "answer": "a" * 400}] * 15,
        notes="n" * 1000,
    )


# ----------------------------------------------------------------------- the tool


def _config(types: list[RuntimeAppointmentType]) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
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


def _type(name, **kw) -> RuntimeAppointmentType:
    return RuntimeAppointmentType(
        name=name,
        description=kw.get("description"),
        duration_min=kw.get("duration", 30),
        price=kw.get("price"),
        long_description=kw.get("long_description"),
        requirements=kw.get("requirements", []),
    )


async def _call(config, name: str) -> dict:
    token = ai_tools._tenant_config_ctx.set(config)
    try:
        return await ai_tools.get_service_info.ainvoke({"service_name": name})
    finally:
        ai_tools._tenant_config_ctx.reset(token)


async def test_tool_returns_the_guidance_of_the_named_service() -> None:
    config = _config(
        [
            _type(
                "Hemograma",
                requirements=["Jejum de 8 horas"],
                price="R$ 40",
                duration=15,
                long_description="Coleta de sangue.",
                description="Exame",
            )
        ]
    )
    result = await _call(config, "  hemograma ")
    assert result == {
        "servico": "Hemograma",
        "duracao_min": 15,
        "preco": "R$ 40",
        "descricao": "Exame",
        "descricao_completa": "Coleta de sangue.",
        "orientacoes": ["Jejum de 8 horas"],
    }


async def test_tool_lists_the_valid_names_when_the_service_is_unknown() -> None:
    result = await _call(_config([_type("Consulta"), _type("Retorno")]), "Raio-X")
    assert "error" in result and "Consulta, Retorno" in result["error"]


async def test_tool_never_sees_another_tenants_catalog() -> None:
    mine = _config([_type("Hemograma", requirements=["Jejum"])])
    theirs = _config([_type("Ressonância", requirements=["Sem metais"])])
    assert "error" in await _call(mine, "Ressonância")
    assert (await _call(theirs, "Ressonância"))["orientacoes"] == ["Sem metais"]


async def test_tool_without_a_config_in_context_reports_no_service() -> None:
    result = await ai_tools.get_service_info.ainvoke({"service_name": "Qualquer"})
    assert "error" in result and "nenhum" in result["error"]


def test_tool_is_registered_for_every_booking_topology() -> None:
    for topology in ("sole", "multi", "tenant"):
        assert "get_service_info" in {t.name for t in ai_graph.base_tools_for(topology)}


# ------------------------------------------------------------------ completeness


def _tenant(**kw) -> SimpleNamespace:
    base = dict(
        address=None,
        business_hours={},
        clinic_facts=None,
        clinic_description=None,
        returning_greeting_message=None,
        post_consult_message=None,
        post_consult_knowledge=None,
        insurance_mode=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _svc(name="Consulta", active=True, requirements=None, long_description=None):
    return SimpleNamespace(
        name=name, is_active=active, requirements=requirements, long_description=long_description
    )


def _status(result) -> dict[str, str]:
    return {item.key: item.status for item in result.items}


def test_an_empty_clinic_scores_zero_and_everything_required_is_missing() -> None:
    result = compute_completeness(_tenant(), [])
    assert result.score == 0
    status = _status(result)
    for key in (
        "address",
        "hours",
        "services",
        "parking_or_arrival",
        "payment_methods",
        "cancellation_policy",
    ):
        assert status[key] == "missing"
    for key in (
        "clinic_description",
        "returning_greeting_message",
        "post_consult_message",
        "post_consult_knowledge",
        "documents_to_bring",
        "faq",
        "service_guidance",
        "insurance_mode",
    ):
        assert status[key] == "optional"


def test_a_fully_filled_clinic_scores_one_hundred_with_no_hints() -> None:
    tenant = _tenant(
        address={"line": "Rua A, 1", "city": "Recife"},
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        clinic_description="Clínica de olhos.",
        returning_greeting_message="Que bom ver você de novo!",
        post_consult_message="Obrigado pela visita.",
        post_consult_knowledge="Retorno em 7 dias.",
        insurance_mode="catalog",
        clinic_facts={
            "parking": "Garagem.",
            "payment_methods": ["Pix"],
            "cancellation_policy": "24h",
            "documents_to_bring": ["RG"],
            "faq": [{"question": "q", "answer": "a"}] * FAQ_TARGET,
        },
    )
    result = compute_completeness(tenant, [_svc(requirements=["Jejum"])])
    assert result.score == 100
    assert all(item.status == "done" and item.hint is None for item in result.items)


def test_either_parking_or_how_to_arrive_satisfies_the_arrival_item() -> None:
    result = compute_completeness(_tenant(clinic_facts={"how_to_arrive": "Metrô"}), [])
    assert _status(result)["parking_or_arrival"] == "done"


def test_address_needs_street_and_city() -> None:
    only_street = compute_completeness(_tenant(address={"line": "Rua A"}), [])
    assert _status(only_street)["address"] == "missing"


def test_guidance_hint_names_up_to_three_services_that_lack_it() -> None:
    services = [_svc(f"S{i}") for i in range(5)] + [_svc("Ok", requirements=["x"])]
    result = compute_completeness(_tenant(), services)
    item = next(i for i in result.items if i.key == "service_guidance")
    assert item.status == "optional" and item.hint == "Faltam orientações em: S0, S1, S2 e mais 2."


def test_inactive_services_do_not_count() -> None:
    result = compute_completeness(_tenant(), [_svc(active=False)])
    assert _status(result)["services"] == "missing"


def test_score_weights_required_items_double() -> None:
    required_only = _tenant(
        address={"line": "R", "city": "C"},
        business_hours={"monday": [{"start": "08:00", "end": "09:00"}]},
        clinic_facts={"parking": "x", "payment_methods": ["Pix"], "cancellation_policy": "y"},
    )
    score = compute_completeness(required_only, [_svc()]).score
    assert 0 < score < 100 and score == round(100 * 6 / (6 + 0.5 * 8))


def test_the_ready_made_messages_are_listed_with_the_section_that_edits_them() -> None:
    items = {i.key: i for i in compute_completeness(_tenant(), []).items}
    assert items["clinic_description"].section == "messages"
    assert items["returning_greeting_message"].section == "messages"
    assert items["post_consult_message"].section == "post_consult"
    assert items["post_consult_knowledge"].section == "post_consult"
    assert all(items[k].hint for k in items if items[k].status != "done")
