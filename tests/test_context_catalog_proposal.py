# ruff: noqa: E501 - the pasted clinic text keeps its real long lines
"""Professionals, services and convênios read from a source (TASK-067). No LLM call.

The source below is the text a clinic really pasted: three doctors with their days, services
(some shared by two doctors), the plans it takes. The "model" answers are written by hand the way
a good model would answer, plus the wrong answers a bad one gives, to prove what survives.
"""

from __future__ import annotations

from types import SimpleNamespace

from secretaria.services.context_catalog_proposal import (
    ExtractedHours,
    ExtractedInsurancePlan,
    ExtractedProfessional,
    ExtractedService,
    match_name,
    weekdays_named_in,
)
from secretaria.services.context_extraction import (
    ContextExtraction,
    ServiceGuidance,
    build_proposal,
    verify_against_source,
)

SOURCE = """
Dr. Carlos Mendes (Cardiologista) - CRM-SP 123456. Contato: carlos.mendes@novasaude.com.br. Atende às segundas, quartas e sextas, das 08:00 às 18:00.

Dra. Ana Paula Silva (Endocrinologista) - CRM-SP 654321. Contato: ana.silva@novasaude.com.br. Atende às terças e quintas, das 09:00 às 17:00.

Dr. Roberto Gomes (Clínico Geral e Geriatra) - CRM-SP 987654. Contato: roberto.gomes@novasaude.com.br. Atende de segunda a sexta, das 13:00 às 19:00.

Consulta em Cardiologia (60 min) - R$ 400,00 (Dr. Carlos)
Consulta em Clínica Médica (40 min) - R$ 300,00 (Dr. Roberto)
Eletrocardiograma - ECG (20 min) - R$ 150,00 (Dr. Carlos e Dr. Roberto)
Retorno de Consulta (30 min) - Gratuito se agendado em até 15 dias após a consulta inicial (Todos os profissionais).

Atualmente, aceitamos os convênios Bradesco Saúde, SulAmérica e Amil. Também trabalhamos com a modalidade de reembolso para outros planos.
"""

CARLOS = (
    "Dr. Carlos Mendes (Cardiologista) - CRM-SP 123456. Contato: carlos.mendes@novasaude.com.br. "
    "Atende às segundas, quartas e sextas, das 08:00 às 18:00."
)
ANA = (
    "Dra. Ana Paula Silva (Endocrinologista) - CRM-SP 654321. Contato: ana.silva@novasaude.com.br. "
    "Atende às terças e quintas, das 09:00 às 17:00."
)
ROBERTO = (
    "Dr. Roberto Gomes (Clínico Geral e Geriatra) - CRM-SP 987654. "
    "Contato: roberto.gomes@novasaude.com.br. Atende de segunda a sexta, das 13:00 às 19:00."
)
PLANS_LINE = "aceitamos os convênios Bradesco Saúde, SulAmérica e Amil"

INSURANCE_CATALOG = [
    SimpleNamespace(id="c-amil", name="Amil", aliases=[]),
    SimpleNamespace(id="c-bra", name="Bradesco Saúde", aliases=[]),
    SimpleNamespace(id="c-sul", name="SulAmérica", aliases=[]),
    SimpleNamespace(id="c-uni", name="Unimed", aliases=[]),
]


