"""HTTP client for the Precheck service (a separate FastAPI app).

Precheck runs in the same Easypanel project and is reached over the internal
Docker network. Authentication is service-to-service via a shared API key in
the `X-Internal-Api-Key` header.

Only used when the tenant's `precheck_enabled` feature flag is on.
"""

from typing import Any

import httpx

from secretaria.config import Settings, get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)


class PrecheckClient:
    """Async client for the Precheck (anamnese) service."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.PRECHECK_BASE_URL,
            headers={"X-Internal-Api-Key": self._settings.PRECHECK_API_KEY},
            timeout=20.0,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        """Issue an authenticated request, logging structured errors."""
        async with self._client() as client:
            try:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "precheck_http_error",
                    method=method,
                    path=path,
                    status_code=exc.response.status_code,
                    response=exc.response.text,
                )
                raise
            except httpx.HTTPError as exc:
                logger.error(
                    "precheck_connection_error",
                    method=method,
                    path=path,
                    error=str(exc),
                )
                raise
            return response.json()

    async def iniciar_anamnese(self, patient_id: str, dados: dict) -> dict:
        """Start an anamnese (pre-consultation questionnaire) for a patient."""
        # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API
        return await self._request(
            "POST",
            "/api/v1/anamnese",
            json={"patient_id": patient_id, "dados": dados},
        )

    async def consultar_resultado(self, anamnese_id: str) -> dict:
        """Fetch the result of a previously started anamnese."""
        # TODO: PRECHECK_CONTRACT_NEEDED - confirmar path e payload com o dono da API
        return await self._request("GET", f"/api/v1/anamnese/{anamnese_id}")
