"""The e-mail half of the payer-data step (the Mercado Pago payer): a strict pure reader."""

import pytest

from secretaria.services.payments import payer_email as pe


@pytest.mark.parametrize(
    "value",
    [
        "maria@example.com",
        "maria.silva@example.com.br",
        "m+clinica@sub.example.org",
        "a_b-c@x1.io",
        "o'brien@example.com",
    ],
)
def test_ordinary_addresses_are_valid(value):
    assert pe.is_valid_email(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "maria",
        "maria@",
        "@example.com",
        "maria@example",  # no TLD
        "maria@example.c",  # a one-letter TLD
        "maria@example.123",  # a numeric TLD
        "maria..silva@example.com",  # two dots in a row
        ".maria@example.com",
        "maria.@example.com",
        "maria@-example.com",
        "maria@example-.com",
        "maria@exa mple.com",
        " maria@example.com",  # surrounding space is not part of an address
        "maria@example.com ",
        "joão@example.com",  # non-ASCII local part
        "maria@exämple.com",
        "a@b@example.com",
        ("a" * 65) + "@example.com",  # local part over 64
        "m@" + ("a" * 63 + ".") * 4 + "com",  # over 254 in total
    ],
)
def test_things_that_only_look_like_addresses_are_refused(value):
    assert pe.is_valid_email(value) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("maria@example.com", "maria@example.com"),
        ("meu email é Ana.Silva@Example.com.br, obrigada", "Ana.Silva@example.com.br"),
        ("<ana@example.com>", "ana@example.com"),
        ("ana@example.com.", "ana@example.com"),  # the full stop of the sentence
        ("escreve pra (ana@example.com) por favor", "ana@example.com"),
        ("ana_maria@x", None),
        ("me chama no zap", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_email_reads_an_address_out_of_a_sentence(text, expected):
    assert pe.extract_email(text) == expected
