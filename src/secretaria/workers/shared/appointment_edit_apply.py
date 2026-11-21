"""Apply a confirmed "Alterar Dados" edit to the SAME appointment row (TASK-032 R6).

`apply_appointment_edit` runs inside `_apply_flow_result`'s transaction (so a failed
persist rolls the whole edit back together with the flow state); `_finish_edit` runs
AFTER the commit, best-effort, like R3's `replacement._finish_replacement`: the old
Google event of a doctor change is deleted on the agenda that owned it, and a failure is
logged and never undoes the edit the patient just confirmed.

TASK-085 R (S3b, tasks/TASK-085/sinal-trocas/contrato.md): the money of an edit.

* PAID sinal and a MONEY field changes (service, unit, doctor, convênio - the one predicate
  `service_change_policy.money_fields_changed`): `_apply_paid_change` hands the WHOLE target to
  `service_change.request_paid_change`. It writes the row itself when the change stands now
  (`applied_now`, `asked`, `applied_awaiting_payment`) and this module then moves the agenda -
  on the new doctor's agenda when the calendar changes (created here, the old event deleted by
  `_finish_edit` after the commit). `suspended` / `awaiting_payment` / `blocked` write nothing:
  an agenda the router already wrote (`calendar_deferred` False) is put back in line with the
  row in the same transaction. Works for a router that deferred the agenda and for one that
  did not. 3rd round (D-R10): after the commit of a `suspended` change the old consultation's
  event leaves the agenda (`AppliedEdit.vacate_adjustment_id` -> `old_slot.vacate_old_slot`).
* UNPAID sinal (AWAITING) and a money field changes (D-R6): the edit is written, the agenda
  follows, and `deposit_lifecycle.requote_awaiting` runs as the LAST fallible step (contrato
  §3.7). `payment_in_flight` / `void_failed` (or an exception) undo the edit (savepoint) and the
  reply says why (`AppliedEdit.refusal`); `voided` asks `flow_runner` for
  `recharge_after_edit` after the commit (`AppliedEdit.recharge`).
* NO sinal on the consultation, the clinic charges one and a money field changes (OQ-10):
  `recharge_after_edit(old_amount_cents=None)` after the commit decides whether one is due.
* A SUSPENDED consultation (a change waiting for the Pix of its difference) is never edited by
  the patient: `refusal` = `SUSPENDED_VISIT_TEXT`, nothing written.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Tenant,
    is_live_status,
)
from secretaria.services import reminder_hooks
from secretaria.services.appointment_edit import (
    EDIT_INSURANCE_NOT_ACCEPTED,
    appointment_email_version,
)
from secretaria.services.appointment_edit_write import (
    row_draft as _row_draft,
    write_appointment_edit,
)
from secretaria.services.appointment_status import SOURCE_FLOW
from secretaria.services.calendar import CalendarUnavailableError, build_event_description
from secretaria.services.insurance_acceptance import booking_refused, log_refusal
from secretaria.services.local_calendar import create_event_exclusive
from secretaria.services.patient_context import as_utc
from secretaria.services.payments import (
    adjustment_suspension,
    deposit_lifecycle,
    old_slot,
    service_change,
)
from secretaria.services.payments.service_change import ServiceChangeResult
from secretaria.services.payments.service_change_policy import (
    MONEY_FIELD_DOCTOR,
    MONEY_FIELD_INSURANCE,
    MoneyTerms,
    money_fields_changed,
)
from secretaria.services.professional_edit_outbox import record_professional_edit
from secretaria.services.tenant_config import list_active_professionals
from secretaria.services.unit_booking import booking_unit_fields
from secretaria.workers.shared.appointment_edit_support import edit_guards
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
)

logger = get_logger(__name__)

# What the engine wrote on the row NOW (the caller owns the agenda of these, contrato §3.6).
_WRITES_NOW = ("applied_now", "asked", "applied_awaiting_payment")
# The LEGACY deferred mode (`awaiting_payment`, mode NULL: a "before" clinic without room for a
# useful Pix) applies only service / time / unit when paid (S1 `apply_paid_adjustment`): a change
# of doctor, convênio or who the visit is for would be charged and never written - refused.
LEGACY_NO_ROOM_TEXT = (
    "Não dá para fazer essa troca tão perto da consulta. Sua consulta continua como estava. "
    "Para mudar, fale com a clínica."
)
# The confirmation card's line for the same case (shown before Confirmar, no Pix promised).
LEGACY_NO_ROOM_CARD_TEXT = (
    "Essa troca não pode ser feita tão perto da consulta. Para mudar, fale com a clínica."
)


@dataclass(frozen=True)
class RechargeAfterEdit:
    """`deposit_lifecycle.recharge_after_edit` to run after the commit (TASK-085 R D-R6 / OQ-10).
    `old_amount_cents` None = the consultation had no sinal."""

    old_amount_cents: int | None


@dataclass(frozen=True)
class AppliedEdit:
    appointment_id: UUID
    moved: bool  # the time changed AND reminders exist/are enabled -> replan them
    old_event_id: str | None  # delete after the commit (a doctor change)
    old_professional_id: UUID | None
    notice_id: str | None = None
    notice_version: str | None = None
    changed_fields: tuple[str, ...] = ()
    old_google_calendar_source: str | None = None
    # A7: set only when the start moved - the "Consulta remarcada" mail to the clinic
    # and the Portal patient (plugins/appointment_change_notifications.py).
    previous_start_at: datetime | None = None
    new_start_at: datetime | None = None
    # 2026-10-26 / TASK-085 R: a money-field change on a paid sinal - what the money decided
    # (`service_change.request_paid_change`); the reply follows it (flow_runner).
    service_change: ServiceChangeResult | None = None
    # TASK-085 R: NOTHING was written (the edit was undone in this transaction); the reply is
    # this one line and the agenda goes back to the committed row (flow_runner compensates).
    refusal: str | None = None
    # TASK-085 R D-R6 / OQ-10: the sinal to (re)charge once the edit is committed.
    recharge: RechargeAfterEdit | None = None
    # TASK-085 R D-R10 (mapa-u §1.4): a `suspended` change - the OLD consultation's event leaves
    # the agenda after the commit (`_finish_edit` -> `old_slot.vacate_old_slot`).
    vacate_adjustment_id: UUID | None = None


def _window(appointment) -> tuple[datetime, datetime]:
    return as_utc(appointment.start_at), as_utc(appointment.end_at or appointment.start_at)


def _calendar_changed(edit: dict) -> bool:
    return bool(edit.get("calendar_changed", edit.get("doctor_changed")))


def _money_target(edit: dict) -> dict:
    """The edit's money terms in `MoneyTerms.with_edit` keys (an empty service keeps the row's,
    as `write_appointment_edit` does; `unit_id` only when the router moved the unit)."""
    target = {key: edit[key] for key in ("professional_id", "insurance") if key in edit}
    if edit.get("appointment_type"):
        target["appointment_type"] = edit["appointment_type"]
    if "unit_id" in edit:
        target["unit_id"] = edit["unit_id"]
    return target


def money_changes(appointment, edit: dict) -> frozenset[str]:
    """Which money fields (contrato §3.4) this edit changes on this row."""
    terms = MoneyTerms.of_appointment(appointment)
    return money_fields_changed(terms, terms.with_edit(_money_target(edit)))


async def _sync_agenda(session, tenant, appointment, edit: dict, window_before) -> None:
    """The agenda write the router deferred, through the one helper every service change uses.
    Raises on a refusal or an outage: the caller's transaction rolls the edit back."""
    calendar = edit.get("calendar")
    if calendar is None:
        raise CalendarUnavailableError("edit_calendar_missing")
    await service_change.sync_agenda_event(
        session,
        tenant,
        appointment,
        calendar,
        patient_name=edit.get("patient_name"),
        window_changed=_window(appointment) != window_before,
        # TASK-088 Task 5: this is the PATIENT's edit - held to the doctor's buffer.
        buffer_for_patient=True,
    )


