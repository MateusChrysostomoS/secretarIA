"""The one truncation used by every WhatsApp list row, on both sides of a tap.

These tests exist to pin the two properties the rest of the flow depends on:

  1. `truncate_list_row_title` is what RENDERS a row title AND what a matcher
     compares a tapped title against. If the two ever diverge, `svc|` rows stop
     resolving (the id does not carry the name — see `_PAYLOAD_ROW_PREFIXES` in
     schemas/webhook.py), so the round-trip is asserted here directly.
  2. Truncation must not collapse two different names onto the same title. A
     word-boundary cut would, on the prefix-heavy catalogues clinics actually
     write, and that would book the wrong service.
"""

from secretaria.core.whatsapp_limits import (
    MAX_BUTTON_LABEL_CHARS,
    MAX_LIST_ROW_TITLE_CHARS,
    TRUNCATION_MARK,
    truncate_button_label,
    truncate_list_row_title,
    truncate_plain,
)

# ---------------------------------------------------------------------------
# truncate_list_row_title
# ---------------------------------------------------------------------------


def test_short_name_is_returned_verbatim():
    # The overwhelmingly common case: nothing is cut, no mark is added.
    assert truncate_list_row_title("Dra. Ana") == "Dra. Ana"


def test_name_exactly_at_the_limit_is_not_truncated():
    name = "Dra. Mariana Albuquerque"
    assert len(name) == MAX_LIST_ROW_TITLE_CHARS
    assert truncate_list_row_title(name) == name
    assert TRUNCATION_MARK not in truncate_list_row_title(name)


def test_one_char_over_the_limit_is_marked_and_still_fits():
    name = "A" * (MAX_LIST_ROW_TITLE_CHARS + 1)
    result = truncate_list_row_title(name)
    assert len(result) == MAX_LIST_ROW_TITLE_CHARS
    assert result.endswith(TRUNCATION_MARK)


def test_long_name_keeps_the_maximum_number_of_real_characters():
    # 22 real characters + the mark: the cut spends exactly one character
    # telling the patient it happened, and not a character more.
    name = "Dr. Mateus Chrysostomo Neto"
    result = truncate_list_row_title(name)
    assert result == "Dr. Mateus Chrysostomo" + TRUNCATION_MARK
    assert len(result) <= MAX_LIST_ROW_TITLE_CHARS


def test_a_cut_landing_on_a_space_does_not_leave_a_dangling_gap():
    # Without the rstrip this would render as "... de …" — the mark must sit
    # flush against the last real word.
    name = "Consulta Geral de Rotina Completa"
    result = truncate_list_row_title(name)
    assert " " + TRUNCATION_MARK not in result


def test_surrounding_whitespace_is_trimmed():
    assert truncate_list_row_title("  Dra. Ana  ") == "Dra. Ana"


def test_none_and_blank_become_empty():
    assert truncate_list_row_title(None) == ""
    assert truncate_list_row_title("   ") == ""


def test_truncation_is_idempotent():
    # whatsapp.py re-applies the cut over a title flow_router already cut.
    # Applying it twice must not eat another character or double the mark.
    name = "Consulta de Acompanhamento Prolongada"
    once = truncate_list_row_title(name)
    assert truncate_list_row_title(once) == once


def test_prefix_heavy_service_names_stay_distinguishable():
    # THE regression this cut exists to avoid. A word-boundary truncation would
    # render both of these as "Consulta de rotina", and `resolve_service_name`
    # would answer a tap on either with whichever came first in the catalogue —
    # i.e. book the wrong service, silently.
    adulto = truncate_list_row_title("Consulta de rotina adulto")
    infantil = truncate_list_row_title("Consulta de rotina infantil")
    assert adulto != infantil
    assert len(adulto) <= MAX_LIST_ROW_TITLE_CHARS
    assert len(infantil) <= MAX_LIST_ROW_TITLE_CHARS


def test_accented_characters_count_as_one_character_each():
    # pt-BR names are full of accents; the budget is characters, not bytes.
    name = "Dr. José Antônio Gonçalves"
    result = truncate_list_row_title(name)
    assert len(result) == MAX_LIST_ROW_TITLE_CHARS


def test_degenerate_limits_do_not_crash():
    assert truncate_list_row_title("Qualquer coisa", limit=0) == ""
    assert truncate_list_row_title("Qualquer coisa", limit=1) == TRUNCATION_MARK


# ---------------------------------------------------------------------------
# truncate_button_label — deliberately unmarked, see the module docstring
# ---------------------------------------------------------------------------


def test_button_label_cut_is_unmarked_and_matches_the_historical_slice():
    # Six matchers key off this exact string; adding a mark would rewrite all
    # of them. Pinned so a future "make it consistent" refactor has to argue.
    label = "Agendar minha consulta"
    assert truncate_button_label(label) == label[:MAX_BUTTON_LABEL_CHARS]
    assert TRUNCATION_MARK not in truncate_button_label(label)


def test_button_label_within_the_limit_is_untouched():
    assert truncate_button_label("Agendar") == "Agendar"


def test_button_label_handles_none():
    assert truncate_button_label(None) == ""


# ---------------------------------------------------------------------------
# truncate_plain — display-only fields
# ---------------------------------------------------------------------------


def test_plain_truncation_has_no_mark_and_no_trim():
    # Descriptions/bodies are never compared against, so they keep the cheap
    # slice — including whatever whitespace the caller passed in.
    assert truncate_plain(" abcdef ", 4) == " abc"
    assert truncate_plain(None, 10) == ""
