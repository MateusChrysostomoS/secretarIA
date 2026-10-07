"""`create_event` and `cancel_event` on the v2 toolset: blind tools that only STAGE.

TASK-030 P4, owner's decision of 2026-10-03: the AI keeps tools NAMED `create_event` and
`cancel_event`, but on a v2 turn they are these - and they never write to the agenda, never
read an event and never take a free-text title, description, end, name or Google event id.
Each one builds exactly what the workflow's own hand-back tools build and raises the same
exception, so the patient lands on the same card through the same path:

  * `create_event(start, service, professional)` raises `BookingDraftRequested` with the
    service, the professional, and the day and time taken from `start` - the draft the P2
    resolver lands (workers/shared/sentinels.py::_handle_set_booking_draft). The "pra quem"
    is left unknown here: the resolver uses the answer the conversation already recorded,
    or asks "Essa consulta é pra você?". The card's Confirmar - today's
    `flow_router._handle_confirmation`, with the Portal code gate, the holds and the
    authorization - is the only thing that books; the event title and description are
    composed there, server side, from the stored patient/attendee record.
  * `cancel_event(appointment)` resolves the "(ref AAAA-MM-DD HH:MM)" reference ONLY among
    the upcoming appointments of THIS conversation's patient in THIS turn's tenant, and
    raises `ManageAppointmentRequested("cancel", appointment=...)` - the same request
    `manage_existing_appointment` v2 sends (P3), which stops at the "Confirmar o
    cancelamento?" card. A reference that names none, or two, of the patient's
    appointments is an error dict: nothing is guessed.

What the model reads back is never data: the hand-back ends the turn, and every refusal is
`{"error": "<frase>"}` that never echoes what the model sent (ai/tool_output.py declares
both tools as error-only). The legacy implementations of these names (ai/tools.py) stay for
the switch-off path and refuse by themselves on a v2 turn (`_blocked_by_toolset_v2`).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool

from secretaria.ai.tools import (
    BLIND_STAGING_VARIANT,
    TOOL_BLOCK_BAD_APPOINTMENT,
    BookingDraftRequested,
    ManageAppointmentRequested,
    _conversation_id_ctx,
    _draft_professional_id,
    _tenant_config_ctx,
    _tenant_id_ctx,
)
from secretaria.core.logging import get_logger
from secretaria.services.manage_request import (
    ACTION_CANCEL,
    APPOINTMENT_REF_FORMAT,
    appointment_ref,
    parse_appointment_ref,
)

logger = get_logger(__name__)

# Why a staging tool refused (stable enums for `agent_tool_blocked`; never the arguments).
TOOL_BLOCK_NO_CLINIC = "no_clinic"
TOOL_BLOCK_BAD_START = "bad_start"
TOOL_BLOCK_NO_PATIENT = "no_patient"
TOOL_BLOCK_UNKNOWN_APPOINTMENT = "unknown_appointment"
TOOL_BLOCK_AMBIGUOUS_APPOINTMENT = "ambiguous_appointment"

# ASCII digits only (`\d` would take any Unicode digit); seconds, if sent, must be :00.
_START_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})[T ]([0-9]{2}:[0-9]{2})(?::00)?")
_TEXT_MAX = 120
_DEFAULT_ZONE = "America/Sao_Paulo"

_NO_CLINIC_ERROR = "Nenhuma clínica configurada para esta conversa."
_BAD_START_ERROR = (
    "start precisa estar no formato AAAA-MM-DDTHH:MM, no fuso da clínica (ex.: 2026-10-08T10:00)."
)
_BAD_REF_ERROR = (
    'appointment precisa ser a referência "(ref AAAA-MM-DD HH:MM)" de uma consulta da lista '
    '"consultas marcadas" - nunca um id de evento.'
)
_UNKNOWN_REF_ERROR = (
    "Nenhuma consulta marcada deste paciente começa nesse horário. Use a referência exata "
    'de "consultas marcadas", ou chame manage_existing_appointment com action "cancel" '
    "para o paciente escolher pelos botões."
)
_AMBIGUOUS_REF_ERROR = (
    "Mais de uma consulta deste paciente começa nesse horário. Chame "
    'manage_existing_appointment com action "cancel" para o paciente escolher pelos botões.'
)
_NO_PATIENT_ERROR = (
    "Não consegui identificar o cadastro deste paciente. Não diga que algo foi cancelado: "
    'chame manage_existing_appointment com action "cancel" para ele escolher pelos botões.'
)
_TEMPORARY_ERROR = (
    "Não consegui consultar as consultas deste paciente agora. NUNCA diga que algo foi "
    "cancelado; tente de novo em instantes."
)


def _blocked(tool_name: str, reason: str, message: str) -> dict:
    logger.info("agent_tool_blocked", tool=tool_name, reason=reason)
    return {"error": message}


def _clinic_zone() -> ZoneInfo:
    """The clinic's zone, the one the "(ref ...)" lines were written in (P3)."""
    name = getattr(_tenant_config_ctx.get(), "timezone", None) or _DEFAULT_ZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(_DEFAULT_ZONE)


def _parse_start(text: str) -> tuple[date, time] | None:
    match = _START_RE.fullmatch((text or "").strip())
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1)), time.fromisoformat(match.group(2))
    except ValueError:
        return None