async def _create_on_new_agenda(session, tenant, appointment, edit: dict) -> None:
    """A deferred change of agenda (another doctor) that stands now: the event is created on the
    new agenda (exclusively on the secretarIA one; the router checked it free at Confirmar) and
    the row re-pointed to it. The old event is deleted after the commit (`_finish_edit`); a
    rollback removes only this new one (`compensate_appointment_edit` reads `edit`)."""
    calendar = edit.get("calendar")
    if calendar is None:
        raise CalendarUnavailableError("edit_calendar_missing")
    description = build_event_description(
        service=appointment.appointment_type,
        insurance=appointment.insurance,
        attendee_name=appointment.attendee_name,
        **await booking_unit_fields(
            session, tenant.id, getattr(appointment, "unit_id", None), appointment.professional_id
        ),
    )
    created = await create_event_exclusive(
        calendar,
        start=as_utc(appointment.start_at),
        end=as_utc(appointment.end_at or appointment.start_at),
        summary=service_change.agenda_summary(appointment, edit.get("patient_name")),
        description=description,
    )
    new_event_id = str(created.get("id") or "").strip()
    if not new_event_id:
        raise CalendarUnavailableError("edit_event_without_id")
    edit["google_event_id"] = new_event_id
    edit["google_event_link"] = created.get("htmlLink")
    appointment.google_event_id = new_event_id
    appointment.google_event_link = created.get("htmlLink")
    appointment.google_calendar_source = (
        "professional" if appointment.professional_id is not None else "clinic"
    )


