"""Outbound email — operational alerts + onboarding transactional templates.

Uses stdlib smtplib run in a thread pool so the async worker is never blocked.
Two independent send paths share the same SMTP connection plumbing
(`_smtp_send`) but have separate settings/kill-switches:

  * The ORIGINAL alert path (`send_calendar_alert` / `send_human_backup_alert`)
    - fail-open, always-on whenever `SMTP_HOST` is set (SMTP_FROM_EMAIL /
      SMTP_FROM_NAME identity). Unchanged behaviour.
  * The onboarding TRANSACTIONAL path (`send_transactional_email_message`,
    contract v1 §4 endpoint 6 / §10 / §12) - gated by its OWN `EMAIL_ENABLED`
    switch (default off) even when SMTP_HOST is already configured for the
    alerts above, with its own EMAIL_FROM_ADDRESS / EMAIL_FROM_NAME identity.
    Called by the `send_transactional_email` arq task (workers/tasks.py),
    itself enqueued by `POST /internal/notifications/email`.

Both paths are fail-open: if SMTP is not configured/enabled or the send
fails, a warning is logged and processing continues normally. NEITHER path
ever raises into its caller.
"""

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from enum import StrEnum

import structlog

from secretaria.config import get_settings

logger = structlog.get_logger(__name__)


def _smtp_send(to_email: str, subject: str, body: str, from_addr: str, from_name: str) -> None:
    """Blocking SMTP send — called via asyncio.to_thread.

    Shared connection/auth plumbing for both send paths in this module; only
    the From address/name differ between them (each caller resolves its own).
    """
    settings = get_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
        if settings.SMTP_USE_TLS:
            smtp.starttls()
        if settings.SMTP_USERNAME and settings.SMTP_PASSWORD:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(msg)


def _send_sync(to_email: str, subject: str, body: str) -> None:
    """Blocking SMTP send for the operational-alert path — called via asyncio.to_thread.

    NOTE: test_tasks_helpers.py asserts this exact function (by `__name__`)
    is the one passed to `asyncio.to_thread` for the alert path — keep this
    name and signature stable.
    """
    settings = get_settings()
    from_addr = settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME
    _smtp_send(to_email, subject, body, from_addr, settings.SMTP_FROM_NAME)


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


async def send_human_backup_alert(to_email: str, clinic_name: str) -> None:
    """Email the clinic owner when the human_backup_24_7 addon engages
    (an inbound message arrived outside business hours and was handed off).

    No-ops silently when SMTP_HOST is not configured.
    """
    settings = get_settings()
    if not settings.SMTP_HOST:
        return

    subject = f"[SecretarIA] Mensagem fora do horário — {clinic_name}"
    body = (
        f"Olá,\n\n"
        f"Um paciente escreveu para '{clinic_name}' fora do horário de atendimento "
        f"configurado.\n\n"
        f"A conversa foi encaminhada para a secretária humana (o bot ficará em "
        f"silêncio nela) e o paciente já recebeu uma mensagem confirmando o "
        f"recebimento.\n\n"
        f"— Equipe SecretarIA"
    )

    try:
        await asyncio.to_thread(_send_sync, to_email, subject, body)
        logger.info("human_backup_alert_email_sent", to=to_email, clinic=clinic_name)
    except Exception as exc:
        logger.warning(
            "human_backup_alert_email_failed",
            error=str(exc),
            to=to_email,
            clinic=clinic_name,
        )


async def send_cancellation_escalation_alert(
    to_email: str, clinic_name: str, whatsapp_link: str | None
) -> None:
    """Email the clinic when a patient could NOT be told their consultation was cancelled.

    The last resort of `workers/tasks.py::send_cancellation_notice`: every
    retry inside the notice's validity window has been spent and WhatsApp is
    still refusing the send. Somebody is going to turn up to a consultation
    that no longer exists unless a human tells them, so this failure has to
    leave the server — an ERROR line in a log nobody is watching is not
    "handled".

    Carries the `wa.me` deep link the hub already offers for the free path, so
    the doctor can write from their own phone in one tap. That link contains
    the patient's number, which is why it goes in the BODY and never into a
    log line (see the `logger.info` below — clinic name and nothing else).

    Fail-open and silent when SMTP_HOST is not configured, exactly like the
    two alerts above: this is the escalation, it cannot itself become a new
    failure to escalate.
    """
    settings = get_settings()
    if not settings.SMTP_HOST:
        return

    subject = f"[SecretarIA] Paciente NÃO avisado do cancelamento — {clinic_name}"
    link_block = (
        f"Fale com o paciente por aqui:\n{whatsapp_link}\n\n"
        if whatsapp_link
        else "Não há um número de WhatsApp registrado para este paciente.\n\n"
    )
    body = (
        f"Olá,\n\n"
        f"Uma consulta de '{clinic_name}' foi cancelada, mas a SecretarIA NÃO "
        f"conseguiu avisar o paciente pelo WhatsApp — todas as tentativas "
        f"falharam.\n\n"
        f"O paciente não sabe que a consulta foi desmarcada. Avise-o "
        f"manualmente.\n\n"
        f"{link_block}"
        f"— Equipe SecretarIA"
    )

    try:
        await asyncio.to_thread(_send_sync, to_email, subject, body)
        logger.info("cancellation_escalation_email_sent", clinic=clinic_name)
    except Exception as exc:
        logger.warning(
            "cancellation_escalation_email_failed",
            error=str(exc),
            clinic=clinic_name,
        )


