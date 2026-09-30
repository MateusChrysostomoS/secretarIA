"""Strict e-mail reading for the payer-data step (the Mercado Pago payer). Pure: no I/O.

Deliberately narrower than RFC 5322: ASCII only, a dotted-atom local part (no quotes, no comments),
DNS-shaped domain labels and an alphabetic TLD of 2-24 letters. The provider rejects what it does
not like anyway; this only keeps typos and prose out of a charge request.
"""

import re

MAX_LENGTH = 254
MAX_LOCAL_LENGTH = 64

_LOCAL = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$"
)
_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_TLD = re.compile(r"^[A-Za-z]{2,24}$")


def is_valid_email(value: str | None) -> bool:
    """True for `local@domain` that passes the strict shape above (no surrounding spaces)."""
    if not value or len(value) > MAX_LENGTH or value != value.strip():
        return False
    local, at, domain = value.rpartition("@")
    if not at or not local or len(local) > MAX_LOCAL_LENGTH or not _LOCAL.match(local):
        return False
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        return False
    return _TLD.match(labels[-1]) is not None


# A token around an "@": the surrounding punctuation of a sentence ("...@x.com, obrigada") is not
# part of it.
_CANDIDATE = re.compile(r"[^\s<>()\[\],;:\"']+@[^\s<>()\[\],;:\"']+")


def extract_email(text: str | None) -> str | None:
    """The first valid e-mail written in `text` (domain lower-cased), or None.

    "meu email é Ana.Silva@Example.com.br, obrigada" -> "Ana.Silva@example.com.br". A token that
    only LOOKS like one (no TLD, two dots in a row, non-ASCII) is not an e-mail.
    """
    for candidate in _CANDIDATE.findall(text or ""):
        cleaned = candidate.rstrip(".")
        if is_valid_email(cleaned):
            local, _, domain = cleaned.rpartition("@")
            return f"{local}@{domain.lower()}"
    return None
