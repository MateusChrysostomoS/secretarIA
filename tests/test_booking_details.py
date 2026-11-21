"""The booking details shown before an AI-landed confirmation (TASK-030 P3, spec §4.4.2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from secretaria.core.whatsapp_limits import MAX_TEXT_MESSAGE_CHARS, TRUNCATION_MARK  # noqa: E402
from secretaria.services.attendee import ATTENDEE_SELF  # noqa: E402
from secretaria.services.booking_details import (  # noqa: E402
    DETAILS_HEADER,
    REQUIREMENTS_KEEP,
    booking_details_text,
    clinic_address_line,
    price_text,
)

ANA = SimpleNamespace(name="Dra. Ana", specialty="Cardiologia")
FULL = {
    "name": "Consulta",
    "price": "250",
    "description": "Consulta de rotina.",
    "long_description": "Avaliação completa.",
    "requirements": ["Jejum de 8 horas", "Trazer exames anteriores"],
}
ADDRESS = {
    "line": "Rua A, 1",
    "complement": "Sala 2",
    "neighborhood": "Centro",
    "city": "São Paulo",
    "state": "SP",
    "postal_code": "01000-000",
}


def test_every_item_the_skipped_steps_would_have_shown():
    text = booking_details_text(
        service=FULL,
        professional=ANA,
        insurance="Unimed",
        attendee_name="Maria da Silva",
        address=clinic_address_line(ADDRESS),
    )
    assert text == (
        "Confira os detalhes da sua consulta:\n"
        "\n"
        "🥼 Profissional: Dra. Ana — Cardiologia\n"
        # Under a convênio the particular price is not shown (Instituto Gicovate, 2026-11-09).
        "🏥 Serviço: Consulta\n"
        "Convênio: Unimed\n"
        "👤 Para: Maria da Silva\n"
        "Endereço: Rua A, 1, Sala 2, Centro, São Paulo/SP, CEP 01000-000\n"
        "\n"
        "Avaliação completa.\n"
        "\n"
        "O que levar / preparo:\n"
        "• Jejum de 8 horas\n"
        "• Trazer exames anteriores"
    )


def test_a_service_with_no_price_description_or_requirements_still_gets_its_lines():
    text = booking_details_text(
        service={"name": "Consulta"},
        professional=None,
        insurance=None,
        attendee_name=ATTENDEE_SELF,
        address=None,
    )
    assert text == "Confira os detalhes da sua consulta:\n\n🏥 Serviço: Consulta\n👤 Para: você"


def test_the_short_description_stands_in_for_a_missing_long_one():
    text = booking_details_text(
        service={"name": "Consulta", "description": "Consulta de rotina."},
        professional=None,
        insurance=None,
        attendee_name=None,
        address=None,
    )
    assert (
        text
        == "Confira os detalhes da sua consulta:\n\n🏥 Serviço: Consulta\n\nConsulta de rotina."
    )


def test_a_never_answered_pra_quem_prints_no_line():
    text = booking_details_text(
        service={"name": "Consulta"},
        professional=None,
        insurance=None,
        attendee_name=None,
        address=None,
    )
    assert "Para:" not in text


def test_requirements_stored_as_one_string_are_one_item():
    text = booking_details_text(
        service={"name": "Consulta", "requirements": "Jejum de 8 horas"},
        professional=None,
        insurance=None,
        attendee_name=None,
        address=None,
    )
    assert text.endswith("O que levar / preparo:\n• Jejum de 8 horas")


def test_a_text_longer_than_the_whatsapp_limit_drops_the_description_then_requirements():
    service = {
        "name": "Consulta",
        "price": "250",
        "long_description": "x" * 2000,
        "description": "Consulta de rotina.",
        "requirements": [f"Item {n:02d} " + "y" * 290 for n in range(20)],
    }
    text = booking_details_text(
        service=service,
        professional=ANA,
        insurance="Particular",
        attendee_name=ATTENDEE_SELF,
        address="Rua A, 1, Recife",
    )
    assert len(text) <= MAX_TEXT_MESSAGE_CHARS
    # The lines that identify the booking are never the ones cut.
    for line in (
        "🥼 Profissional: Dra. Ana — Cardiologia",
        "🏥 Serviço: Consulta",
        "Convênio: Particular",
        "👤 Para: você",
        "Endereço: Rua A, 1, Recife",
    ):
        assert line in text
    assert "x" * 50 not in text  # the long description went first
    assert "Consulta de rotina." not in text  # then the short one
    assert text.count("• ") == REQUIREMENTS_KEEP
    assert text.endswith(f"\n{TRUNCATION_MARK}")


def test_a_pathological_identity_block_is_hard_cut_to_the_limit():
    giant = SimpleNamespace(name="D" * 5000, specialty=None)
    text = booking_details_text(
        service={"name": "Consulta"},
        professional=giant,
        insurance=None,
        attendee_name=None,
        address=None,
    )
    assert len(text) == MAX_TEXT_MESSAGE_CHARS
    assert text.startswith(DETAILS_HEADER)


def test_a_smaller_budget_is_honoured():
    text = booking_details_text(
        service=FULL,
        professional=ANA,
        insurance=None,
        attendee_name=None,
        address=None,
        max_chars=120,
    )
    assert len(text) <= 120


@pytest.mark.parametrize(
    "address, expected",
    [
        (ADDRESS, "Rua A, 1, Sala 2, Centro, São Paulo/SP, CEP 01000-000"),
        ({"line": "Rua A, 1", "city": "Recife"}, "Rua A, 1, Recife"),
        ({"line": "Rua A, 1", "state": "PE"}, "Rua A, 1, PE"),
        ({"line": "  ", "city": "Recife"}, None),
        ({"city": "Recife"}, None),
        ({"line": 7}, None),
        ("Rua A, 1", None),
        (None, None),
    ],
)
def test_clinic_address_line_needs_a_street(address, expected):
    assert clinic_address_line(address) == expected


@pytest.mark.parametrize(
    "price, expected",
    [
        ("250", "R$250"),
        ("R$ 250", "R$ 250"),
        ("r$ 99", "r$ 99"),
        (250, "R$250"),
        (None, None),
        ("", None),
        (0, None),
    ],
)
def test_price_text(price, expected):
    assert price_text(price) == expected


# --------------------------------------------------------------------------
# Price under a convênio (Instituto Gicovate, 2026-11-09: "o Unimed não tem valor")
# --------------------------------------------------------------------------


@pytest.mark.parametrize("insurance", [None, "", "Particular", "particular "])
def test_particular_or_no_answer_shows_no_price_either(insurance):
    # TASK-088 R23: money is said only on the final confirmation card - not in this message.
    text = booking_details_text(
        service=FULL, professional=None, insurance=insurance, attendee_name=None, address=None
    )
    assert "🏥 Serviço: Consulta" in text
    assert "R$" not in text


@pytest.mark.parametrize("insurance", ["Unimed", "Cassi do meu pai"])
def test_a_convenio_hides_the_particular_price(insurance):
    text = booking_details_text(
        service=FULL, professional=None, insurance=insurance, attendee_name=None, address=None
    )
    assert "R$" not in text
    assert f"Convênio: {insurance}" in text
