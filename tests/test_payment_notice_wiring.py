"""TASK-093 Plano 2 / Task 3: o aviso neutro ligado nos ramos que respondem ao paciente.

Reusa o harness de `test_bot_reply_gating.py` (SQLite em memória, cliente WhatsApp falso).
"""

# ruff: noqa: F811  (fixtures importadas e pedidas por nome)

from pathlib import Path

import pytest

from secretaria.models import FlowState
from secretaria.services.payment_notice import PAYMENT_NOTICE_TEXT
from secretaria.workers import tasks
from tests._patching import workers_ns
from tests.payments_support import FakeRedis
from tests.test_bot_reply_gating import (  # noqa: F401
    _fakes,
    _FakeWhatsAppClient,
    _make_conversation,
    _reply_context,
    _summary,
    db,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "secretaria"


def _texts() -> list[str]:
    return [
        item[2]
        for client in _FakeWhatsAppClient.created
        for item in client.sent
        if item[0] == "text"
    ]


@pytest.fixture
def unpaid(monkeypatch):
    async def _unpaid(tenant_id, redis):
        return _summary(active=False, status="past_due")

    async def _no_ai(*a, **k):
        raise AssertionError("a IA não pode ser chamada sem pagamento")

    monkeypatch.setattr(workers_ns, "get_entitlements", _unpaid)
    monkeypatch.setattr(workers_ns, "run_agent", _no_ai)


# (a) turno de paciente sem pagamento -> 1 aviso por dia, sem IA.
async def test_unpaid_turn_sends_one_notice_and_no_ai(unpaid, db) -> None:
    _tenant, patient, conversation = await _make_conversation(db, flow_state=FlowState.LLM)
    redis = FakeRedis()

    for _ in range(3):
        await tasks._send_bot_reply(_reply_context(conversation, patient), redis=redis)

    assert _texts() == [PAYMENT_NOTICE_TEXT]


# (b) o log de supressão continua sendo emitido.
async def test_suppression_log_is_still_emitted(unpaid, db, monkeypatch) -> None:
    from secretaria.workers import orchestrator

    _tenant, patient, conversation = await _make_conversation(db)
    events: list[str] = []
    real = orchestrator.logger.warning

    def _spy(event, *a, **k):
        events.append(event)
        return real(event, *a, **k)

    monkeypatch.setattr(orchestrator.logger, "warning", _spy)

    await tasks._send_bot_reply(_reply_context(conversation, patient), redis=FakeRedis())

    assert "bot_reply_suppressed_unentitled" in events


# Brain fora do ar: nenhuma resposta e nenhum aviso.
async def test_summary_none_sends_nothing(db, monkeypatch) -> None:
    async def _down(tenant_id, redis):
        return None

    monkeypatch.setattr(workers_ns, "get_entitlements", _down)
    _tenant, patient, conversation = await _make_conversation(db)

    await tasks._send_bot_reply(_reply_context(conversation, patient), redis=FakeRedis())

    assert _FakeWhatsAppClient.created == []


# Clínica em dia: nenhum aviso (comportamento atual intacto).
async def test_paid_clinic_gets_no_notice(db, monkeypatch) -> None:
    async def _paid(tenant_id, redis):
        return _summary()

    async def _agent(*a, **k):
        return "Olá!"

    monkeypatch.setattr(workers_ns, "get_entitlements", _paid)
    monkeypatch.setattr(workers_ns, "run_agent", _agent)
    _tenant, patient, conversation = await _make_conversation(db, flow_state=FlowState.LLM)

    await tasks._send_bot_reply(
        _reply_context(conversation, patient, "dor de cabeça"), redis=FakeRedis()
    )

    assert PAYMENT_NOTICE_TEXT not in _texts()


# Anexo sem pagamento: 1 aviso, não o "Recebi seu arquivo".
async def test_attachment_without_payment_gets_the_notice(unpaid, db) -> None:
    tenant, patient, conversation = await _make_conversation(db)
    reply = tasks._ReplyContext(
        conversation_id=conversation.id,
        tenant_id=tenant.id,
        patient_ref=patient.wa_id,
        inbound_body="",
        attachment_received=True,
    )

    await tasks._send_bot_reply(reply, redis=FakeRedis())

    assert _texts() == [PAYMENT_NOTICE_TEXT]


# Degrade "serviço indisponível" sem pagamento: aviso neutro, não o texto de degrade.
async def test_service_unavailable_without_payment_gets_the_notice(unpaid, db) -> None:
    tenant, patient, _conversation = await _make_conversation(db, is_active=False)
    reply = tasks._ReplyContext(
        conversation_id=None,
        tenant_id=tenant.id,
        patient_ref=patient.wa_id,
        inbound_body="",
        service_unavailable=True,
    )

    await tasks._send_bot_reply(reply, redis=FakeRedis())

    assert _texts() == [PAYMENT_NOTICE_TEXT]


# Toque em botão de lembrete antigo sem pagamento: aviso neutro, a ação NÃO roda.
async def test_button_tap_without_payment_gets_the_notice_and_skips_the_action(
    unpaid, db, monkeypatch
) -> None:
    from uuid import uuid4

    from secretaria.workers import orchestrator

    async def _boom(*a, **k):
        raise AssertionError("a ação do botão não pode rodar sem pagamento")

    monkeypatch.setattr(orchestrator, "_handle_action_button", _boom)
    tenant, patient, conversation = await _make_conversation(db)
    reply = tasks._ReplyContext(
        conversation_id=conversation.id,
        tenant_id=tenant.id,
        patient_ref=patient.wa_id,
        inbound_body="",
        action_button=("apptconfirm", str(uuid4())),
    )

    redis = FakeRedis()
    for _ in range(2):
        await tasks._send_bot_reply(reply, redis=redis)

    assert _texts() == [PAYMENT_NOTICE_TEXT]


# Toque com clínica em dia ou pagamento ilegível: segue o caminho de sempre.
@pytest.mark.parametrize("state", ["paid", "unknown"])
async def test_button_tap_paid_or_unknown_runs_the_action(db, monkeypatch, state) -> None:
    from uuid import uuid4

    from secretaria.workers import orchestrator

    async def _entitlement(tenant_id, redis):
        return _summary() if state == "paid" else None

    called: list[str] = []

    async def _action(reply, action, appointment_id, redis=None):
        called.append(action)

    monkeypatch.setattr(workers_ns, "get_entitlements", _entitlement)
    monkeypatch.setattr(orchestrator, "_handle_action_button", _action)
    tenant, patient, conversation = await _make_conversation(db)
    reply = tasks._ReplyContext(
        conversation_id=conversation.id,
        tenant_id=tenant.id,
        patient_ref=patient.wa_id,
        inbound_body="",
        action_button=("apptconfirm", str(uuid4())),
    )

    await tasks._send_bot_reply(reply, redis=FakeRedis())

    assert called == ["apptconfirm"]
    assert PAYMENT_NOTICE_TEXT not in _texts()


async def test_no_redis_means_silence_in_every_branch(unpaid, db) -> None:
    tenant, patient, conversation = await _make_conversation(db)
    await tasks._send_bot_reply(_reply_context(conversation, patient))
    await tasks._send_bot_reply(
        tasks._ReplyContext(
            conversation_id=conversation.id,
            tenant_id=tenant.id,
            patient_ref=patient.wa_id,
            inbound_body="",
            attachment_received=True,
        )
    )
    assert _FakeWhatsAppClient.created == []


# (c)(d)(e) proativos e console: o aviso NUNCA é ligado fora dos 3 ramos de resposta.
@pytest.mark.parametrize(
    "relative",
    [
        "workers/portal/open.py",
        "workers/portal/merge.py",
        "workers/whatsapp/remove_context.py",
        "workers/reminder_engine.py",
        "workers/shared/appointment_edit_notification.py",
        "plugins/reminders.py",
        "api/hub/conversations.py",
        "services/staff_patient_message.py",
    ],
)
def test_proactive_and_console_paths_never_use_the_notice(relative: str) -> None:
    assert "payment_notice" not in (SRC / relative).read_text(encoding="utf-8")


def test_the_three_reply_branches_use_the_notice() -> None:
    for relative in (
        "workers/orchestrator.py",
        "workers/portal/attachments.py",
        "workers/shared/degrade.py",
    ):
        assert "send_payment_notice_once" in (SRC / relative).read_text(encoding="utf-8")
