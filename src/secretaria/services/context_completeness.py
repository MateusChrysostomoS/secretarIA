"""What is still missing in a clinic's context - deterministic, no LLM (TASK-025).

`compute_completeness(tenant, services, professionals)` returns the checklist the manager sees
next to the context form and a 0-100 score. Required items weigh 1, recommended ones 0.5. A
required item that is not filled is `missing`; a recommended one is `optional`. Pure: it reads
attributes of the objects it is given and touches no database, so it is trivially testable.

`professionals` (the clinic's ACTIVE professionals) is what makes the booking items honest.
Without it, "hours" read the clinic's own column and "services" counted catalog rows — but a
patient books against each professional's RESOLVED hours and services, and a catalog row is
offered by nobody until a professional ticks it. A new one-doctor clinic could be told
"Pelo menos um serviço: feito" while its patients read "não há serviços disponíveis". With the
roster the checklist asks the same question the booking flow does
(services/tenant_config.py::professional_business_hours / professional_appointment_types) and
gains a "who attends" item. `None` keeps the roster-blind reading for callers without one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
    professional_business_hours,
)

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


def _names(names: list[str]) -> str:
    """Up to three names, then "e mais N" — the same shape as the guidance hint."""
    extra = len(names) - 3
    return ", ".join(names[:3]) + (f" e mais {extra}" if extra > 0 else "")


HOURS_HINT = "Defina os dias e horários em que a clínica atende."
SERVICES_HINT = "Cadastre os serviços que os pacientes podem agendar."
PROFESSIONAL_HINT = (
    'Diga quem atende os pacientes: você mesmo ("Sim, eu também atendo") ou um '
    "profissional convidado."
)

# (key, label, weight, done, hint-when-not-done, section)
_Spec = tuple[str, str, float, bool, str, str]


def _booking_items(
    tenant: Any, services: list[Any], active: list[Any], professionals: list[Any] | None
) -> list[_Spec]:
    """The items a patient's booking depends on: who attends, when, and what.

    Roster-aware when `professionals` is given (see the module docstring): every active
    professional must resolve to at least one window and one active service, because a
    professional without either is offered to nobody. With `None`, the roster-blind reading.
    """
    clinic_hours = _has_hours(getattr(tenant, "business_hours", None))
    if professionals is None:
        return [
            (
                "hours",
                "Horários de atendimento",
                REQUIRED_WEIGHT,
                clinic_hours,
                HOURS_HINT,
                "hours",
            ),
            (
                "services",
                "Pelo menos um serviço",
                REQUIRED_WEIGHT,
                bool(active),
                SERVICES_HINT,
                "services",
            ),
        ]

    without_hours = [p.name for p in professionals if not professional_business_hours(p, tenant)]
    hours_hint = (
        HOURS_HINT if not clinic_hours else f"Sem horário de atendimento: {_names(without_hours)}."
    )

    if professionals:
        without_services = [
            p.name for p in professionals if not professional_appointment_types(p, tenant, services)
        ]
        services_done = not without_services
    else:
        # Nobody attends: what the flow would offer is the clinic's own legacy list.
        without_services = []
        services_done = bool(active_appointment_types(tenant, services))
    if not active or not without_services:
        services_hint = SERVICES_HINT
    elif len(professionals) == 1:
        services_hint = (
            f"Nenhum serviço está marcado para {professionals[0].name}: marque ao menos um, "
            "com a duração."
        )
    else:
        services_hint = f"Sem serviço marcado: {_names(without_services)}."

    return [
        (
            "professional",
            "Profissional que atende",
            REQUIRED_WEIGHT,
            bool(professionals),
            PROFESSIONAL_HINT,
            "professionals",
        ),
        (
            "hours",
            "Horários de atendimento",
            REQUIRED_WEIGHT,
            clinic_hours and not without_hours,
            hours_hint,
            "hours",
        ),
        (
            "services",
            "Pelo menos um serviço",
            REQUIRED_WEIGHT,
            services_done,
            services_hint,
            "services",
        ),
    ]


def compute_completeness(
    tenant: Any, services: list[Any], professionals: list[Any] | None = None
) -> Completeness:
    # JSON column: a row written outside the schema can hold any shape; ignore a bad one.
    stored = getattr(tenant, "clinic_facts", None)
    facts = stored if isinstance(stored, dict) else {}
    faq = facts.get("faq")
    faq_count = len(faq) if isinstance(faq, list) else 0
    active = [s for s in services if getattr(s, "is_active", True)]
    gap = _guidance_gap(active)

    if not active:
        guidance_hint = "Cadastre os serviços primeiro."
    else:
        guidance_hint = f"Faltam orientações em: {_names(gap)}."

    # What a booking needs comes first: it is what a clinic setting itself up must see first.
    spec: list[_Spec] = [
        *_booking_items(tenant, services, active, professionals),
        (
            "address",
            "Endereço da clínica",
            REQUIRED_WEIGHT,
            _has_address(getattr(tenant, "address", None)),
            "Informe pelo menos a rua e a cidade.",
            "address",
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
            faq_count >= FAQ_TARGET,
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
