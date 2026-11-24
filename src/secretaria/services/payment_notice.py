"""Aviso neutro de pagamento (TASK-093, Plano 2) - só as funções puras.

Quando a clínica não está com o pagamento em dia e um paciente escreve, a secretária
não conduz a conversa e manda UM aviso fixo e neutro por paciente por dia. Aqui vivem
o texto, a decisão e a chave do controle diário; o envio fica em
`workers/shared/payment_notice.py` (a camada `services/` não importa `workers/`).
"""

from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from secretaria.services.entitlements_client import EntitlementSummary

#: Texto único, fixo e neutro. O dono pode trocá-lo aqui, em um só lugar.
PAYMENT_NOTICE_TEXT = (
    "No momento esta clínica não está atendendo por aqui. "
    "Para falar com a clínica, entre em contato por telefone."
)

_DEFAULT_TZ = "America/Sao_Paulo"
_MIN_TTL_SECONDS = 60


def _zone(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or _DEFAULT_TZ)
    except Exception:
        return ZoneInfo(_DEFAULT_TZ)


def should_notify(summary: EntitlementSummary | None) -> bool:
    """True só quando o estado FOI lido e a clínica não está em dia.

    `None` (Brain fora do ar / sem clínica) é "não consegui ler", não "não paga":
    fica em silêncio para não avisar uma clínica que está em dia.
    """
    if summary is None:
        return False
    return not (summary.active and summary.secretaria_enabled)


def clinic_today(now_utc: datetime, tz_name: str | None) -> date:
    """O dia de hoje no fuso da clínica."""
    return now_utc.astimezone(_zone(tz_name)).date()


def seconds_until_clinic_midnight(now_utc: datetime, tz_name: str | None) -> int:
    """Segundos até a próxima meia-noite no fuso da clínica (mínimo 60)."""
    zone = _zone(tz_name)
    local = now_utc.astimezone(zone)
    next_midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), zone)
    seconds = int((next_midnight - local).total_seconds())
    return max(seconds, _MIN_TTL_SECONDS)


def notice_key(tenant_id: UUID, patient_ref: str, today: date) -> str:
    """Chave do controle "1 por dia": clínica + paciente + dia (no fuso da clínica)."""
    return f"payment_notice:{tenant_id}:{patient_ref}:{today.isoformat()}"
