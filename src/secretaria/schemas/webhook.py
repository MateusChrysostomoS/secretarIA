"""Pydantic schemas for the Meta WhatsApp webhook payload.

The models are intentionally permissive (`extra="allow"`, every field
optional) so an unexpected payload shape never crashes the webhook handler
or the worker. Reference:
https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
"""

from collections.abc import Iterator
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_PERMISSIVE = ConfigDict(extra="allow", populate_by_name=True)


class WebhookText(BaseModel):
    model_config = _PERMISSIVE

    body: str | None = None


class WebhookContactProfile(BaseModel):
    model_config = _PERMISSIVE

    name: str | None = None


class WebhookContact(BaseModel):
    model_config = _PERMISSIVE

    wa_id: str | None = None
    profile: WebhookContactProfile | None = None


class WebhookMetadata(BaseModel):
    model_config = _PERMISSIVE

    display_phone_number: str | None = None
    phone_number_id: str | None = None


class WebhookInteractiveReply(BaseModel):
    """Common shape for both `button_reply` and `list_reply` sub-objects."""

    model_config = _PERMISSIVE

    id: str | None = None
    title: str | None = None
    description: str | None = None


class WebhookInteractive(BaseModel):
    """Container the patient sends back after tapping an interactive control.

    `type` is "button_reply" for reply buttons or "list_reply" for list rows.
    Exactly one of the two sub-fields is populated.
    """

    model_config = _PERMISSIVE

    type: str | None = None
    button_reply: WebhookInteractiveReply | None = None
    list_reply: WebhookInteractiveReply | None = None


class WebhookAudio(BaseModel):
    """The `audio` sub-object on an inbound voice-note message."""

    model_config = _PERMISSIVE

    id: str | None = None
    mime_type: str | None = None
    voice: bool | None = None


class WebhookButtonReply(BaseModel):
    """The `button` sub-object on a message of type "button".

    Arrives when the patient taps a quick-reply button on a TEMPLATE (HSM)
    message — the free-form `interactive.button_reply` above is what a reply
    button on a plain/interactive send produces instead. Both carriers smuggle
    the same "<action>|<id>" contract for action buttons (see
    `extract_action_button`); `text` is the button's visible label, mirroring
    `WebhookInteractiveReply.title`.
    """

    model_config = _PERMISSIVE

    payload: str | None = None
    text: str | None = None


class WebhookMessage(BaseModel):
    model_config = _PERMISSIVE

    id: str | None = None
    # `from` is a Python keyword - exposed as `from_`.
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    timestamp: str | None = None
    type: str | None = None
    text: WebhookText | None = None
    interactive: WebhookInteractive | None = None
    audio: WebhookAudio | None = None
    # Present on message type "button": a tap on a template's quick-reply
    # button (see WebhookButtonReply's docstring for how this differs from
    # `interactive.button_reply`).
    button: WebhookButtonReply | None = None


class WebhookHistoryMetadata(BaseModel):
    """`history[].metadata` — chunk progress markers (Coexistence history sync).

    Per Meta's Coexistence documentation, `phase` is `COMPLETE_CHUNK` while
    more chunks remain and `COMPLETE` for the last one; `progress` is a 0-100
    percentage. Both are read defensively (see `history_item_is_final`) so an
    undocumented/renamed variant never crashes the webhook worker.
    """

    model_config = _PERMISSIVE

    phase: str | None = None
    chunk_order: int | None = None
    progress: int | str | None = None


class WebhookHistoryItem(BaseModel):
    """One chunk of a WhatsApp Coexistence chat-history sync (`history` field).

    `threads`/`errors` are deliberately left as raw dicts (never modeled or
    read field-by-field): secretaria NEVER ingests message content from a
    history sync (LGPD - see workers/tasks.py's `_handle_history`), so there
    is nothing to type beyond "how many". `errors` is non-empty when the
    business DECLINED history sharing from the WhatsApp Business App.
    """

    model_config = _PERMISSIVE

    metadata: WebhookHistoryMetadata | None = None
    threads: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)


class WebhookStateSyncItem(BaseModel):
    """One `smb_app_state_sync` entry — a business-contact add/update/removal.

    The real payload's `contact` sub-object carries PII (full_name,
    phone_number) and is deliberately NOT modeled here — this schema is
    `extra="allow"` so it still parses without error, but our code only ever
    reads `type`/`action` (never `contact`), so contact PII is never touched,
    logged, or persisted (see workers/tasks.py's `_handle_smb_app_state_sync`).
    """

    model_config = _PERMISSIVE

    type: str | None = None
    action: str | None = None


