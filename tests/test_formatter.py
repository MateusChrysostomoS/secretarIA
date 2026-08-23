"""Tests for the agent-output formatter (bubble splitting + markup parsing)."""

import os

# Match the determinism guarantees in conftest before any secretaria import.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from secretaria.ai.formatter import (  # noqa: E402
    MAX_LIST_ROWS,
    ButtonBubble,
    SlotsBubble,
    TextBubble,
    parse,
)
from secretaria.core.whatsapp_limits import (  # noqa: E402
    MAX_LIST_ROW_TITLE_CHARS,
    TRUNCATION_MARK,
    truncate_list_row_title,
)


def test_empty_reply_yields_no_bubbles() -> None:
    assert parse("") == []
    assert parse("   \n  ") == []


def test_plain_text_yields_single_bubble() -> None:
    bubbles = parse("Ola! Como posso ajudar?")
    assert len(bubbles) == 1
    assert isinstance(bubbles[0], TextBubble)
    assert bubbles[0].body == "Ola! Como posso ajudar?"


def test_separator_splits_into_multiple_bubbles() -> None:
    reply = "Primeira frase.\n\n---\n\nSegunda frase.\n\n---\n\nTerceira."
    bubbles = parse(reply)
    assert [b.kind for b in bubbles] == ["text", "text", "text"]
    assert [b.body for b in bubbles] == ["Primeira frase.", "Segunda frase.", "Terceira."]


def test_confirm_block_renders_button_bubble() -> None:
    reply = (
        "O 29/05 às 15:00 está livre.\n\n"
        "[CONFIRM]\n"
        "Luiz Picolli\n"
        "29/05/2026 às 15:00\n"
        "Motivo: revisão pós-operatória\n"
        "[/CONFIRM]"
    )
    bubbles = parse(reply)
    assert len(bubbles) == 2
    assert isinstance(bubbles[0], TextBubble)
    assert "29/05" in bubbles[0].body
    assert isinstance(bubbles[1], ButtonBubble)
    assert "Luiz Picolli" in bubbles[1].body
    assert "revisão pós-operatória" in bubbles[1].body
    assert bubbles[1].confirm_label == "Confirmar"
    assert bubbles[1].cancel_label == "Cancelar"


def test_slots_block_consumes_preceding_text_as_header() -> None:
    reply = (
        "Estes são os horários livres em 29/05:\n\n"
        "[SLOTS]\n"
        "2026-05-29T14:00:00|14:00\n"
        "2026-05-29T15:00:00|15:00\n"
        "2026-05-29T16:30:00|16:30\n"
        "[/SLOTS]"
    )
    bubbles = parse(reply)
    assert len(bubbles) == 1
    slot = bubbles[0]
    assert isinstance(slot, SlotsBubble)
    assert "horários livres" in slot.body
    assert slot.rows == [
        ("slot|2026-05-29T14:00:00", "14:00"),
        ("slot|2026-05-29T15:00:00", "15:00"),
        ("slot|2026-05-29T16:30:00", "16:30"),
    ]


def test_slots_block_falls_back_to_default_header_when_no_preceding_text() -> None:
    reply = "[SLOTS]\n2026-05-29T14:00:00|14:00\n[/SLOTS]"
    bubbles = parse(reply)
    assert len(bubbles) == 1
    assert isinstance(bubbles[0], SlotsBubble)
    assert bubbles[0].body == "Escolha um horário:"


def test_malformed_slot_lines_are_dropped() -> None:
    reply = (
        "[SLOTS]\n"
        "2026-05-29T14:00:00|14:00\n"
        "linha invalida sem pipe\n"
        "|sem_id\n"
        "2026-05-29T15:00:00|\n"
        "2026-05-29T16:30:00|16:30\n"
        "[/SLOTS]"
    )
    bubbles = parse(reply)
    assert len(bubbles) == 1
    slot = bubbles[0]
    assert isinstance(slot, SlotsBubble)
    assert [label for _, label in slot.rows] == ["14:00", "16:30"]


