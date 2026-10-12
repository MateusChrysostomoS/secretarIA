"""Store an edit notice in the caller's appointment transaction, never commit here."""

from uuid import uuid4

from sqlalchemy import func, select, update

from secretaria.models import ProfessionalEditNotice
from secretaria.services.appointment_edit import appointment_email_version


async def record_professional_edit(session, appointment, fields):
    if not fields:
        return None
    # Caller holds the appointment row lock. A monotonic revision also distinguishes
    # A -> B -> A: content hashes alone would revive the first A's obsolete notice.
    revision = await session.scalar(
        select(func.max(ProfessionalEditNotice.revision)).where(
            ProfessionalEditNotice.tenant_id == appointment.tenant_id,
            ProfessionalEditNotice.appointment_id == appointment.id,
        )
    )
    await session.execute(
        update(ProfessionalEditNotice)
        .where(
            ProfessionalEditNotice.tenant_id == appointment.tenant_id,
            ProfessionalEditNotice.appointment_id == appointment.id,
            ProfessionalEditNotice.status == "pending",
        )
        .values(status="skipped", last_error_code="superseded")
    )
    row = ProfessionalEditNotice(
        id=uuid4(),
        tenant_id=appointment.tenant_id,
        appointment_id=appointment.id,
        revision=(revision or 0) + 1,
        version=appointment_email_version(appointment),
        changed_fields=list(fields),
    )
    session.add(row)
    await session.flush()
    return row