def _good() -> ContextExtraction:
    return ContextExtraction(
        professionals=[
            ExtractedProfessional(
                name="Dr. Carlos Mendes",
                specialty="Cardiologista",
                about="CRM-SP 123456",
                email="carlos.mendes@novasaude.com.br",
                hours=[
                    ExtractedHours(
                        days=["monday", "wednesday", "friday"], start="08:00", end="18:00"
                    )
                ],
                evidence=CARLOS,
            ),
            ExtractedProfessional(
                name="Dra. Ana Paula Silva",
                specialty="Endocrinologista",
                hours=[ExtractedHours(days=["tuesday", "thursday"], start="09:00", end="17:00")],
                evidence=ANA,
            ),
            ExtractedProfessional(
                name="Dr. Roberto Gomes",
                specialty="Clínico Geral e Geriatra",
                hours=[
                    ExtractedHours(
                        days=["monday", "tuesday", "wednesday", "thursday", "friday"],
                        start="13:00",
                        end="19:00",
                    )
                ],
                evidence=ROBERTO,
            ),
        ],
        services=[
            ExtractedService(
                name="Consulta em Cardiologia",
                duration_min=60,
                price="R$ 400,00",
                professional_names=["Dr. Carlos"],
                evidence="Consulta em Cardiologia (60 min) - R$ 400,00 (Dr. Carlos)",
            ),
            ExtractedService(
                name="Eletrocardiograma - ECG",
                duration_min=20,
                price="R$ 150,00",
                professional_names=["Dr. Carlos", "Dr. Roberto"],
                evidence="Eletrocardiograma - ECG (20 min) - R$ 150,00 (Dr. Carlos e Dr. Roberto)",
            ),
            ExtractedService(
                name="Retorno de Consulta",
                duration_min=30,
                price="Gratuito",
                description="Gratuito se agendado em até 15 dias após a consulta inicial",
                all_professionals=True,
                evidence=(
                    "Retorno de Consulta (30 min) - Gratuito se agendado em até 15 dias após a "
                    "consulta inicial (Todos os profissionais)"
                ),
            ),
        ],
        insurance_plans=[
            ExtractedInsurancePlan(name="Bradesco Saúde", evidence=PLANS_LINE),
            ExtractedInsurancePlan(name="SulAmérica", evidence=PLANS_LINE),
            ExtractedInsurancePlan(name="Amil", evidence=PLANS_LINE),
            ExtractedInsurancePlan(
                name="Outros planos (reembolso)",
                how_it_works="reembolso para outros planos",
                evidence="Também trabalhamos com a modalidade de reembolso para outros planos.",
            ),
        ],
        assumptions=["Entendi que o retorno vale também para exames."],
    )


def _proposal(extraction: ContextExtraction, source: str = SOURCE, **inputs):
    catalog_inputs = {
        "roster": [],
        "catalog": [],
        "insurance_catalog": INSURANCE_CATALOG,
        "clinic_plan_names": [],
        "insurance_mode": None,
        "default_duration": 50,
        **inputs,
    }
    return build_proposal(
        verify_against_source(extraction, source),
        current_facts=None,
        current_address=None,
        current_description=None,
        services=[],
        catalog_inputs=catalog_inputs,
    )


def _by_name(items, name):
    return next(i for i in items if i.name == name)


# ------------------------------------------------------------------ the pasted text, end to end


def test_the_three_doctors_come_out_with_specialty_and_week() -> None:
    proposal = _proposal(_good())
    names = [p.name for p in proposal.professionals]
    assert names == ["Dr. Carlos Mendes", "Dra. Ana Paula Silva", "Dr. Roberto Gomes"]
    carlos = _by_name(proposal.professionals, "Dr. Carlos Mendes")
    assert carlos.matched_id is None  # nobody by that name in the clinic yet
    assert carlos.specialty == "Cardiologista" and carlos.specialty_status == "new"
    assert carlos.about == "CRM-SP 123456"
    assert carlos.hours == {
        "monday": [{"start": "08:00", "end": "18:00"}],
        "wednesday": [{"start": "08:00", "end": "18:00"}],
        "friday": [{"start": "08:00", "end": "18:00"}],
    }
    roberto = _by_name(proposal.professionals, "Dr. Roberto Gomes")
    assert list(roberto.hours) == ["monday", "tuesday", "wednesday", "thursday", "friday"]


def test_a_shared_service_links_both_doctors_and_the_decision_is_written_down() -> None:
    proposal = _proposal(_good())
    ecg = _by_name(proposal.catalog_services, "Eletrocardiograma - ECG")
    assert ecg.professionals == ["Dr. Carlos Mendes", "Dr. Roberto Gomes"]
    assert (ecg.duration_min, ecg.price, ecg.duration_assumed) == (20, "R$ 150,00", False)
    texts = [n.text for n in proposal.notes]
    assert any("Eletrocardiograma" in t and "Repeti o mesmo" in t for t in texts)


def test_a_service_for_everyone_is_marked_and_explained() -> None:
    proposal = _proposal(_good())
    retorno = _by_name(proposal.catalog_services, "Retorno de Consulta")
    assert retorno.everyone is True and retorno.price == "Gratuito"
    assert any(
        "Retorno de Consulta" in n.text and "todos os profissionais" in n.text
        for n in proposal.notes
    )


def test_convenios_match_the_catalog_and_default_to_shared_by_all() -> None:
    proposal = _proposal(_good())
    insurance = proposal.insurance
    assert insurance is not None and insurance.applicable
    assert insurance.current_mode is None and insurance.default_mode == "shared"
    catalog_plans = {p.name: p.catalog_id for p in insurance.plans if p.catalog_id}
    assert catalog_plans == {"Bradesco Saúde": "c-bra", "SulAmérica": "c-sul", "Amil": "c-amil"}
    other = _by_name(insurance.plans, "Outros planos (reembolso)")
    assert other.catalog_id is None and "reembolso" in (other.custom_note or "")
    texts = [n.text for n in proposal.notes]
    assert any("todos os profissionais aceitam todos os convênios" in t for t in texts)
    assert any("“Outros planos (reembolso)”" in t and "Outro" in t for t in texts)


