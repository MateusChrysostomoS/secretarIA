"""E-mail to the clinic when a doctor + service has no valid unit (TASK-083, T6; spec §6.3).

At most ONE e-mail per local day per (clinic, doctor, service), to `Tenant.contact_email`, with a
link to fix it in /contexto; nothing about the patient; and it never breaks the patient's flow.
Counter and clock follow `deposit_failure_notice` (a Redis INCR keyed by tenant + local day).
"""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from secretaria.ai.formatter import TextBubble  # noqa: E402
from secretaria.models import Conversation, FlowState, Patient, Professional, Tenant  # noqa: E402
from secretaria.services import (
    email as email_module,  # noqa: E402
    no_unit_notice as notice,  # noqa: E402
)
from secretaria.services.flow_router import FlowRouterResult  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import flow_runner  # noqa: E402
from tests._patching import workers_ns  # noqa: E402
from tests.payments_support import FakeRedis, make_sessionmaker  # noqa: E402

CLINIC_EMAIL = "contato@clinica.example.com"
PATIENT_NAME = "Maria Souza"
PATIENT_PHONE = "5521999998888"
NOON_UTC = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
PORTAL = "https://portal.example.com/"  # trailing slash on purpose

_REAL_SEND = email_module.send_no_unit_alert  # captured before any fixture patches it


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine, maker = await make_sessionmaker()
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinica Teste",
        phone_number_id=str(uuid4())[:12],
        contact_email=CLINIC_EMAIL,
        timezone="America/Sao_Paulo",
    )
    pro = Professional(tenant_id=tenant.id, name="Dra. Ana")
    async with maker() as session:
        session.add_all([tenant, pro])
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(pro)

    sent: list[dict] = []
    send_ok = {"value": True}

    async def fake_send(to_email, clinic_name, **kwargs):
        sent.append({"to": to_email, "clinic": clinic_name, **kwargs})
        return send_ok["value"]

    monkeypatch.setattr(email_module, "send_no_unit_alert", fake_send)
    clock = {"now": NOON_UTC}
    monkeypatch.setattr(notice, "_now", lambda: clock["now"])
    monkeypatch.setattr(
        notice,
        "get_settings",
        lambda: SimpleNamespace(BRAIN_MESSAGE_PUBLIC_URL=PORTAL, REDIS_URL="redis://unused"),
    )
    yield SimpleNamespace(
        maker=maker,
        tenant=tenant,
        pro=pro,
        service_id=uuid4(),
        sent=sent,
        send_ok=send_ok,
        clock=clock,
        redis=FakeRedis(),
    )
    await engine.dispose()


async def _send(env, *, service_id=None, service_name="Consulta"):
    return await notice.send_no_unit_notice(
        env.tenant,
        env.pro.id,
        env.pro.name,
        service_id or env.service_id,
        service_name,
        redis=env.redis,
    )


async def test_link_points_to_contexto_with_prof(env):
    # the trailing slash of the configured base is stripped
    pid = uuid4()
    assert notice.no_unit_link(env.tenant, pid) == f"https://portal.example.com/contexto/?prof={pid}"


async def test_first_notice_sent_second_same_day_skipped(env):
    assert await _send(env) is True
    assert await _send(env) is False
    assert len(env.sent) == 1
    assert env.sent[0]["to"] == CLINIC_EMAIL


async def test_next_day_sends_again(env):
    assert await _send(env) is True
    # 23:30 local (Sao Paulo, UTC-3) on Nov 14 = 02:30 UTC on Nov 15: still the same LOCAL day.
    env.clock["now"] = datetime(2026, 11, 15, 2, 30, tzinfo=UTC)
    assert await _send(env) is False
    env.clock["now"] = datetime(2026, 11, 15, 3, 1, tzinfo=UTC)  # 00:01 local, Nov 15
    assert await _send(env) is True
    assert len(env.sent) == 2


async def test_different_service_same_day_sends(env):
    assert await _send(env) is True
    assert await _send(env, service_id=uuid4(), service_name="Exame") is True
    assert len(env.sent) == 2


async def test_no_contact_email_returns_false(env):
    env.tenant.contact_email = None
    assert await _send(env) is False
    env.tenant.contact_email = "   "
    assert await _send(env) is False
    assert env.sent == []


