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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from secretaria.ai.formatter import TextBubble
from secretaria.models import FlowState
from secretaria.services import flow_router as fr, flow_units
from secretaria.services.attendee import ATTENDEE_SELF, real_attendee_name
from secretaria.services.availability import (
    booking_notice_floor,
    booking_notice_hours,
    free_slots_for_day,
)
from secretaria.services.booking_details import booking_details_text
from secretaria.services.booking_hold import BookingGate
from secretaria.services.booking_scope import sole_active_professional
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.flow_router import FlowRouterResult
from secretaria.services.insurance_acceptance import (
    has_exceptions,
    is_accepted,
    log_refusal,
    refuses_everywhere,
    snapshot_exceptions,
)
from secretaria.services.service_catalog import normalize as normalize_service_name
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
)

if TYPE_CHECKING:
    from secretaria.services.calendar import CalendarService

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
    professional_unresolved: bool = False

    def has_capture_fields(self) -> bool:
        """Explicit preferences absent from the historical three-field request."""
        return (
            self.attendee is not None or self.day is not None
            or self.time is not None or self.professional_unresolved
        )

    def to_dict(self) -> dict[str, str | bool | None]:
        data: dict[str, str | bool | None] = {
            "t": self.service,
            "p": str(self.professional_id) if self.professional_id is not None else None,
            "i": self.insurance,
            "w": self.attendee,
            "d": self.day.isoformat() if self.day is not None else None,
            "h": self.time.strftime("%H:%M") if self.time is not None else None,
        }
        # No raw doctor name on the wire. The marker distinguishes an explicitly
        # rejected choice from an omitted choice; normal six-key payloads stay unchanged.
        if self.professional_unresolved:
            data["p_unresolved"] = True
        return data

    def to_payload(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Any) -> BookingDraft:
        """Parse the six keys (unknown keys ignored). ValueError on anything malformed."""
        if not isinstance(data, dict):
            raise ValueError("booking draft must be a JSON object")
        raw_professional = _text(data, "p")
        unresolved = data.get("p_unresolved", False)
        if not isinstance(unresolved, bool) or (unresolved and raw_professional is not None):
            raise ValueError("booking draft professional choice is inconsistent")
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
            professional_unresolved=unresolved,
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
            name for name, value in zip(FIELD_NAMES, values, strict=True)
            if value is not None or (name == FIELD_PROFESSIONAL and self.professional_unresolved)
        )


def draft_record(draft: BookingDraft, *, saved_at: dt.datetime) -> dict[str, str | bool | None]:
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


# ---------------------------------------------------------------------------
# The resolver (spec §4.2): one place decides where the draft lands
# ---------------------------------------------------------------------------

# Why a supplied item did not survive. Same strings as workers/shared/handback_log.py
# DROP_* (the first three are P1's; P2b adds the rest to that vocabulary).
DROP_NOT_IN_CATALOG = "not_in_catalog"
DROP_UNKNOWN_PROFESSIONAL = "unknown_professional"
DROP_UNMATCHED_PLAN = "unmatched_plan"
DROP_NOT_OFFERED_BY_PROFESSIONAL = "not_offered_by_professional"
DROP_OUT_OF_WINDOW = "out_of_window"
DROP_DAY_UNAVAILABLE = "day_unavailable"
DROP_NO_FREE_SLOT = "no_free_slot"
DROP_MISSING_DAY = "missing_day"
DROP_REASONS = (
    DROP_NOT_IN_CATALOG,
    DROP_UNKNOWN_PROFESSIONAL,
    DROP_UNMATCHED_PLAN,
    DROP_NOT_OFFERED_BY_PROFESSIONAL,
    DROP_OUT_OF_WINDOW,
    DROP_DAY_UNAVAILABLE,
    DROP_NO_FREE_SLOT,
    DROP_MISSING_DAY,
)

# Why the landing is not a step of the booking (handback_log.py FALLBACK_*). The menu is
# reserved for the spec's three cases: no bookable catalog, an unreachable agenda (which
# becomes today's hand-off to a human) and a corrupt payload (the worker's own fallback).
FALLBACK_NO_BOOKABLE_CATALOG = "no_bookable_catalog"
FALLBACK_CALENDAR_UNAVAILABLE = "calendar_unavailable"
FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE = "professional_config_incomplete"
FALLBACK_NO_FREE_DAYS = "no_free_days"
FALLBACK_REASONS = (
    FALLBACK_NO_BOOKABLE_CATALOG,
    FALLBACK_CALENDAR_UNAVAILABLE,
    FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE,
    FALLBACK_NO_FREE_DAYS,
)

