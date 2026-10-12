"""The AI booking draft v2: wire format, v1 compatibility and the parked record (TASK-030 P2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
import json  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.booking_draft import (  # noqa: E402
    BookingDraft,
    draft_from_record,
    draft_record,
)

SAVED = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.UTC)


def test_the_v2_payload_round_trips_with_the_six_keys():
    professional_id = uuid4()
    draft = BookingDraft(
        service="Consulta",
        professional_id=professional_id,
        insurance="Unimed",
        attendee="other",
        day=dt.date(2026, 10, 8),
        time=dt.time(10, 0),
    )
    assert json.loads(draft.to_payload()) == {
        "t": "Consulta",
        "p": str(professional_id),
        "i": "Unimed",
        "w": "other",
        "d": "2026-10-08",
        "h": "10:00",
    }
    assert BookingDraft.from_payload(draft.to_payload()) == draft


def test_the_v1_payload_still_parses_with_unknown_for_whom():
    draft = BookingDraft.from_payload('{"t": "Limpeza", "p": null, "i": "Unimed"}')
    assert draft == BookingDraft(service="Limpeza", insurance="Unimed")
    assert draft.attendee is None


def test_blank_strings_are_absent_and_long_ones_are_capped():
    assert BookingDraft.from_payload('{"t": "  ", "i": ""}') == BookingDraft()
    assert len(BookingDraft.from_payload(json.dumps({"t": "x" * 500})).service) == 120


@pytest.mark.parametrize(
    "raw",
    [
        "oops",
        "[]",
        "null",
        '{"t": 7}',
        '{"p": "invalid"}',
        '{"w": "Maria Silva"}',  # never a name
        '{"d": "08/10/2026"}',
        '{"d": "2026-02-30"}',
        '{"h": "10h"}',
        '{"h": "25:00"}',
    ],
)
def test_a_corrupt_payload_raises_value_error(raw):
    with pytest.raises(ValueError):
        BookingDraft.from_payload(raw)


def test_supplied_fields_names_only_what_is_present_in_order():
    draft = BookingDraft(day=dt.date(2026, 10, 8), service="Consulta", attendee="self")
    assert draft.supplied_fields() == ("service", "for_whom", "day")
    assert BookingDraft().supplied_fields() == ()


def test_a_record_round_trips_and_carries_its_timestamp():
    draft = BookingDraft(service="Consulta", attendee="other")
    record = draft_record(draft, saved_at=SAVED)
    assert record["saved_at"] == "2026-10-05T12:00:00+00:00"
    assert draft_from_record(record, now=SAVED + dt.timedelta(minutes=29)) == draft


def test_a_record_expires_after_30_minutes():
    record = draft_record(BookingDraft(service="Consulta"), saved_at=SAVED)
    assert fr.FLOW_DRAFT_TTL_MINUTES == 30
    assert draft_from_record(record, now=SAVED + dt.timedelta(minutes=31)) is None


def test_a_naive_saved_at_is_read_as_utc():
    record = {**BookingDraft(service="Consulta").to_dict(), "saved_at": "2026-10-05T12:00:00"}
    assert draft_from_record(record, now=SAVED + dt.timedelta(minutes=5)) is not None


@pytest.mark.parametrize(
    "record",
    [None, "x", {}, {"t": "Consulta"}, {"t": 7, "saved_at": "2026-10-05T12:00:00+00:00"}],
)
def test_a_missing_or_corrupt_record_is_none(record):
    assert draft_from_record(record, now=SAVED) is None


def test_draft_record_refuses_a_naive_timestamp():
    with pytest.raises(ValueError):
        draft_record(BookingDraft(), saved_at=dt.datetime(2026, 10, 5, 12, 0))
