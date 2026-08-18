"""Telling a patient their doctor cancelled — and offering a way back in.

============================ READ THIS BEFORE EDITING ============================

Until this module existed, a hub cancellation notified the patient ONLY if the
doctor happened to type a message (`AppointmentCancel.custom_message`). Blank
box, silent cancellation: the patient learned about it by showing up. The
notification is now unconditional, and the doctor's text became a
*justification* folded into a standard sentence rather than the whole body.

Three things make this module more than a string builder:

**The 24h window is a hard Meta rule, and it costs money.** Outside 24h since
the patient's last inbound message, WhatsApp accepts NO free-form message —
not even plain text. Only a pre-approved template (HSM), and every template
send is billed as a conversation. So the send path forks, and the expensive
fork is never taken without the doctor's explicit say-so (`allow_paid`), which
the hub asks for with the price shown. `plugins/reminders.py` already forks the
same way against the same rule; `last_inbound_at` here is the shared read.

**The buttons must survive the round trip.** WhatsApp caps a reply button title
at 20 characters and `WhatsAppClient.send_buttons` silently truncates past it —
a label of 21 would reach the patient visibly cut in half. Every label below is
<= 20 on purpose; see LABEL_* and the note there.

**Never resend.** Handled by the caller through the `processed_events` ledger
(`workers/tasks.py::send_cancellation_notice`), because a duplicate here is not
just inbox noise like `plugins/professional_notification.py` — outside the
window it is a second real charge.

Never logs a phone number, the justification text, or any patient content.

=================================================================================
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from secretaria.models import Conversation, Message, MessageDirection

# Meta's free-form window. Same constant the reminder fork uses; kept here so a
# reader of this module does not have to go find it.
WINDOW_HOURS = 24

# --- Rebooking buttons ----------------------------------------------------
#
# WhatsApp reply-button titles are capped at 20 chars and `send_buttons`
# truncates silently, so the spec's "Agendar outro horário" (21) and "Agendar
# com outro médico" (24) would have shipped as "Agendar outro horári" and
# "Agendar com outro mé". These say the same thing inside the limit. The ids
# are what the flow router routes on, so they never depend on the wording.
LABEL_REBOOK_SAME = "Outro horário"
LABEL_REBOOK_OTHER_PRO = "Outro médico"
LABEL_REBOOK_DECLINE = "Cancelar"

# The ids are the "<action>|<appointment_id>" data-carrying family already used
# by the deposit-aware reminder (plugins/reminders.py) and decoded by
# schemas/webhook.py::extract_action_button — which handles BOTH carriers: an
# interactive reply-button's `id` inside the 24h window, and a template
# quick-reply's `payload` outside it. Carrying the appointment id is what lets
# the rebooking branch know which doctor cancelled, and which service to keep.
ACTION_REBOOK_SAME = "rebooksame"
ACTION_REBOOK_OTHER_PRO = "rebookother"
ACTION_REBOOK_DECLINE = "rebookno"

_REBOOK_ACTIONS: tuple[tuple[str, str], ...] = (
    (ACTION_REBOOK_SAME, LABEL_REBOOK_SAME),
    (ACTION_REBOOK_OTHER_PRO, LABEL_REBOOK_OTHER_PRO),
    (ACTION_REBOOK_DECLINE, LABEL_REBOOK_DECLINE),
)


def rebook_buttons(appointment_id) -> list[tuple[str, str]]:
    """(id, label) pairs for the cancelled appointment's rebooking offer."""
    return [(f"{action}|{appointment_id}", label) for action, label in _REBOOK_ACTIONS]


def rebook_payloads(appointment_id) -> list[str]:
    """Just the ids, for a template's quick-reply `button_payloads`."""
    return [bid for bid, _label in rebook_buttons(appointment_id)]


UNKNOWN_PROFESSIONAL = "responsável"


def build_cancellation_text(professional_name: str | None, justification: str | None) -> str:
    """The patient-facing body, with the justification folded in when present.

    Exactly the shape the hub's cancel modal previews, so the doctor is never
    surprised by what went out. The justification is quoted so it reads as the
    doctor's own words rather than as the clinic's policy, and is omitted
    entirely — not rendered as an empty quote — when there is none.
    """
    name = (professional_name or "").strip() or UNKNOWN_PROFESSIONAL
    text = f"O médico {name} desmarcou a sua consulta!"
    reason = (justification or "").strip()
    if reason:
        text += f'\n\nJustificativa do médico: "{reason}"'
    return text


def rebooking_invitation() -> str:
    """The line that turns a bare cancellation into an offer to rebook."""
    return "Quer remarcar? É só escolher:"


async def last_inbound_at(session, tenant_id: UUID, patient_id: UUID) -> datetime | None:
    """Timestamp of the patient's most recent INBOUND message, or None.

    Same read `plugins/reminders.py::_last_inbound_at` performs; duplicated as
    a public helper here rather than imported from a plugin, because a service
    reaching up into `plugins/` would invert this repo's layering rule
    (CLAUDE.md: api -> workers -> services -> models -> core).
    """
    conversation_id = await session.scalar(
        select(Conversation.id).where(
            Conversation.tenant_id == tenant_id, Conversation.patient_id == patient_id
        )
    )
    if conversation_id is None:
        return None
    return await session.scalar(
        select(Message.created_at)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.INBOUND,
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive timestamp (SQLite) as UTC; pass tz-aware through."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def is_inside_window(last_inbound: datetime | None, *, now: datetime | None = None) -> bool:
    """True when a free-form message is still allowed.

    `None` (the patient never wrote, or has no conversation) is OUTSIDE — the
    safe direction: it forces the paid/opt-in path instead of attempting a
    free-form send Meta would reject.
    """
    if last_inbound is None:
        return False
    reference = now or datetime.now(UTC)
    return reference - _as_utc(last_inbound) < timedelta(hours=WINDOW_HOURS)


def whatsapp_deep_link(phone: str | None) -> str | None:
    """`https://wa.me/<digits>` so the doctor can write from their own phone.

    The zero-cost alternative to the paid template: the message leaves the
    doctor's own WhatsApp, not the API, so Meta bills nothing. `None` when
    there is no usable number to link to.
    """
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"https://wa.me/{digits}" if digits else None


# Meta wants an underscored locale ("pt_BR"), the tenant stores a hyphenated
# one ("pt-BR"). `plugins/reminders.py` holds an identical private copy;
# consolidating the two means editing the reminder send path, which is
# unrelated risk for this change — left as the one duplication here, marked so
# whoever touches either notices the other.
_META_LANGUAGE_CODES = {"pt-BR": "pt_BR", "pt": "pt_BR", "en": "en_US", "es": "es_ES"}


def meta_language_code(language: str | None) -> str:
    """Tenant language -> Meta template locale, best-effort, never raising."""
    if not language:
        return "pt_BR"
    return _META_LANGUAGE_CODES.get(language, language.replace("-", "_") or "pt_BR")


def join_blocks(*parts: str | None) -> str:
    """Join the non-empty parts with one blank line between them.

    Keeps the paragraph-joining in one place instead of scattering `"\n\n"`
    through the worker, and drops empties so an absent deposit notice or
    invitation leaves no trailing whitespace in the sent body.
    """
    return "\n\n".join(part for part in parts if part)
