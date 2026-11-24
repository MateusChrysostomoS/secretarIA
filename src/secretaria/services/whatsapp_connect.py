"""WhatsApp connection done by the secretarIA backend (TASK-093 Plano 3, stage a).

Until now brain-api exchanged the Meta Embedded Signup `code` for an access token, subscribed
the app to the clinic's WABA and only then called this repo's internal
`whatsapp-connection` endpoint. This module does the same here, behind
`WHATSAPP_CONNECT_BACKEND_ENABLED` (the hub routes in `api/hub/whatsapp.py` are 404 while it is
off), so the future Portal can connect without a screen on the Brain site. brain-api's path is
NOT switched off and keeps owning the onboarding state and the charge tied to it; after a
connection the secretarIA notifies it (Task 4).

The two Meta calls mirror brain-api `services/meta_graph.py` (`exchange_code_for_token`,
`subscribe_app_to_waba`): never raise, return None/False on any failure. The wiring itself is
`services/provisioning.py::connect_whatsapp` - the same phone uniqueness rule (409
`phone_number_conflict`) and the same encrypted token write (`tenant_config.set_waba_token`).

Secrets: the token is only sent as a Bearer header and handed to `set_waba_token`; the code
and META_APP_SECRET only travel as query parameters of the one exchange call. None of them is
ever logged, returned or stored in `whatsapp_connection_attempts`.

Concurrency: `complete_attempt` locks the clinic's tenant row first (FOR UPDATE, the same
pattern as api/hub/context_sources.py), so two managers connecting at once are serialised: the
second one sees the first's result (same attempt_id: a replay; another attempt_id: it runs
after the first committed). Caller commits - ALSO after a ConnectError, so the failed attempt
is recorded.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.models import Tenant, WhatsAppConnectionAttempt
from secretaria.models.whatsapp_connection_attempt import (
    ATTEMPT_CONNECTED,
    ATTEMPT_FAILED,
    ATTEMPT_PENDING,
)
from secretaria.services import provisioning

logger = get_logger(__name__)

CODE_EXCHANGE_FAILED = "code_exchange_failed"
WABA_SUBSCRIBE_FAILED = "waba_subscribe_failed"
PHONE_NUMBER_CONFLICT = "phone_number_conflict"

_TOKEN_EXCHANGE_PATH = "/oauth/access_token"
_TIMEOUT_SECONDS = 10.0
# Tests plug an httpx.MockTransport here; production uses the default network transport.
_TRANSPORT: httpx.AsyncBaseTransport | None = None

__all__ = [
    "CODE_EXCHANGE_FAILED",
    "PHONE_NUMBER_CONFLICT",
    "WABA_SUBSCRIBE_FAILED",
    "ConnectError",
    "complete_attempt",
    "exchange_code_for_token",
    "start_attempt",
    "subscribe_app_to_waba",
]


class ConnectError(Exception):
    """The connection failed with a known `code`; `attempt` is the (failed) row."""

    def __init__(self, code: str, attempt: WhatsAppConnectionAttempt | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.attempt = attempt


def _graph_client() -> httpx.AsyncClient:
    version = get_settings().META_GRAPH_API_VERSION
    return httpx.AsyncClient(
        base_url=f"https://graph.facebook.com/{version}",
        timeout=_TIMEOUT_SECONDS,
        transport=_TRANSPORT,
    )


async def exchange_code_for_token(code: str) -> str | None:
    """`GET /oauth/access_token` - the access token, or None (unconfigured, unreachable,
    non-200, no `access_token`). Never raises; never logs the code, secret or token."""
    settings = get_settings()
    if not settings.META_APP_ID or not settings.META_APP_SECRET:
        logger.warning("whatsapp_connect_exchange_unconfigured")
        return None
    try:
        async with _graph_client() as client:
            resp = await client.get(
                _TOKEN_EXCHANGE_PATH,
                params={
                    "client_id": settings.META_APP_ID,
                    "client_secret": settings.META_APP_SECRET,
                    "code": code,
                },
            )
    except httpx.HTTPError:
        logger.warning("whatsapp_connect_exchange_unreachable")
        return None
    if resp.status_code != 200:
        logger.warning("whatsapp_connect_exchange_failed", upstream_status=resp.status_code)
        return None
    try:
        body: Any = resp.json()
    except ValueError:
        body = None
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token or not isinstance(token, str):
        logger.warning("whatsapp_connect_exchange_malformed_response")
        return None
    return token


async def subscribe_app_to_waba(waba_id: str, access_token: str) -> bool:
    """`POST /{waba_id}/subscribed_apps` with the token as a Bearer header only.
    True on a 200; False on anything else. Never raises; never logs the token."""
    try:
        async with _graph_client() as client:
            resp = await client.post(
                f"/{waba_id}/subscribed_apps",
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError:
        logger.warning("whatsapp_connect_waba_subscribe_unreachable")
        return False
    if resp.status_code != 200:
        logger.warning("whatsapp_connect_waba_subscribe_failed", upstream_status=resp.status_code)
        return False
    return True


async def start_attempt(
    session: AsyncSession, tenant_id: UUID, attempt_id: str
) -> WhatsAppConnectionAttempt:
    """Get-or-create the clinic's attempt row (`pending` when new). Caller commits.

    Always re-reads from the database (populate_existing), so after `complete_attempt`'s lock
    a concurrent request's committed result is what comes back."""
    attempt = await session.scalar(
        select(WhatsAppConnectionAttempt)
        .where(
            WhatsAppConnectionAttempt.tenant_id == tenant_id,
            WhatsAppConnectionAttempt.attempt_id == attempt_id,
        )
        .execution_options(populate_existing=True)
    )
    if attempt is None:
        attempt = WhatsAppConnectionAttempt(
            tenant_id=tenant_id, attempt_id=attempt_id, status=ATTEMPT_PENDING
        )
        session.add(attempt)
        await session.flush()
    return attempt


