"""BookingHold - the slot a Brain-Message visitor holds while they prove their address.

## Why this table exists

Until 2026-09-20 the appointment was created the instant the patient tapped
"Confirmar", and the 6-digit code was an OFFER that arrived afterwards
(`plugins/pending_identity.py`, and the "not a gate" paragraph in its
docstring). The owner reverted that decision after seeing the consequence in
production: an unproven visitor could fill a real agenda, and the secretarIA
could be talked into claiming the code was verified when nothing had been
verified at all.

So the code is now a GATE, and this row is what makes a gate possible without
losing the slot the patient chose. Between "Confirmar" and the verified code
the booking exists ONLY here:

    hold row (this table)  ->  code verified  ->  Google event + Appointment

The order is load-bearing and is the one defect this table was explicitly
designed around: the DB row comes FIRST and the Google event LAST, never the
other way round. The previous wave produced orphans in exactly the opposite
order (an event on Google with nothing in the database, see the "Limpeza e
pendências" section of `docs/CHECKPOINT_secretaria_email_otp_inline.md`); a
hold that is never promoted leaves nothing behind but a row that stops being
read the second `expires_at` passes.

## Expiry is a READ rule, not a job

Nothing has to run for a hold to stop blocking a slot. Every reader filters on
`expires_at > now()`, so an abandoned hold frees its window by the passage of
time alone. `services/booking_hold.py::place_hold` deletes this tenant's
already-expired rows opportunistically, which keeps the table small without
making correctness depend on a sweeper that might not be running.

`expires_at` is set to 10 minutes from creation — deliberately the same
10-minute life brain-api gives the OTP challenge itself
(`brain-api/docs/CHECKPOINT_portal_sessao_pendente.md` §5.4). One clock, so a
code can never outlive the slot it is meant to confirm, nor the reverse.

## Scope

Brain-Message only. A WhatsApp patient is identified by a phone number Meta
already verified, has no address to prove, and never reaches the gate — so no
WhatsApp booking ever writes a row here. The table is still read on every
channel, because a slot held by a Portal visitor must be invisible to a
WhatsApp patient too: a reservation that only half the channels respect is not
a reservation.

Carries no PII: ids, a window, a service label. The patient's name is not
copied here — `summary` is rebuilt at promotion time from the Patient row.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class BookingHold(Base):
    """One slot reserved for one conversation, until `expires_at`."""

    __tablename__ = "booking_holds"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # The conversation that owns this hold. CASCADE rather than SET NULL: a
    # hold with no conversation has nobody to promote it and nobody to tell,
    # so it is garbage, not history. At most one live hold per conversation
    # (`services/booking_hold.py::place_hold` replaces its own).
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Whose agenda the window belongs to. NULL = the tenant-level agenda, the
    # same meaning it has on `appointments.professional_id`, so the overlap
    # test below compares like with like.
    professional_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"), nullable=True, index=True
    )
    appointment_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # The convênio label the patient picked, carried so promotion writes the
    # same appointment the ungated path would have written.
    insurance: Mapped[str | None] = mapped_column(String(120), nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # The shape every read uses: "live holds on this agenda, in this
        # window". Not a UNIQUE constraint on (tenant, professional, start):
        # `professional_id` is nullable, and Postgres treats NULLs as distinct,
        # so such a constraint would silently stop protecting exactly the
        # single-professional tenants that are the majority here. The conflict
        # check is done in `place_hold`, inside one transaction, where a NULL
        # compares the way the product means it.
        Index("ix_booking_holds_agenda_window", "tenant_id", "professional_id", "expires_at"),
    )
