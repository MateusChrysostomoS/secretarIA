"""The LLM may not announce an action it did not perform.

## The bug that produced this module

2026-09-17, production, a real clinic. A visitor waiting in
`FlowState.AWAITING_EMAIL_CODE` typed "não recebi o código, pode reenviar?".
That is not six digits, so the wait ended and the turn was routed normally —
to the generic LLM, which has no verification tool of any kind. The patient's
NEXT message was the correct code, it reached the LLM as ordinary text, and
the LLM answered:

    "Pronto — código verificado e sua conta foi ativada. ✅"

Nothing had been verified. That string exists nowhere in this repository; the
model wrote it because the conversation looked like it was time for it. The
real challenge was still open at brain-api, which went on refusing the
account, and the patient had been told the opposite.

## Why a system-prompt rule is not enough on its own

`ai/prompts.py` now carries an explicit rule forbidding this (rule 5 of the
REGRAS INEGOCIÁVEIS block), and it belongs there. But a prompt is a request,
and the failure mode being defended against IS the model doing something it
was not asked to do. So the rule is paired with this filter, which is not a
request: the reply is read after the model produced it, and a claim about a
sensitive action that no tool performed in the same turn never reaches the
patient.

## The shape of the rule

A claim is allowed only when the matching action is in `proven_actions` — the
set of sensitive actions a TOOL actually completed during this turn. Today the
agent has no tool that can verify an identity or confirm a payment, so that
set is empty on every LLM turn and every such claim is blocked. When a tool
for one of them is added, it adds its own key and the sentence becomes
sayable — the guard is a ratchet on evidence, not a blocklist of words that
has to be maintained in step with the product.

Deliberately NOT a redaction of the offending sentence: a reply built around a
false confirmation is not repaired by deleting the confirmation, because
everything the model wrote after it assumed the claim was true. The whole turn
is replaced by one honest message that also tells the patient what WOULD work.
"""

import re
from dataclasses import dataclass

from secretaria.core.logging import get_logger

logger = get_logger(__name__)

# Action keys. A caller proves one by passing it in `proven_actions`, which it
# may only do when a tool in THIS turn actually did the thing.
ACTION_IDENTITY_VERIFIED = "identity_verified"
ACTION_PAYMENT_RECEIVED = "payment_received"


@dataclass(frozen=True)
class _Claim:
    action: str
    pattern: re.Pattern[str]


def _p(source: str) -> re.Pattern[str]:
    return re.compile(source, re.IGNORECASE)


# Written against what the model actually produces in pt-BR, not against a
# grammar. Each entry is one WAY OF SAYING a completed action, with enough
# context that a question ("seu código foi verificado?") or a future tense
# ("assim que o código for verificado") does not match — the filter must not
# fire on the sentences that are the correct thing to say.
_CLAIMS: tuple[_Claim, ...] = (
    # "código verificado", "código foi verificado", "código confirmado".
    _Claim(
        ACTION_IDENTITY_VERIFIED,
        _p(r"c[óo]digo\s+(j[áa]\s+)?(foi\s+)?(verificad|confirmad|validad|aceit)[oa]"),
    ),
    # "verifiquei o seu código", "validei o código", "confirmei seu código".
    _Claim(
        ACTION_IDENTITY_VERIFIED,
        _p(r"\b(verifiquei|validei|confirmei)\b[^.?!]{0,40}\bc[óo]digo"),
    ),
    # "conta ativada", "sua conta está ativa", "cadastro concluído".
    _Claim(
        ACTION_IDENTITY_VERIFIED,
        _p(r"\b(conta|cadastro)\b[^.?!]{0,30}\b(ativad[oa]|ativ[oa]|criad[oa]|conclu[íi]d[oa])\b"),
    ),
    # "ativei sua conta", "criei o seu cadastro".
    _Claim(
        ACTION_IDENTITY_VERIFIED,
        _p(r"\b(ativei|criei)\b[^.?!]{0,30}\b(sua\s+|seu\s+|o\s+|a\s+)?(conta|cadastro)\b"),
    ),
    # "e-mail verificado", "identidade confirmada".
    _Claim(
        ACTION_IDENTITY_VERIFIED,
        _p(r"\b(e-?mail|identidade)\b[^.?!]{0,25}\b(verificad|confirmad|validad)[oa]\b"),
    ),
    # "pagamento confirmado/recebido/aprovado".
    _Claim(
        ACTION_PAYMENT_RECEIVED,
        _p(r"\bpagamento\b[^.?!]{0,30}\b(confirmad|recebid|aprovad|identificad)[oa]\b"),
    ),
    # "recebemos o seu pagamento", "identifiquei o pagamento".
    _Claim(
        ACTION_PAYMENT_RECEIVED,
        _p(r"\b(recebemos|recebi|identifiquei)\b[^.?!]{0,30}\b(o\s+)?(seu\s+)?pagamento\b"),
    ),
)

# One honest message for every blocked claim. It does not accuse the model, does
# not expose the mechanism, and names the ONE action that actually works — which
# is what the patient in the original incident needed and did not get.
UNBACKED_CLAIM_MESSAGE = (
    "Opa, deixa eu conferir isso direito. 😅\n\n"
    "Não consigo confirmar esse passo por aqui agora.\n\n"
    "✍️ Se você está esperando a verificação do *código de 6 dígitos*, é só "
    "digitar os 6 números que chegaram no seu e-mail — aí eu confirmo na hora."
)


def unbacked_claim(text: str, *, proven_actions: frozenset[str] = frozenset()) -> str | None:
    """The action `text` claims without evidence, or None if it claims nothing.

    Pure. Returns the ACTION key rather than a boolean so the caller's log line
    can name what was blocked without ever logging the patient-facing text.
    """
    if not text:
        return None
    for claim in _CLAIMS:
        if claim.action in proven_actions:
            continue
        if claim.pattern.search(text):
            return claim.action
    return None


def guard_reply(text: str, *, proven_actions: frozenset[str] = frozenset(), **log_fields) -> str:
    """`text`, or the honest replacement when it claims an unproven action.

    The ONE call site is the LLM leg of `workers/tasks.py`. Deterministic
    replies do not pass through here on purpose: every one of them is a
    constant in this repository, so what they assert is decided by a code
    review rather than by a sampler.
    """
    action = unbacked_claim(text, proven_actions=proven_actions)
    if action is None:
        return text
    logger.warning(
        "llm_unbacked_claim_blocked",
        action=action,
        # The reply itself is patient content and never goes to a log; its
        # length is enough to tell a one-liner from a long improvisation.
        reply_length=len(text),
        **log_fields,
    )
    return UNBACKED_CLAIM_MESSAGE
