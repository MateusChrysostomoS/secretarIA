"""Response schemas of the attention-item hub endpoints (api/hub/payment_attention.py).

WHITELISTS: nothing here can carry a provider charge id, a payer name, a document, a phone, a
webhook body or a credential. The patient appears only as a DISPLAY NAME read from `patients`
when the item is shown. There is deliberately no request schema for `resolve`: the endpoint
validates the body by hand so an error can never echo a note that contains personal data.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AttentionResolutionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str  # refunded_in_provider_panel | rebooked | kept_as_credit | other
    note: str
    resolved_by_name: str
    resolved_at: datetime


class AttentionItemOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    # late_slot_taken | paid_then_cancelled | refund_due_policy | duplicate_payment |
    # amount_under | amount_over | orphan_payment | reference_mismatch | promotion_failed |
    # refunded_externally
    reason: str
    reason_text: str
    patient_display_name: str | None = None
    service_label: str | None = None
    slot_start: datetime | None = None
    slot_end: datetime | None = None
    amount_cents: int | None = None
    paid_amount_cents: int | None = None
    paid_at: datetime | None = None
    provider: str
    suggested_refund_cents: int | None = None
    provider_refund_seen_at: datetime | None = None
    detected_at: datetime
    status: str  # open | resolved
    resolution: AttentionResolutionOut | None = None


class AttentionListOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # ALWAYS present, also when listing `resolved`: the Chat badge reads it.
    open_count: int
    items: list[AttentionItemOut]
    next_cursor: str | None = None
