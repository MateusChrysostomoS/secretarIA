"""The event's description and the patient's link carry WHERE and WITH WHOM (TASK-083 follow-up).

Owner (2026-11-13): the last message, the one with the Google Calendar link, must make the unit,
the address and the other details clear - and the same details must be in the event the patient
adds by tapping that link. A doctor who uses the secretarIA as a personal assistant and works in
several places, and a clinic with several units, both depend on it.

Pinned here: the builder's new lines (and that WITHOUT them the text is byte for byte today's), the
pt-BR date line, the unit helpers, and the Google TEMPLATE contract (`details` decoded with
urllib.parse, newlines travel as %0A).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.services.calendar import (  # noqa: E402
    build_event_description,
    build_patient_calendar_link,
    build_patient_link_details,
    format_event_when,
)
from secretaria.services.unit_booking import event_unit_fields, unit_fields  # noqa: E402

SP = ZoneInfo("America/Sao_Paulo")
START = datetime(2026, 11, 17, 14, 0, tzinfo=SP)  # a Tuesday


def _details(link: str) -> str:
    return parse_qs(urlparse(link).query)["details"][0]


# --------------------------------------------------------------------------
# build_event_description
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"service": "Consulta", "insurance": None}, "Serviço: Consulta\nConvênio: não informado"),
        (
            {"service": "Consulta", "insurance": "Unimed", "channel": "whatsapp"},
            "Serviço: Consulta\nConvênio: Unimed\nAgendado pelo: WhatsApp",
        ),
        (
            {
                "service": "Retorno",
                "insurance": "Particular",
                "channel": "brain_message",
                "attendee_name": "Maria",
            },
            "Serviço: Retorno\nConvênio: Particular\nConsulta para: Maria\nAgendado pelo: Portal",
        ),
        ({"service": None, "insurance": None}, "Convênio: não informado"),
    ],
)
def test_without_the_new_fields_the_text_is_todays_byte_for_byte(kwargs, expected):
    assert build_event_description(**kwargs) == expected
    # Passing the new fields as None/"" changes nothing either.
    assert (
        build_event_description(
            **kwargs, unit_name=None, unit_address="", professional=" ", when=None
        )
        == expected
    )


def test_every_field_in_order():
    text = build_event_description(
        service="Consulta",
        insurance="Particular",
        channel="brain_message",
        attendee_name="Maria",
        unit_name="Zona Sul",
        unit_address="Av. B, 20",
        professional="Dra. Ana",
        when="terça-feira, 17/11/2026 às 14:00",
    )
    assert text == (
        "Serviço: Consulta\n"
        "Profissional: Dra. Ana\n"
        "Data e horário: terça-feira, 17/11/2026 às 14:00\n"
        "Unidade: Zona Sul\n"
        "Endereço: Av. B, 20\n"
        "Convênio: Particular\n"
        "Consulta para: Maria\n"
        "Agendado pelo: Portal"
    )


def test_each_new_field_alone():
    base = {"service": "Consulta", "insurance": "Unimed"}
    assert "Unidade: Centro\n" in build_event_description(**base, unit_name="Centro")
    assert "Endereço" not in build_event_description(**base, unit_name="Centro")
    assert "Endereço: Rua A, 10\n" in build_event_description(
        **base, unit_name="Centro", unit_address="Rua A, 10"
    )
    assert "Profissional: Dr. Zé\n" in build_event_description(**base, professional="Dr. Zé")
    assert "Data e horário: hoje\n" in build_event_description(**base, when="hoje")


def test_an_address_without_a_unit_name_is_still_stated():
    text = build_event_description(service="X", insurance=None, unit_address="Rua A, 10")
    assert "Endereço: Rua A, 10" in text and "Unidade:" not in text


def test_values_are_kept_on_one_line():
    """A stray newline in a unit/doctor name cannot forge another labelled line."""
    text = build_event_description(
        service="X", insurance=None, unit_name="Centro\nConvênio: falso", professional="Dra.\r\nAna"
    )
    assert "Unidade: Centro Convênio: falso\n" in text
    assert "Profissional: Dra. Ana\n" in text


# --------------------------------------------------------------------------
# format_event_when
# --------------------------------------------------------------------------


def test_when_is_the_clinic_clock_in_portuguese():
    assert format_event_when(START) == "terça-feira, 17/11/2026 às 14:00"
    # An instant in UTC is shown on the clinic's clock.
    assert format_event_when(START.astimezone(UTC), SP) == "terça-feira, 17/11/2026 às 14:00"
    # Naive = already the clinic's local time.
    assert format_event_when(datetime(2026, 11, 22, 9, 5), SP) == "domingo, 22/11/2026 às 09:05"
    assert format_event_when(datetime(2026, 11, 21, 9, 5)) == "sábado, 21/11/2026 às 09:05"


# --------------------------------------------------------------------------
# unit helpers
# --------------------------------------------------------------------------


def test_unit_fields():
    unit = SimpleNamespace(id=uuid4(), name=" Zona Sul ", address=" Av. B, 20 ")
    assert unit_fields(unit) == {"unit_name": "Zona Sul", "unit_address": "Av. B, 20"}
    assert unit_fields(SimpleNamespace(name="Centro", address=None)) == {"unit_name": "Centro"}
    assert unit_fields(None) == {}


def test_event_unit_fields_name_the_professional_only_at_a_unit():
    unit = SimpleNamespace(id=uuid4(), name="Centro", address="Rua A, 10")
    doctor = SimpleNamespace(name="Dra. Ana")
    assert event_unit_fields(unit, doctor) == {
        "unit_name": "Centro",
        "unit_address": "Rua A, 10",
        "professional": "Dra. Ana",
    }
    assert event_unit_fields(unit, "Dr. Zé")["professional"] == "Dr. Zé"
    assert "professional" not in event_unit_fields(unit, None)
    # No unit: today's description, byte for byte (no professional line either).
    assert event_unit_fields(None, doctor) == {}


# --------------------------------------------------------------------------
# the patient's link
# --------------------------------------------------------------------------


def test_patient_link_details_carry_unit_address_professional_and_date():
    details = build_patient_link_details(
        service="Consulta",
        insurance="Particular",
        start=START,
        tz=SP,
        attendee_name=None,
        professional="Dra. Ana",
        unit_name="Zona Sul",
        unit_address="Av. B, 20",
    )
    link = build_patient_calendar_link(
        START,
        START + timedelta(minutes=30),
        "Consulta - Maria",
        details,
        tz=SP,
        location="Zona Sul — Av. B, 20",
    )
    decoded = _details(link)
    assert decoded == details
    assert decoded == (
        "Serviço: Consulta\n"
        "Profissional: Dra. Ana\n"
        "Data e horário: terça-feira, 17/11/2026 às 14:00\n"
        "Unidade: Zona Sul\n"
        "Endereço: Av. B, 20\n"
        "Convênio: Particular"
    )
    # Google's TEMPLATE contract: newlines as %0A, spaces as %20 (never "+").
    assert "%0A" in link and "+" not in link.split("?", 1)[1]
    assert parse_qs(urlparse(link).query)["location"][0] == "Zona Sul — Av. B, 20"


def test_patient_link_details_never_carry_a_channel():
    details = build_patient_link_details(service="X", insurance=None, start=START, tz=SP)
    assert "Agendado pelo" not in details
    assert details == (
        "Serviço: X\nData e horário: terça-feira, 17/11/2026 às 14:00\nConvênio: não informado"
    )
