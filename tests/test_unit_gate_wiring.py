"""TASK-090 Adendo 1 §A - the side doors that also make the secretária speak honour the unit gate.

The main turn, the safety net, the degrade and the Portal entry have their own tests
(tests/test_bot_reply_gating.py, test_turn_safety_net.py, test_portal_context_opening.py). This
pins the attachment receipt: with the gate closed nothing is sent, with it open the receipt goes.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.workers.portal import attachments
from tests._patching import workers_ns


class _Session:
    def __init__(self, tenant) -> None:
        self._tenant = tenant

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002
        return False

    async def get(self, _model, _id):  # noqa: ANN001
        return self._tenant


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    tenant = SimpleNamespace(id=uuid4())
    sent: list[str] = []

    async def _entitled(_tenant_id, _redis):  # noqa: ANN001
        return SimpleNamespace(active=True, secretaria_enabled=True, status="active")

    async def _send(_to, body, *, client):  # noqa: ANN001
        sent.append(body)

    monkeypatch.setattr(workers_ns, "async_session_factory", lambda: _Session(tenant))
    monkeypatch.setattr(workers_ns, "get_entitlements", _entitled)
    monkeypatch.setattr(attachments, "_reply_sender", lambda *_a: object())
    monkeypatch.setattr(attachments, "_send_simple_text", _send)
    return sent


def _reply() -> SimpleNamespace:
    return SimpleNamespace(tenant_id=uuid4(), conversation_id=uuid4(), patient_ref="ext-1")


@pytest.mark.parametrize("blocked", [True, False])
async def test_the_attachment_receipt_honours_the_unit_gate(monkeypatch, wired, blocked) -> None:
    async def _gate(*_args, **_kwargs) -> bool:
        return blocked

    monkeypatch.setattr(workers_ns, "reply_blocked_no_usable_unit", _gate)
    await attachments._handle_attachment_received(_reply())
    assert wired == ([] if blocked else [attachments.ATTACHMENT_RECEIVED_MESSAGE])
