"""The "Alterar Dados" flow: change an existing appointment through a draft (TASK-032 R6).

Spec: docs/superpowers/specs/2026-10-07-lembretes-alterar-dados-design.md.

`FlowState.EDIT_BOOKING` + `Conversation.flow_edit_draft`. Every step builds a NEW
`EditDraft` and returns it on the result; nothing touches the real appointment until the
final Confirmar (`_apply_confirm`, added with the apply step). Like the rest of the
router this is pure: calendars, the Pix guards and the appointment snapshot arrive as
arguments (`EditContext`, `appointments`, `professionals`), never read here.

This module sits beside `flow_router` and uses its private helpers on purpose - the day
and slot pickers, the label matching and the service/doctor/convênio matching exist
once, there. `flow_router._route` imports THIS module lazily (one dispatch line), which
is what keeps the import graph acyclic.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import (
    MAX_INTERACTIVE_BODY_CHARS,
    MAX_LIST_ROW_DESCRIPTION_CHARS,
    truncate_plain,
)
from secretaria.models import FlowState
from secretaria.services import appointment_edit as ae, flow_router as fr, reminder_hooks
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
from secretaria.services.calendar import CalendarUnavailableError, build_event_description
from secretaria.services.tenant_config import professional_appointment_types

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
    return fr._find_professional_by_id(professionals, _uuid(draft.current["professional_id"]))


def _services_of(tenant, professional) -> list[dict]:
    if professional is not None:
        return professional_appointment_types(professional, tenant)
    return fr.active_appointment_types(tenant)


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


async def _slot_free(
    calendar: Any,
    start: datetime,
    end: datetime,
    ignore: str | None,
    professional_id: str | None = None,
) -> bool:
    """Whether [start, end) is bookable on `calendar`; unknown (no agenda, outage) = not free."""
    if calendar is None:
        return False
    if await _held(calendar, start, end, professional_id):
        return False
    try:
        return bool(await calendar.is_slot_free(start, end, ignore_event_id=ignore))
    except CalendarUnavailableError:
        return False


def _ignore_event(draft: ae.EditDraft, appt: dict, ctx: ae.EditContext) -> str | None:
    """Exclude our event only when it lives on the calendar being checked."""
    same = draft.current["professional_id"] == draft.original["professional_id"]
    same = same or ctx.same_calendar(
        draft.current["professional_id"], draft.original["professional_id"]
    )
    return str(appt.get("google_event_id") or "") or None if same else None


# --- Menus ----------------------------------------------------------------------------


def _menu_labels(ctx: ae.EditContext) -> list[str]:
    labels: list[str] = []
    if not ctx.reschedule_blocked:
        labels += [ae.LABEL_EDIT_DATE, ae.LABEL_EDIT_TIME]
    if not ctx.paid_deposit:
        labels += [ae.LABEL_EDIT_SERVICE, ae.LABEL_EDIT_DOCTOR]
    labels.append(ae.LABEL_EDIT_MORE)
    return labels


def _menu_result(draft: ae.EditDraft, ctx: ae.EditContext) -> fr.FlowRouterResult:
    body = ae.EDIT_MENU_BODY
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
    labels = [] if ctx.paid_deposit else [ae.LABEL_EDIT_INSURANCE]
    labels += [ae.LABEL_EDIT_PATIENT, ae.LABEL_EDIT_BACK]
    return _result(draft, [fr.MenuBubble(body=ae.EDIT_MORE_BODY, labels=labels)], fr.STEP_EDIT_MORE)


def enter_edit_menu(tenant, appt: dict, ctx: ae.EditContext | None = None) -> fr.FlowRouterResult:
    """The entry from the reminder's "Alterar Dados": a fresh draft + the 5-option menu."""
    draft = ae.EditDraft.from_appointment(appt, _tz(tenant))
    return _menu_result(draft, ctx or ae.EditContext())


