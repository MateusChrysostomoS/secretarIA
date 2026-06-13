"""Detection-layer contract: every "Google Calendar is broken" mode funnels into
a single CalendarUnavailableError, and ordinary client errors do NOT.

This is the test that lets us *affirm* the outage automation (patient notice +
human handover + owner alert email, see workers/tasks.py:_handle_calendar_unavailable)
catches the various ways the calendar can break — because all of them are
normalized to one exception at one layer. The four breakage modes:

  1. Token revoked / expired / invalid   -> RefreshError at auth refresh
  2. Auth expired / forbidden on a call  -> HTTP 401 / 403
  3. Google-side outage                  -> HTTP 5xx
  4. Network / DNS / timeout             -> OSError / TransportError / ServerNotFoundError

And the deliberate non-triggers (caller/logic errors, handled by the caller):
404 / 409 / 400. If any of these started raising CalendarUnavailableError we'd
hand off perfectly healthy conversations to a human on a plain "event not found".
"""

import os
from datetime import datetime
from types import SimpleNamespace

# Match the env conftest sets so secretaria imports succeed.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402
from google.auth.exceptions import RefreshError, TransportError  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402
from httplib2 import ServerNotFoundError  # noqa: E402

from secretaria.services import calendar as calendar_mod  # noqa: E402
from secretaria.services.calendar import (  # noqa: E402
    CalendarService,
    CalendarUnavailableError,
    _raise_if_unavailable,
)

# Statuses that mean "the calendar is unavailable", not "your request was wrong".
UNAVAILABLE_STATUSES = [401, 403, 500, 502, 503, 504]
# Client/logic errors the caller must keep handling itself (never an outage).
CLIENT_ERROR_STATUSES = [400, 404, 409]


def _http_error(status: int) -> HttpError:
    """A googleapiclient HttpError carrying `status`, as the Google client raises."""
    resp = SimpleNamespace(status=status, reason="test")
    return HttpError(resp=resp, content=b"{}")


def _stub_settings() -> SimpleNamespace:
    """Minimal Settings duck-type: enough for CalendarService.__init__ + _build_service."""
    return SimpleNamespace(
        CLINIC_TIMEZONE="America/Sao_Paulo",
        GOOGLE_CALENDAR_ID="primary",
        GOOGLE_CLIENT_ID="client-id",
        GOOGLE_CLIENT_SECRET="client-secret",
        GOOGLE_REFRESH_TOKEN="refresh-token",
    )


# ---------------------------------------------------------------------------
# Mode 2 + 3 — HTTP status mapping (_raise_if_unavailable), all at once.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", UNAVAILABLE_STATUSES)
def test_unavailable_http_status_maps_to_outage(status: int) -> None:
    with pytest.raises(CalendarUnavailableError) as exc_info:
        _raise_if_unavailable(_http_error(status))
    # The sanitized detail names the status so the admin probe can show *why*.
    assert str(status) in str(exc_info.value)


@pytest.mark.parametrize("status", CLIENT_ERROR_STATUSES)
def test_client_error_status_is_not_an_outage(status: int) -> None:
    # Returns normally so the caller can handle/re-raise the 4xx as before.
    _raise_if_unavailable(_http_error(status))


# ---------------------------------------------------------------------------
# Mode 1 + 4 (at auth) — refresh failures in _build_service.
# ---------------------------------------------------------------------------


def _service_whose_refresh_raises(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> CalendarService:
    """A CalendarService whose credential refresh raises `exc` at _build_service."""

    class _FakeCredentials:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def refresh(self, request: object) -> None:
            raise exc

    monkeypatch.setattr(calendar_mod, "Credentials", _FakeCredentials)
    monkeypatch.setattr(calendar_mod, "GoogleRequest", lambda: None)
    return CalendarService(settings=_stub_settings())


def test_invalid_token_maps_to_refresh_token_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mode 1: this is exactly what scripts/break_calendar.py provokes in prod.
    service = _service_whose_refresh_raises(monkeypatch, RefreshError("invalid_grant"))
    with pytest.raises(CalendarUnavailableError) as exc_info:
        service._build_service()
    assert "refresh token rejected" in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "network_exc",
    [
        OSError("connection reset"),
        TransportError("tls handshake failed"),
        ServerNotFoundError("Unable to find the server"),
    ],
)
def test_network_failure_at_auth_maps_to_unreachable(
    monkeypatch: pytest.MonkeyPatch, network_exc: BaseException
) -> None:
    # Mode 4, surfaced while exchanging the refresh token (before any API call).
    service = _service_whose_refresh_raises(monkeypatch, network_exc)
    with pytest.raises(CalendarUnavailableError) as exc_info:
        service._build_service()
    assert "unreachable" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Mode 4 (on a live call) — network failure inside an events.* request.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "network_exc",
    [OSError("timed out"), TransportError("reset"), ServerNotFoundError("no dns")],
)
async def test_network_failure_during_call_maps_to_unreachable(
    monkeypatch: pytest.MonkeyPatch, network_exc: BaseException
) -> None:
    service = CalendarService(settings=_stub_settings())

    class _Request:
        def execute(self) -> object:
            raise network_exc

    class _Events:
        def list(self, **kwargs: object) -> _Request:
            return _Request()

    class _FakeGoogleService:
        def events(self) -> _Events:
            return _Events()

    # Pre-seed the built client so check_availability skips _build_service.
    monkeypatch.setattr(service, "_service", _FakeGoogleService())

    now = datetime(2026, 1, 1, 12, 0)
    with pytest.raises(CalendarUnavailableError) as exc_info:
        await service.check_availability(now, now)
    assert "unreachable" in str(exc_info.value).lower()