async def _write_agenda_now(session, tenant, appointment, edit: dict, window_before) -> None:
    """The agenda of an edit the router DEFERRED, now that the row holds it."""
    if _calendar_changed(edit):
        await _create_on_new_agenda(session, tenant, appointment, edit)
    else:
        await _sync_agenda(session, tenant, appointment, edit, window_before)


async def _restore_router_agenda(session, tenant, appointment, edit: dict) -> None:
    """The router already wrote the agenda (`calendar_deferred` False) of a change that did NOT
    stand now: its new event on another agenda is removed; the same event is put back exactly on
    the row (plain write - the window is the row's own, still ours). Raises on a failure (the
    transaction rolls back and `compensate_appointment_edit` tries again)."""
    calendar = edit.get("calendar")
    if calendar is None:
        raise CalendarUnavailableError("edit_calendar_missing")
    if _calendar_changed(edit):
        new_event = edit.get("google_event_id")
        if new_event and new_event != edit.get("old_google_event_id"):
            await calendar.cancel_event(new_event)
        edit["google_event_id"] = edit.get("old_google_event_id")
        edit["google_event_link"] = None
        return
    await service_change.sync_agenda_event(
        session,
        tenant,
        appointment,
        calendar,
        patient_name=edit.get("patient_name"),
        window_changed=False,
    )


# TASK-085 R (S7a): the edit dict carries, past the rollback, a Pix of the difference the provider
# refused to void (`service_change.UnseenCharge`); `flow_runner` hands it to
# `record_unvoided_charge` once the transaction is gone.
UNVOIDED_CHARGE_KEY = "_unvoided_charge"


async def _void_unseen_charge(
    session, tenant, deposit, result: ServiceChangeResult, edit: dict
) -> None:
    """The edit rolls back after the engine created a Pix the patient never saw: void it at the
    provider (`service_change.void_unseen_charge`, no session write). A refused void is kept on
    `edit` so the clinic gets ONE `unseen_charge_to_void` item after the rollback
    (`record_unvoided_charge`) - the charge may still be payable. Never raises."""
    try:
        charge = await service_change.unseen_charge_of(
            session, tenant=tenant, deposit=deposit, result=result
        )
        if charge is None:
            return
        outcome = await service_change.void_unseen_charge(
            session, tenant_id=tenant.id, charge=charge
        )
    except Exception as exc:  # noqa: BLE001 - the rollback goes on regardless
        logger.warning("appointment_edit_charge_void_failed", error_type=type(exc).__name__)
        return
    if outcome == "failed":
        edit[UNVOIDED_CHARGE_KEY] = charge


async def record_unvoided_charge(edit: dict | None) -> None:
    """AFTER the rollback of an edit (`flow_runner`): the clinic's item for the Pix of the
    difference `_void_unseen_charge` could not void. Its own session; never raises."""
    charge = (edit or {}).get(UNVOIDED_CHARGE_KEY)
    if charge is None:
        return
    try:
        async with async_session_factory() as session:
            await service_change.open_unvoided_charge_item(session, charge)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - logged; the provider's own expiry still applies
        logger.error(
            "appointment_edit_unvoided_charge_item_failed",
            tenant_id=str(charge.tenant_id),
            error_type=type(exc).__name__,
        )


