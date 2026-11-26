"""Structured Brazilian address of a unit (TASK-090).

A unit stores its address as parts (CEP, street, number, complement, neighborhood, city, UF);
the single text line in `units.address` - the one every booking/notification text reads - is
always DERIVED from those parts by `format_unit_address`. A legacy unit that only has the text
line keeps working and is reported as `address_incomplete` (see `invalid_fields`).

Pure functions, no I/O. Country is fixed (Brasil) and is not stored.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

BR_STATE_NAMES: Mapping[str, str] = {
    "AC": "Acre",
    "AL": "Alagoas",
    "AP": "Amapá",
    "AM": "Amazonas",
    "BA": "Bahia",
    "CE": "Ceará",
    "DF": "Distrito Federal",
    "ES": "Espírito Santo",
    "GO": "Goiás",
    "MA": "Maranhão",
    "MT": "Mato Grosso",
    "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais",
    "PA": "Pará",
    "PB": "Paraíba",
    "PR": "Paraná",
    "PE": "Pernambuco",
    "PI": "Piauí",
    "RJ": "Rio de Janeiro",
    "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul",
    "RO": "Rondônia",
    "RR": "Roraima",
    "SC": "Santa Catarina",
    "SP": "São Paulo",
    "SE": "Sergipe",
    "TO": "Tocantins",
}

# How pt-BR says "in <state>": "na Bahia", "no Paraná", "em São Paulo". The greeting prints
# `<preposition> <name>`, so a state without its own entry here could not be named correctly.
BR_STATE_PREPOSITIONS: Mapping[str, str] = {
    "AC": "no",
    "AL": "em",
    "AP": "no",
    "AM": "no",
    "BA": "na",
    "CE": "no",
    "DF": "no",
    "ES": "no",
    "GO": "em",
    "MA": "no",
    "MT": "no",
    "MS": "no",
    "MG": "em",
    "PA": "no",
    "PB": "na",
    "PR": "no",
    "PE": "em",
    "PI": "no",
    "RJ": "no",
    "RN": "no",
    "RS": "no",
    "RO": "em",
    "RR": "em",
    "SC": "em",
    "SP": "em",
    "SE": "em",
    "TO": "no",
}

# The parts every unit address needs (complement and neighborhood are optional).
REQUIRED_FIELDS: tuple[str, ...] = ("cep", "street", "number", "city", "state")

# Max length of each free-text part = its `units` column; the derived line must fit
# `units.address` (String(512)). Checked here so an oversize value is a 422, never a DB error.
MAX_PART_LENGTHS: Mapping[str, int] = {
    "street": 255,
    "number": 30,
    "complement": 255,
    "neighborhood": 255,
    "city": 255,
}
MAX_ADDRESS_LENGTH = 512

# ASCII digits only: `\d` would also accept other Unicode digits (e.g. Arabic-Indic).
_NON_DIGITS = re.compile(r"[^0-9]")


@dataclass(frozen=True)
class UnitAddressParts:
    cep: str | None = None
    street: str | None = None
    number: str | None = None
    complement: str | None = None
    neighborhood: str | None = None
    city: str | None = None
    state: str | None = None


def clean_text(raw: str | None) -> str | None:
    """Strip; blank becomes None."""
    text = (raw or "").strip()
    return text or None


def normalize_cep(raw: str | None) -> str | None:
    """CEP as exactly 8 digits (mask accepted), or None when it does not have 8 digits."""
    digits = _NON_DIGITS.sub("", raw or "")
    return digits if len(digits) == 8 else None


def normalize_state(raw: str | None) -> str | None:
    """UF upper-cased and stripped (validity is checked by `invalid_fields`)."""
    text = (raw or "").strip().upper()
    return text or None


def invalid_fields(p: UnitAddressParts) -> list[str]:
    """Names of the parts that are missing, invalid or longer than their column, in a stable
    order (cep, street, number, complement, neighborhood, city, state)."""
    bad: list[str] = []
    if normalize_cep(p.cep) is None:
        bad.append("cep")
    for name in ("street", "number", "complement", "neighborhood", "city"):
        value = clean_text(getattr(p, name))
        required = name in REQUIRED_FIELDS
        if (required and not value) or (value and len(value) > MAX_PART_LENGTHS[name]):
            bad.append(name)
    if normalize_state(p.state) not in BR_STATE_NAMES:
        bad.append("state")
    return bad


_PART_NAMES: tuple[str, ...] = (
    "cep",
    "street",
    "number",
    "complement",
    "neighborhood",
    "city",
    "state",
)


def parts_of(row: object) -> UnitAddressParts:
    """The address parts of anything carrying them as attributes (a `Unit` row)."""
    return UnitAddressParts(**{name: getattr(row, name, None) for name in _PART_NAMES})


def address_complete(row: object) -> bool:
    """Whether `row`'s address passes the same test a new unit must pass (`invalid_fields`
    empty): CEP, street, number, city and UF valid; complement and neighborhood optional.
    The negation is what the hub reports as `address_incomplete`."""
    return not invalid_fields(parts_of(row))


def oversize_line_fields(p: UnitAddressParts) -> list[str]:
    """When the derived line would exceed `MAX_ADDRESS_LENGTH`, the longest free-text parts
    (greedily, until dropping them would make it fit); `[]` when it fits."""
    excess = len(format_unit_address(p)) - MAX_ADDRESS_LENGTH
    if excess <= 0:
        return []
    lengths = {
        name: len(clean_text(getattr(p, name)) or "")
        for name in ("street", "complement", "neighborhood", "city")
    }
    names: list[str] = []
    for name, length in sorted(lengths.items(), key=lambda item: item[1], reverse=True):
        if excess <= 0 or length == 0:
            break
        names.append(name)
        excess -= length
    order = ("street", "complement", "neighborhood", "city")
    return [name for name in order if name in names]


def format_unit_address(p: UnitAddressParts) -> str:
    """"street, number[ - complement], neighborhood, city - UF, 00000-000"; empty parts omitted."""
    street = clean_text(p.street)
    number = clean_text(p.number)
    complement = clean_text(p.complement)
    neighborhood = clean_text(p.neighborhood)
    city = clean_text(p.city)
    state = normalize_state(p.state)
    cep = normalize_cep(p.cep)

    number_part = " - ".join(x for x in (number, complement) if x)
    city_part = " - ".join(x for x in (city, state) if x)
    cep_part = f"{cep[:5]}-{cep[5:]}" if cep else None
    segments = (street, number_part, neighborhood, city_part, cep_part)
    return ", ".join(s for s in segments if s)
