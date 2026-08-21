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
