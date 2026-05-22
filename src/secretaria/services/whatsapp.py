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
        # TODO(rate-limit): WhatsApp Coexistence caps outbound traffic at
        #   ~5 messages/second per number. Add a token-bucket / Redis-backed
        #   limiter here before going to production.
        url = f"{self._base_url}/{self._settings.META_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {self._settings.META_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
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
                )
                raise
            except httpx.HTTPError as exc:
                logger.error("whatsapp_send_connection_error", error=str(exc), to=to)
                raise

        data = response.json()
        logger.info("whatsapp_message_sent", to=to, message_id=_extract_message_id(data))
        return data
