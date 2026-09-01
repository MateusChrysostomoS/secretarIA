"""WhatsApp Cloud API display limits, and the ONE way we cut text down to them.

WHY THIS MODULE EXISTS
----------------------
Meta caps every interactive element at a different length, and until now those
caps lived in this codebase as bare literals scattered across three layers:
`title[:24]` in the client (services/whatsapp.py), `str(name)[:24]` again at
every render site (services/flow_router.py), and a THIRD `name[:24]` inside the
matcher that has to undo the cut (services/booking_scope.py). Only the button
cap had a name (`MAX_BUTTON_LABEL_CHARS`), and even that one was used solely for
hub-side validation while the send path re-spelled `[:20]` by hand.

That duplication is not cosmetic. For `svc|` list rows the truncated title IS
the lookup key: `svc|` is deliberately NOT in `_PAYLOAD_ROW_PREFIXES`
(schemas/webhook.py), so a tap arrives as the *displayed* title and nothing
else. Render and match therefore have to cut identically, forever. One helper
called from both sides makes that structural instead of a comment.

WHY A MARKED CUT AND NOT A WORD-BOUNDARY CUT
--------------------------------------------
Cutting at the last space before the limit reads better, but it is the wrong
trade for a string that doubles as a key. Clinic catalogues are prefix-heavy:

    "Consulta de rotina adulto"   -> word cut: "Consulta de rotina"
    "Consulta de rotina infantil" -> word cut: "Consulta de rotina"

Two rows would render identically AND `resolve_service_name` would return
whichever came first, silently booking the wrong service. The raw slice never
had that failure mode because it keeps the distinguishing tail. So we keep the
distinguishing tail too, and fix the actual complaint - that the cut was
*invisible* - by marking it with an ellipsis:

    "Consulta de rotina adul…"  /  "Consulta de rotina infa…"

Still distinct, still one-to-one with the full name, and the patient can see
the name was shortened rather than reading a word chopped mid-syllable.

The real fix for ugly truncation is upstream: secretarIA-frontend now caps the
professional-name input at MAX_LIST_ROW_TITLE_CHARS, so names entered from the
hub never reach this code long enough to cut. Everything here is the safety net
for rows created before that cap existed, and for names arriving from anywhere
else (Google Calendar titles, service names, brain-api user names).
"""

from __future__ import annotations

# Reply buttons: at most 3 per message, 20-char titles.
MAX_BUTTON_LABEL_CHARS = 20
MAX_BUTTONS_PER_MESSAGE = 3

# Interactive lists: 10 rows per section; the row title is the short label the
# patient taps, the description the grey line under it. The button that OPENS
# the picker has its own (different) limit.
MAX_LIST_ROWS = 10
MAX_LIST_ROW_TITLE_CHARS = 24
MAX_LIST_ROW_DESCRIPTION_CHARS = 72
MAX_LIST_ROW_ID_CHARS = 200
MAX_LIST_SECTION_TITLE_CHARS = 24
MAX_LIST_OPEN_BUTTON_CHARS = 20

# An interactive message body caps at 1024 chars (a plain text message allows
# 4096), so a greeting that carries buttons must stay within the smaller limit.
MAX_INTERACTIVE_BODY_CHARS = 1024

# Marks a cut so it reads as "shortened", not as a typo. One code unit, so it
# costs exactly one character of the budget.
TRUNCATION_MARK = "…"


def truncate_list_row_title(text: str | None, limit: int = MAX_LIST_ROW_TITLE_CHARS) -> str:
    """Cut `text` to `limit` characters, marking the cut with an ellipsis.

    The single truncation used by BOTH sides of a list row:

      * render - services/flow_router.py builds the row title with it, and
        services/whatsapp.py re-applies it as the last line of defence before
        the payload leaves the process.
      * match - services/booking_scope.py and flow_router's
        `_match_professional` compare against it, so a tap that echoes the
        displayed title still resolves to the full name.

    Anything shorter than or equal to `limit` is returned trimmed but otherwise
    untouched, so the overwhelming majority of names round-trip verbatim.
    Applying it twice is a no-op (`truncate(truncate(x)) == truncate(x)`), which
    is what lets whatsapp.py re-apply it over an already-cut title.

    Never cuts at a word boundary - see this module's docstring for why that
    would make two different services render (and resolve) as the same row.
    """
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    if limit <= 0:
        return ""
    if limit == 1:
        return TRUNCATION_MARK
    # rstrip so a cut landing right after a space does not emit " …".
    return value[: limit - 1].rstrip() + TRUNCATION_MARK