async def _fail(attempt: WhatsAppConnectionAttempt, code: str) -> ConnectError:
    attempt.status = ATTEMPT_FAILED
    attempt.failure_code = code
    logger.info(
        "whatsapp_connect_attempt_failed",
        tenant_id=str(attempt.tenant_id),
        attempt_id=attempt.attempt_id,
        failure_code=code,
    )
    return ConnectError(code, attempt)


async def complete_attempt(
    session: AsyncSession,
    tenant_id: UUID,
    attempt_id: str,
    *,
    code: str,
    phone_number_id: str,
    waba_id: str,
) -> WhatsAppConnectionAttempt:
    """Connect the clinic's WhatsApp for `attempt_id`, idempotently. Caller commits.

    Replay: a `connected` attempt is returned as is and a `failed` one raises its ConnectError
    again - neither calls Meta. A new attempt: number pre-check (another clinic's number is a
    conflict before any Meta call), code exchange, WABA subscription, then
    `provisioning.connect_whatsapp` (re-checks the number, writes number + encrypted token).
    Every failure marks the attempt `failed` with its code, writes NO credential and raises
    ConnectError(code). LookupError when the clinic does not exist.
    """
    locked = await session.scalar(
        select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()
    )
    if locked is None:
        raise LookupError("tenant_not_found")

    attempt = await start_attempt(session, tenant_id, attempt_id)
    if attempt.status == ATTEMPT_CONNECTED:
        return attempt
    if attempt.status == ATTEMPT_FAILED:
        raise ConnectError(attempt.failure_code or CODE_EXCHANGE_FAILED, attempt)

    attempt.phone_number_id = phone_number_id
    attempt.waba_id = waba_id

    taken = await session.scalar(
        select(Tenant.id).where(
            Tenant.phone_number_id == phone_number_id, Tenant.id != tenant_id
        )
    )
    if taken is not None:
        raise await _fail(attempt, PHONE_NUMBER_CONFLICT)

    access_token = await exchange_code_for_token(code)
    if access_token is None:
        raise await _fail(attempt, CODE_EXCHANGE_FAILED)

    if not await subscribe_app_to_waba(waba_id, access_token):
        raise await _fail(attempt, WABA_SUBSCRIBE_FAILED)

    outcome = await provisioning.connect_whatsapp(
        session,
        tenant_id=tenant_id,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
        access_token=access_token,
    )
    if outcome is provisioning.ConnectOutcome.CONFLICT:
        raise await _fail(attempt, PHONE_NUMBER_CONFLICT)
    if outcome is provisioning.ConnectOutcome.NOT_FOUND:  # pragma: no cover - locked above
        raise LookupError("tenant_not_found")

    attempt.status = ATTEMPT_CONNECTED
    attempt.failure_code = None
    logger.info(
        "whatsapp_connect_attempt_connected",
        tenant_id=str(tenant_id),
        attempt_id=attempt_id,
    )
    return attempt