def _confirm_result(draft: ae.EditDraft, tenant, professionals) -> fr.FlowRouterResult:
    """The ONE complete confirmation message: everything, what changed, three buttons."""
    doctor = _doctor(professionals, draft)
    text = ae.build_edit_recap(
        draft,
        doctor=str(getattr(doctor, "name", "") or "") or None,
        address=(getattr(tenant, "address", None) or "").strip() or None,
        requirements=_requirements(tenant, professionals, draft),
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
        ctx.calendar_for(draft.current["professional_id"]),
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
        ctx.calendar_for(draft.current["professional_id"]),
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
    for a slot that does not exist.
    """
    calendar = ctx.calendar_for(draft.current["professional_id"])
    free = await _slot_free(
        calendar,
        draft.start,
        draft.end,
        _ignore_event(draft, appt, ctx),
        draft.current["professional_id"],
    )
    if free:
        return _confirm_result(draft, tenant, professionals)
    return await _reslot_picker(conversation, tenant, draft, professionals, ctx)


async def _reslot_picker(conversation, tenant, draft, professionals, ctx) -> fr.FlowRouterResult:
    """Day, then time, for the draft's doctor/service - behind the one-line explanation."""
    reslot = draft.with_stage(mode="reslot")
    picker = await fr.enter_day_picker(
        _carrier(conversation, reslot),
        tenant,
        ctx.calendar_for(reslot.current["professional_id"]),
        duration_minutes=reslot.duration_minutes,
        branch=fr.EDIT_DAY_BRANCH,
        anchor=reslot.start,
        professionals=professionals,
    )
    if picker.action == "reply":
        picker.bubbles.insert(0, TextBubble(body=ae.EDIT_RESLOT_NOTICE))
    return picker


async def _day_step(conversation, tenant, body, draft, appt, professionals, ctx):
    calendar = ctx.calendar_for(draft.current["professional_id"])
    if draft.stage.get("mode") == "date":
        target, _page = fr._day_from_body(body)
        if target is None and calendar is not None:
            target = fr._parse_day(body, datetime.now(calendar.tzinfo))
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
    )


async def _time_too_step(conversation, tenant, body, draft, appt, professionals, ctx):
    raw = draft.stage.get("target_day")
    if not raw:
        return _menu_result(draft.with_stage(), ctx)
    day = datetime.fromisoformat(raw)
    if fr._label_match(body, ae.LABEL_TIME_TOO_YES):
        return await _slots_for(conversation, tenant, draft, day, professionals, ctx)
    if fr._label_match(body, ae.LABEL_TIME_TOO_NO):
        start = datetime.combine(day.date(), draft.start.time())
        end = start + timedelta(minutes=draft.duration_minutes)
        calendar = ctx.calendar_for(draft.current["professional_id"])
        if await _slot_free(
            calendar,
            start,
            end,
            _ignore_event(draft, appt, ctx),
            draft.current["professional_id"],
        ):
            moved = draft.with_changes(start_at=_iso(start), end_at=_iso(end))
            return _confirm_result(moved, tenant, professionals)
        picker = await _slots_for(conversation, tenant, draft, day, professionals, ctx)
        if picker.action == "reply":
            picker.bubbles.insert(0, TextBubble(body=ae.EDIT_TIME_BUSY))
        return picker
    return fr._preserve(conversation, "delegate_llm")


async def _slot_step(conversation, tenant, body, draft, appt, professionals, ctx):
    control = await fr._handle_slot_controls(
        conversation,
        tenant,
        ctx.calendar_for(draft.current["professional_id"]),
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
        draft.with_changes(start_at=_iso(start), end_at=_iso(end)), tenant, professionals
    )


# --- Service --------------------------------------------------------------------------


def _service_list(
    draft: ae.EditDraft, tenant, professionals, ctx, *, after_doctor: bool = False
) -> fr.FlowRouterResult:
    """The services of the draft's doctor only (owner, 2026-10-07)."""
    services = _services_of(tenant, _doctor(professionals, draft))
    if not services:
        menu = _menu_result(draft.with_stage(), ctx)
        menu.bubbles.insert(0, TextBubble(body=ae.EDIT_NO_SERVICES))
        return menu
    rows = [
        (f"svc|{service.get('name', '')}", fr._service_row_title(service.get("name")))
        for service in services[: fr.MAX_CATALOG_OPTION_ROWS]
    ]
    return _result(
        draft.with_stage(**({"after_doctor": "1"} if after_doctor else {})),
        [
            SlotsBubble(
                body=ae.EDIT_SERVICE_BODY,
                rows=rows,
                button_label="Ver serviços",
                section_title="Serviços",
            )
        ],
        fr.STEP_EDIT_SERVICE,
    )


