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

# The parts every unit address needs (complement and neighborhood are optional).
REQUIRED_FIELDS: tuple[str, ...] = ("cep", "street", "number", "city", "state")

_NON_DIGITS = re.compile(r"\D")


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
    """Names (of `REQUIRED_FIELDS`) that are missing or invalid, in a stable order."""
    bad: list[str] = []
    if normalize_cep(p.cep) is None:
        bad.append("cep")
    if not clean_text(p.street):
        bad.append("street")
    if not clean_text(p.number):
        bad.append("number")
    if not clean_text(p.city):
        bad.append("city")
    if normalize_state(p.state) not in BR_STATE_NAMES:
        bad.append("state")
    return bad


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