def test_the_models_own_account_of_its_choices_is_kept_and_labelled() -> None:
    proposal = _proposal(_good())
    own = [n for n in proposal.notes if n.origin == "model"]
    assert [n.text for n in own] == ["Entendi que o retorno vale também para exames."]
    assert own[0].kind == "decision"


def test_emails_are_a_hint_and_a_note_says_no_invite_was_sent() -> None:
    proposal = _proposal(_good())
    carlos = _by_name(proposal.professionals, "Dr. Carlos Mendes")
    assert carlos.email == "carlos.mendes@novasaude.com.br"
    note = next(n.text for n in proposal.notes if "e-mail" in n.text)
    assert "Não enviei convite" in note and "@" not in note


# ------------------------------------------------------------------------- what is dropped


def test_a_made_up_service_is_dropped() -> None:
    bad = _good()
    bad.services.append(
        ExtractedService(
            name="Endoscopia", price="R$ 900,00", evidence="Endoscopia digestiva por R$ 900,00"
        )
    )
    names = [s.name for s in _proposal(bad).catalog_services]
    assert "Endoscopia" not in names and len(names) == 3


def test_a_price_missing_from_the_quote_is_dropped_but_the_service_stays() -> None:
    bad = ExtractedService(
        name="Consulta em Cardiologia",
        duration_min=60,
        price="R$ 999,00",
        professional_names=["Dr. Carlos"],
        evidence="Consulta em Cardiologia (60 min) - R$ 400,00 (Dr. Carlos)",
    )
    proposal = _proposal(ContextExtraction(services=[bad]))
    service = proposal.catalog_services[0]
    assert service.price is None and service.duration_min == 60
    assert any("Sem valor no texto" in n.text for n in proposal.notes)


def test_a_duration_the_text_does_not_say_falls_back_and_says_so() -> None:
    svc = ExtractedService(
        name="Consulta em Cardiologia",
        duration_min=45,  # the quote says 60
        price="R$ 400,00",
        evidence="Consulta em Cardiologia (60 min) - R$ 400,00 (Dr. Carlos)",
    )
    proposal = _proposal(ContextExtraction(services=[svc]), default_duration=50)
    service = proposal.catalog_services[0]
    assert (service.duration_min, service.duration_assumed) == (50, True)
    assert any("duração padrão" in n.text and "50 min" in n.text for n in proposal.notes)


def test_a_day_the_quote_does_not_name_is_dropped_from_the_week() -> None:
    bad = _good()
    bad.professionals[1].hours = [
        ExtractedHours(days=["tuesday", "thursday", "saturday"], start="09:00", end="17:00")
    ]
    ana = _by_name(_proposal(bad).professionals, "Dra. Ana Paula Silva")
    assert list(ana.hours) == ["tuesday", "thursday"]


def test_hours_that_are_not_in_the_quote_are_dropped_and_the_gap_is_reported() -> None:
    bad = _good()
    bad.professionals[1].hours = [ExtractedHours(days=["tuesday"], start="10:00", end="12:00")]
    proposal = _proposal(bad)
    assert _by_name(proposal.professionals, "Dra. Ana Paula Silva").hours is None
    assert any("dias e horários" in n.text and "Ana Paula" in n.text for n in proposal.notes)


def test_a_doctor_whose_quote_is_not_in_the_source_is_dropped() -> None:
    bad = _good()
    bad.professionals.append(
        ExtractedProfessional(name="Dr. Inventado", evidence="Dr. Inventado atende às segundas")
    )
    assert "Dr. Inventado" not in [p.name for p in _proposal(bad).professionals]


def test_an_email_the_quote_does_not_carry_is_dropped() -> None:
    bad = _good()
    bad.professionals[1].email = "outra@pessoa.com"
    assert _by_name(_proposal(bad).professionals, "Dra. Ana Paula Silva").email is None


def test_a_plan_the_text_does_not_mention_is_dropped() -> None:
    bad = _good()
    bad.insurance_plans.append(
        ExtractedInsurancePlan(name="Unimed", evidence="Unimed é aceito aqui")
    )
    plans = [p.name for p in _proposal(bad).insurance.plans]
    assert "Unimed" not in plans


