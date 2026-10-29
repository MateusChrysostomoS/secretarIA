"""What a payment provider needs to know about the PAYER, held only for the seconds it needs it.

The provider decides which field is required (decision D7, generalised): Asaas refuses a new
customer without a CPF/CNPJ (`ProviderError("payer_document_required")`), Mercado Pago refuses a
charge without the payer's e-mail (`ProviderError("payer_email_required")`). Nothing is asked until
the provider says so, only the missing field is asked, and the answer is kept for one provider call.
This module is the whole policy:

  * the pure part: which field a provider code means, one patient turn read, the copy, the
    redaction rule for the message history;
  * the transient store: Redis, encrypted with the same Fernet point as every tenant secret
    (`core/crypto.py`), keyed by field + tenant + conversation, expiring on its own.

WHERE THE VALUES LIVE, AND FOR HOW LONG
---------------------------------------
Only in the Redis keys `payer_data:<field>:<tenant>:<conversation>`, as Fernet ciphertext, for at
most `DATA_TTL_SECONDS` (15 minutes: shorter than the shortest hold a clinic can configure, 30
minutes). They are deleted the moment the provider call that needed them has been made, and on
every way out of the wait (opt-out, three invalid answers, expiry, `/menu`). They are NEVER written
to a database column, a log line, a structlog context, an LLM prompt or history, an attention
item, the deposit row or the booking snapshot. The provider keeps what it needs to issue the charge
(that is what the patient is told); what WE keep after it is the provider customer id only.

WHY THE PATIENT'S ANSWER NEVER REACHES THE MESSAGE HISTORY (nor the LLM)
------------------------------------------------------------------------
The answer is an ordinary inbound message. `should_redact_inbound` makes the worker store the fixed
`REDACTED_BODY` instead of what was typed (the same boundary that already hides the portal's
e-mail code), so the raw value never becomes a `messages` row: not for the staff console, not for
`ai/graph.py::_load_history`, not for the LLM. This is stricter than the
`pii-field-capture-pseudonymization` recipe (which registers a STORED value with the pseudonymizer)
because here there is no stored value to register; the pseudonymizer (CPF and e-mail by pattern, NOT
CNPJ) stays as the second net for a value typed unprompted.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from secretaria.core import crypto
from secretaria.core.logging import get_logger
from secretaria.services.payments.payer_document import extract_document
from secretaria.services.payments.payer_email import extract_email

logger = get_logger(__name__)

Kind = Literal["document", "email"]
Timing = Literal["before", "after"]
KINDS: tuple[Kind, ...] = ("document", "email")

# The provider says which field it needs: the codes are the `ProviderError` codes of the wrappers
# (F1 `AsaasProvider`, F3 `MercadoPagoProvider`). A new provider that needs a field adds ONE line.
KIND_FOR_CODE: dict[str, Kind] = {
    "payer_document_required": "document",
    "payer_email_required": "email",
}


def kind_for_code(code: str | None) -> Kind | None:
    return KIND_FOR_CODE.get(code or "")


# How the copy names the provider that receives the value.
PROVIDER_LABELS = {"asaas": "Asaas", "mercadopago": "Mercado Pago"}

# Shorter than `deposit_gate.HOLD_TTL_MIN_MINUTES` (30) on purpose: a value can never outlive the
# reservation it was collected for (a test pins it).
DATA_TTL_SECONDS = 15 * 60
MAX_INVALID_ATTEMPTS = 3

# `Conversation.flow_step` (VARCHAR(48)) carries "payer_data:<timing>:<doc|mail>:<invalid answers so
# far>": a counter that is not personal data, in the column the flow already owns, no migration.
STEP_PREFIX = "payer_data:"
_TAG: dict[str, str] = {"document": "doc", "email": "mail"}
_KIND_OF_TAG = {tag: kind for kind, tag in _TAG.items()}

REDACTED_BODY = "[dado oculto]"
# The one sentence that asks for the CPF/CNPJ (single source for WhatsApp and Portal).
ASK_DOCUMENT_TEXT = (
    "Envie seu CPF para que possamos cadastrá-lo no nosso sistema de pagamento para que você "
    "consiga marcar a sua consulta."
)
# No new request sends the opt-out button, but a chat that was sent one earlier can still tap it
# (and a typed "não quero" goes through `looks_like_optout`): keep the label and the id.
LABEL_OPTOUT = "Não quero informar"
OPTOUT_REPLY_ID = "payer_data_optout"
_AWAITING_STATE = "AWAITING_PAYER_DATA"
_WAITING_FOR_PIX_STATE = "AWAITING_DEPOSIT"
_CATALOG_STATE = "SERVICE_CATALOG"


class PayerDataNeeded(Exception):
    """`maybe_create_deposit(collect_payer_data=True)`: the provider needs `kind` and the patient
    was never asked (the `after` timing asks AFTER the consultation exists). Carries no value."""

    def __init__(self, kind: Kind, provider: str | None) -> None:
        super().__init__(f"payer_data_needed:{kind}")
        self.kind = kind
        self.provider = provider


@dataclass(frozen=True)
class PayerData:
    """What was collected for one conversation. Neither field ever appears in a repr."""

    document: str | None = field(default=None, repr=False)
    email: str | None = field(default=None, repr=False)

    def has(self, kind: Kind) -> bool:
        return (self.document if kind == "document" else self.email) is not None

    def any(self) -> bool:
        return self.document is not None or self.email is not None


# ---- copy (pt-BR; none of it repeats what the patient typed) ----------------------------------


def _noun(kind: Kind) -> str:
    return "CPF ou CNPJ" if kind == "document" else "e-mail"


def ask_text(kind: Kind, provider: str | None) -> str:
    """The question. For the CPF/CNPJ it is ONE plain sentence (`ASK_DOCUMENT_TEXT`, owner
    decision 2026-10-28), on both channels and for any provider, and NO request carries the opt-out
    button any more (a typed "não quero" and an old button tap are still understood). The e-mail
    variant (Mercado Pago) keeps its explanation, minus the button."""
    if kind == "document":
        return ASK_DOCUMENT_TEXT
    name = PROVIDER_LABELS.get(provider or "", "provedor de pagamento")
    return (
        f"Para gerar o Pix, o {name} (nosso provedor de pagamento) exige o {_noun(kind)} de quem "
        f"paga: envie só o endereço, ele é enviado ao {name} para emitir a cobrança e não fica "
        "guardado no nosso cadastro. Se preferir não informar, é só me avisar."
    )


def invalid_text(kind: Kind) -> str:
    if kind == "document":
        return (
            "Não consegui validar esse número. Envie o CPF (11 dígitos) ou o CNPJ (14 dígitos), "
            "só os números."
        )
    return (
        "Não consegui validar esse e-mail. Envie o endereço completo, "
        "no formato nome@provedor.com.br."
    )


def optout_text(timing: Timing, kind: Kind) -> str:
    if timing == "after":
        return (
            "Tudo bem. Sua consulta continua agendada, mas o Pix do sinal precisa do "
            f"{_noun(kind)} de quem paga; vou chamar a equipe da clínica para combinar como "
            "seguir."
        )
    # `before`: the fixed "quer que eu chame nosso atendente humano?" card follows (owner,
    # 2026-10-29: the patient chooses), so nothing here promises the team.
    return (
        f"Tudo bem, não vou pedir o {_noun(kind)}. Como o Pix precisa dele, o horário não foi "
        "reservado."
    )


def exhausted_text(timing: Timing) -> str:
    if timing == "after":
        return (
            "Não consegui validar o dado depois de três tentativas. Sua consulta continua "
            "agendada; vou chamar a equipe da clínica para combinar o sinal."
        )
    return (
        "Não consegui validar o dado depois de três tentativas, então o horário não foi "
        "reservado."
    )


def expired_text(timing: Timing) -> str:
    if timing == "after":
        return (
            "Ficamos sem resposta sobre os dados do Pix. Sua consulta continua agendada; vou "
            "chamar a equipe da clínica para combinar o sinal."
        )
    return (
        "Ficamos sem resposta sobre os dados do Pix e o horário não foi reservado. "
        "Quer escolher o horário de novo?"
    )


AFTER_FAILED_TEXT = (
    "Não consegui gerar o Pix agora. Sua consulta continua agendada; vou chamar a equipe da "
    "clínica para combinar o sinal."
)


# ---- one turn of the wait (pure) --------------------------------------------------------------


def _fold(text: str | None) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).strip()


_OPTOUT = re.compile(
    r"^nao$|\bnao\s+(?:quero|vou|posso|desejo|tenho|informo|informar)\b|\bprefiro\s+nao\b"
    r"|\bsem\s+(?:cpf|cnpj|e-?mail)\b|\bdesist\w*|\bcancel\w*|\bdeixa\s+(?:pra|para)\s+la\b"
)
# The values themselves are removed before the opt-out words are looked for: "cancelar@x.com" is an
# e-mail, not a "cancel".
_VALUE_SHAPES = re.compile(r"\d[\d.\-/ ]*\d|[^\s<>()\[\],;:\"']+@[^\s<>()\[\],;:\"']+")


def looks_like_optout(text: str | None) -> bool:
    """"Não quero informar" (the button), "prefiro não", "não tenho", "sem e-mail", "desisti"."""
    return _OPTOUT.search(_fold(_VALUE_SHAPES.sub(" ", text or ""))) is not None


def extract_value(kind: Kind, text: str | None) -> str | None:
    """The value of `kind` written in `text` (document digits or e-mail), or None."""
    return extract_document(text) if kind == "document" else extract_email(text)


@dataclass(frozen=True)
class StepInfo:
    timing: Timing
    kind: Kind
    attempts: int


def step_for(timing: Timing, kind: Kind, attempts: int) -> str:
    return f"{STEP_PREFIX}{timing}:{_TAG[kind]}:{attempts}"


def parse_step(step: str | None) -> StepInfo | None:
    """Read `flow_step` back; None when it is not ours or is malformed."""
    if not step or not step.startswith(STEP_PREFIX):
        return None
    parts = step[len(STEP_PREFIX) :].split(":")
    if len(parts) != 3:
        return None
    timing, tag, tail = parts
    if timing not in ("before", "after") or tag not in _KIND_OF_TAG or not tail.isdigit():
        return None
    return StepInfo(timing, _KIND_OF_TAG[tag], int(tail))  # type: ignore[arg-type]


@dataclass(frozen=True)
class Reading:
    """What one inbound message means to the wait. `value` never appears in a repr."""

    kind: Literal["captured", "invalid", "exhausted", "optout", "expired"]
    value: str | None = field(default=None, repr=False)
    attempts: int = 0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def read_turn(
    wanted: Kind,
    body: str | None,
    *,
    attempts_so_far: int,
    last_activity_at: datetime | None,
    now: datetime | None = None,
) -> Reading:
    """Classify the answer. Order matters: silence past the window first (a late answer belongs
    to a question nobody is asking any more), then the opt-out, then the value, then a miss."""
    now = now or datetime.now(UTC)
    if last_activity_at is not None and now - _aware(last_activity_at) >= timedelta(
        seconds=DATA_TTL_SECONDS
    ):
        return Reading("expired")
    if looks_like_optout(body):
        return Reading("optout")
    value = extract_value(wanted, body)
    if value is not None:
        return Reading("captured", value, attempts_so_far)
    attempts = attempts_so_far + 1
    if attempts >= MAX_INVALID_ATTEMPTS:
        return Reading("exhausted", attempts=attempts)
    return Reading("invalid", attempts=attempts)


# The flow steps (`services/flow_router.py`: STEP_AWAITING_CONFIRMATION / STEP_AWAITING_RETRY) a
# chat sits on right after the value was taken; a test pins these spellings to the router's.
_SHAPE_STEPS = frozenset({"awaiting_confirmation", "awaiting_retry_choice"})


def should_redact_inbound(flow_state: object, flow_step: str | None, body: str | None) -> bool:
    """Whether the worker stores `REDACTED_BODY` instead of this inbound.

    While the question is open EVERY answer is redacted (a typo, "cpf 123", a voice-note
    transcript: none of it may reach the history, and a shape test could miss it). Right after the
    value was taken (a Pix is open, the slot was taken, the confirmation card is back) a message
    that CARRIES a valid document or e-mail, sent a second time, is redacted too.
    """
    state = str(flow_state)
    if state == _AWAITING_STATE:
        return True
    in_the_aftermath = state == _WAITING_FOR_PIX_STATE or (
        state == _CATALOG_STATE and flow_step in _SHAPE_STEPS
    )
    return in_the_aftermath and (
        extract_document(body) is not None or extract_email(body) is not None
    )


def is_redacted_placeholder(body: str | None) -> bool:
    """Whether `body` is the text stored in place of the patient's CPF/e-mail answer.

    The marker exists for the history (the clinic console and the AI see that an answer was given);
    the patient's own transcript must not show it, so every patient-facing reader asks this.
    """
    return body == REDACTED_BODY


# ---- the transient store ----------------------------------------------------------------------


def _key(kind: Kind, tenant_id: UUID, conversation_id: UUID) -> str:
    return f"payer_data:{kind}:{tenant_id}:{conversation_id}"


def _pending_key(tenant_id: UUID, conversation_id: UUID) -> str:
    return f"payer_pending:{tenant_id}:{conversation_id}"


async def remember(
    redis, *, tenant_id: UUID, conversation_id: UUID, kind: Kind, value: str
) -> bool:
    """Keep `value` encrypted for `DATA_TTL_SECONDS`. False when it could not be kept: the caller
    must NOT go on as if it had been."""
    if redis is None or not value:
        return False
    try:
        await redis.set(
            _key(kind, tenant_id, conversation_id), crypto.encrypt(value), ex=DATA_TTL_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - the reason is the type, never the value
        logger.warning("payer_data_store_failed", kind=kind, error_type=type(exc).__name__)
        return False
    return True


async def recall(redis, *, tenant_id: UUID, conversation_id: UUID, kind: Kind) -> str | None:
    """The value remembered for this conversation, or None (never stored/expired/unreadable)."""
    if redis is None or conversation_id is None:
        return None
    try:
        raw = await redis.get(_key(kind, tenant_id, conversation_id))
        if raw is None:
            return None
        token = raw.decode("ascii") if isinstance(raw, bytes) else str(raw)
        return crypto.decrypt(token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("payer_data_recall_failed", kind=kind, error_type=type(exc).__name__)
        return None


async def recall_all(redis, *, tenant_id: UUID, conversation_id: UUID) -> PayerData:
    return PayerData(
        document=await recall(
            redis, tenant_id=tenant_id, conversation_id=conversation_id, kind="document"
        ),
        email=await recall(
            redis, tenant_id=tenant_id, conversation_id=conversation_id, kind="email"
        ),
    )


async def discard(redis, *, tenant_id: UUID, conversation_id: UUID | None) -> None:
    """Forget EVERYTHING kept for this conversation (both fields and the `after` pointer). Never
    raises: the TTL is the backstop for a Redis that is unreachable now."""
    if redis is None or conversation_id is None:
        return
    keys = [_key(kind, tenant_id, conversation_id) for kind in KINDS]
    keys.append(_pending_key(tenant_id, conversation_id))
    try:
        await redis.delete(*keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning("payer_data_discard_failed", error_type=type(exc).__name__)


# ---- the `after` pointer: which consultation is waiting for its Pix -----------------------------


async def remember_pending(
    redis, *, tenant_id: UUID, conversation_id: UUID, appointment_id: UUID
) -> bool:
    """`after` timing: the consultation whose Pix waits for the answer. An id, not personal data."""
    if redis is None:
        return False
    try:
        await redis.set(
            _pending_key(tenant_id, conversation_id), str(appointment_id), ex=DATA_TTL_SECONDS
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("payer_pending_store_failed", error_type=type(exc).__name__)
        return False
    return True


async def recall_pending(redis, *, tenant_id: UUID, conversation_id: UUID) -> UUID | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(_pending_key(tenant_id, conversation_id))
        if raw is None:
            return None
        return UUID(raw.decode("ascii") if isinstance(raw, bytes) else str(raw))
    except Exception as exc:  # noqa: BLE001
        logger.warning("payer_pending_recall_failed", error_type=type(exc).__name__)
        return None
