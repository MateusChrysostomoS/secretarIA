"""Response schemas of the payment-connection hub endpoints (api/hub/payment_accounts.py).

WHITELISTS by construction (tenant-secrets-encryption skill): nothing here can carry a key, a
token, a ciphertext or a provider payload, and the endpoints build these objects field by
field - they are never `model_validate`d from an ORM row, so a secret column can never be
serialised by accident. There is deliberately no REQUEST schema for the key: the endpoint
parses that one field by hand so a validation error can never echo it back.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PaymentAccountStatusOut(BaseModel):
    """One provider connection as the clinic sees it (the "connected" pill and its reason)."""

    model_config = ConfigDict(extra="ignore")

    provider: str
    # pending | connected | incomplete | error | reauth_required | revoked
    status: str
    # e.g. key_expired_or_disabled | excess_permissions | missing_permission:<groups> |
    # webhook_create_failed | webhook_readback_failed | legacy_unverified
    incomplete_reason: str | None = None
    capabilities: dict[str, bool] = {}
    excess_groups: list[str] = []
    excess_labels: list[str] = []
    connected_at: datetime | None = None
    last_verified_at: datetime | None = None
    # True only when this account may gate a booking (`before` timing): connected AND verified.
    ready_for_before: bool = False
    # TASK-071 Plan 3 (task 4): did the last Pix charge with this account work? "ok" only after a
    # successful charge is on record, "failing" while the last failure is newer, "untested"
    # before any charge. The last failure is a closed category (`FailureReason` value), its
    # plain sentence for the clinic, and when it happened; all None when nothing ever failed.
    charge_health: Literal["ok", "failing", "untested"] = "untested"
    last_charge_failure_reason: str | None = None
    last_charge_failure_text: str | None = None
    last_charge_failure_at: datetime | None = None


class ChecklistItemOut(BaseModel):
    """One line of "Checklist para ter seu sinal e evitar no-show" (TASK-071, Plan 4)."""

    model_config = ConfigDict(extra="ignore")

    key: str
    label: str
    # done | pending | warning | unknown
    state: str
    # system (filled by the system) | user (confirmed by the manager)
    source: str
    hint: str | None = None
    link: str | None = None
    names: list[str] = []


class ChecklistProgressOut(BaseModel):
    done: int
    total: int


class ReadinessItemOut(BaseModel):
    code: str
    severity: str
    text: str
    where: str | None = None
    names: list[str] = []


class ChecklistOut(BaseModel):
    """The whole checklist: items, the "N de 8" progress, the badge (green ONLY with proof) and the
    pending list. Fixed texts and closed vocabularies only - never provider text or patient data."""

    model_config = ConfigDict(extra="ignore")

    items: list[ChecklistItemOut]
    progress: ChecklistProgressOut
    badge: Literal["ok", "failing", "untested"]
    readiness: list[ReadinessItemOut]
    charge_health: Literal["ok", "failing", "untested"] = "untested"
    last_charge_failure_reason: str | None = None
    last_charge_failure_text: str | None = None
    last_charge_failure_at: datetime | None = None
    # False when the API key cannot read the Asaas registration (the Portal then says "confira no
    # painel" and never claims approval).
    onboarding_readable: bool = False
    asaas_panel_url: str = ""


class TestChargeResultOut(BaseModel):
    """The answer of "Testar cobrança". `reason` is a closed category, `reason_text` its plain
    sentence; `cleaned` False means the test charge could not be removed (do it in the panel)."""

    __test__ = False  # not a pytest class, despite the name

    ok: bool
    reason: str | None = None
    reason_text: str | None = None
    stage: str | None = None
    cleaned: bool = True
    busy: bool = False


class AsaasConnectionResultOut(BaseModel):
    """The answer to connect / retry / disconnect. No key, no token, no group detail (a refusal
    is a 422 whose `detail` carries the group codes and labels instead)."""

    model_config = ConfigDict(extra="ignore")

    provider: str = "asaas"
    status: str
    incomplete_reason: str | None = None
    capabilities: dict[str, bool] = {}
    # TASK-053: while the excess refusal is OFF, an ACCEPTED key may hold more permissions than
    # needed; these are information for the screen ("parece ter mais permissoes"), never a block.
    excess_groups: list[str] = []
    excess_labels: list[str] = []
