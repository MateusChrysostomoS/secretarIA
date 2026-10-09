"""Rows and clinic setup shared by the TASK-032 R4 warning tests.

Builds the row exactly as R2's engine leaves it after a send (`status='sent'`,
`warn_kind='unconfirmed'`, `warn_due_at` already past), so the tests exercise
only R4's selection, claim and e-mail.
"""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from secretaria.models import AppointmentReminder, Tenant
from tests._reminders_v2 import NOW, World

_DEFAULT = object()


async def add_warnable_row(
    db,
    world: World,
    *,
    kind: str = "day",
    status: str = "sent",
    warn_kind: str | None = "unconfirmed",
    warn_due_at=_DEFAULT,
    warned_at: datetime | None = None,
    appointment_start_at: datetime | None = None,
    tenant_id: UUID | None = None,
) -> UUID:
    due = NOW - timedelta(hours=3)
    async with db() as session:
        row = AppointmentReminder(
            id=uuid4(),
            tenant_id=tenant_id or world.tenant.id,
            appointment_id=world.appointment.id,
            patient_id=world.patient.id,
            kind=kind,
            appointment_start_at=appointment_start_at or world.start_at,
            due_at=due,
            status=status,
            channel="whatsapp",
            with_prompt=True,
            attempts=1,
            sent_at=due if status == "sent" else None,
            warn_due_at=NOW - timedelta(minutes=1) if warn_due_at is _DEFAULT else warn_due_at,
            warned_at=warned_at,
            warn_kind=warn_kind,
        )
        session.add(row)
        await session.commit()
        return row.id


async def set_contact_email(db, tenant_id: UUID, email: str | None) -> None:
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.contact_email = email
        await session.commit()