# The one line the day picker opens with when the AI's day could not be kept, so the
# patient who said "quinta" knows why they are being asked again.
DRAFT_DAY_OUT_OF_WINDOW_PREFIX = "Não temos agenda aberta para esse dia."
DRAFT_DAY_UNAVAILABLE_PREFIX = "Esse dia não tem horário livre."
# TASK-086 A8: the line the convênio question opens with when the convênio matrix refuses the
# draft's (doctor, service, unit) - the patient who named a plan knows why it is asked again.
DRAFT_PLAN_NOT_ACCEPTED_PREFIX = "Esse convênio não é aceito para esse serviço."

# The agenda for the professional the resolver settled on (None = the tenant-level agenda,
# which on a single-professional clinic already carries that professional's credentials).
# Called at most once, and only when the landing needs a calendar.
CalendarSource = Callable[[Any | None], Awaitable["CalendarService | None"]]


@dataclass
class DraftResolution:
    """Where a draft landed and what happened to each supplied item on the way."""

    result: FlowRouterResult
    landing_step: str
    accepted: tuple[str, ...] = ()
    dropped: dict[str, str] = field(default_factory=dict)
    fallback: str | None = None


def landing_step(result: FlowRouterResult) -> str:
    """The log name of where `result` leaves the patient.

    Same rule as workers/shared/handback_log.py::landing_of (that module is the worker
    side and services cannot import it): a calendar outage is "human_handover", an
    unbookable doctor is "config_incomplete", anything else is the flow step or, without
    one, the lower-cased flow state.
    """
    if result.action == "calendar_unavailable":
        return "human_handover"
    if result.action == "professional_config_incomplete":
        return "config_incomplete"
    return result.flow_step or result.flow_state.value.lower()


def _result_fallback(result: FlowRouterResult) -> str | None:
    if result.action == "calendar_unavailable":
        return FALLBACK_CALENDAR_UNAVAILABLE
    if result.action == "professional_config_incomplete":
        return FALLBACK_PROFESSIONAL_CONFIG_INCOMPLETE
    if result.flow_state is FlowState.MENU:
        # The only menu a booking builder returns past the catalog check: the day picker
        # found no free day at all in the window.
        return FALLBACK_NO_FREE_DAYS
    return None


def _recorded_service(conversation: Any) -> str | None:
    """The service the conversation already recorded, never the pra-quem entry marker."""
    value = getattr(conversation, "flow_selected_type", None)
    if not value or value in (fr.ATTENDEE_NEXT_BOOK, fr.ATTENDEE_NEXT_CATALOG):
        return None
    return str(value)


def _insurance_view(tenant: Any, tenant_insurance: Any | None) -> Any:
    """What `match_insurance_plan` reads: the FRESH convênio catalog when one is given."""
    if tenant_insurance is None:
        return tenant
    return SimpleNamespace(
        insurance_plans=list(tenant_insurance.plans),
        insurances=getattr(tenant, "insurances", None),
    )


@dataclass
class _Checked:
    """Every draft item after the catalog-level checks (no calendar read yet)."""

    multi: bool
    clinic_catalog: list[dict]
    professional: Any | None
    service: dict | None
    insurance: str | None
    attendee_name: str | None  # None = pra-quem still unknown (or "other" without a name)
    needs_attendee_name: bool
    dropped: dict[str, str]


