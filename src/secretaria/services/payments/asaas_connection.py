"""Connect a clinic's Asaas account from an API key the clinic pastes ONCE (spec 12.3, D8/D14).

Flow of `connect_asaas_account` (nothing is stored before step 3):

1. cheap format/environment check (`$aact_prod_...` against production, `$aact_hmlg_...` against
   the sandbox) - no network;
2. permission probes (`asaas_probes.run_probe_suite`) with the key held ONLY in memory;
3. REFUSED or INCONCLUSIVE -> return; the key is dropped, no row is created or changed (a key
   that replaces a working one and is refused leaves the working one in place);
4. ACCEPTED -> store the key (and a fresh `authToken`) encrypted, then create the clinic's
   webhook at Asaas and READ IT BACK. Any failure there leaves the account `incomplete` with the
   already-approved key kept, so "try again" never asks for the key again. Only a verified,
   read-back webhook makes the account `connected`.

The key is never logged, never returned, never put in a job argument (secret-hygiene tests pin
it). The webhook `authToken` is generated here, stored encrypted and never shown.

`recheck_asaas_account` is the daily re-verification: a dead key, a permission change, an
`excess` permission, a paused webhook queue. It pauses NEW charges (`incomplete`) and alerts the
clinic (debounced e-mail); charges already issued keep being reconciled.
"""

