"""WhatsApp Cloud API client - sends outbound messages."""

import httpx

from secretaria.config import Settings, get_settings
from secretaria.core.logging import get_logger
from secretaria.models.tenant import Tenant

logger = get_logger(__name__)


def _extract_message_id(response_data: dict) -> str | None:
    """Pull the wamid from a Cloud API send response, tolerating bad shapes."""
    try:
        return response_data["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


class WhatsAppClient:
    """Async client for the Meta WhatsApp Cloud API."""

    def __init__(
        self,
        settings: Settings | None = None,
        phone_number_id: str | None = None,
        access_token: str | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._base_url = f"https://graph.facebook.com/{self._settings.META_GRAPH_API_VERSION}"
        # Per-tenant override: when provided, these take precedence over settings.
        self._phone_number_id = phone_number_id or self._settings.META_PHONE_NUMBER_ID
        self._access_token = access_token or self._settings.META_ACCESS_TOKEN

    @classmethod
    def for_tenant(cls, tenant: Tenant, access_token: str | None) -> "WhatsAppClient":
        """Build a client for a tenant. The DECRYPTED token is injected by the caller.

        The token no longer lives on the Tenant row — it is Fernet ciphertext in
        `tenant_credentials`, decrypted ONLY by `services/tenant_config.get_waba_token`
        (the single decrypt seam). `None` falls back to the single-tenant
        META_ACCESS_TOKEN env scaffold via the constructor.
        """
        return cls(phone_number_id=tenant.phone_number_id, access_token=access_token)

    async def _post(self, payload: dict, to: str) -> dict:
        # TODO(rate-limit): WhatsApp Coexistence caps outbound traffic at
        #   ~5 messages/second per number. Add a token-bucket / Redis-backed
        #   limiter here before going to production.
        url = f"{self._base_url}/{self._phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "whatsapp_send_http_error",
                    status_code=exc.response.status_code,
                    response=exc.response.text,
                    to=to,
                    payload_type=payload.get("type"),
                )
                raise
            except httpx.HTTPError as exc:
                logger.error("whatsapp_send_connection_error", error=str(exc), to=to)
                raise
        data = response.json()
        logger.info(
            "whatsapp_message_sent",
            to=to,
            message_id=_extract_message_id(data),
            payload_type=payload.get("type"),
        )
        return data

    async def send_text_message(self, to: str, body: str) -> dict:
        """Send a plain-text WhatsApp message.

        Args:
            to: recipient wa_id (the patient's phone number digits).
            body: message text.

        Returns:
            The parsed Cloud API JSON response.

        Raises:
            httpx.HTTPError: on connection failures or non-2xx responses.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        return await self._post(payload, to=to)

    async def send_buttons(
        self,
        to: str,
        body: str,
        buttons: list[tuple[str, str]],
    ) -> dict:
        """Send an interactive reply-button message (max 3 buttons).

        Args:
            to: recipient wa_id.
            body: message body (max 1024 chars).
            buttons: list of (id, title) pairs. WhatsApp caps title at 20 chars
                and the list at 3 entries; extra entries are dropped silently
                so the LLM never blocks a send by over-listing.
        """
        capped = [
            {"type": "reply", "reply": {"id": bid[:256], "title": title[:20]}}
            for bid, title in buttons[:3]
        ]
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body[:1024]},
                "action": {"buttons": capped},
            },
        }
        return await self._post(payload, to=to)

    async def send_template(
        self,
        to: str,
        template: str,
        lang: str,
        variables: list[str],
    ) -> dict:
        """Send a pre-approved WhatsApp utility template (HSM) message.

        Required OUTSIDE the 24h customer-service window (Meta Cloud API
        rule: free-form text/interactive messages are only allowed within it)
        and billed per send. `template` must already be an approved template
        on the tenant's WABA.

        Args:
            to: recipient wa_id.
            template: the approved template's name.
            lang: Meta language code, e.g. "pt_BR" (NOT "pt-BR").
            variables: body parameter values, filling the template's
                positional `{{1}}`, `{{2}}`, ... placeholders in order.
        """
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": lang},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": v} for v in variables],
                    }
                ],
            },
        }
        return await self._post(payload, to=to)

    async def send_list(
        self,
        to: str,
        body: str,
        button_label: str,
        rows: list[tuple[str, str, str | None]],
        section_title: str = "Opções",
    ) -> dict:
        """Send an interactive list message (max 10 rows in one section).

        Args:
            to: recipient wa_id.
            body: message body (max 1024 chars).
            button_label: text on the button that opens the list (max 20 chars).
            rows: list of (id, title, description) tuples. Title max 24 chars,
                description max 72 chars, id max 200 chars. Extras dropped.
            section_title: label above the rows in the picker (max 24 chars).
        """
        capped_rows = []
        for rid, title, desc in rows[:10]:
            row = {"id": rid[:200], "title": title[:24]}
            if desc:
                row["description"] = desc[:72]
            capped_rows.append(row)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body[:1024]},
                "action": {
                    "button": button_label[:20],
                    "sections": [{"title": section_title[:24], "rows": capped_rows}],
                },
            },
        }
        return await self._post(payload, to=to)
