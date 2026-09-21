"""The explicit "qual é o seu nome?" step, on BOTH channels.

Until this module existed no channel ever ASKED for the patient's name. It was
only ever picked up on the side: the WhatsApp `contact.profile.name` Meta sends
with every inbound (`workers/tasks.py::_persist_inbound_message`), and the
opportunistic `extract_patient_name` regex that fires on "meu nome é ..." /
"me chamo ...". The owner decided (2026-09-20) that the profile name is not
trustworthy — it is whatever the person typed into their WhatsApp settings,
often a nickname, an emoji or a business name — and that the product asks.

## Where the question sits

    WhatsApp:  greeting -> NAME -> LGPD                  (always on a first contact)
    Portal:    greeting -> e-mail -> NAME -> LGPD        (only when the e-mail is new)

On WhatsApp the phone number is already the identity, so there is no e-mail
step at all and the name question takes the slot right after the greeting. On
the Portal the e-mail stays first because it is the e-mail that tells a new
visitor from a known one: an address that already belongs to an account skips
this step entirely and goes straight to the 6-digit code
(`services/pending_identity.py::existing_account_code_body`).

## Why the answer is written to `Patient.name` and nowhere else

That single fact is what keeps the name away from the LLM.
`services/pii_pseudonymization.py::load_pseudonymizer` registers `Patient.name`
as the `PACIENTE` identifier on every turn, and `ai/graph.py::run_agent` scrubs
the WHOLE history with it before OpenAI sees anything — including this step's
inbound answer, which is an ordinary `messages` row. A second column, a flow
field or a copy of the name inside another message would be a second path the
pseudonymizer does not know about. `pseudonymize-core` matches the identifier
case- and accent-insensitively, which is why normalizing the answer here
(Title-Case, trimmed punctuation) still masks the raw text the patient typed.

The one thing the pseudonymizer cannot mask is a PART of the name: the
identifier is registered whole (`add_identifier`, deliberately not
`add_person_name` — see `docs/CHECKPOINT_pseudonimizacao.md` §4), so a bot
message that echoed only "Maria" out of "Maria Silva" would reach the model
unmasked. None of the copy below echoes the name back, and it must stay that
way.
"""

import re

# ---------------------------------------------------------------------------
# Copy (pure)
# ---------------------------------------------------------------------------
# WhatsApp: message 2 of a first contact, right after the greeting frame and in
# the slot the LGPD notice used to take (the notice moves one message later).
# Button-free like the greeting before it: the answer is typed. "Prazer!" is
# the owner's opener, verbatim.
NAME_REQUEST_MESSAGE = (
    "Prazer! 😊 Qual é o seu *nome*?\n\n"
    "Assim eu sei como te chamar durante o atendimento.\n\n"
    "✍️ É só digitar aqui."
)

# Portal: the same question, one message later — it answers the e-mail the
# visitor just typed. Nothing else on this channel acknowledges the address
# (the claim is silent), so the first line does, without echoing it back: a
# mistyped address is often somebody else's real one.
NAME_REQUEST_AFTER_EMAIL_MESSAGE = "✅ E-mail anotado!\n\n" + NAME_REQUEST_MESSAGE

# The answer did not look like a name. Said once: a second unreadable answer
# moves on to the LGPD notice without a name (`workers/tasks.py`, the
# AWAITING_NAME gate) rather than holding the patient hostage to a field the
# product can live without. Shows the expected shape instead of repeating what
# they typed.
NAME_INVALID_MESSAGE = (
    "Hmm, não consegui entender o seu nome. 😕\n\n"
    "✍️ Pode digitar só o seu nome? Assim, por exemplo: Maria Silva"
)

# The patient answered "Não" to the "quer continuar?" that the silence floor
# offers for an abandoned name question. Same words as the e-mail step's pause,
# deliberately: both are the same place in the opening, before the LGPD notice.
NAME_PAUSED_MESSAGE = (
    "Tudo bem, seu atendimento ficou pausado.\n\n"
    "Quando quiser continuar, é só mandar uma nova mensagem por aqui."
)