def _check(
    draft: BookingDraft,
    *,
    conversation: Any,
    tenant: Any,
    professionals: list,
    service_catalog: list | None,
    tenant_insurance: Any | None,
) -> _Checked:
    dropped: dict[str, str] = {}
    multi = fr._is_multi_professional(professionals)
    clinic_catalog = (
        fr._clinic_service_catalog(tenant, professionals, service_catalog)
        if multi
        else active_appointment_types(tenant)
    )

    def own(professional: Any) -> list[dict]:
        return professional_appointment_types(professional, tenant, service_catalog)

    # Profissional: on a multi-doctor clinic, the draft's doctor (or the recorded one);
    # otherwise implicit - the sole professional, or nobody on a tenant without any.
    if draft.professional_unresolved:
        professional = None
        dropped[FIELD_PROFESSIONAL] = DROP_UNKNOWN_PROFESSIONAL
    elif multi:
        if draft.professional_id is not None:
            professional = fr._find_professional_by_id(professionals, draft.professional_id)
            if professional is None:
                dropped[FIELD_PROFESSIONAL] = DROP_UNKNOWN_PROFESSIONAL
        else:
            professional = fr._find_professional_by_id(
                professionals, fr._selected_professional_id(conversation)
            )
            if (
                professional is not None
                and draft.service is not None
                and fr._match_service(own(professional), draft.service) is None
            ):
                # The newer statement wins: the doctor recorded earlier does not offer what
                # the patient asks for now, so the service picks the doctor below.
                professional = None
    else:
        professional = sole_active_professional(professionals)
        if draft.professional_id is not None and (
            getattr(professional, "id", None) != draft.professional_id
        ):
            dropped[FIELD_PROFESSIONAL] = DROP_UNKNOWN_PROFESSIONAL
            professional = None

    # Serviço: canonical in the catalog the booking will use. A recorded service that no
    # longer resolves is silently ignored - only a SUPPLIED item can be "dropped".
    from_draft = draft.service is not None
    text = draft.service if from_draft else _recorded_service(conversation)
    service: dict | None = None
    if text is not None:
        if multi and professional is not None:
            service = fr._match_service(own(professional), text)
            if service is None and from_draft:
                dropped[FIELD_SERVICE] = (
                    DROP_NOT_OFFERED_BY_PROFESSIONAL
                    if fr._match_service(clinic_catalog, text) is not None
                    else DROP_NOT_IN_CATALOG
                )
        else:
            known = fr._match_service(clinic_catalog, text)
            if known is None:
                if from_draft:
                    dropped[FIELD_SERVICE] = DROP_NOT_IN_CATALOG
            elif multi:
                offering = fr._professionals_offering(
                    tenant, professionals, str(known.get("name", "")), service_catalog
                )
                if len(offering) == 1 and FIELD_PROFESSIONAL not in dropped:
                    professional = offering[0]
                    service = fr._match_service(own(professional), text)
                else:
                    service = known  # two or more offer it: the doctor question comes first
            else:
                service = known

    # Convênio: the draft's text only counts when it names a real plan; a recorded answer
    # (a typed "Outro convênio" included) is the patient's own and is kept verbatim.
    if draft.insurance is not None:
        insurance = fr.match_insurance_plan(
            _insurance_view(tenant, tenant_insurance), draft.insurance
        )
        if insurance is None:
            dropped[FIELD_INSURANCE] = DROP_UNMATCHED_PLAN
    else:
        insurance = fr._selected_insurance(conversation)

    # Pra quem: "self" wins over a recorded third party; "other" needs an authorized name.
    recorded = fr._attendee_name(conversation)
    needs_attendee_name = False
    if draft.attendee == DRAFT_ATTENDEE_SELF:
        attendee_name: str | None = ATTENDEE_SELF
    elif draft.attendee == DRAFT_ATTENDEE_OTHER:
        attendee_name = real_attendee_name(recorded)
        needs_attendee_name = attendee_name is None
    else:
        attendee_name = recorded

    if draft.time is not None and draft.day is None:
        dropped[FIELD_TIME] = DROP_MISSING_DAY

    return _Checked(
        multi=multi,
        clinic_catalog=clinic_catalog,
        professional=professional,
        service=service,
        insurance=insurance,
        attendee_name=attendee_name,
        needs_attendee_name=needs_attendee_name,
        dropped=dropped,
    )


def _pending(draft: BookingDraft, checked: _Checked) -> BookingDraft:
    """What waits in `flow_draft` for the pra-quem answer: only the items that survived."""
    return BookingDraft(
        service=str(checked.service.get("name")) if checked.service else None,
        professional_id=(
            checked.professional.id if checked.multi and checked.professional is not None else None
        ),
        insurance=checked.insurance if draft.insurance is not None else None,
        attendee=draft.attendee,
        day=draft.day,
        time=draft.time if draft.day is not None else None,
        professional_unresolved=(
            draft.professional_unresolved or FIELD_PROFESSIONAL in checked.dropped
        ),
    )


