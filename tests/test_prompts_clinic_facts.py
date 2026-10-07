"""The "SOBRE A CLÍNICA" prompt block and the service-guidance marker (TASK-025)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from secretaria.ai.prompts import CLINIC_FACTS_BUDGET, secretary_system_prompt
from secretaria.services import booking_dates
from secretaria.services.tenant_config import RuntimeAppointmentType, TenantRuntimeConfig

GOLDEN = Path(__file__).parent / "golden" / "system_prompt_default.txt"
HEADING = "SOBRE A CLÍNICA"
FOOTER = "não invente: diga que vai confirmar com a equipe."
HEADING_PREFIX = "\n\n================ "


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None) -> datetime:
        return datetime(2026, 1, 15, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booking_dates, "datetime", _FixedDateTime)


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
    """Clinic-context isolation, with the approved clinic-local calendar reference."""
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
    """The TRUE block: from the heading's own prefix ("\\n\\n================ ") to the footer."""
    start = prompt.index(HEADING_PREFIX + HEADING)
    return prompt[start : prompt.index(FOOTER, start) + len(FOOTER)]


# The labels the renderer emits, in the priority order of SPEC F3 (address first, notes last).
LABELS_IN_PRIORITY_ORDER = (
    "- Endereço:",
    "- Como chegar:",
    "- Estacionamento:",
    "- Formas de pagamento:",
    "- Cancelamento e remarcação:",
    "- Documentos a levar:",
    "- Acessibilidade:",
    "- Pergunta frequente —",
    "- Observações:",
)

_MAX_ADDRESS = {"line": "Rua " + "z" * 200, "city": "Recife", "state": "PE"}
_MAX_FACTS = {
    "parking": "p" * 300,
    "how_to_arrive": "h" * 400,
    "payment_methods": ["x" * 60] * 10,
    "cancellation_policy": "c" * 500,
    "documents_to_bring": ["d" * 120] * 10,
    "accessibility": "a" * 300,
    "faq": [{"question": "q" * 150, "answer": "r" * 400}] * 15,
    "notes": "n" * 1000,
}
# Smaller fields: the first items fit and the cut falls later in the list (inside the FAQ).
_MID_FACTS = {
    "parking": "p" * 100,
    "how_to_arrive": "h" * 100,
    "payment_methods": ["Pix", "Cartão", "Dinheiro"],
    "cancellation_policy": "c" * 200,
    "documents_to_bring": ["RG", "Carteirinha"],
    "accessibility": "a" * 100,
    "faq": [{"question": "q" * 150, "answer": "r" * 400}] * 15,
    "notes": "n" * 1000,
}


@pytest.mark.parametrize(
    "facts", [_MAX_FACTS, _MID_FACTS], ids=["every-field-at-max", "cut-in-faq"]
)
def test_a_full_block_stays_inside_the_budget_and_keeps_a_priority_prefix(facts) -> None:
    prompt = secretary_system_prompt(_config(clinic_facts=facts, address=_MAX_ADDRESS))
    block = _block(prompt)
    assert len(block) <= CLINIC_FACTS_BUDGET
    present = [label in block for label in LABELS_IN_PRIORITY_ORDER]
    assert present[0]  # the first priority (the address) is never the one dropped
    assert not all(present)  # the fixture really overflows the budget: something was cut
    first_missing = present.index(False)
    # Once one label is absent, EVERY later one must be absent too: the list is cut at its tail.
    assert not any(present[first_missing:]), dict(
        zip(LABELS_IN_PRIORITY_ORDER, present, strict=True)
    )
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


# ----------------------------------------------------------------- malformed stored facts
# `tenants.clinic_facts` / `tenants.address` are JSON columns: a row written outside the schema
# (script, migration, manual fix) can hold any shape. A bad row must never break the turn.

_VALID_FAQ = {"question": "Tem wi-fi?", "answer": "Sim."}


@pytest.mark.parametrize(
    "faq",
    [
        [{"answer": "sem pergunta"}, _VALID_FAQ],
        [{"question": "sem resposta"}, _VALID_FAQ],
        ["texto solto", 7, None, _VALID_FAQ],
        [{"question": 1, "answer": 2}, _VALID_FAQ],
    ],
    ids=["no-question", "no-answer", "non-dict-items", "non-string-values"],
)
def test_malformed_faq_items_are_skipped_and_the_valid_ones_still_render(faq) -> None:
    prompt = secretary_system_prompt(_config(clinic_facts={"parking": "Garagem.", "faq": faq}))
    assert "- Estacionamento: Garagem." in prompt
    assert "Pergunta frequente — Tem wi-fi? Resposta: Sim." in prompt
    assert prompt.count("Pergunta frequente —") == 1


@pytest.mark.parametrize("faq", ["texto", dict(_VALID_FAQ), 5])
def test_a_faq_that_is_not_a_list_is_ignored(faq) -> None:
    prompt = secretary_system_prompt(_config(clinic_facts={"parking": "Garagem.", "faq": faq}))
    assert "- Estacionamento: Garagem." in prompt
    assert "Pergunta frequente" not in prompt


@pytest.mark.parametrize(
    "key,label",
    [
        ("payment_methods", "- Formas de pagamento: "),
        ("documents_to_bring", "- Documentos a levar: "),
    ],
)
def test_non_string_list_items_are_dropped_from_the_lists(key, label) -> None:
    items = ["Pix", 5, None, "  ", {"a": 1}, ["x"], " RG "]
    prompt = secretary_system_prompt(_config(clinic_facts={key: items}))
    assert f"{label}Pix, RG\n" in prompt


@pytest.mark.parametrize("key", ["payment_methods", "documents_to_bring"])
@pytest.mark.parametrize("value", ["Pix", 5, {"a": "b"}, [5, None, " "]])
def test_a_list_field_without_usable_text_adds_no_line(key, value) -> None:
    prompt = secretary_system_prompt(_config(clinic_facts={key: value}))
    assert HEADING not in prompt  # nothing usable anywhere, so the whole block is absent


@pytest.mark.parametrize("facts", [["parking"], "Estacionamento ao lado", 42, True])
def test_clinic_facts_that_are_not_a_mapping_are_ignored_but_the_address_still_renders(
    facts,
) -> None:
    prompt = secretary_system_prompt(
        _config(clinic_facts=facts, address={"line": "Rua das Flores, 10", "city": "Recife"})
    )
    assert "- Endereço: Rua das Flores, 10, Recife" in prompt
    assert FOOTER in prompt


@pytest.mark.parametrize("address", ["Rua das Flores, 10", ["Rua das Flores, 10"], 42, True])
def test_an_address_that_is_not_a_mapping_is_ignored_but_the_facts_still_render(address) -> None:
    prompt = secretary_system_prompt(
        _config(address=address, clinic_facts={"parking": "Garagem própria."})
    )
    assert "- Estacionamento: Garagem própria." in prompt
    assert "- Endereço:" not in prompt


def test_when_nothing_stored_is_usable_the_prompt_is_the_reference_prompt() -> None:
    expected = GOLDEN.read_text(encoding="utf-8").replace("\r\n", "\n")
    prompt = secretary_system_prompt(_config(clinic_facts=["x"], address="Rua A"))
    assert prompt.replace("\r\n", "\n") == expected
