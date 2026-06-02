"""WhatsApp Cloud API client - sends outbound messages."""

import httpx

from secretaria.config import Settings, get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)


def _extract_message_id(response_data: dict) -> str | None:
    """Pull the wamid from a Cloud API send response, tolerating bad shapes."""
    try:
        return response_data["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


class WhatsAppClient:
    """Async client for the Meta WhatsApp Cloud API.

    For the MVP (single tenant) credentials come from Settings. A multi-tenant
    version would take the tenant's phone_number_id / access_token instead.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._base_url = f"https://graph.facebook.com/{self._settings.META_GRAPH_API_VERSION}"

    async def _post(self, payload: dict, to: str) -> dict:
        # TODO(rate-limit): WhatsApp Coexistence caps outbound traffic at
        #   ~5 messages/second per number. Add a token-bucket / Redis-backed
        #   limiter here before going to production.
        url = f"{self._base_url}/{self._settings.META_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {self._settings.META_ACCESS_TOKEN}",
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
                    "sections": [
                        {"title": section_title[:24], "rows": capped_rows}
                    ],
                },
            },
        }
        return await self._post(payload, to=to)