def test_a_source_without_catalog_data_produces_none_of_it() -> None:
    proposal = _proposal(ContextExtraction())
    assert proposal.professionals == [] and proposal.catalog_services == []
    assert proposal.insurance is None and proposal.notes == []


# ----------------------------------------------------------- the clinic already has things


def test_a_doctor_the_clinic_already_has_is_matched_not_duplicated() -> None:
    roster = [
        SimpleNamespace(
            id="p1", name="Carlos Mendes", specialty=None, about=None, business_hours=None
        )
    ]
    proposal = _proposal(_good(), roster=roster)
    carlos = _by_name(proposal.professionals, "Dr. Carlos Mendes")
    assert carlos.matched_id == "p1" and carlos.matched_name == "Carlos Mendes"
    assert carlos.specialty_status == "new" and carlos.hours_status == "new"


def test_what_the_clinic_already_wrote_is_reported_as_different_not_replaced() -> None:
    roster = [
        SimpleNamespace(
            id="p1",
            name="Carlos Mendes",
            specialty="Clínico",
            about=None,
            business_hours={"monday": [{"start": "10:00", "end": "12:00"}]},
        )
    ]
    carlos = _by_name(_proposal(_good(), roster=roster).professionals, "Dr. Carlos Mendes")
    assert carlos.specialty_status == "different" and carlos.hours_status == "different"


def test_a_service_already_in_the_catalog_is_matched() -> None:
    catalog = [SimpleNamespace(id="s1", name="Consulta em cardiologia")]
    proposal = _proposal(_good(), catalog=catalog)
    assert _by_name(proposal.catalog_services, "Consulta em Cardiologia").matched_id == "s1"


def test_convenios_per_professional_mode_is_left_alone_with_a_note() -> None:
    proposal = _proposal(_good(), insurance_mode="independent")
    assert proposal.insurance.applicable is False and proposal.insurance.default_mode is None
    assert any("convênio por profissional" in n.text for n in proposal.notes)


def test_an_existing_mode_is_not_overridden_by_the_default() -> None:
    proposal = _proposal(_good(), insurance_mode="clinic_with_exceptions")
    assert proposal.insurance.default_mode is None
    assert not any("todos os profissionais aceitam todos" in n.text for n in proposal.notes)


def test_a_shorter_spelling_maps_to_the_catalog_and_is_flagged_as_a_decision() -> None:
    extraction = ContextExtraction(
        insurance_plans=[ExtractedInsurancePlan(name="Bradesco", evidence=PLANS_LINE)]
    )
    proposal = _proposal(extraction)
    plan = proposal.insurance.plans[0]
    assert (plan.catalog_id, plan.loose) == ("c-bra", True)
    assert any("“Bradesco” como “Bradesco Saúde”" in n.text for n in proposal.notes)


def test_a_plan_that_extends_a_catalog_name_is_not_forced_into_it() -> None:
    extraction = ContextExtraction(
        insurance_plans=[
            ExtractedInsurancePlan(
                name="Unimed Paulistana", evidence="Atendemos Unimed Paulistana por reembolso"
            )
        ]
    )
    proposal = _proposal(extraction, source="Atendemos Unimed Paulistana por reembolso.")
    plan = proposal.insurance.plans[0]
    assert plan.catalog_id is None and plan.custom_note


def test_guidance_for_a_service_this_source_creates_is_not_listed_twice() -> None:
    extraction = _good()
    extraction.service_guidance = [
        ServiceGuidance(
            service_name="Eletrocardiograma - ECG",
            requirements=["Levar exames anteriores"],
            evidence="Eletrocardiograma - ECG (20 min)",
        )
    ]
    assert _proposal(extraction).services == []


# --------------------------------------------------------------------------- helpers


def test_weekday_phrases() -> None:
    weekdays = {"monday", "tuesday", "wednesday", "thursday", "friday"}
    assert weekdays_named_in("às segundas, quartas e sextas") == {"monday", "wednesday", "friday"}
    assert weekdays_named_in("de segunda a sexta") == weekdays
    assert weekdays_named_in("terças-feiras e sábados") == {"tuesday", "saturday"}
    assert weekdays_named_in("atende em dias úteis") == weekdays
    assert weekdays_named_in("sem dias") == set()


def test_name_matching_is_exact_enough_to_refuse_a_guess() -> None:
    known = ["Dr. Carlos Mendes", "Dra. Ana Paula Silva"]
    assert match_name("Dr. Carlos", known) == 0
    assert match_name("Ana Paula", known) == 1
    assert match_name("Dr. Roberto", known) is None
    assert match_name("Dr. Carlos", ["Carlos Mendes", "Carlos Souza"]) is None  # ambiguous
