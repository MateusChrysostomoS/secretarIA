"""TASK-087 T7: every patient-facing date list carries the weekday ("Seg, 24/11")."""

from datetime import UTC, date, datetime, time

from secretaria.core.whatsapp_limits import (
    MAX_LIST_ROW_DESCRIPTION_CHARS,
    MAX_LIST_ROW_TITLE_CHARS,
)
from secretaria.services import flow_router as fr
from secretaria.services.availability_windows import FreeWindow
from secretaria.services.booking_dates import (
    WEEKDAY_SHORT,
    short_day_label,
    short_day_phrase,
)


def test_short_day_label_has_capitalised_weekday():
    assert short_day_label(date(2026, 11, 24)) == "Ter, 24/11"


def test_short_day_phrase_is_lowercase_for_sentences():
    assert short_day_phrase(date(2026, 11, 23)) == "seg, 23/11"


def test_short_day_label_accepts_datetime_and_saturday_accent():
    assert short_day_label(datetime(2026, 11, 28, 9, 0)) == "Sáb, 28/11"
    assert WEEKDAY_SHORT[6] == "Dom"


def test_router_reuses_the_shared_weekday_tuple():
    assert fr._WEEKDAY_SHORT_PT is WEEKDAY_SHORT


def test_free_window_payload_carries_weekday():
    window = FreeWindow(date(2026, 11, 23), time(8, 0), time(10, 0))
    assert window.payload()["weekday"] == "seg"


def test_appt_row_label_is_the_weekday_day_and_time():
    """Review P4: the title is only "Seg, 23/11 10:00" - a service after it was cut at 24."""
    appt = {
        "start_at": datetime(2026, 11, 23, 13, 0, tzinfo=UTC),  # 10:00 in São Paulo
        "appointment_type": "Consulta de retorno longa",
    }
    label = fr._appt_row_label(appt, None)
    assert label == "Seg, 23/11 10:00"
    assert len(label) <= MAX_LIST_ROW_TITLE_CHARS


def test_manage_pick_list_carries_the_service_in_the_row_description():
    """Review P4: WhatsApp's "suas consultas" list (remarcar/cancelar) shows the whole service
    in each row's description, the day and time in its title."""
    long_name = "Consulta de retorno longa " * 4  # longer than a row description
    appts = [
        {
            "id": "a",
            "start_at": datetime(2026, 11, 28, 13, 0, tzinfo=UTC),  # Saturday 10:00 local
            "appointment_type": "Consulta de retorno",
        },
        {
            "id": "b",
            "start_at": datetime(2026, 12, 1, 13, 0, tzinfo=UTC),
            "appointment_type": long_name,
        },
    ]

    bubble = fr._manage_pick_list_bubble(appts, "Qual consulta?")

    (first_id, first_title, first_description), (_, _, second_description) = bubble.rows
    assert first_id == f"slot|{appts[0]['start_at'].isoformat()}"
    assert first_title == "Sáb, 28/11 10:00"
    assert first_description == "Consulta de retorno"
    assert len(second_description) <= MAX_LIST_ROW_DESCRIPTION_CHARS
    assert all(len(row[1]) <= MAX_LIST_ROW_TITLE_CHARS for row in bubble.rows)
