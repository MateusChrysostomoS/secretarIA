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
        "O Asaas não aceitou o CPF informado pelo paciente."
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
    FailureReason.api_key_invalid: "A chave do Asaas guardada não é mais válida.",
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


def coerce(value: "FailureReason | str | None") -> FailureReason | None:
    """A stored or carried reason back into the closed vocabulary: None/blank stays None (no
    reason), anything not in the vocabulary becomes `unknown` - never raises, so a stray value
    can never be stored or shown as it is."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return FailureReason(value)
    except ValueError:
        return FailureReason.unknown


def text_for(value: "FailureReason | str | None") -> str | None:
    """`user_text` of a stored reason (unknown values read as `unknown`), None without one."""
    reason = coerce(value)
    return user_text(reason) if reason is not None else None


_ACTION_TEXT: dict[FailureReason, str] = {
    FailureReason.customer_rejected: (
        "Confira no painel do Asaas se a conta está liberada para cadastrar clientes"
    ),
    FailureReason.customer_document_invalid: (
        "Nada a fazer na clínica: peça ao paciente o CPF correto "
        "ao confirmar a consulta com ele"
    ),
    FailureReason.charge_rejected: "Confira no painel do Asaas se a conta pode criar cobranças",
    FailureReason.value_too_low: (
        "Aumente o valor do sinal no Portal para pelo menos o mínimo aceito pelo Asaas"
    ),
    FailureReason.account_not_enabled: "Conclua o cadastro no painel do Asaas",
    FailureReason.pix_key_missing: "Cadastre uma chave Pix no painel do Asaas",
    FailureReason.qr_unavailable: "Cadastre uma chave Pix no painel do Asaas",
    FailureReason.api_key_invalid: (
        "Gere uma chave nova no painel do Asaas e cole na configuração do sinal no Portal"
    ),
    FailureReason.permission_denied: (
        "Gere no painel do Asaas uma chave nova com permissão para cobranças e cole na "
        "configuração do sinal no Portal"
    ),
    FailureReason.rate_limited: "Nada a fazer: o Asaas costuma voltar ao normal em poucos minutos",
    FailureReason.provider_unavailable: (
        "Nada a fazer: o Asaas costuma voltar ao normal em poucos minutos"
    ),
    FailureReason.persist_failed: "Avise o suporte da SecretarIA e confira a cobrança no Asaas",
    FailureReason.no_account: "Conecte a conta do Asaas no Portal",
    FailureReason.unknown: "Avise o suporte da SecretarIA",
}


def action_text(reason: FailureReason) -> str:
    """One-line "what to do" for the clinic, per reason. Kept next to `user_text`: both are
    fixed plain-Portuguese copy with no names, documents, e-mails or provider text."""
    return _ACTION_TEXT[reason]


class ActionLink(StrEnum):
    """Where the clinic fixes a reason: the Asaas panel or the deposit settings in the Portal."""

    asaas_panel = "asaas_panel"
    portal_deposit = "portal_deposit"


# Owner's decision (2026-10-29): the e-mail carries a link straight to where the "O que fazer"
# happens. Reasons with nothing for the clinic to do (patient's CPF, Asaas busy or down, unknown
# -> support) carry none.
_ACTION_LINKS: dict[FailureReason, tuple[ActionLink, ...]] = {
    FailureReason.customer_rejected: (ActionLink.asaas_panel,),
    FailureReason.customer_document_invalid: (),
    FailureReason.charge_rejected: (ActionLink.asaas_panel,),
    FailureReason.value_too_low: (ActionLink.portal_deposit,),
    FailureReason.account_not_enabled: (ActionLink.asaas_panel,),
    FailureReason.pix_key_missing: (ActionLink.asaas_panel,),
    FailureReason.qr_unavailable: (ActionLink.asaas_panel,),
    FailureReason.api_key_invalid: (ActionLink.asaas_panel, ActionLink.portal_deposit),
    FailureReason.permission_denied: (ActionLink.asaas_panel, ActionLink.portal_deposit),
    FailureReason.rate_limited: (),
    FailureReason.provider_unavailable: (),
    FailureReason.persist_failed: (ActionLink.asaas_panel,),
    FailureReason.no_account: (ActionLink.portal_deposit,),
    FailureReason.unknown: (),
}


def action_links(reason: FailureReason) -> tuple[ActionLink, ...]:
    """The places (in order) the e-mail links to for this reason; empty when there is none."""
    return _ACTION_LINKS[reason]
