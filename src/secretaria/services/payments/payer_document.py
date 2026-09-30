"""CPF/CNPJ reading for the payer-data step (decision D7). Pure: no I/O, no logging.

Only the arithmetic and the spellings live here; WHEN a document is asked for, where it is kept and
where it must never appear is `services/payments/payer_data.py`.
"""

import re


def digits_only(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def is_valid_cpf(digits: str) -> bool:
    """11 digits, not one repeated digit, both check digits right."""
    if len(digits) != 11 or not digits.isdigit() or digits == digits[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(digits[i]) * (size + 1 - i) for i in range(size))
        if (total * 10 % 11) % 10 != int(digits[size]):
            return False
    return True


_CNPJ_WEIGHTS = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def is_valid_cnpj(digits: str) -> bool:
    """14 numeric digits, not one repeated digit, both check digits right."""
    if len(digits) != 14 or not digits.isdigit() or digits == digits[0] * 14:
        return False
    for size, weights in ((12, _CNPJ_WEIGHTS), (13, (6, *_CNPJ_WEIGHTS))):
        total = sum(int(digits[i]) * weights[i] for i in range(size))
        remainder = total % 11
        if (0 if remainder < 2 else 11 - remainder) != int(digits[size]):
            return False
    return True


_CANDIDATE = re.compile(r"\d[\d.\-/ ]*\d")


def extract_document(text: str | None) -> str | None:
    """The first CPF or CNPJ (digits only) written in `text`, or None.

    Accepts the usual spellings ("529.982.247-25", "52998224725", "meu cpf é 529.982.247-25").
    A number that only LOOKS like one (wrong check digits, a repeated digit, a phone, 12 digits)
    is not a document.
    """
    for candidate in _CANDIDATE.findall(text or ""):
        digits = digits_only(candidate)
        if is_valid_cpf(digits) or is_valid_cnpj(digits):
            return digits
    return None
