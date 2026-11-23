"""The "Alterar Dados" flow: change an existing appointment through a draft (TASK-032 R6).

Spec: docs/superpowers/specs/2026-10-07-lembretes-alterar-dados-design.md.

`FlowState.EDIT_BOOKING` + `Conversation.flow_edit_draft`. Every step builds a NEW
`EditDraft` and returns it on the result; nothing touches the real appointment until the
final Confirmar (`_apply_confirm`, added with the apply step). Like the rest of the
router this is pure: calendars, the Pix guards (paid sinal, reschedule limit and - TASK-085 R -
a suspended consultation) and the appointment snapshot arrive as arguments (`EditContext`,
`appointments`, `professionals`), never read here.

This module sits beside `flow_router` and uses its private helpers on purpose - the day
and slot pickers, the label matching and the service/doctor/convênio matching exist
once, there. `flow_router._route` imports THIS module lazily (one dispatch line), which
is what keeps the import graph acyclic.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from secretaria.ai.formatter import (
    OPTION_PAGE_SIZE,
    ButtonBubble,
    OptionSource,
    SlotsBubble,
    TextBubble,
)
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import (
    EMOJI_DOCTOR,
    MAX_INTERACTIVE_BODY_CHARS,
    MAX_LIST_ROW_DESCRIPTION_CHARS,
    decorate,
    truncate_plain,
)
from secretaria.models import FlowState
from secretaria.services import appointment_edit as ae, flow_router as fr, flow_units
from secretaria.services.appointment_calendar_origin import CalendarOriginUnresolved
from secretaria.services.attendee import (
    ATTENDEE_NAME_INVALID,
    ATTENDEE_NAME_REQUEST,
    ATTENDEE_QUESTION_BODY,
    LABEL_ATTENDEE_AUTH_BACK,
    LABEL_ATTENDEE_AUTH_CONFIRM,
    LABEL_ATTENDEE_OTHER,
    LABEL_ATTENDEE_SELF,
    authorization_body,
    parse_attendee_name,
)
from secretaria.services.booking_dates import short_day_phrase
from secretaria.services.booking_details import clinic_address_line
from secretaria.services.calendar import (
    CalendarSlotTakenError,
    CalendarUnavailableError,
    build_event_description,
)
from secretaria.services.insurance_acceptance import (
    has_exceptions,
    is_accepted,
    refuses_all_services,
    refuses_everywhere,
    snapshot_exceptions,
)
from secretaria.services.local_calendar import (
    create_event_exclusive,
    update_event_details_exclusive,
)
from secretaria.services.payments.adjustment_suspension import SUSPENDED_VISIT_TEXT
from secretaria.services.payments.service_change_policy import MoneyTerms, money_fields_changed
from secretaria.services.service_catalog import entry_service_id
from secretaria.services.tenant_config import professional_appointment_types
from secretaria.services.unit_booking import blocked_on, event_unit_fields, service_calendar

logger = get_logger(__name__)


# --- Small helpers -------------------------------------------------------------------


def _tz(tenant) -> ZoneInfo:
    return ZoneInfo(getattr(tenant, "timezone", None) or "America/Sao_Paulo")


def _uuid(value: str | None) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    """A naive clinic-local ISO minute, the shape `EditDraft` stores."""
    return value.replace(tzinfo=None, second=0, microsecond=0).isoformat(timespec="minutes")


def _result(draft: ae.EditDraft, bubbles: list, step: str, **extra) -> fr.FlowRouterResult:
    """A reply that stays in the edit, carrying the appointment, the doctor and the draft."""
    return fr.FlowRouterResult(
        action="reply",
        bubbles=bubbles,
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=step,
        flow_managing_appointment_id=_uuid(draft.appointment_id),
        flow_selected_professional_id=_uuid(draft.current["professional_id"]),
        flow_edit_draft=draft.to_json(),
        **extra,
    )


def _leave(tenant, professionals, *prefix) -> fr.FlowRouterResult:
    """Close the edit: `prefix` bubbles, then the returning-patient menu. The draft is dropped."""
    return fr.FlowRouterResult(
        action="reply",
        bubbles=[*prefix, *fr._menu_bubbles(tenant, professionals)],
        flow_state=FlowState.MENU,
    )


def _carrier(conversation, draft: ae.EditDraft) -> fr._DayPickerState:
    """What the day/slot pickers read: the appointment, the doctor and the NEW draft."""
    return fr._DayPickerState(
        id=getattr(conversation, "id", None),
        flow_step=getattr(conversation, "flow_step", None),
        flow_selected_professional_id=_uuid(draft.current["professional_id"]),
        flow_managing_appointment_id=_uuid(draft.appointment_id),
        flow_edit_draft=draft.to_json(),
    )


def _doctor(professionals, draft: ae.EditDraft):
    # The consultation's own doctor: hidden ones too (TASK-090 Adendo 1, I1).
    return fr._find_owner_by_id(professionals, _uuid(draft.current["professional_id"]))


def _draft_entry(tenant, draft: ae.EditDraft, doctor) -> dict | None:
    """`doctor`'s own catalog entry for the draft's service, or None when he does not offer it."""
    return fr._match_service(_services_of(tenant, doctor), draft.current["service"])


def _blocked_days(tenant, draft: ae.EditDraft, doctor) -> frozenset[str]:
    """Weekdays on which patients cannot book the draft's service with `doctor` at the draft's
    unit (TASK-087). Nothing when the doctor does not offer the service (R5: not known yet)."""
    return flow_units.blocked_weekdays_of(
        doctor, _draft_entry(tenant, draft, doctor), _uuid(draft.current.get("unit_id"))
    )


def _day_check(tenant, draft: ae.EditDraft, doctor) -> fr.DayRefusal | None:
    """day -> the patient's refusal line when the draft's service is not done that weekday
    (`flow_router._service_day_check`), over the same doctor, entry and unit as `_blocked_days`
    - so exactly the days `_calendar` leaves out. None when nothing is blocked (review A1)."""
    unit_id = _uuid(draft.current.get("unit_id"))
    return fr._service_day_check(
        doctor,
        _draft_entry(tenant, draft, doctor),
        SimpleNamespace(id=unit_id) if unit_id is not None else None,
    )


# R4 (TASK-087 spec §9): the service's weekdays are only checked against a draft that changes one
# of these - never against the visit as it was booked (a convênio or patient change, or picking
# the same service / doctor again, keeps a visit on a day blocked afterwards).
_DAY_RULE_FIELDS = frozenset({"data", "horário", "serviço", "médico", "unidade"})


def _rechecks_day(draft: ae.EditDraft) -> bool:
    return bool(_DAY_RULE_FIELDS.intersection(draft.changed()))


def _calendar(
    ctx: ae.EditContext,
    draft: ae.EditDraft,
    professionals,
    tenant,
    *,
    service_days: bool = True,
) -> Any | None:
    """The draft's agenda as seen from its unit (TASK-083 final wave I1), like the manage
    flow's "Remarcar" (`flow_router._appointment_unit_calendar`): the booking's unit hours,
    else the doctor's only unit / the union of their units; the plain agenda for a doctor
    without units - and, TASK-087, without the weekdays the draft's service is not done on there
    (every day list, time list and free check of this flow reads this one agenda).
    `service_days=False` only for re-checking the visit AS BOOKED (R4). May raise
    `CalendarOriginUnresolved` exactly like `ctx.calendar_for`."""
    doctor = _doctor(professionals, draft)
    agenda = flow_units.appointment_calendar(
        ctx.calendar_for(draft.calendar_professional_id),
        _uuid(draft.current.get("unit_id")),
        doctor,
    )
    if not service_days:
        return agenda
    return service_calendar(agenda, _blocked_days(tenant, draft, doctor))


def _draft_unit(draft: ae.EditDraft, doctor) -> Any | None:
    """The draft's CURRENT unit as the doctor's snapshot carries it (name, address), or None -
    no unit, or one the doctor no longer offers (the description then simply has no unit lines;
    the router does no I/O to look it up)."""
    unit_id = _uuid(draft.current.get("unit_id"))
    if unit_id is None:
        return None
    return next((u for u in flow_units.offered_units(doctor) if u.id == unit_id), None)


def _clinic_address(tenant) -> str | None:
    """The clinic's address as one line. In production `Tenant.address` is the JSON dict
    (`booking_details.clinic_address_line`); a plain string (older snapshots, tests) is used
    as it is. Never `.strip()` on a dict (follow-up E1, D4)."""
    raw = getattr(tenant, "address", None)
    if isinstance(raw, str):
        return raw.strip() or None
    return clinic_address_line(raw)


def _recap_place(draft: ae.EditDraft, doctor, tenant) -> tuple[str | None, str | None]:
    """(unit name, address) for the confirmation text (D4): a booking at a unit shows that
    unit and ITS address (none when the unit has none - never the clinic's, which may be
    another building); without a unit, the clinic's address exactly as before."""
    unit = _draft_unit(draft, doctor)
    if unit is None:
        return None, _clinic_address(tenant)
    address = str(getattr(unit, "address", None) or "").strip() or None
    return str(getattr(unit, "name", "") or "") or None, address


def _stage_unit(draft: ae.EditDraft, doctor) -> Any | None:
    """The unit a staged service list is drawn for (`stage.at_unit`), if the doctor can still
    be booked there; else None (the list falls back to today's)."""
    raw = draft.stage.get("at_unit")
    if not raw:
        return None
    return next((u for u in flow_units.bookable_units(doctor) if str(u.id) == raw), None)


def _service_id(tenant, professional, name: str | None) -> UUID | None:
    """The canonical id of `name` in `professional`'s own entries (spec §9.11), or None - no
    unit restriction - when they do not offer it or the entry carries no `service_id`."""
    if professional is None:
        return None
    entry = fr._match_service(professional_appointment_types(professional, tenant), name)
    return entry_service_id(entry) if entry is not None else None


def _with_doctor(
    draft: ae.EditDraft, professional, service_id: UUID | None = None
) -> ae.EditDraft:
    """The draft with `professional` as its doctor AND the unit that follows: back to the
    original doctor -> the original unit (unless a NEW service is not done there); else
    `flow_units.unit_after_doctor_change` for the draft's service (`service_id`, TASK-083 final
    review). A draft that never had a unit and gets none stays byte-identical (no `unit_id`)."""
    new = draft.with_changes(professional_id=str(professional.id))
    original_unit = _uuid(draft.original.get("unit_id"))
    if str(professional.id) == draft.original["professional_id"] and (
        draft.current["service"] == draft.original["service"]
        or not flow_units.unit_refused(professional, original_unit, service_id)
    ):
        unit = original_unit
    else:
        unit = flow_units.unit_after_doctor_change(
            professional,
            _uuid(draft.current.get("unit_id")),
            original_unit,
            service_id,
        )
    if unit is None and "unit_id" not in draft.current:
        return new
    return new.with_changes(unit_id=str(unit) if unit is not None else None)


def _is_original_booking(draft: ae.EditDraft) -> bool:
    """Same doctor, same service, same unit as the appointment: a pure reschedule. Spec §9.4 -
    an existing appointment is never invalidated, even if its service left that unit since."""
    cur, org = draft.current, draft.original
    return (
        cur["professional_id"] == org["professional_id"]
        and cur["service"] == org["service"]
        and cur.get("unit_id") == org.get("unit_id")
    )


def _unit_refused(draft: ae.EditDraft, doctor, service_id: UUID | None) -> bool:
    """The draft would book its service at a unit where the doctor does not do it (spec §2
    anti-objective). No marks / no links / an entry without `service_id` -> never."""
    if _is_original_booking(draft):
        return False
    return flow_units.unit_refused(doctor, _uuid(draft.current.get("unit_id")), service_id)


# --- Convênio acceptance per service x unit (TASK-086, inventory A9-A12) ---------------------
#
# The matrix (`tenant.insurance_refused`) only ever SUBTRACTS from the list rule
# (`fr._accepts_plan`, unchanged): with no refused cell every helper below answers exactly what
# the edit flow answered before. Particular, a typed "Outro" and a legacy name have no plan id
# and are never refused.


# The scope key that makes the Portal's options route attach each doctor's units before it
# re-resolves a matrix-marked card (same literal as `list_options.SCOPE_UNIT_MARKS`, TASK-086
# T4a; not imported - list_options imports this module).
SCOPE_UNIT_MARKS = "unit_marks"


def _plan_id(tenant, insurance: str | None) -> str | None:
    """The flow-space id of the clinic plan `insurance` names, or None (nothing to refuse)."""
    plan = fr._selected_plan(tenant, insurance)
    return str(plan["id"]) if plan is not None else None


def _matrix_takes(tenant, doctor, service_id, unit_id, plan_id) -> bool:
    """Whether the matrix lets `doctor` take `plan_id` for `service_id` at `unit_id`.

    `unit_id=None` asks "anywhere the service is booked with this doctor" (no unit decided yet;
    for a doctor without units that is the "no unit" slice). Unknown doctor, service or plan,
    or a clinic that never refused a cell -> True.
    """
    exc = snapshot_exceptions(tenant)
    if not has_exceptions(exc) or doctor is None or service_id is None or plan_id is None:
        return True
    if unit_id is None:
        return not refuses_everywhere(exc, doctor, service_id, plan_id)
    return is_accepted(exc, doctor.id, service_id, unit_id, plan_id)


