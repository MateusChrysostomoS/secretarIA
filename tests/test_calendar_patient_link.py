"""Unit tests for `services/calendar.py::build_patient_calendar_link` (pure, no network).

The link the PATIENT receives. It is not `event["htmlLink"]` — that one points
at the event on the clinic's own calendar and opens a permission error for
anybody else, which is the bug this helper exists to replace. These tests pin
the three things that break invisibly: the URL is the public TEMPLATE endpoint,
the instant survives the clinic's timezone, and the title survives accents.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime, timedelta  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.services.calendar import (  # noqa: E402
    PATIENT_CALENDAR_TEMPLATE_URL,
    build_patient_calendar_link,
)

_SP = ZoneInfo("America/Sao_Paulo")  # UTC-3, the product's default clinic zone
_UTC = ZoneInfo("UTC")


def _params(link: str) -> dict[str, str]:
    """The link's querystring as single-valued params (every key appears once)."""
    parsed = urlparse(link)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == PATIENT_CALENDAR_TEMPLATE_URL
    return {key: values[0] for key, values in parse_qs(parsed.query).items()}


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_points_at_the_public_template_not_at_an_event():
    """The whole point: a "create event" screen anyone can open, not a private
    event on the clinic's calendar."""
    link = build_patient_calendar_link(
        datetime(2026, 5, 29, 14, 0, tzinfo=_SP),
        datetime(2026, 5, 29, 14, 30, tzinfo=_SP),
        "Consulta - Joao",
    )
    assert link.startswith(f"{PATIENT_CALENDAR_TEMPLATE_URL}?")
    assert _params(link)["action"] == "TEMPLATE"
    # A Google event permalink looks like /calendar/event?eid=... — never this.
    assert "eid=" not in link


def test_carries_title_and_window():
    link = build_patient_calendar_link(
        datetime(2026, 5, 29, 14, 0, tzinfo=_SP),
        datetime(2026, 5, 29, 14, 30, tzinfo=_SP),
        "Consulta - Joao",
    )
    params = _params(link)
    assert params["text"] == "Consulta - Joao"
    # 14:00 in UTC-3 is 17:00Z.
    assert params["dates"] == "20260529T170000Z/20260529T173000Z"


def test_description_becomes_details_and_is_omitted_when_empty():
    args = (
        datetime(2026, 5, 29, 14, 0, tzinfo=_SP),
        datetime(2026, 5, 29, 14, 30, tzinfo=_SP),
        "Consulta - Joao",
    )
    assert _params(build_patient_calendar_link(*args, "Levar exames"))["details"] == (
        "Levar exames"
    )
    assert "details" not in _params(build_patient_calendar_link(*args))


def test_carries_no_location():
    """Deliberate: the address is not on TenantRuntimeConfig (so the LLM path
    could not fill one) and for a multi_unit tenant the real address is the
    UNIT's. No address beats a wrong one in the patient's own calendar."""
    link = build_patient_calendar_link(
        datetime(2026, 5, 29, 14, 0, tzinfo=_SP),
        datetime(2026, 5, 29, 14, 30, tzinfo=_SP),
        "Consulta - Joao",
    )
    assert "location" not in _params(link)


# --------------------------------------------------------------------------
# Timezone: the failure nobody sees until the patient shows up at the wrong hour
# --------------------------------------------------------------------------


def test_a_clinic_outside_utc_still_names_the_right_instant():
    """The digits in the URL are UTC, never the clinic's wall clock, and never
    accompanied by a &ctz= whose loss would silently reinterpret them."""
    link = build_patient_calendar_link(
        datetime(2026, 5, 29, 8, 0, tzinfo=_SP),
        datetime(2026, 5, 29, 8, 40, tzinfo=_SP),
        "Primeira Consulta - Ana",
    )
    params = _params(link)
    assert params["dates"] == "20260529T110000Z/20260529T114000Z"
    assert "ctz" not in params


def test_naive_datetimes_are_localized_with_tz():
    """Both booking paths hand over aware values; this is the contract that
    keeps a naive one from being read as UTC and shipped 3 hours off."""
    aware = build_patient_calendar_link(
        datetime(2026, 5, 29, 8, 0, tzinfo=_SP),
        datetime(2026, 5, 29, 8, 40, tzinfo=_SP),
        "Primeira Consulta - Ana",
    )
    naive = build_patient_calendar_link(
        datetime(2026, 5, 29, 8, 0),
        datetime(2026, 5, 29, 8, 40),
        "Primeira Consulta - Ana",
        tz=_SP,
    )
    assert naive == aware


def test_an_aware_datetime_ignores_tz():
    """tz localizes naive values only — it must never re-stamp an aware one."""
    link = build_patient_calendar_link(
        datetime(2026, 5, 29, 14, 0, tzinfo=_SP),
        datetime(2026, 5, 29, 14, 30, tzinfo=_SP),
        "Consulta - Joao",
        tz=_UTC,
    )
    assert _params(link)["dates"] == "20260529T170000Z/20260529T173000Z"


def test_a_naive_datetime_without_tz_refuses_to_guess():
    """No knowable instant. Raising beats emitting a link that is quietly wrong
    by the clinic's whole offset."""
    with pytest.raises(ValueError):
        build_patient_calendar_link(
            datetime(2026, 5, 29, 8, 0),
            datetime(2026, 5, 29, 8, 40),
            "Primeira Consulta - Ana",
        )


def test_an_appointment_that_crosses_midnight_keeps_both_dates():
    """A late slot in UTC-3 lands on the NEXT UTC day: the two halves of
    `dates` must be allowed to disagree about the date."""
    start = datetime(2026, 5, 29, 22, 30, tzinfo=_SP)
    link = build_patient_calendar_link(
        start, start + timedelta(minutes=60), "Consulta - Joao"
    )
    assert _params(link)["dates"] == "20260530T013000Z/20260530T023000Z"


def test_a_window_spanning_two_local_days_is_preserved():
    """Same for a window that genuinely straddles local midnight."""
    link = build_patient_calendar_link(
        datetime(2026, 5, 29, 23, 30, tzinfo=_SP),
        datetime(2026, 5, 30, 0, 30, tzinfo=_SP),
        "Plantao - Joao",
    )
    assert _params(link)["dates"] == "20260530T023000Z/20260530T033000Z"


# --------------------------------------------------------------------------
# Encoding: pt-BR service names are full of accents and spaces
# --------------------------------------------------------------------------


def test_accents_and_spaces_survive_the_round_trip():
    summary = "Consulta Cardiológica - João Gonçalves"
    details = "Avaliação completa — trazer exames"
    link = build_patient_calendar_link(
        datetime(2026, 5, 29, 14, 0, tzinfo=_SP),
        datetime(2026, 5, 29, 14, 30, tzinfo=_SP),
        summary,
        details,
    )
    # Nothing raw in the URL...
    assert " " not in link
    assert "ó" not in link
    assert "ç" not in link
    # ...and everything intact once Google decodes it.
    params = _params(link)
    assert params["text"] == summary
    assert params["details"] == details


def test_a_summary_with_url_metacharacters_cannot_forge_a_parameter():
    """A service named with an & or an = must not be able to inject a param."""
    link = build_patient_calendar_link(
        datetime(2026, 5, 29, 14, 0, tzinfo=_SP),
        datetime(2026, 5, 29, 14, 30, tzinfo=_SP),
        "Consulta&location=Rua Falsa&x=1",
    )
    params = _params(link)
    assert params["text"] == "Consulta&location=Rua Falsa&x=1"
    assert "location" not in params
    assert "x" not in params
    assert set(params) == {"action", "text", "dates"}
