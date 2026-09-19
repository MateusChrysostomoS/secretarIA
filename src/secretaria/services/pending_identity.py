"""The two inline identity steps a Brain-Message visitor goes through, and their wire leg.

WhatsApp does not appear anywhere in this module, and that is the point: on
WhatsApp the phone number IS the identity, delivered by Meta and already proven
before the first message reaches us. A Brain-Message visitor arrives with
nothing — brain-api opened a "pending visit" for their browser and handed
secretarIA a `patient_ref` that names a row with no address on it
(`brain-api/docs/CHECKPOINT_portal_sessao_pendente.md` §3). So the address has
to be collected IN the conversation, and proven in the conversation, and this
module is the only place that knows it.

## The order the owner fixed (2026-09-16), and why it is not negotiable here

    link -> chat -> greeting (no LGPD) -> e-mail -> LGPD -> booking -> code

The e-mail is asked BEFORE the LGPD notice and the code is asked AFTER the
appointment is already committed. Both positions are product decisions, not
implementation ones: the appointment must not depend on a code
(`PLANO_LOGIN_SEM_GATE_PACIENTE_NOVO.md`, "Decisões de produto fechadas"), and
the greeting must not arrive welded to a consent wall.

## Claimed vs. proven

`claim_email` writes the address onto the VISIT, never onto an identity and
never onto an account. It is the patient's word for it. `verify_code` is what
turns that word into an account, and until it happens the address is invisible
to every query that means "this person's clinics" — brain-api enforces that,
not us (CHECKPOINT §3).

That split is also why `request_code` takes NO address. A caller that could
name the inbox to mail would be a caller that could aim a code at any inbox;
brain-api reads the one the visit already claimed (CHECKPOINT §6.1). The same
reasoning applies one hop earlier, here: `claim_email` is reachable only from
the conversation that typed the address, keyed on the `external_id` that
conversation already owns.

## Fail-closed, and what "closed" means for each call

Every function returns an outcome enum and NEVER raises — same contract as
`services/precheck.py`, whose shape this module deliberately mirrors. What
differs is what the caller does with a failure, and the three are not alike:

  * `claim_email` UNAVAILABLE -> the conversation stays before LGPD and asks
    again later. The owner's order is strict: the notice only follows a claim
    brain-api actually acknowledged.
  * `request_code` UNAVAILABLE -> no code notice is sent at all. The
    appointment is already committed; a promise of a code that never arrives is
    worse than silence (same reasoning as `plugins/precheck_handoff.py`).
  * `verify_code` UNAVAILABLE -> the patient is told the account could not be
    activated right now AND that the appointment stands, because they are
    staring at a prompt they just answered.

## The masked address, and why it is checked on the way IN

`request_code` came back with a bare status until TASK-003 §3 added
`email_masked` to it. That value is the only part of this whole module that
reaches the patient's transcript verbatim, so it is validated here
(`masked_email_or_none`) before anything is allowed to render it: it must
LOOK masked. brain-api is the one that masks, and this is not distrust of
it so much as a refusal to let one bug over there become a permanent row in
`messages` over here. An absent or malformed mask silently falls back to
wording that names no inbox at all — never to a guess.

## What is never logged

No address, ever — not even hashed, which is the one difference from
`services/precheck.py::_phone_hash`. A hashed phone number is useful there
because the same number recurs across handoffs and a hash lets two log lines be
tied together without storing the number. Nothing here needs that tie: one
address belongs to one visit, and the visit already has an id worth logging.
No code either, in any state — a six-digit secret with a ten-minute life is
exactly the thing a log aggregator should never see. The MASK is not logged
either: it is a fragment of the same address, and a log line only ever
records whether one arrived (`has_email_mask`), never its characters.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Copy (pure)
# ---------------------------------------------------------------------------
# Message 2 of a Brain-Message first contact: it takes the slot the LGPD notice
# holds on WhatsApp, and the LGPD notice moves one message later. Button-free
# on purpose, exactly like the greeting that precedes it — there is nothing to
# tap, the answer is typed.
EMAIL_REQUEST_MESSAGE = (
    "📧 Antes de continuarmos, qual é o seu *e-mail*?\n\n"
    "Uso ele para guardar esta conversa e te enviar as confirmações do seu "
    "agendamento.\n\n"
    "✍️ É só digitar aqui."
)

# The re-ask. Says what was wrong (the SHAPE, not the address — we never echo
# what they typed back at them, because a mistyped address is often a real
# address belonging to someone else) and shows the shape expected.
EMAIL_INVALID_MESSAGE = (
    "Hmm, não consegui ler esse e-mail. 😕\n\n"
    "✍️ Pode digitar de novo? Assim, por exemplo: nome@email.com"
)

# The address was syntactically valid, but brain-api did not acknowledge the
# claim. Do not echo it (a typo can be somebody else's real address) and do not
# advance to LGPD: the product order is claim -> consent, not best effort.
EMAIL_CLAIM_RETRY_MESSAGE = (
    "Não consegui salvar seu e-mail agora. 😕\n\n"
    "✍️ Pode tentar digitá-lo novamente em alguns instantes?"
)

EMAIL_PAUSED_MESSAGE = (
    "Tudo bem, seu atendimento ficou pausado.\n\n"
    "Quando quiser continuar, é só mandar uma nova mensagem por aqui."
)

# Sent by `plugins/pending_identity.py` right after an appointment is committed
# for a visitor who has not proven their address yet.
#
# It opens by confirming the appointment on purpose. This message arrives
# unrequested, immediately after a booking, and asks for a secret — the exact
# shape of a scam. Leading with the thing the patient just did, and saying the
# appointment is already done, is what makes the ask legible.
#
# This is the FALLBACK wording: the one used when brain-api did not tell us
# which inbox the code went to. It names no address at all rather than
# inventing one — "o e-mail que você me passou" is true whatever was claimed.
CODE_NOTICE_MESSAGE = (
    "🔐 Sua consulta já está confirmada! ✅\n\n"
    "Para você conseguir voltar a esta conversa depois, de qualquer aparelho, "
    "enviei um código de *6 dígitos* para o e-mail que você me passou.\n\n"
    "✍️ Digite o código aqui para ativar sua conta."
)

# The same notice when brain-api DID hand back a masked address. Saying which
# inbox to look in is the whole reason the mask exists: the patient typed the
# address minutes ago and a code arriving at an inbox they cannot name is the
# single most common reason the step is abandoned.
_CODE_NOTICE_MASKED_MESSAGE = (
    "🔐 Sua consulta já está confirmada! ✅\n\n"
    "Para você conseguir voltar a esta conversa depois, de qualquer aparelho, "
    "enviei um código de *6 dígitos* para {email_masked}.\n\n"
    "✍️ Digite o código aqui para ativar sua conta."
)

# The three buttons the notice carries, in the order the owner listed them.
# Ids are stable strings, NOT positions: they are what comes back from the
# patient's tap (already revalidated against the cards this conversation
# offered, `workers/tasks.py::_validated_brain_message_reply_id`), and the
# router branches on the id alone — never on the label, which is display text
# that a copy change may move at any time.
IDENTITY_BACK_ACTION = "identity_back"
IDENTITY_RESEND_ACTION = "identity_resend"
IDENTITY_CHANGE_EMAIL_ACTION = "identity_change_email"

# Titles fit WhatsApp's 20-code-unit reply-button cap (which
# `interactive_buttons_record` enforces for this channel too, so the portal's
# card and a WhatsApp card cannot drift): "⬅️ Voltar" 9, "↩️ Reenviar código"
# 18, "📩 Mudar e-mail" 15 — the variation selector in ⬅️/↩️ and the astral
# 📩 each cost an extra unit. A test pins this.
CODE_NOTICE_BUTTONS: tuple[tuple[str, str], ...] = (
    (IDENTITY_BACK_ACTION, "⬅️ Voltar"),
    (IDENTITY_RESEND_ACTION, "↩️ Reenviar código"),
    (IDENTITY_CHANGE_EMAIL_ACTION, "📩 Mudar e-mail"),
)

_IDENTITY_ACTIONS = frozenset(action for action, _ in CODE_NOTICE_BUTTONS)


def identity_action_or_none(reply_id: str | None) -> str | None:
    """The code-notice action `reply_id` names, or None for anything else.

    Pure. Keeps the id vocabulary in the module that owns the card, so a
    fourth button is added in one place rather than in the router's `if`
    ladder as well.
    """
    return reply_id if reply_id in _IDENTITY_ACTIONS else None


# What a masked address looks like, per the brain-api contract (§3 of
# TASK-003): first character + `***` + last character of the local part, `@`,
# then the whole domain. A one-character local part becomes `a***@dominio`.
#
# This is checked HERE, on the way in, and that check is a safety property and
# not tidiness: `email_masked` is the only field of this contract whose value
# ends up verbatim in the patient's transcript, and the transcript is exactly
# where a raw address must never appear. A brain-api that one day sends the
# full address — a bug, a rollback, a mis-merge — must not be able to write it
# into `messages` through us. Anything that is not visibly masked is dropped
# and the fallback wording is used instead.
_MASKED_EMAIL_RE = re.compile(r"^[^@\s]*\*\*\*[^@\s]*@[^@\s]+$")


def masked_email_or_none(value: str | None) -> str | None:
    """`value` if it is a MASKED address we may show, else None.

    Pure apart from one log line, which records only that a value was refused
    — never the value, which is precisely the thing we are refusing to let
    out.
    """
    candidate = (value or "").strip()
    if not candidate:
        return None
    if len(candidate) > 254 or not _MASKED_EMAIL_RE.match(candidate):
        logger.warning("pending_code_email_mask_rejected", length=len(candidate))
        return None
    return candidate


def code_notice_body(email_masked: str | None) -> str:
    """The code notice, naming the masked inbox when we have one.

    Pure. Degrades to `CODE_NOTICE_MESSAGE` for an absent OR unacceptable
    mask, so a brain-api that predates the field — and one that sends a
    malformed value — both produce today's message rather than an empty slot
    or a leak.
    """
    masked = masked_email_or_none(email_masked)
    if masked is None:
        return CODE_NOTICE_MESSAGE
    return _CODE_NOTICE_MASKED_MESSAGE.format(email_masked=masked)


CODE_ACCEPTED_MESSAGE = "✅ Tudo certo, sua conta está ativa! Esta conversa fica salva para você."

# Wrong or expired code. Does NOT say how many attempts are left: the budget is
# brain-api's (5 per challenge) and mirroring a number we do not own here would
# be a second source of truth that drifts on its first change.
CODE_INVALID_MESSAGE = (
    "❌ Esse código não confere, ou já expirou.\n\n"
    "✍️ Confira o e-mail e digite os 6 dígitos de novo."
)

# Every dead end that is not "wrong code": brain-api unreachable, the attempt
# budget spent, the visit gone. One message for all of them, and it says the
# appointment stands — that is the fact the patient actually needs, and the
# reason for the dead end is not something they can act on.
CODE_GIVE_UP_MESSAGE = (
    "Não consegui ativar sua conta agora. 😕\n\n"
    "Mas fique tranquilo(a): *sua consulta continua marcada*. A gente resolve "
    "isso da próxima vez que você voltar por aqui."
)


# Deliberately permissive, and deliberately NOT RFC 5322. The address is about
# to be handed to brain-api, which mails a code to it — so the only question
# this regex has to answer is "is it worth spending a send on?", and the real
# verdict is whether a code arrives. A stricter pattern here can only produce
# one outcome the loose one cannot: rejecting an address that would have
# worked, in a chat, with no way for the patient to argue.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

# Brain-Message bodies arrive with whatever the keyboard added. Autocorrect
# capitalises the first letter of a line, and folding is safe for the domain
# and safe in practice for the local part everywhere that matters, so it saves
# a class of "it says my e-mail is wrong" reports. `mailto:` shows up when a
# patient copies from a contact card.
_EMAIL_PREFIXES = ("mailto:", "e-mail:", "email:")


def parse_email(body: str | None) -> str | None:
    """The address in `body`, normalized — or None if it does not look like one.

    Pure. Accepts the message as a WHOLE: a patient answering "qual é o seu
    e-mail?" types the address and nothing else far more often than not, and
    fishing an address out of a sentence would also fish one out of
    "meu e-mail antigo era x@y.com, não use esse".
    """
    text = (body or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    for prefix in _EMAIL_PREFIXES:
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :].strip()
    # Trailing punctuation a sentence-ending keyboard adds on its own.
    lowered = lowered.rstrip(".,;:!?")
    if not _EMAIL_RE.match(lowered):
        return None
    # Upper bound so a pathological body cannot be stored or forwarded. 254 is
    # the RFC 5321 limit on a forward path, which is the bound the mail leg
    # will actually enforce.
    if len(lowered) > 254:
        return None
    return lowered


# Six digits, and the message may carry nothing else. Spaces and dashes are
# stripped first because a patient copying "123 456" out of an e-mail is the
# common case, not an attack.
_CODE_RE = re.compile(r"^\d{6}$")


def parse_code(body: str | None) -> str | None:
    """The 6-digit code in `body`, or None if the message is not one.

    Pure. A None here is what tells the caller "this turn is not an answer to
    the code prompt" — which is the difference between re-prompting and letting
    the patient say something else entirely.
    """
    text = (body or "").strip()
    if not text:
        return None
    compact = re.sub(r"[\s\-.]", "", text)
    return compact if _CODE_RE.match(compact) else None


# ---------------------------------------------------------------------------
# The wire leg
# ---------------------------------------------------------------------------


class ClaimOutcome(StrEnum):
    """What `claim_email` achieved."""

    CLAIMED = "claimed"
    # No live pending visit for this (tenant, external_id). Per CHECKPOINT §5.3
    # brain-api answers 404 identically for an unknown handle, the wrong clinic,
    # an expired visit and one already turned into an account — so this value
    # means "there is nothing pending to write on", never "something is wrong".
    NOT_PENDING = "not_pending"
    UNAVAILABLE = "unavailable"


class RequestCodeOutcome(StrEnum):
    """What `request_code` achieved."""

    SENT = "sent"
    # The visit never captured an address (409 pending_email_missing).
    NO_EMAIL = "no_email"
    NOT_PENDING = "not_pending"
    UNAVAILABLE = "unavailable"


class VerifyOutcome(StrEnum):
    """What `verify_code` achieved."""

    VERIFIED = "verified"
    # Wrong code, expired code, code already spent, attempt budget exhausted,
    # clinic closed mid-flow: brain-api answers 400 for all of them on purpose
    # (CHECKPOINT §5.5), and so do we. The patient may try again.
    INVALID = "invalid"
    NO_EMAIL = "no_email"
    NOT_PENDING = "not_pending"
    UNAVAILABLE = "unavailable"


class IdentityState(StrEnum):
    """Where this `external_id` stands with brain-api, as of one probe."""

    # A live visit with no address claimed yet — the ONLY state that earns the
    # e-mail question.
    PENDING_UNCLAIMED = "pending_unclaimed"
    # A live visit that already has an address, not yet proven.
    PENDING_CLAIMED = "pending_claimed"
    # Already an account. Asking for an address again would be asking a patient
    # to re-prove something they proved — possibly at another clinic, which is
    # the case this probe exists for (a verified patient opening a NEW clinic's
    # link is a first contact HERE and would otherwise be asked again).
    VERIFIED = "verified"
    # No pending visit and no account brain-api will talk about.
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of one `verify_code` call."""

    outcome: VerifyOutcome


