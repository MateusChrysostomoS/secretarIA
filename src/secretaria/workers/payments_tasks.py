"""arq jobs for the Pix deposit (sinal) payment lifecycle.

Kept in its own module (not workers/tasks.py), mirroring the existing addon-plugin split (see
plugins/reminders.py's module docstring): the payment path is a distinct concern from the
WhatsApp message pipeline, and workers/tasks.py is already large. workers/arq_worker.py
imports and registers the jobs below.

* `process_payment_event(ctx, event_row_id)` - the per-clinic webhook path: applies one durable
  `payment_webhook_events` row (services/payments/event_processor.py). The argument is ONLY the
  row id: arq logs every job's arguments, so no secret and no payload may ride on it.
* `fulfill_paid_charge_job(ctx, deposit_id)` - the post-payment step
  (services/payments/fulfillment.py), retryable from the deposit's own state.
* `process_asaas_event(...)` - the LEGACY global-route job, kept until every clinic has moved to
  its per-clinic URL (the API and the worker deploy separately, so a queued legacy job must
  still be processable by the new worker).
"""

from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import PaymentWebhookEvent, Tenant
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import (
    deposit_lifecycle,
    event_processor,
    fulfillment,
    registry,
)
from secretaria.services.payments.provider import ProviderError

logger = get_logger(__name__)


def enqueue_for(ctx: dict):
    """An `enqueue(job_name, argument)` bound to this worker's own arq pool."""

    async def enqueue(name: str, argument: str) -> None:
        await ctx["redis"].enqueue_job(name, argument)

    return enqueue


async def process_payment_event(ctx: dict, event_row_id: str) -> None:
    """arq job: apply one stored payment webhook event, then run its post-commit steps.

    A failure is COUNTED on the row (attempts, a symbolic error code) and re-raised so arq
    retries; after `MAX_EVENT_ATTEMPTS` the row is `failed`. A paid charge whose webhook keeps
    failing is still found by the reconciliation job, which polls the provider.
    """
    row_id = UUID(event_row_id)
    async with async_session_factory() as session:
        try:
            result = await event_processor.process_payment_event_row(session, row_id)
        except Exception as exc:
            await session.rollback()
            code = exc.code if isinstance(exc, ProviderError) else type(exc).__name__
            await event_processor.record_event_failure(session, row_id, error_code=code)
            await session.commit()
            raise

        row = await session.get(PaymentWebhookEvent, row_id)
        tenant = await session.get(Tenant, row.tenant_id) if row is not None else None
        if tenant is not None:
            await event_processor.run_post_commit(
                session, tenant, result, redis=ctx.get("redis"), enqueue=enqueue_for(ctx)
            )
    logger.info("payment_event_job_done", outcome=result.outcome)


async def fulfill_paid_charge_job(ctx: dict, deposit_id: str) -> None:
    """arq job: run the post-payment step for one paid deposit (idempotent)."""
    async with async_session_factory() as session:
        outcome = await fulfillment.fulfill_paid_charge(session, deposit_id=UUID(deposit_id))
        await session.commit()
    logger.info("payment_fulfillment_job_done", outcome=outcome)


async def delete_provider_customer_job(
    ctx: dict, tenant_id: str, provider: str, provider_customer_id: str
) -> None:
    """arq job: ask a provider to delete a patient's customer record (LGPD erasure).

    Enqueued by `DELETE /internal/privacy/.../subjects/{wa_id}` AFTER the local erasure
    committed; arguments are opaque ids only. Skips quietly when the clinic has no account
    anymore or the provider has no `delete_customer`; a retryable provider failure propagates
    so arq retries.
    """
    target = registry.find_provider(provider)
    delete = getattr(target, "delete_customer", None)
    if delete is None:
        logger.info("payment_customer_delete_skipped", reason="no_capability", provider=provider)
        return
    async with async_session_factory() as session:
        account = await cfg.get_payment_account(session, UUID(tenant_id), provider)
    if account is None:
        logger.info("payment_customer_delete_skipped", reason="no_account", provider=provider)
        return
    await delete(account, provider_customer_id)
    logger.info("payment_customer_deleted", provider=provider)


async def process_asaas_event(
    ctx: dict,
    *,
    event_id: str,
    event_type: str,
    payment_id: str | None,
    access_token: str | None,
) -> None:
    """LEGACY arq job: apply one event of the old global `/webhooks/asaas` route.

    All of the legitimate, expected outcomes (unknown payment, failed auth, duplicate
    delivery) are handled inside `apply_asaas_event` itself and never raise here. Any
    UNEXPECTED exception is allowed to propagate so arq can retry the job — the idempotency
    ledger (ProcessedAsaasEvent) makes a retry safe.
    """
    async with async_session_factory() as session:
        outcome = await deposit_lifecycle.apply_asaas_event(
            session,
            event_id=event_id,
            event_type=event_type,
            payment_id=payment_id,
            access_token_header=access_token,
        )
    logger.info("asaas_event_processed", event_type=event_type, outcome=outcome)
