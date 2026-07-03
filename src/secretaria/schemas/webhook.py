"""Pydantic schemas for the Meta WhatsApp webhook payload.

The models are intentionally permissive (`extra="allow"`, every field
optional) so an unexpected payload shape never crashes the webhook handler
or the worker. Reference:
https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
"""

from collections.abc import Iterator

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


class WebhookChange(BaseModel):
    model_config = _PERMISSIVE

    # e.g. "messages" or "smb_message_echoes"
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
    return title or payload_id


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
