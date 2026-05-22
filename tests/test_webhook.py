"""Tests for the WhatsApp webhook endpoints.

These exercise only paths that need no Redis/Postgres: the GET handshake and
signature rejection on POST (both decided before any infrastructure access).
"""

import json

from httpx import AsyncClient

# Must match META_VERIFY_TOKEN configured in tests/conftest.py.
VERIFY_TOKEN = "test-verify-token"


async def test_get_handshake_returns_challenge(client: AsyncClient) -> None:
    response = await client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 200
    assert response.text == "12345"


async def test_get_handshake_wrong_token_returns_403(client: AsyncClient) -> None:
    response = await client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "the-wrong-token",
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 403


async def test_post_invalid_signature_returns_403(client: AsyncClient) -> None:
    body = json.dumps({"object": "whatsapp_business_account", "entry": []})
    response = await client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    assert response.status_code == 403


async def test_post_missing_signature_returns_403(client: AsyncClient) -> None:
    body = json.dumps({"object": "whatsapp_business_account", "entry": []})
    response = await client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 403


async def test_health_endpoint(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
