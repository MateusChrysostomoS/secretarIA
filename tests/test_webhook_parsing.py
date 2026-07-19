"""Tests for the webhook payload parser, focused on interactive replies."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from secretaria.schemas.webhook import (  # noqa: E402
    WebhookMessage,
    extract_inbound_body,
)


def test_extract_text_body() -> None:
    msg = WebhookMessage.model_validate(
        {
            "id": "wamid.1",
            "from": "5511999999999",
            "type": "text",
            "text": {"body": "Oi, queria marcar uma consulta"},
        }
    )
    assert extract_inbound_body(msg) == "Oi, queria marcar uma consulta"


def test_extract_button_reply() -> None:
    msg = WebhookMessage.model_validate(
        {
            "id": "wamid.2",
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "confirm_yes", "title": "Confirmar"},
            },
        }
    )
    assert extract_inbound_body(msg) == "Confirmar"


def test_extract_list_reply_slot_includes_iso_in_body() -> None:
    msg = WebhookMessage.model_validate(
        {
            "id": "wamid.3",
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {
                    "id": "slot|2026-05-29T15:00:00",
                    "title": "15:00",
                    "description": "29/05/2026",
                },
            },
        }
    )
    body = extract_inbound_body(msg)
    assert body is not None
    assert "15:00" in body
    assert "2026-05-29T15:00:00" in body


def test_extract_list_reply_professional_includes_uuid_in_body() -> None:
    msg = WebhookMessage.model_validate(
        {
            "id": "wamid.31",
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {
                    "id": "prof|9f3c2ab4-7c1d-4b6e-9a70-1234567890ab",
                    "title": "Dra. Ana",
                    "description": "Cardiologia",
                },
            },
        }
    )
    body = extract_inbound_body(msg)
    assert body == "Dra. Ana (9f3c2ab4-7c1d-4b6e-9a70-1234567890ab)"


def test_extract_list_reply_non_slot_id_falls_back_to_title() -> None:
    msg = WebhookMessage.model_validate(
        {
            "id": "wamid.4",
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {"id": "service_consult", "title": "Consulta"},
            },
        }
    )
    assert extract_inbound_body(msg) == "Consulta"


def test_unknown_message_type_yields_none() -> None:
    msg = WebhookMessage.model_validate(
        {"id": "wamid.5", "from": "5511999999999", "type": "image"}
    )
    assert extract_inbound_body(msg) is None


def test_empty_interactive_yields_none() -> None:
    msg = WebhookMessage.model_validate(
        {
            "id": "wamid.6",
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {}},
        }
    )
    assert extract_inbound_body(msg) is None
