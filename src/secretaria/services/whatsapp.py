"""WhatsApp Cloud API client - sends outbound messages.

FAIL-CLOSED per tenant (PROMPT_FIX_21). Every tenant-scoped send resolves its
OWN `phone_number_id` + decrypted WABA token; a missing value raises
`TenantWhatsAppCredentialMissing` BEFORE any HTTP request is made. There is no
implicit fallback to the global `META_*` env scaffold: falling back would mean
sending one clinic's message from another clinic's WhatsApp number. The env
scaffold survives only behind `for_dev_scaffold()`, which a caller has to ask
for by name.
"""

from uuid import UUID

import httpx

from secretaria.config import Settings, get_settings
from secretaria.core.logging import get_logger, wa_suffix
from secretaria.core.whatsapp_limits import (
    MAX_BUTTONS_PER_MESSAGE,
    MAX_INTERACTIVE_BODY_CHARS,
    MAX_LIST_OPEN_BUTTON_CHARS,
    MAX_LIST_ROW_DESCRIPTION_CHARS,
    MAX_LIST_ROW_ID_CHARS,
    MAX_LIST_ROW_TITLE_CHARS,
    MAX_LIST_ROWS,
    MAX_LIST_SECTION_TITLE_CHARS,
    truncate_button_label,
    truncate_list_row_title,
    truncate_plain,
)
from secretaria.models.tenant import Tenant

logger = get_logger(__name__)


class TenantWhatsAppCredentialMissing(RuntimeError):
    """A tenant-scoped send was attempted without that tenant's credentials.

    Carries the FIELD NAMES that were missing ("phone_number_id" /
    "access_token"), never a value — this exception's string ends up in logs
    and operational alerts.
    """

    def __init__(self, tenant_id: UUID | str | None, missing: tuple[str, ...]) -> None:
        self.tenant_id = str(tenant_id) if tenant_id is not None else None
        self.missing = tuple(missing)
        super().__init__(f"missing tenant WhatsApp credential: {', '.join(self.missing)}")