def _doctor_takes(tenant, professional, plan: dict, service: str | None, unit_id) -> bool:
    """A9: the doctor row's "aceita o convênio" - the list rule AND the matrix for the draft's
    service at the unit the booking would have with them (`unit_id`; None = not decided). A
    doctor who does not do that service (or no service given: the Portal's later pages) is
    marked when he takes the plan for at least one of his services."""
    plan_id = str(plan["id"])
    if not fr._accepts_plan(tenant, professional, plan_id):
        return False
    exc = snapshot_exceptions(tenant)
    if not has_exceptions(exc):
        return True
    entries = _services_of(tenant, professional)
    entry = fr._match_service(entries, service) if service else None
    if entry is None:
        return not refuses_all_services(exc, professional, entries, plan_id)
    return _matrix_takes(tenant, professional, entry_service_id(entry), _uuid(unit_id), plan_id)


def _insurance_refused(draft: ae.EditDraft, tenant, doctor) -> bool:
    """A11: the draft changed the doctor, service, unit or convênio into a cell the matrix
    refuses. A pure reschedule (same doctor, service, unit and convênio) is never refused - the
    appointment itself is never questioned, like spec §9.4 of TASK-083."""
    cur, org = draft.current, draft.original
    if _is_original_booking(draft) and cur["insurance"] == org["insurance"]:
        return False
    if doctor is None:
        return False
    return not _matrix_takes(
        tenant,
        doctor,
        _service_id(tenant, doctor, cur["service"]),
        _uuid(cur.get("unit_id")),
        _plan_id(tenant, cur["insurance"]),
    )


def _mark_refused_services(rows: list, services: list, refused: set[str]) -> list:
    """`rows` (2- or 3-tuples, `svc|<name>` ids) with "não atende este convênio" on the
    services in `refused`; every other row exactly as it was."""
    if not refused:
        return list(rows)
    ids = {f"svc|{s.get('name', '')}" for s in services if str(s.get("name", "")) in refused}
    return [
        (row[0], row[1], ae.EDIT_ROW_INSURANCE_REFUSED) if row[0] in ids else row for row in rows
    ]


def _refused_service_names(
    tenant, doctor, services: list, insurance: str | None, unit_id
) -> set[str]:
    """A12: the names among `services` the doctor does not take the convênio for - at
    `unit_id` when the service is done there, else at any unit where it is."""
    plan_id = _plan_id(tenant, insurance)
    if doctor is None or plan_id is None or not has_exceptions(snapshot_exceptions(tenant)):
        return set()
    refused: set[str] = set()
    for entry in services:
        service_id = entry_service_id(entry)
        here = unit_id
        if here is not None and here not in {
            u.id for u in flow_units.units_for_service(doctor, service_id)
        }:
            here = None
        if not _matrix_takes(tenant, doctor, service_id, here, plan_id):
            refused.add(str(entry.get("name", "")))
    return refused


def _undo_placement(draft: ae.EditDraft, problem: str) -> ae.EditDraft:
    """The draft a refused card falls back to: the appointment's doctor/service/unit, and for a
    convênio refusal also its convênio (else the menu would keep a convênio it cannot have)."""
    undone = _undo_doctor_change(draft)
    if problem == ae.EDIT_INSURANCE_NOT_ACCEPTED:
        undone = undone.with_changes(insurance=draft.original["insurance"])
    return undone


def _refuse(draft: ae.EditDraft, ctx: ae.EditContext, notice: str) -> fr.FlowRouterResult:
    """Back to the edit menu with `draft` as it was, behind the one line that says why."""
    menu = _menu_result(draft.with_stage(), ctx)
    menu.bubbles.insert(0, TextBubble(body=notice))
    return menu


def _undo_doctor_change(draft: ae.EditDraft) -> ae.EditDraft:
    """The draft with the appointment's own doctor, service and unit again (the length follows
    the original service; the start the patient picked stays)."""
    org = draft.original
    length = datetime.fromisoformat(str(org["end_at"])) - datetime.fromisoformat(
        str(org["start_at"])
    )
    undone = draft.with_changes(
        professional_id=org["professional_id"],
        service=org["service"],
        end_at=_iso(draft.start + length),
    )
    if "unit_id" in draft.current or "unit_id" in org:
        undone = undone.with_changes(unit_id=org.get("unit_id"))
    return undone


def _services_of(tenant, professional) -> list[dict]:
    if professional is not None:
        return professional_appointment_types(professional, tenant)
    return fr.active_appointment_types(tenant)


def _minutes_at(service: dict, tenant, doctor, unit_id) -> int:
    """`service`'s length for `doctor` at the draft's unit (`unit_id`: a UUID or the draft's
    str) - TASK-085 D2/D7: the unit's own duration when it has one, else today's base."""
    return fr._service_duration_at(service, tenant, doctor, _uuid(unit_id))


def _requirements(tenant, professionals, draft: ae.EditDraft) -> list[str]:
    service = fr._match_service(
        _services_of(tenant, _doctor(professionals, draft)), draft.current["service"]
    )
    raw = (service or {}).get("requirements") or []
    return [str(item).strip() for item in raw if str(item).strip()]


async def _held(calendar, start, end, professional_id) -> bool:
    """A promised booking window blocks edits just as it blocks a new booking."""
    start = start if start.tzinfo else start.replace(tzinfo=calendar.tzinfo)
    end = end if end.tzinfo else end.replace(tzinfo=calendar.tzinfo)
    holds = await fr._hold_windows(_uuid(professional_id))
    return any(a < end and b > start for a, b in holds)


async def _inside_buffer(
    calendar: Any,
    start: datetime,
    end: datetime,
    ignore: str | None,
    professional_id: str | None,
    professionals: list | None,
) -> bool:
    """TASK-088 Task 5: is [start, end) inside the buffer of the consultations of the doctor who
    owns `calendar` (the draft's, R31)? "Alterar dados" is the PATIENT's edit: the time he keeps
    or picks is held to the same buffer as a new booking; `ignore` - the appointment's own event -
    never pads itself (R32). Without `professionals` (or a buffer) nothing is read."""
    if not professionals:
        return False
    owner_id = _uuid(professional_id)
    everyone = fr.everyone_of(professionals)
    doctor = (
        fr._find_owner_by_id(professionals, owner_id)
        if owner_id is not None
        else (everyone[0] if len(everyone) == 1 else None)
    )
    return await fr._inside_buffer(
        calendar,
        start,
        end,
        doctor,
        source="appointment_edit",
        professional_id=owner_id,
        ignore_event_id=ignore,
    )


async def _slot_free(
    calendar: Any,
    start: datetime,
    end: datetime,
    ignore: str | None,
    professional_id: str | None = None,
    professionals: list | None = None,
) -> bool:
    """Whether [start, end) is bookable on `calendar`; unknown (no agenda, outage) = not free.
    Bookable = free on the agenda, not held, and outside the doctor's buffer (TASK-088)."""
    if calendar is None:
        return False
    if await _held(calendar, start, end, professional_id):
        return False
    try:
        free = bool(await calendar.is_slot_free(start, end, ignore_event_id=ignore))
    except CalendarUnavailableError:
        return False
    return free and not await _inside_buffer(
        calendar, start, end, ignore, professional_id, professionals
    )


def _ignore_event(draft: ae.EditDraft, appt: dict, ctx: ae.EditContext) -> str | None:
    """Exclude our event only when it lives on the calendar being checked."""
    same = ctx.same_calendar(
        draft.calendar_professional_id, draft.original_calendar_professional_id
    )
    return str(appt.get("google_event_id") or "") or None if same else None


# --- Menus ----------------------------------------------------------------------------


def _menu_labels(ctx: ae.EditContext) -> list[str]:
    labels: list[str] = []
    if not ctx.reschedule_blocked:
        labels += [ae.LABEL_EDIT_DATE, ae.LABEL_EDIT_TIME]
    # TASK-085 R D-R3: a paid sinal no longer hides the doctor (nor the convênio): the worker
    # settles the difference of every money change at Confirmar.
    labels += [ae.LABEL_EDIT_SERVICE, ae.LABEL_EDIT_DOCTOR, ae.LABEL_EDIT_MORE]
    return labels


def _menu_result(draft: ae.EditDraft, ctx: ae.EditContext) -> fr.FlowRouterResult:
    body = ae.edit_menu_body(_menu_labels(ctx))
    if ctx.paid_deposit:
        body = f"{body}\n\n{ae.EDIT_PIX_NOTICE}"
    if ctx.reschedule_blocked:
        body = f"{body}\n\n{ae.EDIT_LIMIT_NOTICE}"
    return _result(
        draft,
        [fr.MenuBubble(body=body[:MAX_INTERACTIVE_BODY_CHARS], labels=_menu_labels(ctx))],
        fr.STEP_EDIT_MENU,
    )


def _more_result(draft: ae.EditDraft, ctx: ae.EditContext) -> fr.FlowRouterResult:
    labels = [ae.LABEL_EDIT_INSURANCE, ae.LABEL_EDIT_PATIENT, ae.LABEL_EDIT_BACK]
    body = ae.edit_more_body(labels)
    return _result(draft, [fr.MenuBubble(body=body, labels=labels)], fr.STEP_EDIT_MORE)


def _suspended_refusal() -> fr.FlowRouterResult:
    """TASK-085 R (contrato §3.8): the consultation waits for the Pix of the difference of a
    change the patient asked for - it is not edited. ONE message; the edit (draft) is dropped
    and nothing else changes (the money and the agenda are the engine's until the Pix or the
    choice decides)."""
    return fr.FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=SUSPENDED_VISIT_TEXT)],
        flow_state=FlowState.IDLE,
    )


def enter_edit_menu(
    tenant,
    appt: dict,
    ctx: ae.EditContext | None = None,
    *,
    draft: ae.EditDraft | None = None,
) -> fr.FlowRouterResult:
    """The entry from the reminder's "Alterar Dados": a fresh draft + the 5-option menu.

    A suspended consultation (`ctx.suspended`) is refused with one message instead."""
    ctx = ctx or ae.EditContext()
    if ctx.suspended:
        return _suspended_refusal()
    draft = draft or ae.EditDraft.from_appointment(appt, _tz(tenant))
    return _menu_result(draft, ctx)


def _money_fields_changed(draft: ae.EditDraft) -> frozenset[str]:
    """Which of the four money fields (service, unit, doctor, convênio) the draft changes -
    the SAME predicate the worker and the staff use (`service_change_policy`, contrato §3.4).
    The unit only counts on a booking with one (only such a draft carries the key)."""

    def terms(values: dict) -> MoneyTerms:
        return MoneyTerms(
            service=values.get("service"),
            professional_id=values.get("professional_id"),
            unit_id=values.get("unit_id"),
            insurance=values.get("insurance"),
        )

    return money_fields_changed(terms(draft.original), terms(draft.current))


def _placement_problem(draft: ae.EditDraft, tenant, doctor) -> str | None:
    """Why the draft's doctor/service/unit/convênio cannot be booked as they are, or None.

    The unit problems (follow-up E1 review), then TASK-086 A11: a changed doctor, service, unit
    or convênio the clinic does not accept for that convênio (`_insurance_refused`). A pure
    reschedule is never a problem (§9.4)."""
    problem = _unit_problem(draft, tenant, doctor)
    if problem is None and _insurance_refused(draft, tenant, doctor):
        return ae.EDIT_INSURANCE_NOT_ACCEPTED
    return problem


def _unit_problem(draft: ae.EditDraft, tenant, doctor) -> str | None:
    """Why the draft's doctor/service/unit cannot be booked as they are (follow-up E1 review),
    or None: a booking that had a unit left without one while the doctor has units to pick,
    or a service at a unit where it is not done. A pure reschedule is never a problem (§9.4)."""
    if _is_original_booking(draft):
        return None
    if (
        draft.original.get("unit_id")
        and draft.current.get("unit_id") is None
        and flow_units.bookable_units(doctor)
    ):
        return ae.EDIT_UNIT_MISSING
    # Follow-up final review: a unit the doctor does not attend at (e.g. a new doctor's unit
    # left behind when the original doctor came back). Only when the doctor or the
    # unit changed - the appointment's own unit is never questioned (§9.4).
    unit_id = _uuid(draft.current.get("unit_id"))
    offered = {unit.id for unit in flow_units.offered_units(doctor)}
    if (
        unit_id is not None
        and offered
        and unit_id not in offered
        and (
            draft.current.get("unit_id") != draft.original.get("unit_id")
            or draft.current["professional_id"] != draft.original["professional_id"]
        )
    ):
        return ae.EDIT_DOCTOR_UNIT_MISMATCH
    service = fr._match_service(_services_of(tenant, doctor), draft.current["service"])
    if service is not None and _unit_refused(draft, doctor, entry_service_id(service)):
        return ae.EDIT_SERVICE_UNIT_MISMATCH
    return None