async def _service_step(conversation, tenant, body, draft, appt, professionals, ctx):
    service = fr._match_service(_services_of(tenant, _doctor(professionals, draft)), body)
    if service is None:
        return fr._preserve(conversation, "delegate_llm")
    minutes = fr._service_duration(service, tenant)
    after_doctor = bool(draft.stage.get("after_doctor"))
    new = draft.with_changes(
        service=str(service.get("name")),
        end_at=_iso(draft.start + timedelta(minutes=minutes)),
    ).with_stage()
    if after_doctor or minutes != draft.duration_minutes:
        # A different length (or a new doctor) may not fit the current time: check first.
        return await _after_slot_affecting_change(
            conversation, tenant, new, appt, professionals, ctx
        )
    return _confirm_result(new, tenant, professionals)


# --- Doctor ---------------------------------------------------------------------------


async def _availability(
    calendar: Any,
    start: datetime,
    end: datetime,
    ignore: str | None,
    professional_id: str | None = None,
):
    """True / False, or None when the agenda is missing or unreachable (no mark then)."""
    if calendar is None:
        return None
    if await _held(calendar, start, end, professional_id):
        return False
    try:
        return bool(await calendar.is_slot_free(start, end, ignore_event_id=ignore))
    except CalendarUnavailableError:
        return None


async def _doctor_list(draft, tenant, appt, professionals, ctx) -> fr.FlowRouterResult:
    """Every doctor, marked free (✅) or busy (❌) at the appointment's current day and time."""
    plan = fr._selected_plan(tenant, draft.current["insurance"])
    when = draft.start.strftime("%d/%m %H:%M")
    rows: list[tuple] = []
    for professional in (professionals or [])[: fr.MAX_CATALOG_OPTION_ROWS]:
        pid = str(professional.id)
        own_event = str(appt.get("google_event_id") or "") or None
        same = pid == draft.original["professional_id"] or ctx.same_calendar(
            pid, draft.original["professional_id"]
        )
        ignore = own_event if same else None
        free = await _availability(ctx.calendar_for(pid), draft.start, draft.end, ignore, pid)
        parts: list[str] = []
        if free is not None:
            parts.append(
                f"{ae.MARK_FREE if free else ae.MARK_BUSY} {'Livre' if free else 'Ocupado'} {when}"
            )
        if plan is not None and fr._accepts_plan(tenant, professional, str(plan["id"])):
            parts.append("aceita o convênio")
        specialty = (getattr(professional, "specialty", None) or "").strip() or None
        description = (
            truncate_plain(" · ".join(parts) or (specialty or ""), MAX_LIST_ROW_DESCRIPTION_CHARS)
            or None
        )
        rows.append(
            (f"prof|{professional.id}", fr._professional_row_title(professional.name), description)
        )
    return _result(
        draft.with_stage(),
        [
            SlotsBubble(
                body=ae.EDIT_DOCTOR_BODY,
                rows=rows,
                button_label="Ver médicos",
                section_title="Médicos",
            )
        ],
        fr.STEP_EDIT_DOCTOR,
    )


async def _doctor_step(conversation, tenant, body, draft, appt, professionals, ctx):
    professional = fr._match_professional(professionals or [], body)
    if professional is None:
        return fr._preserve(conversation, "delegate_llm")
    new = draft.with_changes(professional_id=str(professional.id))
    service = fr._match_service(
        professional_appointment_types(professional, tenant), new.current["service"]
    )
    if service is None:
        # The new doctor does not offer the current service: theirs next, then the check.
        return _service_list(new, tenant, professionals, ctx, after_doctor=True)
    minutes = fr._service_duration(service, tenant)
    new = new.with_changes(end_at=_iso(new.start + timedelta(minutes=minutes))).with_stage()
    return await _after_slot_affecting_change(conversation, tenant, new, appt, professionals, ctx)


# --- Convênio -------------------------------------------------------------------------


