"""The CPF/CNPJ half of the payer-data step (decision D7): check digits and spellings. Pure."""

import pytest

from secretaria.services.payments import payer_document as pd

CPF = "52998224725"  # a well-known valid test number, never a real person's
CNPJ = "11222333000181"  # ditto


@pytest.mark.parametrize(
    ("digits", "valid"),
    [
        (CPF, True),
        ("11144477735", True),
        ("52998224726", False),  # wrong last check digit
        ("52998224715", False),  # wrong first check digit
        ("11111111111", False),  # a repeated digit passes the arithmetic and is refused
        ("00000000000", False),
        ("1234567890", False),  # ten digits
        ("529982247250", False),  # twelve digits
        ("", False),
    ],
)
def test_cpf_check_digits(digits, valid):
    assert pd.is_valid_cpf(digits) is valid


@pytest.mark.parametrize(
    ("digits", "valid"),
    [
        (CNPJ, True),
        ("11222333000182", False),
        ("11222333000180", False),
        ("00000000000000", False),
        ("1122233300018", False),
    ],
)
def test_cnpj_check_digits(digits, valid):
    assert pd.is_valid_cnpj(digits) is valid


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("529.982.247-25", CPF),
        ("52998224725", CPF),
        ("meu cpf é 529.982.247-25, obrigado", CPF),
        (" 529 982 247 25 ", CPF),
        ("11.222.333/0001-81", CNPJ),
        ("11222333000181", CNPJ),
        ("529.982.247-26", None),  # looks like one
        ("11999998888", None),  # a phone number is not a document
        ("cpf 123", None),
        ("não sei", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_document_reads_the_usual_spellings_and_nothing_else(text, expected):
    assert pd.extract_document(text) == expected
