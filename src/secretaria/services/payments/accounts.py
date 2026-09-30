"""Payment accounts and per-patient customer ids: the DB side of the provider core.

Every query here is scoped by `tenant_id` (or by the opaque `webhook_slug`, which itself
resolves to exactly one tenant). Secrets are never handled here: encryption and the single
decrypt point are `services/tenant_config.py` (`get_payment_account`,
`set_payment_account_secrets`).
"""

import uuid
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Patient
from secretaria.models.clinic_payment_account import (
    ACCOUNT_CONNECTED,
    ACCOUNT_PENDING,
    ACCOUNT_REVOKED,
    ClinicPaymentAccount,
)
from secretaria.models.patient_payment_customer import PatientPaymentCustomer
from secretaria.services import tenant_config as cfg
from secretaria.services.payments.provider import PaymentAccount

Timing = Literal["before", "after"]


async def get_account_row(
    session: AsyncSession, *, tenant_id: UUID, provider: str
) -> ClinicPaymentAccount | None:
    return await session.scalar(
        select(ClinicPaymentAccount).where(
            ClinicPaymentAccount.tenant_id == tenant_id,
            ClinicPaymentAccount.provider == provider,
        )
    )


async def get_account_by_slug(
    session: AsyncSession, *, provider: str, webhook_slug: str
) -> ClinicPaymentAccount | None:
    """The account behind a webhook URL. The slug is opaque and unique; requiring the
    provider too means a slug of one provider never authenticates a request to another."""
    return await session.scalar(
        select(ClinicPaymentAccount).where(
            ClinicPaymentAccount.webhook_slug == webhook_slug,
            ClinicPaymentAccount.provider == provider,
        )
    )


async def get_or_create_account_row(
    session: AsyncSession, *, tenant_id: UUID, provider: str
) -> ClinicPaymentAccount:
    """The (tenant, provider) row, created `pending` with a fresh random `webhook_slug` when
    missing. Flushes; the caller commits."""
    row = await get_account_row(session, tenant_id=tenant_id, provider=provider)
    if row is None:
        row = ClinicPaymentAccount(
            tenant_id=tenant_id,
            provider=provider,
            status=ACCOUNT_PENDING,
            webhook_slug=str(uuid.uuid4()),
        )
        session.add(row)
        await session.flush()
    return row


async def load_charge_account(
    session: AsyncSession, *, tenant_id: UUID, provider: str, timing: Timing
) -> PaymentAccount | None:
    """The decrypted account IF the clinic may create a NEW charge with it in `timing`.

    `after` (the consultation already exists): any account that holds a key and is not
    revoked/pending - including a legacy row migrated from `tenant_credentials` and an
    `incomplete` one, so cancelling the add-on or a permission hiccup never stops a clinic's
    agenda. `before` (the deposit gates the booking): the account must be `connected` AND
    verified by the new connection flow (`last_verified_at`), because a never-verified or
    incomplete account is exactly what must not silently gate a booking.
    """
    row = await get_account_row(session, tenant_id=tenant_id, provider=provider)
    if row is not None:
        if row.status in (ACCOUNT_REVOKED, ACCOUNT_PENDING):
            return None
        if timing == "before" and (
            row.status != ACCOUNT_CONNECTED or row.last_verified_at is None
        ):
            return None
    elif timing == "before":
        return None
    account = await cfg.get_payment_account(session, tenant_id, provider)
    if account is None or not (account.api_key or account.access_token):
        return None
    return account


async def get_customer_id(
    session: AsyncSession, *, tenant_id: UUID, patient_id: UUID, provider: str
) -> str | None:
    """The provider-side customer id of a patient (this clinic's), or None. Asaas falls back
    to the legacy `patients.asaas_customer_id` column."""
    found = await session.scalar(
        select(PatientPaymentCustomer.provider_customer_id).where(
            PatientPaymentCustomer.tenant_id == tenant_id,
            PatientPaymentCustomer.patient_id == patient_id,
            PatientPaymentCustomer.provider == provider,
        )
    )
    if found or provider != "asaas":
        return found
    return await session.scalar(
        select(Patient.asaas_customer_id).where(
            Patient.id == patient_id, Patient.tenant_id == tenant_id
        )
    )


async def remember_customer_id(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    patient_id: UUID,
    provider: str,
    provider_customer_id: str,
) -> None:
    """Upsert the (tenant, patient, provider) customer id. Stores ONLY the provider's opaque
    id - never a document or e-mail (decision D7). Dual-writes the legacy Asaas column."""
    existing = await session.scalar(
        select(PatientPaymentCustomer).where(
            PatientPaymentCustomer.tenant_id == tenant_id,
            PatientPaymentCustomer.patient_id == patient_id,
            PatientPaymentCustomer.provider == provider,
        )
    )
    if existing is None:
        session.add(
            PatientPaymentCustomer(
                tenant_id=tenant_id,
                patient_id=patient_id,
                provider=provider,
                provider_customer_id=provider_customer_id,
            )
        )
    else:
        existing.provider_customer_id = provider_customer_id
    if provider == "asaas":
        patient = await session.scalar(
            select(Patient).where(Patient.id == patient_id, Patient.tenant_id == tenant_id)
        )
        if patient is not None:
            patient.asaas_customer_id = provider_customer_id