# The stage mark of the convênio list drawn ONLY with the plans the draft's combination accepts
# (a redisplay and the list's pages keep it).
STAGE_INSURANCE_ONLY_ACCEPTED = "insurance_only_accepted"


def _draft_combination(tenant, draft: ae.EditDraft, doctor) -> tuple[dict | None, Any | None]:
    """(the doctor's catalog entry for the draft's service, the draft's unit) - what the convênio
    is judged at. No unit on the draft (a doctor without units): None, the "no unit" slice."""
    return _draft_entry(tenant, draft, doctor), _draft_unit(draft, doctor)


def _reask_insurance(
    draft: ae.EditDraft, tenant, professionals, ctx: ae.EditContext, doctor
) -> fr.FlowRouterResult:
    """TASK-088 R19 (SPEC, designer decisions 1 and 4): the new doctor / service / unit refuses
    the appointment's convênio.

    The placement the patient chose STAYS; the convênio is the open question, asked through the
    booking's own decision (`flow_router._accepted_plans`, the one function the booking's gate
    uses): some plans accepted -> the list of ONLY those, with a line on top; none -> the
    appointment becomes Particular and the card says so - never silently, never a dead end.
    Logged with ids and booleans only."""
    entry, unit = _draft_combination(tenant, draft, doctor)
    accepted = fr._accepted_plans(tenant, doctor, entry, unit)
    logger.info(
        "appointment_edit_insurance_reasked",
        professional_id=str(getattr(doctor, "id", None)),
        unit_id=str(unit.id) if unit is not None else None,
        accepted_count=len(accepted),
    )
    if not accepted:
        line = fr.INSURANCE_REFUSED_PARTICULAR_UNIT if unit else fr.INSURANCE_REFUSED_PARTICULAR
        card = _confirm_result(
            draft.with_changes(insurance=fr.LABEL_INSURANCE_PARTICULAR).with_stage(),
            tenant,
            professionals,
            ctx,
        )
        card.bubbles.insert(0, TextBubble(body=line))
        return card
    line = fr.INSURANCE_REFUSED_LIST_UNIT if unit else fr.INSURANCE_REFUSED_LIST
    return _filtered_insurance_list(draft, tenant, professionals, doctor, unit, notice=line)


def _filtered_insurance_list(
    draft: ae.EditDraft, tenant, professionals, doctor, unit, *, page: int = 0, notice=None
) -> fr.FlowRouterResult:
    """The booking's convênio list (`flow_router._enter_insurance`: only the accepted plans,
    Particular always, "Outro" only with at least one plan) as a step of the edit. Its Portal
    card resolves later pages with the booking's own scope, which `list_options` already
    re-filters - so page 1 and every later page agree. No back row: the edit has none."""
    carrier = fr._DayPickerState(
        flow_selected_type=draft.current["service"],
        flow_selected_professional_id=getattr(doctor, "id", None),
        flow_selected_unit_id=getattr(unit, "id", None),
    )
    listed = fr._enter_insurance(
        tenant,
        carrier,
        page=page,
        professionals=professionals,
        unit_id=getattr(unit, "id", None),
        notice=notice,
        with_back=False,
    )
    return _result(
        draft.with_stage(**{STAGE_INSURANCE_ONLY_ACCEPTED: "1"}),
        listed.bubbles,
        fr.STEP_EDIT_INSURANCE,
    )


def _confirm_result(
    draft: ae.EditDraft, tenant, professionals, ctx: ae.EditContext, *, check: bool = True
) -> fr.FlowRouterResult:
    """The ONE complete confirmation message: everything, what changed, three buttons.

    Never for a half-placed draft (`_placement_problem`): the doctor, service and unit go back
    to the appointment's own, behind the line that says what was missing.
    """
    doctor = _doctor(professionals, draft)
    problem = _placement_problem(draft, tenant, doctor) if check else None
    if problem is not None:
        if problem == ae.EDIT_INSURANCE_NOT_ACCEPTED:
            logger.info("appointment_edit_insurance_not_accepted", stage="card")
            if draft.current["insurance"] == draft.original["insurance"]:
                # TASK-088 R19: the patient changed the doctor, service or unit and kept the
                # appointment's convênio, which the new combination refuses: never reverted and
                # never swapped for Particular in silence - see `_reask_insurance`.
                return _reask_insurance(draft, tenant, professionals, ctx, doctor)
        return _refuse(_undo_placement(draft, problem), ctx, problem)
    unit, address = _recap_place(draft, doctor, tenant)
    text = ae.build_edit_recap(
        draft,
        doctor=str(getattr(doctor, "name", "") or "") or None,
        address=address,
        requirements=_requirements(tenant, professionals, draft),
        unit=unit,
    )
    return _result(
        draft.with_stage(),
        [
            fr.MenuBubble(
                body=text, labels=[fr.LABEL_CONFIRM, fr.LABEL_CANCEL, ae.LABEL_EDIT_MORE_DATA]
            )
        ],
        fr.STEP_EDIT_CONFIRM,
    )


# --- Date / time ----------------------------------------------------------------------


async def _begin_date(conversation, tenant, draft, professionals, ctx) -> fr.FlowRouterResult:
    draft = draft.with_stage(mode="date")
    return await fr.enter_day_picker(
        _carrier(conversation, draft),
        tenant,
        _calendar(ctx, draft, professionals, tenant),
        duration_minutes=draft.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        anchor=draft.start,
        professionals=professionals,
    )


async def _slots_for(
    conversation, tenant, draft, day: datetime, professionals, ctx
) -> fr.FlowRouterResult:
    draft = draft.with_stage(mode="time")
    return await fr._enter_slot_picker(
        _carrier(conversation, draft),
        tenant,
        _calendar(ctx, draft, professionals, tenant),
        datetime.combine(day.date(), time.min),
        duration_minutes=draft.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        professionals=professionals,
    )


async def _after_slot_affecting_change(
    conversation, tenant, draft, appt, professionals, ctx
) -> fr.FlowRouterResult:
    """A doctor/service change may have taken the current time with it: check, then go on.

    Free -> straight to the confirmation. Not free (or unknown) -> day, then time, for the
    NEW doctor/service, behind a one-line explanation (spec P1) - never a confirmation
    for a slot that does not exist. TASK-087 R4: when nothing slot-affecting changed (the same
    doctor or unit picked again) the visit is checked as booked, without the service's weekdays.
    """
    calendar = _calendar(ctx, draft, professionals, tenant, service_days=_rechecks_day(draft))
    free = await _slot_free(
        calendar,
        draft.start,
        draft.end,
        _ignore_event(draft, appt, ctx),
        draft.current["professional_id"],
        professionals,
    )
    if free:
        return _confirm_result(draft, tenant, professionals, ctx)
    return await _reslot_picker(conversation, tenant, draft, professionals, ctx)


def _no_room(draft: ae.EditDraft, ctx: ae.EditContext) -> fr.FlowRouterResult:
    """The new window does not fit and the time may not move (Pix reschedule limit): the
    service goes back to what it was, with the one line that says why.

    TASK-085 R fix round 1: a NEW doctor (offered at the limit since D-R3) busy at that time
    goes back too - doctor, service, unit and time are the appointment's own again, behind a
    doctor line - so Confirmar never loops on a doctor who cannot take the consultation."""
    if draft.current["professional_id"] != draft.original["professional_id"]:
        undone = _undo_doctor_change(draft).with_changes(
            start_at=draft.original["start_at"], end_at=draft.original["end_at"]
        )
        return _refuse(undone, ctx, ae.EDIT_DOCTOR_NO_ROOM)
    keep = {key: draft.original[key] for key in ("service", "start_at", "end_at")}
    if "unit_id" in draft.original:
        # Follow-up E1: a unit picked for that service goes back with it.
        keep["unit_id"] = draft.original["unit_id"]
    menu = _menu_result(draft.with_changes(**keep).with_stage(), ctx)
    menu.bubbles.insert(0, TextBubble(body=ae.EDIT_SERVICE_NO_ROOM))
    return menu


async def _reslot_picker(conversation, tenant, draft, professionals, ctx) -> fr.FlowRouterResult:
    """Day, then time, for the draft's doctor/service - behind the one-line explanation."""
    if ctx.reschedule_blocked:
        # A day/time picker the patient may not use would loop back to the menu.
        return _no_room(draft, ctx)
    reslot = draft.with_stage(mode="reslot")
    picker = await fr.enter_day_picker(
        _carrier(conversation, reslot),
        tenant,
        _calendar(ctx, reslot, professionals, tenant),
        duration_minutes=reslot.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        anchor=reslot.start,
        professionals=professionals,
    )
    if picker.action == "reply":
        picker.bubbles.insert(0, TextBubble(body=ae.EDIT_RESLOT_NOTICE))
    return picker


async def _refused_day_picker(
    conversation, tenant, draft, professionals, ctx, refusal: str
) -> fr.FlowRouterResult:
    """A day the draft's service is not done on (TASK-087 review A1): its refusal line over the
    day picker - which already leaves such days out - instead of "no free time" or a question
    about the time. `draft` carries the stage the next answer is read in."""
    logger.info(
        "appointment_edit_day_blocked_for_service",
        conversation_id=str(getattr(conversation, "id", None)),
    )
    return await fr.enter_day_picker(
        _carrier(conversation, draft),
        tenant,
        _calendar(ctx, draft, professionals, tenant),
        duration_minutes=draft.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        anchor=draft.start,
        prefix=refusal,
        professionals=professionals,
    )


async def _time_entry(conversation, tenant, draft, professionals, ctx) -> fr.FlowRouterResult:
    """The "Horário" entry: the times of the visit's own day - unless the service is no longer
    done on that weekday (R4: the time changes, so the rule applies): the reason, then a new day
    first."""
    day_check = _day_check(tenant, draft, _doctor(professionals, draft))
    refusal = day_check(draft.start) if day_check is not None else None
    if refusal:
        return await _refused_day_picker(
            conversation, tenant, draft.with_stage(mode="time"), professionals, ctx, refusal
        )
    return await _slots_for(conversation, tenant, draft, draft.start, professionals, ctx)


async def _day_step(conversation, tenant, body, draft, appt, professionals, ctx):
    calendar = _calendar(ctx, draft, professionals, tenant)
    day_check = _day_check(tenant, draft, _doctor(professionals, draft))
    if draft.stage.get("mode") == "date":
        target, _page = fr._day_from_body(body)
        if target is None and calendar is not None:
            target = fr._parse_day(body, datetime.now(calendar.tzinfo))
        refusal = day_check(target) if target is not None and day_check is not None else None
        if refusal:
            return await _refused_day_picker(
                conversation, tenant, draft.with_stage(mode="date"), professionals, ctx, refusal
            )
        if target is not None:
            asked = draft.with_stage(mode="date", target_day=target.date().isoformat())
            return _result(
                asked,
                [
                    fr.MenuBubble(
                        body=ae.EDIT_TIME_TOO_BODY,
                        labels=[ae.LABEL_TIME_TOO_YES, ae.LABEL_TIME_TOO_NO],
                    )
                ],
                fr.STEP_EDIT_TIME_TOO,
            )
    return await fr._handle_day_step(
        conversation,
        tenant,
        calendar,
        body,
        duration_minutes=draft.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        services=[],
        back_target=None,
        professionals=professionals,
        refuse_day=day_check,
    )


async def _time_too_step(conversation, tenant, body, draft, appt, professionals, ctx):
    raw = draft.stage.get("target_day")
    if not raw:
        return _menu_result(draft.with_stage(), ctx)
    day = datetime.fromisoformat(raw)
    answered = fr._label_match(body, ae.LABEL_TIME_TOO_YES) or fr._label_match(
        body, ae.LABEL_TIME_TOO_NO
    )
    day_check = _day_check(tenant, draft, _doctor(professionals, draft)) if answered else None
    refusal = day_check(day) if day_check is not None else None
    if refusal:
        # A day stored before the service stopped being done on that weekday (review A1).
        return await _refused_day_picker(
            conversation, tenant, draft.with_stage(mode="date"), professionals, ctx, refusal
        )
    if fr._label_match(body, ae.LABEL_TIME_TOO_YES):
        return await _slots_for(conversation, tenant, draft, day, professionals, ctx)
    if fr._label_match(body, ae.LABEL_TIME_TOO_NO):
        start = datetime.combine(day.date(), draft.start.time())
        end = start + timedelta(minutes=draft.duration_minutes)
        calendar = _calendar(ctx, draft, professionals, tenant)
        if await _slot_free(
            calendar,
            start,
            end,
            _ignore_event(draft, appt, ctx),
            draft.current["professional_id"],
            professionals,
        ):
            moved = draft.with_changes(start_at=_iso(start), end_at=_iso(end))
            return _confirm_result(moved, tenant, professionals, ctx)
        picker = await _slots_for(conversation, tenant, draft, day, professionals, ctx)
        if picker.action == "reply":
            picker.bubbles.insert(0, TextBubble(body=ae.EDIT_TIME_BUSY))
        return picker
    return fr._preserve(conversation, "delegate_llm")


