"""Tests for services/payments/money.py — BRL free-text parsing/formatting.

`appointment_types[].price` is free text a clinic typed by hand; these tests
exercise the documented separator-ambiguity rule plus the "never raises"
guarantee (fuzzed) that services/payments/deposit_lifecycle.py::maybe_create_deposit
relies on to degrade to "no deposit" rather than ever 500ing on a weird price.
"""

import random

import pytest

from secretaria.services.payments.money import format_brl, parse_brl_to_cents


@pytest.mark.parametrize(
    "text,expected_cents",
    [
        ("R$ 250,00", 25000),
        ("250", 25000),
        ("1.250,50", 125050),
        ("250,5", 25050),
        ("R$1.250", 125000),
        ("r$ 99,90", 9990),  # case-insensitive "r$"
        ("  R$   10,00  ", 1000),  # surrounding + internal whitespace
        ("R$\xa0250,00", 25000),  # non-breaking space between "R$" and amount
        ("0", 0),
        ("10,0", 1000),  # single separator, 1 digit after -> decimal (tenths)
        ("1.234.567,89", 123456789),  # multiple thousands marks + decimal
    ],
)
def test_parse_brl_to_cents_valid(text: str, expected_cents: int) -> None:
    assert parse_brl_to_cents(text) == expected_cents


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "a combinar",
        "sob consulta",
        "R$",
        "grátis",
        "R$$$",
        "-",
        ",",
        ".",
        "R$ -",
        "abc,def",
        "..",
        ",,",
        "R$abc",
    ],
)
def test_parse_brl_to_cents_non_numeric_returns_none(text: str | None) -> None:
    assert parse_brl_to_cents(text) is None


def test_parse_brl_to_cents_never_raises_fuzzed() -> None:
    """Random garbage built from currency-adjacent characters must never raise —
    only ever return an int or None (maybe_create_deposit's guard depends on this)."""
    rng = random.Random(42)
    alphabet = "0123456789.,Rr$ \xa0abcXYZ-"
    for _ in range(2000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        result = parse_brl_to_cents(text)
        assert result is None or isinstance(result, int)


def test_format_brl_basic() -> None:
    assert format_brl(25000) == "R$ 250,00"


def test_format_brl_thousands_grouping() -> None:
    assert format_brl(125050) == "R$ 1.250,50"


def test_format_brl_multiple_thousands_marks() -> None:
    assert format_brl(100000000) == "R$ 1.000.000,00"


def test_format_brl_sub_real_amount() -> None:
    assert format_brl(50) == "R$ 0,50"


def test_format_brl_zero() -> None:
    assert format_brl(0) == "R$ 0,00"


def test_parse_and_format_roundtrip() -> None:
    cents = parse_brl_to_cents("R$ 1.250,50")
    assert cents == 125050
    assert format_brl(cents) == "R$ 1.250,50"