async def _own_upcoming(tenant_id: UUID, conversation_id: UUID) -> list[dict] | None:
    """THIS conversation's patient's upcoming appointments in `tenant_id`; None = no patient."""
    # Lazy, like ai/tools.py: no DB/ORM import at module load.
    from secretaria.core.database import async_session_factory
    from secretaria.models import Conversation
    from secretaria.services.patient_context import load_upcoming_appointments

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if (
            conversation is None
            or conversation.patient_id is None
            or conversation.tenant_id != tenant_id
        ):
            return None
        return await load_upcoming_appointments(session, tenant_id, conversation.patient_id)


@tool("create_event")
async def create_event_v2(start: str, service: str = "", professional: str = "") -> dict:
    """Prepara a marcação de uma consulta no horário `start` e leva o paciente ao cartão de
    confirmação. NÃO marca nada: quem marca é o paciente, tocando em Confirmar no cartão;
    até lá, nada está marcado nem reservado. O fluxo confere serviço, profissional, dia e se
    o horário está livre, e pergunta o que faltar - inclusive para quem é a consulta, se o
    paciente ainda não respondeu. Não existe título, descrição nem nome aqui: quem escreve
    na agenda é o fluxo. Se o paciente disse para quem é a consulta ou citou o convênio, use
    set_booking_draft, que leva esses campos.

    Args:
        start: Início pedido, AAAA-MM-DDTHH:MM, no fuso da clínica (ex.: 2026-10-08T10:00) -
            de preferência dentro de uma janela devolvida por get_availability.
        service: Nome EXATO de um serviço da clínica (ou vazio).
        professional: Nome do profissional (ou vazio).
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return _blocked("create_event", TOOL_BLOCK_NO_CLINIC, _NO_CLINIC_ERROR)
    parsed = _parse_start(start)
    if parsed is None:
        # The value is never echoed nor logged: the model may have copied the patient's words.
        return _blocked("create_event", TOOL_BLOCK_BAD_START, _BAD_START_ERROR)
    day, at = parsed
    professional_id = await _draft_professional_id(tenant_id, (professional or "").strip())
    # The same draft set_booking_draft v2 raises (P2b): the worker lands it through the
    # resolver and, with everything valid, on the details + confirmation card (P3). No
    # insurance and no "pra quem" here - the resolver uses what the conversation recorded.
    raise BookingDraftRequested(
        (service or "").strip()[:_TEXT_MAX] or None,
        professional_id,
        None,
        attendee=None,
        day=day,
        time=at,
        professional_unresolved=bool((professional or "").strip()) and professional_id is None,
    )


@tool("cancel_event")
async def cancel_event_v2(appointment: str) -> dict:
    """Leva o paciente ao cartão "Confirmar o cancelamento?" de UMA consulta JÁ MARCADA
    dele. NÃO cancela nada: quem cancela é o paciente, tocando em Sim no cartão; até lá a
    consulta continua marcada - nunca diga que foi cancelada.

    Args:
        appointment: QUAL consulta, pela referência "(ref AAAA-MM-DD HH:MM)" mostrada em
            "consultas marcadas" (ex.: 2026-10-13 10:00). Nunca um id de evento.
    """
    tenant_id = _tenant_id_ctx.get()
    conversation_id = _conversation_id_ctx.get()
    if tenant_id is None or conversation_id is None:
        return _blocked("cancel_event", TOOL_BLOCK_NO_CLINIC, _NO_CLINIC_ERROR)
    try:
        reference = parse_appointment_ref(appointment)
    except ValueError:
        return _blocked("cancel_event", TOOL_BLOCK_BAD_APPOINTMENT, _BAD_REF_ERROR)
    try:
        upcoming = await _own_upcoming(tenant_id, conversation_id)
    except Exception as exc:
        logger.warning("agent_cancel_staging_failed", error_type=type(exc).__name__)
        return {"error": _TEMPORARY_ERROR}
    if upcoming is None:
        return _blocked("cancel_event", TOOL_BLOCK_NO_PATIENT, _NO_PATIENT_ERROR)
    zone = _clinic_zone()
    wanted = reference.strftime(APPOINTMENT_REF_FORMAT)
    matches: list[dict[str, Any]] = [
        row
        for row in upcoming
        if isinstance(row.get("start_at"), datetime)
        and appointment_ref(row["start_at"], zone) == wanted
    ]
    if not matches:
        return _blocked("cancel_event", TOOL_BLOCK_UNKNOWN_APPOINTMENT, _UNKNOWN_REF_ERROR)
    if len(matches) > 1:
        return _blocked("cancel_event", TOOL_BLOCK_AMBIGUOUS_APPOINTMENT, _AMBIGUOUS_REF_ERROR)
    # The same request manage_existing_appointment v2 sends (P3): the worker re-reads the
    # patient's appointments FRESH and stops at the cancel confirmation card.
    raise ManageAppointmentRequested(ACTION_CANCEL, appointment=reference)


# Read by ai/graph.py: `_tool_cache_key` keeps these apart from the legacy tools of the same
# names, and `effective_tools` lets ONLY this variant of a staging name into a v2 turn.
create_event_v2.metadata = {"cache_variant": BLIND_STAGING_VARIANT}
cancel_event_v2.metadata = {"cache_variant": BLIND_STAGING_VARIANT}
