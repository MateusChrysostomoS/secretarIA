"""opening - the context-aware first message for a patient who ENTERS a conversation.

Owner, 2026-10-05: the Portal must speak first, as soon as the patient enters,
and what it says depends on who they are. The first contact of an unknown
visitor (frame → e-mail question) already had its own door
(`workers/portal/open.py` + `_first_contact_reply`). This module is the other
half: a patient the clinic already KNOWS — a logged-in account, a verified
code, an account returning to its history — gets ONE of three messages:

  1. a live upcoming appointment → the canonical appointment reminder with
     [Confirmar, Cancelar, Alterar Dados] when reminders v2 is enabled. Its
     chat reminder row and ids use the existing reminder action handlers;
     switched-off clinics retain the previous greeting;
  2. the first appearance after a consult → "Como foi a sua consulta do dia
     DD/MM/AAAA?" plus the clinic's own post-consult text when configured;
  3. everyone else → the clinic's menu question (or "Como posso te ajudar?")
     with [🗓️ Agendar, Outro], which opens the usual booking flow.

Channel-neutral on purpose (it lives in `shared/`): the callers today are all
Portal paths, but nothing here knows the transport — `_apply_flow_result`
dispatches through the reply's channel like every other turn.
"""

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS, truncate_plain
from secretaria.models import (
    Appointment,
    Conversation,
    FlowState,
    Patient,
    Professional,
    Tenant,
    is_live_status,
)
from secretaria.services import reminder_hooks
from secretaria.services.flow_router import (
    FlowRouterResult,
    MenuBubble,
    main_menu_buttons,
    menu_label,
)
from secretaria.services.patient_context import (
    PatientOpeningState,
    find_post_consult_followup,
    resolve_patient_opening_state,
)
from secretaria.services.reminder_text import (
    LABEL_CANCEL,
    LABEL_CONFIRM,
    LABEL_EDIT,
    REMINDER_TERMINAL_TEXT,
    build_reminder_body,
    load_reminder_content,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.dispatch import _send_plain_reply
from secretaria.workers.shared.flow_runner import (
    _apply_flow_result,
)
from secretaria.workers.shared.greeting import (
    _adapt_greeting_has_upcoming,
    _greeting_buttons_for,
    _load_upcoming_greeting_data,
)
from secretaria.workers.shared.reminder_opening import _send_reminder_opening
from secretaria.workers.shared.text import (
    _as_utc,
)

logger = get_logger(__name__)

POST_CONSULT_DEFAULT_FOLLOWUP = (
    "Conte pra gente como você está se sentindo depois do atendimento. "
    "Se precisar de algo, é só tocar numa das opções abaixo."
)


class OpeningKind(enum.StrEnum):
    """Which first message the patient's context calls for (first match wins)."""

    UPCOMING = "upcoming"
    POST_CONSULT = "post_consult"
    MENU = "menu"


@dataclass(frozen=True)
class OpeningMessage:
    kind: OpeningKind
    body: str
    labels: list[str] = field(default_factory=list)
    appointment_id: UUID | None = None


def _first_name(name: str | None) -> str | None:
    parts = (name or "").split()
    return parts[0] if parts else None


def _hello(name: str | None) -> str:
    first = _first_name(name)
    return f"Olá, {first}! 😊" if first else "Olá! 😊"


def compose_post_consult_message(
    tenant: Tenant,
    patient_name: str | None,
    appointment: dict,
    doctor_name: str | None,
) -> str:
    """Pure: "Como foi a sua consulta do dia DD/MM/AAAA?" + the clinic's follow-up."""
    tz = ZoneInfo(tenant.timezone or "America/Sao_Paulo")
    day = _as_utc(appointment["start_at"]).astimezone(tz).strftime("%d/%m/%Y")
    attendee = (appointment.get("attendee_name") or "").strip()
    whose = f"a consulta de {attendee}" if attendee else "a sua consulta"
    question = f"Como foi {whose} do dia {day}"
    if doctor_name:
        question += f" com {doctor_name}"
    question += "?"
    follow_up = (tenant.post_consult_message or "").strip() or POST_CONSULT_DEFAULT_FOLLOWUP
    body = f"{_hello(patient_name)} {question}\n\n{follow_up}"
    return truncate_plain(body, MAX_INTERACTIVE_BODY_CHARS)


async def resolve_opening_message(conversation_id: UUID, tenant: Tenant) -> OpeningMessage | None:
    """Decide (read-only) which first message this conversation's patient gets.

    None only when the conversation or its patient cannot be found — the caller
    then sends nothing rather than guess.
    """
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if (
            conversation is None
            or conversation.patient_id is None
            or conversation.tenant_id != tenant.id
        ):
            return None
        patient = await session.get(Patient, conversation.patient_id)
        if patient is None or patient.tenant_id != tenant.id:
            return None
        context = await resolve_patient_opening_state(session, tenant.id, patient.id)
        if (
            context is not None
            and context.state
            in (PatientOpeningState.HAS_UPCOMING_SOON, PatientOpeningState.HAS_UPCOMING)
            and context.future_appointments
        ):
            if reminder_hooks.enabled_for(tenant):
                appointment = await session.scalar(
                    select(Appointment).where(
                        Appointment.id == UUID(context.future_appointments[0]["id"]),
                        Appointment.tenant_id == tenant.id,
                        Appointment.patient_id == patient.id,
                    )
                )
                if appointment is None:
                    return None
                content = await load_reminder_content(session, tenant, appointment)
                return OpeningMessage(
                    kind=OpeningKind.UPCOMING,
                    body=build_reminder_body(content),
                    labels=[LABEL_CONFIRM, LABEL_CANCEL, LABEL_EDIT],
                    appointment_id=appointment.id,
                )
            data = await _load_upcoming_greeting_data(session, tenant, context.future_appointments)
            body = _adapt_greeting_has_upcoming(
                _hello(patient.name), context.future_appointments, tenant, data
            )
            return OpeningMessage(
                kind=OpeningKind.UPCOMING,
                body=truncate_plain(body, MAX_INTERACTIVE_BODY_CHARS),
                labels=_greeting_buttons_for(tenant, body, context),
            )
        followup = await find_post_consult_followup(session, tenant.id, patient.id, conversation.id)
        if followup is not None:
            doctor = None
            if followup.get("professional_id"):
                professional = await session.get(Professional, UUID(followup["professional_id"]))
                doctor = professional.name if professional is not None else None
            return OpeningMessage(
                kind=OpeningKind.POST_CONSULT,
                body=compose_post_consult_message(tenant, patient.name, followup, doctor),
                labels=main_menu_buttons(),
            )
    return OpeningMessage(
        kind=OpeningKind.MENU, body=menu_label(tenant), labels=main_menu_buttons()
    )


async def _send_context_opening(
    reply: _ReplyContext,
    tenant: Tenant | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
    source: str = "portal_entry",
    still_current: Callable[[], Awaitable[bool]] | None = None,
    appointment_id: UUID | None = None,
) -> bool | None:
    """Send the context-aware first message; True when it was rendered.

    `still_current`, when given, is asked right before the write; False means
    the conversation moved on meanwhile and NOTHING is sent (returns None, so
    the caller can tell "superseded" from "failed to send").

    Every kind leaves the conversation at MENU. Reminder actions carry their
    own scoped ids; other buttons use `route()`'s greeting-button matches.
    Free text at
    MENU — the natural answer to "como foi a sua consulta?" — goes to the LLM.
    Nothing is deleted, exactly like `_handle_show_main_menu`, which this
    replaces on the paths that already know the patient.
    """
    if tenant is None:
        return False
    if appointment_id is not None:
        async with async_session_factory() as session:
            target = await session.scalar(
                select(Appointment)
                .join(Conversation, Conversation.patient_id == Appointment.patient_id)
                .where(
                    Appointment.id == appointment_id,
                    Appointment.tenant_id == tenant.id,
                    Conversation.id == reply.conversation_id,
                    Conversation.tenant_id == tenant.id,
                )
            )
            if target is None:
                return False
            if not is_live_status(target.status) or _as_utc(target.start_at) <= datetime.now(UTC):
                if still_current is not None and not await still_current():
                    return None
                return await _send_plain_reply(
                    reply,
                    tenant=tenant,
                    waba_token=waba_token,
                    body=REMINDER_TERMINAL_TEXT,
                    event="reminder_entry_terminal",
                )
    opening = (
        OpeningMessage(kind=OpeningKind.UPCOMING, body="", appointment_id=appointment_id)
        if appointment_id is not None
        else await resolve_opening_message(reply.conversation_id, tenant)
    )
    if opening is None:
        logger.warning(
            "conversation_opening_unresolved",
            conversation_id=str(reply.conversation_id),
            source=source,
        )
        return False
    result = FlowRouterResult(
        action="reply",
        bubbles=[MenuBubble(body=opening.body, labels=opening.labels)],
        flow_state=FlowState.MENU,
    )
    if still_current is not None and not await still_current():
        return None
    if opening.kind == OpeningKind.UPCOMING and opening.appointment_id is not None:
        # Same body, row ids, transport and action handlers as the cron reminder.
        card_reply = replace(
            reply,
            reminder_opening_appointment_id=opening.appointment_id,
            reminder_opening_first_contact=True,
        )
        # Persist before delivery, like every flow result. A patient can tap the
        # card as soon as it lands; never clear that action with a later MENU write.
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, reply.conversation_id)
            editing = conversation is not None and conversation.flow_state == FlowState.EDIT_BOOKING
        if not editing and source not in ("reminder_link", "clinic_link"):
            await _apply_flow_result(
                reply,
                FlowRouterResult(action="reply", flow_state=FlowState.MENU),
                patient_wa,
                redis=redis,
                tenant=tenant,
                waba_token=waba_token,
            )
        return await _send_reminder_opening(
            card_reply,
            tenant=tenant,
            waba_token=waba_token,
            still_current=still_current,
        )
    rendered = await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )
    logger.info(
        "conversation_opening_rendered",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        kind=opening.kind.value,
        source=source,
        rendered=rendered,
    )
    return rendered
