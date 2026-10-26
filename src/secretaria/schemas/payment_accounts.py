"""Response schemas of the payment-connection hub endpoints (api/hub/payment_accounts.py).

WHITELISTS by construction (tenant-secrets-encryption skill): nothing here can carry a key, a
token, a ciphertext or a provider payload, and the endpoints build these objects field by
field - they are never `model_validate`d from an ORM row, so a secret column can never be
serialised by accident. There is deliberately no REQUEST schema for the key: the endpoint
parses that one field by hand so a validation error can never echo it back.
"""

from datetime import datetime

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
