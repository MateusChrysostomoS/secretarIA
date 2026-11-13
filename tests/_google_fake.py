"""A small Google Calendar stand-in for the real `CalendarService` (TASK-083 Task 5).

Not a CalendarService double: it replaces only `CalendarService._build_service`, so every
real method (`check_availability`, `create_event`, `update_event_details`, the exclusive
write, ...) runs end to end against it. One calendar per refresh token ("primary" is that
token's calendar), like two clinics that connected two different Google accounts. Events
are stored with RFC 3339 strings, exactly the shape `events.list` returns. No network.
"""

import threading
from datetime import datetime
from types import SimpleNamespace

from googleapiclient.errors import HttpError

from secretaria.services.calendar import CalendarService


def _http_error(status: int) -> HttpError:
    return HttpError(resp=SimpleNamespace(status=status, reason="fake"), content=b"{}")


class _Request:
    def __init__(self, run) -> None:
        self._run = run

    def execute(self):
        return self._run()


class GoogleFake:
    """Per-token calendars. `inserts`/`patches` record (token, event_id) in call order."""

    def __init__(self) -> None:
        self.calendars: dict[str, dict[str, dict]] = {}
        self.inserts: list[tuple[str, str]] = []
        self.patches: list[tuple[str, str]] = []
        # The socket timeout each built client got (None: the library default).
        self.timeouts: list[float | None] = []
        # Called (in the worker thread) right before an insert/patch lands; may raise.
        self.on_write = None
        # One-shot error for the next insert/patch (e.g. a 503).
        self.fail_next_write: Exception | None = None
        self._lock = threading.Lock()
        self._seq = 0

    def events_of(self, token: str) -> list[dict]:
        return list(self.calendars.get(token, {}).values())

    def put(self, token: str, start: datetime, end: datetime, *, event_id: str | None = None):
        """An event typed straight into that Google calendar (not by the secretarIA)."""
        with self._lock:
            self._seq += 1
            event_id = event_id or f"gput{self._seq:04d}"
            self.calendars.setdefault(token, {})[event_id] = {
                "id": event_id,
                "summary": "Pessoal",
                "status": "confirmed",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
            }
        return event_id

    def _before_write(self) -> None:
        if self.on_write is not None:
            self.on_write()
        if self.fail_next_write is not None:
            error, self.fail_next_write = self.fail_next_write, None
            raise error

    def client(self, token: str):
        fake = self
        calendar = self.calendars.setdefault(token, {})

        class _Events:
            def list(self, calendarId, timeMin, timeMax, **_kw):  # noqa: N803
                def run():
                    lo, hi = datetime.fromisoformat(timeMin), datetime.fromisoformat(timeMax)
                    with fake._lock:
                        items = [
                            dict(e)
                            for e in calendar.values()
                            if datetime.fromisoformat(e["start"]["dateTime"]) < hi
                            and datetime.fromisoformat(e["end"]["dateTime"]) > lo
                        ]
                    items.sort(key=lambda e: datetime.fromisoformat(e["start"]["dateTime"]))
                    return {"items": items}

                return _Request(run)

            def insert(self, calendarId, body):  # noqa: N803
                def run():
                    fake._before_write()
                    with fake._lock:
                        event_id = body.get("id")
                        if event_id is None:
                            fake._seq += 1
                            event_id = f"gevt{fake._seq:04d}"
                        elif event_id in calendar:
                            raise _http_error(409)
                        calendar[event_id] = {
                            "id": event_id,
                            "summary": body.get("summary"),
                            "status": "confirmed",
                            "start": {"dateTime": body["start"]["dateTime"]},
                            "end": {"dateTime": body["end"]["dateTime"]},
                        }
                        fake.inserts.append((token, event_id))
                        return {**calendar[event_id], "htmlLink": f"https://g/{event_id}"}

                return _Request(run)

            def get(self, calendarId, eventId):  # noqa: N803
                def run():
                    with fake._lock:
                        if eventId not in calendar:
                            raise _http_error(404)
                        return dict(calendar[eventId])

                return _Request(run)

            def patch(self, calendarId, eventId, body, **_kw):  # noqa: N803
                def run():
                    fake._before_write()
                    with fake._lock:
                        if eventId not in calendar:
                            raise _http_error(404)
                        event = calendar[eventId]
                        if "start" in body:
                            event["start"] = {"dateTime": body["start"]["dateTime"]}
                            event["end"] = {"dateTime": body["end"]["dateTime"]}
                        if "summary" in body:
                            event["summary"] = body["summary"]
                        fake.patches.append((token, eventId))
                        return dict(event)

                return _Request(run)

            def delete(self, calendarId, eventId):  # noqa: N803
                def run():
                    with fake._lock:
                        if eventId not in calendar:
                            raise _http_error(404)
                        del calendar[eventId]

                return _Request(run)

        return SimpleNamespace(events=lambda: _Events())


def install_google_fake(monkeypatch) -> GoogleFake:
    """Route every real CalendarService's Google client to one shared `GoogleFake`."""
    fake = GoogleFake()

    def _build(self, *, timeout=None):
        fake.timeouts.append(timeout)
        return fake.client(self._refresh_token_override)

    monkeypatch.setattr(CalendarService, "_build_service", _build)
    return fake
