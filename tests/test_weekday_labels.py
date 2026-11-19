"""TASK-087 T7: every patient-facing date list carries the weekday ("Seg, 24/11")."""

from datetime import UTC, date, datetime, time

from secretaria.core.whatsapp_limits import MAX_LIST_ROW_TITLE_CHARS
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


def test_appt_row_label_starts_with_weekday_and_fits_whatsapp_title():
    appt = {
        "start_at": datetime(2026, 11, 23, 13, 0, tzinfo=UTC),  # 10:00 in São Paulo
        "appointment_type": "Consulta de retorno longa",
    }
    label = fr._appt_row_label(appt, None)
    assert label.startswith("Seg, 23/11 10:00")
    assert len(label) <= MAX_LIST_ROW_TITLE_CHARS
