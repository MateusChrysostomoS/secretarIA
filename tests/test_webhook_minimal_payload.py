"""The arq job payload is MINIMAL, not the raw Meta body — PROMPT_FIX_21.

Whatever `POST /webhook` enqueues is what gets serialized into Redis, i.e.
stored outside the database. The full Meta body carries a lot the worker never
reads, and some of it is personal data:

  * `statuses[].recipient_id` — a full phone number on every delivery receipt;
  * `metadata.display_phone_number` — the business' own number;
  * `smb_app_state_sync[].contact` — full_name + phone_number of the business'
    address book;
  * `history[].threads` — the chat backlog itself.

`minimal_event_payload` drops all of that while keeping the exact key names, so
the worker parses the reduced envelope with the unchanged `WebhookPayload` and
every message id (the idempotency key) survives.

The HMAC check and the fast 200 ack are asserted here too: shrinking the
payload must not weaken either.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

from secretaria.schemas.webhook import (  # noqa: E402
    WebhookPayload,
    extract_action_button,
    extract_echo_body,
    extract_greeting_button,
    extract_inbound_body,
    history_item_is_final,
    iter_event_ids,
    minimal_event_payload,
)

PHONE_NUMBER_ID = "1234567890"
PATIENT_WA_ID = "5511988887777"
BUSINESS_NUMBER = "+55 11 3333-4444"
OTHER_PATIENT = "5521977776666"


def _messages_payload() -> dict:
    """A realistic `messages` change, including the parts we must NOT enqueue."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": BUSINESS_NUMBER,
                                "phone_number_id": PHONE_NUMBER_ID,
                            },
                            "contacts": [{"wa_id": PATIENT_WA_ID, "profile": {"name": "Maria"}}],
                            "messages": [
                                {
                                    "id": "wamid.TEXT",
                                    "from": PATIENT_WA_ID,
                                    "to": PHONE_NUMBER_ID,
                                    "timestamp": "1750000000",
                                    "type": "text",
                                    "text": {"body": "quero marcar"},
                                    "context": {"from": OTHER_PATIENT, "id": "wamid.QUOTED"},
                                },
                                {
                                    "id": "wamid.BTN",
                                    "from": PATIENT_WA_ID,
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {
                                            "id": "greeting|agendar",
                                            "title": "Agendar",
                                            "description": "unused",
                                        },
                                    },
                                },
                            ],
                            # Delivery receipts: pure PII, never read.
                            "statuses": [
                                {
                                    "id": "wamid.OLD",
                                    "recipient_id": OTHER_PATIENT,
                                    "status": "delivered",
                                    "pricing": {"billable": True},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _flat(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------
# What the envelope keeps
# --------------------------------------------------------------------------


def test_keeps_everything_the_worker_reads() -> None:
    minimal = minimal_event_payload(_messages_payload())
    value = minimal["entry"][0]["changes"][0]["value"]

    assert minimal["entry"][0]["changes"][0]["field"] == "messages"
    assert value["metadata"] == {"phone_number_id": PHONE_NUMBER_ID}
    assert value["contacts"] == [{"wa_id": PATIENT_WA_ID, "profile": {"name": "Maria"}}]

    text_msg, button_msg = value["messages"]
    assert text_msg["id"] == "wamid.TEXT"
    assert text_msg["from"] == PATIENT_WA_ID
    assert text_msg["type"] == "text"
    assert text_msg["text"] == {"body": "quero marcar"}
    assert button_msg["interactive"]["button_reply"] == {
        "id": "greeting|agendar",
        "title": "Agendar",
    }


def test_drops_the_personal_data_the_worker_never_reads() -> None:
    minimal = minimal_event_payload(_messages_payload())
    value = minimal["entry"][0]["changes"][0]["value"]
    flat = _flat(minimal)

    assert "statuses" not in value
    assert OTHER_PATIENT not in flat  # the delivery receipt's recipient
    assert BUSINESS_NUMBER not in flat  # display_phone_number
    assert "messaging_product" not in value
    assert "timestamp" not in value["messages"][0]
    assert "context" not in value["messages"][0]  # quoted-message metadata
    assert "to" not in value["messages"][0]  # the clinic's own number
    assert "description" not in value["messages"][1]["interactive"]["button_reply"]


def test_idempotency_keys_survive() -> None:
    payload = _messages_payload()
    assert list(iter_event_ids(minimal_event_payload(payload))) == list(iter_event_ids(payload))


def test_it_actually_shrinks() -> None:
    payload = _messages_payload()
    assert len(_flat(minimal_event_payload(payload))) < len(_flat(payload))


# --------------------------------------------------------------------------
# It still parses, and every extractor still works
# --------------------------------------------------------------------------


def test_the_reduced_envelope_parses_with_the_unchanged_model() -> None:
    minimal = minimal_event_payload(_messages_payload())
    event = WebhookPayload.model_validate(minimal)
    value = event.entry[0].changes[0].value

    assert value.metadata.phone_number_id == PHONE_NUMBER_ID
    assert value.contacts[0].wa_id == PATIENT_WA_ID
    assert value.contacts[0].profile.name == "Maria"
    text_msg, button_msg = value.messages
    assert text_msg.from_ == PATIENT_WA_ID
    assert extract_inbound_body(text_msg) == "quero marcar"
    assert extract_inbound_body(button_msg) == "Agendar"
    assert extract_greeting_button(button_msg) == "agendar"


def test_template_button_payloads_survive() -> None:
    """Reminder action buttons ride on `button.payload` (template carrier)."""
    appointment_id = "4b1c2d3e-0000-4000-8000-000000000001"
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "messages": [
                                {
                                    "id": "wamid.BTNTMPL",
                                    "from": PATIENT_WA_ID,
                                    "type": "button",
                                    "button": {
                                        "payload": f"apptconfirm|{appointment_id}",
                                        "text": "Confirmar",
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }
    event = WebhookPayload.model_validate(minimal_event_payload(payload))
    msg = event.entry[0].changes[0].value.messages[0]
    assert extract_action_button(msg) == ("apptconfirm", appointment_id)


def test_audio_messages_keep_only_the_media_id() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "messages": [
                                {
                                    "id": "wamid.AUDIO",
                                    "from": PATIENT_WA_ID,
                                    "type": "audio",
                                    "audio": {
                                        "id": "MEDIA1",
                                        "mime_type": "audio/ogg",
                                        "voice": True,
                                        "sha256": "unused",
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }
    value = minimal_event_payload(payload)["entry"][0]["changes"][0]["value"]
    assert value["messages"][0]["audio"] == {"id": "MEDIA1"}


def test_echoes_keep_the_recipient_because_it_is_the_patient() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "smb_message_echoes",
                        "value": {
                            "metadata": {
                                "display_phone_number": BUSINESS_NUMBER,
                                "phone_number_id": PHONE_NUMBER_ID,
                            },
                            "contacts": [{"wa_id": PATIENT_WA_ID, "profile": {"name": "Maria"}}],
                            "message_echoes": [
                                {
                                    "id": "wamid.ECHO",
                                    "from": PHONE_NUMBER_ID,
                                    "to": PATIENT_WA_ID,
                                    "type": "text",
                                    "text": {"body": "ja te retorno"},
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }
    minimal = minimal_event_payload(payload)
    event = WebhookPayload.model_validate(minimal)
    echo = event.entry[0].changes[0].value.message_echoes[0]
    assert echo.to == PATIENT_WA_ID  # how the worker resolves the patient
    assert extract_echo_body(echo) == "ja te retorno"
    assert BUSINESS_NUMBER not in _flat(minimal)


def test_history_keeps_progress_and_error_count_but_no_threads() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "history",
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "history": [
                                {
                                    "metadata": {"phase": "COMPLETE", "progress": 100},
                                    "threads": [
                                        {"id": OTHER_PATIENT, "messages": ["private backlog"]}
                                    ],
                                    "errors": [{"code": 1, "message": "declined"}],
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }
    minimal = minimal_event_payload(payload)
    flat = _flat(minimal)
    assert "private backlog" not in flat
    assert OTHER_PATIENT not in flat
    assert "declined" not in flat

    event = WebhookPayload.model_validate(minimal)
    value = event.entry[0].changes[0].value
    assert len(value.history) == 1  # chunk_count preserved
    assert len(value.history[0].errors) == 1  # truthiness/count preserved
    assert history_item_is_final(value.history[0]) is True


def test_state_sync_never_carries_the_contact_book() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "smb_app_state_sync",
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "state_sync": [
                                {
                                    "type": "contact",
                                    "action": "add",
                                    "contact": {
                                        "full_name": "Maria Silva",
                                        "phone_number": OTHER_PATIENT,
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }
    minimal = minimal_event_payload(payload)
    flat = _flat(minimal)
    assert "Maria Silva" not in flat
    assert OTHER_PATIENT not in flat

    event = WebhookPayload.model_validate(minimal)
    assert len(event.entry[0].changes[0].value.state_sync) == 1  # count preserved


def test_unknown_fields_keep_their_name_and_nothing_else() -> None:
    payload = {
        "entry": [{"changes": [{"field": "account_alerts", "value": {"secret": OTHER_PATIENT}}]}]
    }
    minimal = minimal_event_payload(payload)
    assert minimal["entry"][0]["changes"][0] == {"field": "account_alerts", "value": {}}


@pytest.mark.parametrize(
    "payload",
    [None, "not a dict", {}, {"entry": None}, {"entry": ["bad"]}, {"entry": [{"changes": None}]}],
)
def test_never_raises_on_a_malformed_payload(payload) -> None:
    assert isinstance(minimal_event_payload(payload), dict)


# --------------------------------------------------------------------------
# The webhook endpoint: HMAC, fast ACK, and what it enqueues
# --------------------------------------------------------------------------


class _FakeArqPool:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))


def _sign(body: bytes) -> str:
    return f"sha256={hmac.new(b'test-app-secret', body, hashlib.sha256).hexdigest()}"


@pytest.fixture
def arq_pool(monkeypatch: pytest.MonkeyPatch) -> _FakeArqPool:
    from secretaria.main import app as fastapi_app

    pool = _FakeArqPool()
    monkeypatch.setattr(fastapi_app.state, "arq_pool", pool, raising=False)
    return pool


async def test_webhook_enqueues_only_the_minimal_envelope(client, arq_pool) -> None:
    payload = _messages_payload()
    body = json.dumps(payload).encode()

    response = await client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)},
    )

    assert response.status_code == 200  # fast ack preserved
    process_calls = [c for c in arq_pool.calls if c[0] == "process_webhook_event"]
    assert len(process_calls) == 1
    enqueued = process_calls[0][1][0]
    assert enqueued == minimal_event_payload(payload)
    # The load-bearing assertion: nothing personal is written to Redis.
    flat = _flat(enqueued)
    assert OTHER_PATIENT not in flat
    assert BUSINESS_NUMBER not in flat


async def test_invalid_signature_is_still_rejected(client, arq_pool) -> None:
    body = json.dumps(_messages_payload()).encode()
    response = await client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert response.status_code == 403
    assert arq_pool.calls == []
