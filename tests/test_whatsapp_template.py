"""Tests for WhatsAppClient.send_template — the HSM utility-template send
(whatsapp-webhook-arq skill: reminders outside the 24h window must use a
pre-approved template, in the exact Cloud API template payload shape)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402

from secretaria.services.whatsapp import WhatsAppClient  # noqa: E402


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace WhatsAppClient._post so no network call is made; capture the payload."""
    box: dict = {}

    async def _fake_post(self, payload, to):
        box["payload"] = payload
        box["to"] = to
        return {"messages": [{"id": "wamid.test"}]}

    monkeypatch.setattr(WhatsAppClient, "_post", _fake_post)
    return box


async def test_send_template_payload_shape(captured: dict) -> None:
    client = WhatsAppClient(phone_number_id="123", access_token="tok")

    result = await client.send_template(
        to="5511999999999",
        template="appointment_reminder",
        lang="pt_BR",
        variables=["Consulta em 10/07/2026 às 14:00 na Clinic"],
    )

    assert result == {"messages": [{"id": "wamid.test"}]}
    payload = captured["payload"]
    assert captured["to"] == "5511999999999"
    assert payload == {
        "messaging_product": "whatsapp",
        "to": "5511999999999",
        "type": "template",
        "template": {
            "name": "appointment_reminder",
            "language": {"code": "pt_BR"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Consulta em 10/07/2026 às 14:00 na Clinic"}
                    ],
                }
            ],
        },
    }


async def test_send_template_multiple_variables_preserve_order(captured: dict) -> None:
    client = WhatsAppClient(phone_number_id="123", access_token="tok")

    await client.send_template(
        to="5511999999999", template="tmpl", lang="en_US", variables=["one", "two", "three"]
    )

    params = captured["payload"]["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["one", "two", "three"]
    assert all(p["type"] == "text" for p in params)
