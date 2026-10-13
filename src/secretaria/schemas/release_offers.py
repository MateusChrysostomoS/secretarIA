"""Wire shapes of the released-consultations list (TASK-052 -> TASK-054). A whitelist: the patient
is a display name; never a phone, an e-mail, a charge id or a payer."""

from datetime import datetime

from pydantic import BaseModel


class ReleaseOfferOut(BaseModel):
    id: str
    released_appointment_id: str
    new_appointment_id: str | None
    conversation_id: str | None
    patient_display_name: str | None
    professional_id: str | None
    service_label: str | None
    slot_start: datetime | None
    slot_end: datetime | None
    amount_cents: int
    deposit_paid_at_release: bool
    deposit_status: str  # the deposit's CURRENT status (PixDepositStatus value)
    refund_status: str  # not_due | due | marked | received | not_received | clinic_decided
    refund_source: str | None  # patient | timeout | keep_refused | keep_timeout
    # none | rescheduling | keep_requested | kept | keep_declined | rebooked | cancelled |
    # no_answer_cancelled | not_offered
    decision: str
    # no_answer | keep_requested | kept | keep_declined | rescheduled | cancelled |
    # cancelled_no_answer | received | not_received | not_offered
    patient_decision: str
    keep_deadline_at: datetime | None
    released_at: datetime
    decide_by_at: datetime
    expires_at: datetime
    first_contact_at: datetime | None
    refund_due_at: datetime | None
    refund_marked_at: datetime | None
    patient_answered_at: datetime | None
    can_mark_refunded: bool
    can_decide_keep: bool
    # Owner 2026-10-12: a "Remarcar" reused the paid deposit - never refund it (409 on marking).
    deposit_reused: bool
    carry_check: str | None  # reusable | refunded | unknown (provider check at the confirmation)
    # "Sinal reaproveitado — não devolver" / "Sinal novo cobrado na remarcação — devolva o sinal
    # anterior" / None
    state_note: str | None


class ReleaseOfferListOut(BaseModel):
    items: list[ReleaseOfferOut]