class WebhookValue(BaseModel):
    model_config = _PERMISSIVE

    messaging_product: str | None = None
    metadata: WebhookMetadata | None = None
    contacts: list[WebhookContact] = Field(default_factory=list)
    messages: list[WebhookMessage] = Field(default_factory=list)
    statuses: list[dict] = Field(default_factory=list)
    # Coexistence: echoes of messages the human secretary sent from the
    # WhatsApp mobile app (webhook field `smb_message_echoes`).
    message_echoes: list[WebhookMessage] = Field(default_factory=list)
    # Coexistence: chat-history sync chunks (webhook field `history`).
    history: list[WebhookHistoryItem] = Field(default_factory=list)
    # Coexistence: business contact list sync (webhook field `smb_app_state_sync`).
    state_sync: list[WebhookStateSyncItem] = Field(default_factory=list)


def history_item_is_final(item: WebhookHistoryItem) -> bool:
    """True when this `history` chunk signals the sync is FULLY complete.

    Three independent signals, any of which finalizes the sync (accept
    unknown variants gracefully rather than requiring one exact shape):
      1. `metadata.phase == "COMPLETE"` (case-insensitive) — the documented
         last-chunk marker. `COMPLETE_CHUNK` (more chunks still coming) does
         NOT match this exact comparison, so it correctly stays in-progress.
      2. `metadata.progress >= 100` — covers any variant that reports percent
         completion without a literal "COMPLETE" phase string.
      3. A non-empty `errors` list — the business DECLINED history sharing;
         no further chunks will ever arrive, so the sync is over (with no
         data), not "in progress" forever.
    """
    if item.errors:
        return True
    meta = item.metadata
    if meta is None:
        return False
    if str(meta.phase or "").strip().upper() == "COMPLETE":
        return True
    try:
        if meta.progress is not None and int(meta.progress) >= 100:
            return True
    except (TypeError, ValueError):
        pass
    return False


class WebhookChange(BaseModel):
    model_config = _PERMISSIVE

    # e.g. "messages", "smb_message_echoes", "history", "smb_app_state_sync"
    field: str | None = None
    value: WebhookValue | None = None


class WebhookEntry(BaseModel):
    model_config = _PERMISSIVE

    id: str | None = None
    changes: list[WebhookChange] = Field(default_factory=list)


class WebhookPayload(BaseModel):
    model_config = _PERMISSIVE

    object: str | None = None
    entry: list[WebhookEntry] = Field(default_factory=list)


def iter_event_ids(payload: dict) -> Iterator[str]:
    """Yield every message / echo id in a raw webhook payload (light parsing).

    Used for the idempotency fast-path in the webhook handler, so it must
    never raise on a malformed payload.
    """
    if not isinstance(payload, dict):
        return
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            for key in ("messages", "message_echoes"):
                for item in value.get(key) or []:
                    if isinstance(item, dict) and item.get("id"):
                        yield item["id"]


def iter_audio_messages(payload: dict) -> Iterator[dict]:
    """Yield one minimal dict per inbound WhatsApp voice-note audio message.

    Mirrors `iter_event_ids`'s style and defensiveness exactly: pure dict
    walking, isinstance checks throughout, never raises on a malformed
    payload. Used by the webhook POST handler to enqueue the dedicated
    `transcribe_audio_message` arq job with a minimal payload (never the
    full webhook body).

    Unlike `iter_event_ids`, this ONLY scans changes where
    `change.get("field") == "messages"` - Coexistence echoes under
    `smb_message_echoes` are never transcribed (that's the business's own
    outbound audio, sent by the human secretary from the WhatsApp app, not a
    patient voice note).

    For each message dict with `type == "audio"` and a truthy `audio.id`
    (and both `id` and `from` present), yields:
        {"media_id", "phone_number_id", "wa_id", "message_id", "patient_name"}
    `patient_name` is looked up from the change's `contacts` list (matching
    `wa_id`), defensively - missing/malformed contacts just yield None.
    """
    if not isinstance(payload, dict):
        return
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue

            metadata = value.get("metadata")
            phone_number_id = (
                metadata.get("phone_number_id") if isinstance(metadata, dict) else None
            )

            names_by_wa_id: dict[str, str | None] = {}
            for contact in value.get("contacts") or []:
                if not isinstance(contact, dict):
                    continue
                wa_id = contact.get("wa_id")
                if not wa_id:
                    continue
                profile = contact.get("profile")
                names_by_wa_id[wa_id] = profile.get("name") if isinstance(profile, dict) else None

            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") != "audio":
                    continue
                message_id = msg.get("id")
                wa_id = msg.get("from")
                if not message_id or not wa_id:
                    continue
                audio = msg.get("audio")
                media_id = audio.get("id") if isinstance(audio, dict) else None
                if not media_id:
                    continue

                yield {
                    "media_id": media_id,
                    "phone_number_id": phone_number_id or None,
                    "wa_id": wa_id,
                    "message_id": message_id,
                    "patient_name": names_by_wa_id.get(wa_id),
                }