@dataclass(frozen=True)
class RequestCodeResult:
    """The outcome of one `request_code` call, plus where the code went.

    `email_masked` is brain-api's own masking of the address the visit
    claimed (`a***a@gmail.com`) — the ONLY form of it this service is ever
    handed, and the only form allowed into the transcript. It is None
    whenever brain-api did not send the field (a deploy that predates it) or
    sent something that is not visibly masked (`masked_email_or_none`), and
    every caller must cope with that rather than substituting a guess.
    """

    outcome: RequestCodeOutcome
    email_masked: str | None = None


async def _post(path: str, body: dict, *, tenant_id: UUID, event: str) -> httpx.Response | None:
    """One internal POST to brain-api, or None if it could not be made.

    Shares the settings gate, the header and the timeout with
    `services/precheck.py`; kept as a local helper rather than imported from
    there because that module's function is about a handoff and folding a
    second caller into it would make its logging lie.
    """
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        logger.warning(f"{event}_unconfigured", tenant_id=str(tenant_id))
        return None
    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            return await client.post(path, json=body)
    except httpx.HTTPError as exc:
        logger.warning(
            f"{event}_failed",
            reason="network_error",
            tenant_id=str(tenant_id),
            error=str(exc),
        )
        return None


def _status_field(response: httpx.Response, *, tenant_id: UUID, event: str) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        logger.warning(f"{event}_failed", reason="invalid_json", tenant_id=str(tenant_id))
        return None
    value = payload.get("status") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