def _carry(result: FlowRouterResult, state: Any) -> FlowRouterResult:
    """Name every validated answer on `result`: `_apply_flow_result` clears unnamed fields.

    Only results that stay in the booking carry them; the attendee steps carry the
    convênio alone (the rest waits in `flow_draft`), and a dead end (menu, alert) carries
    nothing, so its booking ends there.
    """
    if result.flow_state is not FlowState.SERVICE_CATALOG:
        return result
    if result.flow_selected_insurance is None and result.flow_step != fr.STEP_AWAITING_INSURANCE:
        result.flow_selected_insurance = state.flow_selected_insurance
    if result.flow_step in fr.ATTENDEE_STEPS:
        return result
    if result.flow_selected_type is None:
        result.flow_selected_type = state.flow_selected_type
    if (
        result.flow_selected_professional_id is None
        and result.flow_step != fr.STEP_AWAITING_PROFESSIONAL
    ):
        # The doctor list asks for a doctor: never one pre-selected behind it (TASK-083 final
        # review - "0 unidades" lands here with the refused doctor dropped on purpose).
        result.flow_selected_professional_id = state.flow_selected_professional_id
    if result.flow_attendee_name is None:
        result.flow_attendee_name = state.flow_attendee_name
    if result.flow_selected_unit_id is None and result.flow_step in fr.UNIT_KEEP_STEPS:
        # TASK-083: the unit the landing used (chosen before, or the doctor's only one).
        result.flow_selected_unit_id = getattr(state, "flow_selected_unit_id", None)
    return result


