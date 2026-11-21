"""A typed "ver mais" at a WhatsApp list = a tap on that list's "Ver mais" row.

TASK-082 F1, CONTRACT.md D10 (owner, 2026-11-13): WhatsApp has no search - only the
lists, and a list past the limit ends with a "Ver mais serviços / profissionais /
convênios / unidades" row (`svcmore|<n>` / `profmore|<n>` / `insmore|<n>` / `unitmore|<n>`).
A patient who TYPES "ver mais" (any case/accents, optionally followed by "serviços",
"profissionais", "convênios" or "unidades") instead of tapping gets the same next page.

Resolved here, in the WhatsApp inbound path, BEFORE routing, so the flow router stays
DB-free: the most recent OUTBOUND interactive card of this patient's conversation
(tenant + patient scoped, the same lookup as the Portal's offered ids,
workers/portal/inbound.py) is read, and when it has such a row the turn is handed to
`_route_inbound_turn` exactly as a real tap of that row (its title + its id). No card,
or a card without that row -> None, and the text routes as typed (`main`'s behaviour).
Only that one row id can come out of here - an id the clinic itself put on screen.
The Portal never calls this: typed text there goes to the model, as on `main` (O4).
"""

from __future__ import annotations

import unicodedata
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Conversation, Message, MessageDirection

_PHRASE = "ver mais"
# The optional word after "ver mais" -> the only row prefix it may pick.
_KIND_PREFIXES: dict[str, tuple[str, ...]] = {
    "servicos": ("svcmore|",),
    "profissionais": ("profmore|",),
    "convenios": ("insmore|",),
    "unidades": ("unitmore|",),
}
_ANY_MORE_PREFIX: tuple[str, ...] = ("svcmore|", "profmore|", "insmore|", "unitmore|")
# Light punctuation a patient may end the two words with ("ver mais.", "ver mais!").
_TRAILING = " .!"


def _fold(text: str) -> str:
    """Casefolded, accents dropped, whitespace collapsed."""
    decomposed = unicodedata.normalize("NFKD", text)
    bare = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join(bare.casefold().split())


def typed_more_kind(text: str | None) -> str | None:
    """The empty string for a bare "ver mais", the list word after it, or None.

    The list word is one of `_KIND_PREFIXES` ("servicos", "profissionais",
    "convenios", "unidades", accents and case ignored). Anything longer or different
    ("ver mais dias", "quero ver mais opções") is not a typed "ver mais".
    """
    if not text:
        return None
    folded = _fold(text).strip(_TRAILING)
    if folded == _PHRASE:
        return ""
    head, _, word = folded.partition(f"{_PHRASE} ")
    if head == "" and word in _KIND_PREFIXES:
        return word
    return None


def more_row(card: object, kind: str) -> tuple[str, str] | None:
    """(row id, row title) of the card's "Ver mais" row `kind` may pick, or None.

    Tolerant of a stored blob that lost its shape: a malformed card offers nothing.
    """
    if not isinstance(card, dict):
        return None
    prefixes = _KIND_PREFIXES.get(kind, _ANY_MORE_PREFIX)
    for option in card.get("options") or []:
        if not isinstance(option, dict):
            continue
        row_id, title = option.get("id"), option.get("title")
        if isinstance(row_id, str) and isinstance(title, str) and row_id.startswith(prefixes):
            return row_id, title
    return None


async def typed_more_tap(
    session: AsyncSession, *, tenant_id: UUID, patient_id: UUID, text: str | None
) -> tuple[str, str] | None:
    """The (row id, row title) a typed "ver mais" stands for in THIS conversation, or None.

    Reads only the most recent outbound interactive card of the (tenant, patient)
    conversation; a plain-text reply sent after it does not hide it, a newer card
    without the row does. Never reads another conversation or another clinic.
    """
    kind = typed_more_kind(text)
    if kind is None:
        return None
    conversation_id = await session.scalar(
        select(Conversation.id).where(
            Conversation.tenant_id == tenant_id,
            Conversation.patient_id == patient_id,
        )
    )
    if conversation_id is None:
        return None
    card = await session.scalar(
        select(Message.interactive)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.OUTBOUND,
            Message.interactive.is_not(None),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )
    return more_row(card, kind)