def _insurance_list(draft: ae.EditDraft, tenant, professionals) -> fr.FlowRouterResult:
    """The clinic's plans, each marked by whether the CURRENT doctor takes it; never filtered."""
    doctor = _doctor(professionals, draft)
    rows: list[tuple] = []
    for plan in fr._tenant_insurance_plans(tenant)[: fr.MAX_INSURANCE_PLAN_ROWS]:
        name = str(plan["name"])
        description = None
        if doctor is not None and plan.get("id"):
            accepted = fr._accepts_plan(tenant, doctor, str(plan["id"]))
            description = (
                f"{ae.MARK_FREE} aceito pelo médico"
                if accepted
                else f"{ae.MARK_BUSY} não aceito pelo médico"
            )
        rows.append((f"ins|{name}", fr.truncate_list_row_title(name), description))
    rows.append(("ins|particular", fr.LABEL_INSURANCE_PARTICULAR, None))
    rows.append(("ins|outro", fr.LABEL_INSURANCE_OTHER, None))
    return _result(
        draft.with_stage(),
        [
            SlotsBubble(
                body="Qual o convênio dessa consulta?",
                rows=rows,
                button_label="Ver convênios",
                section_title="Convênios",
            )
        ],
        fr.STEP_EDIT_INSURANCE,
    )


def _insurance_step(conversation, tenant, body, draft, professionals):
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
        draft.with_changes(insurance=matched[:120]).with_stage(), tenant, professionals
    )


def _insurance_other_step(conversation, tenant, body, draft, professionals):
    text = (body or "").strip()
    if not text or fr._reads_as_conversation(text):
        return fr._preserve(conversation, "delegate_llm")
    return _confirm_result(
        draft.with_changes(insurance=text[:120]).with_stage(), tenant, professionals
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


def _attendee_step(conversation, tenant, body, draft, professionals):
    """pra-quem -> [name -> authorization] -> the confirmation (same wording as booking)."""
    step = conversation.flow_step
    match = fr._label_match
    if step == fr.STEP_EDIT_ATT_CHOICE:
        if match(body, LABEL_ATTENDEE_SELF):
            return _confirm_result(draft.with_changes(attendee_name=None), tenant, professionals)
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
        result = _confirm_result(draft.with_changes(attendee_name=pending), tenant, professionals)
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
    event_id = str(appt.get("google_event_id") or "")
    new_cal = ctx.calendar_for(draft.current["professional_id"])
    doctor_changed = "médico" in changed
    calendar_changed = doctor_changed and not ctx.same_calendar(
        draft.current["professional_id"],
        draft.original["professional_id"],
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
    if time_changed or doctor_changed or draft.duration_minutes != original_minutes:
        try:
            free = await new_cal.is_slot_free(
                start, end, ignore_event_id=_ignore_event(draft, appt, ctx)
            )
        except CalendarUnavailableError:
            return _unavailable(draft)
        if free and await _held(new_cal, start, end, draft.current["professional_id"]):
            free = False
        if not free:
            return await _reslot_picker(conversation, tenant, draft, professionals, ctx)

    who = draft.current["attendee_name"] or patient_name or "Paciente"
    summary = f"{draft.current['service'] or 'Consulta'} - {who}"
    description = build_event_description(
        service=draft.current["service"],
        insurance=draft.current["insurance"],
        attendee_name=draft.current["attendee_name"],
    )
    try:
        if calendar_changed:
            created = await new_cal.create_event(start, end, summary, description)
            new_event_id, link = str(created.get("id")), created.get("htmlLink")
        else:
            await new_cal.update_event_details(event_id, start, end, summary, description)
            new_event_id, link = event_id, None
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
    }
    doctor = _doctor(professionals, draft)
    recap = ae.build_edit_recap(
        draft,
        doctor=str(getattr(doctor, "name", "") or "") or None,
        address=(getattr(tenant, "address", None) or "").strip() or None,
        requirements=_requirements(tenant, professionals, draft),
        title=ae.EDIT_APPLIED,
    )
    logger.info("appointment_edit_confirmed", changed=len(changed), doctor_changed=doctor_changed)
    return fr.FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=recap), *fr._menu_bubbles(tenant, professionals)],
        flow_state=FlowState.MENU,
        appointment_edit=edit,
    )


