"""text - split out of workers/tasks.py (TASK-023)."""

import re
from datetime import UTC, datetime

from secretaria.core.whatsapp_limits import (
    strip_decoration,
)
from secretaria.services.greeting_template import (
    CONSENT_BUTTON_LABEL,
)

# Patient-facing fallbacks for non-conversational outcomes. Hardcoded for the
# MVP; candidates for per-tenant configuration later.
SERVICE_UNAVAILABLE_MESSAGE = (
    "Nosso sistema de agendamento está em configuração. Em breve estará disponível. 🙏"
)

CALENDAR_UNAVAILABLE_MESSAGE = (
    "Estou com uma dificuldade técnica para acessar a agenda agora. "
    "Nossa equipe foi avisada e entrará em contato em breve. 🙏"
)

# Sent when a voice note can't be turned into a usable transcript (rejected
# media, or a low-confidence/empty STT result) - see transcribe_audio_message.
AUDIO_UNINTELLIGIBLE_MESSAGE = "Não consegui entender o áudio, pode repetir?"

# Context-aware opening. Appended by _adapt_greeting_to_state to the tenant's
# verbatim greeting; RETURNING_NO_APPOINTMENT / NEW states keep the greeting
# untouched (the returning_greeting_message already covers that tone).
# HAS_UPCOMING(_SOON) (PROMPT 2 final copy): the nearest appointment's detail
# (when/doctor/service/price/description/pre-consult orientations), a brief
# line per OTHER future appointment, and a closing action hint - see
# _adapt_greeting_has_upcoming / _compose_upcoming_greeting_body below.
GREETING_REQUIREMENTS_HEADER = "Orientações de pré-consulta:"

GREETING_BRIEF_HEADER = "Suas próximas consultas:"

GREETING_ACTION_HINT = (
    "Se quiser, use os botões abaixo para remarcar ou cancelar — ou toque em "
    '"Outro" para qualquer outra coisa.'
)

# WhatsApp's interactive body caps at 1024 chars (schemas.config.
# MAX_GREETING_WITH_BUTTONS_CHARS); this leaves margin below that once the
# tenant's own greeting is prepended to the blocks composed here.
GREETING_DETAIL_MAX_CHARS = 1000

# "The first few" lines/bullets kept when trimming for size - see
# _compose_upcoming_greeting_body.
GREETING_BRIEF_KEEP = 3

GREETING_REQUIREMENTS_KEEP = 3

# Neutral on purpose: a past appointment inside the window may still sit in
# SCHEDULED/CONFIRMED (nobody marked the outcome), so this must NOT assert
# the consult happened. Still MVP copy - PROMPT 2 only finalized HAS_UPCOMING.
JUST_HAD_CONSULT_NEUTRAL_LINE = "Vi que você teve uma consulta recentemente, posso ajudar em algo?"

# Presupposes attendance — used ONLY when the doctor explicitly set ATTENDED.
JUST_HAD_CONSULT_ATTENDED_LINE = "Como foi sua consulta? Posso ajudar em algo?"

# Slash commands the patient can type to go back to the main menu. Matched
# case-insensitively against the trimmed message body.
#
# These are NON-DESTRUCTIVE (PROMPT_FIX_18). They used to route to a "dev
# reset" that deleted the patient row, their conversation, every message and
# their appointments — off a word a patient types by accident. That handler
# still exists, but only behind `REMOVE_CONTEXT_COMMAND` below; `/menu` and
# friends now do exactly what `ai/tools.py::show_main_menu` does: reset the
# transient flow fields and re-render the menu, touching nothing else.
# The bare (slash-less) variants were added with the greeting frame, and are
# not cosmetic: the frame tells every patient "Errou? Digite *voltar* a
# qualquer momento para recomeçar", and before this the ONLY menu commands
# were slash-prefixed — something no patient types. The promise would have
# been dead copy. Matching stays whole-body and case-insensitive (see
# `is_menu_command`), so "voltar" mid-booking means the main menu, while
# "quero voltar na segunda" is untouched and still routes normally.
_MENU_COMMANDS = frozenset(
    {
        "/menu",
        "/reset",
        "/recomecar",
        "/recomeçar",
        "/inicio",
        "/início",
        "menu",
        "voltar",
        "recomecar",
        "recomeçar",
        "inicio",
        "início",
    }
)

