"""dispatch - split out of workers/tasks.py (TASK-023)."""

from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import (
    MAX_INTERACTIVE_BODY_CHARS,
    strip_decoration,
    truncate_plain,
)
from secretaria.models import (
    Tenant,
)
from secretaria.services.channel_sender import (
    sender_persists_outbound,
)
from secretaria.services.flow_router import (
    LABEL_BOOK,
    LABEL_CANCEL_APPT,
    LABEL_MANAGE_APPOINTMENT,
    LABEL_OTHER,
    LABEL_RESCHEDULE,
)
from secretaria.services.greeting_template import (
    CONSENT_BUTTON_LABEL,
    LGPD_CONSENT_MESSAGE,
)
from secretaria.services.whatsapp import (
    interactive_buttons_record,
)
from secretaria.workers.shared.bubbles import (
    _bubble_history_body,
    _bubble_interactive,
    _send_bubble,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.sender import (
    _record_outbound,
    _reply_sender,
)

logger = get_logger(__name__)


async def _dispatch_bubbles(
    reply: _ReplyContext,
    bubbles: list,
    tenant: Tenant | None = None,
    waba_token: str | None = None,
) -> int:
    """Send each bubble in order, recording outbound messages. Returns count sent.

    MVP: no retry. A transient send failure stops the turn; bubbles already sent
    are recorded so the LLM history stays consistent on the next inbound.
    `tenant`/`waba_token` select the per-tenant WhatsApp client. FAIL-CLOSED
    (PROMPT_FIX_21): a missing tenant or missing credentials sends NOTHING and
    returns 0 - it never falls back to the global env scaffold, which would
    answer this clinic's patient from another clinic's number.
    """
    client = _reply_sender(reply, tenant, waba_token)
    if client is None:
        logger.error(
            "worker_bot_reply_no_credential",
            conversation_id=str(reply.conversation_id),
            bubbles=len(bubbles),
        )
        return 0
    sent_count = 0
    for index, bubble in enumerate(bubbles):
        try:
            result = await _send_bubble(client, reply.patient_ref, bubble)
        except Exception as exc:
            logger.error(
                "worker_bot_reply_failed",
                error=str(exc),
                conversation_id=str(reply.conversation_id),
                bubble_index=index,
                bubble_kind=getattr(bubble, "kind", "?"),
            )
            break

        sent_count += 1
        if sender_persists_outbound(client):
            # The sender already wrote this bubble's row - on Brain-Message the
            # row IS the delivery (services/channel_sender.py), so recording a
            # second one here would double every reply in the patient's console.
            continue
        # The message is already delivered; a failure to record it must not
        # crash the turn (which would propagate, retry, and short-circuit on the
        # committed ProcessedEvent, losing the bubble entirely). Log and go on.
        try:
            await _record_outbound(
                reply.conversation_id,
                _bubble_history_body(bubble),
                result,
                interactive=_bubble_interactive(bubble),
            )
        except Exception as exc:
            logger.error(
                "worker_bot_reply_record_failed",
                error=str(exc),
                conversation_id=str(reply.conversation_id),
                bubble_index=index,
            )

    if sent_count:
        logger.info(
            "worker_bot_reply_sent",
            conversation_id=str(reply.conversation_id),
            bubbles=sent_count,
        )
    return sent_count

# Semantic payload-id suffix for each of the greeting's FIXED, product-defined
# action buttons (see _greeting_buttons_for) - used by _send_greeting below and
# read back by schemas/webhook.py::extract_greeting_button. Deliberately only
# covers these five code-constant labels; anything else (reactivation's
# Sim/Não, tenant-configurable) is NOT in this map on purpose - see
# _send_greeting's comment. LABEL_RESCHEDULE/LABEL_CANCEL_APPT stay: the
# HAS_UPCOMING(_SOON) trio still sends them (and old threads keep their
# buttons tappable long after the initial trio moved to "gerenciar").
_GREETING_ACTION_IDS: dict[str, str] = {
    LABEL_BOOK: "agendar",
    LABEL_MANAGE_APPOINTMENT: "gerenciar",
    LABEL_RESCHEDULE: "remarcar",
    LABEL_CANCEL_APPT: "cancelar",
    LABEL_OTHER: "outro",
}

# The one greeting-button suffix `_persist_inbound_message`'s flows-disabled
# short-circuit deliberately lets through: "Outro" promises the LLM, and the
# flows-disabled cohort's normal path IS the LLM.
_GREETING_LLM_ESCAPE_SUFFIX = _GREETING_ACTION_IDS[LABEL_OTHER]

async def _send_greeting(
    reply: _ReplyContext,
    *,
    tenant: Tenant,
    waba_token: str | None = None,
) -> None:
    """Send a tenant's initial or returning greeting as one verbatim message.

    With configured labels it goes out as an interactive reply-button message
    (the tapped label becomes the patient's next inbound); otherwise as plain
    text. The whole greeting is one WhatsApp message either way.
    """
    body = reply.greeting_override or ""
    if reply.greeting_buttons:
        # LAST line of defence on the interactive-body cap, in the same spirit
        # as send_list's re-application of truncate_list_row_title: a no-op for
        # a body that already fits. It matters because the body reaching here
        # is a SUM — the product frame (~843 chars) plus the clinic's
        # description plus whatever `_adapt_greeting_to_state` appended for a
        # patient with upcoming appointments — and `send_buttons` does NOT
        # truncate. One char over and Meta 400s, the except below logs it, and
        # the patient's first-ever message goes unanswered.
        body = truncate_plain(body, MAX_INTERACTIVE_BODY_CHARS)
    # Fail closed (PROMPT_FIX_21) rather than letting the credential error
    # escape into the arq job, which would retry the whole turn forever on
    # what is a configuration problem, not a transient one.
    client = _reply_sender(reply, tenant, waba_token)
    if client is None:
        logger.error(
            "worker_greeting_no_credential",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        return
    try:
        if reply.greeting_buttons:
            # The label still drives route()'s dispatch (as with every other
            # deterministic tap - see extract_inbound_body), so the id itself
            # doesn't have to. It carries semantic meaning anyway: a KNOWN
            # fixed action label (_GREETING_ACTION_IDS - the current
            # _greeting_buttons_for trio) gets a matching "greeting|<action>"
            # id, which `extract_greeting_button` uses to give a flows-
            # disabled tenant's tap a deterministic degrade instead of
            # route()'s unconditional LLM delegation. Any OTHER label - e.g.
            # the reactivation Sim/Não prompt, which is tenant-configurable
            # free text - gets a positional "reactivation|<index>" id
            # instead, deliberately never "greeting|<number>", so it can
            # never be confused with a legacy pre-deploy greeting-button tap
            # (which used exactly that numeric shape) by
            # `extract_greeting_button`.
            buttons = [
                (
                    f"greeting|{_GREETING_ACTION_IDS[strip_decoration(label)]}"
                    # Looked up through `strip_decoration` for the same reason
                    # every matcher normalises: since the greeting trio started
                    # rendering "🗓️ Agendar", the raw label is no longer a key
                    # in this dict. A plain `label in _GREETING_ACTION_IDS`
                    # would silently miss and hand the tap a positional
                    # `reactivation|N` id, which `extract_greeting_button`
                    # ignores - costing a flows-disabled tenant its
                    # deterministic degrade with nothing logged.
                    if strip_decoration(label) in _GREETING_ACTION_IDS
                    else f"reactivation|{index}",
                    label,
                )
                for index, label in enumerate(reply.greeting_buttons)
            ]
            result = await client.send_buttons(to=reply.patient_ref, body=body, buttons=buttons)
            # The card as the patient's screen shows it, for the staff console
            # (`Message.interactive`); `body` alone stays the history text.
            interactive = interactive_buttons_record(body, buttons)
        else:
            result = await client.send_text_message(to=reply.patient_ref, body=body)
            interactive = None
    except Exception as exc:
        # MVP: no retry (mirrors _send_bot_reply). The patient's message still
        # reached the human secretary; the auto-greeting is simply lost.
        logger.error(
            "worker_greeting_send_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )
        return

    if not sender_persists_outbound(client):
        await _record_outbound(reply.conversation_id, body, result, interactive=interactive)
    logger.info("worker_greeting_sent", conversation_id=str(reply.conversation_id))

async def _send_consent_notice(
    reply: _ReplyContext,
    *,
    tenant: Tenant,
    waba_token: str | None = None,
    body: str = LGPD_CONSENT_MESSAGE,
) -> None:
    """Send an LGPD terms message carrying the `✅ Concordo` button.

    Two callers, one shape: the notice that follows the first-contact greeting
    (`body` defaults to it), and the re-prompt for a subject who answered
    something else while consent was still pending (`CONSENT_REMINDER_MESSAGE`).
    Both need the same button, so they share the send rather than growing a
    second near-identical function.

    Separate from the greeting on purpose rather than appended to it: the
    greeting is already near WhatsApp's 1024-char interactive cap, and the
    notice needs its own button — a single message cannot carry both the
    action trio and the consent button.

    Best-effort, exactly like `_send_greeting`: a failure here is logged and
    dropped, never retried. The greeting has already gone out and the patient's
    turn is already served, so raising would make arq replay the whole turn and
    re-send the greeting. The subject is simply asked again on their next
    first contact, since nothing recorded an acceptance.
    """
    client = _reply_sender(reply, tenant, waba_token)
    if client is None:
        logger.error(
            "worker_consent_notice_no_credential",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        return
    # The id is semantic (not positional) so it can never be confused with a
    # `greeting|N` tap by `extract_greeting_button`; the LABEL is still what
    # `_is_consent_acceptance` matches on, since `extract_inbound_body` hands
    # back a plain button's title. One list for the send AND its record.
    buttons = [("consent|accept", CONSENT_BUTTON_LABEL)]
    try:
        result = await client.send_buttons(
            to=reply.patient_ref,
            body=body,
            buttons=buttons,
        )
    except Exception as exc:
        logger.error(
            "worker_consent_notice_send_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )
        return

    if reply.conversation_id is not None and not sender_persists_outbound(client):
        await _record_outbound(
            reply.conversation_id,
            body,
            result,
            interactive=interactive_buttons_record(body, buttons),
        )
    logger.info("worker_consent_notice_sent", conversation_id=str(reply.conversation_id))

async def _send_plain_reply(
    reply: _ReplyContext,
    *,
    tenant: Tenant,
    waba_token: str | None,
    body: str,
    event: str,
) -> None:
    """Send one button-free message on this turn's channel. Best-effort.

    The text-only twin of `_send_consent_notice`: same sender resolution, same
    fail-closed on a missing credential, same "log it and move on" on a send
    error. A separate function rather than a `buttons=None` parameter on that
    one, because its whole reason to exist is the consent button and a caller
    reading `_send_consent_notice(..., buttons=None)` would have to check what
    that even means.

    Used by the Brain-Message identity steps, all of which are typed answers
    with nothing to tap (`services/pending_identity.py`).
    """
    client = _reply_sender(reply, tenant, waba_token)
    if client is None:
        logger.error(
            f"{event}_no_credential",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        return
    try:
        result = await client.send_text_message(to=reply.patient_ref, body=body)
    except Exception as exc:
        logger.error(
            f"{event}_send_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )
        return
    if reply.conversation_id is not None and not sender_persists_outbound(client):
        await _record_outbound(reply.conversation_id, body, result)
    logger.info(event, conversation_id=str(reply.conversation_id))

async def _send_buttons_reply(
    reply: _ReplyContext,
    *,
    tenant: Tenant,
    waba_token: str | None,
    body: str,
    buttons: list[tuple[str, str]],
    event: str,
) -> None:
    """Send one CARD (body + tappable options) on this turn's channel. Best-effort.

    The button-carrying twin of `_send_plain_reply`, with the same sender
    resolution, the same fail-closed on a missing credential and the same "log
    it and move on" on a send error. Deliberately NOT a refactor of
    `_send_consent_notice`, whose three log event names (`worker_consent_notice_*`)
    are load-bearing in production dashboards and do not fit this function's
    `f"{event}_..."` scheme; the duplication is twenty lines and the alternative
    is renaming log lines nobody asked to rename.

    `buttons` is `(id, title)` pairs, and the ID is what comes back from a tap.
    The record and the delivered card are built from the same list, so the
    options a patient can send back are exactly the ones they were shown
    (`services/whatsapp.py::interactive_buttons_record`).
    """
    client = _reply_sender(reply, tenant, waba_token)
    if client is None:
        logger.error(
            f"{event}_no_credential",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        return
    try:
        result = await client.send_buttons(to=reply.patient_ref, body=body, buttons=buttons)
    except Exception as exc:
        logger.error(
            f"{event}_send_failed",
            # `error_type` and not `str(exc)`, unlike its plain-text twin: the
            # one card this function sends carries a masked address in its
            # BODY, and a driver error raised while inserting that row can
            # carry its bound parameters into the exception's text. The type
            # is what an operator acts on anyway.
            error_type=type(exc).__name__,
            conversation_id=str(reply.conversation_id),
        )
        return
    if reply.conversation_id is not None and not sender_persists_outbound(client):
        await _record_outbound(
            reply.conversation_id,
            body,
            result,
            interactive=interactive_buttons_record(body, buttons),
        )
    logger.info(event, conversation_id=str(reply.conversation_id))
