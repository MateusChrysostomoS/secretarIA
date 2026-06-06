"""Subscription-token verification — the seam where the future Payments API plugs in.

============================ READ THIS BEFORE EDITING ============================

TODAY (MVP):
    A single static "fake" token (settings.MVP_FAKE_TOKEN) stands in for an active
    paid subscription. Presenting it as `Authorization: Bearer <token>` on the
    doctor-hub endpoints authenticates the request as the tenant identified by
    settings.MVP_FAKE_TOKEN_TENANT_ID (or, if that is empty and exactly one tenant
    exists, that single tenant).

FUTURE (after secretarIA is finished, when you build the Payments/Subscription API):
    Replace the body of `verify_subscription_token` with a call to that API. It
    should validate the bearer token, confirm the subscription is active/paid, and
    return the tenant_id it maps to. Return None for any invalid/expired/unpaid
    token.

    *** THIS FUNCTION IS THE ONLY PLACE THAT NEEDS TO CHANGE. ***
    Every hub endpoint depends on api/deps.py:get_current_tenant, which depends on
    this function. Nothing else in the codebase reads the token directly.

=================================================================================
"""

import hmac
from dataclasses import dataclass
from uuid import UUID

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SubscriptionClaim:
    """The result of validating a subscription token.

    tenant_id: which tenant the token authorises (None = "resolve the single
        tenant" in MVP mode; the future API should always return a concrete id).
    active:    whether the subscription is currently active/paid.
    """

    tenant_id: UUID | None
    active: bool


def verify_subscription_token(token: str) -> SubscriptionClaim | None:
    """Validate a bearer token and return its subscription claim, or None.

    MVP implementation: constant-time compare against settings.MVP_FAKE_TOKEN.
    See the module docstring for the future-API replacement plan.
    """
    settings = get_settings()
    expected = settings.MVP_FAKE_TOKEN

    if not expected:
        # No fake token configured -> the hub is effectively locked. Fail closed.
        logger.warning("subscription_no_fake_token_configured")
        return None

    if not hmac.compare_digest(token, expected):
        return None

    tenant_id: UUID | None = None
    if settings.MVP_FAKE_TOKEN_TENANT_ID:
        try:
            tenant_id = UUID(settings.MVP_FAKE_TOKEN_TENANT_ID)
        except ValueError:
            logger.error(
                "subscription_bad_tenant_id_setting",
                value=settings.MVP_FAKE_TOKEN_TENANT_ID,
            )
            tenant_id = None

    return SubscriptionClaim(tenant_id=tenant_id, active=True)
