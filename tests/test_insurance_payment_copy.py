import pytest

from secretaria.services.insurance_payment_copy import insurance_payment_description
from secretaria.services.whatsapp import interactive_list_record


@pytest.mark.parametrize(('mechanism', 'expected'), [
    ('reembolso', 'Reembolso: você paga e solicita à operadora, conforme seu contrato.'),
    ('coparticipacao', 'Coparticipação: parte do custo pode ser cobrada de você.'),
    ('desconto_direto', 'Desconto direto: pode haver redução no valor pago à clínica.'),
    ('variavel_por_plano', 'Varia por plano: confirme cobertura e pagamento com a operadora.'),
    ('desconhecido', 'Pagamento: confirme as condições com a clínica.'),
    (None, 'Pagamento: confirme as condições com a clínica.'),
    ('invalid', 'Pagamento: confirme as condições com a clínica.'),
    ({}, 'Pagamento: confirme as condições com a clínica.'),
])
def test_payment_description(mechanism, expected):
    description = insurance_payment_description(mechanism)
    assert description == expected
    assert len(description) <= 72
    record = interactive_list_record('Escolha', 'Ver convênios',
        [('ins|Exemplo', 'Exemplo', description)], 'Convênios')
    assert record['options'][0]['description'] == expected
    assert record['options'][0]['id'] == 'ins|Exemplo'
