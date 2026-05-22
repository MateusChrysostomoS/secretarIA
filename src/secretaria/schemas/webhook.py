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


class WebhookMessage(BaseModel):
    model_config = _PERMISSIVE

    id: str | None = None
    # `from` is a Python keyword - exposed as `from_`.
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    timestamp: str | None = None
    type: str | None = None
    text: WebhookText | None = None


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
