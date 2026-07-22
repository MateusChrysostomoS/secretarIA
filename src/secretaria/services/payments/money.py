"""BRL free-text money parsing/formatting — no currency library, no network.

`appointment_types[].price` (models/tenant.py, schemas/config.py's
`AppointmentType.price`) is FREE TEXT: the clinic types whatever it wants
("R$ 250,00", "250", "a combinar", ...). `parse_brl_to_cents` turns that text
into integer cents (the unit every money computation in
services/payments/deposit_lifecycle.py works in), tolerating the ways a
Brazilian clinic actually types a price, and NEVER raising — a clinic's
free-text field is not a validated input, so "I can't tell what this means"
must degrade to "no deposit for this appointment type", never a 500.

Ambiguity rule for a value with exactly ONE separator ("," or "."): when
followed by exactly 3 digits, it is a THOUSANDS grouping mark ("R$1.250" ->
1250 reais — a currency value is never "grouped-only" without a decimal
part, so 3 trailing digits after a lone separator can only be a thousands
mark); when followed by 1 or 2 digits, it is the DECIMAL mark ("250,5" ->
250.50, "250,00" -> 250.00). With two-or-more separators, the LAST separator
is always the decimal mark and every earlier one is a thousands mark to
strip ("1.250,50" -> 1250.50). With zero separators, the whole string is
whole reais ("250" -> 250.00).
"""

import re

_NBSP = "\xa0"
_R_PREFIX = re.compile(r"(?i)^r\$\s*")
_ALLOWED = re.compile(r"[0-9.,]+")
_SEPARATORS = re.compile(r"[.,]")


def parse_brl_to_cents(text: str | None) -> int | None:
    """Parse free-text BRL into integer cents. Returns None, never raises.

    None for non-numeric input ("a combinar", "sob consulta", "", None,
    stray currency symbols) — see the module docstring for the separator
    ambiguity rule applied to genuinely numeric text.
    """
    if text is None:
        return None
    cleaned = text.replace(_NBSP, " ").strip()
    if not cleaned:
        return None
    cleaned = _R_PREFIX.sub("", cleaned).strip()
    if not cleaned or not _ALLOWED.fullmatch(cleaned):
        return None

    seps = _SEPARATORS.findall(cleaned)
    try:
        if not seps:
            reais, cents_part = cleaned, "00"
        elif len(seps) == 1:
            integer_part, frac_part = _SEPARATORS.split(cleaned)
            if not integer_part:
                return None
            if len(frac_part) == 3:
                reais, cents_part = integer_part + frac_part, "00"
            elif len(frac_part) in (1, 2):
                reais, cents_part = integer_part, frac_part.ljust(2, "0")
            else:
                return None
        else:
            last_sep_at = max(cleaned.rfind("."), cleaned.rfind(","))
            integer_raw, frac_part = cleaned[:last_sep_at], cleaned[last_sep_at + 1 :]
            if len(frac_part) not in (1, 2):
                return None
            reais = _SEPARATORS.sub("", integer_raw)
            cents_part = frac_part.ljust(2, "0")

        if not reais or not reais.isdigit() or not cents_part.isdigit():
            return None
        return int(reais) * 100 + int(cents_part)
    except (ValueError, IndexError):
        return None


def format_brl(cents: int) -> str:
    """Render integer cents as pt-BR currency text, e.g. 125050 -> "R$ 1.250,50"."""
    sign = "-" if cents < 0 else ""
    reais, remainder = divmod(abs(cents), 100)
    grouped = f"{reais:,}".replace(",", ".")
    return f"{sign}R$ {grouped},{remainder:02d}"
