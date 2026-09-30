"""Shared test helpers for the `before` timing (TASK-017). Not a test module.

`World` is one clinic on `before` timing with one patient and one conversation, a connected
payment account and a priced service, on an in-memory SQLite database. Everything a `before`
test needs to script - the provider, the entitlement, the channel senders - is a fake here, so a
call the code makes that a fake does not model fails loudly instead of being ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from secretaria.models import Conversation, Patient, Tenant
from secretaria.services import tenant_config as cfg
from secretaria.services.entitlements_client import EntitlementSummary
from secretaria.services.payments import accounts
from secretaria.services.payments.provider import CreatedCharge

SLOT_START = datetime(2030, 3, 4, 13, 0, tzinfo=UTC)  # 10:00 in São Paulo
SLOT_END = SLOT_START + timedelta(minutes=30)


def entitlement_summary(*, addon: bool = True, active: bool = True) -> EntitlementSummary:
    return EntitlementSummary(
        tenant_id="t",
        status="active" if active else "canceled",
        active=active,
        secretaria_enabled=True,
        plan="pro",
        secretaria_tier="secretaria",
        addons={"pix_deposit": addon},
        limits={},
    )


@dataclass
class World:
    tenant: Tenant
    patient: Patient
    conversation: Conversation

    @property
    def tenant_id(self) -> UUID:
        return self.tenant.id


async def make_world(
    session,
    *,
    channel: str = "whatsapp",
    timing: str = "before",
    with_account: bool = True,
    ttl: int = 30,
    price: str = "R$ 200,00",
    percent: int = 30,
    provider: str = "asaas",
    **tenant_kw,
) -> World:
    values = {
        "clinic_name": "Clinic",
        "phone_number_id": str(uuid4())[:12],
        "timezone": "America/Sao_Paulo",
        "pix_deposit_enabled": True,
        "pix_deposit_percent": percent,
        "payment_provider": provider,
        "deposit_timing": timing,
        "deposit_hold_ttl_minutes": ttl,
        "appointment_types": [
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": price}
        ],
    }
    values.update(tenant_kw)
    tenant = Tenant(id=uuid4(), **values)
    if channel == "whatsapp":
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
    else:
        patient = Patient(
            id=uuid4(),
            tenant_id=tenant.id,
            wa_id=None,
            channel="brain_message",
            external_id=str(uuid4()),
            name="Maria",
        )
    session.add_all([tenant, patient])
    await session.flush()
    conversation = Conversation(id=uuid4(), tenant_id=tenant.id, patient_id=patient.id)
    session.add(conversation)
    if with_account:
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider=provider
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
        row.status = "connected"
        row.last_verified_at = datetime.now(UTC)
    await session.commit()
    return World(tenant, patient, conversation)


def new_charge(charge_id: str = "pay_1", copy_paste: str | None = "PIXCOPIAECOLA") -> CreatedCharge:
    return CreatedCharge(
        provider_charge_id=charge_id, copy_paste=copy_paste, provider_customer_id="cus_1"
    )
