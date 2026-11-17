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

Imports models only, on purpose: `patient_context` and the reminder queries add this clause, and
the payments package itself imports them. `adjustment_queries` re-exports every name here.
"""

from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from secretaria.models import Appointment
from secretaria.models.deposit_adjustment import (
    MODE_SUSPEND,
    SUSPENDED_STATUSES,
    DepositAdjustment,
)


def suspended_clause() -> ColumnElement[bool]:
    """TRUE for an `Appointment` row that is suspended. Correlated to `Appointment`: use it in a
    query that selects from `appointments` (`select(Appointment).where(suspended_clause())`)."""
    return exists(
        select(DepositAdjustment.id).where(
            DepositAdjustment.appointment_id == Appointment.id,
            DepositAdjustment.tenant_id == Appointment.tenant_id,
            DepositAdjustment.mode == MODE_SUSPEND,
            DepositAdjustment.status.in_(SUSPENDED_STATUSES),
        )
    )


def visible_to_patient_clause() -> ColumnElement[bool]:
    """TRUE for an `Appointment` row the patient may see/act on: NOT suspended. What S5 adds to
    every patient-facing query of upcoming consultations and reminders."""
    return ~suspended_clause()


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


__all__ = ["is_suspended", "suspended_clause", "visible_to_patient_clause"]