async def probe_identity(tenant_id: UUID, external_id: str) -> IdentityState:
    """Where this visitor stands, so the conversation knows whether to ask.

    One call, made once per Brain-Message first contact. UNAVAILABLE is its own
    value rather than collapsing into UNKNOWN: the caller treats them
    differently on purpose (see `workers/tasks.py`'s pending-identity gate) —
    UNKNOWN means "brain-api says there is nothing pending here", which is a
    real answer, and UNAVAILABLE means we did not get one.
    """
    response = await _post(
        "/internal/brain-message/pending-identity",
        {"tenant_id": str(tenant_id), "external_id": external_id},
        tenant_id=tenant_id,
        event="pending_identity_probe",
    )
    if response is None:
        return IdentityState.UNAVAILABLE
    if response.status_code == 404:
        return IdentityState.UNKNOWN
    if response.status_code != 200:
        logger.warning(
            "pending_identity_probe_failed",
            reason="unexpected_status",
            status_code=response.status_code,
            tenant_id=str(tenant_id),
        )
        return IdentityState.UNAVAILABLE
    raw = _status_field(response, tenant_id=tenant_id, event="pending_identity_probe")
    try:
        state = IdentityState(raw or "")
    except ValueError:
        logger.warning(
            "pending_identity_probe_failed",
            reason="unexpected_status_body",
            tenant_id=str(tenant_id),
        )
        return IdentityState.UNAVAILABLE
    if state is IdentityState.UNAVAILABLE:
        # Not a value brain-api is allowed to send; treating it as one would let
        # the far side fabricate our own failure mode.
        return IdentityState.UNKNOWN
    logger.info("pending_identity_probed", state=state.value, tenant_id=str(tenant_id))
    return state


