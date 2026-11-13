"""Typed partial search over a WhatsApp catalogue list (TASK-082 S4).

A patient who TYPES at a service/doctor/convênio list used to need the exact name
(`booking_scope.canonical_service_name`, `insurance_catalog.match_plan`); anything
else went to the LLM. The router now tries the exact match first, then this:
accent-, case-, emoji- and punctuation-insensitive "contains" over the names.

The key also travels in a "Ver mais" row id of a filtered list (`svcmore|<page>|<key>`,
services/flow_router.py), so it is reduced to letters, digits and single spaces - no
"|" or ")" can ever break the row payload. Search text is never logged.

Kept apart from `list_options` (the Portal's options route) on purpose: that module
imports `flow_router`, and the Portal's own search contract is frozen (CONTRACT.md §c4).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable

# A safety net, same number as the Portal's options route (list_options.MAX_QUERY_CHARS).
MAX_QUERY_CHARS = 60
# One letter matches half a catalogue; it is not a search.
MIN_QUERY_CHARS = 2


def search_key(text: str | None) -> str:
    """`text` without accents, case, emoji/symbols or punctuation; spaces collapsed."""
    kept: list[str] = []
    for ch in unicodedata.normalize("NFKD", text or ""):
        category = unicodedata.category(ch)
        if category in ("Mn", "Me", "Cf") or category[0] == "S":
            continue  # accents split off by NFKD, emoji/symbols, zero-width joiners
        kept.append(ch if ch.isalnum() else " ")
    return " ".join("".join(kept).casefold().split())


def normalize_query(text: str | None) -> str:
    """The comparable form of what the patient typed, clamped to MAX_QUERY_CHARS."""
    return search_key((text or "").strip()[:MAX_QUERY_CHARS])[:MAX_QUERY_CHARS].strip()


def partial_matches[T](entries: Iterable[T], query: str, name_of: Callable[[T], str]) -> list[T]:
    """The entries whose name contains `query` (already `normalize_query`d), in order.

    Empty for a query shorter than MIN_QUERY_CHARS.
    """
    if len(query) < MIN_QUERY_CHARS:
        return []
    return [entry for entry in entries if query in search_key(name_of(entry))]