def _extract_message_id(response_data: dict) -> str | None:
    """Pull the wamid from a Cloud API send response, tolerating bad shapes."""
    try:
        return response_data["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


def _meta_error_code(response: httpx.Response) -> str | None:
    """Meta's NUMERIC error code/subcode from an error body, or None.

    Deliberately allowlisted to the two integer fields: `error.message` and
    `error.error_user_msg` routinely echo the recipient's phone number and the
    message text back at us, so the raw body must never reach a log.
    """
    try:
        error = response.json().get("error") or {}
    except Exception:
        return None
    code = error.get("code")
    subcode = error.get("error_subcode")
    if not isinstance(code, int):
        return None
    return f"{code}/{subcode}" if isinstance(subcode, int) else str(code)


def _status_class(status_code: int) -> str:
    """A "4xx"/"5xx"-style bucket — enough to alert on, carries no content."""
    return f"{status_code // 100}xx"


class WhatsAppClient:
    """Async client for the Meta WhatsApp Cloud API.

    Both credentials are REQUIRED and explicit. Build one with `for_tenant`
    (production) or `for_dev_scaffold` (single-tenant dev only).
    """

    def __init__(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        settings: Settings | None = None,
        tenant_id: UUID | str | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._base_url = f"https://graph.facebook.com/{self._settings.META_GRAPH_API_VERSION}"
        self._phone_number_id = phone_number_id
        self._access_token = access_token
        # Internal id only, for log correlation. Never a phone number.
        self._tenant_id = str(tenant_id) if tenant_id is not None else None

    @classmethod
    def for_tenant(cls, tenant: Tenant, access_token: str | None) -> "WhatsAppClient":
        """Build a client for a tenant. The DECRYPTED token is injected by the caller.

        The token no longer lives on the Tenant row — it is Fernet ciphertext in
        `tenant_credentials`, decrypted ONLY by `services/tenant_config.get_waba_token`
        (the single decrypt seam).

        Raises:
            TenantWhatsAppCredentialMissing: when this tenant has no
                `phone_number_id` or no token. It does NOT fall back to the
                global env scaffold — that would send this clinic's message
                from whatever WABA the process happens to be configured with.
                Fail closed, before any HTTP call.
        """
        missing = []
        phone_number_id = (tenant.phone_number_id or "").strip()
        token = (access_token or "").strip()
        if not phone_number_id:
            missing.append("phone_number_id")
        if not token:
            missing.append("access_token")
        if missing:
            # Emitted HERE so the event fires on every path, including callers
            # that let the exception propagate rather than degrading.
            logger.error(
                "whatsapp_credential_missing",
                tenant_id=str(tenant.id) if tenant.id is not None else None,
                missing=",".join(missing),
            )
            raise TenantWhatsAppCredentialMissing(tenant.id, tuple(missing))
        return cls(phone_number_id=phone_number_id, access_token=token, tenant_id=tenant.id)

    @classmethod
    def for_dev_scaffold(cls, settings: Settings | None = None) -> "WhatsAppClient":
        """The single-tenant `META_*` env scaffold, requested BY NAME.

        Development only: it sends from whatever number the process env points
        at, which is meaningless (and dangerous) once more than one tenant
        exists. Nothing on the webhook/worker reply path may call this — those
        paths all resolve a tenant first and use `for_tenant`.
        """
        resolved = settings or get_settings()
        missing = []
        if not (resolved.META_PHONE_NUMBER_ID or "").strip():
            missing.append("phone_number_id")
        if not (resolved.META_ACCESS_TOKEN or "").strip():
            missing.append("access_token")
        if missing:
            logger.error("whatsapp_credential_missing", tenant_id=None, missing=",".join(missing))
            raise TenantWhatsAppCredentialMissing(None, tuple(missing))
        return cls(
            phone_number_id=resolved.META_PHONE_NUMBER_ID,
            access_token=resolved.META_ACCESS_TOKEN,
            settings=resolved,
        )

    async def _post(self, payload: dict, to: str) -> dict:
        # TODO(rate-limit): WhatsApp Coexistence caps outbound traffic at
        #   ~5 messages/second per number. Add a token-bucket / Redis-backed
        #   limiter here before going to production.
        url = f"{self._base_url}/{self._phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        payload_type = payload.get("type")
        # LGPD: the recipient is reduced to its last four digits, and the
        # message body never appears at all. Same rule on every branch below.
        to_suffix = wa_suffix(to)
        logger.info(
            "whatsapp_send_attempt",
            tenant_id=self._tenant_id,
            to_suffix=to_suffix,
            payload_type=payload_type,
        )
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "whatsapp_send_result",
                    outcome="http_error",
                    tenant_id=self._tenant_id,
                    to_suffix=to_suffix,
                    payload_type=payload_type,
                    status_code=exc.response.status_code,
                    status_class=_status_class(exc.response.status_code),
                    # Numeric Meta code only — NEVER `exc.response.text`.
                    meta_error_code=_meta_error_code(exc.response),
                )
                raise
            except httpx.HTTPError as exc:
                logger.error(
                    "whatsapp_send_result",
                    outcome="connection_error",
                    tenant_id=self._tenant_id,
                    to_suffix=to_suffix,
                    payload_type=payload_type,
                    error_type=type(exc).__name__,
                )
                raise
        data = response.json()
        logger.info(
            "whatsapp_send_result",
            outcome="sent",
            tenant_id=self._tenant_id,
            to_suffix=to_suffix,
            payload_type=payload_type,
            status_code=response.status_code,
            status_class=_status_class(response.status_code),
            message_id=_extract_message_id(data),
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
            buttons: list of (id, title) pairs. WhatsApp caps title at
                MAX_BUTTON_LABEL_CHARS and the list at MAX_BUTTONS_PER_MESSAGE
                entries; extra entries are dropped silently so the LLM never
                blocks a send by over-listing.
        """
        capped = [
            {
                "type": "reply",
                "reply": {
                    "id": bid[:256],
                    "title": truncate_button_label(title),
                },
            }
            for bid, title in buttons[:MAX_BUTTONS_PER_MESSAGE]
        ]
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": truncate_plain(body, MAX_INTERACTIVE_BODY_CHARS)},
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
        button_payloads: list[str] | None = None,
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
            button_payloads: when the template has quick-reply buttons (e.g.
                REMINDER_DEPOSIT_TEMPLATE_NAME's Confirmar/Reagendar/Cancelar
                trio), one payload string per button, in the template's own
                button order. None (default) omits the button components
                entirely — backward compatible with every plain-text template
                sent before this parameter existed.
        """
        components = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": v} for v in variables],
            }
        ]
        if button_payloads:
            components.extend(
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": index,
                    "parameters": [{"type": "payload", "payload": payload}],
                }
                for index, payload in enumerate(button_payloads)
            )
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": lang},
                "components": components,
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
            button_label: text on the button that opens the list
                (MAX_LIST_OPEN_BUTTON_CHARS).
            rows: list of (id, title, description) tuples, capped at
                MAX_LIST_ROWS. Titles go through `truncate_list_row_title` -
                the SAME cut the callers render with and the matchers compare
                against (services/booking_scope.py), so re-applying it here is
                a no-op for a title that already fits and a last line of
                defence for one built anywhere else.
            section_title: label above the rows in the picker
                (MAX_LIST_SECTION_TITLE_CHARS).
        """
        capped_rows = []
        for rid, title, desc in rows[:MAX_LIST_ROWS]:
            row = {
                "id": rid[:MAX_LIST_ROW_ID_CHARS],
                "title": truncate_list_row_title(title, MAX_LIST_ROW_TITLE_CHARS),
            }
            if desc:
                row["description"] = truncate_plain(desc, MAX_LIST_ROW_DESCRIPTION_CHARS)
            capped_rows.append(row)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": truncate_plain(body, MAX_INTERACTIVE_BODY_CHARS)},
                "action": {
                    "button": truncate_plain(button_label, MAX_LIST_OPEN_BUTTON_CHARS),
                    "sections": [
                        {
                            "title": truncate_plain(section_title, MAX_LIST_SECTION_TITLE_CHARS),
                            "rows": capped_rows,
                        }
                    ],
                },
            },
        }
        return await self._post(payload, to=to)
