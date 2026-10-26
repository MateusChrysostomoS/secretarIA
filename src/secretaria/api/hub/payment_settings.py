"""Doctor hub - the read side of the deposit settings (spec sections 12.1 and 12.2).

    GET /tenants/me/payment-settings

The three settings themselves (`payment_provider`, `deposit_timing`, `deposit_hold_ttl_minutes`)
are WRITTEN through the existing `PUT /tenants/me/configuration` (`tenant` patch), where the
rules of `services/payments/settings.py` run before anything is mutated and a refusal is a 422
`{"detail": {"code", "message"}}`. This GET is what the screen reads to know which controls are
enabled and why the disabled ones are: it asks the SAME functions the PUT uses, so the screen and
the save can never disagree.

The hub authenticates the TENANT, not the user's role (`api/hub/deps.py`), so "only a manager
edits payment settings" is a UI rule until brain-api puts a role in the hub token (decision D10).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.models import Tenant
from secretaria.schemas.payment_settings import (
    PaymentSettingsOut,
    ProviderChoiceOut,
    SettingBlockerOut,
)
from secretaria.services.payments import deposit_gate, settings

router = APIRouter(prefix="/tenants/me/payment-settings", tags=["hub-payment-settings"])


def _blockers(codes: list[str]) -> list[SettingBlockerOut]:
    return [SettingBlockerOut(code=c, message=settings.MESSAGES.get(c, c)) for c in codes]


def _asaas_environment() -> str:
    """Which Asaas the backend talks to, from ASAAS_BASE_URL (the same test the key-format check
    uses in asaas_connection.validate_key_format)."""
    return "sandbox" if "sandbox" in get_settings().ASAAS_BASE_URL.lower() else "production"


@router.get("", response_model=PaymentSettingsOut)
async def get_payment_settings(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> PaymentSettingsOut:
    return PaymentSettingsOut(
        payment_provider=tenant.payment_provider,
        deposit_timing=tenant.deposit_timing,
        deposit_hold_ttl_minutes=deposit_gate.clamp_ttl(tenant.deposit_hold_ttl_minutes),
        ttl_min_minutes=deposit_gate.HOLD_TTL_MIN_MINUTES,
        ttl_max_minutes=deposit_gate.HOLD_TTL_MAX_MINUTES,
        deposit_enabled=tenant.pix_deposit_enabled,
        deposit_percent=tenant.pix_deposit_percent,
        providers=[
            ProviderChoiceOut(provider=c.provider, available=c.available)
            for c in settings.provider_choices()
        ],
        before_blockers=_blockers(await settings.before_blockers(session, tenant)),
        provider_switch_blockers=_blockers(
            await settings.provider_switch_blockers(session, tenant.id)
        ),
        asaas_environment=_asaas_environment(),
    )