def truncate_button_label(text: str | None, limit: int = MAX_BUTTON_LABEL_CHARS) -> str:
    """Cut a reply-button title to `limit`, UNMARKED - see why below.

    Buttons deliberately do not get the ellipsis that list rows get:

      * The hub already REJECTS a configured label over MAX_BUTTON_LABEL_CHARS
        (schemas/config.py's greeting-button validator), so for clinic-authored
        labels this cut is unreachable - it only ever fires on a default label
        shipped in code, or on an LLM-proposed button.
      * Six matchers compare a tapped label against this exact cut
        (flow_router's `_is_label`, `_matches_yes_no`, the menu router, and
        workers/tasks.py's hand-back). Spending a character on a mark would
        change every one of those keys for no patient-visible gain on a surface
        the hub already guards.

    Same contract as `truncate_list_row_title` otherwise: render and match both
    call it, so the two cannot drift.
    """
    return (text or "")[:limit]


def truncate_plain(text: str | None, limit: int) -> str:
    """Hard cut with no mark, for fields nothing ever matches against.

    Row descriptions, section titles and message bodies are display-only - no
    matcher compares against them - so they keep the cheap slice rather than
    spending a character on the ellipsis.
    """
    return (text or "")[:limit]


# --------------------------------------------------------------------------
# Emoji decoration (FEAT 44)
# --------------------------------------------------------------------------
#
# The conversation's visual language: an affirmative button carries a check, a
# negative one a cross, a service row a hospital, a day/time row a calendar,
# and a "go back one step" row an arrow. PreCheck (the n8n conductor this
# borrows the look from) can hardcode "✅ Sim" in six places because none of
# its labels are DATA - they are all fixed microcopy. Ours are not: a service
# row title is a clinic-authored name that already flirts with the 24-char cap
# this module exists to police, so the prefix has to be budgeted, not pasted.
#
# TWO COSTS, NOT ONE
# ------------------
# Everything here counts in Python code units (`len()` over `str`), the same
# unit MAX_LIST_ROW_TITLE_CHARS is written in. That is NOT one per emoji:
#
#     "🏥 " / "✅ " / "❌ "  -> 2   (one codepoint + the space)
#     "🗓️ " / "⬅️ "         -> 3   (codepoint + U+FE0F variation selector
#                                    + the space)
#
# The variation selector is invisible and free to forget, which is exactly why
# the budget is computed from `len(emoji)` at every call site below rather than
# from a constant someone would have to remember to keep at 2 or 3.
#
# WHY THE MATCHER GETS AN INVERSE
# -------------------------------
# Decorating a label changes what a TAP echoes back ("✅ Sim", not "Sim"), and
# for `svc|` rows the echoed title IS the lookup key (see this module's
# docstring). Two things would break if render were the only side taught about
# the prefix:
#
#   * a patient who TYPES "sim" instead of tapping - the overwhelmingly common
#     case for a yes/no question - would stop matching LABEL_YES;
#   * a tap on a card rendered BEFORE this shipped, still sitting in the
#     patient's thread, would arrive undecorated.
#
# So `strip_decoration` is applied inside each layer's `_norm` (flow_router,
# booking_scope, workers/tasks), which every label comparison already funnels
# through. Decorated and plain forms then normalise to the same key, and the
# comparison keeps working in both directions without a single call site
# learning about emoji.
EMOJI_AFFIRMATIVE = "✅"
EMOJI_NEGATIVE = "❌"
EMOJI_SERVICE = "🏥"
EMOJI_SCHEDULE = "🗓️"
EMOJI_BACK = "⬅️"
EMOJI_DOCTOR = "🥼"