def _accepted(
    draft: BookingDraft, dropped: dict[str, str], names: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(name for name in draft.supplied_fields() if name in names and name not in dropped)


def _booking_services(checked: _Checked, tenant: Any, service_catalog: list | None) -> list[dict]:
    if checked.multi and checked.professional is not None:
        return professional_appointment_types(checked.professional, tenant, service_catalog)
    return checked.clinic_catalog


def _plan_refused(
    checked: _Checked,
    state: Any,
    *,
    tenant: Any,
    professionals: list,
    service_catalog: list | None,
) -> bool:
    """Whether the convênio matrix refuses the draft's service with its convênio (TASK-086 A8).

    Pure, over the tenant snapshot's matrix (`insurance_refused`). Only when service, convênio
    (a plan with an id: never Particular / a typed "Outro") and the booking's doctor are all
    known. The unit is the one `_land_day` would settle on (`flow_units.effective_unit`): known
    -> that unit (`is_accepted`); unknown -> every unit the service can be booked at, or the
    "no unit" slice for a doctor without links (`refuses_everywhere`, which is False whenever
    the reason is not the matrix). A clinic that never touched the matrix: always False.
    """
    exc = snapshot_exceptions(tenant)
    if not has_exceptions(exc) or checked.service is None:
        return False
    plan = fr._selected_plan(tenant, state.flow_selected_insurance)
    if plan is None:
        return False
    owner = fr._booking_professional(state, professionals)
    services = _booking_services(checked, tenant, service_catalog)
    service_id = fr._booking_service_id(state, tenant, services, owner)
    if service_id is None:
        return False
    owner_id = getattr(owner, "id", None)
    unit = flow_units.effective_unit(state, owner, service_id)
    if unit is not None:
        refused = not is_accepted(exc, owner_id, service_id, unit.id, plan["id"])
    else:
        refused = refuses_everywhere(exc, owner, service_id, plan["id"])
    if refused:
        log_refusal(
            "booking_draft_insurance_refused",
            conversation_id=getattr(state, "id", None),
            professional_id=owner_id,
            service_id=service_id,
            unit_id=getattr(unit, "id", None),
        )
    return refused


async def resolve_booking_draft(
    draft: BookingDraft,
    *,
    conversation: Any,
    tenant: Any,
    professional_rows: list,
    service_catalog: list | None,
    tenant_insurance: Any | None,
    calendar: CalendarSource,
    now: dt.datetime,
) -> DraftResolution:
    """Walk the booking in the workflow's own order and stop at the first open question.

    pra quem -> convênio -> profissional -> serviço -> dia -> horário (spec §4.2). An
    invalid item is dropped ALONE (a reason code in `dropped`) and the workflow asks from
    that step; the menu is only for a clinic with nothing bookable. Pure: reads nothing
    from the database - the caller passes FRESH data (workers/shared/draft_resolution.py).

    `conversation` is a snapshot with `id`, `tenant_id` and the `flow_*` fields; `tenant`
    is the flow tenant snapshot (`_flow_tenant_snapshot`); `professional_rows` is the
    router-shaped ACTIVE roster (`_flow_professionals`); `now` must be timezone-aware.

    Runs inside a booking-gate scope so held slots count as busy: the caller's gate when
    one is published (`flow_router.booking_gate_scope`), else an unarmed gate for this
    conversation - a direct call never sees a reserved slot as free.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    gate = fr._ACTIVE_GATE.get()
    if gate is None:
        gate = BookingGate(
            tenant_id=getattr(conversation, "tenant_id", None),
            conversation_id=conversation.id,
            patient_id=None,
            external_id=None,
            armed=False,
        )
    with fr.booking_gate_scope(gate):
        return await _resolve(
            draft,
            conversation=conversation,
            tenant=tenant,
            professionals=list(professional_rows or []),
            service_catalog=service_catalog,
            tenant_insurance=tenant_insurance,
            calendar=calendar,
            now=now,
        )


async def _resolve(
    draft: BookingDraft,
    *,
    conversation: Any,
    tenant: Any,
    professionals: list,
    service_catalog: list | None,
    tenant_insurance: Any | None,
    calendar: CalendarSource,
    now: dt.datetime,
) -> DraftResolution:
    checked = _check(
        draft,
        conversation=conversation,
        tenant=tenant,
        professionals=professionals,
        service_catalog=service_catalog,
        tenant_insurance=tenant_insurance,
    )
    state = fr._DayPickerState(
        id=getattr(conversation, "id", None),
        flow_selected_type=str(checked.service.get("name", "")) if checked.service else None,
        flow_selected_professional_id=(
            checked.professional.id if checked.multi and checked.professional is not None else None
        ),
        flow_selected_insurance=checked.insurance,
        flow_attendee_name=checked.attendee_name,
        # TASK-083: a unit already chosen in the buttons; `_land_day` keeps it only when it is
        # one the booking's doctor still offers (`flow_units.effective_unit`).
        flow_selected_unit_id=getattr(conversation, "flow_selected_unit_id", None),
    )

    def done(result: FlowRouterResult, *names: str, fallback: str | None = None) -> DraftResolution:
        result = _carry(result, state)
        return DraftResolution(
            result=result,
            landing_step=landing_step(result),
            accepted=_accepted(draft, checked.dropped, names),
            dropped=dict(checked.dropped),
            fallback=fallback or _result_fallback(result),
        )

    # A chosen doctor nobody can book (FEAT 41): the clinic hears about it, before any
    # question the patient could only answer into a dead end.
    if (
        checked.multi
        and checked.professional is not None
        and not professional_appointment_types(checked.professional, tenant, service_catalog)
    ):
        return done(
            fr._enter_professional_services(checked.professional, tenant), FIELD_PROFESSIONAL
        )
    # Nothing bookable at all: the plain menu, as every hand-back did before.
    if not checked.clinic_catalog:
        menu = FlowRouterResult(
            action="reply",
            bubbles=fr._menu_bubbles(tenant, professionals),
            flow_state=FlowState.MENU,
        )
        return done(menu, fallback=FALLBACK_NO_BOOKABLE_CATALOG)
    # 1. Pra quem - asked, never assumed. The rest waits in flow_draft.
    if (
        real_attendee_name(checked.attendee_name) is not None
        and getattr(conversation, "flow_step", None) == fr.STEP_AWAITING_ATTENDEE_AUTH
    ):
        # Capturing a name does not authorize booking for that person. Free text can
        # delegate this step to the AI while the authorization is still pending.
        result = fr._attendee_authorization_card(fr.ATTENDEE_NEXT_BOOK, checked.attendee_name)
        result.flow_draft = draft_record(_pending(draft, checked), saved_at=now)
        return done(result, *FIELD_NAMES)
    if checked.attendee_name is None:
        ask = fr._attendee_name_request if checked.needs_attendee_name else fr._attendee_question
        result = ask(fr.ATTENDEE_NEXT_BOOK)
        result.flow_draft = draft_record(_pending(draft, checked), saved_at=now)
        return done(result, *FIELD_NAMES)

    def asked(result: FlowRouterResult, *names: str) -> DraftResolution:
        """A convênio, doctor or service question: what comes after it waits in flow_draft.

        TASK-030 P3 and explicit-capture requests: when the
        pending draft still holds a service or a day, it is parked on the question, and the
        patient's answer re-runs the resolver over it (`flow_router._resume_parked_draft`,
        `draft_resolution._resume_booking_draft`) instead of falling into the next button
        step - so neither the day nor the booking details get lost on the way. With nothing
        after the question to keep, nothing waits.
        """
        pending = _pending(draft, checked)
        if (
            (fr.ai_draft_v2_enabled(tenant) or draft.has_capture_fields())
            and (pending.service is not None or pending.day is not None)
            and result.flow_state is FlowState.SERVICE_CATALOG
            and result.flow_step in fr.DRAFT_WAIT_STEPS
        ):
            result.flow_draft = draft_record(pending, saved_at=now)
            names = (*names, FIELD_DAY, FIELD_TIME)
        return done(result, *names)

    # 2. Convênio, when the clinic collects it.
    if fr._insurance_step_skip_reason(tenant) is None and checked.insurance is None:
        return asked(
            fr._enter_insurance(tenant, state, professionals=professionals),
            FIELD_FOR_WHOM,
            FIELD_PROFESSIONAL,
            FIELD_SERVICE,
        )
    # 3. Profissional (multi-doctor clinics; implicit otherwise).
    if (
        checked.multi or FIELD_PROFESSIONAL in checked.dropped
    ) and checked.professional is None:
        result = fr._enter_professional_list(tenant, professionals, insurance=checked.insurance)
        return asked(result, FIELD_FOR_WHOM, FIELD_INSURANCE, FIELD_SERVICE)
    # 4. Serviço.
    if checked.service is None:
        result = (
            fr._enter_professional_services(
                checked.professional, tenant, insurance=checked.insurance
            )
            if checked.multi
            else fr._start_booking(tenant, professionals, insurance=checked.insurance)
        )
        return asked(result, FIELD_FOR_WHOM, FIELD_INSURANCE, FIELD_PROFESSIONAL)
    # TASK-086 A8: service + doctor + convênio together and the matrix refuses them -> never
    # the express card: the convênio question again (filtered by the known service in the
    # flow), service and doctor kept. The flow's final guard covers whatever slips past here.
    if _plan_refused(
        checked,
        state,
        tenant=tenant,
        professionals=professionals,
        service_catalog=service_catalog,
    ):
        checked.insurance = None
        state.flow_selected_insurance = None
        result = fr._enter_insurance(tenant, state, professionals=professionals)
        result.bubbles = [TextBubble(body=DRAFT_PLAN_NOT_ACCEPTED_PREFIX), *result.bubbles]
        return asked(result, FIELD_FOR_WHOM, FIELD_PROFESSIONAL, FIELD_SERVICE)
    # 5-6. Dia e horário.
    return await _land_day(
        draft,
        checked,
        state,
        conversation=conversation,
        tenant=tenant,
        professionals=professionals,
        service_catalog=service_catalog,
        calendar=calendar,
        now=now,
        done=done,
    )


# Steps the service-detail card has already been shown by the time a conversation sits on
# them, in the button flow: "Sim" on that card is the only way past it. On clinics with the
# AI draft v2 switch, every landing of the resolver past that card (day picker, slot list,
# express card) carries the booking details message instead (`_land_day`), so reaching one
# of these steps means the details were shown on BOTH paths.
DETAILS_SEEN_STEPS = (
    fr.STEP_AWAITING_SERVICE_CONFIRM,
    fr.STEP_AWAITING_DAY,
    fr.STEP_AWAITING_DAY_RETRY,
    fr.STEP_AWAITING_DAY_ESCAPE,
    fr.STEP_AWAITING_SLOT,
    fr.STEP_AWAITING_CONFIRMATION,
    fr.STEP_AWAITING_RETRY,
)


def details_already_shown(
    conversation: Any, *, service_name: str | None, professional_id: Any
) -> bool:
    """Whether this patient already saw THIS booking's details (spec §4.4.2).

    True only when the FRESH conversation sits on a step past the service-detail card
    (`DETAILS_SEEN_STEPS`) for the same service (accent- and case-insensitive) and the same
    doctor. Anything else - the LLM state (no step), a pra-quem, convênio, doctor or service
    question, another service or another doctor - means the details go out.
    """
    if conversation is None or not service_name:
        return False
    if getattr(conversation, "flow_state", None) is not FlowState.SERVICE_CATALOG:
        return False
    if getattr(conversation, "flow_step", None) not in DETAILS_SEEN_STEPS:
        return False
    recorded = _recorded_service(conversation)
    if recorded is None or normalize_service_name(recorded) != normalize_service_name(service_name):
        return False
    return getattr(conversation, "flow_selected_professional_id", None) == professional_id


def _booking_details(
    checked: _Checked,
    state: Any,
    *,
    conversation: Any,
    tenant: Any,
    owner: Any | None,
    unit: Any | None = None,
) -> str | None:
    """The details message for a landing past the service card, or None.

    None on a clinic without the AI draft v2 switch (P2's landings stay exactly as they
    were), without a resolved service, or when the patient already saw this booking's
    details. The address is the booking's UNIT when it has one (TASK-083 spec §8), else
    `tenant.clinic_address`, the line workers/shared/draft_resolution.py put on the snapshot
    (None when the clinic has none, or has units - then no unit means no address at all).
    The price is `owner`'s at that unit (TASK-085); without a unit it follows the service
    card's rule (D3: left out when it differs between the units, never a range).
    """
    if not fr.ai_draft_v2_enabled(tenant) or checked.service is None:
        return None
    if details_already_shown(
        conversation,
        service_name=str(checked.service.get("name") or ""),
        professional_id=state.flow_selected_professional_id,
    ):
        return None
    return booking_details_text(
        service=fr._priced_entry(checked.service, owner, unit.id if unit is not None else None),
        professional=owner,
        insurance=state.flow_selected_insurance,
        attendee_name=state.flow_attendee_name,
        address=(
            unit.location if unit is not None else getattr(tenant, "clinic_address", None)
        ),
    )


def _with_details(result: FlowRouterResult, details: str | None) -> FlowRouterResult:
    """Open a booking landing past the service card with the details message."""
    if (
        details is None
        or result.action != "reply"
        or result.flow_state is not FlowState.SERVICE_CATALOG
        or result.flow_step not in DETAILS_SEEN_STEPS
    ):
        return result
    result.bubbles = [TextBubble(body=details), *result.bubbles]
    return result


async def _land_day(
    draft: BookingDraft,
    checked: _Checked,
    state: Any,
    *,
    conversation: Any,
    tenant: Any,
    professionals: list,
    service_catalog: list | None,
    calendar: CalendarSource,
    now: dt.datetime,
    done: Callable[..., DraftResolution],
) -> DraftResolution:
    """Steps 5-6: the day, then the time - both re-derived from the FRESH agenda.

    Nothing the AI sends becomes a time without passing through the agenda's own free
    slots for that day, minus the slots other conversations are holding (spec §4.4.1).
    An invalid day lands on the day picker, an invalid time on the slot list of its day.

    TASK-030 P3 and the explicit-capture correction: a valid time lands on the express
    confirmation (`_express_confirmation`); and every landing here skips the service-detail
    card, so each one - day picker, slot list, express card - opens with the booking
    details message, unless this patient already saw them (`details_already_shown`).
    """
    dropped = checked.dropped
    base = (FIELD_FOR_WHOM, FIELD_INSURANCE, FIELD_PROFESSIONAL, FIELD_SERVICE)
    services = _booking_services(checked, tenant, service_catalog)

    # The static "this doctor has no hours at all" check, before any calendar call - the
    # same one `_ask_day` makes for the button flow.
    owner = fr._booking_professional(state, professionals)
    if owner is not None and fr.professional_has_no_hours(owner, tenant):
        return done(fr._professional_config_incomplete(owner, fr.PROFESSIONAL_GAP_HOURS), *base)
    # TASK-083 spec §8 + follow-up §6.1: a doctor who attends at units books at one of those
    # that do THIS service. Not decided yet (two or more, the Sim/Não on the only one, or none
    # at all for the service): the day picker's own choke point (`_ask_day`) asks or says so -
    # never a day or a time without the unit, never a unit outside the service's filter (a
    # pick left from another service is ignored). Implied: that unit, silently. No unit link:
    # today's landing.
    service_id = fr._booking_service_id(state, tenant, services, owner)
    unit = flow_units.effective_unit(state, owner, service_id)
    state.flow_selected_unit_id = unit.id if unit is not None else None
    if flow_units.offered_units(owner) and unit is None:
        result = await fr._ask_day(state, tenant, None, services, professionals)
        return done(result, *base)

    details = _booking_details(
        checked, state, conversation=conversation, tenant=tenant, owner=owner, unit=unit
    )
    cal = flow_units.scoped_calendar(
        await calendar(checked.professional if checked.multi else None), unit
    )

    def land(result: FlowRouterResult, *names: str) -> DraftResolution:
        return done(_with_details(result, details), *names)

    async def day_picker(prefix: str | None = None) -> DraftResolution:
        result = await fr._ask_day(state, tenant, cal, services, professionals, prefix=prefix)
        return land(result, *base)

    def dropped_day(reason: str) -> None:
        dropped[FIELD_DAY] = reason
        if draft.time is not None:
            dropped[FIELD_TIME] = DROP_MISSING_DAY

    if draft.day is None:
        return await day_picker()
    if cal is None:
        unavailable = fr._calendar_unavailable(state, fr.BOOKING_DAY_BRANCH, fr.STEP_AWAITING_DAY)
        return done(unavailable, *base)

    today = now.astimezone(cal.tzinfo).date()
    if not today <= draft.day < today + dt.timedelta(days=fr.DAY_PICKER_WINDOW_DAYS):
        dropped_day(DROP_OUT_OF_WINDOW)
        return await day_picker(DRAFT_DAY_OUT_OF_WINDOW_PREFIX)

    # TASK-085: the unit settled above - the same length the day picker and "Confirmar" use.
    duration = fr._booking_duration(state, tenant, services, professionals, unit=unit)
    holds = await fr._hold_windows(fr._hold_owner(state, professionals))
    # The doctor's minimum notice: a day (or time) the patient named that falls inside it
    # is never offered, and the patient is told why instead of "no free time".
    floor = booking_notice_floor(owner, now.astimezone(cal.tzinfo))
    try:
        free = await free_slots_for_day(
            cal, day=draft.day, duration_minutes=duration, holds=holds, not_before=floor
        )
    except CalendarUnavailableError:
        unavailable = fr._calendar_unavailable(state, fr.BOOKING_DAY_BRANCH, fr.STEP_AWAITING_DAY)
        return done(unavailable, *base)
    if not free:
        dropped_day(DROP_DAY_UNAVAILABLE)
        inside_notice = floor is not None and draft.day <= floor.date()
        return await day_picker(
            fr.booking_notice_message(booking_notice_hours(owner))
            if inside_notice
            else DRAFT_DAY_UNAVAILABLE_PREFIX
        )

    names = (*base, FIELD_DAY)
    if draft.time is not None:
        match = next(
            (slot for slot in free if slot.time().replace(second=0, microsecond=0) == draft.time),
            None,
        )
        if match is None:
            dropped[FIELD_TIME] = DROP_NO_FREE_SLOT
        else:
            names = (*names, FIELD_TIME)
            express = await _express_confirmation(
                state=state,
                tenant=tenant,
                professional=owner,
                service=checked.service,
                slot_start=match,
                duration_minutes=duration,
                calendar=cal,
                professionals=professionals,
                details=details,
                explicit_request=draft.has_capture_fields(),
                unit=unit,
            )
            if express is not None:
                return done(express, *names)

    target = dt.datetime(draft.day.year, draft.day.month, draft.day.day)
    result = await fr._enter_slot_picker(
        state,
        tenant,
        cal,
        target,
        duration_minutes=duration,
        branch=fr.BOOKING_DAY_BRANCH,
        back_target=fr.BACK_TARGET_SERVICE,
        professionals=professionals,
    )
    return land(result, *names)


async def _express_confirmation(
    *,
    state: Any,
    tenant: Any,
    professional: Any | None,
    service: dict,
    slot_start: dt.datetime,
    duration_minutes: int,
    calendar: CalendarService,
    professionals: list,
    details: str | None = None,
    explicit_request: bool = False,
    unit: Any | None = None,
) -> FlowRouterResult | None:
    """Straight to the confirmation card when every item is valid (spec §4.4) - or None.

    Enabled by the clinic's v2 rollout or by an explicit capture request on the
    current tool. An old three-field request cannot bypass the missing preferences.

    Called only with pra-quem answered, the service known, and `slot_start` re-derived from
    the agenda's free slots minus holds (aware). `state` is the resolver's `_DayPickerState`;
    `professional` is the booking owner, named on the card. The bubbles are the booking
    details (`details`, when the patient has not seen them) and the card
    (`flow_router._confirmation_card`: service, doctor, who, date/time; Confirmar/Cancelar).

    NOTHING is booked here - no event, no appointment, no hold. "Confirmar" is routed by
    `flow_router._handle_confirmation`, exactly as for the card the slot tap draws (the
    Portal's code gate, the holds, the event, the row and the deposit hooks included).
    `flow_selected_slot` is the naive ISO minute of `slot_start` in the agenda's timezone,
    the same instant the card prints (skill date-derived-ui-labels); the resolver's `_carry`
    fills type, professional, convênio and attendee.

    `unit` is the one `_land_day` already settled for this service (TASK-083 follow-up): the
    card names the same unit the landing used, never one recomputed without the service.
    """
    if not fr.ai_draft_v2_enabled(tenant) and not explicit_request:
        return None
    start = (
        slot_start.astimezone(calendar.tzinfo)
        if calendar is not None and slot_start.tzinfo is not None
        else slot_start
    )
    card = fr._confirmation_card(
        state,
        start,
        professional=professional,
        unit=unit,
    )
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=details), card] if details else [card],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_CONFIRMATION,
        flow_selected_day=start.date().isoformat(),
        flow_selected_slot=start.replace(tzinfo=None).isoformat(timespec="minutes"),
    )