def legacy_cannot_carry(appointment, edit: dict, changed: frozenset[str]) -> bool:
    """Does this change hold a part the legacy deferred mode would never write (doctor,
    convênio, who the visit is for)? `edit` needs `attendee_name` only when it may change."""
    return bool(changed & {MONEY_FIELD_DOCTOR, MONEY_FIELD_INSURANCE}) or (
        "attendee_name" in edit
        and (edit.get("attendee_name") or None) != (appointment.attendee_name or None)
    )


def legacy_without_room(tenant, appointment, new_start, now: datetime) -> bool:
    """A dearer change on this clinic would fall into the LEGACY deferred mode: a "before"
    clinic with no room left for a minimally useful Pix before the visits (contrato §1, the
    engine's own `suspend_pix_expiry` rule)."""
    if not service_change.timing_is_before(tenant):
        return False
    ttl = tenant.deposit_hold_ttl_minutes or service_change.DEFAULT_PIX_TTL_MINUTES
    return (
        service_change.suspend_pix_expiry(
            now, ttl, appointment.start_at, new_start or appointment.start_at
        )
        is None
    )


async def _legacy_refused(
    session, tenant, appointment, deposit, edit: dict, changed: frozenset[str]
) -> bool:
    """Decided BEFORE the engine (no Pix is ever created for it): the change is dearer, would
    fall into the legacy deferred mode and holds a part that mode never writes. S4 answers 409
    `no_room` for the same case on the staff side."""
    if not legacy_cannot_carry(appointment, edit, changed):
        return False
    now = datetime.now(UTC)
    new_start = edit.get("start_at") or appointment.start_at
    if not legacy_without_room(tenant, appointment, new_start, now):
        return False
    target = {}
    if "unit_id" in edit:
        target["unit_id"] = edit["unit_id"]
    if "insurance" in edit:
        target["insurance"] = edit["insurance"]
    decision = await service_change.preview_service_change(
        session,
        tenant=tenant,
        appointment=appointment,
        deposit=deposit,
        new_service=edit.get("appointment_type") or appointment.appointment_type,
        professional_id=edit.get("professional_id", appointment.professional_id),
        start_at=new_start,
        now=now,
        **target,
    )
    return decision.action == "increase_charge"


async def _apply_paid_change(
    session, *, tenant, current_tenant, appointment, edit: dict, window_before, changed
) -> AppliedEdit | None:
    """A change of a MONEY field on a PAID sinal (2026-10-26; TASK-085 R D-R1..D-R5): the engine
    decides with the WHOLE target and, when the change stands now, writes the row and the
    doctor's ONE notice itself. Then the agenda (deferred) - or, when the change does not stand
    now, the router's agenda back to the row (not deferred)."""
    deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
    if deposit is None or deposit.tenant_id != current_tenant.id:
        return None
    deferred = bool(edit.get("calendar_deferred"))
    previous_start = appointment.start_at
    if await _legacy_refused(session, current_tenant, appointment, deposit, edit, changed):
        # Never charge for a change the payment would not write: refused before any Pix;
        # `flow_runner` puts the router's agenda back (refusal) and says why.
        logger.info("appointment_edit_legacy_no_room", tenant_id=str(current_tenant.id))
        return _refused(appointment.id, LEGACY_NO_ROOM_TEXT)
    result = await service_change.request_paid_change(
        session,
        tenant=current_tenant,
        appointment=appointment,
        deposit=deposit,
        new_service=edit.get("appointment_type") or appointment.appointment_type,
        pending_edit=edit,
        now=datetime.now(UTC),
        source=SOURCE_FLOW,
    )
    if result.kind not in _WRITES_NOW:
        # suspended / awaiting_payment (legacy) / blocked: the row and the old event stay.
        if not deferred:
            try:
                await _restore_router_agenda(session, current_tenant, appointment, edit)
            except Exception:
                await _void_unseen_charge(session, current_tenant, deposit, result, edit)
                raise
        return AppliedEdit(
            appointment_id=appointment.id,
            moved=False,
            old_event_id=None,
            old_professional_id=None,
            service_change=result,
            # D-R10: only a suspended change frees the old slot (the legacy deferred mode and a
            # blocked change keep the old consultation on the agenda).
            vacate_adjustment_id=(
                result.adjustment_id if result.kind == "suspended" else None
            ),
        )
    time_changed = bool(edit["time_changed"])
    try:
        if time_changed:
            allowed, _count = await deposit_lifecycle.register_reschedule(
                session, tenant=current_tenant, appointment=appointment
            )
            if not allowed:
                await _void_unseen_charge(session, current_tenant, deposit, result, edit)
                return None
        if deferred:
            await _write_agenda_now(session, current_tenant, appointment, edit, window_before)
    except Exception:
        await _void_unseen_charge(session, current_tenant, deposit, result, edit)
        raise
    return AppliedEdit(
        appointment_id=appointment.id,
        moved=time_changed
        and (reminder_hooks.enabled_for(tenant) or (appointment.confirmation_count or 0) > 0),
        old_event_id=edit.get("old_google_event_id") if _calendar_changed(edit) else None,
        old_professional_id=edit.get("old_professional_id"),
        old_google_calendar_source=edit.get("old_google_calendar_source"),
        notice_id=str(result.notice_id) if result.notice_id else None,
        notice_version=appointment_email_version(appointment) if result.changed_fields else None,
        changed_fields=result.changed_fields,
        previous_start_at=previous_start if time_changed else None,
        new_start_at=appointment.start_at if time_changed else None,
        service_change=result,
    )


