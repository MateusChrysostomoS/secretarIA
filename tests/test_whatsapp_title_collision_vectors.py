"""Shared vectors: when two service names look the same in a WhatsApp list (TASK-084).

A `svc|` row title is cut at 24 characters (core/whatsapp_limits.py) and a tap arrives as
that visible title, which `canonical_service_name` resolves to the FIRST catalog entry with
the same key - so two names equal in their first 23 characters book the first one.

The Brain-Message front warns WhatsApp clinics about exactly these pairs with a TypeScript
port of the cut (Brain-Message-Frontend `lib/whatsapp-limits.ts`). Its test,
`lib/__tests__/whatsapp-title-collisions.test.ts`, carries THESE vectors byte for byte: change
one side, change the other. This file pins the Python behaviour they describe; it is not new
behaviour (a failing vector here means the vector is wrong, or the cut changed and the front
must follow).
"""

import pytest

from secretaria.core.whatsapp_limits import (
    EMOJI_SERVICE,
    decorate_if_fits,
    strip_decoration,
    truncate_list_row_title,
)
from secretaria.services.booking_scope import canonical_service_name

# (name, title shown in the list without the emoji, matcher key)
VECTORS = [
    ("Avaliação para cirurgia refrativa", "Avaliação para cirurgia…", "avaliação para cirurgia…"),
    ("Avaliação para cirurgia de catarata", "Avaliação para cirurgia…", "avaliação para cirurgia…"),
    ("AVALIAÇÃO PARA CIRURGIA DE CÓRNEA", "AVALIAÇÃO PARA CIRURGIA…", "avaliação para cirurgia…"),
    ("Consulta de rotina adulto", "Consulta de rotina adul…", "consulta de rotina adul…"),
    ("Consulta de rotina infantil", "Consulta de rotina infa…", "consulta de rotina infa…"),
    ("Campimetria visual com laudo", "Campimetria visual com…", "campimetria visual com…"),
    ("Campimetria visual com mapa", "Campimetria visual com…", "campimetria visual com…"),
    ("Campimetria visual computadorizada", "Campimetria visual comp…", "campimetria visual comp…"),
    ("Biometria ultrassônica X", "Biometria ultrassônica X", "biometria ultrassônica x"),
    ("Biometria ultrassônica XY", "Biometria ultrassônica…", "biometria ultrassônica…"),
    ("Clareamento 🦷 dental premium", "Clareamento 🦷 dental pr…", "clareamento 🦷 dental pr…"),
    ("Clareamento 🦷 dental princípio", "Clareamento 🦷 dental pr…", "clareamento 🦷 dental pr…"),
    ("Avaliacao para cirurgia refrativa", "Avaliacao para cirurgia…", "avaliacao para cirurgia…"),
    ("Retorno", "Retorno", "retorno"),
    ("Retorno pós-operatório", "Retorno pós-operatório", "retorno pós-operatório"),
]

# (first in the catalog, second, whether a tap on the second books the first)
PAIRS = [
    ("Avaliação para cirurgia refrativa", "Avaliação para cirurgia de catarata", True),
    ("AVALIAÇÃO PARA CIRURGIA DE CÓRNEA", "Avaliação para cirurgia refrativa", True),
    ("Consulta de rotina adulto", "Consulta de rotina infantil", False),
    ("Campimetria visual com laudo", "Campimetria visual com mapa", True),
    ("Campimetria visual com laudo", "Campimetria visual computadorizada", False),
    ("Biometria ultrassônica X", "Biometria ultrassônica XY", False),
    ("Clareamento 🦷 dental premium", "Clareamento 🦷 dental princípio", True),
    ("Avaliacao para cirurgia refrativa", "Avaliação para cirurgia de catarata", False),
    ("Retorno", "Retorno pós-operatório", False),
]


@pytest.mark.parametrize(("name", "shown", "key"), VECTORS)
def test_the_shown_title_and_the_matcher_key(name, shown, key):
    assert truncate_list_row_title(name) == shown
    assert strip_decoration(decorate_if_fits(EMOJI_SERVICE, name)).casefold() == key


@pytest.mark.parametrize(("first", "second", "collide"), PAIRS)
def test_a_tap_on_the_second_row_books_the_first_only_when_they_collide(first, second, collide):
    catalog = [{"name": first}, {"name": second}]
    tapped = decorate_if_fits(EMOJI_SERVICE, second)
    assert canonical_service_name(catalog, tapped) == (first if collide else second)
