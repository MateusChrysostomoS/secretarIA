"""Thin async client for the Asaas v3 API (Pix deposit charges).

There is NO refund method (decision D15): the platform never returns money, on any
provider. `services/payments/asaas_provider.py` is the wrapper the rest of the system uses;
this module is only the wire.

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

from dataclasses import dataclass

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)


class AsaasError(Exception):
    """A non-2xx response from Asaas. Carries the status code + an optional
    short symbolic code hint — NEVER response body text (ids/status only)."""

    def __init__(
        self,
        status_code: int,
        code_hint: str | None = None,
        *,
        document_missing: bool = False,
        text_reason: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code_hint = code_hint
        # A short fixed keyword ("document", ...) from `_classify_text`, or None. It names WHAT
        # the free text complained about; the text itself is never kept.
        self.text_reason = text_reason
        # True only when Asaas said the CUSTOMER lacks a CPF/CNPJ (sandbox fact: POST /customers
        # accepts one without it, then POST /payments answers 400 invalid_object).
        # A boolean, never text.
        self.document_missing = document_missing
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


def _mentions_missing_document(response: httpx.Response) -> bool:
    """True when an error's free text asks for the customer's CPF/CNPJ. The text is only TESTED
    here, never stored, logged or raised: the privacy rule above still holds."""
    try:
        data = response.json()
    except ValueError:
        return False
    errors = data.get("errors") if isinstance(data, dict) else None
    if not isinstance(errors, list):
        return False
    for item in errors:
        text = item.get("description") if isinstance(item, dict) else None
        if isinstance(text, str) and "cpf" in text.lower() and "cnpj" in text.lower():
            return True
    return False


def _classify_text(response: httpx.Response) -> str | None:
    """Fixed keyword for what an error's free text complains about, or None. The text is only
    TESTED here, never stored, logged or raised. Only "document" is known today; further
    keywords (value, pix_key, account) are added after the real responses are captured."""
    return "document" if _mentions_missing_document(response) else None


@dataclass(frozen=True)
class ProbeResponse:
    """The raw facts of one read-only permission probe: status, short symbolic error code,
    whether the body was the expected JSON shape, and a parsed `Retry-After`. Never a body."""

    status_code: int
    code_hint: str | None
    json_ok: bool
    retry_after: float | None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


class AsaasClient:
    """Async wrapper around the Asaas v3 REST API, scoped to ONE tenant's own API key."""

    def __init__(
        self, api_key: str, *, base_url: str | None = None, timeout: float | None = None
    ) -> None:
        settings = get_settings()
        self._api_key = api_key
        self._base_url = (base_url or settings.ASAAS_BASE_URL).rstrip("/")
        self._timeout = settings.ASAAS_TIMEOUT_SECONDS if timeout is None else timeout

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> dict:
        url = f"{self._base_url}{path}"
        headers = {"access_token": self._api_key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.request(
                    method, url, headers=headers, json=json, params=params
                )
            except httpx.HTTPError as exc:
                logger.error("asaas_connection_error", path=path, error_type=type(exc).__name__)
                raise AsaasError(0, "connection_error") from exc

        if response.status_code >= 300:
            code_hint = _extract_code_hint(response)
            text_reason = _classify_text(response) if response.status_code == 400 else None
            logger.error("asaas_http_error", path=path, status_code=response.status_code)
            raise AsaasError(
                response.status_code,
                code_hint,
                document_missing=text_reason == "document",
                text_reason=text_reason,
            )

        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def create_customer(
        self, name: str, mobile_phone: str | None, cpf_cnpj: str | None = None
    ) -> str:
        """POST /customers -> the new (or Asaas-resolved) customer id.

        `cpf_cnpj` (digits only) is sent when given: Asaas' reference lists it as required
        for a customer. It is transient - never stored, never logged.
        """
        body: dict = {"name": name}
        if mobile_phone:
            body["mobilePhone"] = mobile_phone
        if cpf_cnpj:
            body["cpfCnpj"] = cpf_cnpj
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

    async def get_account_status(self) -> dict:
        """GET /myAccount/status -> the four onboarding states (`general`, `documentation`,
        `commercialInfo`, `bankAccountInfo`). A key without that permission answers 403."""
        return await self._request("GET", "/myAccount/status")

    async def get_payment(self, payment_id: str) -> dict:
        """GET /payments/{id} -> {id, status, value, externalReference, deleted, ...}."""
        return await self._request("GET", f"/payments/{payment_id}")

    async def delete_payment(self, payment_id: str) -> dict:
        """DELETE /payments/{id} — voids an unpaid (AWAITING) charge."""
        return await self._request("DELETE", f"/payments/{payment_id}")

    async def delete_customer(self, customer_id: str) -> dict:
        """DELETE /customers/{id} — LGPD erasure of the clinic-side customer record."""
        return await self._request("DELETE", f"/customers/{customer_id}")

    # --- webhook management (the connection service registers the clinic's webhook) ---

    async def create_webhook(self, body: dict) -> dict:
        """POST /webhooks. The response carries the webhook `id`; the `authToken` we sent is
        never read back from Asaas (it is only ever returned at creation)."""
        return await self._request("POST", "/webhooks", json=body)

    async def list_webhooks(self, limit: int = 100) -> dict:
        """GET /webhooks -> {"data": [{id, url, enabled, interrupted, ...}], ...}."""
        return await self._request("GET", "/webhooks", params={"limit": limit})

    async def update_webhook(self, webhook_id: str, body: dict) -> dict:
        """PUT /webhooks/{id} (re-enable a paused queue with {"interrupted": False})."""
        return await self._request("PUT", f"/webhooks/{webhook_id}", json=body)

    async def delete_webhook(self, webhook_id: str) -> dict:
        return await self._request("DELETE", f"/webhooks/{webhook_id}")

    async def probe(
        self,
        path: str,
        *,
        method: str = "GET",
        expect_list: bool = False,
        timeout: float = 5.0,
    ) -> ProbeResponse:
        """One read-only permission probe (`limit=1`, empty body, 5 s timeout).

        Unlike `_request`, an HTTP status is DATA here (403 `insufficient_permission` means
        "this key lacks that permission"), so it never raises. Only the status, a short
        symbolic error code and a JSON-shape flag survive: the response body is discarded.
        """
        url = f"{self._base_url}{path}"
        headers = {"access_token": self._api_key}
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.request(
                    method, url, headers=headers, params={"limit": 1} if method == "GET" else None
                )
            except httpx.HTTPError as exc:
                logger.warning("asaas_probe_connection_error", error_type=type(exc).__name__)
                return ProbeResponse(0, "connection_error", False, None)

        json_ok = False
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                data = None
            json_ok = isinstance(data, dict) and (
                not expect_list or isinstance(data.get("data"), list)
            )
        return ProbeResponse(
            status_code=response.status_code,
            code_hint=_extract_code_hint(response) if response.status_code >= 300 else None,
            json_ok=json_ok,
            retry_after=_retry_after(response),
        )
