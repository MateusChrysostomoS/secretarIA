"""Convite do Google ao paciente (MVP Portal, Task 7B)."""

import os
from unittest.mock import MagicMock

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from secretaria.services.calendar import CalendarService  # noqa: E402


def _client(existing):
    fake = MagicMock()
    fake.events.return_value.get.return_value.execute.return_value = {"attendees": existing}
    patch = fake.events.return_value.patch
    patch.return_value.execute.return_value = {"id": "evt1"}
    return fake, patch


async def test_adds_the_attendee_and_asks_google_to_notify(monkeypatch):
    fake, patch = _client([])
    monkeypatch.setattr(CalendarService, "_client", lambda self: fake)
    await CalendarService().add_attendee("evt1", "p@x.com")
    kwargs = patch.call_args.kwargs
    assert kwargs["eventId"] == "evt1"
    assert kwargs["sendUpdates"] == "all"
    assert kwargs["body"] == {"attendees": [{"email": "p@x.com"}]}


async def test_does_not_add_the_same_attendee_twice(monkeypatch):
    fake, patch = _client([{"email": "P@x.com"}])
    monkeypatch.setattr(CalendarService, "_client", lambda self: fake)
    await CalendarService().add_attendee("evt1", "p@x.com")
    patch.assert_not_called()


async def test_preserves_existing_attendee_fields(monkeypatch):
    existing = {"email": "doctor@clinic.example", "responseStatus": "accepted", "optional": True}
    fake, patch = _client([existing])
    monkeypatch.setattr(CalendarService, "_client", lambda self: fake)
    await CalendarService().add_attendee("evt1", "p@x.com")
    assert patch.call_args.kwargs["body"]["attendees"] == [existing, {"email": "p@x.com"}]