def is_menu_command(body: str | None) -> bool:
    """True when the patient typed a `/menu`-style (non-destructive) command."""
    if not body:
        return False
    return body.strip().lower() in _MENU_COMMANDS

def _is_consent_acceptance(body: str | None) -> bool:
    """True for a tap on (or a typed match of) the LGPD "Concordo" button.

    Compared through `strip_decoration` so the rendered "✅ Concordo", a bare
    "Concordo" and a typed "concordo" all match — the same normalisation every
    other decorated label goes through, so the emoji stays a render concern.
    """
    if not body:
        return False
    return strip_decoration(body).casefold() == strip_decoration(CONSENT_BUTTON_LABEL).casefold()

# The DESTRUCTIVE reset. Deliberately long, literal and self-describing: it is
# the one command nobody types by accident, so reaching it is unambiguously a
# deliberate operator gesture.
REMOVE_CONTEXT_COMMAND = "/dangerously-remove-context"

def is_remove_context_command(body: str | None) -> bool:
    """True ONLY for the exact `/dangerously-remove-context` string.

    EXACT match on purpose (PROMPT_FIX_18): no aliases, no case folding, no
    whitespace trimming, no prefix matching. The literal string IS the safety
    mechanism — accepting variations is what made `/menu` dangerous in the
    first place, and would hand the accident right back.
    """
    return body == REMOVE_CONTEXT_COMMAND

# Strong, low-false-positive openers a patient uses to state their name. We do
# NOT try to infer a name from arbitrary capitalised words - only from these
# explicit self-introductions.
_NAME_PATTERNS = (
    re.compile(r"\bmeu\s+nome\s+(?:é|eh|e)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bme\s+chamo\s+(.+)", re.IGNORECASE),
    re.compile(r"\bpode\s+me\s+chamar\s+de\s+(.+)", re.IGNORECASE),
)

# Words that terminate a captured name (connectors / fillers that follow it).
_NAME_STOPWORDS = frozenset(
    {
        "e",
        "de",
        "da",
        "do",
        "das",
        "dos",
        "que",
        "mas",
        "então",
        "entao",
        "por",
        "favor",
        "pra",
        "para",
        "com",
        "sou",
        "tudo",
        "bem",
    }
)

def extract_patient_name(body: str | None) -> str | None:
    """Best-effort patient name from an explicit self-introduction.

    Returns a Title-Cased name (max 3 words) or None. Conservative by design:
    only fires on "meu nome é ...", "me chamo ...", "pode me chamar de ...".
    """
    if not body:
        return None
    for pattern in _NAME_PATTERNS:
        match = pattern.search(body)
        if not match:
            continue
        words: list[str] = []
        for raw in re.split(r"\s+", match.group(1).strip()):
            token = raw.strip(".,!?;:()").strip()
            if not token or not token.replace("-", "").isalpha():
                break
            if token.lower() in _NAME_STOPWORDS:
                break
            words.append(token)
            if len(words) == 3:
                break
        if words:
            name = " ".join(w.capitalize() for w in words)
            if 2 <= len(name) <= 80:
                return name
    return None

def _render_greeting_template(template: str, name: str | None) -> str:
    """Substitute the `{{name}}` placeholder and tidy the spacing.

    When the name is unknown the placeholder collapses to nothing and stray
    spaces before punctuation / doubled spaces are cleaned up so the greeting
    still reads naturally (e.g. "Olá {{name}}!" -> "Olá!").
    """
    rendered = template.replace("{{name}}", (name or "").strip())
    rendered = re.sub(r"\s+([,.!?;:])", r"\1", rendered)
    rendered = re.sub(r"[ \t]{2,}", " ", rendered)
    return rendered.strip()

def _as_utc(dt: datetime) -> datetime:
    """Treat a naive timestamp (e.g. from SQLite) as UTC; pass tz-aware through."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
