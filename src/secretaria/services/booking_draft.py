"""The AI's booking draft (v2) and the ONE resolver that decides where it lands.

`set_booking_draft` (ai/tools.py) tells the workflow what the patient already said -
service, doctor, convênio, who the booking is for, day and time - and the worker decides
where the patient lands (`resolve_booking_draft`, below). The AI never writes `flow_*` and
never carries a third party's name: `attendee` is only "self"/"other" (skill
pii-field-capture-pseudonymization); the name is captured by the deterministic attendee
step, so the pseudonymizer registers it before any model sees it.

Wire format (the `__BOOKING_DRAFT__:` sentinel, ai/graph.py): compact JSON with keys `t`
(service), `p` (professional id), `i` (convênio), `w` ("self" | "other" | null), `d`
(YYYY-MM-DD, clinic timezone) and `h` (HH:MM); null for absent. The sentinel is produced
and consumed in the same worker, in the same turn - there is no cross-service contract and
no version mix - but the parser still accepts the v1 payload (`t`/`p`/`i` only), where a
missing `w` means "unknown".

The same keys plus `saved_at` are what `Conversation.flow_draft` stores while the patient
answers "Essa consulta é pra você?" (spec §4.3); `draft_from_record` drops a record older
than `flow_router.FLOW_DRAFT_TTL_MINUTES`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from secretaria.services import flow_router as fr

DRAFT_ATTENDEE_SELF = "self"
DRAFT_ATTENDEE_OTHER = "other"

# Field NAMES, as the AI tool spells them and as the hand-back event logs them
# (workers/shared/handback_log.py FIELD_*). Never values.
FIELD_SERVICE = "service"
FIELD_PROFESSIONAL = "professional"
FIELD_INSURANCE = "insurance"
FIELD_FOR_WHOM = "for_whom"
FIELD_DAY = "day"
FIELD_TIME = "time"
FIELD_NAMES = (
    FIELD_SERVICE,
    FIELD_PROFESSIONAL,
    FIELD_INSURANCE,
    FIELD_FOR_WHOM,
    FIELD_DAY,
    FIELD_TIME,
)

DRAFT_RECORD_SAVED_AT = "saved_at"

_TEXT_MAX = 120
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_HHMM = re.compile(r"\d{2}:\d{2}")


def _text(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"booking draft key {key!r} must be a string")
    return value.strip()[:_TEXT_MAX] or None


@dataclass(frozen=True)
class BookingDraft:
    """What the AI says the patient asked for. Every field optional; None = not said."""

    service: str | None = None
    professional_id: UUID | None = None
    insurance: str | None = None
    attendee: Literal["self", "other"] | None = None
    day: dt.date | None = None
    time: dt.time | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "t": self.service,
            "p": str(self.professional_id) if self.professional_id is not None else None,
            "i": self.insurance,
            "w": self.attendee,
            "d": self.day.isoformat() if self.day is not None else None,
            "h": self.time.strftime("%H:%M") if self.time is not None else None,
        }

    def to_payload(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Any) -> BookingDraft:
        """Parse the six keys (unknown keys ignored). ValueError on anything malformed."""
        if not isinstance(data, dict):
            raise ValueError("booking draft must be a JSON object")
        raw_professional = _text(data, "p")
        attendee = _text(data, "w")
        if attendee not in (None, DRAFT_ATTENDEE_SELF, DRAFT_ATTENDEE_OTHER):
            raise ValueError("booking draft 'w' must be 'self', 'other' or null")
        raw_day = _text(data, "d")
        if raw_day is not None and not _ISO_DAY.fullmatch(raw_day):
            raise ValueError("booking draft 'd' must be YYYY-MM-DD")
        raw_time = _text(data, "h")
        if raw_time is not None and not _HHMM.fullmatch(raw_time):
            raise ValueError("booking draft 'h' must be HH:MM")
        return cls(
            service=_text(data, "t"),
            professional_id=UUID(raw_professional) if raw_professional else None,
            insurance=_text(data, "i"),
            attendee=attendee,  # type: ignore[arg-type]
            day=dt.date.fromisoformat(raw_day) if raw_day else None,
            time=dt.time.fromisoformat(raw_time) if raw_time else None,
        )

    @classmethod
    def from_payload(cls, raw: str) -> BookingDraft:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("booking draft is not JSON") from exc
        return cls.from_dict(data)

    def supplied_fields(self) -> tuple[str, ...]:
        """The field NAMES present, in `FIELD_NAMES` order (what the hand-back logs)."""
        values = (
            self.service,
            self.professional_id,
            self.insurance,
            self.attendee,
            self.day,
            self.time,
        )
        return tuple(
            name for name, value in zip(FIELD_NAMES, values, strict=True) if value is not None
        )


def draft_record(draft: BookingDraft, *, saved_at: dt.datetime) -> dict[str, str | None]:
    """The `Conversation.flow_draft` value: the wire keys plus an aware UTC `saved_at`."""
    if saved_at.tzinfo is None:
        raise ValueError("saved_at must be timezone-aware")
    return {**draft.to_dict(), DRAFT_RECORD_SAVED_AT: saved_at.astimezone(dt.UTC).isoformat()}


def draft_from_record(record: Any, *, now: dt.datetime) -> BookingDraft | None:
    """The parked draft, or None when absent, corrupt or older than the TTL.

    `now` must be timezone-aware. A naive `saved_at` (a hand-edited row) is read as UTC.
    """
    if not isinstance(record, dict):
        return None
    try:
        draft = BookingDraft.from_dict(record)
        saved_at = dt.datetime.fromisoformat(str(record[DRAFT_RECORD_SAVED_AT]))
    except (KeyError, ValueError):
        return None
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=dt.UTC)
    if now - saved_at > dt.timedelta(minutes=fr.FLOW_DRAFT_TTL_MINUTES):
        return None
    return draft