def extract_inbound_body(msg: WebhookMessage) -> str | None:
    """Return the human-readable text body of an inbound message.

    Handles text messages (`text.body`) plus the two Cloud API interactive
    callbacks: reply-button taps (`interactive.button_reply`) and list-row
    taps (`interactive.list_reply`).

    Returns None when the message type is not one the bot can act on
    (image, audio, location, etc.) so the worker can decide to stay quiet
    rather than feed a meaningless body to the LLM.
    """
    if msg.text and msg.text.body:
        return msg.text.body

    interactive = msg.interactive
    if interactive is None:
        return None

    reply = interactive.button_reply or interactive.list_reply
    if reply is None:
        return None
    title = (reply.title or "").strip()
    payload_id = (reply.id or "").strip()
    if not title and not payload_id:
        return None

    # Slot taps carry both a human label and the ISO datetime in the id, so
    # the agent sees a self-describing string: "15:00 (2026-05-29T15:00)".
    if payload_id.startswith("slot|"):
        iso = payload_id.split("|", 1)[1]
        return f"{title} ({iso})" if title else iso
    # Professional-row taps mirror the slot contract: the id smuggles the
    # professional's UUID ("Dra. Ana (uuid)") so the flow router resolves the
    # exact row even when the 24-char row title truncated the name or two
    # professionals share one.
    if payload_id.startswith("prof|"):
        professional_id = payload_id.split("|", 1)[1]
        return f"{title} ({professional_id})" if title else professional_id
    return title or payload_id


# Action-button id/payload prefixes (the "<action>|<appointment_id>"
# data-carrying family — precedent: "slot|"/"prof|" above). Order matters:
# "apptcancelyes|" must be checked before "apptcancel|" would ever be
# (they don't collide since we match a full prefix incl. the trailing "|",
# but keeping the more specific one explicit avoids relying on that subtlety).
_ACTION_BUTTON_PREFIXES: tuple[str, ...] = (
    "apptconfirm|",
    "apptresched|",
    "apptcancelyes|",
    "apptcancel|",
)


def extract_action_button(msg: WebhookMessage) -> tuple[str, str] | None:
    """Decode a reminder action-button reply into (action, appointment_id).

    Two carriers arrive for the same kind of tap, depending on which the
    reminder used (see plugins/reminders.py): a free-form interactive
    reply-button (`interactive.button_reply.id`, sent inside the 24h
    service window) or a template quick-reply's button payload
    (`button.payload`, sent outside it — see WebhookButtonReply). Both
    smuggle the same "<action>|<appointment_id>" id/payload contract as
    `slot|`/`prof|` above.

    Returns None (fall through to normal flow/LLM routing) when the id/payload
    doesn't start with one of the known action prefixes, OR when it does but
    the trailing part isn't a valid UUID — a malformed or tampered-with id
    must never crash routing, and `action` alone (without a real appointment
    id) is useless to the caller anyway.
    """
    raw: str | None = None
    if msg.interactive is not None and msg.interactive.button_reply is not None:
        raw = msg.interactive.button_reply.id
    elif msg.button is not None:
        raw = msg.button.payload
    if not raw:
        return None

    for prefix in _ACTION_BUTTON_PREFIXES:
        if raw.startswith(prefix):
            action = prefix[:-1]  # drop the trailing "|"
            appointment_id = raw[len(prefix) :]
            try:
                UUID(appointment_id)
            except ValueError:
                return None
            return action, appointment_id
    return None


def extract_echo_body(msg: WebhookMessage) -> str | None:
    """Return the history body for a `smb_message_echoes` (Coexistence) event.

    Echoes are stored as Message rows the LLM later reads back as
    conversation history, so `edit` and `revoke` — which carry no readable
    body of their own — get a readable placeholder instead of the None that
    `extract_inbound_body` would produce (a body=None row would look like a
    missing turn). Everything else (plain text, and any type we don't
    special-case, e.g. media) delegates to `extract_inbound_body`, including
    its None-for-unactionable contract.
    """
    if msg.type == "edit":
        if msg.text and msg.text.body:
            return f"[mensagem editada: {msg.text.body}]"
        return "[mensagem editada]"
    if msg.type == "revoke":
        return "[mensagem apagada]"
    return extract_inbound_body(msg)