# What the LLM reads in place of any answer given to the name question
# (`is_name_question`, applied by `ai/graph.py::_load_history`). The answer
# that PARSED is already masked by the pseudonymizer; the ones that did not —
# "Ana, tudo bem?" before a re-ask, "Maria 😊" twice — hold a name nobody
# registered. The staff console keeps the real text; only the model's copy of
# the history is redacted, and the model never needs it: the name it may use
# arrives as the PACIENTE token.
NAME_ANSWER_LLM_PLACEHOLDER = "[resposta à pergunta do nome — omitida]"

# The opening of the "quer continuar?" offer an expired name wait gets. Shared
# with the e-mail wait on purpose (both sit before consent); an answer to it
# may also be a name, so it is redacted too.
PRE_CONSENT_PAUSE_PREFIX = "Seu atendimento ficou pausado antes da etapa de privacidade."


# `Conversation.flow_step` value that marks "the re-ask already went out".
# `flow_step` is the booking flow's working memory and is always empty at
# this point of the opening (no flow has started before consent), so it can
# hold this one marker without competing with anything; every exit from
# AWAITING_NAME clears it.
NAME_REASKED_STEP = "name_reasked"


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------

# A direct answer to "qual é o seu nome?" is usually just the name, but people
# also answer in a sentence. Only a LEADING introduction is stripped; anything
# else in the sentence makes it "not a name", which re-asks.
_INTRO_RE = re.compile(
    r"^(?:(?:oi|olá|ola|opa)[\s,!.]+)?"
    r"(?:(?:o\s+)?meu\s+nome\s+(?:é|e|eh)|me\s+chamo|pode\s+me\s+chamar\s+de|"
    r"sou\s+(?:o|a)|sou)\s+",
    re.IGNORECASE,
)

# Answers that are words but not names. Checked against EVERY token, not just
# the first: "Maria obrigada" is not a name either, and a partial match is how
# "Bom Dia" would end up greeting the patient as Mr. Dia forever.
_NOT_A_NAME = frozenset(
    {
        "oi",
        "oie",
        "oii",
        "olá",
        "ola",
        "opa",
        "bom",
        "boa",
        "dia",
        "tarde",
        "noite",
        "sim",
        "não",
        "nao",
        "ok",
        "okay",
        "obrigado",
        "obrigada",
        "tudo",
        "bem",
        "quero",
        "queria",
        "gostaria",
        "agendar",
        "marcar",
        "consulta",
        "menu",
        "ajuda",
        "prefiro",
        "dizer",
        "nome",
        "meu",
        "seu",
        "qual",
        "voltar",
        "cancelar",
        "remarcar",
        "concordo",
        "hello",
        "hi",
        # Pronouns and the verbs people answer a question WITH. A reply like
        # "Vocês atendem Unimed" or "Pode ser amanhã" is a question or an
        # answer to something else, and storing it would overwrite the Meta
        # profile name with it on WhatsApp — and make the phrase, not the
        # person, the only PACIENTE identifier.
        "eu",
        "você",
        "voce",
        "vocês",
        "voces",
        "vc",
        "ele",
        "ela",
        "isso",
        "isto",
        "aqui",
        "tenho",
        "tem",
        "temos",
        "pode",
        "posso",
        "podem",
        "ser",
        "é",
        "eh",
        "sou",
        "está",
        "esta",
        "estou",
        "atende",
        "atendem",
        "preciso",
        "quanto",
        "custa",
        "valor",
        "horário",
        "horario",
        "amanhã",
        "amanha",
        "hoje",
        "dúvida",
        "duvida",
        "uma",
        "um",
        "quem",
        "onde",
        "como",
        "quando",
        "porque",
        "por",
        "para",
        "pra",
        "com",
        "sem",
    }
)

