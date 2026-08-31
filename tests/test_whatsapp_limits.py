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
    DECORATION_EMOJI,
    EMOJI_AFFIRMATIVE,
    EMOJI_BACK,
    EMOJI_NEGATIVE,
    EMOJI_SCHEDULE,
    EMOJI_SERVICE,
    MAX_BUTTON_LABEL_CHARS,
    MAX_LIST_ROW_TITLE_CHARS,
    TRUNCATION_MARK,
    decorate,
    decorate_and_truncate,
    decorate_if_fits,
    decorated_text_budget,
    strip_decoration,
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


# ---------------------------------------------------------------------------
# Emoji decoration (FEAT 44) — the budget, and the inverse the matcher needs
# ---------------------------------------------------------------------------


def test_the_two_emoji_costs_are_not_the_same():
    # The whole reason the budget is computed instead of assumed. The calendar
    # and arrow carry an invisible U+FE0F variation selector, so they cost one
    # MORE than the hospital/check/cross. A hand-written "minus 2" would put
    # "⬅️ …" one character over the row cap - and the send path would then cut
    # a fixed label the matcher compares against in full.
    assert decorated_text_budget(EMOJI_SERVICE) == MAX_LIST_ROW_TITLE_CHARS - 2
    assert decorated_text_budget(EMOJI_AFFIRMATIVE) == MAX_LIST_ROW_TITLE_CHARS - 2
    assert decorated_text_budget(EMOJI_NEGATIVE) == MAX_LIST_ROW_TITLE_CHARS - 2
    assert decorated_text_budget(EMOJI_SCHEDULE) == MAX_LIST_ROW_TITLE_CHARS - 3
    assert decorated_text_budget(EMOJI_BACK) == MAX_LIST_ROW_TITLE_CHARS - 3


def test_decorate_if_fits_decorates_a_name_that_fits():
    assert decorate_if_fits(EMOJI_SERVICE, "Retorno") == "🏥 Retorno"


def test_decorate_if_fits_lands_exactly_on_the_cap_at_the_budget():
    name = "a" * decorated_text_budget(EMOJI_SERVICE)
    result = decorate_if_fits(EMOJI_SERVICE, name)
    assert result == f"{EMOJI_SERVICE} {name}"
    assert len(result) == MAX_LIST_ROW_TITLE_CHARS


def test_decorate_if_fits_yields_the_emoji_one_character_past_the_budget():
    # The conservative rule: the emoji is a nicety, the name's distinguishing
    # tail is not. One character too long and the row goes bare rather than
    # spending two more of the name on decoration.
    name = "a" * (decorated_text_budget(EMOJI_SERVICE) + 1)
    result = decorate_if_fits(EMOJI_SERVICE, name)
    assert result == name
    assert EMOJI_SERVICE not in result


def test_decorate_if_fits_never_makes_truncation_worse_than_it_already_was():
    # Acceptance criterion: no name may end up MORE prone to a truncation
    # collision than it was before the emoji existed. Undecorated names must
    # come back byte-identical to what the old render produced.
    for name in (
        "Consulta de rotina adulto",
        "Consulta de rotina infantil",
        "Dr. Mateus Chrysostomo Neto",
        "a" * 60,
    ):
        assert decorate_if_fits(EMOJI_SERVICE, name) == truncate_list_row_title(name)


def test_prefix_heavy_names_keep_their_full_tail_through_decoration():
    # The collision test above, re-run with the emoji in play. Both of these
    # are past the budget, so both stay bare and keep every character of tail
    # the cut left them - the decoration cannot shorten a name it declines.
    adulto = decorate_if_fits(EMOJI_SERVICE, "Consulta de rotina adulto")
    infantil = decorate_if_fits(EMOJI_SERVICE, "Consulta de rotina infantil")
    assert adulto != infantil
    assert adulto == "Consulta de rotina adul" + TRUNCATION_MARK
    assert infantil == "Consulta de rotina infa" + TRUNCATION_MARK


def test_two_short_names_that_differ_only_late_survive_decoration():
    # The decorated case of the same property: when both names DO fit, the
    # prefix is added to both and cannot merge them.
    a = decorate_if_fits(EMOJI_SERVICE, "Retorno adulto")
    b = decorate_if_fits(EMOJI_SERVICE, "Retorno infantil")
    assert a != b
    assert a.startswith(EMOJI_SERVICE) and b.startswith(EMOJI_SERVICE)
    assert max(len(a), len(b)) <= MAX_LIST_ROW_TITLE_CHARS


def test_decorate_and_truncate_always_decorates_and_never_overflows():
    # The opposite trade, for labels that are NOT keys (slot rows). The emoji
    # always appears; the text pays for it.
    long_label = "Quinta-feira as 14:00 com a Dra. Mariana"
    result = decorate_and_truncate(EMOJI_SCHEDULE, long_label)
    assert result.startswith(EMOJI_SCHEDULE)
    assert result.endswith(TRUNCATION_MARK)
    assert len(result) <= MAX_LIST_ROW_TITLE_CHARS


def test_strip_decoration_is_the_inverse_of_decorate():
    for emoji in DECORATION_EMOJI:
        assert strip_decoration(decorate(emoji, "Consulta")) == "Consulta"


def test_strip_decoration_leaves_an_undecorated_string_alone():
    # The forms that keep arriving forever: a typed answer, and a tap on a card
    # rendered before FEAT 44 shipped.
    assert strip_decoration("Sim") == "Sim"
    assert strip_decoration("  cancelar  ") == "cancelar"
    assert strip_decoration(None) == ""


def test_strip_decoration_keeps_an_emoji_a_clinic_chose_itself():
    # Only OUR five prefixes are undone. A clinic that named a service
    # "🦷 Limpeza" meant the tooth as part of the name; normalising it away
    # would make that row unresolvable against its own catalog entry.
    assert strip_decoration("🦷 Limpeza") == "🦷 Limpeza"


def test_strip_decoration_removes_at_most_one_prefix():
    # We never render two, and stripping greedily would eat a clinic's own
    # leading emoji that happened to follow ours.
    assert strip_decoration("✅ 🏥 Consulta") == "🏥 Consulta"


def test_decoration_helpers_handle_blank_input():
    assert decorate(EMOJI_SERVICE, "") == ""
    assert decorate_if_fits(EMOJI_SERVICE, None) == ""
    assert decorate_and_truncate(EMOJI_SCHEDULE, "   ") == ""