async def _slot_step(conversation, tenant, body, draft, appt, professionals, ctx):
    control = await fr._handle_slot_controls(
        conversation,
        tenant,
        _calendar(ctx, draft, professionals, tenant),
        body,
        duration_minutes=draft.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        services=[],
        back_target=None,
        professionals=professionals,
    )
    if control is not None:
        return control
    start = fr._slot_iso_from_body(body)
    if start is None:
        return fr._preserve(conversation, "delegate_llm")
    end = start + timedelta(minutes=draft.duration_minutes)
    return _confirm_result(
        draft.with_changes(start_at=_iso(start), end_at=_iso(end)), tenant, professionals, ctx
    )


# --- Service --------------------------------------------------------------------------


def _flow_doctor(professionals, draft: ae.EditDraft):
    """The doctor a staged unit/service choice is for: `stage.pending_doctor` (a doctor change
    not drafted yet - follow-up E1 review: the draft is never half-applied), else the draft's
    own doctor."""
    pending = _uuid(draft.stage.get("pending_doctor"))
    if pending is not None:
        return fr._find_professional_by_id(professionals, pending)
    return _doctor(professionals, draft)


def _does_service_elsewhere(tenant, doctor, draft: ae.EditDraft, unit_id) -> bool:
    """D1, the owner's rule for the "Alterar unidade" row: `doctor` does the booking's
    pre-selected service (the draft's current one) at a bookable unit other than `unit_id`."""
    service = fr._match_service(_services_of(tenant, doctor), draft.current["service"])
    if service is None:
        return False
    return any(
        unit.id != unit_id
        for unit in flow_units.units_for_service(doctor, entry_service_id(service))
    )


def _service_list(
    draft: ae.EditDraft,
    tenant,
    professionals,
    ctx,
    *,
    after_doctor: bool = False,
    at_unit: Any | None = None,
    page: int = 0,
) -> fr.FlowRouterResult:
    """The services of the draft's doctor only (owner, 2026-10-07).

    Follow-up E1 (b): with `at_unit`, the services the staged doctor (`_flow_doctor`) does at
    that unit, under `EDIT_SERVICES_AT_UNIT_BODY`, and the "Alterar unidade" row last (D1) only
    when that doctor does the booking's service at another bookable unit. Nothing is drafted:
    the doctor stays in `stage.pending_doctor` until a service is picked.

    Pages on WhatsApp like the booking's list (`fr.catalog_page`, TASK-082 S4); the
    "Alterar unidade" row is a fixed row of every page (counted in `fixed_rows`, so a page
    never passes WhatsApp's 10 rows) and a pinned row of the Portal card, whose pointer is
    the doctor's services AT that unit (`list_options`, scope `doctor_unit`).
    """
    doctor = _flow_doctor(professionals, draft) if at_unit is not None else _doctor(
        professionals, draft
    )
    services = _services_of(tenant, doctor)
    body = ae.EDIT_SERVICE_BODY
    stage = {"after_doctor": "1"} if after_doctor else {}
    scope = fr.service_scope(doctor.id if doctor is not None else None)
    change_unit = False
    if at_unit is not None:
        services = flow_units.services_at_unit(doctor, services, at_unit.id)
        body = ae.EDIT_SERVICES_AT_UNIT_BODY.format(
            medico=str(getattr(doctor, "name", "") or "").strip(),
            unidade=str(getattr(at_unit, "name", "") or "").strip(),
        )[:MAX_INTERACTIVE_BODY_CHARS]
        stage["at_unit"] = str(at_unit.id)
        if draft.stage.get("pending_doctor"):
            stage["pending_doctor"] = draft.stage["pending_doctor"]
        change_unit = _does_service_elsewhere(tenant, doctor, draft, at_unit.id)
        if change_unit:
            stage["change_unit"] = "1"
        if doctor is not None:
            scope = {
                "list": "doctor_unit",
                "professional_id": str(doctor.id),
                "unit_id": str(at_unit.id),
            }
    if not services:
        menu = _menu_result(draft.with_stage(), ctx)
        menu.bubbles.insert(0, TextBubble(body=ae.EDIT_NO_SERVICES))
        return menu
    pinned: list[tuple] = (
        [
            (
                ae.ROW_EDIT_CHANGE_UNIT,
                ae.LABEL_EDIT_CHANGE_UNIT,
                ae.LABEL_EDIT_CHANGE_UNIT_DESCRIPTION,
            )
        ]
        if change_unit
        else []
    )
    # TASK-086 A12: a service the doctor does not take the booking's convênio for (at this
    # unit / the booking's unit) says so; still listed (a mark, not a filter - the card refuses).
    refused = _refused_service_names(
        tenant,
        doctor,
        services,
        draft.current["insurance"],
        at_unit.id if at_unit is not None else _uuid(draft.current.get("unit_id")),
    )
    if refused:
        marked_unit = at_unit.id if at_unit is not None else _uuid(draft.current.get("unit_id"))
        scope = {**scope, "insurance": draft.current["insurance"], SCOPE_UNIT_MARKS: True}
        if marked_unit is not None:
            # The Portal's later pages mark at the same unit page 1 used (list_options).
            scope.setdefault("unit_id", str(marked_unit))
    rows: list[tuple] = _mark_refused_services(
        fr.service_page_rows(
            services, page, fixed_rows=len(pinned), event="edit_service_list_paged"
        ),
        services,
        refused,
    )
    rows.extend(pinned)
    return _result(
        draft.with_stage(**stage),
        [
            SlotsBubble(
                body=body,
                rows=rows,
                button_label="Ver serviços",
                section_title="Serviços",
                source=OptionSource(
                    kind="service",
                    scope=scope,
                    items=_mark_refused_services(
                        fr.portal_service_rows(services), services, refused
                    ),
                    pinned=pinned,
                ),
            )
        ],
        fr.STEP_EDIT_SERVICE,
    )


def _unit_list(draft: ae.EditDraft, units: list, body: str, **stage: str) -> fr.FlowRouterResult:
    """The unit list of "Alterar Dados" (follow-up E1): one payload row per unit (name +
    address), the booking flow's own rows (`flow_units.unit_rows`). Nothing is drafted yet."""
    if len(units) > flow_units.MAX_UNIT_ROWS:
        logger.warning("edit_unit_list_truncated", total=len(units), shown=flow_units.MAX_UNIT_ROWS)
    return _result(
        draft.with_stage(**stage),
        [
            SlotsBubble(
                body=body[:MAX_INTERACTIVE_BODY_CHARS],
                rows=flow_units.unit_rows(units),
                button_label=flow_units.UNIT_LIST_BUTTON,
                section_title=flow_units.UNIT_LIST_SECTION,
            )
        ],
        fr.STEP_EDIT_UNIT,
    )


def _pending_stage(pending: str | None) -> dict[str, str]:
    return {"pending_doctor": pending} if pending else {}


def _service_unit_list(
    draft: ae.EditDraft,
    tenant,
    professionals,
    ctx,
    service_name: str | None,
    *,
    refuse_from: ae.EditDraft | None = None,
    notice: str = ae.EDIT_SERVICE_NOT_AT_UNIT,
    pending: str | None = None,
) -> fr.FlowRouterResult:
    """(a) The doctor's bookable units where `service_name` is done - always a list, even of
    one (D3). The service (and, from an AI proposal, a new doctor: `pending`) wait in the stage
    until a unit is picked. No unit at all -> back to the menu with `notice`, `refuse_from`
    (default `draft`) unchanged."""
    doctor = (
        fr._find_professional_by_id(professionals, _uuid(pending))
        if pending
        else _doctor(professionals, draft)
    )
    service = fr._match_service(_services_of(tenant, doctor), service_name)
    units = (
        flow_units.units_for_service(doctor, entry_service_id(service))
        if service is not None
        else []
    )
    if not units:
        return _refuse(refuse_from or draft, ctx, notice)
    # TASK-088 R19: every unit of the service is offered - the convênio is judged AFTER the pick
    # (`_reask_insurance`), as the booking does since the unit comes before the convênio.
    name = str(service.get("name"))
    return _unit_list(
        draft,
        units,
        ae.EDIT_UNIT_FOR_SERVICE_BODY.format(servico=name),
        unit_purpose="service",
        pending_service=name,
        **_pending_stage(pending),
    )


def _doctor_units(tenant, doctor) -> list:
    """The doctor's bookable units where they do at least one of their services."""
    return flow_units.units_with_services(doctor, _services_of(tenant, doctor))


def _doctor_unit_list(
    draft: ae.EditDraft, tenant, professionals, ctx, *, pending: str | None = None
) -> fr.FlowRouterResult:
    """(b)/D2 "Em qual unidade...?" for the staged new doctor; then their services there. The
    draft itself is untouched (the doctor waits in `stage.pending_doctor`)."""
    pending = pending or draft.stage.get("pending_doctor")
    doctor = (
        fr._find_professional_by_id(professionals, _uuid(pending))
        if pending
        else _doctor(professionals, draft)
    )
    units = _doctor_units(tenant, doctor)
    if not units:
        # Follow-up final review: never back to the menu without saying why.
        return _refuse(draft, ctx, ae.EDIT_DOCTOR_SERVICE_NO_UNIT)
    # TASK-088 R19: no filter by the convênio here either - judged after the pick.
    return _unit_list(
        draft,
        units,
        ae.EDIT_DOCTOR_UNITS_BODY.format(medico=str(getattr(doctor, "name", "") or "").strip()),
        unit_purpose="doctor",
        **_pending_stage(pending),
    )


def _unit_list_again(draft: ae.EditDraft, tenant, professionals, ctx) -> fr.FlowRouterResult:
    """Redraw whichever unit list the draft's stage is on."""
    pending = draft.stage.get("pending_doctor")
    if draft.stage.get("unit_purpose") == "service":
        return _service_unit_list(
            draft, tenant, professionals, ctx, draft.stage.get("pending_service"), pending=pending
        )
    return _doctor_unit_list(draft, tenant, professionals, ctx, pending=pending)


async def _unit_step(conversation, tenant, body, draft, appt, professionals, ctx):
    """A tap (or typed name) on the edit's unit list.

    (a) `unit_purpose=service`: the staged service AND the unit (and a staged doctor) are
    drafted together, then the time is re-checked (the unit's hours may not hold it).
    (b) `unit_purpose=doctor`: the staged doctor's services at that unit. Talk, not a unit:
    one model turn on this same step.
    """
    doctor = _flow_doctor(professionals, draft)
    pending = draft.stage.get("pending_doctor")
    if draft.stage.get("unit_purpose") == "service":
        service = fr._match_service(
            _services_of(tenant, doctor), draft.stage.get("pending_service")
        )
        if service is None:
            return _menu_result(draft.with_stage(), ctx)
        units = flow_units.units_for_service(doctor, entry_service_id(service))
        if not units:
            return _refuse(draft, ctx, ae.EDIT_SERVICE_NOT_AT_UNIT)
        unit = flow_units.match_unit(units, body)
        if unit is None:
            return fr._preserve(conversation, "delegate_llm")
        logger.info("edit_unit_selected", purpose="service", unit_count=len(units))
        minutes = _minutes_at(service, tenant, doctor, unit.id)
        changes = {
            "service": str(service.get("name")),
            "end_at": _iso(draft.start + timedelta(minutes=minutes)),
            "unit_id": str(unit.id),
        }
        if pending:
            changes["professional_id"] = pending
        new = draft.with_changes(**changes).with_stage()
        return await _after_slot_affecting_change(
            conversation, tenant, new, appt, professionals, ctx
        )
    units = _doctor_units(tenant, doctor)
    if not units:
        return _refuse(draft, ctx, ae.EDIT_DOCTOR_SERVICE_NO_UNIT)
    unit = flow_units.match_unit(units, body)
    if unit is None:
        return fr._preserve(conversation, "delegate_llm")
    logger.info("edit_unit_selected", purpose="doctor", unit_count=len(units))
    return _service_list(draft, tenant, professionals, ctx, at_unit=unit)