# Name particles stay lowercase inside a name ("Maria da Silva"), but a name
# never STARTS with one.
_PARTICLES = frozenset({"da", "de", "do", "das", "dos", "e", "di", "du", "del", "van", "von"})

_MAX_WORDS = 6
# Without an introduction ("me chamo ...") a bare answer with more than this
# many words that are not particles is far more often a sentence than a name.
_MAX_BARE_WORDS = 4
_MIN_CHARS = 2
_MAX_CHARS = 80


def _title(word: str) -> str:
    """Title-case one word, keeping hyphenated and apostrophe'd parts apart."""
    word = "-".join(part[:1].upper() + part[1:].lower() for part in word.split("-"))
    return "'".join(part[:1].upper() + part[1:] for part in word.split("'"))


def parse_patient_name(body: str | None) -> str | None:
    """The name in a direct answer to "qual é o seu nome?", normalized — or None.

    Pure. The rule, chosen for a DIRECT answer rather than for prose:

      * trim, collapse whitespace, strip one leading introduction
        ("meu nome é", "me chamo", "sou a", ...) and edge punctuation;
      * every word must be letters only (hyphen and apostrophe allowed inside
        a word): digits, emoji, e-mail addresses and "/menu" are refused;
      * no word may be a greeting / command / filler word (`_NOT_A_NAME`);
      * at most 6 words, 2 to 80 characters in total, and the first word is
        not a particle;
      * Title-Case, particles in lowercase ("maria DA silva" -> "Maria da Silva").

    Also refused: any "?" or "," (a question or a list), a first word of one
    letter, pronouns / answering verbs anywhere, and more than 4 non-particle
    words when there is no introduction phrase. Wrongly refusing costs one
    re-ask; wrongly accepting overwrites a real name with a sentence.

    It reuses the SHAPE of `workers/tasks.py::extract_patient_name` (split,
    letters-only tokens, a length window) without its regex, because here the
    patient is answering a question: the whole message is the candidate. The
    cap is higher (6 words, not 3) for the same reason — a full name typed on
    request is often "Maria Clara da Silva Santos".
    """
    text = re.sub(r"\s+", " ", (body or "").strip())
    if not text or len(text) > 120:
        return None
    # A question or a list is never a name ("sou a Maria, e você?").
    if "?" in text or "," in text:
        return None
    stripped = _INTRO_RE.sub("", text, count=1)
    introduced = stripped != text
    text = stripped.strip(" .!;:")
    if not text:
        return None
    words: list[str] = []
    for raw in text.split(" "):
        token = raw.strip('.,!?;:()"')
        if not token:
            continue
        letters = token.replace("-", "").replace("'", "")
        if not letters or not letters.isalpha():
            return None
        if token.casefold() in _NOT_A_NAME:
            return None
        words.append(token)
    if not words or len(words) > _MAX_WORDS:
        return None
    significant = [w for w in words if w.casefold() not in _PARTICLES]
    if not introduced and len(significant) > _MAX_BARE_WORDS:
        return None
    if words[0].casefold() in _PARTICLES or len(words[0]) < 2:
        return None
    rendered = [
        word.casefold() if index and word.casefold() in _PARTICLES else _title(word)
        for index, word in enumerate(words)
    ]
    name = " ".join(rendered)
    if not _MIN_CHARS <= len(name) <= _MAX_CHARS:
        return None
    return name


def is_name_question(bot_body: str | None) -> bool:
    """Whether an outbound row asked the patient for their name.

    Pure. `ai/graph.py::_load_history` redacts the patient row that follows
    one of these (NAME_ANSWER_LLM_PLACEHOLDER). Matched by prefix because a
    Brain-Message card is stored flattened with its options appended.
    """
    body = bot_body or ""
    return body.startswith(
        (
            NAME_REQUEST_MESSAGE,
            NAME_REQUEST_AFTER_EMAIL_MESSAGE,
            NAME_INVALID_MESSAGE,
            PRE_CONSENT_PAUSE_PREFIX,
        )
    )
