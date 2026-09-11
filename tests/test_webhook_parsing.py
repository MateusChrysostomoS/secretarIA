"""Tests for the webhook payload parser, focused on interactive replies.

A tap arrives from Meta as two separate strings - the control's `title` (what
the patient saw) and its `id` (what we put there to route on) - and they stay
apart all the way to storage. `extract_inbound_body` returns the title, which
becomes `Message.body` and is what the staff console and the patient portal
render; `extract_inbound_reply_id` returns the raw id. `inbound_routing_text`
joins them back, in memory only, into the "<title> (<payload>)" string the flow
router and the agent read.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402

from secretaria.schemas.webhook import (  # noqa: E402
    _PAYLOAD_ROW_PREFIXES,
    WebhookMessage,
    extract_greeting_button,
    extract_inbound_body,
    extract_inbound_reply_id,
    inbound_routing_text,
)

_UUID = "9f3c2ab4-7c1d-4b6e-9a70-1234567890ab"


def _routed(msg: WebhookMessage) -> str | None:
    """What the router reads for this message: the two halves, recomposed."""
    return inbound_routing_text(extract_inbound_body(msg), extract_inbound_reply_id(msg))


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
    # Nothing was tapped: no id, and the routing text is the text itself.
    assert extract_inbound_reply_id(msg) is None
    assert _routed(msg) == "Oi, queria marcar uma consulta"


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
    assert extract_inbound_reply_id(msg) == "confirm_yes"
    # Not a data-carrying id: the router reads the bare label, as it always has.
    assert _routed(msg) == "Confirmar"


def test_extract_list_reply_slot_keeps_the_iso_out_of_the_body() -> None:
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
    assert extract_inbound_body(msg) == "15:00"
    assert extract_inbound_reply_id(msg) == "slot|2026-05-29T15:00:00"
    # The router - and the agent, whose prompt promises "<rótulo> (<iso>)" -
    # still get the ISO: from the id, recomposed in memory.
    assert _routed(msg) == "15:00 (2026-05-29T15:00:00)"


def test_extract_list_reply_professional_keeps_the_uuid_out_of_the_body() -> None:
    """The live bug: "Dr. Fulano (8faa12e1-…)" shown in the staff console."""
    msg = WebhookMessage.model_validate(
        {
            "id": "wamid.31",
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {
                    "id": f"prof|{_UUID}",
                    "title": "Dra. Ana",
                    "description": "Cardiologia",
                },
            },
        }
    )
    body = extract_inbound_body(msg)
    assert body == "Dra. Ana"
    assert _UUID not in body
    assert extract_inbound_reply_id(msg) == f"prof|{_UUID}"
    assert _routed(msg) == f"Dra. Ana ({_UUID})"


def _list_reply(row_id: str, title: str) -> WebhookMessage:
    return WebhookMessage.model_validate(
        {
            "id": "wamid.32",
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {"id": row_id, "title": title},
            },
        }
    )


def test_extract_list_reply_day_routes_with_the_iso_date() -> None:
    """The row title is "Seg, 01/03" — no year, and "01/03" alone is ambiguous
    DMY free text. The id carries the resolved date (and the page the row was
    listed on) so the router never has to re-guess it — and the patient's own
    bubble never has to show it."""
    msg = _list_reply("day|2027-03-01|0", "Seg, 01/03")
    assert extract_inbound_body(msg) == "Seg, 01/03"
    assert _routed(msg) == "Seg, 01/03 (2027-03-01|0)"


def test_extract_list_reply_day_picker_controls_carry_their_cursor() -> None:
    """Pagination state lives in the tap, not in a new conversations column."""
    assert _routed(_list_reply("daymore|2", "Ver mais dias")) == "Ver mais dias (2)"
    assert _routed(_list_reply("dayagain|2", "Escolher outro dia")) == "Escolher outro dia (2)"
    assert _routed(_list_reply("dayback|service", "Voltar")) == "Voltar (service)"
    assert extract_inbound_body(_list_reply("dayback|service", "Voltar")) == "Voltar"


def test_extract_list_reply_day_escape_row_stays_a_plain_label() -> None:
    """The bounded free-text escape must arrive as the plain "Outro" the
    router already dispatches — hence an id OUTSIDE the payload family."""
    msg = _list_reply("dayescape|0", "Outro")
    assert extract_inbound_body(msg) == "Outro"
    assert _routed(msg) == "Outro"


# One sample tap per data-carrying prefix: (row id, title, the payload the
# router must get back). Pinned against _PAYLOAD_ROW_PREFIXES below, so a new
# prefix cannot join the family without being proven display-clean here too.
_PAYLOAD_TAPS = [
    ("slot|2026-05-29T15:00:00", "🗓️ 15:00", "2026-05-29T15:00:00"),
    (f"prof|{_UUID}", "🥼 Dra. Ana", _UUID),
    ("day|2027-03-01|0", "🗓️ Seg, 01/03", "2027-03-01|0"),
    ("daymore|2", "Ver mais dias", "2"),
    ("dayagain|2", "⬅️ Outro dia", "2"),
    ("dayback|service", "⬅️ Outro serviço", "service"),
]


def test_every_payload_prefix_is_covered_below() -> None:
    covered = {row_id.split("|", 1)[0] + "|" for row_id, _title, _payload in _PAYLOAD_TAPS}
    assert covered == set(_PAYLOAD_ROW_PREFIXES)


@pytest.mark.parametrize(("row_id", "title", "payload"), _PAYLOAD_TAPS)
def test_no_data_row_puts_its_payload_in_the_body(row_id: str, title: str, payload: str) -> None:
    msg = _list_reply(row_id, title)
    body = extract_inbound_body(msg)
    assert body == title
    assert payload not in body
    assert extract_inbound_reply_id(msg) == row_id
    # ...while the router keeps getting exactly the string it always got.
    assert _routed(msg) == f"{title} ({payload})"


def test_a_tap_with_no_title_routes_but_has_no_display_text() -> None:
    """Meta echoes the row title, so this is the degenerate case - and it was
    the one where the payload became the WHOLE stored body. Now there is no
    human-readable text at all, while routing keeps the old fallback: the
    payload alone for a data row, the whole id for anything else."""
    data_row = _list_reply(f"prof|{_UUID}", "")
    assert extract_inbound_body(data_row) is None
    assert _routed(data_row) == _UUID

    other = _list_reply("svchelp|0", "")
    assert extract_inbound_body(other) is None
    assert _routed(other) == "svchelp|0"


def test_a_row_stored_before_the_split_reads_back_unchanged() -> None:
    """Rows written before the id had a column keep "<title> (<payload>)" in
    `body` and carry no id: recomposing them must be a no-op, never a second
    "(…)" appended to the first."""
    legacy = f"Dra. Ana ({_UUID})"
    assert inbound_routing_text(legacy, None) == legacy


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
    assert _routed(msg) == "Consulta"


def test_unknown_message_type_yields_none() -> None:
    msg = WebhookMessage.model_validate(
        {"id": "wamid.5", "from": "5511999999999", "type": "image"}
    )
    assert extract_inbound_body(msg) is None
    assert extract_inbound_reply_id(msg) is None


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
    assert extract_inbound_reply_id(msg) is None
    assert _routed(msg) is None


# --------------------------------------------------------------------------
# extract_greeting_button
# --------------------------------------------------------------------------


def _greeting_button_msg(button_id: str, title: str = "x") -> WebhookMessage:
    return WebhookMessage.model_validate(
        {
            "id": "wamid.7",
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": button_id, "title": title},
            },
        }
    )


def test_extract_greeting_button_known_action() -> None:
    for action in ("agendar", "gerenciar", "remarcar", "cancelar", "outro"):
        assert extract_greeting_button(_greeting_button_msg(f"greeting|{action}")) == action


def test_extract_greeting_button_legacy_numeric_id() -> None:
    """A button sent before the fixed-buttons deploy: positional numeric id,
    the clinic's own (now-unread) free-text label as the title."""
    msg = _greeting_button_msg("greeting|0", title="Agendar consulta")
    assert extract_greeting_button(msg) == "0"


def test_extract_greeting_button_not_a_greeting_tap_returns_none() -> None:
    assert extract_greeting_button(_greeting_button_msg("confirm_yes")) is None
    assert extract_greeting_button(_greeting_button_msg("apptconfirm|123")) is None


def test_extract_greeting_button_reactivation_prefix_returns_none() -> None:
    """Reactivation's Sim/Não use a DISTINCT id prefix precisely so they are
    never mistaken for a greeting-button tap here (see
    workers/tasks.py::_send_greeting)."""
    assert extract_greeting_button(_greeting_button_msg("reactivation|0")) is None
    assert extract_greeting_button(_greeting_button_msg("reactivation|1")) is None


def test_extract_greeting_button_no_button_at_all_returns_none() -> None:
    msg = WebhookMessage.model_validate(
        {"id": "wamid.8", "from": "5511999999999", "type": "text", "text": {"body": "oi"}}
    )
    assert extract_greeting_button(msg) is None
