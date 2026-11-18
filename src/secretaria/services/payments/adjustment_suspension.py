"""Which consultations are SUSPENDED by a change waiting for the Pix of its difference (TASK-085 R).

D-R4: in a clinic that books only AFTER the sinal, a change to a dearer service/unit/doctor/
convênio leaves the old consultation's row LIVE (its slot stays taken) while the Pix of the
difference is open - and while the patient chooses after it expired (`awaiting_choice`). During
that time the patient must not see the old consultation (S5: upcoming lists, reminders, the AI)
and the clinic may only "Liberar horário" (D-R9). The ONE definition:

    an appointment is suspended  <=>  a `deposit_adjustments` row of the same clinic with
    `appointment_id` = it, `mode` = "suspend" and `status` in SUSPENDED_STATUSES exists.

A predicate, not an appointment status: `appointment_status` is a native Postgres enum read by
many places (the hub among them, which must keep seeing the row) - see mapa.md §1.

3rd round (D-R10/D-R11, mapa-u.md §1.3): the suspended consultation's EVENT leaves the doctor's
agenda, so its row must not occupy the slot either. `occupies_slot_clause` (= NOT suspended) is
what the three "is this slot taken by a LIVE row?" queries add (`agenda.shared_busy`,
`service_change._window_fits`, `release_keep._consultation_overlaps`). SUSPENDED_STATUSES gained
`restoring` and `old_slot_lost`, so every reader here covers them without a change.

Imports models only, on purpose: `patient_context` and the reminder queries add this clause, and
the payments package itself imports them. `adjustment_queries` re-exports every name here.
"""

from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from secretaria.models import Appointment
from secretaria.models.deposit_adjustment import (
    MODE_SUSPEND,
    SUSPENDED_STATUSES,
    DepositAdjustment,
)

# Fix round 1 (Orchestrator ruling): what the patient reads when they try to confirm, reschedule or
# edit a SUSPENDED consultation (an old reminder's button, the edit flow). Cancelling stays allowed
# and behaves as "Cancelar ambas" (the normal cancellation settles the pending change).
SUSPENDED_VISIT_TEXT = (
    "Essa consulta está aguardando o Pix da diferença da troca que você pediu. "
    "Use os botões da mensagem do Pix ou aguarde o prazo."
)


def suspended_clause(
    appointment_id: InstrumentedAttribute | ColumnElement = Appointment.id,
    tenant_id: InstrumentedAttribute | ColumnElement = Appointment.tenant_id,
) -> ColumnElement[bool]:
    """TRUE for an appointment row that is suspended. Correlated to the given columns - by
    default `Appointment.id` / `Appointment.tenant_id`; for an aliased appointments table pass the
    alias's columns (`a = aliased(Appointment); suspended_clause(a.id, a.tenant_id)`)."""
    return exists(
        select(DepositAdjustment.id).where(
            DepositAdjustment.appointment_id == appointment_id,
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.mode == MODE_SUSPEND,
            DepositAdjustment.status.in_(SUSPENDED_STATUSES),
        )
    )


def visible_to_patient_clause(
    appointment_id: InstrumentedAttribute | ColumnElement = Appointment.id,
    tenant_id: InstrumentedAttribute | ColumnElement = Appointment.tenant_id,
) -> ColumnElement[bool]:
    """TRUE for an appointment row the patient may see/act on: NOT suspended. What S5 adds to
    every patient-facing query of upcoming consultations and reminders (same alias rule)."""
    return ~suspended_clause(appointment_id, tenant_id)


def occupies_slot_clause(
    appointment_id: InstrumentedAttribute | ColumnElement = Appointment.id,
    tenant_id: InstrumentedAttribute | ColumnElement = Appointment.tenant_id,
) -> ColumnElement[bool]:
    """TRUE for an appointment row that may take its slot: NOT suspended (D-R10 - a suspended
    consultation is out of the agenda while the difference Pix is open). Add it to every query
    that reads LIVE `appointments` rows as busy time; same alias rule as `suspended_clause`.
    The same rule as `visible_to_patient_clause`, named for what the occupancy readers mean."""
    return ~suspended_clause(appointment_id, tenant_id)


async def is_suspended(session: AsyncSession, *, tenant_id: UUID, appointment_id: UUID) -> bool:
    """Whether this clinic's appointment is suspended right now (one row; the hub's 409 guard)."""
    found = await session.scalar(
        select(DepositAdjustment.id)
        .where(
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.appointment_id == appointment_id,
            DepositAdjustment.mode == MODE_SUSPEND,
            DepositAdjustment.status.in_(SUSPENDED_STATUSES),
        )
        .limit(1)
    )
    return found is not None


async def open_suspension(
    session: AsyncSession, *, tenant_id: UUID, appointment_id: UUID
) -> DepositAdjustment | None:
    """The change that suspends this clinic's appointment right now (newest first), or None.
    For the patient paths that must refuse a mutation (S3a/S3b) and the texts that name it."""
    return await session.scalar(
        select(DepositAdjustment)
        .where(
            DepositAdjustment.tenant_id == tenant_id,
            DepositAdjustment.appointment_id == appointment_id,
            DepositAdjustment.mode == MODE_SUSPEND,
            DepositAdjustment.status.in_(SUSPENDED_STATUSES),
        )
        .order_by(DepositAdjustment.created_at.desc())
        .limit(1)
    )


__all__ = [
    "SUSPENDED_VISIT_TEXT",
    "is_suspended",
    "occupies_slot_clause",
    "open_suspension",
    "suspended_clause",
    "visible_to_patient_clause",
]
