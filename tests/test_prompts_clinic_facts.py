"""The "SOBRE A CLÍNICA" prompt block and the service-guidance marker (TASK-025)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from secretaria.ai import prompts
from secretaria.ai.prompts import CLINIC_FACTS_BUDGET, secretary_system_prompt
from secretaria.services.tenant_config import RuntimeAppointmentType, TenantRuntimeConfig

GOLDEN = Path(__file__).parent / "golden" / "system_prompt_default.txt"
HEADING = "SOBRE A CLÍNICA"
FOOTER = "não invente: diga que vai confirmar com a equipe."


class _FixedDate(date):
    @classmethod
    def today(cls) -> date:  # type: ignore[override]
        return date(2026, 1, 15)


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompts, "date", _FixedDate)


def _config(**overrides) -> TenantRuntimeConfig:
    fields = dict(
        tenant_id=UUID(int=1),
        clinic_name="Clínica Teste",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="primary",
        google_refresh_token=None,
    )
    fields.update(overrides)
    return TenantRuntimeConfig(**fields)


def test_prompt_without_clinic_context_is_unchanged() -> None:
    """The reference file was generated BEFORE the change (tools/make_golden_prompt.py)."""
    expected = GOLDEN.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert secretary_system_prompt(_config()).replace("\r\n", "\n") == expected


def test_no_block_without_address_or_facts() -> None:
    prompt = secretary_system_prompt(_config(clinic_facts={}, address={}))
    assert HEADING not in prompt


def test_address_alone_creates_the_block() -> None:
    prompt = secretary_system_prompt(
        _config(address={"line": "Rua das Flores, 10", "city": "Recife", "state": "PE"})
    )
    assert HEADING in prompt
    assert "- Endereço: Rua das Flores, 10, Recife - PE" in prompt
    assert FOOTER in prompt


def test_block_carries_every_fact_and_the_footer() -> None:
    facts = {
        "parking": "Estacionamento conveniado ao lado.",
        "how_to_arrive": "Metrô Boa Viagem, saída B.",
        "payment_methods": ["Pix", "Cartão"],
        "cancellation_policy": "Cancele até 24h antes.",
        "documents_to_bring": ["RG", "Carteirinha"],
        "accessibility": "Rampa na entrada.",
        "faq": [{"question": "Tem wi-fi?", "answer": "Sim."}],
        "notes": "Fechamos em feriados.",
    }
    prompt = secretary_system_prompt(_config(clinic_facts=facts))
    for needle in (
        "- Como chegar: Metrô Boa Viagem, saída B.",
        "- Estacionamento: Estacionamento conveniado ao lado.",
        "- Formas de pagamento: Pix, Cartão",
        "- Cancelamento e remarcação: Cancele até 24h antes.",
        "- Documentos a levar: RG, Carteirinha",
        "- Acessibilidade: Rampa na entrada.",
        "Pergunta frequente — Tem wi-fi? Resposta: Sim.",
        "- Observações: Fechamos em feriados.",
        FOOTER,
    ):
        assert needle in prompt, needle


def test_block_sits_after_the_professional_section_and_before_post_consult() -> None:
    prompt = secretary_system_prompt(
        _config(
            specialty="Cardiologia",
            about="Dra. Ana",
            context_doctor_message="Olá",
            post_consult_knowledge="Retorno em 7 dias.",
            clinic_facts={"parking": "Garagem própria."},
        )
    )
    assert prompt.index("SOBRE O PROFISSIONAL") < prompt.index(HEADING)
    assert prompt.index(HEADING) < prompt.index("CONHECIMENTO PÓS-CONSULTA")


def _block(prompt: str) -> str:
    start = prompt.index(HEADING)
    return prompt[start : prompt.index(FOOTER, start) + len(FOOTER)]


def test_a_full_block_stays_inside_the_budget_and_drops_the_tail_first() -> None:
    facts = {
        "parking": "p" * 300,
        "how_to_arrive": "h" * 400,
        "payment_methods": ["x" * 60] * 10,
        "cancellation_policy": "c" * 500,
        "documents_to_bring": ["d" * 120] * 10,
        "accessibility": "a" * 300,
        "faq": [{"question": "q" * 150, "answer": "r" * 400}] * 15,
        "notes": "n" * 1000,
    }
    address = {"line": "Rua " + "z" * 200, "city": "Recife", "state": "PE"}
    prompt = secretary_system_prompt(_config(clinic_facts=facts, address=address))
    block = _block(prompt)
    assert len(block) <= CLINIC_FACTS_BUDGET
    assert "- Endereço:" in block  # the first priority is never the one dropped
    assert "- Observações:" not in block  # the last priority went first
    assert FOOTER in block  # the guardrail is never cut


def test_a_single_oversized_first_item_is_cut_not_dropped() -> None:
    prompt = secretary_system_prompt(_config(address={"line": "R" * 5000, "city": "X"}))
    block = _block(prompt)
    assert len(block) <= CLINIC_FACTS_BUDGET
    assert "- Endereço: RRR" in block and "…" in block


def _service(name: str, **kw) -> RuntimeAppointmentType:
    return RuntimeAppointmentType(
        name=name,
        description=kw.get("description"),
        duration_min=30,
        price=kw.get("price"),
        long_description=kw.get("long_description"),
        requirements=kw.get("requirements", []),
    )


def test_service_with_guidance_gets_the_marker_and_one_without_does_not() -> None:
    prompt = secretary_system_prompt(
        _config(
            appointment_types=[
                _service("Hemograma", requirements=["Jejum de 8 horas"]),
                _service("Consulta"),
                _service("Retorno", long_description="Traga os exames anteriores."),
            ]
        )
    )
    assert "- Hemograma (30 min) (há orientações: use get_service_info)" in prompt
    assert "- Consulta (30 min)\n" in prompt
    assert "- Retorno (30 min) (há orientações: use get_service_info)" in prompt


def test_object_catalog_entries_without_the_attributes_are_tolerated() -> None:
    legacy = SimpleNamespace(name="Consulta", duration_min=30, price=None, description=None)
    prompt = secretary_system_prompt(_config(appointment_types=[legacy]))
    assert "- Consulta (30 min)" in prompt and "get_service_info" not in prompt
