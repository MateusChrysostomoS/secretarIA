"""End-to-end proof of the `after` timing against the Asaas SANDBOX (TASK-016, Task 22).

Runs the REAL service code (no fakes) against a throwaway LOCAL Postgres and the Asaas sandbox:
connect the key (real permission probes, real webhook creation) -> create the Pix deposit
(real customer + charge + QR) -> pay the QR with the sandbox's own payer -> reconcile (polls the
provider, injects the synthetic PAID event) -> process it (getCharge confirms, atomically PAID)
-> replay it (no-op) -> a second deposit is cancelled (real DELETE) -> attention item check.

    $env:DATABASE_URL = 'postgresql+asyncpg://postgres@127.0.0.1:5544/t016_e2e'  # migrated
    $env:ASAAS_BASE_URL               = 'https://api-sandbox.asaas.com/v3'
    $env:ASAAS_SANDBOX_API_KEY        = '<sandbox key, all permissions; also used to PAY the QR>'
    $env:ASAAS_SANDBOX_API_KEY_SCOPED = '<OPTIONAL: Clientes+Cobrancas+Webhooks only>'
    $env:ENCRYPTION_KEY               = '<a throwaway Fernet key>'
    uv run python scripts/asaas_sandbox_e2e.py

Automatic-or-skip: no sandbox key in the environment -> one `skipped_all` line, exit 0. With a
key it refuses a non-local database and a non-sandbox host. Prints one line per step with
outcomes and ids only - never a key, a token, a document or a Pix code.
"""

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx


def require_local_database(url: str) -> str:
    host = urlparse(url).hostname if url else None
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to run: DATABASE_URL must point at a LOCAL throwaway database")
    return url


def require_sandbox_base_url(url: str) -> str:
    if "sandbox" not in (url or ""):
        raise SystemExit("refusing to run: ASAAS_BASE_URL must be the Asaas SANDBOX")
    return url


def require_sandbox_key(value: str | None) -> str:
    if not value or not value.startswith("$aact_hmlg_"):
        raise SystemExit("refusing to run: need SANDBOX keys ($aact_hmlg_...) in the environment")
    return value


def resolve_keys(environ) -> tuple[str | None, str | None, str | None]:
    """(default key, scoped key, skip reason). A missing or non-sandbox key is a SKIP; without a
    scoped key the default key plays that role too."""
    full = environ.get("ASAAS_SANDBOX_API_KEY") or ""
    scoped = environ.get("ASAAS_SANDBOX_API_KEY_SCOPED") or ""
    if not full:
        return None, None, "no_sandbox_key"
    if not full.startswith("$aact_hmlg_"):
        return None, None, "key_not_sandbox"
    return full, (scoped if scoped.startswith("$aact_hmlg_") else full), None


def step(name: str, **facts: object) -> None:
    print(" | ".join([name, *[f"{k}={v}" for k, v in facts.items()]]))


