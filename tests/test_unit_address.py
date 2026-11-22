"""Pure tests for services/unit_address.py (TASK-090 B2)."""

import pytest

from secretaria.services.unit_address import (
    BR_STATE_NAMES,
    UnitAddressParts,
    format_unit_address,
    invalid_fields,
    normalize_cep,
    oversize_line_fields,
)

FULL = UnitAddressParts(
    cep="01310-100",
    street="Av. Paulista",
    number="1000",
    complement="sala 2",
    neighborhood="Bela Vista",
    city="São Paulo",
    state="SP",
)


def test_normalize_cep() -> None:
    assert normalize_cep("01310-100") == "01310100"
    assert normalize_cep("01310100") == "01310100"
    assert normalize_cep("1310100") is None
    assert normalize_cep("013101000") is None
    assert normalize_cep("") is None
    assert normalize_cep(None) is None


EXPECTED_STATE_NAMES = {
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


def test_state_names_cover_exactly_the_27_ufs() -> None:
    assert len(EXPECTED_STATE_NAMES) == 27
    assert set(BR_STATE_NAMES) == set(EXPECTED_STATE_NAMES)


@pytest.mark.parametrize(("uf", "name"), sorted(EXPECTED_STATE_NAMES.items()))
def test_state_full_names(uf: str, name: str) -> None:
    # Spelled out independently of the module so an accent typo there is caught.
    assert BR_STATE_NAMES[uf] == name


def test_normalize_cep_rejects_non_ascii_digits() -> None:
    assert normalize_cep("٠١٣١٠١٠٠") is None  # Arabic-Indic digits
    assert normalize_cep("０１３１０１００") is None  # fullwidth digits
    assert normalize_cep("01310-100") == "01310100"


def test_invalid_fields_flags_parts_longer_than_their_column() -> None:
    def with_(**kw):
        return UnitAddressParts(**{**FULL.__dict__, **kw})

    assert invalid_fields(with_(street="s" * 255)) == []
    assert invalid_fields(with_(street="s" * 256)) == ["street"]
    assert invalid_fields(with_(number="1" * 31)) == ["number"]
    assert invalid_fields(with_(complement="c" * 256, neighborhood="n" * 256)) == [
        "complement",
        "neighborhood",
    ]


def test_oversize_line_fields() -> None:
    assert oversize_line_fields(FULL) == []
    # Shortening the street alone is enough: only it is named.
    one = UnitAddressParts(**{**FULL.__dict__, "street": "s" * 255, "city": "c" * 255})
    assert oversize_line_fields(one) == ["street"]
    # Every free-text part at its maximum: several must shrink (ties keep field order).
    many = UnitAddressParts(
        **{
            **FULL.__dict__,
            "street": "s" * 255,
            "complement": "k" * 255,
            "neighborhood": "b" * 255,
            "city": "c" * 255,
        }
    )
    assert oversize_line_fields(many) == ["street", "complement", "neighborhood"]


def test_invalid_fields_complete_address_is_valid() -> None:
    assert invalid_fields(FULL) == []


def test_invalid_fields_lists_missing_and_bad_uf() -> None:
    assert invalid_fields(UnitAddressParts()) == ["cep", "street", "number", "city", "state"]
    assert invalid_fields(UnitAddressParts(**{**FULL.__dict__, "state": "XX"})) == ["state"]
    assert invalid_fields(UnitAddressParts(**{**FULL.__dict__, "cep": "123"})) == ["cep"]
    assert invalid_fields(UnitAddressParts(**{**FULL.__dict__, "street": "  "})) == ["street"]


def test_invalid_fields_complement_and_neighborhood_are_optional() -> None:
    parts = UnitAddressParts(**{**FULL.__dict__, "complement": None, "neighborhood": None})
    assert invalid_fields(parts) == []


def test_invalid_fields_accepts_lowercase_uf() -> None:
    assert invalid_fields(UnitAddressParts(**{**FULL.__dict__, "state": " sp "})) == []


def test_format_unit_address_full() -> None:
    assert format_unit_address(FULL) == (
        "Av. Paulista, 1000 - sala 2, Bela Vista, São Paulo - SP, 01310-100"
    )


def test_format_unit_address_without_complement() -> None:
    parts = UnitAddressParts(**{**FULL.__dict__, "complement": None})
    assert format_unit_address(parts) == "Av. Paulista, 1000, Bela Vista, São Paulo - SP, 01310-100"


def test_format_unit_address_omits_empty_parts() -> None:
    parts = UnitAddressParts(street="Rua A", number="1", city="Rio", state="RJ", cep="20000000")
    assert format_unit_address(parts) == "Rua A, 1, Rio - RJ, 20000-000"
    assert format_unit_address(UnitAddressParts()) == ""
