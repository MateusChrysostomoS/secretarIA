"""Outbound email — operational alerts sent to clinic owners.

Uses stdlib smtplib run in a thread pool so the async worker is never blocked.
Fail-open: if SMTP is not configured or the send fails, a warning is logged
and processing continues normally.
"""

import asyncio
import smtplib
from email.message import EmailMessage

import structlog

from secretaria.config import get_settings

logger = structlog.get_logger(__name__)


def _send_sync(to_email: str, subject: str, body: str) -> None:
    """Blocking SMTP send — called via asyncio.to_thread."""
    settings = get_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    from_addr = settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME
    from_name = settings.SMTP_FROM_NAME
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
        if settings.SMTP_USE_TLS:
            smtp.starttls()
        if settings.SMTP_USERNAME and settings.SMTP_PASSWORD:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(msg)


async def send_calendar_alert(to_email: str, clinic_name: str) -> None:
    """Email the clinic owner when Google Calendar becomes unreachable.

    No-ops silently when SMTP_HOST is not configured.
    """
    settings = get_settings()
    if not settings.SMTP_HOST:
        return

    subject = f"[SecretarIA] Agenda do Google Calendar inacessível — {clinic_name}"
    body = (
        f"Olá,\n\n"
        f"A SecretarIA detectou que não consegue acessar o Google Calendar de "
        f"'{clinic_name}'.\n\n"
        f"Isso pode ocorrer por:\n"
        f"  • Revogação do acesso pelo Google (token expirado ou desconectado)\n"
        f"  • Instabilidade temporária na API do Google Calendar\n\n"
        f"Enquanto o problema persistir, novas consultas não poderão ser agendadas "
        f"automaticamente e as conversas serão encaminhadas para a secretária humana.\n\n"
        f"Para reconectar, acesse o painel da SecretarIA e refaça a autenticação "
        f"com o Google Calendar.\n\n"
        f"— Equipe SecretarIA"
    )

    try:
        await asyncio.to_thread(_send_sync, to_email, subject, body)
        logger.info("calendar_alert_email_sent", to=to_email, clinic=clinic_name)
    except Exception as exc:
        logger.warning(
            "calendar_alert_email_failed",
            error=str(exc),
            to=to_email,
            clinic=clinic_name,
        )