# ---------------------------------------------------------------------------
# Onboarding transactional email (contract v1 §4 endpoint 6 / §10 / §12)
# ---------------------------------------------------------------------------


def _send_transactional_sync(to_email: str, subject: str, body: str) -> None:
    """Blocking SMTP send for the transactional path — called via asyncio.to_thread.

    Uses EMAIL_FROM_ADDRESS/EMAIL_FROM_NAME (falling back to the alert path's
    SMTP_FROM_EMAIL/SMTP_FROM_NAME, then SMTP_USERNAME, when unset) so a
    deployment that only ever configured the legacy alert settings still gets
    a sane From identity for transactional email.
    """
    settings = get_settings()
    from_addr = settings.EMAIL_FROM_ADDRESS or settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME
    from_name = settings.EMAIL_FROM_NAME or settings.SMTP_FROM_NAME
    _smtp_send(to_email, subject, body, from_addr, from_name)


@dataclass(frozen=True)
class EmailTemplate:
    """One pt-BR transactional template: `str.format`-style subject + body."""

    subject: str
    body: str


class _SafeDict(dict):
    """`str.format_map` companion that leaves an unknown `{placeholder}` intact
    instead of raising `KeyError`.

    The `variables` dict for a template is supplied by a SIBLING service
    (brain-api's onboarding/doctor endpoints, via `POST
    /internal/notifications/email`) — this module has no control over, and
    must never crash on, a caller that omits an expected key. A missing
    variable renders as the literal `{name}` text rather than failing the
    send outright (the module contract is "never raises into callers").
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


# Variables used across templates below (a caller may omit any of these —
# see `_SafeDict`): `clinic_name`, `name` (person being addressed), `link`
# (a URL — invite/portal), `blocker_reason` (nudge templates only), `days`
# and `restart_url` (test_window_expired only).
_TEMPLATES: dict[str, EmailTemplate] = {
    "professional_invite": EmailTemplate(
        subject="Você foi convidado(a) para a equipe da {clinic_name} no SecretarIA",
        body=(
            "Olá, {name}!\n\n"
            "Você foi adicionado(a) à equipe da {clinic_name} no SecretarIA, o assistente "
            "de agendamento por WhatsApp da clínica.\n\n"
            "Para definir sua senha e acessar o painel, clique no link abaixo:\n"
            "{link}\n\n"
            "Este link é de uso único e expira em 72 horas.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    "password_reset": EmailTemplate(
        subject="Redefinição de senha — SecretarIA",
        body=(
            "Olá, {name}!\n\n"
            "Recebemos um pedido para redefinir a senha da sua conta.\n\n"
            "Para escolher uma nova senha, clique no link abaixo:\n"
            "{link}\n\n"
            "Este link é de uso único e expira em {ttl_minutes} minutos.\n\n"
            "Se você não pediu essa redefinição, pode ignorar este e-mail — sua senha "
            "continua a mesma.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    "retry_nudge_atividade_insuficiente": EmailTemplate(
        subject="Seu número no WhatsApp ainda está ganhando histórico",
        body=(
            "Olá!\n\n"
            "Seu número do WhatsApp na {clinic_name} ainda está ganhando histórico — é "
            "assim que o WhatsApp confirma que o número está realmente em uso antes de "
            "liberar a ativação automática da SecretarIA.\n\n"
            "Isso é normal e não exige nenhuma ação sua agora: continue usando o número "
            "normalmente, mandando e recebendo mensagens, e tentaremos novamente em breve.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    "retry_nudge_numero_em_outro_bsp": EmailTemplate(
        subject="Seu número precisa ser liberado de outro provedor",
        body=(
            "Olá!\n\n"
            "Identificamos que o número informado para a {clinic_name} já está vinculado a "
            "outro provedor de WhatsApp Business (BSP). Para conectar à SecretarIA, é "
            "preciso primeiro liberá-lo do provedor atual.\n\n"
            "Assim que o número estiver liberado, volte à tela de ativação e tente "
            "novamente — vamos continuar tentando automaticamente também.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    "retry_nudge_sem_acesso_admin_waba": EmailTemplate(
        subject="Precisamos de acesso de administrador à sua conta do WhatsApp Business",
        body=(
            "Olá!\n\n"
            "Para concluir a ativação da {clinic_name} no SecretarIA, é necessário ter "
            "acesso de administrador à conta do WhatsApp Business (WABA) vinculada à sua "
            "página do Facebook.\n\n"
            "Peça a quem administra a página para te conceder esse acesso e depois volte "
            "à tela de ativação para tentar novamente.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    "retry_nudge_sem_pagina_facebook": EmailTemplate(
        subject="Falta criar uma página no Facebook para ativar seu número",
        body=(
            "Olá!\n\n"
            "Para ativar o WhatsApp Business da {clinic_name} é necessário ter uma página "
            "no Facebook vinculada à conta. Ainda não encontramos uma página associada ao "
            "seu cadastro.\n\n"
            "Crie (ou vincule) uma página do Facebook e volte à tela de ativação para "
            "tentar novamente.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    "retry_nudge_outro": EmailTemplate(
        subject="Ainda não conseguimos ativar seu número no WhatsApp",
        body=(
            "Olá!\n\n"
            "Ainda não conseguimos concluir a ativação do WhatsApp da {clinic_name} no "
            "SecretarIA. Vamos continuar tentando automaticamente — mas se preferir, você "
            "pode revisar os dados na tela de ativação e tentar novamente a qualquer "
            "momento.\n\n"
            "Se o problema persistir, responda este e-mail que nossa equipe ajuda a "
            "resolver.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    "connection_success": EmailTemplate(
        subject="Seu WhatsApp foi conectado à SecretarIA!",
        body=(
            "Boas notícias!\n\n"
            "O número do WhatsApp da {clinic_name} foi conectado com sucesso à "
            "SecretarIA. Nos próximos minutos finalizamos a sincronização e, assim que a "
            "configuração (horários, serviços e agenda) estiver completa, sua secretária "
            "virtual entra no ar automaticamente.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    "config_reminder_pre_connection": EmailTemplate(
        subject="Falta pouco para sua secretária virtual entrar no ar",
        body=(
            "Olá!\n\n"
            "Notamos que a configuração da {clinic_name} no SecretarIA ainda não está "
            "completa (agenda, horários ou serviços) e o número do WhatsApp também ainda "
            "não foi conectado.\n\n"
            "Complete os dois passos no painel quando puder — assim que ambos estiverem "
            "prontos, sua secretária virtual começa a atender automaticamente.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    "config_reminder_connected": EmailTemplate(
        subject="Finalize a configuração da sua secretária virtual",
        body=(
            "Olá!\n\n"
            "O WhatsApp da {clinic_name} já está conectado, mas a configuração (agenda do "
            "Google, horários de atendimento ou serviços) ainda não está completa.\n\n"
            "Finalize esses dados no painel para que sua secretária virtual comece a "
            "atender os pacientes automaticamente.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    "closing_email": EmailTemplate(
        subject="Estamos encerrando seu período de ativação",
        body=(
            "Olá!\n\n"
            "Faz um bom tempo que não conseguimos concluir a ativação do WhatsApp da "
            "{clinic_name} no SecretarIA, então vamos parar de enviar lembretes "
            "automáticos por enquanto.\n\n"
            "Se você ainda quiser ativar a secretária virtual, é só voltar à tela de "
            "ativação no painel quando estiver pronto(a) — nada foi perdido, seus dados "
            "continuam salvos.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    "test_window_expired": EmailTemplate(
        subject="Não conseguimos ativar seu WhatsApp no período de teste",
        body=(
            "Olá!\n\n"
            "O período de teste da {clinic_name} chegou ao fim ({days} dias) e não "
            "conseguimos concluir a ativação do seu número no WhatsApp — a causa mais "
            "comum é a Meta ainda não ter aprovado o número para o WhatsApp "
            "Coexistence, geralmente por atividade baixa nele.\n\n"
            "Fique tranquilo(a): nada foi cobrado e sua assinatura foi cancelada "
            "automaticamente.\n\n"
            "Se quiser tentar de novo, é só reiniciar o período de teste por aqui:\n"
            "{restart_url}\n\n"
            "Se preferir, responda este e-mail que nossa equipe ajuda a resolver.\n\n"
            "— Equipe SecretarIA"
        ),
    ),
    # The one template that is NOT about onboarding/account lifecycle: it tells
    # a professional that a patient just booked with them.
    #
    # Deliberately minimal. SMTP is cleartext-in-transit into a mailbox this
    # product does not control, so the body carries only what the doctor needs
    # to RECOGNISE the appointment and open the agenda — never the patient's
    # phone number, never a price, never anything clinical.
    #
    # TWO agenda links, deliberately, because they open different things:
    # `agenda_line` is this product's own agenda screen (`DOCTOR_AGENDA_URL`,
    # whichever frontend the installation serves) and `calendar_line` is the
    # event itself on the clinic's Google Calendar (`Appointment.
    # google_event_link`, Google's private `htmlLink`). The second is only
    # ever right for someone who OWNS that calendar — which the professional
    # does and a patient does not (patients get
    # services/calendar.py::build_patient_calendar_link instead). Either can
    # be missing, so both are optional lines.
    #
    # `insurance_line`, `agenda_line` and `calendar_line` all arrive
    # PRE-RENDERED from the caller (empty string when there is nothing to say)
    # rather than being conditionalised here, because `EmailTemplate` is a
    # flat `str.format_map` pair with no branching.
    "appointment_booked_professional": EmailTemplate(
        subject="Nova consulta marcada — {when}",
        body=(
            "Olá, {professional_name}!\n\n"
            "Uma nova consulta foi marcada com você pela SecretarIA:\n\n"
            "Paciente: {patient_name}\n"
            "Serviço: {service}\n"
            "Quando: {when}\n"
            "{insurance_line}"
            "\n"
            "{agenda_line}"
            "{calendar_line}"
            "— Equipe SecretarIA"
        ),
    ),
}


class EmailOutcome(StrEnum):
    """WHY a transactional send did or did not happen.

    The bool `send_transactional_email_message` returns cannot tell "the
    mailer is switched off" from "SMTP just blipped", and a caller that wants
    to RETRY must not retry the first one: no number of attempts turns
    `EMAIL_ENABLED=false` into a delivered email, and treating a deliberate
    kill-switch as a failure would raise an alarm on every booking of every
    clinic that has not enabled mail.

    Only `SEND_FAILED` is worth retrying. `DISABLED` is a configuration
    choice; `UNKNOWN_TEMPLATE` and `RENDER_FAILED` are code defects that will
    reproduce identically on every attempt — they escalate, they do not retry.
    """

    SENT = "sent"
    DISABLED = "disabled"
    UNKNOWN_TEMPLATE = "unknown_template"
    RENDER_FAILED = "render_failed"
    SEND_FAILED = "send_failed"

    @property
    def is_transient(self) -> bool:
        """True iff trying the exact same send again could plausibly succeed."""
        return self is EmailOutcome.SEND_FAILED


async def send_transactional_email_result(to: str, template: str, variables: dict) -> EmailOutcome:
    """Render `template` with `variables` and send it. NEVER raises.

    Same work as `send_transactional_email_message`, but reports WHICH of the
    non-send paths was taken — see `EmailOutcome`. Callers that only need
    "did it go out?" should keep using the bool wrapper below.
    """
    settings = get_settings()
    if not settings.EMAIL_ENABLED or not settings.SMTP_HOST:
        logger.info(
            "transactional_email_noop",
            template=template,
            reason="disabled" if not settings.EMAIL_ENABLED else "smtp_unconfigured",
        )
        return EmailOutcome.DISABLED

    tpl = _TEMPLATES.get(template)
    if tpl is None:
        logger.warning("transactional_email_unknown_template", template=template)
        return EmailOutcome.UNKNOWN_TEMPLATE

    safe_vars = _SafeDict(variables or {})
    try:
        subject = tpl.subject.format_map(safe_vars)
        body = tpl.body.format_map(safe_vars)
    except Exception as exc:
        logger.warning("transactional_email_render_failed", template=template, error=str(exc))
        return EmailOutcome.RENDER_FAILED

    try:
        await asyncio.to_thread(_send_transactional_sync, to, subject, body)
    except Exception as exc:
        logger.warning("transactional_email_send_failed", template=template, error=str(exc))
        return EmailOutcome.SEND_FAILED

    logger.info("transactional_email_sent", template=template)
    return EmailOutcome.SENT


async def send_transactional_email_message(to: str, template: str, variables: dict) -> bool:
    """Render `template` with `variables` and send it. NEVER raises.

    Returns False (a clean no-op) when: `EMAIL_ENABLED` is off (default),
    `SMTP_HOST` is empty, `template` is not a known id, rendering fails, or
    the SMTP send itself fails. The caller (the `send_transactional_email`
    arq task, workers/tasks.py) always gets a bool — never an exception —
    so a misconfigured or blipping SMTP server can never turn an arq job
    into a retry loop.

    Thin wrapper over `send_transactional_email_result`, for the callers that
    genuinely have nothing different to do per failure reason.
    """
    outcome = await send_transactional_email_result(to, template, variables)
    return outcome is EmailOutcome.SENT
