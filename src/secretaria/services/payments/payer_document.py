"""CPF/CNPJ reading for the payer-data step (decision D7). Pure: no I/O, no logging.

Only the arithmetic and the spellings live here; WHEN a document is asked for, where it is kept and
where it must never appear is `services/payments/payer_data.py`.

Two readers on purpose (owner, 2026-10-29):

  * `parse_cpf_answer` - the patient's ANSWER to the question: a CPF and nothing else, written
    as exactly 11 digits or as 123.456.789-01 (surrounding whitespace ignored). A CPF inside a
    sentence, other separators and the CNPJ are refused (the question is asked again).
  * `extract_document` - the redaction net (`payer_data.should_redact_inbound`): any CPF or CNPJ
    written anywhere in a message. It stays permissive so nothing that looks like a document
    reaches the message history.
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
_CPF_ANSWER = re.compile(r"\d{11}|\d{3}\.\d{3}\.\d{3}-\d{2}", re.ASCII)


def parse_cpf_answer(text: str | None) -> str | None:
    """The CPF (digits only) when `text` is exactly `52998224725` or `529.982.247-25` (surrounding
    whitespace ignored) with the right check digits, else None. The strict reader of the patient's
    answer; never use it as a redaction net (that is `extract_document`)."""
    candidate = (text or "").strip()
    if _CPF_ANSWER.fullmatch(candidate) is None:
        return None
    digits = digits_only(candidate)
    return digits if is_valid_cpf(digits) else None


def extract_document(text: str | None) -> str | None:
    """The first CPF or CNPJ (digits only) written in `text`, or None.

    The redaction net, NOT the reader of the patient's answer (`parse_cpf_answer`): accepts the
    usual spellings ("529.982.247-25", "52998224725", "meu cpf é 529.982.247-25", a CNPJ). A
    number that only LOOKS like one (wrong check digits, a repeated digit, a phone, 12 digits) is
    not a document.
    """
    for candidate in _CANDIDATE.findall(text or ""):
        digits = digits_only(candidate)
        if is_valid_cpf(digits) or is_valid_cnpj(digits):
            return digits
    return None