async def _service_step(conversation, tenant, body, draft, appt, professionals, ctx):
    staged = _flow_doctor(professionals, draft)
    at_unit = _stage_unit(draft, staged)
    if at_unit is None and draft.stage.get("at_unit") and draft.stage.get("pending_doctor"):
        # Follow-up final review: the unit this list was drawn for can no longer be booked.
        # The new doctor stays staged and his units are shown again, behind a plain notice.
        listed = _doctor_unit_list(draft, tenant, professionals, ctx)
        if listed.flow_step == fr.STEP_EDIT_UNIT:
            listed.bubbles.insert(0, TextBubble(body=flow_units.UNIT_GONE_NOTICE))
        return listed
    after_doctor = bool(draft.stage.get("after_doctor"))
    more = fr.catalog_more_page(body, fr.LABEL_MORE_SERVICES)
    if more is not None:
        # TASK-082 S4 paging; the services-at-unit list keeps its unit, staged doctor and
        # "Alterar unidade" row on every page (TASK-083).
        return _service_list(
            draft, tenant, professionals, ctx, after_doctor=after_doctor, at_unit=at_unit, page=more
        )
    if at_unit is not None:
        if draft.stage.get("change_unit") and fr._label_match(body, ae.LABEL_EDIT_CHANGE_UNIT):
            # (b) "Alterar unidade": every unit where this doctor does something, then services.
            return _doctor_unit_list(draft, tenant, professionals, ctx)
        service = fr._match_service(
            flow_units.services_at_unit(staged, _services_of(tenant, staged), at_unit.id), body
        )
        if service is None:
            return fr._preserve(conversation, "delegate_llm")
        minutes = _minutes_at(service, tenant, staged, at_unit.id)
        # (b) The new doctor, this service and the unit the list was drawn for, together.
        new = draft.with_changes(
            professional_id=str(staged.id),
            service=str(service.get("name")),
            end_at=_iso(draft.start + timedelta(minutes=minutes)),
            unit_id=str(at_unit.id),
        ).with_stage()
        return await _after_slot_affecting_change(
            conversation, tenant, new, appt, professionals, ctx
        )
    doctor = _doctor(professionals, draft)
    service = fr._match_service(_services_of(tenant, doctor), body)
    if service is None:
        return fr._preserve(conversation, "delegate_llm")
    new = draft.with_changes(service=str(service.get("name"))).with_stage()
    # TASK-083 final review: never the service at a unit where it is not done. After a doctor
    # change on a booking without a unit, the unit is only derived: derive it again for THIS
    # service. Otherwise the booking's unit stays, and a service not done there opens the
    # units where it is (follow-up E1 (a)) - or keeps the old service when there is none.
    service_id = entry_service_id(service)
    if after_doctor and doctor is not None:
        new = _with_doctor(new, doctor, service_id)
    # TASK-085: the length at the unit the draft ends up with (settled just above).
    minutes = _minutes_at(service, tenant, doctor, new.current.get("unit_id"))
    new = new.with_changes(end_at=_iso(draft.start + timedelta(minutes=minutes)))
    if _unit_refused(new, doctor, service_id):
        if after_doctor:
            return _refuse(_undo_doctor_change(draft), ctx, ae.EDIT_DOCTOR_SERVICE_NO_UNIT)
        return _service_unit_list(
            draft.with_stage(), tenant, professionals, ctx, str(service.get("name"))
        )
    # (TASK-088 R19: a service the booking's convênio is refused for at the booking's unit goes
    # on like any other change - the card then re-asks the convênio, `_reask_insurance`; it no
    # longer detours through the units that take it.)
    if (
        after_doctor
        or minutes != draft.duration_minutes
        or (_rechecks_day(new) and blocked_on(new.start, _blocked_days(tenant, new, doctor)))
    ):
        # A different length (or a new doctor) may not fit the current time - nor may a service
        # that is not done on the booked weekday (TASK-087; R4: only when something changed -
        # the same service picked again keeps the visit as booked, review A2): check first.
        return await _after_slot_affecting_change(
            conversation, tenant, new, appt, professionals, ctx
        )
    return _confirm_result(new, tenant, professionals, ctx)


# --- Doctor ---------------------------------------------------------------------------


async def _availability(
    calendar: Any,
    start: datetime,
    end: datetime,
    ignore: str | None,
    professional_id: str | None = None,
    professionals: list | None = None,
):
    """True / False, or None when the agenda is missing or unreachable (no mark then)."""
    if calendar is None:
        return None
    if await _held(calendar, start, end, professional_id):
        return False
    try:
        free = bool(await calendar.is_slot_free(start, end, ignore_event_id=ignore))
    except CalendarUnavailableError:
        return None
    return free and not await _inside_buffer(
        calendar, start, end, ignore, professional_id, professionals
    )


def _edit_doctor_description(
    tenant,
    professional,
    plan: dict | None,
    availability: str | None = None,
    *,
    service: str | None = None,
    unit_id: str | None = None,
) -> str | None:
    """An "alterar consulta" doctor row's subtitle: free/busy (when known), plan, specialty.

    TASK-086 A9: "aceita o convênio" also asks the matrix for the draft's `service` at the
    `unit_id` the booking would have with this doctor (`_doctor_takes`)."""
    parts: list[str] = [availability] if availability else []
    if plan is not None and _doctor_takes(tenant, professional, plan, service, unit_id):
        parts.append("aceita o convênio")
    specialty = (getattr(professional, "specialty", None) or "").strip() or None
    return (
        truncate_plain(" · ".join(parts) or (specialty or ""), MAX_LIST_ROW_DESCRIPTION_CHARS)
        or None
    )


def edit_professional_rows(
    tenant,
    professionals,
    insurance: str | None,
    availability: dict[str, str] | None = None,
    *,
    service: str | None = None,
    units: dict[str, str | None] | None = None,
) -> list[tuple[str, str, str | None]]:
    """Every doctor as a Portal row of the "alterar consulta" list (full "🥼 <name>").

    `availability` (doctor id -> "✅ Livre 15/10 15:20") is known only for the doctors
    the list checked live; the rest - and every row the options route serves - carry
    the convênio mark and the specialty only (services/list_options.py).

    TASK-086 A9: `service` (the draft's) and `units` (doctor id -> the unit the booking would
    have with them) refine "aceita o convênio" by the acceptance matrix. The card's scope
    carries both (only when the clinic has a refused cell); without them a doctor is marked
    when he takes the plan for at least one of his services.
    """
    plan = fr._selected_plan(tenant, insurance)
    marks = availability or {}
    placed = units or {}
    return [
        (
            f"prof|{professional.id}",
            decorate(EMOJI_DOCTOR, str(professional.name or "")),
            _edit_doctor_description(
                tenant,
                professional,
                plan,
                marks.get(str(professional.id)),
                service=service,
                unit_id=placed.get(str(professional.id)),
            ),
        )
        for professional in professionals or []
    ]


async def _doctor_list(
    draft,
    tenant,
    appt,
    professionals,
    ctx,
    *,
    page: int = 0,
) -> fr.FlowRouterResult:
    """Every doctor, marked free (✅) or busy (❌) at the appointment's current day and time.

    The agenda is asked for the doctors on screen: the WhatsApp page shown, the Portal's
    first page (`OPTION_PAGE_SIZE`); the Portal's later pages carry no free/busy mark.
    WhatsApp pages (`page`) like the booking's doctor list (TASK-082 S4).
    """
    plan = fr._selected_plan(tenant, draft.current["insurance"])
    # Inside the line, the day reads "ter, 15/10" (TASK-087 D4, review P5).
    when = f"{short_day_phrase(draft.start)} {draft.start.strftime('%H:%M')}"
    roster = list(professionals or [])
    shown, more = fr.professional_page(roster, page, fixed_rows=0, event="edit_doctor_list_paged")
    to_check = list(shown) + [p for p in roster[:OPTION_PAGE_SIZE] if p not in shown]
    availability: dict[str, str] = {}
    for professional in to_check:
        pid = str(professional.id)
        own_event = str(appt.get("google_event_id") or "") or None
        calendar_pid = (
            draft.original_calendar_professional_id
            if pid == draft.original["professional_id"] else pid
        )
        same = ctx.same_calendar(calendar_pid, draft.original_calendar_professional_id)
        ignore = own_event if same else None
        # Marked within the unit the booking would have with THIS doctor (TASK-083).
        candidate = _with_doctor(
            draft, professional, _service_id(tenant, professional, draft.current["service"])
        )
        calendar = service_calendar(
            flow_units.appointment_calendar(
                ctx.calendar_for(calendar_pid),
                _uuid(candidate.current.get("unit_id")),
                professional,
            ),
            # TASK-087: a doctor who does not do this service on that day reads busy - except
            # the visit's own doctor on the visit as booked (R4, review A3).
            _blocked_days(tenant, candidate, professional)
            if _rechecks_day(candidate)
            else frozenset(),
        )
        free = await _availability(calendar, draft.start, draft.end, ignore, pid, professionals)
        if free is not None:
            availability[pid] = (
                f"{ae.MARK_FREE if free else ae.MARK_BUSY} {'Livre' if free else 'Ocupado'} {when}"
            )
    # TASK-086 A9: the unit the booking would have with each doctor, for the convênio mark.
    # Only computed (and only carried in the scope) when the clinic refused some cell, so a
    # clinic that never touched the matrix gets the very same card as before.
    service = draft.current["service"]
    scope: dict[str, Any] = {"insurance": draft.current["insurance"], "edit": True}
    placed: dict[str, str | None] = {}
    if plan is not None and has_exceptions(snapshot_exceptions(tenant)):
        placed = {
            str(p.id): _with_doctor(
                draft, p, _service_id(tenant, p, service)
            ).current.get("unit_id")
            for p in roster
        }
        scope.update({"service": service, "units": placed, SCOPE_UNIT_MARKS: True})
    rows: list[tuple] = [
        (
            f"prof|{professional.id}",
            fr._professional_row_title(professional.name),
            _edit_doctor_description(
                tenant,
                professional,
                plan,
                availability.get(str(professional.id)),
                service=service,
                unit_id=placed.get(str(professional.id)),
            ),
        )
        for professional in shown
    ]
    rows.extend((row_id, title, None) for row_id, title in more)
    return _result(
        draft.with_stage(),
        [
            SlotsBubble(
                body=ae.EDIT_DOCTOR_BODY,
                rows=rows,
                button_label="Ver médicos",
                section_title="Médicos",
                source=OptionSource(
                    kind="professional",
                    scope=scope,
                    items=edit_professional_rows(
                        tenant,
                        roster,
                        draft.current["insurance"],
                        availability,
                        service=service,
                        units=placed,
                    ),
                ),
            )
        ],
        fr.STEP_EDIT_DOCTOR,
    )


async def _doctor_step(conversation, tenant, body, draft, appt, professionals, ctx):
    roster = list(professionals or [])
    more = fr.catalog_more_page(body, fr.LABEL_MORE_PROFESSIONALS)
    if more is not None:
        return await _doctor_list(draft, tenant, appt, roster, ctx, page=more)
    professional = fr._match_professional(roster, body)
    if professional is None:
        return fr._preserve(conversation, "delegate_llm")
    return await _doctor_choice(conversation, tenant, draft, professional, appt, professionals, ctx)


async def _doctor_choice(
    conversation, tenant, draft, professional, appt, professionals, ctx, *, today_ok=False
):
    """`professional` as the draft's new doctor (the guided tap or an AI proposal).

    Today's rule when the booking has no unit, the doctor has none, or the doctor does the
    current service at the booking's unit (or is the appointment's own doctor again) - with
    `today_ok` (an AI proposal carrying other fields) that case returns None so the caller
    goes on drafting. Else follow-up E1 (b): their services AT that unit, with "Alterar
    unidade" when they do the current service at another bookable unit; a doctor who does
    nothing there -> their units (D2; one unit is implied); no bookable unit at all ->
    `EDIT_DOCTOR_SERVICE_NO_UNIT`. A list never drafts the doctor (`stage.pending_doctor`).
    """
    entries = professional_appointment_types(professional, tenant)
    service = fr._match_service(entries, draft.current["service"])
    service_id = entry_service_id(service) if service is not None else None
    new = _with_doctor(draft, professional, service_id)
    # The booking's unit: the drafted one, else the appointment's own (a doctor without units
    # chosen before cleared it - the next doctor is still placed against the real booking).
    pre = _uuid(draft.current.get("unit_id") or draft.original.get("unit_id"))
    keeps = (
        service is not None
        and not _unit_refused(new, professional, service_id)
        and (
            new.current.get("unit_id") == str(pre)
            or str(professional.id) == draft.original["professional_id"]
        )
    )
    if pre is None or not flow_units.offered_units(professional) or keeps:
        if today_ok:
            return None
        if service is None:
            # The new doctor does not offer the current service: theirs next, then the check.
            return _service_list(new, tenant, professionals, ctx, after_doctor=True)
        if _unit_refused(new, professional, service_id):
            # The new doctor does this service at no bookable unit: the doctor stays as it was.
            return _refuse(draft, ctx, ae.EDIT_DOCTOR_SERVICE_NO_UNIT)
        minutes = _minutes_at(service, tenant, professional, new.current.get("unit_id"))
        new = new.with_changes(end_at=_iso(new.start + timedelta(minutes=minutes))).with_stage()
        return await _after_slot_affecting_change(
            conversation, tenant, new, appt, professionals, ctx
        )
    pending = draft.with_stage(pending_doctor=str(professional.id))
    here = next((u for u in flow_units.bookable_units(professional) if u.id == pre), None)
    if here is not None and flow_units.services_at_unit(professional, entries, pre):
        return _service_list(pending, tenant, professionals, ctx, at_unit=here)
    units = flow_units.units_with_services(professional, entries)
    if not units:
        return _refuse(draft, ctx, ae.EDIT_DOCTOR_SERVICE_NO_UNIT)
    if len(units) > 1:
        return _doctor_unit_list(
            draft, tenant, professionals, ctx, pending=str(professional.id)
        )
    # One unit where this doctor works: no question about it (the unit is secondary).
    only = units[0]
    if service is not None and not flow_units.unit_refused(professional, only.id, service_id):
        minutes = _minutes_at(service, tenant, professional, only.id)
        new = draft.with_changes(
            professional_id=str(professional.id),
            unit_id=str(only.id),
            end_at=_iso(draft.start + timedelta(minutes=minutes)),
        ).with_stage()
        return await _after_slot_affecting_change(
            conversation, tenant, new, appt, professionals, ctx
        )
    return _service_list(pending, tenant, professionals, ctx, at_unit=only)


