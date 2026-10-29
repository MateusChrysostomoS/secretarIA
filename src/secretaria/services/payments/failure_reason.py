"""Closed vocabulary of Pix charge-failure reasons.

A failed charge is recorded as stage + reason + HTTP status, never as free text: `classify`
only ever returns a member of `FailureReason`, and anything it does not recognise is `unknown`.
`user_text` gives the plain-Portuguese sentence shown to the clinic (e-mail, badge).
"""

from enum import StrEnum


class FailureReason(StrEnum):
    customer_rejected = "customer_rejected"
    customer_document_invalid = "customer_document_invalid"
    charge_rejected = "charge_rejected"
    value_too_low = "value_too_low"
    account_not_enabled = "account_not_enabled"
    pix_key_missing = "pix_key_missing"
    qr_unavailable = "qr_unavailable"
    api_key_invalid = "api_key_invalid"
    permission_denied = "permission_denied"
    rate_limited = "rate_limited"
    provider_unavailable = "provider_unavailable"
    persist_failed = "persist_failed"
    no_account = "no_account"
    unknown = "unknown"


_CHARGE_TEXT_REASONS = {
    "value": FailureReason.value_too_low,
    "pix_key": FailureReason.pix_key_missing,
    "account": FailureReason.account_not_enabled,
}


def classify(
    stage: str, status_code: int, code_hint: str | None, text_reason: str | None
) -> FailureReason:
    """Map one failed provider call to a closed reason. Status rules come first; `text_reason`
    is a fixed keyword (never Asaas text) and only refines a 400. `code_hint` is accepted for
    the day a symbolic code is worth a rule; no rule uses it yet."""
    if status_code == 401:
        return FailureReason.api_key_invalid
    if status_code == 403:
        return FailureReason.permission_denied
    if status_code == 429:
        return FailureReason.rate_limited
    if status_code == 0 or status_code >= 500:
        return FailureReason.provider_unavailable
    if status_code == 400 and stage == "customer":
        if text_reason == "document":
            return FailureReason.customer_document_invalid
        return FailureReason.customer_rejected
    if status_code == 400 and stage == "charge":
        return _CHARGE_TEXT_REASONS.get(text_reason or "", FailureReason.charge_rejected)
    return FailureReason.unknown


_USER_TEXT: dict[FailureReason, str] = {
    FailureReason.customer_rejected: "O Asaas não aceitou o cadastro do paciente.",
    FailureReason.customer_document_invalid: (
        "O Asaas não aceitou o CPF ou CNPJ informado pelo paciente."
    ),
    FailureReason.charge_rejected: "O Asaas não aceitou a cobrança do sinal.",
    FailureReason.value_too_low: "O valor do sinal é menor que o mínimo aceito pelo Asaas.",
    FailureReason.account_not_enabled: (
        "A conta do Asaas ainda não está habilitada para receber cobranças."
    ),
    FailureReason.pix_key_missing: "A conta do Asaas ainda não tem uma chave Pix cadastrada.",
    FailureReason.qr_unavailable: (
        "O Asaas criou a cobrança, mas não devolveu o código Pix. "
        "Normalmente falta cadastrar uma chave Pix na conta do Asaas."
    ),
    FailureReason.api_key_invalid: (
        "A chave do Asaas guardada não é mais válida. Cole uma chave nova na conexão."
    ),
    FailureReason.permission_denied: "A chave do Asaas não tem permissão para criar cobranças.",
    FailureReason.rate_limited: "O Asaas recebeu pedidos demais e pediu para aguardar um pouco.",
    FailureReason.provider_unavailable: "O Asaas ficou fora do ar ou não respondeu a tempo.",
    FailureReason.persist_failed: "A cobrança foi criada, mas não conseguimos registrá-la aqui.",
    FailureReason.no_account: "A clínica ainda não conectou uma conta do Asaas.",
    FailureReason.unknown: "Não foi possível identificar o motivo da falha na cobrança.",
}


def user_text(reason: FailureReason) -> str:
    """Plain-Portuguese sentence for one reason: no names, documents, e-mails or provider text."""
    return _USER_TEXT[reason]


_ACTION_TEXT: dict[FailureReason, str] = {
    FailureReason.customer_rejected: (
        "Confira no painel do Asaas se a conta está liberada para cadastrar clientes"
    ),
    FailureReason.customer_document_invalid: (
        "Nada a fazer na clínica: peça ao paciente o CPF ou CNPJ correto "
        "ao confirmar a consulta com ele"
    ),
    FailureReason.charge_rejected: "Confira no painel do Asaas se a conta pode criar cobranças",
    FailureReason.value_too_low: (
        "Aumente o valor do sinal no Portal para pelo menos o mínimo aceito pelo Asaas"
    ),
    FailureReason.account_not_enabled: "Conclua o cadastro no painel do Asaas",
    FailureReason.pix_key_missing: "Cadastre uma chave Pix no painel do Asaas",
    FailureReason.qr_unavailable: "Cadastre uma chave Pix no painel do Asaas",
    FailureReason.api_key_invalid: "Gere uma nova chave de API e reconecte no Portal",
    FailureReason.permission_denied: (
        "Gere uma nova chave de API com permissão para cobranças e reconecte no Portal"
    ),
    FailureReason.rate_limited: "Nada a fazer: o Asaas costuma voltar ao normal em poucos minutos",
    FailureReason.provider_unavailable: (
        "Nada a fazer: o Asaas costuma voltar ao normal em poucos minutos"
    ),
    FailureReason.persist_failed: "Avise o suporte da SecretarIA e confira a cobrança no Asaas",
    FailureReason.no_account: "Conecte a conta do Asaas no Portal",
    FailureReason.unknown: "Confira o painel do Asaas e a conexão no Portal; se persistir, "
    "fale com o suporte",
}


def action_text(reason: FailureReason) -> str:
    """One-line "what to do" for the clinic, per reason. Kept next to `user_text`: both are
    fixed plain-Portuguese copy with no names, documents, e-mails or provider text."""
    return _ACTION_TEXT[reason]
