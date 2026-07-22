"""Thin async client for the Asaas v3 API (Pix deposit charges).

Each tenant supplies its OWN Asaas API key
(`tenant_credentials.asaas_api_key_encrypted`, decrypted only by
`services/tenant_config.get_asaas_api_key`): the CLINIC's own Asaas account
receives the Pix deposit and pays Asaas' own small per-transaction PSP fee
(~R$1.99 at the time of writing) — never the platform's account, never a
platform cost. `settings.ASAAS_BASE_URL` points at the real api.asaas.com
host by default; pass `base_url=` (or override the setting) to target Asaas'
sandbox during development.

Every method tolerates unknown/extra fields on a 2xx response (no schema
validation — Asaas' payload shape is free to grow) and raises `AsaasError` on
any non-2xx WITHOUT ever including response body text in the exception
message or a log line — only the HTTP status and, best-effort, a short
symbolic error `code` (never a free-text `description`/`message`, which may
echo request data back).
"""

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)


class AsaasError(Exception):
    """A non-2xx response from Asaas. Carries the status code + an optional
    short symbolic code hint — NEVER response body text (ids/status only)."""

    def __init__(self, status_code: int, code_hint: str | None = None) -> None:
        self.status_code = status_code
        self.code_hint = code_hint
        suffix = f" ({code_hint})" if code_hint else ""
        super().__init__(f"Asaas request failed: HTTP {status_code}{suffix}")


def _extract_code_hint(response: httpx.Response) -> str | None:
    """Best-effort short error-code hint. Reads ONLY `errors[0].code` (a short
    symbolic string like "invalid_customer") — deliberately never `description`
    or `message`, which may contain request-echoed / free-text content."""
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    errors = data.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0]
    if not isinstance(first, dict):
        return None
    code = first.get("code")
    return code[:64] if isinstance(code, str) else None


class AsaasClient:
    """Async wrapper around the Asaas v3 REST API, scoped to ONE tenant's own API key."""

    def __init__(
        self, api_key: str, *, base_url: str | None = None, timeout: float | None = None
    ) -> None:
        settings = get_settings()
        self._api_key = api_key
        self._base_url = (base_url or settings.ASAAS_BASE_URL).rstrip("/")
        self._timeout = settings.ASAAS_TIMEOUT_SECONDS if timeout is None else timeout

    async def _request(self, method: str, path: str, *, json: dict | None = None) -> dict:
        url = f"{self._base_url}{path}"
        headers = {"access_token": self._api_key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.request(method, url, headers=headers, json=json)
            except httpx.HTTPError as exc:
                logger.error("asaas_connection_error", path=path, error_type=type(exc).__name__)
                raise AsaasError(0, "connection_error") from exc

        if response.status_code >= 300:
            code_hint = _extract_code_hint(response)
            logger.error("asaas_http_error", path=path, status_code=response.status_code)
            raise AsaasError(response.status_code, code_hint)

        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def create_customer(self, name: str, mobile_phone: str | None) -> str:
        """POST /customers -> the new (or Asaas-resolved) customer id."""
        body: dict = {"name": name}
        if mobile_phone:
            body["mobilePhone"] = mobile_phone
        data = await self._request("POST", "/customers", json=body)
        return str(data.get("id") or "")

    async def create_pix_payment(
        self,
        customer_id: str,
        value_cents: int,
        external_reference: str,
        description: str,
        due_date: str,
    ) -> dict:
        """POST /payments (billingType=PIX). `due_date` ("YYYY-MM-DD") is the
        caller's responsibility (services/payments/deposit_lifecycle.py computes
        "today" in the TENANT's own timezone — this client has no timezone
        opinion). Returns the raw dict, at least {id, status} on success."""
        body = {
            "customer": customer_id,
            "billingType": "PIX",
            "value": round(value_cents / 100, 2),
            "dueDate": due_date,
            "externalReference": external_reference,
            "description": description,
        }
        return await self._request("POST", "/payments", json=body)

    async def get_pix_qr(self, payment_id: str) -> dict:
        """GET /payments/{id}/pixQrCode -> {payload, encodedImage, expirationDate, ...}."""
        return await self._request("GET", f"/payments/{payment_id}/pixQrCode")

    async def refund_payment(self, payment_id: str, value_cents: int | None) -> dict:
        """POST /payments/{id}/refund. `value_cents=None` omits "value" entirely
        (Asaas refunds the payment in full); otherwise a partial refund amount."""
        body: dict = {}
        if value_cents is not None:
            body["value"] = round(value_cents / 100, 2)
        return await self._request("POST", f"/payments/{payment_id}/refund", json=body)

    async def delete_payment(self, payment_id: str) -> dict:
        """DELETE /payments/{id} — voids an unpaid (AWAITING) charge."""
        return await self._request("DELETE", f"/payments/{payment_id}")
