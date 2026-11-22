"""Pure tests for services/unit_address.py (TASK-090 B2)."""

from secretaria.services.unit_address import (
    BR_STATE_NAMES,
    UnitAddressParts,
    format_unit_address,
    invalid_fields,
    normalize_cep,
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


def test_state_names_cover_the_27_ufs() -> None:
    assert len(BR_STATE_NAMES) == 27
    assert BR_STATE_NAMES["MG"] == "Minas Gerais"
    assert BR_STATE_NAMES["RN"] == "Rio Grande do Norte"


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
