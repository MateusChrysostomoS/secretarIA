"""Response schema of `GET /tenants/me/payment-settings` (api/hub/payment_settings.py).

What the `/contexto` "Pagamento do sinal" section needs to render the provider / timing / window
controls and to explain, in words, why a control is disabled. A whitelist: no credential, no
provider payload, nothing but the clinic's own settings and rule verdicts.
"""

from pydantic import BaseModel, ConfigDict


class ProviderChoiceOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    # False until the platform has a wrapper for it (Mercado Pago ships in TASK-018).
    available: bool


class SettingBlockerOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    message: str


class PaymentSettingsOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    payment_provider: str
    deposit_timing: str
    deposit_hold_ttl_minutes: int
    ttl_min_minutes: int
    ttl_max_minutes: int
    deposit_enabled: bool
    # The existing `pix_deposit_percent` (the amount is a percentage of the service price).
    deposit_percent: int
    providers: list[ProviderChoiceOut]
    # Empty = `before` can be selected now. Otherwise the reasons, in words.
    before_blockers: list[SettingBlockerOut]
    # Empty = the provider can be changed now.
    provider_switch_blockers: list[SettingBlockerOut]
    # TASK-053: which Asaas environment this backend talks to ("sandbox" | "production").
    # (No egress IP on purpose - owner decision 2026-10-12: Asaas does not require one.)
    asaas_environment: str | None = None