def _refused(appointment_id: UUID, text: str) -> AppliedEdit:
    return AppliedEdit(
        appointment_id=appointment_id,
        moved=False,
        old_event_id=None,
        old_professional_id=None,
        refusal=text,
    )


async def _insurance_refused_now(session, tenant_id, appointment, edit: dict) -> bool:
    """TASK-086 A18: the edit's final (doctor, service, unit, convênio) is a cell the clinic's
    matrix refuses NOW - read from the database inside the caller's row lock, so a refusal
    saved between the card and "Confirmar" still wins. Only when one of the four changed: a
    pure reschedule never is (the appointment itself is never questioned)."""
    unit_id = edit["unit_id"] if "unit_id" in edit else appointment.unit_id
    if (
        edit["appointment_type"] == appointment.appointment_type
        and edit["professional_id"] == appointment.professional_id
        and edit["insurance"] == appointment.insurance
        and unit_id == appointment.unit_id
    ):
        return False
    refused = await booking_refused(
        session,
        tenant_id=tenant_id,
        professional_id=edit["professional_id"],
        service_id=edit.get("service_id"),
        unit_id=unit_id,
        insurance=edit["insurance"],
    )
    if refused:
        log_refusal(
            "appointment_edit_insurance_refused",
            tenant_id=tenant_id,
            appointment_id=appointment.id,
            professional_id=edit["professional_id"],
            service_id=edit.get("service_id"),
            unit_id=unit_id,
        )
    return refused


