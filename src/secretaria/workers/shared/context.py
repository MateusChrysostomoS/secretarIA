"""context - split out of workers/tasks.py (TASK-023)."""

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from secretaria.services.booking_hold import (
    HeldSlot,
)
from secretaria.services.channel_sender import (
    CHANNEL_WHATSAPP,
)


@dataclass(frozen=True)
class _ReactivationDirective:
    """How to handle a returning patient's answer to the 'continuar?' prompt.

    kind="resume" re-renders where they left off (or, for an LLM-mode origin,
    falls through to the agent with history intact); kind="reset" drops the
    saved flow and shows the menu. `origin` is the FlowState value captured when
    the prompt was offered.
    """

    kind: Literal["resume", "reset"]
    origin: str

@dataclass(frozen=True)
class _ReplyContext:
    """Minimal data needed to send a bot reply once the inbound DB txn commits."""

    conversation_id: UUID | None
    # How to address this patient back: their `wa_id` on WhatsApp, their
    # `external_id` on Brain-Message. Named for the ROLE, not the format, since
    # the day a second channel existed the old name `patient_wa_id` became a
    # lie for half the rows it carried.
    patient_ref: str
    # The ROUTING text of the inbound (see `_route_inbound_turn`): for a tap on
    # a data-carrying list row it still carries the row's payload - "Dra. Ana
    # (<uuid>)" - because the flow router and the agent resolve the tap from
    # it. Never store or display it: the stored `Message.body` is the title.
    inbound_body: str
    # Which surface this turn arrived on, and therefore which one the reply
    # leaves by. `_reply_sender` is the only reader; every send site downstream
    # stays channel-blind. Defaults to WhatsApp so every `_ReplyContext` built
    # outside `_route_inbound_turn` (the audio-transcription and human-echo
    # paths, and ~20 tests) keeps its exact pre-refactor meaning.
    channel: str = CHANNEL_WHATSAPP
    # The tenant this turn resolved to. Carried explicitly because the
    # `service_unavailable` degrade below has NO conversation to look it up
    # from, and every outbound send must still go out on this tenant's own
    # WhatsApp number/token — never a global env fallback (PROMPT_FIX_21).
    tenant_id: UUID | None = None
    # When set, this is the tenant's verbatim first-contact (or returning)
    # greeting: it is sent as a single message and the LLM is NOT invoked.
    greeting_override: str | None = None
    # Optional quick-reply labels rendered as buttons on the greeting. The label
    # the patient taps comes back as their next message body.
    greeting_buttons: list[str] = field(default_factory=list)
    # When True, the LGPD terms notice goes out as a SECOND message right after
    # the greeting, carrying the "✅ Concordo" button. Set only on a first
    # contact by a subject whose `Patient.lgpd_accepted_at` is still NULL.
    send_consent_notice: bool = False
    # When True, this turn is nothing BUT the consent re-prompt: the subject
    # was already asked, said something else, and is still owed a legal basis.
    # No greeting, no routing, no LLM - see the consent gate in
    # `_persist_inbound_message`.
    send_consent_reminder: bool = False
    # When True, the tenant's bot is not activated: send a single polite
    # fallback and do nothing else (no conversation, no LLM).
    service_unavailable: bool = False
    # A Brain-Message patient sent a FILE: the whole answer is the fixed receipt
    # (`_handle_attachment_received`) - transport, not analysis.
    attachment_received: bool = False
    # Set when this inbound is a returning patient's answer to the "quer
    # continuar?" prompt: drives resume-vs-reset in `_send_bot_reply`.
    reactivation: "_ReactivationDirective | None" = None
    # Set when this inbound is a tap on a reminder's deposit-aware action
    # button (schemas/webhook.py::extract_action_button): (action,
    # appointment_id). Captured in `_persist_inbound_message` BEFORE the
    # handover/flow/LLM gates (see that function) and handled by
    # `_handle_action_button`, called first thing in `_send_bot_reply`.
    action_button: tuple[str, str] | None = None
    # Set when this inbound is a tap on one of the greeting's fixed action
    # buttons (schemas/webhook.py::extract_greeting_button) AND this tenant
    # has no deterministic flow to dispatch it to (flows disabled - see
    # flow_router.flows_enabled). Holds the raw suffix ("agendar"/"remarcar"/
    # "cancelar"/a legacy digit/anything else unrecognized). Handled by
    # `_handle_greeting_button_unavailable`, called early in `_send_bot_reply`
    # - see `_persist_inbound_message`'s greeting-button short-circuit for why
    # a flows-ENABLED tenant's tap never sets this (it flows through the
    # normal text-routed path instead, which route() already handles).
    greeting_button_unavailable: str | None = None
    # Set when this inbound is a `/menu`-style command (see `is_menu_command`):
    # a NON-DESTRUCTIVE request to go back to the main menu. Handled by
    # `_handle_show_main_menu` from `_send_bot_reply`, i.e. only AFTER the
    # allowlist, tenant-active, handover, entitlement and plugin gates have
    # all been cleared like any other turn (PROMPT_FIX_18).
    menu_requested: bool = False
    # --- Brain-Message inline identity (services/pending_identity.py) ----
    # Set on a Brain-Message FIRST contact. The greeting goes out, then
    # `_send_bot_reply` asks brain-api whether this visitor still owes an
    # address; only then does it choose between the e-mail question and the
    # LGPD notice. The probe lives there, not in `_persist_inbound_message`,
    # because that function runs inside the inbound transaction and an HTTP
    # call has no business holding one open.
    probe_pending_identity: bool = False
    # The visitor typed something that is not six digits WHILE a slot is
    # held for them. Before the code became a gate this ended the wait; now
    # leaving would cost them the reservation, so the card is re-sent
    # instead and its Voltar button stays the explicit way out. Carries
    # the live hold so the re-ask can name the window and the minutes left.
    pending_code_reprompt: HeldSlot | None = None
    # A syntactically valid address the visitor just typed, already normalized.
    # `_send_bot_reply` claims it against brain-api and then sends the LGPD
    # notice — in that order, so a patient never consents before we have
    # somewhere to send their confirmations.
    pending_email_claim: str | None = None
    # The visitor answered the e-mail question with something that is not an
    # address. The re-ask is the WHOLE turn.
    pending_email_invalid: bool = False
    # A 6-digit code the visitor typed while the conversation was waiting for
    # one. `_send_bot_reply` spends it against brain-api.
    pending_code: str | None = None
    # The visitor TAPPED one of the three buttons on the code-notice card
    # instead of typing a code (`services/pending_identity.py`:
    # identity_back / identity_resend / identity_change_email). Carries the
    # option id, never the label: the id is what
    # `_validated_brain_message_reply_id` already checked against the cards
    # this conversation offered, while the label is display copy that a
    # wording change may move at any time. One field for all three because
    # they are one card and `_handle_identity_card_action` owns the whole
    # turn for every one of them.
    identity_action: str | None = None
    # --- The name question (services/patient_name.py), BOTH channels ------
    # WhatsApp first contact: the greeting goes out and the name question
    # follows it in the slot the LGPD notice used to take. The state was
    # already moved to AWAITING_NAME inside the inbound transaction.
    send_name_request: bool = False
    # The patient answered the name question with a name (already written to
    # `Patient.name` inside the inbound transaction): the LGPD notice follows.
    # Also set when a second unreadable answer gives up on the name — the
    # notice is the next step either way.
    name_captured: bool = False
    # The answer did not look like a name; the re-ask is the whole turn.
    name_invalid: bool = False
