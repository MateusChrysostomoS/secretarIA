# ruff: noqa: E501
"""The owner's FULL pasted text (TASK-067): ten services, three doctors, plans.

`test_context_catalog_proposal.py` uses a shortened copy of this text (four services). A correct
model answer for the full text must end with every service on the right doctors; these tests pin
what the code does with such an answer. `full_extraction` is also reused by the local stand-in model.
"""

from types import SimpleNamespace

from secretaria.services.context_catalog_proposal import (
    ExtractedHours,
    ExtractedInsurancePlan,
    ExtractedProfessional,
    ExtractedService,
)
from secretaria.services.context_extraction import (
    ContextExtraction,
    build_proposal,
    verify_against_source,
)

SOURCE = """
Clínica Nova Saúde, referência em cuidado humanizado e medicina integrada. Nossa missão é oferecer um atendimento médico de excelência, unindo tecnologia e empatia.

Nossa Equipe Médica:
Contamos com profissionais altamente qualificados:

Dr. Carlos Mendes (Cardiologista) - CRM-SP 123456. Contato: carlos.mendes@novasaude.com.br. Atende às segundas, quartas e sextas, das 08:00 às 18:00.

Dra. Ana Paula Silva (Endocrinologista) - CRM-SP 654321. Contato: ana.silva@novasaude.com.br. Atende às terças e quintas, das 09:00 às 17:00.

Dr. Roberto Gomes (Clínico Geral e Geriatra) - CRM-SP 987654. Contato: roberto.gomes@novasaude.com.br. Atende de segunda a sexta, das 13:00 às 19:00.

Serviços e Valores Oferecidos:

Consulta em Cardiologia (60 min) - R$ 400,00 (Dr. Carlos)

Consulta em Endocrinologia (60 min) - R$ 400,00 (Dra. Ana Paula)

Consulta em Clínica Médica (40 min) - R$ 300,00 (Dr. Roberto)

Consulta em Geriatria (60 min) - R$ 350,00 (Dr. Roberto)

Eletrocardiograma - ECG (20 min) - R$ 150,00 (Dr. Carlos e Dr. Roberto)

Ecocardiograma (40 min) - R$ 350,00 (Dr. Carlos)

Exame de Bioimpedância (30 min) - R$ 180,00 (Dra. Ana Paula e Dr. Roberto)

Avaliação de Risco Cirúrgico (50 min) - R$ 450,00 (Dr. Carlos e Dr. Roberto)

Check-up Integrado (90 min) - R$ 800,00 (Dra. Ana Paula e Dr. Roberto)

Retorno de Consulta (30 min) - Gratuito se agendado em até 15 dias após a consulta inicial (Todos os profissionais).

Localização e Contato:
Estamos localizados na Avenida Paulista, 1000, Sala 302, Bloco B, Bairro Bela Vista, São Paulo - SP, CEP 01310-100.

Perguntas Frequentes (FAQ):

"Vocês aceitam plano de saúde?" R: Atualmente, aceitamos os convênios Bradesco Saúde, SulAmérica e Amil. Também trabalhamos com a modalidade de reembolso para outros planos.
"""

CARLOS = "Dr. Carlos Mendes (Cardiologista) - CRM-SP 123456. Contato: carlos.mendes@novasaude.com.br. Atende às segundas, quartas e sextas, das 08:00 às 18:00."
ANA = "Dra. Ana Paula Silva (Endocrinologista) - CRM-SP 654321. Contato: ana.silva@novasaude.com.br. Atende às terças e quintas, das 09:00 às 17:00."
ROBERTO = "Dr. Roberto Gomes (Clínico Geral e Geriatra) - CRM-SP 987654. Contato: roberto.gomes@novasaude.com.br. Atende de segunda a sexta, das 13:00 às 19:00."
PLANS_LINE = "aceitamos os convênios Bradesco Saúde, SulAmérica e Amil"

# name, minutes, price, who (None = everyone), the exact line of the text
SERVICES = [
    ("Consulta em Cardiologia", 60, "R$ 400,00", ["Dr. Carlos"], "Consulta em Cardiologia (60 min) - R$ 400,00 (Dr. Carlos)"),
    ("Consulta em Endocrinologia", 60, "R$ 400,00", ["Dra. Ana Paula"], "Consulta em Endocrinologia (60 min) - R$ 400,00 (Dra. Ana Paula)"),
    ("Consulta em Clínica Médica", 40, "R$ 300,00", ["Dr. Roberto"], "Consulta em Clínica Médica (40 min) - R$ 300,00 (Dr. Roberto)"),
    ("Consulta em Geriatria", 60, "R$ 350,00", ["Dr. Roberto"], "Consulta em Geriatria (60 min) - R$ 350,00 (Dr. Roberto)"),
    ("Eletrocardiograma - ECG", 20, "R$ 150,00", ["Dr. Carlos", "Dr. Roberto"], "Eletrocardiograma - ECG (20 min) - R$ 150,00 (Dr. Carlos e Dr. Roberto)"),
    ("Ecocardiograma", 40, "R$ 350,00", ["Dr. Carlos"], "Ecocardiograma (40 min) - R$ 350,00 (Dr. Carlos)"),
    ("Exame de Bioimpedância", 30, "R$ 180,00", ["Dra. Ana Paula", "Dr. Roberto"], "Exame de Bioimpedância (30 min) - R$ 180,00 (Dra. Ana Paula e Dr. Roberto)"),
    ("Avaliação de Risco Cirúrgico", 50, "R$ 450,00", ["Dr. Carlos", "Dr. Roberto"], "Avaliação de Risco Cirúrgico (50 min) - R$ 450,00 (Dr. Carlos e Dr. Roberto)"),
    ("Check-up Integrado", 90, "R$ 800,00", ["Dra. Ana Paula", "Dr. Roberto"], "Check-up Integrado (90 min) - R$ 800,00 (Dra. Ana Paula e Dr. Roberto)"),
    ("Retorno de Consulta", 30, "Gratuito", None, "Retorno de Consulta (30 min) - Gratuito se agendado em até 15 dias após a consulta inicial (Todos os profissionais)"),
]


