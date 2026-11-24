"""Envio do aviso neutro de pagamento, no máximo 1 por dia por paciente (TASK-093, Plano 2).

Só é chamado como RESPOSTA a uma mensagem recebida do paciente, nos ramos que já
suprimem a resposta por falta de pagamento. Disparos proativos (abertura automática do
Portal, lembretes, avisos de cancelamento, ofertas de liberação) nunca passam por aqui.

Silêncio é o padrão: sem estado de pagamento legível, sem Redis ou com falha ao gravar o
controle diário, nada é enviado (melhor calar do que repetir ou avisar clínica em dia).
"""

from datetime import UTC, datetime

from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.services.entitlements_client import EntitlementSummary
from secretaria.services.payment_notice import (
    PAYMENT_NOTICE_TEXT,
    clinic_today,
    notice_key,
    seconds_until_clinic_midnight,
    should_notify,
)
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.sender import _reply_sender, _send_simple_text

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def send_payment_notice_once(
    reply: _ReplyContext,
    *,
    tenant: Tenant | None,
    waba_token: str | None,
    summary: EntitlementSummary | None,
    redis,
) -> bool:
    """Manda o aviso fixo uma vez por dia (fuso da clínica) por paciente.

    Devolve True só quando tentou enviar. Nunca levanta.
    """
    try:
        if tenant is None or redis is None or not should_notify(summary):
            return False
        now = _utcnow()
        tz_name = getattr(tenant, "timezone", None)
        key = notice_key(tenant.id, reply.patient_ref, clinic_today(now, tz_name))
        claimed = await redis.set(
            key, "1", ex=seconds_until_clinic_midnight(now, tz_name), nx=True
        )
        if not claimed:
            return False
        sender = _reply_sender(reply, tenant, waba_token)
        if sender is None:
            return False
        await _send_simple_text(reply.patient_ref, PAYMENT_NOTICE_TEXT, client=sender)
        logger.info(
            "payment_notice_sent",
            tenant_id=str(tenant.id),
            channel=reply.channel,
            conversation_id=str(reply.conversation_id),
        )
        return True
    except Exception as exc:
        logger.warning("payment_notice_failed", error_type=type(exc).__name__)
        return False