async def main() -> int:
    key_full, key_min, skip_reason = resolve_keys(os.environ)
    if skip_reason:
        step("skipped_all", reason=skip_reason)
        return 0
    require_local_database(os.environ.get("DATABASE_URL", ""))
    base_url = require_sandbox_base_url(os.environ.get("ASAAS_BASE_URL", ""))

    # This throwaway process trusts the four minimum probes (the flag is per-process; the code
    # default stays OFF): the end-to-end run must exercise the real accept/refuse path.
    os.environ["ASAAS_PROBES_QUALIFIED"] = "true"

    # Imports AFTER the guards so a refused run never touches a database engine.
    from fakeredis import FakeAsyncRedis
    from sqlalchemy import select

    from secretaria.core.database import async_session_factory, engine
    from secretaria.models import (
        Appointment,
        AppointmentStatus,
        Patient,
        PaymentAttentionItem,
        PixDeposit,
        Tenant,
    )
    from secretaria.services.payments import (
        accounts,
        asaas_connection,
        deposit_lifecycle,
        event_processor,
        jobs,
        payer_data,
    )

    tag = uuid4().hex[:8]
    async with async_session_factory() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name=f"E2E {tag}",
            phone_number_id=f"e2e-{tag}",
            pix_deposit_enabled=True,
            pix_deposit_percent=30,
            appointment_types=[
                {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 20,00"}
            ],
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id=f"55119{tag[:8]}"[:14], name="E2E")
        session.add(tenant)
        await session.flush()  # Postgres enforces the FK: the tenant row must exist first
        session.add(patient)
        await session.commit()
        step("seed", tenant=tenant.id)

        connection = await asaas_connection.connect_asaas_account(
            session,
            tenant_id=tenant.id,
            api_key=key_min,
            webhook_base_url=os.environ.get("E2E_WEBHOOK_BASE_URL", "https://example.com"),
        )
        await session.commit()
        step(
            "connect",
            outcome=connection.outcome,
            status=connection.status,
            code=connection.code,
            reason=connection.incomplete_reason,
            excess=",".join(connection.excess_groups) or "-",
        )
        if connection.outcome not in ("connected", "incomplete"):
            # Without the scoped key the default key holds excess permissions, so a REAL refusal
            # (`key_excess_permissions`) is the expected and correct outcome: it proves D14 against
            # the sandbox. The rest needs an accepted key, so it is SKIPPED.
            step("skipped_rest", reason=f"connect_{connection.outcome}", needs="scoped_key")
            return 0

        async def _appointment(event_id: str) -> Appointment:
            appointment = Appointment(
                id=uuid4(),
                tenant_id=tenant.id,
                patient_id=patient.id,
                google_event_id=event_id,
                appointment_type="Consulta",
                phone=patient.wa_id,
                status=AppointmentStatus.SCHEDULED,
                start_at=datetime.now(UTC) + timedelta(days=3),
            )
            session.add(appointment)
            await session.commit()
            return appointment

        # The payer's CPF is asked in the conversation and kept transiently (decision D7): the
        # real Asaas refuses a charge without it ("preencher o CPF ou CNPJ do cliente").
        redis = FakeAsyncRedis()
        conversation_id = uuid4()
        await payer_data.remember(
            redis, tenant_id=tenant.id, conversation_id=conversation_id, kind="document",
            value="24971563792",  # a public sandbox-style test CPF, not a person
        )

        # 1) create + pay + reconcile + process + replay
        first = await _appointment(f"e2e-1-{tag}")
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=first, waba_token=None,
            redis=redis, conversation_id=conversation_id,
        )
        await session.commit()
        step(
            "create_deposit",
            created=deposit is not None,
            copy_paste_len=len(deposit.pix_copy_paste or "") if deposit else 0,
        )
        if deposit is None or not deposit.pix_copy_paste:
            return 1

        async with httpx.AsyncClient(timeout=20.0) as http:
            paid = await http.post(
                f"{base_url}/pix/qrCodes/pay",
                headers={"access_token": key_full},
                json={"qrCode": {"payload": deposit.pix_copy_paste}, "value": 6.0},
            )
        step("pay_qr_in_sandbox", status=paid.status_code)
        if paid.status_code != 200 and deposit.asaas_payment_id:
            # The default key may lack the withdrawal permission that paying a QR needs: the
            # sandbox's own "confirm payment" (sandbox-only endpoint) produces the same RECEIVED.
            async with httpx.AsyncClient(timeout=20.0) as http:
                confirmed = await http.post(
                    f"{base_url}/sandbox/payment/{deposit.asaas_payment_id}/confirm",
                    headers={"access_token": key_full},
                    json={},
                )
            step("confirm_payment_in_sandbox", status=confirmed.status_code)

        sent: list[tuple[str, str]] = []

        async def enqueue(name: str, argument: str) -> None:
            sent.append((name, argument))

        report = await jobs.reconcile_open_charges(
            session, now=datetime.now(UTC), enqueue=enqueue, older_than=timedelta(0)
        )
        step("reconcile", checked=report.checked, paid_found=report.paid_found, queued=len(sent))
        if not sent:
            return 1
        row_id = sent[0][1]
        first_result = await event_processor.process_payment_event_row(session, UUID(row_id))
        replay = await event_processor.process_payment_event_row(session, UUID(row_id))
        await session.refresh(deposit)
        refreshed = deposit
        step(
            "process",
            outcome=first_result.outcome,
            replay=replay.outcome,
            status=refreshed.status.value,
        )

        # 2) an unpaid deposit is voided at Asaas when its consultation is cancelled
        second = await _appointment(f"e2e-2-{tag}")
        voided = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=second, waba_token=None,
            redis=redis, conversation_id=conversation_id,
        )
        await session.commit()
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=second
        )
        await session.commit()
        voided_now = await session.get(PixDeposit, voided.id)
        step("cancel_unpaid", outcome=outcome, deposit_status=voided_now.status.value)

        # 3) the money rows never produced an automatic refund; attention items are the only trace
        items = (await session.scalars(select(PaymentAttentionItem))).all()
        reasons = ",".join(sorted(i.reason for i in items)) or "-"
        step("attention_items", count=len(items), reasons=reasons)
        account = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
        step("account", status=account.status, ready_for_before=bool(account.last_verified_at))
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
