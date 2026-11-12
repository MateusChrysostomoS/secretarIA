"""Agenda - one real PERSON's calendar, shared by every clinic they work at (TASK-083).

A `Professional` belongs to ONE tenant, so a doctor who works at two clinics that both use the
secretarIA has two `professionals` rows - and, before this table, two agendas that could not see
each other (two clinics could book the same hour of the same person). An `Agenda` is the thing
those rows have in common: `professionals.agenda_id` points every row of one person at the same
`agendas` row, and services/agenda.py reads the siblings' busy time through it.

## Identity

`person_key` is `str(StaffAccount.id)` - the brain-api account id, the single source of identity
(docs: brain-api-sso-boundary). It is NEVER an e-mail or a name, and it is never accepted from a
client: the link is made server side from brain-api's `professional-accounts` answer
(services/agenda_linking.py). UNIQUE, so two linkings of one account converge on one row.

## Privacy

The row carries no tenant, name or clinic: a sibling's busy time is exposed as bare
`(start, end)` windows only (services/agenda.py). Deleting an agenda (never done by code) leaves
its professionals as they are today: `professionals.agenda_id` is ON DELETE SET NULL.

Without an `agenda_id` every path is identical to before this table existed (spec §3).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class Agenda(Base):
    """The shared calendar identity of one person across clinics."""

    __tablename__ = "agendas"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # str(StaffAccount.id) from brain-api; UNIQUE so linking is idempotent.
    person_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
