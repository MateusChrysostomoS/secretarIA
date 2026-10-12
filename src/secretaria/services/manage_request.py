"""The AI's request about an EXISTING appointment (v2) - TASK-030 P3, spec §4.5.

`manage_existing_appointment` (ai/tools.py) hands a reschedule or a cancellation back to
the deterministic manage flow. v1 carried only the action; v2 may also name WHICH of the
patient's upcoming appointments - by its start, exactly as the appointment context block
shows it to the model (`APPOINTMENT_REF_FORMAT`, the "(ref ...)" written by
workers/shared/llm_context.py::_appointment_context_text) - and, for a reschedule, the
new day and time. The worker (workers/shared/sentinels.py::_handle_manage_appointment)
lands it on the SAME steps the buttons use, never further than a confirmation card.

Wire format (after `__MANAGE_APPOINTMENT__:`, ai/graph.py): the bare action
("reschedule" / "cancel") when nothing else is said - byte for byte the v1 sentinel - and
compact JSON `{"a", "ap", "d", "h"}` otherwise. Produced and consumed in the same worker
turn; the parser accepts both.

The appointment is only ever looked for AMONG THE CONVERSATION'S OWN PATIENT'S upcoming
appointments (the caller loads `load_upcoming_appointments(tenant, patient)`): a reference
that matches none of them, or two of them, is dropped - never replaced by a guess.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from secretaria.models import FlowState
from secretaria.services import flow_router as fr
from secretaria.services.availability import free_slots_for_day
from secretaria.services.booking_draft import (
    DRAFT_DAY_OUT_OF_WINDOW_PREFIX,
    DRAFT_DAY_UNAVAILABLE_PREFIX,
    DROP_DAY_UNAVAILABLE,
    DROP_MISSING_DAY,
    DROP_NO_FREE_SLOT,
    DROP_OUT_OF_WINDOW,
    FALLBACK_CALENDAR_UNAVAILABLE,
    FALLBACK_NO_FREE_DAYS,
    landing_step,
)
from secretaria.services.booking_hold import BookingGate
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.flow_router import FlowRouterResult
from secretaria.services.patient_context import as_utc

if TYPE_CHECKING:
    from secretaria.services.calendar import CalendarService

ACTION_RESCHEDULE = "reschedule"
ACTION_CANCEL = "cancel"
ACTIONS = (ACTION_RESCHEDULE, ACTION_CANCEL)

# Field NAMES, as the tool spells them and as the hand-back event logs them
# (workers/shared/handback_log.py FIELD_*). Never values.
FIELD_ACTION = "action"
FIELD_APPOINTMENT = "appointment"
FIELD_DAY = "day"
FIELD_TIME = "time"
FIELD_NAMES = (FIELD_ACTION, FIELD_APPOINTMENT, FIELD_DAY, FIELD_TIME)

# How an upcoming appointment is named to the model and how the model names it back: its
# start, to the minute, in the clinic's timezone.
APPOINTMENT_REF_FORMAT = "%Y-%m-%d %H:%M"
_REF = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_HHMM = re.compile(r"\d{2}:\d{2}")


def parse_appointment_ref(text: str) -> dt.datetime:
    """ "AAAA-MM-DD HH:MM" ("T" also accepted) as a NAIVE clinic-local datetime.

    ValueError on anything else.
    """
    match = _REF.fullmatch((text or "").strip())
    if match is None:
        raise ValueError("appointment reference must be YYYY-MM-DD HH:MM")
    return dt.datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}")


def appointment_ref(start_at: dt.datetime, tz: dt.tzinfo) -> str:
    """The reference the model sees for an appointment that starts at `start_at`."""
    return as_utc(start_at).astimezone(tz).strftime(APPOINTMENT_REF_FORMAT)


def _optional_text(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"manage payload key {key!r} must be a string")
    return value.strip() or None


@dataclass(frozen=True)
class ManageRequest:
    """What the AI says about an existing appointment. Only `action` is required."""

    action: str
    appointment: dt.datetime | None = None  # naive, clinic-local, to the minute
    day: dt.date | None = None
    time: dt.time | None = None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError("manage action must be 'reschedule' or 'cancel'")

    def to_payload(self) -> str:
        if self.appointment is None and self.day is None and self.time is None:
            return self.action
        return json.dumps(
            {
                "a": self.action,
                "ap": (
                    self.appointment.strftime(APPOINTMENT_REF_FORMAT)
                    if self.appointment is not None
                    else None
                ),
                "d": self.day.isoformat() if self.day is not None else None,
                "h": self.time.strftime("%H:%M") if self.time is not None else None,
            }
        )

    @classmethod
    def from_payload(cls, raw: str) -> ManageRequest:
        """The bare v1 action or the v2 JSON. ValueError on anything malformed."""
        text = (raw or "").strip()
        if text in ACTIONS:
            return cls(action=text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("manage payload is neither an action nor JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("manage payload must be a JSON object")
        reference = _optional_text(data, "ap")
        day = _optional_text(data, "d")
        hhmm = _optional_text(data, "h")
        if day is not None and not _ISO_DAY.fullmatch(day):
            raise ValueError("manage payload 'd' must be YYYY-MM-DD")
        if hhmm is not None and not _HHMM.fullmatch(hhmm):
            raise ValueError("manage payload 'h' must be HH:MM")
        return cls(
            action=str(data.get("a")),
            appointment=parse_appointment_ref(reference) if reference is not None else None,
            day=dt.date.fromisoformat(day) if day is not None else None,
            time=dt.time.fromisoformat(hhmm) if hhmm is not None else None,
        )

    def supplied_fields(self) -> tuple[str, ...]:
        """The field NAMES present, in `FIELD_NAMES` order (what the hand-back logs)."""
        values = (self.action, self.appointment, self.day, self.time)
        return tuple(
            name for name, value in zip(FIELD_NAMES, values, strict=True) if value is not None
        )


def find_appointment(
    appointments: Sequence[dict], ref: dt.datetime | None, tz: dt.tzinfo
) -> dict | None:
    """The ONE appointment in `appointments` starting at `ref` (clinic-local minute).

    None when `ref` is None, when nothing starts then, and when two do - ambiguous is never
    guessed.
    """
    if ref is None:
        return None
    wanted = ref.strftime(APPOINTMENT_REF_FORMAT)
    matches = [
        appointment
        for appointment in appointments
        if isinstance(appointment.get("start_at"), dt.datetime)
        and appointment_ref(appointment["start_at"], tz) == wanted
    ]
    return matches[0] if len(matches) == 1 else None


def manage_target(
    request: ManageRequest, appointments: Sequence[dict], tz: dt.tzinfo
) -> dict | None:
    """WHICH appointment the request is about, or None (the buttons' pick list decides).

    The one it names; for a reschedule that names none, the patient's only one. A reference
    that names nothing in `appointments` is NOT replaced by a guess, even when the patient
    has a single appointment. A cancel without a reference is left to
    `flow_router.enter_manage_action`, exactly like the "Cancelar" button.
    """
    if request.appointment is not None:
        return find_appointment(appointments, request.appointment, tz)
    if request.action == ACTION_RESCHEDULE and len(appointments) == 1:
        return appointments[0]
    return None


# ---------------------------------------------------------------------------
# Where a manage request lands (spec §4.5)
# ---------------------------------------------------------------------------

# Why a supplied item did not survive - same strings as workers/shared/handback_log.py
# DROP_* (the day/time ones are the booking resolver's).
DROP_UNKNOWN_APPOINTMENT = "unknown_appointment"
DROP_APPOINTMENT_NOT_CHOSEN = "appointment_not_chosen"
DROP_REASONS = (
    DROP_UNKNOWN_APPOINTMENT,
    DROP_APPOINTMENT_NOT_CHOSEN,
    DROP_OUT_OF_WINDOW,
    DROP_DAY_UNAVAILABLE,
    DROP_NO_FREE_SLOT,
    DROP_MISSING_DAY,
)
FALLBACK_NO_APPOINTMENTS = "no_appointments"
FALLBACK_AMBIGUOUS_APPOINTMENT = "ambiguous_appointment"
FALLBACK_REASONS = (
    FALLBACK_NO_APPOINTMENTS,
    FALLBACK_AMBIGUOUS_APPOINTMENT,
    FALLBACK_CALENDAR_UNAVAILABLE,
    FALLBACK_NO_FREE_DAYS,
)


@dataclass
class ManageResolution:
    """Where a manage request landed and what happened to each supplied item."""

    result: FlowRouterResult
    landing_step: str
    accepted: tuple[str, ...] = ()
    dropped: dict[str, str] = field(default_factory=dict)
    fallback: str | None = None


def _fallback(result: FlowRouterResult, appointments: Sequence[dict]) -> str | None:
    if result.action == "handover":
        return FALLBACK_AMBIGUOUS_APPOINTMENT
    if not appointments:
        return FALLBACK_NO_APPOINTMENTS
    if result.action == "calendar_unavailable":
        return FALLBACK_CALENDAR_UNAVAILABLE
    if result.flow_state is FlowState.MENU:
        # The only menu past the appointment check: the day picker found no free day.
        return FALLBACK_NO_FREE_DAYS
    return None


def _owner_id(appointment: dict) -> UUID | None:
    raw = appointment.get("professional_id")
    try:
        return UUID(str(raw)) if raw else None
    except ValueError:
        return None


def _specific_pick_result(
    result: FlowRouterResult, appointments: list[dict], professionals: list | None, tenant: Any
) -> FlowRouterResult:
    """v2 lists distinguish appointments by scoped IDs and visible service/doctor data.

    Indistinguishable choices go to the existing human handover instead of inviting a
    blind pick. No attendee data is loaded into the AI's context for disambiguation.
    """
    if result.flow_step not in (fr.STEP_MANAGE_PICK_CANCEL, fr.STEP_MANAGE_PICK_RESCHEDULE):
        return result
    bubble = result.bubbles[0]
    valid = [a for a in appointments if isinstance(a.get("start_at"), dt.datetime)]
    rows = []
    visible = set()
    for appointment in valid[: fr.MAX_MANAGE_APPOINTMENT_ROWS]:
        identifier = fr._appt_uuid(appointment)
        title = fr._appt_row_label(appointment, tenant)
        doctor = next(
            (
                p
                for p in (professionals or [])
                if str(p.id) == str(appointment.get("professional_id"))
            ),
            None,
        )
        service = str(appointment.get("appointment_type") or "Consulta")
        description = f"{doctor.name} — {service}" if doctor is not None else service
        description = fr.truncate_plain(description, fr.MAX_LIST_ROW_DESCRIPTION_CHARS)
        identity = (title, description)
        if identifier is None or identity in visible:
            return FlowRouterResult(
                action="handover",
                bubbles=[
                    fr.TextBubble(
                        body=(
                            "Há consultas parecidas nesse horário. Vou chamar a equipe para ajudar "
                            "você a escolher a consulta certa."
                        )
                    )
                ],
            )
        visible.add(identity)
        rows.append(
            (f"slot|{fr.MANAGE_APPOINTMENT_PAYLOAD_PREFIX}{identifier}", title, description)
        )
    bubble.rows = rows
    return result


async def resolve_manage_request(
    request: ManageRequest,
    *,
    tenant: Any,
    appointments: Sequence[dict],
    professionals: list | None,
    calendar: CalendarService | None,
    conversation_id: Any,
    tz: dt.tzinfo,
    now: dt.datetime,
) -> ManageResolution:
    """Land an AI manage request on the steps the buttons use - never further than a card.

    Cancel: "Confirmar o cancelamento?" for the named appointment (or the buttons' own
    0/1/2+ handling); the cancellation itself is only ever the patient's "Sim". Reschedule
    of a known appointment: no day -> the reschedule day picker; a day outside the window or
    without free time -> the day picker with the reason line; a time free on the OWNING
    agenda (`calendar`, fresh) minus held slots -> the "Remarcar para:" card; else that day's
    slot list. A reschedule whose appointment is not known (two of them and none named, or
    a reference that matches none) shows the buttons' pick list and drops the day/time.

    `appointments` are THIS conversation's patient's upcoming appointments - nothing outside
    them can ever be targeted. The deposit/reschedule-limit pre-check is the worker's
    (workers/shared/deposit.py); it runs before this. Runs inside a booking-gate scope so
    held slots count as busy: the caller's gate when published, else an unarmed one.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    gate = fr._ACTIVE_GATE.get()
    if gate is None:
        gate = BookingGate(
            tenant_id=getattr(tenant, "id", None),
            conversation_id=conversation_id,
            patient_id=None,
            external_id=None,
            armed=False,
        )
    with fr.booking_gate_scope(gate):
        return await _resolve_manage(
            request,
            tenant=tenant,
            appointments=list(appointments),
            professionals=professionals,
            calendar=calendar,
            tz=tz,
            now=now,
        )


async def _resolve_manage(
    request: ManageRequest,
    *,
    tenant: Any,
    appointments: list[dict],
    professionals: list | None,
    calendar: CalendarService | None,
    tz: dt.tzinfo,
    now: dt.datetime,
) -> ManageResolution:
    dropped: dict[str, str] = {}
    target = manage_target(request, appointments, tz)
    if request.appointment is not None and target is None:
        dropped[FIELD_APPOINTMENT] = DROP_UNKNOWN_APPOINTMENT
    if request.time is not None and request.day is None:
        dropped[FIELD_TIME] = DROP_MISSING_DAY

    def done(result: FlowRouterResult, *names: str) -> ManageResolution:
        return ManageResolution(
            result=result,
            landing_step="human_handover" if result.action == "handover" else landing_step(result),
            accepted=tuple(
                name for name in request.supplied_fields() if name in names and name not in dropped
            ),
            dropped=dict(dropped),
            fallback=_fallback(result, appointments),
        )

    if request.appointment is not None and target is None and appointments:
        # A supplied reference that does not resolve must not use the buttons' sole-
        # appointment shortcut: the patient must choose, even with only one option.
        for name in (FIELD_DAY, FIELD_TIME):
            if getattr(request, name) is not None:
                dropped.setdefault(name, DROP_APPOINTMENT_NOT_CHOSEN)
        cancelling = request.action == ACTION_CANCEL
        prompt = (
            "Qual consulta você quer cancelar?"
            if cancelling
            else "Qual consulta você quer remarcar?"
        )
        result = FlowRouterResult(
            action="reply",
            bubbles=[fr._manage_pick_list_bubble(appointments, prompt, tenant=tenant)],
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=fr.STEP_MANAGE_PICK_CANCEL if cancelling else fr.STEP_MANAGE_PICK_RESCHEDULE,
        )
        return done(
            _specific_pick_result(result, appointments, professionals, tenant), FIELD_ACTION
        )

    target_id = fr._appt_uuid(target) if target is not None else None
    if request.action == ACTION_CANCEL:
        result = await fr.enter_manage_action(
            "cancel", tenant, appointments, professionals, preselected_id=target_id
        )
        return done(
            _specific_pick_result(result, appointments, professionals, tenant),
            FIELD_ACTION,
            FIELD_APPOINTMENT,
        )
    if target is None:
        for name in (FIELD_DAY, FIELD_TIME):
            if getattr(request, name) is not None:
                dropped.setdefault(name, DROP_APPOINTMENT_NOT_CHOSEN)
        result = await fr.enter_manage_action(
            "reschedule", tenant, appointments, professionals, calendar=calendar
        )
        return done(
            _specific_pick_result(result, appointments, professionals, tenant), FIELD_ACTION
        )
    if request.day is None:
        result = await fr._begin_reschedule(target_id, target, tenant, calendar, professionals)
        return done(result, FIELD_ACTION, FIELD_APPOINTMENT)
    return await _land_new_day(
        request,
        target,
        tenant=tenant,
        professionals=professionals,
        calendar=calendar,
        now=now,
        dropped=dropped,
        done=done,
    )


async def _land_new_day(
    request: ManageRequest,
    target: dict,
    *,
    tenant: Any,
    professionals: list | None,
    calendar: CalendarService | None,
    now: dt.datetime,
    dropped: dict[str, str],
    done: Callable[..., ManageResolution],
) -> ManageResolution:
    """The new day, then the new time - re-derived from the OWNING agenda minus holds."""
    managing_id = fr._appt_uuid(target)
    # `flow_selected_professional_id` here is only what `_hold_owner` reads: the holds are
    # looked up on the appointment's own agenda. The manage branch never persists it.
    state = fr._DayPickerState(
        flow_managing_appointment_id=managing_id,
        flow_selected_professional_id=_owner_id(target),
    )
    duration = fr._appt_duration_minutes(target, tenant)
    base = (FIELD_ACTION, FIELD_APPOINTMENT)

    async def day_picker(prefix: str) -> ManageResolution:
        result = await fr.enter_day_picker(
            state,
            tenant,
            calendar,
            duration_minutes=duration,
            branch=fr.MANAGE_DAY_BRANCH,
            prefix=prefix,
            professionals=professionals,
        )
        return done(result, *base)

    def dropped_day(reason: str) -> None:
        dropped[FIELD_DAY] = reason
        if request.time is not None:
            dropped[FIELD_TIME] = DROP_MISSING_DAY

    if calendar is None:
        return done(
            fr._calendar_unavailable(state, fr.MANAGE_DAY_BRANCH, fr.STEP_MANAGE_DAY), *base
        )
    today = now.astimezone(calendar.tzinfo).date()
    if not today <= request.day < today + dt.timedelta(days=fr.DAY_PICKER_WINDOW_DAYS):
        dropped_day(DROP_OUT_OF_WINDOW)
        return await day_picker(DRAFT_DAY_OUT_OF_WINDOW_PREFIX)
    holds = await fr._hold_windows(fr._hold_owner(state, professionals))
    try:
        free = await free_slots_for_day(
            calendar, day=request.day, duration_minutes=duration, holds=holds
        )
    except CalendarUnavailableError:
        return done(
            fr._calendar_unavailable(state, fr.MANAGE_DAY_BRANCH, fr.STEP_MANAGE_DAY), *base
        )
    if not free:
        dropped_day(DROP_DAY_UNAVAILABLE)
        return await day_picker(DRAFT_DAY_UNAVAILABLE_PREFIX)

    names = (*base, FIELD_DAY)
    if request.time is not None:
        match = next(
            (slot for slot in free if slot.time().replace(second=0, microsecond=0) == request.time),
            None,
        )
        if match is None:
            dropped[FIELD_TIME] = DROP_NO_FREE_SLOT
        else:
            card = fr._manage_confirm_result(managing_id, target, match, request.day.isoformat())
            return done(card, *names, FIELD_TIME)
    target_day = dt.datetime(request.day.year, request.day.month, request.day.day)
    result = await fr._enter_slot_picker(
        state,
        tenant,
        calendar,
        target_day,
        duration_minutes=duration,
        branch=fr.MANAGE_DAY_BRANCH,
        professionals=professionals,
    )
    return done(result, *names)