# --- Convênio -------------------------------------------------------------------------


def _insurance_list(
    draft: ae.EditDraft, tenant, professionals, *, page: int = 0
) -> fr.FlowRouterResult:
    """The clinic's plans, each marked by whether the CURRENT doctor takes it; never filtered.

    Paged like the booking's list (`fr.insurance_page`): no plan is ever cut. A list drawn after
    a refusal (`_reask_insurance`) stays the filtered one - redisplays and later pages too.
    """
    doctor = _doctor(professionals, draft)
    if draft.stage.get(STAGE_INSURANCE_ONLY_ACCEPTED):
        _entry, unit = _draft_combination(tenant, draft, doctor)
        return _filtered_insurance_list(draft, tenant, professionals, doctor, unit, page=page)
    plans = fr._tenant_insurance_plans(tenant)
    shown, next_page = fr.insurance_page(plans, page)
    # TASK-086 A10: the mark is for the draft's service at the draft's unit.
    service_id = _service_id(tenant, doctor, draft.current["service"])
    unit_id = _uuid(draft.current.get("unit_id"))
    scope: dict[str, Any] = {"edit_doctor_id": str(doctor.id) if doctor is not None else None}
    if has_exceptions(snapshot_exceptions(tenant)):
        # Only with a refused cell, so the card stays byte-identical without the matrix.
        scope.update(
            {
                "service_id": str(service_id) if service_id is not None else None,
                "unit_id": str(unit_id) if unit_id is not None else None,
                SCOPE_UNIT_MARKS: True,
            }
        )
    rows: list[tuple] = [
        (
            f"ins|{plan['name']}",
            fr.truncate_list_row_title(str(plan["name"])),
            _edit_plan_mark(tenant, plan, doctor, service_id=service_id, unit_id=unit_id),
        )
        for plan in shown
    ]
    rows.extend((row_id, title, None) for row_id, title in fr.insurance_more_row(next_page))
    rows.extend(fr.INSURANCE_PINNED_ROWS)
    return _result(
        draft.with_stage(),
        [
            SlotsBubble(
                body="Qual o convênio dessa consulta?",
                rows=rows,
                button_label="Ver convênios",
                section_title="Convênios",
                source=OptionSource(
                    kind="insurance",
                    scope=scope,
                    items=edit_insurance_rows(
                        tenant, plans, doctor, service_id=service_id, unit_id=unit_id
                    ),
                    pinned=list(fr.INSURANCE_PINNED_ROWS),
                    has_clinic_notes=fr.insurance_plans_have_clinic_notes(plans),
                ),
            )
        ],
        fr.STEP_EDIT_INSURANCE,
    )


def _edit_plan_mark(
    tenant, plan: dict, doctor, *, service_id: Any = None, unit_id: Any = None
) -> str | None:
    """Whether the appointment's CURRENT doctor takes `plan` (None without doctor or plan id).

    TASK-086 A10: for the draft's `service_id` at its `unit_id` the matrix may refuse it too
    (no service known -> the list rule alone, as before). A mark, never a filter."""
    if doctor is None or not plan.get("id"):
        return None
    plan_id = str(plan["id"])
    if fr._accepts_plan(tenant, doctor, plan_id) and _matrix_takes(
        tenant, doctor, service_id, _uuid(unit_id), plan_id
    ):
        return f"{ae.MARK_FREE} aceito pelo médico"
    return f"{ae.MARK_BUSY} não aceito pelo médico"


def edit_insurance_rows(
    tenant, plans: list, doctor, *, service_id: Any = None, unit_id: Any = None
) -> list[tuple[str, str, str | None]]:
    """Every plan as a Portal row of the "alterar consulta" list: full name + the doctor's mark.

    `service_id`/`unit_id` (TASK-086 A10): the draft's, carried by the card's scope when the
    clinic has a refused cell; without them the mark is the list rule alone."""
    return [
        (
            f"ins|{plan['name']}",
            str(plan["name"]),
            _edit_plan_mark(tenant, plan, doctor, service_id=service_id, unit_id=unit_id),
        )
        for plan in plans
    ]


def _insurance_step(conversation, tenant, body, draft, professionals, ctx):
    more = fr.insurance_more_page(body)
    if more is not None:
        return _insurance_list(draft, tenant, professionals, page=more)
    if fr._label_match(body, fr.LABEL_INSURANCE_OTHER):
        return _result(
            draft, [TextBubble(body=fr.INSURANCE_PROMPT_OTHER)], fr.STEP_EDIT_INSURANCE_OTHER
        )
    matched = fr._match_insurance_plan(tenant, body)
    if matched is None and fr._says_no_insurance(body):
        matched = fr.LABEL_INSURANCE_PARTICULAR
    if matched is None:
        if fr._reads_as_conversation(body):
            return fr._preserve(conversation, "delegate_llm")
        return _insurance_list(draft, tenant, professionals)
    return _confirm_result(
        draft.with_changes(insurance=matched[:120]).with_stage(), tenant, professionals, ctx
    )


def _insurance_other_step(conversation, tenant, body, draft, professionals, ctx):
    text = (body or "").strip()
    if not text or fr._reads_as_conversation(text):
        return fr._preserve(conversation, "delegate_llm")
    return _confirm_result(
        draft.with_changes(insurance=text[:120]).with_stage(), tenant, professionals, ctx
    )


# --- Patient --------------------------------------------------------------------------


def _attendee_question_result(draft: ae.EditDraft) -> fr.FlowRouterResult:
    return _result(
        draft.with_stage(),
        [
            fr.MenuBubble(
                body=ATTENDEE_QUESTION_BODY, labels=[LABEL_ATTENDEE_SELF, LABEL_ATTENDEE_OTHER]
            )
        ],
        fr.STEP_EDIT_ATT_CHOICE,
    )


def _attendee_step(conversation, tenant, body, draft, professionals, ctx):
    """pra-quem -> [name -> authorization] -> the confirmation (same wording as booking)."""
    step = conversation.flow_step
    match = fr._label_match
    if step == fr.STEP_EDIT_ATT_CHOICE:
        if match(body, LABEL_ATTENDEE_SELF):
            return _confirm_result(
                draft.with_changes(attendee_name=None), tenant, professionals, ctx
            )
        if match(body, LABEL_ATTENDEE_OTHER):
            return _result(draft, [TextBubble(body=ATTENDEE_NAME_REQUEST)], fr.STEP_EDIT_ATT_NAME)
        return fr._preserve(conversation, "delegate_llm")
    if step == fr.STEP_EDIT_ATT_NAME:
        name = parse_attendee_name(body)
        # A boolean only - never the value (skill pii-field-capture-pseudonymization).
        logger.info("edit_attendee_name_answered", parsed=name is not None)
        if name is None:
            return _result(draft, [TextBubble(body=ATTENDEE_NAME_INVALID)], fr.STEP_EDIT_ATT_NAME)
        return _result(
            draft.with_stage(pending_attendee=name),
            [
                ButtonBubble(
                    body=authorization_body(name),
                    confirm_label=LABEL_ATTENDEE_AUTH_CONFIRM,
                    cancel_label=LABEL_ATTENDEE_AUTH_BACK,
                )
            ],
            fr.STEP_EDIT_ATT_AUTH,
            flow_attendee_name=name,
        )
    # STEP_EDIT_ATT_AUTH
    pending = draft.stage.get("pending_attendee")
    if not pending or match(body, LABEL_ATTENDEE_AUTH_BACK):
        return _attendee_question_result(draft)
    if match(body, LABEL_ATTENDEE_AUTH_CONFIRM):
        result = _confirm_result(
            draft.with_changes(attendee_name=pending), tenant, professionals, ctx
        )
        result.attendee_authorized = True  # the worker writes the ConsentEvent
        return result
    return fr._preserve(conversation, "delegate_llm")


def _unavailable(draft: ae.EditDraft) -> fr.FlowRouterResult:
    """The agenda refused: nothing changed, the draft stays, a human is told (as manage does)."""
    return fr.FlowRouterResult(
        action="calendar_unavailable",
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=fr.STEP_EDIT_CONFIRM,
        flow_managing_appointment_id=_uuid(draft.appointment_id),
        flow_selected_professional_id=_uuid(draft.current["professional_id"]),
        flow_edit_draft=draft.to_json(),
    )


async def _apply_confirm(
    conversation, tenant, draft, appt, professionals, ctx, patient_name
) -> fr.FlowRouterResult:
    """The final Confirmar: the ONLY place the edit reaches the calendar.

    Revalidates (the slot may have been taken since the list was drawn), then patches the
    event (same doctor) or creates it on the new doctor's agenda (the old one is deleted by
    the worker AFTER the database commit - `workers/shared/appointment_edit_apply.py`). The
    row itself is updated by the worker; this returns `appointment_edit` and never persists.
    Exception: a change of a money field (service, unit, doctor, convênio) on a PAID sinal is
    only checked here (`calendar_deferred`) - the worker decides the money first and writes the
    agenda only if the change stands now (TASK-085 R D-R1/D-R3).
    """
    changed = draft.changed()
    if not changed:
        menu = _menu_result(draft.with_stage(), ctx)
        menu.bubbles.insert(0, TextBubble(body=ae.EDIT_NOTHING_CHANGED))
        return menu
    doctor = _doctor(professionals, draft)
    if draft.current["professional_id"] is not None and doctor is None:
        return _leave(tenant, professionals, TextBubble(body=ae.EDIT_STALE))
    service = fr._match_service(_services_of(tenant, doctor), draft.current["service"])
    if service is None:
        return _service_list(draft, tenant, professionals, ctx, after_doctor=True)
    problem = _placement_problem(draft, tenant, doctor)
    if problem is not None:
        # Follow-up E1: the last line of defence - the card already refuses such a draft (and
        # TASK-086: the matrix may have changed between the card and this Confirmar).
        if problem == ae.EDIT_INSURANCE_NOT_ACCEPTED:
            logger.info("appointment_edit_insurance_not_accepted", stage="confirm")
        return _refuse(_undo_placement(draft, problem), ctx, problem)
    event_id = str(appt.get("google_event_id") or "")
    try:
        new_cal = _calendar(ctx, draft, professionals, tenant)
    except CalendarOriginUnresolved:
        return _unavailable(draft)
    doctor_changed = "médico" in changed
    calendar_changed = doctor_changed and not ctx.same_calendar(
        draft.calendar_professional_id,
        draft.original_calendar_professional_id,
    )
    time_changed = "data" in changed or "horário" in changed
    if not event_id:
        return fr._preserve(conversation, "delegate_llm")
    if new_cal is None:
        return _unavailable(draft)

    tz = new_cal.tzinfo
    start, end = draft.start.replace(tzinfo=tz), draft.end.replace(tzinfo=tz)
    original_minutes = int(
        (
            datetime.fromisoformat(str(draft.original["end_at"]))
            - datetime.fromisoformat(str(draft.original["start_at"]))
        ).total_seconds()
        // 60
    )
    # Another unit has other hours: the kept time is checked there too (follow-up E1).
    window_changed = (
        time_changed
        or doctor_changed
        or "unidade" in changed
        or draft.duration_minutes != original_minutes
    )
    # TASK-087 (R4): only a changed day, time, service, doctor or unit is checked against the
    # service's weekdays - a convênio-only edit never is (`_rechecks_day`). A service change at
    # the same length leaves `window_changed` False, hence the explicit check.
    if _rechecks_day(draft) and blocked_on(start, _blocked_days(tenant, draft, doctor)):
        return await _reslot_picker(conversation, tenant, draft, professionals, ctx)
    if window_changed:
        try:
            free = await new_cal.is_slot_free(
                start, end, ignore_event_id=_ignore_event(draft, appt, ctx)
            )
        except CalendarUnavailableError:
            return _unavailable(draft)
        if free and await _held(new_cal, start, end, draft.current["professional_id"]):
            free = False
        if free and await _inside_buffer(
            new_cal,
            start,
            end,
            _ignore_event(draft, appt, ctx),
            draft.current["professional_id"],
            professionals,
        ):
            free = False
        if not free:
            return await _reslot_picker(conversation, tenant, draft, professionals, ctx)

    who = draft.current["attendee_name"] or patient_name or "Paciente"
    summary = f"{draft.current['service'] or 'Consulta'} - {who}"
    description = build_event_description(
        service=draft.current["service"],
        insurance=draft.current["insurance"],
        attendee_name=draft.current["attendee_name"],
        # TASK-083 follow-up: the booking's CURRENT unit keeps its lines after "Alterar Dados".
        **event_unit_fields(_draft_unit(draft, doctor), doctor),
    )
    # A change of a MONEY field on a paid sinal is a money decision (2026-10-26 for the service;
    # TASK-085 R D-R1/D-R3 for the unit, the doctor and the convênio): a dearer change may wait
    # for the Pix of the difference and must not reach the agenda before the engine decides. The
    # window was checked above (on the new doctor's / unit's agenda); the worker writes the agenda
    # - the new doctor's included - only when the change stands now
    # (`workers/shared/appointment_edit_apply.py`). Date/time/patient alone are today's edit.
    deferred = bool(ctx.paid_deposit and _money_fields_changed(draft))
    # When the window moves, exclusive on the secretarIA agenda: the free check above and
    # the write are one atomic step there (plain on Google). An edit that keeps the window
    # (convênio, who it is for) is never refused for an overlap the clinic itself made.
    try:
        if deferred:
            new_event_id, link = event_id, None
        elif calendar_changed:
            created = await create_event_exclusive(
                new_cal, start=start, end=end, summary=summary, description=description
            )
            new_event_id, link = str(created.get("id")), created.get("htmlLink")
        elif window_changed:
            await update_event_details_exclusive(
                new_cal, event_id, start, end, summary, description
            )
            new_event_id, link = event_id, None
        else:
            await new_cal.update_event_details(event_id, start, end, summary, description)
            new_event_id, link = event_id, None
    except CalendarSlotTakenError:
        return await _reslot_picker(conversation, tenant, draft, professionals, ctx)
    except CalendarUnavailableError:
        return _unavailable(draft)

    edit = {
        "appointment_id": UUID(draft.appointment_id),
        "old_google_event_id": event_id,
        "google_event_id": new_event_id,
        "google_event_link": link,
        "appointment_type": draft.current["service"],
        "professional_id": _uuid(draft.current["professional_id"]),
        "old_professional_id": _uuid(draft.original["professional_id"]),
        "old_google_calendar_source": draft.original.get("google_calendar_source"),
        "insurance": draft.current["insurance"],
        "attendee_name": draft.current["attendee_name"],
        "start_at": start,
        "end_at": end,
        "time_changed": time_changed,
        "doctor_changed": doctor_changed,
        "calendar_changed": calendar_changed,
        "original": dict(draft.original),
        "calendar": new_cal,
        "patient_name": patient_name,
        "calendar_deferred": deferred,
        # TASK-086 A18: the canonical service the worker re-checks the convênio matrix for,
        # inside the row lock (never serialized into a paid difference's `pending_edit`).
        "service_id": entry_service_id(service),
    }
    # TASK-083: a doctor change that moved the booking's unit writes the new one on the row
    # (`appointment_edit_write`); without that the key is absent and the unit stays as it is.
    if draft.current.get("unit_id") != draft.original.get("unit_id"):
        edit["unit_id"] = _uuid(draft.current.get("unit_id"))
    doctor = _doctor(professionals, draft)
    unit, address = _recap_place(draft, doctor, tenant)
    recap = ae.build_edit_recap(
        draft,
        doctor=str(getattr(doctor, "name", "") or "") or None,
        address=address,
        requirements=_requirements(tenant, professionals, draft),
        title=ae.EDIT_APPLIED,
        unit=unit,
    )
    logger.info("appointment_edit_confirmed", changed=len(changed), doctor_changed=doctor_changed)
    return fr.FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=recap), *fr._menu_bubbles(tenant, professionals)],
        flow_state=FlowState.MENU,
        appointment_edit=edit,
    )


