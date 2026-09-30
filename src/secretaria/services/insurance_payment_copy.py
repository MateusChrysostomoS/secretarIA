_PAYMENT_DESCRIPTIONS = {
    'reembolso': 'Reembolso: você paga e solicita à operadora, conforme seu contrato.',
    'coparticipacao': 'Coparticipação: parte do custo pode ser cobrada de você.',
    'desconto_direto': 'Desconto direto: pode haver redução no valor pago à clínica.',
    'variavel_por_plano': 'Varia por plano: confirme cobertura e pagamento com a operadora.',
}
_UNKNOWN_DESCRIPTION = 'Pagamento: confirme as condições com a clínica.'

def insurance_payment_description(mechanism: object) -> str:
    if not isinstance(mechanism, str):
        return _UNKNOWN_DESCRIPTION
    return _PAYMENT_DESCRIPTIONS.get(mechanism, _UNKNOWN_DESCRIPTION)
