"""Business rules of the clinic's payment settings (spec sections 6.4, 12.1, 12.2).

Three per-clinic fields decide how a deposit works: `payment_provider` (which PSP), `deposit_timing`
(`before` = the consultation is confirmed only after the Pix is paid, `after` = today's behaviour)
and `deposit_hold_ttl_minutes` (how long the slot and the Pix live in `before`, 30-120). The
amount is the existing `pix_deposit_percent`.

The rules live in the SERVICE, not in the database (spec 6.4):

* `before` is accepted only when deposits are switched on, the `pix_deposit` add-on is active and
  the provider's account is connected AND verified - a `before` clinic that cannot create a Pix
  would block every one of its bookings;
* the provider can be changed only while nothing is pending with the current one (an open Pix, or
  a paid one still being confirmed): switching under a live charge would strand its money;
* a provider with no wrapper registered (Mercado Pago before its phase) cannot be selected.

Both functions are READ-ONLY (`hub_configuration` runs the validator before it mutates anything,
because it may call brain-api for the entitlement and the save transaction must not wait on that).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import PixDeposit, Tenant
from secretaria.models.clinic_payment_account import PAYMENT_PROVIDERS
from secretaria.models.pix_deposit import (
    FULFILLMENT_NONE,
    FULFILLMENT_PROMOTING,
    PixDepositStatus,
)
from secretaria.services.payments import accounts, deposit_gate, registry

BLOCKER_DEPOSIT_DISABLED = "deposit_disabled"
BLOCKER_ADDON_REQUIRED = "addon_required"
BLOCKER_ENTITLEMENT_UNRESOLVABLE = "entitlement_unresolvable"
BLOCKER_ACCOUNT_NOT_READY = "account_not_ready"
BLOCKER_PROVIDER_UNAVAILABLE = "provider_unavailable"
BLOCKER_OPEN_DEPOSITS = "open_deposits"

MESSAGES = {
    BLOCKER_DEPOSIT_DISABLED: "Ative o sinal via Pix antes de escolher quando cobrar.",
    BLOCKER_ADDON_REQUIRED: "O sinal via Pix não está contratado nesta clínica.",
    BLOCKER_ENTITLEMENT_UNRESOLVABLE: (
        "Não foi possível confirmar o plano agora. Tente de novo em instantes."
    ),
    BLOCKER_ACCOUNT_NOT_READY: (
        "Conecte e valide a conta do provedor antes de cobrar antes de confirmar."
    ),
    BLOCKER_PROVIDER_UNAVAILABLE: "Este provedor ainda não está disponível.",
    BLOCKER_OPEN_DEPOSITS: (
        "Há cobranças em andamento com o provedor atual. Aguarde a conclusão para trocar."
    ),
}


class PaymentSettingsInvalid(Exception):
    """A settings patch broke a rule. `code` is one of the `BLOCKER_*` values."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.message = MESSAGES.get(code, code)
        super().__init__(code)


@dataclass(frozen=True)
class ProviderChoice:
    provider: str
    available: bool


def provider_choices() -> list[ProviderChoice]:
    """Every provider the platform knows, with whether a wrapper is registered for it."""
    return [
        ProviderChoice(name, registry.find_provider(name) is not None)
        for name in PAYMENT_PROVIDERS
    ]


async def before_blockers(
    session: AsyncSession,
    tenant: Tenant,
    *,
    provider: str | None = None,
    enabled: bool | None = None,
    redis=None,
) -> list[str]:
    """Why `before` cannot be selected right now (empty = it can). The stored values are used
    unless a patch supplies `provider` / `enabled`."""
    blockers: list[str] = []
    if not (tenant.pix_deposit_enabled if enabled is None else enabled):
        blockers.append(BLOCKER_DEPOSIT_DISABLED)
    active = await deposit_gate.is_addon_active(tenant.id, redis)
    if active is None:
        blockers.append(BLOCKER_ENTITLEMENT_UNRESOLVABLE)
    elif not active:
        blockers.append(BLOCKER_ADDON_REQUIRED)
    name = provider or tenant.payment_provider
    account = await accounts.load_charge_account(
        session, tenant_id=tenant.id, provider=name, timing="before"
    )
    if account is None:
        blockers.append(BLOCKER_ACCOUNT_NOT_READY)
    return blockers


async def provider_switch_blockers(session: AsyncSession, tenant_id: UUID) -> list[str]:
    """Why the provider cannot be changed right now: an open Pix, or a paid one whose
    consultation is still being confirmed, with the CURRENT provider."""
    open_count = await session.scalar(
        select(func.count())
        .select_from(PixDeposit)
        .where(
            PixDeposit.tenant_id == tenant_id,
            or_(
                PixDeposit.status == PixDepositStatus.AWAITING,
                (PixDeposit.status == PixDepositStatus.PAID)
                & PixDeposit.fulfillment_status.in_((FULFILLMENT_NONE, FULFILLMENT_PROMOTING)),
            ),
        )
    )
    return [BLOCKER_OPEN_DEPOSITS] if open_count else []


async def validate_patch(
    session: AsyncSession, tenant: Tenant, data: dict, *, redis=None
) -> None:
    """Check a settings patch against the STORED tenant, before anything is mutated.

    `data` is the `exclude_unset` dump of `TenantConfigUpdate`. Raises `PaymentSettingsInvalid`.
    A save that touches none of the three fields (or only switches deposits off) is never
    blocked by these rules: a config save is always allowed.
    """
    new_provider = data.get("payment_provider")
    new_timing = data.get("deposit_timing")
    provider_changes = new_provider is not None and new_provider != tenant.payment_provider

    if provider_changes:
        choice = {c.provider: c.available for c in provider_choices()}
        if not choice.get(new_provider, False):
            raise PaymentSettingsInvalid(BLOCKER_PROVIDER_UNAVAILABLE)
        if await provider_switch_blockers(session, tenant.id):
            raise PaymentSettingsInvalid(BLOCKER_OPEN_DEPOSITS)

    effective_timing = new_timing if new_timing is not None else tenant.deposit_timing
    selecting_before = effective_timing == "before" and (
        new_timing == "before" or provider_changes
    )
    if selecting_before:
        enabled = data.get("pix_deposit_enabled")
        blockers = await before_blockers(
            session,
            tenant,
            provider=new_provider if provider_changes else None,
            enabled=enabled,
            redis=redis,
        )
        if blockers:
            raise PaymentSettingsInvalid(blockers[0])