async def claim_email(tenant_id: UUID, external_id: str, email: str) -> ClaimOutcome:
    """Write the address the patient typed onto their pending visit.

    Never logs the address (module docstring). Re-claiming overwrites, which is
    what a chat needs: the patient corrected a typo.
    """
    response = await _post(
        "/internal/brain-message/pending-email",
        {"tenant_id": str(tenant_id), "external_id": external_id, "email": email},
        tenant_id=tenant_id,
        event="pending_email_claim",
    )
    if response is None:
        return ClaimOutcome.UNAVAILABLE
    if response.status_code == 404:
        logger.info("pending_email_claim_not_pending", tenant_id=str(tenant_id))
        return ClaimOutcome.NOT_PENDING
    if response.status_code != 200:
        logger.warning(
            "pending_email_claim_failed",
            reason="unexpected_status",
            status_code=response.status_code,
            tenant_id=str(tenant_id),
        )
        return ClaimOutcome.UNAVAILABLE
    logger.info("pending_email_claimed", tenant_id=str(tenant_id))
    return ClaimOutcome.CLAIMED


async def request_code(tenant_id: UUID, external_id: str) -> RequestCodeResult:
    """Ask brain-api to mail the code. Takes no address — see the docstring.

    Reads the OPTIONAL `email_masked` off a 200 (TASK-003 §3). Optional in
    both directions: a brain-api that does not send it yet still produces
    SENT, and a mask that does not look like one is dropped without
    downgrading the outcome — the code WAS mailed either way, and refusing to
    say so because the cosmetic half of the answer was malformed would turn a
    copy problem into a silence.
    """
    response = await _post(
        "/internal/brain-message/pending-otp/request",
        {"tenant_id": str(tenant_id), "external_id": external_id},
        tenant_id=tenant_id,
        event="pending_code_request",
    )
    if response is None:
        return RequestCodeResult(RequestCodeOutcome.UNAVAILABLE)
    if response.status_code == 404:
        return RequestCodeResult(RequestCodeOutcome.NOT_PENDING)
    if response.status_code == 409:
        return RequestCodeResult(RequestCodeOutcome.NO_EMAIL)
    if response.status_code != 200:
        logger.warning(
            "pending_code_request_failed",
            reason="unexpected_status",
            status_code=response.status_code,
            tenant_id=str(tenant_id),
        )
        return RequestCodeResult(RequestCodeOutcome.UNAVAILABLE)
    masked = masked_email_or_none(_masked_email_field(response, tenant_id=tenant_id))
    logger.info(
        "pending_code_requested",
        tenant_id=str(tenant_id),
        # Whether we can name the inbox, never which one it is.
        has_email_mask=masked is not None,
    )
    return RequestCodeResult(RequestCodeOutcome.SENT, email_masked=masked)


