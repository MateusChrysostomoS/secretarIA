"""Parse the agent's text output into a sequence of WhatsApp message bubbles.

The LLM emits a simple markup that the worker turns into one or more sends:

    Plain text bubble 1.

    ---

    Plain text bubble 2.

    [CONFIRM]
    Body of the confirm card.
    [/CONFIRM]

    [SLOTS]
    2026-05-29T14:00|14:00
    2026-05-29T15:00|15:00
    [/SLOTS]

Each bubble becomes one outbound WhatsApp message: plain text, an interactive
reply-button card (Confirmar / Cancelar) or an interactive list of selectable
slot rows. Malformed markup falls back to a plain-text bubble so a bad LLM
turn still reaches the patient.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from secretaria.core.whatsapp_limits import MAX_LIST_ROWS, truncate_list_row_title

# Product caps on how much WE choose to send per turn. These are ours, not
# Meta's, which is why they live here: a text message may carry 4096 characters
# but a wall of text is a bad answer, so a bubble stops at 1024.
MAX_BUBBLES_PER_TURN = 4
MAX_TEXT_BUBBLE_CHARS = 1024
MAX_BUTTON_BODY_CHARS = 1024
MAX_LIST_BODY_CHARS = 1024

# `MAX_LIST_ROWS` is NOT one of ours - it is Meta's hard cap, and it used to be
# re-declared here as a second `= 10` beside the copy in core/whatsapp_limits.py
# that flow_router and the WhatsApp client already read. Re-exported instead of
# redefined so `from secretaria.ai.formatter import MAX_LIST_ROWS` keeps working
# (tests/test_flow_day_picker.py) while there is only one place to change it.

# Deterministic IDs the webhook parser maps back to plain text.
BUTTON_ID_CONFIRM = "confirm_yes"
BUTTON_ID_CANCEL = "confirm_no"
SLOT_ID_PREFIX = "slot|"

# Block markup recognised in the LLM output.
_CONFIRM_RE = re.compile(r"\[CONFIRM\](.*?)\[/CONFIRM\]", re.DOTALL | re.IGNORECASE)
_SLOTS_RE = re.compile(r"\[SLOTS\](.*?)\[/SLOTS\]", re.DOTALL | re.IGNORECASE)
_BUBBLE_SEPARATOR_RE = re.compile(r"^\s*---+\s*$", re.MULTILINE)


@dataclass
class TextBubble:
    kind: Literal["text"] = "text"
    body: str = ""


@dataclass
class ButtonBubble:
    body: str
    kind: Literal["buttons"] = "buttons"
    confirm_label: str = "Confirmar"
    cancel_label: str = "Cancelar"


@dataclass
class SlotsBubble:
    body: str
    # Each row is (id, title) or (id, title, description) — the optional third
    # element is the WhatsApp list-row subtitle (send_list caps it at 72 chars).
    # 2-tuples remain the common case; senders must treat the description as
    # absent when the tuple has only two elements.
    rows: list[tuple] = field(default_factory=list)
    kind: Literal["slots"] = "slots"
    button_label: str = "Ver horários"
    section_title: str = "Horários livres"


Bubble = TextBubble | ButtonBubble | SlotsBubble


def parse(reply: str) -> list[Bubble]:
    """Turn an LLM reply string into a list of `Bubble`s.

    Order of operations:
      1. Pull out every `[SLOTS]` and `[CONFIRM]` block in source order.
      2. The remaining text is split into text bubbles on `---` separators
         and on the gaps where the blocks used to be.
      3. Empty bubbles, sole-emoji bubbles and bubbles past the per-turn cap
         are dropped or merged so we never spam the patient.
    """
    reply = (reply or "").strip()
    if not reply:
        return []

    bubbles: list[Bubble] = []
    cursor = 0
    # Each span is (start, end, bubble_or_None). A None bubble means the
    # span was a markup block but its body was empty/malformed — we still
    # consume the source range so the raw tags never leak into a text bubble.
    spans: list[tuple[int, int, Bubble | None]] = []

    for match in _CONFIRM_RE.finditer(reply):
        body = _clean(match.group(1))
        bubble: Bubble | None = (
            ButtonBubble(body=body[:MAX_BUTTON_BODY_CHARS]) if body else None
        )
        spans.append((match.start(), match.end(), bubble))

    for match in _SLOTS_RE.finditer(reply):
        rows = _parse_slot_rows(match.group(1))
        bubble = SlotsBubble(body="", rows=rows) if rows else None
        spans.append((match.start(), match.end(), bubble))

    spans.sort(key=lambda s: s[0])

    for start, end, bubble in spans:
        for text in _split_text(reply[cursor:start]):
            bubbles.append(TextBubble(body=text))
        if bubble is not None:
            if isinstance(bubble, SlotsBubble):
                body = _pop_preceding_text_for(bubbles)
                bubble.body = body or "Escolha um horário:"
            bubbles.append(bubble)
        cursor = end

    for text in _split_text(reply[cursor:]):
        bubbles.append(TextBubble(body=text))

    return _finalise(bubbles)


def _split_text(chunk: str) -> list[str]:
    """Split a free-text chunk into one or more text bubbles on `---`."""
    chunk = chunk.strip()
    if not chunk:
        return []
    parts = _BUBBLE_SEPARATOR_RE.split(chunk)
    out: list[str] = []
    for part in parts:
        cleaned = _clean(part)
        if cleaned:
            out.append(cleaned[:MAX_TEXT_BUBBLE_CHARS])
    return out


def _clean(text: str) -> str:
    """Trim trailing blank lines and normalise WhatsApp-unfriendly spacing.

    WhatsApp shows leading/trailing whitespace literally and collapses 3+
    consecutive newlines into one big gap, so we normalise here.
    """
    text = text.strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _parse_slot_rows(block: str) -> list[tuple[str, str]]:
    """Each non-empty line is `<id>|<label>`; malformed lines are skipped.

    The label is cut to MAX_LIST_ROW_TITLE_CHARS here, at the parse, rather than
    left for `WhatsAppClient.send_list` to cut at the very end. The system
    prompt now states the limit outright (ai/prompts.py, block B), so this is
    the backstop for a model that ignores it - and cutting on the way in means
    an over-long label never travels the rest of the pipeline, never reaches a
    log line, and never lands in a bubble some other caller renders.

    Safe to truncate because the label is NOT the key: `slot|` is in
    `_PAYLOAD_ROW_PREFIXES` (schemas/webhook.py), so a tap arrives as
    "<label> (<iso datetime>)" and the ISO is what identifies the slot.
    """
    rows: list[tuple[str, str]] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "|" not in line:
            continue
        slot_id, _, label = line.partition("|")
        slot_id = slot_id.strip()
        label = truncate_list_row_title(label)
        if not slot_id or not label:
            continue
        rows.append((f"{SLOT_ID_PREFIX}{slot_id}", label))
        if len(rows) == MAX_LIST_ROWS:
            break
    return rows


def _pop_preceding_text_for(bubbles: list[Bubble]) -> str:
    """Promote the last short text bubble into the slot card's header.

    A list message already has its own body line, so the bubble that
    introduced it ("estes são os horários livres em 29/05:") reads better
    as the list's header than as a separate WhatsApp message.
    """
    if not bubbles:
        return ""
    last = bubbles[-1]
    if isinstance(last, TextBubble) and len(last.body) <= MAX_LIST_BODY_CHARS:
        bubbles.pop()
        return last.body
    return ""


_EMOJI_ONLY_RE = re.compile(
    r"^[\s☀-➿\U0001F300-\U0001FAFF‍️]+$"
)


def _finalise(bubbles: list[Bubble]) -> list[Bubble]:
    """Drop empty/emoji-only bubbles and merge tails beyond the cap."""
    filtered: list[Bubble] = []
    for bubble in bubbles:
        if isinstance(bubble, TextBubble):
            if not bubble.body or _EMOJI_ONLY_RE.match(bubble.body):
                continue
        filtered.append(bubble)

    if len(filtered) <= MAX_BUBBLES_PER_TURN:
        return filtered

    head = filtered[: MAX_BUBBLES_PER_TURN - 1]
    tail = filtered[MAX_BUBBLES_PER_TURN - 1 :]
    merged_text_parts: list[str] = []
    interactive_tail: list[Bubble] = []
    for bubble in tail:
        if isinstance(bubble, TextBubble):
            merged_text_parts.append(bubble.body)
        else:
            interactive_tail.append(bubble)
    if merged_text_parts:
        head.append(TextBubble(body=" ".join(merged_text_parts)[:MAX_TEXT_BUBBLE_CHARS]))
    head.extend(interactive_tail[:1])
    return head[:MAX_BUBBLES_PER_TURN]