def test_slots_block_caps_at_10_rows() -> None:
    rows = "\n".join(
        f"2026-05-29T{h:02d}:00:00|{h:02d}:00" for h in range(8, 22)
    )
    reply = f"[SLOTS]\n{rows}\n[/SLOTS]"
    bubbles = parse(reply)
    assert isinstance(bubbles[0], SlotsBubble)
    assert len(bubbles[0].rows) == 10


def test_bubble_cap_merges_extra_text_tail() -> None:
    reply = "\n\n---\n\n".join(f"Bolha {i}" for i in range(1, 7))
    bubbles = parse(reply)
    assert len(bubbles) == 4
    assert all(isinstance(b, TextBubble) for b in bubbles)
    assert "Bolha 4" in bubbles[3].body
    assert "Bolha 5" in bubbles[3].body
    assert "Bolha 6" in bubbles[3].body


def test_empty_blocks_do_not_emit_bubbles() -> None:
    assert parse("[CONFIRM]   [/CONFIRM]") == []
    assert parse("[SLOTS]\n  \n[/SLOTS]") == []


def test_emoji_only_text_bubble_is_dropped() -> None:
    reply = "Ok 👁️\n\n---\n\n👍"
    bubbles = parse(reply)
    assert len(bubbles) == 1
    assert isinstance(bubbles[0], TextBubble)
    assert bubbles[0].body == "Ok 👁️"


def test_excess_triple_newlines_are_normalised() -> None:
    bubble = parse("Primeira linha.\n\n\n\nSegunda linha.")
    assert len(bubble) == 1
    assert "\n\n\n" not in bubble[0].body


# ---------------------------------------------------------------------------
# Row-title limit - the LLM writes these labels itself, so the parse is the
# first place we can hold it to the WhatsApp cap. See ai/prompts.py block B,
# which now states the number instead of only asking for a "rotulo curto".
# ---------------------------------------------------------------------------


def test_slot_label_over_the_limit_is_truncated_at_parse() -> None:
    # A model that ignores the instruction must not put an over-long title on
    # the wire; send_list would cut it anyway, but by then it has travelled the
    # whole pipeline and been logged at full length.
    long_label = "Quinta-feira as 14:00 com a Dra. Mariana"
    assert len(long_label) > MAX_LIST_ROW_TITLE_CHARS
    bubbles = parse(f"[SLOTS]\n2026-05-29T14:00:00|{long_label}\n[/SLOTS]")
    assert len(bubbles) == 1
    bubble = bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    (_, title) = bubble.rows[0]
    assert len(title) <= MAX_LIST_ROW_TITLE_CHARS
    assert title.endswith(TRUNCATION_MARK)
    assert title == truncate_list_row_title(long_label)


def test_slot_label_within_the_limit_is_untouched() -> None:
    # The normal case by far: "14:00" must not grow a mark or lose a character.
    bubbles = parse("[SLOTS]\n2026-05-29T14:00:00|14:00\n[/SLOTS]")
    bubble = bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    assert bubble.rows[0] == ("slot|2026-05-29T14:00:00", "14:00")


def test_slot_label_exactly_at_the_limit_keeps_every_character() -> None:
    label = "a" * MAX_LIST_ROW_TITLE_CHARS
    bubbles = parse(f"[SLOTS]\n2026-05-29T14:00:00|{label}\n[/SLOTS]")
    bubble = bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    assert bubble.rows[0][1] == label


def test_whitespace_only_label_is_still_dropped_after_truncation() -> None:
    # truncate_list_row_title strips, so it replaces the old `label.strip()`.
    # A line whose label is only spaces must stay malformed, not become "".
    assert parse("[SLOTS]\n2026-05-29T14:00:00|   \n[/SLOTS]") == []


def test_row_cap_is_the_shared_constant_not_a_local_copy() -> None:
    # formatter.py used to declare its own `MAX_LIST_ROWS = 10` beside the one
    # in core/whatsapp_limits.py. Re-exported now, so this asserts they are the
    # same value rather than two numbers that happen to agree today.
    from secretaria.core import whatsapp_limits

    assert MAX_LIST_ROWS is whatsapp_limits.MAX_LIST_ROWS