def _masked_email_field(response: httpx.Response, *, tenant_id: UUID) -> str | None:
    """The raw `email_masked` value off a 200, or None if there is not one.

    A body that is not JSON, not an object, or carries the key as something
    other than a string is simply a body without the field — the code was
    still mailed, so this never turns into a failure.
    """
    try:
        payload = response.json()
    except ValueError:
        logger.warning(
            "pending_code_request_body_unreadable",
            reason="invalid_json",
            tenant_id=str(tenant_id),
        )
        return None
    value = payload.get("email_masked") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


async def verify_code(tenant_id: UUID, external_id: str, code: str) -> VerifyResult:
    """Spend the code. The rate limit and the attempt budget are brain-api's.

    A 429 maps to INVALID rather than UNAVAILABLE deliberately: from the
    patient's seat "you are trying too fast" and "that code is wrong" both mean
    "this attempt did not work, the next one might", and the distinction is one
    we would only be able to explain by describing a limiter we do not own.
    """
    response = await _post(
        "/internal/brain-message/pending-otp/verify",
        {"tenant_id": str(tenant_id), "external_id": external_id, "code": code},
        tenant_id=tenant_id,
        event="pending_code_verify",
    )
    if response is None:
        return VerifyResult(VerifyOutcome.UNAVAILABLE)
    if response.status_code in (400, 429):
        logger.info(
            "pending_code_rejected",
            status_code=response.status_code,
            tenant_id=str(tenant_id),
        )
        return VerifyResult(VerifyOutcome.INVALID)
    if response.status_code == 404:
        return VerifyResult(VerifyOutcome.NOT_PENDING)
    if response.status_code == 409:
        return VerifyResult(VerifyOutcome.NO_EMAIL)
    if response.status_code != 200:
        logger.warning(
            "pending_code_verify_failed",
            reason="unexpected_status",
            status_code=response.status_code,
            tenant_id=str(tenant_id),
        )
        return VerifyResult(VerifyOutcome.UNAVAILABLE)
    logger.info("pending_code_verified", tenant_id=str(tenant_id))
    return VerifyResult(VerifyOutcome.VERIFIED)