import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.models import ClinicPaymentAccount, Tenant
from secretaria.models.clinic_payment_account import (
    ACCOUNT_CONNECTED,
    ACCOUNT_INCOMPLETE,
    ACCOUNT_PENDING,
    ACCOUNT_REVOKED,
    PROVIDER_ASAAS,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, alerts, asaas_provider, jobs, onboarding
from secretaria.services.payments.asaas_probes import (
    CODE_KEY_INVALID,
    CODE_KEY_RESTRICTED,
    CODE_MISSING,
    Verdict,
    VerdictKind,
    run_probe_suite,
)

logger = get_logger(__name__)

# The events the clinic's webhook subscribes to: what the payment core acts on, nothing else.
WEBHOOK_EVENTS = [
    "PAYMENT_RECEIVED",
    "PAYMENT_CONFIRMED",
    "PAYMENT_OVERDUE",
    "PAYMENT_DELETED",
    "PAYMENT_REFUNDED",
    "PAYMENT_PARTIALLY_REFUNDED",
]
WEBHOOK_NAME = "SecretarIA - sinal Pix"

REASON_WEBHOOK_CREATE_FAILED = "webhook_create_failed"
REASON_WEBHOOK_READBACK_FAILED = "webhook_readback_failed"
REASON_KEY_EXPIRED = "key_expired_or_disabled"
REASON_EXCESS = "excess_permissions"
REASON_MISSING_PREFIX = "missing_permission:"

_KEY_SHAPE = re.compile(r"^\$aact_(prod|hmlg)_[A-Za-z0-9_\-:=+/.$]{10,300}$")
_INCONCLUSIVE_DAYS_BEFORE_ALERT = 3


@dataclass(frozen=True)
class ConnectionResult:
    """What the caller (the hub endpoint) may say about a connection attempt. Carries group
    CODES and short reasons only - never the key, a token or provider content."""

    outcome: Literal["connected", "incomplete", "refused", "inconclusive"]
    status: str | None = None
    code: str | None = None
    incomplete_reason: str | None = None
    missing_groups: tuple[str, ...] = ()
    excess_groups: tuple[str, ...] = ()
    capabilities: dict = field(default_factory=dict)


def validate_key_format(api_key: str) -> bool:
    """`$aact_prod_...` for production, `$aact_hmlg_...` for the sandbox host, no whitespace."""
    match = _KEY_SHAPE.match(api_key or "")
    if match is None:
        return False
    sandbox_host = "sandbox" in get_settings().ASAAS_BASE_URL.lower()
    return (match.group(1) == "hmlg") == sandbox_host


async def _register_webhook(
    client, row: ClinicPaymentAccount, auth_token: str, base_url: str | None
) -> str | None:
    """Create (or update) the clinic's webhook and read it back. Returns None on success, else
    the `incomplete` reason. Sets `row.external_webhook_id` when Asaas gave one."""
    if not base_url:
        return REASON_WEBHOOK_CREATE_FAILED
    url = f"{base_url.rstrip('/')}/webhooks/asaas/{row.webhook_slug}"
    body = {
        "name": WEBHOOK_NAME,
        "url": url,
        "enabled": True,
        "interrupted": False,
        "apiVersion": 3,
        "authToken": auth_token,
        "sendType": "SEQUENTIALLY",
        "events": WEBHOOK_EVENTS,
    }
    # Asaas requires it (real sandbox: 400 without); who gets the "queue paused" e-mail is an
    # operator decision, so it comes from settings and is left out when unset, never sent empty.
    alert_email = get_settings().ASAAS_WEBHOOK_ALERT_EMAIL.strip()
    if alert_email:
        body["email"] = alert_email
    try:
        existing = next(
            (
                hook
                for hook in (await client.list_webhooks()).get("data") or []
                if isinstance(hook, dict) and hook.get("url") == url and hook.get("id")
            ),
            None,
        )
        if existing is not None:
            hook_id = str(existing["id"])
            await client.update_webhook(hook_id, body)
        else:
            created = await client.create_webhook(body)
            hook_id = str(created.get("id") or "")
    except Exception as exc:  # noqa: BLE001 - a status/transport error, never a body
        logger.warning("asaas_webhook_create_failed", error_type=type(exc).__name__)
        return REASON_WEBHOOK_CREATE_FAILED
    if not hook_id:
        return REASON_WEBHOOK_CREATE_FAILED
    row.external_webhook_id = hook_id

    try:
        listed = (await client.list_webhooks()).get("data") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("asaas_webhook_readback_failed", error_type=type(exc).__name__)
        return REASON_WEBHOOK_READBACK_FAILED
    visible = any(
        isinstance(hook, dict)
        and hook.get("id") == hook_id
        and hook.get("url") == url
        and _webhook_ready(hook)
        for hook in listed
    )
    return None if visible else REASON_WEBHOOK_READBACK_FAILED


def _mark_connected(row: ClinicPaymentAccount, now: datetime) -> None:
    row.status = ACCOUNT_CONNECTED
    row.incomplete_reason = None
    # `excess_groups` is NOT reset here: while the excess refusal is OFF (TASK-053) a connected
    # account may carry the informational groups of its last probe; the probe paths set it.
    row.probe_inconclusive_days = 0
    row.connected_at = row.connected_at or now
    row.last_verified_at = now


def _webhook_ready(hook: dict) -> bool:
    return (
        hook.get("enabled") is True
        and hook.get("interrupted") is False
        and isinstance(hook.get("events"), list)
        and set(WEBHOOK_EVENTS).issubset(set(hook["events"]))
    )


def _result_of(row: ClinicPaymentAccount, outcome: str) -> ConnectionResult:
    return ConnectionResult(
        outcome=outcome,  # type: ignore[arg-type]
        status=row.status,
        incomplete_reason=row.incomplete_reason,
        excess_groups=tuple(row.excess_groups or []),
        capabilities=dict(row.capabilities or {}),
    )


async def connect_asaas_account(
    session: AsyncSession,
    *,
    tenant_id,
    api_key: str,
    webhook_base_url: str | None = None,
    auth_token: str | None = None,
    now: datetime | None = None,
    run_probes=None,
) -> ConnectionResult:
    """Validate, probe, store and register. Flushes; the CALLER commits (also after a refusal,
    when there is nothing to commit). `auth_token` lets the internal operator endpoint supply the
    webhook token; otherwise a fresh 43-character one is generated."""
    now = now or datetime.now(UTC)
    if not validate_key_format(api_key):
        return ConnectionResult("refused", code=CODE_KEY_INVALID)
    if auth_token is not None and not 32 <= len(auth_token) <= 255:
        return ConnectionResult("refused", code="invalid_webhook_token")  # Asaas: 32-255 chars

    verdict: Verdict = await (run_probes or run_probe_suite)(api_key)
    if verdict.kind is VerdictKind.INCONCLUSIVE:
        logger.warning("asaas_key_probe_inconclusive", verdict=verdict.code)
        return ConnectionResult("inconclusive", code=verdict.code)
    if verdict.kind is VerdictKind.REFUSED:
        logger.warning(
            "asaas_key_probe_refused",
            verdict=verdict.code,
            missing_groups=list(verdict.missing_groups),
            excess_groups=list(verdict.excess_groups),
        )
        return ConnectionResult(
            "refused",
            code=verdict.code,
            missing_groups=verdict.missing_groups,
            excess_groups=verdict.excess_groups,
        )

    # ACCEPTED: only now does the key leave memory (encrypted).
    token = auth_token or secrets.token_urlsafe(32)
    row = await accounts.get_or_create_account_row(
        session, tenant_id=tenant_id, provider=PROVIDER_ASAAS
    )
    cfg.set_payment_account_secrets(row, api_key=api_key, webhook_token=token)
    await cfg.set_asaas_api_key(session, tenant_id, api_key)  # dual-write (legacy readers)
    await cfg.set_asaas_webhook_token(session, tenant_id, token)
    row.capabilities = dict(verdict.capabilities)
    # Empty unless the excess refusal is OFF and the key holds extra groups (information, TASK-053).
    row.excess_groups = list(verdict.excess_groups)
    row.last_probe_at = now
    row.probe_inconclusive_days = 0
    # A new key: a charge that worked with the previous one is no longer proof (charge_health).
    row.key_connected_at = now

    client = asaas_provider.client_for(api_key)
    # Informational: the registration states, when this key may read them. Never blocks.
    onboarding.store_onboarding(row, await onboarding.read_onboarding(client), now)
    failure = await _register_webhook(client, row, token, webhook_base_url)
    if failure is None:
        _mark_connected(row, now)
        logger.info("asaas_account_connected", tenant_id=str(tenant_id))
        return _result_of(row, "connected")
    row.status = ACCOUNT_INCOMPLETE
    row.incomplete_reason = failure
    logger.warning("asaas_account_incomplete", tenant_id=str(tenant_id), reason=failure)
    return _result_of(row, "incomplete")


async def retry_webhook_setup(
    session: AsyncSession,
    *,
    tenant_id,
    webhook_base_url: str | None = None,
    now: datetime | None = None,
) -> ConnectionResult:
    """"Tentar de novo": redo ONLY the webhook step with the key already approved and stored."""
    now = now or datetime.now(UTC)
    row = await accounts.get_account_row(session, tenant_id=tenant_id, provider=PROVIDER_ASAAS)
    account = await cfg.get_payment_account(session, tenant_id, PROVIDER_ASAAS)
    if row is None or account is None or not account.api_key:
        return ConnectionResult("refused", code="no_key")
    token = account.webhook_token or secrets.token_urlsafe(32)
    if not account.webhook_token:
        cfg.set_payment_account_secrets(row, webhook_token=token)
        await cfg.set_asaas_webhook_token(session, tenant_id, token)
    failure = await _register_webhook(
        asaas_provider.client_for(account.api_key), row, token, webhook_base_url
    )
    if failure is None:
        _mark_connected(row, now)
        return _result_of(row, "connected")
    row.status = ACCOUNT_INCOMPLETE
    row.incomplete_reason = failure
    return _result_of(row, "incomplete")


async def disconnect_asaas_account(session: AsyncSession, *, tenant_id) -> bool:
    """Forget the key: best-effort DELETE of the webhook at Asaas (a failure never blocks the
    local wipe), then erase every stored secret. False when the clinic had no account."""
    row = await accounts.get_account_row(session, tenant_id=tenant_id, provider=PROVIDER_ASAAS)
    if row is None:
        return False
    account = await cfg.get_payment_account(session, tenant_id, PROVIDER_ASAAS)
    if account is not None and account.api_key and row.external_webhook_id:
        try:
            await asaas_provider.client_for(account.api_key).delete_webhook(row.external_webhook_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("asaas_webhook_delete_failed", error_type=type(exc).__name__)
    cfg.clear_payment_account_secrets(row)
    await cfg.clear_asaas_credentials(session, tenant_id)
    row.status = ACCOUNT_REVOKED
    row.external_webhook_id = None
    row.capabilities = {}
    row.excess_groups = []
    row.incomplete_reason = None
    row.connected_at = None
    row.last_verified_at = None
    row.probe_inconclusive_days = 0
    return True


async def recheck_asaas_account(
    session: AsyncSession,
    *,
    tenant_id,
    redis=None,
    now: datetime | None = None,
    run_probes=None,
) -> str:
    """Re-verify one clinic's key and webhook (the daily step). Returns skipped | inconclusive |
    refused | accepted. Never stores or logs the key; an inconclusive result changes no state."""
    now = now or datetime.now(UTC)
    row = await accounts.get_account_row(session, tenant_id=tenant_id, provider=PROVIDER_ASAAS)
    account = await cfg.get_payment_account(session, tenant_id, PROVIDER_ASAAS)
    if (
        row is None
        or account is None
        or not account.api_key
        or row.status in (ACCOUNT_REVOKED, ACCOUNT_PENDING)
    ):
        return "skipped"
    tenant = await session.get(Tenant, tenant_id)

    async def alert(kind: str) -> None:
        if tenant is not None:
            await alerts.send_payment_alert(redis, tenant, kind)

    verdict = await (run_probes or run_probe_suite)(account.api_key)
    row.last_probe_at = now

    if verdict.kind is VerdictKind.INCONCLUSIVE:
        row.probe_inconclusive_days += 1
        if row.probe_inconclusive_days >= _INCONCLUSIVE_DAYS_BEFORE_ALERT:
            await alert("probe_unavailable")
        return "inconclusive"

    row.probe_inconclusive_days = 0
    if verdict.kind is VerdictKind.REFUSED:
        row.status = ACCOUNT_INCOMPLETE
        if verdict.code in (CODE_KEY_INVALID, CODE_KEY_RESTRICTED):
            row.incomplete_reason = REASON_KEY_EXPIRED
            await alert("key_expired_or_disabled")
        elif verdict.code == CODE_MISSING:
            row.incomplete_reason = REASON_MISSING_PREFIX + ",".join(verdict.missing_groups)
            await alert("missing_permission")
        else:  # CODE_EXCESS or CODE_MIXED
            row.incomplete_reason = REASON_EXCESS
            row.excess_groups = list(verdict.excess_groups)
            await alert("excess_permissions")
        logger.warning("asaas_key_recheck_refused", verdict=verdict.code, tenant_id=str(tenant_id))
        return "refused"

    # ACCEPTED: the key is fine; now the webhook. The groups are informational (TASK-053).
    row.capabilities = dict(verdict.capabilities)
    row.excess_groups = list(verdict.excess_groups)
    client = asaas_provider.client_for(account.api_key)
    # The "cadastro concluido" tick lights up by itself once Asaas approves the account.
    onboarding.store_onboarding(row, await onboarding.read_onboarding(client), now)
    try:
        hooks = (await client.list_webhooks()).get("data") or []
    except Exception as exc:  # noqa: BLE001 - cannot look: leave the state as it is
        logger.warning("asaas_recheck_webhook_unavailable", error_type=type(exc).__name__)
        return "accepted"
    ours = next(
        (
            hook
            for hook in hooks
            if isinstance(hook, dict)
            and hook.get("id")
            and (hook.get("id") == row.external_webhook_id or _is_our_url(hook, row))
        ),
        None,
    )
    if ours is None:
        row.status = ACCOUNT_INCOMPLETE
        row.incomplete_reason = REASON_WEBHOOK_READBACK_FAILED
        row.last_verified_at = None
        return "accepted"
    row.external_webhook_id = str(ours["id"])
    if not account.webhook_token:
        row.status = ACCOUNT_INCOMPLETE
        row.incomplete_reason = REASON_WEBHOOK_READBACK_FAILED
        row.last_verified_at = None
        return "accepted"
    if ours.get("interrupted") is True:
        try:
            await client.update_webhook(
                row.external_webhook_id,
                {"interrupted": False, "enabled": True, "authToken": account.webhook_token},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("asaas_webhook_reactivate_failed", error_type=type(exc).__name__)
            row.status = ACCOUNT_INCOMPLETE
            row.incomplete_reason = REASON_WEBHOOK_READBACK_FAILED
            row.last_verified_at = None
            return "accepted"
        await alert("webhook_queue_paused")
        try:
            hooks = (await client.list_webhooks()).get("data") or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("asaas_recheck_webhook_unavailable", error_type=type(exc).__name__)
            row.status = ACCOUNT_INCOMPLETE
            row.incomplete_reason = REASON_WEBHOOK_READBACK_FAILED
            row.last_verified_at = None
            return "accepted"
        ours = next((hook for hook in hooks if isinstance(hook, dict)
                     and hook.get("id") == row.external_webhook_id), None)
    if ours is None or not _webhook_ready(ours):
        row.status = ACCOUNT_INCOMPLETE
        row.incomplete_reason = REASON_WEBHOOK_READBACK_FAILED
        row.last_verified_at = None
        return "accepted"
    # Asaas documents that authToken is returned only at creation. Reapply our stored token
    # during the daily check, then verify the observable settings again.
    try:
        await client.update_webhook(row.external_webhook_id, {"authToken": account.webhook_token})
        hooks = (await client.list_webhooks()).get("data") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("asaas_webhook_token_restore_failed", error_type=type(exc).__name__)
        row.status = ACCOUNT_INCOMPLETE
        row.incomplete_reason = REASON_WEBHOOK_READBACK_FAILED
        row.last_verified_at = None
        return "accepted"
    ours = next((hook for hook in hooks if isinstance(hook, dict)
                 and hook.get("id") == row.external_webhook_id), None)
    if ours is None or not _webhook_ready(ours):
        row.status = ACCOUNT_INCOMPLETE
        row.incomplete_reason = REASON_WEBHOOK_READBACK_FAILED
        row.last_verified_at = None
        return "accepted"
    _mark_connected(row, now)
    return "accepted"


def _is_our_url(hook: dict, row: ClinicPaymentAccount) -> bool:
    return isinstance(hook.get("url"), str) and hook["url"].endswith(
        f"/webhooks/asaas/{row.webhook_slug}"
    )


async def recheck_all_asaas_accounts(session: AsyncSession, now: datetime, redis) -> None:
    """Daily step: recheck every Asaas account that holds a key. One clinic's failure never
    stops the others."""
    tenant_ids = (
        await session.scalars(
            select(ClinicPaymentAccount.tenant_id).where(
                ClinicPaymentAccount.provider == PROVIDER_ASAAS,
                ClinicPaymentAccount.api_key_encrypted.is_not(None),
                ClinicPaymentAccount.status.in_((ACCOUNT_CONNECTED, ACCOUNT_INCOMPLETE)),
            )
        )
    ).all()
    for tenant_id in tenant_ids:
        try:
            await recheck_asaas_account(session, tenant_id=tenant_id, redis=redis, now=now)
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.warning("asaas_recheck_failed", error_type=type(exc).__name__)


jobs.register_daily_step(recheck_all_asaas_accounts)
