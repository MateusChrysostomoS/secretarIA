"""What is still missing in a clinic's context - deterministic, no LLM (TASK-025).

`compute_completeness(tenant, services)` returns the checklist the manager sees next to the
context form and a 0-100 score. Required items weigh 1, recommended ones 0.5. A required item
that is not filled is `missing`; a recommended one is `optional`. Pure: it reads attributes
of the objects it is given and touches no database, so it is trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Status = Literal["done", "missing", "optional"]

REQUIRED_WEIGHT = 1.0
RECOMMENDED_WEIGHT = 0.5
FAQ_TARGET = 3


@dataclass(frozen=True)
class CompletenessItem:
    key: str
    label: str
    status: Status
    hint: str | None
    section: str


@dataclass(frozen=True)
class Completeness:
    score: int
    items: list[CompletenessItem]


def _filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict | tuple):
        return len(value) > 0
    return True


def _has_hours(business_hours: Any) -> bool:
    return isinstance(business_hours, dict) and any(_filled(v) for v in business_hours.values())


def _has_address(address: Any) -> bool:
    return (
        isinstance(address, dict) and _filled(address.get("line")) and _filled(address.get("city"))
    )


def _has_guidance(service: Any) -> bool:
    return _filled(getattr(service, "requirements", None)) or _filled(
        getattr(service, "long_description", None)
    )


def _guidance_gap(active_services: list[Any]) -> list[str]:
    return [s.name for s in active_services if not _has_guidance(s)]


def compute_completeness(tenant: Any, services: list[Any]) -> Completeness:
    facts = getattr(tenant, "clinic_facts", None) or {}
    active = [s for s in services if getattr(s, "is_active", True)]
    gap = _guidance_gap(active)

    if not active:
        guidance_hint = "Cadastre os serviços primeiro."
    else:
        extra = len(gap) - 3
        names = ", ".join(gap[:3]) + (f" e mais {extra}" if extra > 0 else "")
        guidance_hint = f"Faltam orientações em: {names}."

    # (key, label, weight, done, hint-when-not-done, section)
    spec: list[tuple[str, str, float, bool, str, str]] = [
        (
            "address",
            "Endereço da clínica",
            REQUIRED_WEIGHT,
            _has_address(getattr(tenant, "address", None)),
            "Informe pelo menos a rua e a cidade.",
            "address",
        ),
        (
            "hours",
            "Horários de atendimento",
            REQUIRED_WEIGHT,
            _has_hours(getattr(tenant, "business_hours", None)),
            "Defina os dias e horários em que a clínica atende.",
            "hours",
        ),
        (
            "services",
            "Pelo menos um serviço",
            REQUIRED_WEIGHT,
            bool(active),
            "Cadastre os serviços que os pacientes podem agendar.",
            "services",
        ),
        (
            "parking_or_arrival",
            "Estacionamento ou como chegar",
            REQUIRED_WEIGHT,
            _filled(facts.get("parking")) or _filled(facts.get("how_to_arrive")),
            "Diga onde estacionar ou como chegar à clínica.",
            "facts",
        ),
        (
            "payment_methods",
            "Formas de pagamento",
            REQUIRED_WEIGHT,
            _filled(facts.get("payment_methods")),
            "Liste as formas de pagamento aceitas (Pix, cartão, dinheiro...).",
            "facts",
        ),
        (
            "cancellation_policy",
            "Política de cancelamento",
            REQUIRED_WEIGHT,
            _filled(facts.get("cancellation_policy")),
            "Explique prazos e regras para cancelar ou remarcar.",
            "facts",
        ),
        (
            "clinic_description",
            "Descrição da clínica (aparece na saudação)",
            RECOMMENDED_WEIGHT,
            _filled(getattr(tenant, "clinic_description", None)),
            "Uma frase sobre a clínica melhora a saudação.",
            "messages",
        ),
        (
            "returning_greeting_message",
            "Mensagem para quem volta à clínica",
            RECOMMENDED_WEIGHT,
            _filled(getattr(tenant, "returning_greeting_message", None)),
            "Escreva uma saudação para pacientes que já conhecem a clínica.",
            "messages",
        ),
        (
            "post_consult_message",
            "Mensagem depois da consulta",
            RECOMMENDED_WEIGHT,
            _filled(getattr(tenant, "post_consult_message", None)),
            "Escreva o que o paciente recebe depois de ser atendido.",
            "post_consult",
        ),
        (
            "post_consult_knowledge",
            "Orientações para depois da consulta",
            RECOMMENDED_WEIGHT,
            _filled(getattr(tenant, "post_consult_knowledge", None)),
            "Registre repouso, retorno e sinais de alerta para a secretária responder.",
            "post_consult",
        ),
        (
            "documents_to_bring",
            "Documentos a levar",
            RECOMMENDED_WEIGHT,
            _filled(facts.get("documents_to_bring")),
            "Liste o que o paciente deve levar (documento, carteirinha, exames).",
            "facts",
        ),
        (
            "faq",
            f"Perguntas frequentes (pelo menos {FAQ_TARGET})",
            RECOMMENDED_WEIGHT,
            len(facts.get("faq") or []) >= FAQ_TARGET,
            "Registre as perguntas que os pacientes mais fazem, com a resposta.",
            "facts",
        ),
        (
            "service_guidance",
            "Orientações em cada serviço (jejum, exames, preparo)",
            RECOMMENDED_WEIGHT,
            bool(active) and not gap,
            guidance_hint,
            "services",
        ),
        (
            "insurance_mode",
            "Modo de convênio definido",
            RECOMMENDED_WEIGHT,
            getattr(tenant, "insurance_mode", None) is not None,
            "Diga se a clínica atende por convênio e como.",
            "insurance",
        ),
    ]

    items: list[CompletenessItem] = []
    earned = total = 0.0
    for key, label, weight, done, hint, section in spec:
        total += weight
        if done:
            earned += weight
            items.append(CompletenessItem(key, label, "done", None, section))
        else:
            status: Status = "missing" if weight == REQUIRED_WEIGHT else "optional"
            items.append(CompletenessItem(key, label, status, hint, section))
    return Completeness(score=round(100 * earned / total), items=items)
