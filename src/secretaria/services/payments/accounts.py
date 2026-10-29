"""Payment accounts and per-patient customer ids: the DB side of the provider core.

Every query here is scoped by `tenant_id` (or by the opaque `webhook_slug`, which itself
resolves to exactly one tenant). Secrets are never handled here: encryption and the single
decrypt point are `services/tenant_config.py` (`get_payment_account`,
`set_payment_account_secrets`).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
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
from secretaria.services.payments import failure_reason
from secretaria.services.payments.failure_reason import FailureReason
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


ChargeHealth = Literal["ok", "failing", "untested"]


def _utc(value: datetime) -> datetime:
    """Naive datetimes (SQLite) are taken as UTC so they compare with aware ones."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def record_charge_failure(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    provider: str,
    reason: FailureReason | str,
    stage: str,
) -> None:
    """Remember that the last Pix charge with this account failed (`reason` is stored as its
    string value, `stage` is a short step name - never provider text or patient data).

    Flushes; the caller commits. Does nothing when the (tenant, provider) row does not exist:
    recording a failure never creates an account.
    """
    row = await get_account_row(session, tenant_id=tenant_id, provider=provider)
    if row is None:
        return
    try:
        reason_value = FailureReason(reason).value
    except ValueError:  # a failure recorder must never raise: keep the vocabulary closed
        reason_value = FailureReason.unknown.value
    row.last_charge_failure_reason = reason_value
    row.last_charge_failure_stage = stage[:16]
    row.last_charge_failure_at = datetime.now(UTC)
    await session.flush()


async def record_charge_success(session: AsyncSession, *, tenant_id: UUID, provider: str) -> None:
    """Remember that a Pix charge with this account just worked. The last failure stays on
    the row as history; `charge_health` compares the two timestamps.

    Flushes; the caller commits. Does nothing when the (tenant, provider) row does not exist.
    """
    row = await get_account_row(session, tenant_id=tenant_id, provider=provider)
    if row is None:
        return
    row.last_charge_ok_at = datetime.now(UTC)
    await session.flush()


def charge_health(row: ClinicPaymentAccount) -> ChargeHealth:
    """`failing` when the last failure is newer than the last success (or nothing succeeded
    since); `ok` when a success exists and is at least as new as the last failure; otherwise
    `untested` (never charged).

    Owner, 2026-10-29: `failing` (the red "Sinal com problema") clears ONLY with a real
    successful charge (`record_charge_success`) newer than the last failure. A connection check
    (`last_verified_at`: a reconnection, the "Tentar de novo" webhook step, the daily recheck)
    never clears it - the daily recheck used to turn the badge grey with nothing fixed. The
    breaker reads the connection check on its own (`deposit_failure_notice.breaker_open`)."""
    failure_at = row.last_charge_failure_at
    ok_at = row.last_charge_ok_at
    if failure_at is not None and (ok_at is None or _utc(failure_at) > _utc(ok_at)):
        return "failing"
    if ok_at is not None:
        return "ok"
    return "untested"


@dataclass(frozen=True)
class ChargeHealthView:
    """What the clinic's screen shows about the account's charges (TASK-071 Plan 3, task 4):
    the verdict, and the last failure as a closed category, its plain sentence and when (UTC).
    Never provider text: a stored value outside the vocabulary reads as `unknown`."""

    health: ChargeHealth
    last_failure_reason: str | None
    last_failure_text: str | None
    last_failure_at: datetime | None


def charge_health_view(row: ClinicPaymentAccount) -> ChargeHealthView:
    """`charge_health` plus the last failure of the row, ready for the status endpoint. The last
    failure stays as history once a newer success turned the verdict `ok`."""
    reason = failure_reason.coerce(row.last_charge_failure_reason)
    failed_at = row.last_charge_failure_at
    return ChargeHealthView(
        health=charge_health(row),
        last_failure_reason=reason.value if reason is not None else None,
        last_failure_text=failure_reason.text_for(reason),
        last_failure_at=_utc(failed_at) if failed_at is not None else None,
    )


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