def full_extraction() -> ContextExtraction:
    """What a complete, correct model answer for SOURCE looks like."""
    week = ["monday", "tuesday", "wednesday", "thursday", "friday"]
    return ContextExtraction(
        professionals=[
            ExtractedProfessional(name="Dr. Carlos Mendes", specialty="Cardiologista", about="CRM-SP 123456", email="carlos.mendes@novasaude.com.br",
                hours=[ExtractedHours(days=["monday", "wednesday", "friday"], start="08:00", end="18:00")], evidence=CARLOS),
            ExtractedProfessional(name="Dra. Ana Paula Silva", specialty="Endocrinologista", about="CRM-SP 654321", email="ana.silva@novasaude.com.br",
                hours=[ExtractedHours(days=["tuesday", "thursday"], start="09:00", end="17:00")], evidence=ANA),
            ExtractedProfessional(name="Dr. Roberto Gomes", specialty="Clínico Geral e Geriatra", about="CRM-SP 987654", email="roberto.gomes@novasaude.com.br",
                hours=[ExtractedHours(days=week, start="13:00", end="19:00")], evidence=ROBERTO),
        ],
        services=[
            ExtractedService(
                name=name,
                duration_min=minutes,
                price=price,
                professional_names=who or [],
                all_professionals=who is None,
                description="Gratuito se agendado em até 15 dias após a consulta inicial" if who is None else None,
                evidence=line,
            )
            for name, minutes, price, who, line in SERVICES
        ],
        insurance_plans=[
            ExtractedInsurancePlan(name="Bradesco Saúde", evidence=PLANS_LINE),
            ExtractedInsurancePlan(name="SulAmérica", evidence=PLANS_LINE),
            ExtractedInsurancePlan(name="Amil", evidence=PLANS_LINE),
            ExtractedInsurancePlan(name="Outros planos (reembolso)", how_it_works="reembolso para outros planos",
                evidence="Também trabalhamos com a modalidade de reembolso para outros planos."),
        ],
    )


def _proposal(extraction: ContextExtraction):
    return build_proposal(
        verify_against_source(extraction, SOURCE),
        current_facts=None,
        current_address=None,
        current_description=None,
        services=[],
        catalog_inputs={
            "roster": [],
            "catalog": [],
            "insurance_catalog": [
                SimpleNamespace(id="c-amil", name="Amil", aliases=[]),
                SimpleNamespace(id="c-bra", name="Bradesco Saúde", aliases=[]),
                SimpleNamespace(id="c-sul", name="SulAmérica", aliases=[]),
            ],
            "clinic_plan_names": [],
            "insurance_mode": None,
            "default_duration": 50,
        },
    )


def test_every_service_of_the_text_reaches_the_proposal_with_its_terms() -> None:
    proposal = _proposal(full_extraction())
    got = {s.name: s for s in proposal.catalog_services}
    assert set(got) == {row[0] for row in SERVICES}  # none of the ten is lost
    for name, minutes, price, who, _ in SERVICES:
        assert got[name].duration_min == minutes, name
        assert got[name].price == price, name
        assert got[name].everyone is (who is None), name
        if who:
            assert len(got[name].professionals) == len(who), (name, got[name].professionals)


def test_each_service_goes_to_the_doctors_the_text_names() -> None:
    proposal = _proposal(full_extraction())
    by_name = {s.name: s for s in proposal.catalog_services}
    carlos, ana, roberto = (p.name for p in proposal.professionals)
    assert (carlos, ana, roberto) == ("Dr. Carlos Mendes", "Dra. Ana Paula Silva", "Dr. Roberto Gomes")
    assert set(by_name["Avaliação de Risco Cirúrgico"].professionals) == {carlos, roberto}
    assert set(by_name["Check-up Integrado"].professionals) == {ana, roberto}
    assert set(by_name["Exame de Bioimpedância"].professionals) == {ana, roberto}
    assert set(by_name["Consulta em Endocrinologia"].professionals) == {ana}
    assert set(by_name["Consulta em Geriatria"].professionals) == {roberto}
    assert set(by_name["Ecocardiograma"].professionals) == {carlos}