# --- Dispatcher -----------------------------------------------------------------------


async def edit_step(
    conversation,
    tenant,
    body: str,
    appointments: list[dict],
    professionals: list | None,
    ctx: ae.EditContext | None,
    patient_name: str | None = None,
) -> fr.FlowRouterResult:
    """Route one inbound turn while the conversation is in `FlowState.EDIT_BOOKING`."""
    if not reminder_hooks.enabled_for(tenant):
        return _leave(tenant, professionals, TextBubble(body=ae.EDIT_KEPT))
    draft = ae.EditDraft.from_json(getattr(conversation, "flow_edit_draft", None))
    appt = fr._find_appt_by_id(appointments, fr._managing_appt_id_str(conversation))
    if draft is None or appt is None or ctx is None or str(appt.get("id")) != draft.appointment_id:
        return _leave(tenant, professionals, TextBubble(body=ae.EDIT_STALE))
    latest = ae.EditDraft.from_appointment(appt, _tz(tenant))
    if latest.original != draft.original or latest.start.replace(
        tzinfo=_tz(tenant)
    ) <= datetime.now(_tz(tenant)):
        return _leave(tenant, professionals, TextBubble(body=ae.EDIT_STALE))
    step = conversation.flow_step
    match = fr._label_match

    paid_steps = (
        fr.STEP_EDIT_SERVICE,
        fr.STEP_EDIT_DOCTOR,
        fr.STEP_EDIT_INSURANCE,
        fr.STEP_EDIT_INSURANCE_OTHER,
    )
    time_steps = (*fr._EDIT_DAY_STEPS, fr.STEP_EDIT_TIME_TOO, fr.STEP_EDIT_SLOT)
    restricted = set()
    if ctx.paid_deposit:
        restricted.update(("serviço", "médico", "convênio"))
    if ctx.reschedule_blocked:
        restricted.update(("data", "horário"))
    if (
        restricted.intersection(draft.changed())
        or (ctx.paid_deposit and step in paid_steps)
        or (ctx.reschedule_blocked and step in time_steps)
    ):
        # A deposit can be paid or its limit reached after this card was sent.
        fields = {}
        if ctx.paid_deposit:
            fields.update(
                {key: draft.original[key] for key in ("service", "professional_id", "insurance")}
            )
        if ctx.reschedule_blocked or ctx.paid_deposit:
            fields.update({key: draft.original[key] for key in ("start_at", "end_at")})
        return _menu_result(draft.with_changes(**fields).with_stage(), ctx)

    if step == fr.STEP_EDIT_MENU:
        if match(body, ae.LABEL_EDIT_DATE) and not ctx.reschedule_blocked:
            return await _begin_date(conversation, tenant, draft, professionals, ctx)
        if match(body, ae.LABEL_EDIT_TIME) and not ctx.reschedule_blocked:
            return await _slots_for(conversation, tenant, draft, draft.start, professionals, ctx)
        if match(body, ae.LABEL_EDIT_SERVICE) and not ctx.paid_deposit:
            return _service_list(draft.with_stage(), tenant, professionals, ctx)
        if match(body, ae.LABEL_EDIT_DOCTOR) and not ctx.paid_deposit:
            return await _doctor_list(draft, tenant, appt, professionals, ctx)
        if match(body, ae.LABEL_EDIT_MORE):
            return _more_result(draft.with_stage(), ctx)
        return fr._preserve(conversation, "delegate_llm")

    if step == fr.STEP_EDIT_MORE:
        if match(body, ae.LABEL_EDIT_INSURANCE) and not ctx.paid_deposit:
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
    if step == fr.STEP_EDIT_INSURANCE:
        return _insurance_step(conversation, tenant, body, draft, professionals)
    if step == fr.STEP_EDIT_INSURANCE_OTHER:
        return _insurance_other_step(conversation, tenant, body, draft, professionals)
    if step in (fr.STEP_EDIT_ATT_CHOICE, fr.STEP_EDIT_ATT_NAME, fr.STEP_EDIT_ATT_AUTH):
        return _attendee_step(conversation, tenant, body, draft, professionals)

    return fr._preserve(conversation, "delegate_llm")