# --- Dispatcher -----------------------------------------------------------------------


def _moved_event_of(conversation, appointments) -> str | None:
    """The `google_event_id` of the appointment being edited: it never pads its own new time."""
    appt = fr._find_appt_by_id(appointments, fr._managing_appt_id_str(conversation))
    return str((appt or {}).get("google_event_id") or "") or None


async def edit_step(
    conversation,
    tenant,
    body: str,
    appointments: list[dict],
    professionals: list | None,
    ctx: ae.EditContext | None,
    patient_name: str | None = None,
) -> fr.FlowRouterResult:
    # TASK-088 Task 5 (R32): every day / slot list of this turn ignores the booking being moved.
    with fr.moving_event_scope(_moved_event_of(conversation, appointments)):
        return await _edit_step_inner(
            conversation, tenant, body, appointments, professionals, ctx, patient_name
        )


async def _edit_step_inner(
    conversation,
    tenant,
    body: str,
    appointments: list[dict],
    professionals: list | None,
    ctx: ae.EditContext | None,
    patient_name: str | None = None,
) -> fr.FlowRouterResult:
    """Route one inbound turn while the conversation is in `FlowState.EDIT_BOOKING`.

    Not tied to the clinic's reminder switch (owner 2026-10-27): "Alterar Dados" rides on
    the card every patient gets on entering the conversation, reminders on or off.
    """
    draft = ae.EditDraft.from_json(getattr(conversation, "flow_edit_draft", None))
    # TASK-085 R (contrato §3.8): first, so the patient reads why even though the suspended
    # consultation is hidden from the list the worker hands in (S5).
    if ctx is not None and ctx.suspended:
        return _suspended_refusal()
    appt = fr._find_appt_by_id(appointments, fr._managing_appt_id_str(conversation))
    if draft is None or appt is None or ctx is None or str(appt.get("id")) != draft.appointment_id:
        return _leave(tenant, professionals, TextBubble(body=ae.EDIT_STALE))
    try:
        _ = draft.original_calendar_professional_id
    except CalendarOriginUnresolved:
        return _unavailable(draft)
    latest = ae.EditDraft.from_appointment(appt, _tz(tenant))
    if latest.original != draft.original or latest.start.replace(
        tzinfo=_tz(tenant)
    ) <= datetime.now(_tz(tenant)):
        return _leave(tenant, professionals, TextBubble(body=ae.EDIT_STALE))
    step = conversation.flow_step
    match = fr._label_match

    # TASK-085 R D-R3: a paid sinal no longer restricts any field (Confirmar hands every money
    # change to the worker). Only the Pix reschedule limit still keeps date and time.
    time_steps = (*fr._EDIT_DAY_STEPS, fr.STEP_EDIT_TIME_TOO, fr.STEP_EDIT_SLOT)
    if ctx.reschedule_blocked and (
        {"data", "horário"}.intersection(draft.changed()) or step in time_steps
    ):
        # The limit can be reached after this card was sent: the time goes back, the rest stays.
        fields = {key: draft.original[key] for key in ("start_at", "end_at")}
        return _menu_result(draft.with_changes(**fields).with_stage(), ctx)

    # An offered menu entry remains a valid change of intention inside ANY picker.
    if step != fr.STEP_EDIT_MENU:
        if match(body, ae.LABEL_EDIT_DATE) and not ctx.reschedule_blocked:
            return await _begin_date(conversation, tenant, draft, professionals, ctx)
        if match(body, ae.LABEL_EDIT_TIME) and not ctx.reschedule_blocked:
            return await _time_entry(conversation, tenant, draft, professionals, ctx)
        if match(body, ae.LABEL_EDIT_SERVICE):
            return _service_list(draft, tenant, professionals, ctx)
        if match(body, ae.LABEL_EDIT_DOCTOR):
            return await _doctor_list(draft, tenant, appt, professionals, ctx)
        if match(body, ae.LABEL_EDIT_MORE):
            # A staged choice (a unit list's doctor/service) is dropped, never carried along.
            return _more_result(draft.with_stage(), ctx)
        if match(body, ae.LABEL_EDIT_INSURANCE):
            return _insurance_list(draft, tenant, professionals)
        if match(body, ae.LABEL_EDIT_PATIENT):
            return _attendee_question_result(draft)

    if step == fr.STEP_EDIT_MENU:
        if match(body, ae.LABEL_EDIT_DATE) and not ctx.reschedule_blocked:
            return await _begin_date(conversation, tenant, draft, professionals, ctx)
        if match(body, ae.LABEL_EDIT_TIME) and not ctx.reschedule_blocked:
            return await _time_entry(conversation, tenant, draft, professionals, ctx)
        if match(body, ae.LABEL_EDIT_SERVICE):
            return _service_list(draft.with_stage(), tenant, professionals, ctx)
        if match(body, ae.LABEL_EDIT_DOCTOR):
            return await _doctor_list(draft, tenant, appt, professionals, ctx)
        if match(body, ae.LABEL_EDIT_MORE):
            return _more_result(draft.with_stage(), ctx)
        return fr._preserve(conversation, "delegate_llm")

    if step == fr.STEP_EDIT_MORE:
        if match(body, ae.LABEL_EDIT_INSURANCE):
            return _insurance_list(draft, tenant, professionals)
        if match(body, ae.LABEL_EDIT_PATIENT):
            return _attendee_question_result(draft)
        if match(body, ae.LABEL_EDIT_BACK):
            return _menu_result(draft.with_stage(), ctx)
        return fr._preserve(conversation, "delegate_llm")

    if step in fr._EDIT_DAY_STEPS:
        return await _day_step(conversation, tenant, body, draft, appt, professionals, ctx)

    if step == fr.STEP_EDIT_TIME_TOO:
        return await _time_too_step(conversation, tenant, body, draft, appt, professionals, ctx)

    if step == fr.STEP_EDIT_SLOT:
        return await _slot_step(conversation, tenant, body, draft, appt, professionals, ctx)

    if step == fr.STEP_EDIT_CONFIRM:
        if match(body, fr.LABEL_CANCEL):
            return _leave(tenant, professionals, TextBubble(body=ae.EDIT_KEPT))
        if match(body, ae.LABEL_EDIT_MORE_DATA):
            return _menu_result(draft.with_stage(), ctx)
        if match(body, fr.LABEL_CONFIRM):
            return await _apply_confirm(
                conversation, tenant, draft, appt, professionals, ctx, patient_name
            )
        return fr._preserve(conversation, "delegate_llm")

    if step == fr.STEP_EDIT_SERVICE:
        return await _service_step(conversation, tenant, body, draft, appt, professionals, ctx)
    if step == fr.STEP_EDIT_DOCTOR:
        return await _doctor_step(conversation, tenant, body, draft, appt, professionals, ctx)
    if step == fr.STEP_EDIT_UNIT:
        return await _unit_step(conversation, tenant, body, draft, appt, professionals, ctx)
    if step == fr.STEP_EDIT_INSURANCE:
        return _insurance_step(conversation, tenant, body, draft, professionals, ctx)
    if step == fr.STEP_EDIT_INSURANCE_OTHER:
        return _insurance_other_step(conversation, tenant, body, draft, professionals, ctx)
    if step in (fr.STEP_EDIT_ATT_CHOICE, fr.STEP_EDIT_ATT_NAME, fr.STEP_EDIT_ATT_AUTH):
        return _attendee_step(conversation, tenant, body, draft, professionals, ctx)

    return fr._preserve(conversation, "delegate_llm")


async def redisplay_edit(conversation, tenant, appointments, professionals, ctx):
    """Redraw this stage without changing either draft snapshot (including staged values)."""
    with fr.moving_event_scope(_moved_event_of(conversation, appointments)):
        return await _redisplay_edit_inner(conversation, tenant, appointments, professionals, ctx)