async def apply_appointment_edit(
    session,
    *,
    tenant,
    tenant_id,
    patient_id,
    edit: dict,
) -> AppliedEdit | None:
    """Write the edit onto the live appointment; None (nothing written) when it is not live."""
    appointment = await session.scalar(
        select(Appointment)
        .where(
            Appointment.id == edit["appointment_id"],
            Appointment.tenant_id == tenant_id,
            Appointment.patient_id == patient_id,
        )
        .with_for_update()
    )
    current_tenant = await session.get(Tenant, tenant_id)
    # Not tied to the clinic's reminder switch (owner 2026-10-27): "Alterar Dados" rides on the
    # card every patient gets on entering the conversation, reminders on or off.
    if (
        appointment is None
        or current_tenant is None
        or not is_live_status(appointment.status)
        or as_utc(appointment.start_at) <= datetime.now(UTC)
    ):
        logger.info(
            "appointment_edit_skipped", tenant_id=str(tenant_id), found=appointment is not None
        )
        return None
    before = _row_draft(appointment, current_tenant.timezone)
    if edit.get("original") is not None:
        if (
            before.original != edit["original"]
            or appointment.google_event_id != edit["old_google_event_id"]
        ):
            return None
    # TASK-085 R (contrato §3.8): a consultation waiting for the Pix of a change's difference is
    # never edited by the patient (an old card) - one line, nothing written.
    if await adjustment_suspension.is_suspended(
        session, tenant_id=tenant_id, appointment_id=appointment.id
    ):
        logger.info("appointment_edit_suspended_refused", tenant_id=str(tenant_id))
        return _refused(appointment.id, adjustment_suspension.SUSPENDED_VISIT_TEXT)
    # Serialize the money counter too: a stale card cannot consume the same allowance twice.
    deposit = await session.scalar(
        select(PixDeposit)
        .where(
            PixDeposit.appointment_id == appointment.id,
            PixDeposit.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    paid, blocked = await edit_guards(session, current_tenant, appointment)
    changed = money_changes(appointment, edit)
    deferred = bool(edit.get("calendar_deferred"))
    window_before = _window(appointment)
    if blocked and edit["time_changed"]:
        return None
    # TASK-086 A18 (W8/W9): the convênio matrix, fresh, before any write or money decision. A
    # refusal is NOT a lost row (`None` reads as a calendar outage: "agenda indisponível", a human
    # handover and a false alert to the clinic): the patient is told why, the agenda goes back to
    # the committed row (T9 review I1).
    if await _insurance_refused_now(session, tenant_id, appointment, edit):
        return _refused(appointment.id, EDIT_INSURANCE_NOT_ACCEPTED)
    if paid and changed:
        # The money decides (services/payments/service_change.py), for every money field.
        return await _apply_paid_change(
            session,
            tenant=tenant,
            current_tenant=current_tenant,
            appointment=appointment,
            edit=edit,
            window_before=window_before,
            changed=changed,
        )
    # D-R6: an AWAITING sinal whose amount may move - the edit runs in a savepoint so a refused
    # re-quote undoes it alone (the flow state of the turn still commits).
    requote = (
        bool(changed) and deposit is not None and deposit.status == PixDepositStatus.AWAITING
    )
    old_amount_cents = deposit.amount_cents if requote else None
    appointment_id = appointment.id
    savepoint = await session.begin_nested() if requote else None
    if edit["time_changed"]:
        allowed, _count = await deposit_lifecycle.register_reschedule(
            session,
            tenant=current_tenant,
            appointment=appointment,
        )
        if not allowed:
            return None
    previous_start = appointment.start_at
    fields = await write_appointment_edit(
        session,
        appointment,
        tenant_id=tenant_id,
        timezone=current_tenant.timezone,
        edit=edit,
        source=SOURCE_FLOW,
        idempotency_key=f"edit:{appointment.id}:{edit['start_at'].isoformat()}",
    )
    moved = bool(edit["time_changed"]) and (
        reminder_hooks.enabled_for(tenant) or (appointment.confirmation_count or 0) > 0
    )
    notice = await record_professional_edit(session, appointment, fields)
    old_event_id = edit["old_google_event_id"] if _calendar_changed(edit) else None
    if deferred:
        # The router left the agenda to us (the sinal stopped being paid since): same helpers,
        # inside this transaction - a refusal rolls the edit back (`flow_runner` compensates).
        await _write_agenda_now(session, current_tenant, appointment, edit, window_before)
    recharge = None
    if requote:
        # Contrato §3.7: the LAST fallible step before the commit (a voided Pix cannot come back).
        await session.flush()
        try:
            outcome = await deposit_lifecycle.requote_awaiting(
                session,
                tenant=current_tenant,
                appointment=appointment,
                deposit=deposit,
                now=datetime.now(UTC),
            )
        except Exception as exc:  # noqa: BLE001 - an exception counts as void_failed (§3.7)
            logger.warning("appointment_edit_requote_failed", error_type=type(exc).__name__)
            outcome = "void_failed"
        if outcome in ("payment_in_flight", "void_failed"):
            # The rollback expires the row: only the id read before it is used from here on.
            await savepoint.rollback()
            logger.info("appointment_edit_requote_refused", outcome=outcome)
            return _refused(appointment_id, deposit_lifecycle.requote_blocked_text(outcome))
        await savepoint.commit()
        if outcome == "voided":
            recharge = RechargeAfterEdit(old_amount_cents=old_amount_cents)
    elif changed and deposit is None and current_tenant.pix_deposit_enabled:
        # OQ-10: no sinal yet; `recharge_after_edit` decides whether the edit made one due.
        recharge = RechargeAfterEdit(old_amount_cents=None)
    return AppliedEdit(
        appointment_id=appointment.id,
        moved=moved,
        old_event_id=old_event_id,
        old_professional_id=edit.get("old_professional_id"),
        old_google_calendar_source=edit.get("old_google_calendar_source"),
        notice_id=str(notice.id) if notice else None,
        notice_version=appointment_email_version(appointment) if fields else None,
        changed_fields=tuple(fields),
        previous_start_at=previous_start if edit["time_changed"] else None,
        new_start_at=appointment.start_at if edit["time_changed"] else None,
        recharge=recharge,
    )


async def compensate_appointment_edit(tenant, patient_id, edit: dict) -> None:
    """Restore Calendar to the committed row, or remove only our uncommitted new event.

    Another turn may have won the race. Restore its current committed values rather than
    overwriting that success with our old snapshot. Never remove the old event on failure.
    """
    calendar = edit.get("calendar")
    if calendar is None:
        return
    try:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(Appointment).where(
                    Appointment.id == edit["appointment_id"],
                    Appointment.tenant_id == tenant.id,
                    Appointment.patient_id == patient_id,
                )
            )
            if edit.get("calendar_changed", edit["doctor_changed"]):
                if row is None or row.google_event_id != edit["google_event_id"]:
                    await calendar.cancel_event(edit["google_event_id"])
                return
            if row is None or row.google_event_id != edit["google_event_id"]:
                return
            if not is_live_status(row.status):
                await calendar.cancel_event(row.google_event_id)
                return
            patient = await session.get(Patient, patient_id)
            who = row.attendee_name or (patient.name if patient is not None else None) or "Paciente"
            start, end = as_utc(row.start_at), as_utc(row.end_at)
            summary = f"{row.appointment_type or 'Consulta'} - {who}"
            description = build_event_description(
                service=row.appointment_type,
                insurance=row.insurance,
                attendee_name=row.attendee_name,
                # TASK-083 follow-up: the committed row's CURRENT unit keeps its lines.
                **await booking_unit_fields(session, tenant.id, row.unit_id, row.professional_id),
            )
        await calendar.update_event_details(
            edit["google_event_id"], start, end, summary, description
        )
    except Exception as exc:
        logger.error(
            "appointment_edit_compensation_failed",
            appointment_id=str(edit["appointment_id"]),
            error_type=type(exc).__name__,
        )


async def _vacate_old_slot(tenant, applied: AppliedEdit) -> None:
    """TASK-085 R D-R10: a suspended change - the OLD consultation's event leaves the doctor's
    agenda now that the change committed (`old_slot.vacate_old_slot`: own session, never raises;
    a failure stays `releasing` and the engine's sweep retries). Never raises."""
    if applied.vacate_adjustment_id is None:
        return
    try:
        outcome = await old_slot.vacate_old_slot(tenant.id, applied.vacate_adjustment_id)
    except Exception as exc:  # noqa: BLE001 - defence only: vacate_old_slot never raises
        logger.warning("appointment_edit_vacate_failed", error_type=type(exc).__name__)
        return
    logger.info(
        "appointment_edit_old_slot_vacated",
        tenant_id=str(tenant.id),
        adjustment_id=str(applied.vacate_adjustment_id),
        outcome=outcome,
    )


async def _finish_edit(tenant, applied: AppliedEdit) -> None:
    """Delete the old Google event of a doctor change on its own agenda; a suspended change's
    old event leaves the agenda (`_vacate_old_slot`). Never raises."""
    await _vacate_old_slot(tenant, applied)
    if not applied.old_event_id:
        return
    try:
        async with async_session_factory() as session:
            rows = await list_active_professionals(session, tenant.id)
            target = _appointment_calendar_target(
                {"professional_id": applied.old_professional_id,
                 "google_calendar_source": applied.old_google_calendar_source}, rows
            )
            calendar = await _appointment_calendar(session, tenant, target)
        if calendar is None:
            logger.warning(
                "appointment_edit_calendar_missing",
                tenant_id=str(tenant.id),
                appointment_id=str(applied.appointment_id),
            )
            return
        await calendar.cancel_event(applied.old_event_id)
    except Exception as exc:
        logger.warning(
            "appointment_edit_old_event_delete_failed",
            tenant_id=str(tenant.id),
            appointment_id=str(applied.appointment_id),
            error_type=type(exc).__name__,
        )