async def test_send_failure_never_raises(env, monkeypatch):
    env.send_ok["value"] = False
    assert await _send(env) is False

    async def boom(*args, **kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(email_module, "send_no_unit_alert", boom)
    assert await _send(env, service_id=uuid4()) is False

    class BrokenRedis:
        async def incr(self, key):
            raise ConnectionError("redis down")

    assert (
        await notice.send_no_unit_notice(
            env.tenant, env.pro.id, env.pro.name, uuid4(), "Consulta", redis=BrokenRedis()
        )
        is False
    )


async def test_failed_send_releases_the_day_slot_so_the_next_turn_can_retry(env):
    env.send_ok["value"] = False
    assert await _send(env) is False
    env.send_ok["value"] = True
    assert await _send(env) is True
    assert await _send(env) is False  # now the day's mail is out
    assert len(env.sent) == 2  # the failed attempt and the delivered one


async def test_body_has_service_doctor_link_and_no_patient_data(env, monkeypatch):
    captured: list[tuple[str, str, str]] = []

    def fake_send_sync(to_email, subject, body):
        captured.append((to_email, subject, body))

    monkeypatch.setattr(email_module, "_send_sync", fake_send_sync)
    monkeypatch.setattr(
        email_module, "get_settings", lambda: SimpleNamespace(SMTP_HOST="smtp.example.com")
    )
    # The fixture swapped in a recording fake; this test goes through the REAL sender.
    monkeypatch.setattr(email_module, "send_no_unit_alert", _REAL_SEND)
    assert await _send(env, service_name="Consulta de rotina") is True
    ((to_email, subject, body),) = captured
    assert to_email == CLINIC_EMAIL
    assert "Consulta de rotina" in body
    assert "Dra. Ana" in body
    assert f"https://portal.example.com/contexto/?prof={env.pro.id}" in body
    for private in (PATIENT_NAME, PATIENT_PHONE, "WhatsApp"):
        assert private not in body
        assert private not in subject


# ---- the worker seam ------------------------------------------------------------------------


@pytest.fixture
def seam(monkeypatch, env):
    monkeypatch.setattr(workers_ns, "async_session_factory", env.maker)
    bubbles: list = []

    async def _fake_dispatch(reply, out, tenant=None, waba_token=None):
        bubbles.extend(out)
        return len(out)

    monkeypatch.setattr(workers_ns, "_dispatch_bubbles", _fake_dispatch)
    calls: list[tuple] = []

    async def fake_notice(tenant, professional_id, name, service_id, service_name, **kw):
        calls.append((tenant.id, professional_id, name, service_id, service_name))
        return True

    monkeypatch.setattr(flow_runner, "send_no_unit_notice", fake_notice)
    return SimpleNamespace(bubbles=bubbles, calls=calls)


async def _reply(env):
    async with env.maker() as session:
        patient = Patient(
            tenant_id=env.tenant.id, channel="brain_message", external_id="x", name=PATIENT_NAME
        )
        session.add(patient)
        await session.flush()
        conv = Conversation(
            tenant_id=env.tenant.id, patient_id=patient.id, flow_state=FlowState.LLM
        )
        session.add(conv)
        await session.commit()
        await session.refresh(conv)
    return tasks._ReplyContext(conversation_id=conv.id, patient_ref="ref", inbound_body="oi")


def _result(env):
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="Não consegui seguir. Avisei a clínica.")],
        notify_no_unit=(env.pro.id, env.service_id, "Consulta"),
    )


async def test_flow_runner_calls_notice_on_effect_and_survives_failure(env, seam, monkeypatch):
    reply = await _reply(env)
    handled = await flow_runner._apply_flow_result(
        reply, _result(env), None, redis=env.redis, tenant=env.tenant
    )
    assert handled is True
    assert len(seam.bubbles) == 1  # the patient was answered
    # the doctor's name comes from the clinic's own roster, never from the patient
    assert seam.calls == [(env.tenant.id, env.pro.id, "Dra. Ana", env.service_id, "Consulta")]

    async def boom(*args, **kwargs):
        raise RuntimeError("mail exploded")

    monkeypatch.setattr(flow_runner, "send_no_unit_notice", boom)
    seam.bubbles.clear()
    handled = await flow_runner._apply_flow_result(
        reply, _result(env), None, redis=env.redis, tenant=env.tenant
    )
    assert handled is True  # a failing e-mail never breaks the patient's turn
    assert len(seam.bubbles) == 1


async def test_flow_runner_without_effect_sends_nothing(env, seam):
    reply = await _reply(env)
    result = _result(env)
    result.notify_no_unit = None
    await flow_runner._apply_flow_result(reply, result, None, redis=env.redis, tenant=env.tenant)
    assert seam.calls == []


async def test_unknown_professional_still_sends_with_a_generic_name(env, seam):
    reply = await _reply(env)
    result = _result(env)
    result.notify_no_unit = (uuid4(), env.service_id, "Consulta")  # not in this clinic's roster
    await flow_runner._apply_flow_result(reply, result, None, redis=env.redis, tenant=env.tenant)
    assert [call[2] for call in seam.calls] == [""]