async def _redisplay_edit_inner(conversation, tenant, appointments, professionals, ctx):
    draft = ae.EditDraft.from_json(getattr(conversation, "flow_edit_draft", None))
    if draft is None:
        return fr._preserve(conversation, "reply")
    ctx = ctx or ae.EditContext()
    if ctx.suspended:
        return _suspended_refusal()
    appt = fr._find_appt_by_id(appointments, draft.appointment_id) or {}
    step = conversation.flow_step
    if step == fr.STEP_EDIT_MENU:
        result = _menu_result(draft, ctx)
    elif step == fr.STEP_EDIT_MORE:
        result = _more_result(draft, ctx)
    elif step == fr.STEP_EDIT_SERVICE:
        result = _service_list(
            draft,
            tenant,
            professionals,
            ctx,
            after_doctor=bool(draft.stage.get("after_doctor")),
            at_unit=_stage_unit(draft, _flow_doctor(professionals, draft)),
        )
    elif step == fr.STEP_EDIT_UNIT:
        result = _unit_list_again(draft, tenant, professionals, ctx)
    elif step == fr.STEP_EDIT_DOCTOR:
        result = await _doctor_list(draft, tenant, appt, professionals, ctx)
    elif step in (fr.STEP_EDIT_INSURANCE, fr.STEP_EDIT_INSURANCE_OTHER):
        result = (
            _insurance_list(draft, tenant, professionals)
            if step == fr.STEP_EDIT_INSURANCE
            else _result(draft, [TextBubble(body=fr.INSURANCE_PROMPT_OTHER)], step)
        )
    elif step == fr.STEP_EDIT_CONFIRM:
        result = _confirm_result(draft, tenant, professionals, ctx, check=False)
    elif step == fr.STEP_EDIT_TIME_TOO:
        result = _result(
            draft,
            [
                fr.MenuBubble(
                    body=ae.EDIT_TIME_TOO_BODY, labels=[ae.LABEL_TIME_TOO_YES, ae.LABEL_TIME_TOO_NO]
                )
            ],
            step,
        )
    elif step == fr.STEP_EDIT_SLOT:
        day = datetime.fromisoformat(
            conversation.flow_selected_day or draft.start.date().isoformat()
        )
        result = await _slots_for(conversation, tenant, draft, day, professionals, ctx)
    elif step in fr._EDIT_DAY_STEPS:
        result = await fr.enter_day_picker(
            _carrier(conversation, draft),
            tenant,
            _calendar(ctx, draft, professionals, tenant),
            duration_minutes=draft.duration_minutes,
            branch=fr.EDIT_DAY_BRANCH,
            anchor=draft.start,
            professionals=professionals,
        )
    elif step == fr.STEP_EDIT_ATT_AUTH and draft.stage.get("pending_attendee"):
        result = _result(
            draft,
            [
                ButtonBubble(
                    body=authorization_body(draft.stage["pending_attendee"]),
                    confirm_label=LABEL_ATTENDEE_AUTH_CONFIRM,
                    cancel_label=LABEL_ATTENDEE_AUTH_BACK,
                )
            ],
            step,
            flow_attendee_name=draft.stage["pending_attendee"],
        )
    elif step == fr.STEP_EDIT_ATT_NAME:
        result = _result(draft, [TextBubble(body=ATTENDEE_NAME_REQUEST)], step)
    else:
        result = _attendee_question_result(draft)
    # Pickers may delegate when an agenda is down. The failed AI must still answer
    # within the same edit, never invoke itself again or return a general menu.
    if result.action != "reply":
        result = _result(draft, [TextBubble(body=ae.EDIT_TIME_BUSY)], step)
    result.flow_edit_draft = draft.to_json()
    result.flow_step = step
    result.flow_selected_day = getattr(conversation, "flow_selected_day", None)
    result.flow_selected_slot = getattr(conversation, "flow_selected_slot", None)
    return result


async def apply_ai_edit(conversation, tenant, proposal, appointments, professionals, ctx):
    """Validate an AI proposal and land it in the same edit; NEVER apply/confirm it."""
    with fr.moving_event_scope(_moved_event_of(conversation, appointments)):
        return await _apply_ai_edit_inner(
            conversation, tenant, proposal, appointments, professionals, ctx
        )


async def _apply_ai_edit_inner(conversation, tenant, proposal, appointments, professionals, ctx):

    async def unchanged():
        return await redisplay_edit(conversation, tenant, appointments, professionals, ctx)

    if ctx is not None and ctx.suspended:
        return _suspended_refusal()
    draft = ae.EditDraft.from_json(getattr(conversation, "flow_edit_draft", None))
    appt = fr._find_appt_by_id(appointments, fr._managing_appt_id_str(conversation))
    if draft is None or ctx is None or appt is None or str(appt.get("id")) != draft.appointment_id:
        return await unchanged()
    if ae.EditDraft.from_appointment(appt, _tz(tenant)).original != draft.original:
        return await unchanged()
    if draft.start.replace(tzinfo=_tz(tenant)) <= datetime.now(_tz(tenant)):
        return await unchanged()
    if not isinstance(proposal, dict):
        return await unchanged()
    allowed = {
        "open_field",
        "professional",
        "service",
        "day",
        "time",
        "insurance",
        "attendee",
        "unit",
        "keep",
        "finish",
        "abandon",
    }
    if set(proposal) - allowed:
        return await unchanged()
    for key in allowed - {"keep", "finish", "abandon"}:
        if proposal.get(key) is not None and not isinstance(proposal[key], str):
            return await unchanged()
    if any(not isinstance(proposal.get(k, False), bool) for k in ("finish", "abandon")):
        return await unchanged()
    keep = proposal.get("keep") or []
    if not isinstance(keep, list) or any(
        not isinstance(k, str)
        or k not in {"day", "time", "service", "doctor", "insurance", "patient"}
        for k in keep
    ):
        return await unchanged()
    keep_keys = {
        "day": "day",
        "time": "time",
        "service": "service",
        "doctor": "professional",
        "insurance": "insurance",
        "patient": "attendee",
    }
    if any(proposal.get(keep_keys[field]) for field in keep):
        return await unchanged()
    # A paid sinal: the AI only DRAFTS (the patient still confirms, and Confirmar hands every
    # money change to the worker - TASK-085 R D-R3: the doctor and the convênio too).
    if ctx.reschedule_blocked and any(proposal.get(k) for k in ("day", "time")):
        return await unchanged()
    new = draft
    doctor = _doctor(professionals, draft)
    # Follow-up E1 (D5): a unit NAME, checked against the doctor's bookable units where the
    # service is done. "" (the tool's default) is no proposal.
    unit_name = (proposal.get("unit") or "").strip()
    if (
        unit_name
        and getattr(conversation, "flow_step", None) == fr.STEP_EDIT_UNIT
        and not proposal.get("professional")
        and not proposal.get("service")
    ):
        # A bare unit on the edit's unit list answers THAT list, exactly like a typed name.
        answered = await _unit_step(
            conversation, tenant, unit_name, draft, appt, professionals, ctx
        )
        return answered if answered.action == "reply" else await unchanged()
    # A service/unit named while a staged doctor's list is open is for THAT doctor.
    staged = _uuid(draft.stage.get("pending_doctor"))
    proposes_doctor = bool(proposal.get("professional"))
    staged_unit = None
    if staged is not None and not proposes_doctor and (proposal.get("service") or unit_name):
        doctor = fr._find_professional_by_id(professionals, staged)
        if doctor is None:
            return await unchanged()
        proposes_doctor = True
        new = _with_doctor(new, doctor)
        # Follow-up final review: the unit that open list was drawn for (the patient may have
        # picked it on "Alterar unidade") - never re-derived from the booking's own unit.
        staged_unit = _stage_unit(draft, doctor)
    if proposal.get("professional"):
        matches = [
            p
            for p in professionals or []
            if fr._match_professional([p], proposal["professional"]) is not None
        ]
        doctor = matches[0] if len(matches) == 1 else None
        if doctor is None:
            return await unchanged()
        new = _with_doctor(new, doctor)
    if unit_name and not flow_units.offered_units(doctor):
        unit_name = ""  # a clinic (or doctor) without units: the name is ignored, not fatal
    if proposes_doctor and not proposal.get("service") and unit_name:
        # Follow-up final review: a doctor (proposed, or the open list's) + a unit, no service.
        # Not one of his units -> the same choice as the tap. His unit, but he does not do the
        # booking's service there -> his services AT that unit (step b), the unit kept.
        unit = flow_units.match_unit(_doctor_units(tenant, doctor), unit_name)
        if unit is None:
            return await _doctor_choice(
                conversation, tenant, draft, doctor, appt, professionals, ctx
            )
        current = fr._match_service(_services_of(tenant, doctor), draft.current["service"])
        if current is None or flow_units.unit_refused(
            doctor, unit.id, entry_service_id(current)
        ):
            return _service_list(
                draft.with_stage(pending_doctor=str(doctor.id)),
                tenant,
                professionals,
                ctx,
                at_unit=unit,
            )
    elif proposes_doctor and not proposal.get("service"):
        # Follow-up E1 review: a doctor alone goes through the same choice as the tap (b)/D2;
        # only when it keeps today's rule does the rest of the proposal go on below.
        routed = await _doctor_choice(
            conversation, tenant, draft, doctor, appt, professionals, ctx, today_ok=True
        )
        if routed is not None:
            return routed
    if proposal.get("service"):
        service = fr._match_service(_services_of(tenant, doctor), proposal["service"])
        if service is None:
            return await unchanged()
        new = new.with_changes(service=str(service["name"]))
    service = fr._match_service(_services_of(tenant, doctor), new.current["service"])
    if service is not None and (proposes_doctor or proposal.get("service") or unit_name):
        # TASK-083 final review: the unit follows the doctor FOR this service, and a service
        # is never drafted at a unit where it is not done (same rule as the guided steps).
        service_id = entry_service_id(service)
        if proposes_doctor:
            new = _with_doctor(new, doctor, service_id)
        # Follow-up E1 (D5): never a silent no-op - the (a) unit list for this service, the
        # proposed doctor staged (`pending_doctor`), nothing drafted until a unit is picked.
        pending = str(doctor.id) if proposes_doctor else None
        notice = ae.EDIT_DOCTOR_SERVICE_NO_UNIT if proposes_doctor else ae.EDIT_SERVICE_NOT_AT_UNIT
        unit_list = _service_unit_list(
            draft.with_stage(),
            tenant,
            professionals,
            ctx,
            str(service["name"]),
            refuse_from=draft,
            notice=notice,
            pending=pending,
        )
        if unit_name:
            unit = flow_units.match_unit(
                flow_units.units_for_service(doctor, service_id), unit_name
            )
            if unit is None:
                return unit_list
            new = new.with_changes(unit_id=str(unit.id))
        elif staged_unit is not None:
            # Follow-up final review: the open list's unit if the service is done there, else
            # the units where it is (the doctor stays staged).
            if flow_units.unit_refused(doctor, staged_unit.id, service_id):
                return unit_list
            new = new.with_changes(unit_id=str(staged_unit.id))
        # Unit problems only: a convênio the matrix refuses is refused by the card below
        # (`_confirm_result`, TASK-086 A11), with its own words - never a unit-list notice.
        elif _unit_refused(new, doctor, service_id) or _unit_problem(new, tenant, doctor):
            return unit_list
        # TASK-085: the length at the unit settled above (the new unit's own, else the base).
        minutes = _minutes_at(service, tenant, doctor, new.current.get("unit_id"))
        new = new.with_changes(end_at=_iso(new.start + timedelta(minutes=minutes)))
    if proposal.get("insurance"):
        insurance = fr._match_insurance_plan(tenant, proposal["insurance"])
        if insurance is None and fr._says_no_insurance(proposal["insurance"]):
            insurance = fr.LABEL_INSURANCE_PARTICULAR
        if insurance is None:
            return await unchanged()
        new = new.with_changes(insurance=insurance)
    if proposal.get("day") or proposal.get("time"):
        try:
            day = (
                datetime.strptime(proposal["day"], "%Y-%m-%d").date()
                if proposal.get("day")
                else new.start.date()
            )
            hour = (
                datetime.strptime(proposal["time"], "%H:%M").time()
                if proposal.get("time")
                else new.start.time()
            )
            start = datetime.combine(day, hour)
        except ValueError:
            return await unchanged()
        end = start + timedelta(minutes=new.duration_minutes)
        if start.replace(tzinfo=_tz(tenant)) <= datetime.now(_tz(tenant)):
            return await unchanged()
        if not await _slot_free(
            _calendar(ctx, new, professionals, tenant),
            start,
            end,
            _ignore_event(new, appt, ctx),
            new.current["professional_id"],
            professionals,
        ):
            # Keep the prior slot until a replacement is actually available and chosen.
            return await _reslot_picker(conversation, tenant, new, professionals, ctx)
        new = new.with_changes(start_at=_iso(start), end_at=_iso(end))
    if proposal.get("attendee") not in (None, "self", "other"):
        return await unchanged()
    if proposal.get("attendee") == "self":
        new = new.with_changes(attendee_name=None)
    if proposal.get("attendee") == "other":
        return _result(new, [TextBubble(body=ATTENDEE_NAME_REQUEST)], fr.STEP_EDIT_ATT_NAME)
    if proposal.get("abandon") and not new.changed():
        return _leave(tenant, professionals, TextBubble(body=ae.EDIT_KEPT))
    fields = {
        "date": ae.LABEL_EDIT_DATE,
        "time": ae.LABEL_EDIT_TIME,
        "service": ae.LABEL_EDIT_SERVICE,
        "doctor": ae.LABEL_EDIT_DOCTOR,
        "insurance": ae.LABEL_EDIT_INSURANCE,
        "patient": ae.LABEL_EDIT_PATIENT,
        "more": ae.LABEL_EDIT_MORE,
    }
    opened = proposal.get("open_field")
    if opened:
        if (
            opened not in fields
            or (ctx.reschedule_blocked and opened in {"date", "time"})
        ):
            return await unchanged()
        from types import SimpleNamespace

        carrier = SimpleNamespace(
            **{
                **vars(conversation),
                "flow_edit_draft": new.to_json(),
                "flow_step": fr.STEP_EDIT_MORE
                if opened in {"insurance", "patient"}
                else fr.STEP_EDIT_MENU,
            }
        )
        return await edit_step(carrier, tenant, fields[opened], appointments, professionals, ctx)
    if service is None:
        return _service_list(new, tenant, professionals, ctx, after_doctor=True)
    if new.changed() or keep or proposal.get("finish") or proposal.get("abandon"):
        if {"médico", "serviço", "unidade"}.intersection(new.changed()):
            return await _after_slot_affecting_change(
                conversation, tenant, new, appt, professionals, ctx
            )
        return _confirm_result(new, tenant, professionals, ctx)
    return await unchanged()
