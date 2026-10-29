"""`/menu3`: a TEST-ONLY command that puts one patient back at their first contact.

The founder re-tests the patient flow many times on the test clinics; before this
the only way back to the LGPD notice was a fresh Portal visit or the WhatsApp-only
`/dangerously-remove-context` (which deletes the Patient row - impossible on the
Portal, whose Patient row IS the link to the patient's brain-api account).

`/menu3` keeps the patient and resets everything conversational instead:

  * deletes this conversation's messages (what the Portal and the staff console
    read - both come from this table), its booking holds and its PII token map;
  * puts every flow / reactivation / handover field back to what a brand-new
    `Conversation` row carries;
  * clears `Patient.lgpd_accepted_at`, the only fact the consent gate reads, so the
    next turn owes consent again.

What it deliberately does NOT touch: appointments (and through them Google events,
reminders and Pix deposits - cancelling is a separate action), `consent_events`
(the append-only LGPD ledger: the next "Concordo" adds a row, nothing is erased),
the patient's identity (`channel`/`external_id`/`wa_id`/`email`, so the Portal
session keeps working) and the patient's name (see `reset_conversation_for_debug`),
other conversations and other patients.

Allowed only for the tenants in `Settings.debug_reset_tenant_ids`; for every other
tenant `/menu3` is an ordinary message and none of this module runs.
"""

from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.models import (
    BookingHold,
    Conversation,
    ConversationPiiTokenMap,
    FlowState,
    HandoverState,
    Message,
    Patient,
    Tenant,
)
from secretaria.services.greeting_template import render_greeting
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.greeting import _fit_clinic_description

DEBUG_RESET_COMMAND = "/menu3"


def is_debug_reset_command(body: str | None) -> bool:
    """True for `/menu3` alone - surrounding whitespace and letter case ignored.

    Whole-body match, like `is_menu_command`: "/menu3 de novo" is not the command.
    """
    if not body:
        return False
    return body.strip().casefold() == DEBUG_RESET_COMMAND


def debug_reset_allowed(tenant_id: UUID) -> bool:
    """Whether `/menu3` is a command for this tenant (DEBUG_RESET_TENANT_IDS)."""
    return tenant_id in get_settings().debug_reset_tenant_ids


async def reset_conversation_for_debug(
    session: AsyncSession, *, conversation: Conversation, patient: Patient
) -> dict[str, int]:
    """Wipe `conversation` back to a first contact, inside the caller's transaction.

    Every statement is scoped to this one conversation id (the patient row is the
    conversation's own), so nothing of another conversation or patient can be
    reached. Returns the deleted-row counts for the audit log line.

    The name stays. A brand-new Portal patient gives it right after the e-mail and
    BEFORE consent, and nothing after consent ever asks for it again; `/menu3` keeps
    the e-mail (the account link), so it lands on the step that follows the name -
    the LGPD notice, which carries "✏️ Mudar nome" whenever a name is stored. The
    name step stays testable from that button.
    """
    conversation_id = conversation.id
    messages = await session.execute(
        delete(Message).where(Message.conversation_id == conversation_id)
    )
    holds = await session.execute(
        delete(BookingHold).where(BookingHold.conversation_id == conversation_id)
    )
    pii_maps = await session.execute(
        delete(ConversationPiiTokenMap).where(
            ConversationPiiTokenMap.conversation_id == conversation_id
        )
    )

    # What a freshly created Conversation row carries (models/conversation.py).
    conversation.handover_state = HandoverState.BOT_ACTIVE
    conversation.last_human_message_at = None
    conversation.last_bot_message_at = None
    conversation.flow_state = FlowState.IDLE
    conversation.flow_step = None
    conversation.flow_selected_type = None
    conversation.flow_selected_day = None
    conversation.flow_selected_slot = None
    conversation.flow_selected_professional_id = None
    conversation.flow_managing_appointment_id = None
    conversation.flow_selected_insurance = None
    conversation.flow_attendee_name = None
    conversation.flow_draft = None
    conversation.flow_replaces_appointment_id = None
    conversation.flow_edit_draft = None
    conversation.reactivation_origin = None

    # The fact the consent gate reads (`_route_inbound_turn`): False = consent owed.
    patient.lgpd_accepted_at = None
    patient.lgpd_accepted = False
    await session.flush()

    return {
        "messages_deleted": messages.rowcount or 0,
        "booking_holds_deleted": holds.rowcount or 0,
        # Not "..._token_...": the log redactor masks any key containing "token".
        "pii_maps_deleted": pii_maps.rowcount or 0,
    }


def debug_reset_reply(
    *, tenant: Tenant, conversation_id: UUID, patient_ref: str, channel: str
) -> _ReplyContext:
    """The opening a first contact gets: the frame, button-free, then the LGPD notice.

    The same frame `_first_contact_reply` renders, but with `debug_reset` instead of
    `send_consent_notice` + `probe_pending_identity`: the probe would mirror a
    VERIFIED Portal account's consent and skip the notice, which is exactly the
    screen the founder is resetting to see.
    """
    return _ReplyContext(
        channel=channel,
        conversation_id=conversation_id,
        tenant_id=tenant.id,
        patient_ref=patient_ref,
        inbound_body="",
        greeting_override=render_greeting(tenant.clinic_name, _fit_clinic_description(tenant)),
        greeting_buttons=[],
        debug_reset=True,
    )
