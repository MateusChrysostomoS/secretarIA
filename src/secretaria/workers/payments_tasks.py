"""arq job(s) for the Pix deposit (sinal) payment lifecycle.

Kept in its own module (not workers/tasks.py), mirroring the existing
addon-plugin split (see plugins/reminders.py's module docstring): the Asaas
webhook path is a distinct concern from the WhatsApp message pipeline, and
workers/tasks.py is already large. workers/arq_worker.py just imports and
registers `process_asaas_event` in `WorkerSettings.functions`.
"""

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.services.payments import deposit_lifecycle

logger = get_logger(__name__)


async def process_asaas_event(
    ctx: dict,
    *,
    event_id: str,
    event_type: str,
    payment_id: str | None,
    access_token: str | None,
) -> None:
    """arq job: apply one Asaas webhook event. Enqueued by POST /webhooks/asaas.

    All of the legitimate, expected outcomes (unknown payment, failed auth,
    duplicate delivery) are handled inside `apply_asaas_event` itself and
    never raise here. Any UNEXPECTED exception is allowed to propagate so arq
    can retry the job — the idempotency ledger (ProcessedAsaasEvent) makes a
    retry safe.
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