# Every prefix `strip_decoration` knows how to undo. Deliberately OUR six and
# not "any leading emoji": a clinic that named a service "🦷 Limpeza" means the
# tooth as part of the name, and normalising it away would make that row
# unresolvable against its own catalog entry.
DECORATION_EMOJI: tuple[str, ...] = (
    EMOJI_AFFIRMATIVE,
    EMOJI_NEGATIVE,
    EMOJI_SERVICE,
    EMOJI_SCHEDULE,
    EMOJI_BACK,
    EMOJI_DOCTOR,
)


def decorate(emoji: str, text: str | None) -> str:
    """`emoji` + space + `text`, with no budget check.

    For FIXED labels defined in code, where the total is provably under the
    surface's cap once and forever (a test asserts it). Never for a name that
    arrives from a clinic, a calendar or the model - those go through
    `decorate_if_fits` or `decorate_and_truncate`, which know what to spend.
    """
    value = (text or "").strip()
    return f"{emoji} {value}" if value else ""


def decorate_if_fits(emoji: str, text: str | None, limit: int = MAX_LIST_ROW_TITLE_CHARS) -> str:
    """Prefix `text` only when the WHOLE of it still fits inside `limit`.

    The conservative rule for a title that DOUBLES AS A KEY - the `svc|` row.
    A name long enough to need cutting keeps every character of budget it has
    today and goes undecorated:

        "Retorno"                     -> "🏥 Retorno"
        "Consulta de rotina adulto"   -> "Consulta de rotina adul…"
        "Consulta de rotina infantil" -> "Consulta de rotina infa…"

    Spending two characters on the emoji there would shorten the very tail that
    keeps those last two rows one-to-one with their catalog entries - the
    prefix-heavy collision this module's docstring exists to describe, and the
    one that once booked the wrong service. The emoji is a nicety; a service
    resolving to itself is not, so the nicety yields.

    Falls back to `truncate_list_row_title`, so this is a drop-in replacement
    for it at a decorated render site rather than a second cut layered on top.
    """
    value = (text or "").strip()
    if not value:
        return ""
    if len(emoji) + 1 + len(value) <= limit:
        return f"{emoji} {value}"
    return truncate_list_row_title(value, limit)


def decorated_text_budget(emoji: str, limit: int = MAX_LIST_ROW_TITLE_CHARS) -> int:
    """How many characters of TEXT still fit beside `emoji` inside `limit`.

    The one place the "emoji + a space" arithmetic is written down, so the
    render helper below and ai/prompts.py - which has to TELL the model the
    budget it is writing against - can never disagree about whether the arrow
    costs two characters or three.
    """
    return limit - len(emoji) - 1


def decorate_and_truncate(
    emoji: str, text: str | None, limit: int = MAX_LIST_ROW_TITLE_CHARS
) -> str:
    """Always prefix; `text` absorbs the cost by being cut that much harder.

    The opposite trade from `decorate_if_fits`, and correct only where the
    label is NOT a key - the `slot|` rows, whose tap arrives as
    "<label> (<iso datetime>)" with the ISO doing the identifying
    (`_PAYLOAD_ROW_PREFIXES`, schemas/webhook.py). Nothing resolves by the
    visible text, so consistency wins over the tail and every row gets its
    calendar emoji even when the model writes a long one.
    """
    value = (text or "").strip()
    if not value:
        return ""
    return f"{emoji} {truncate_list_row_title(value, decorated_text_budget(emoji, limit))}"


def strip_decoration(text: str | None) -> str:
    """Undo one leading `decorate*` prefix - the matcher's half of the pair.

    Called from every layer's `_norm`, so "✅ Sim", "Sim" and a typed "sim" all
    normalise to the same key and no individual comparison has to know whether
    the label it was built from carries an emoji. See this section's header for
    why both directions have to keep working.

    Strips at most one prefix, and only from the five WE render: a clinic's own
    leading emoji is part of its name and stays.
    """
    value = (text or "").strip()
    for emoji in DECORATION_EMOJI:
        if value.startswith(emoji):
            return value[len(emoji) :].lstrip()
    return value
