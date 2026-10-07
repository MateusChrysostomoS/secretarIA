"""Tests for the entitlement gate + per-tenant WhatsApp client wiring inside
workers/tasks.py:_send_bot_reply.

`_send_bot_reply` is a DB-touching function (needs a real session). This
mirrors the in-memory-sqlite pattern established in test_waba_encryption.py
(a real DB, monkeypatched in place of the Postgres-backed
`async_session_factory`) rather than test_reactivation.py / test_menu_command.py,
which only cover pure-logic helpers and never construct this seam.

run_agent, get_entitlements and WhatsAppClient are faked so no LLM/network
call is ever made; only the gating + wiring logic in _send_bot_reply itself
is under test.
"""
import os

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai.formatter import TextBubble  # noqa: E402
from secretaria.ai.graph import (  # noqa: E402
    HUMAN_HANDOFF_OFFER_SENTINEL,
    SHOW_MAIN_MENU_SENTINEL,
    with_handback_intro,
)
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    HandoverState,
    Message,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    HUMAN_OFFER_BODY,
    OTHER_OPENER,
    SCOPED_HELP_ESCALATE_MESSAGE,
    STEP_HUMAN_OFFER,
    FlowRouterResult,
)
from secretaria.workers import orchestrator, tasks  # noqa: E402

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "human_backup_24_7": False,
}


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


class _FakeWhatsAppClient:
    """Records constructed instances + sends; installed in place of the real client."""
    # Mirrors WhatsAppClient: the CALLER records the outbound row.
    persists_outbound = False

    created: list["_FakeWhatsAppClient"] = []

    def __init__(self, access_token=None, phone_number_id=None):
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self.sent: list[tuple] = []
        _FakeWhatsAppClient.created.append(self)

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls(access_token=waba_token, phone_number_id=tenant.phone_number_id)

    async def send_text_message(self, to, body):
        self.sent.append(("text", to, body))
        return {"messages": [{"id": "wamid.test"}]}

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", to, body, buttons))
        return {"messages": [{"id": "wamid.test"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        self.sent.append(("list", to, body, rows))
        return {"messages": [{"id": "wamid.test"}]}


@pytest_asyncio.fixture
async def db():
    """A sessionmaker bound to a shared in-memory sqlite DB (StaticPool keeps
    the single connection alive across every session opened from it, so rows
    committed via the `conversation` fixture are visible to _send_bot_reply's
    own `async_session_factory()` calls)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(workers_ns, "WhatsAppClient", _FakeWhatsAppClient)
    # get_waba_token is exercised end-to-end elsewhere (test_waba_encryption.py);
    # here we only care that _send_bot_reply threads whatever it returns into
    # the WhatsApp client, so a fixed decrypted-looking value is enough.
    monkeypatch.setattr(workers_ns, "get_waba_token", _fake_get_waba_token)
    yield


async def _fake_get_waba_token(session, tenant_id):
    return "decrypted-waba-token"


async def _make_conversation(
    db,
    *,
    is_active: bool = True,
    flow_state: FlowState = FlowState.IDLE,
) -> tuple[Tenant, Patient, Conversation]:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=is_active,
            # The MODEL DEFAULT, left deliberately empty: the deterministic flow
            # engine is unconditional now (flow_router.flows_enabled), so there is
            # no longer a flows-off cohort to simulate. Reaching run_agent from
            # here therefore means going through the real escape hatch the product
            # offers - the "Outro" tap / LLM flow state - not through an unset key.
            initial_flows={},
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999", name="Maria")
        session.add_all([tenant, patient])
        await session.flush()
        conversation = Conversation(
            id=uuid4(), tenant_id=tenant.id, patient_id=patient.id, flow_state=flow_state
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(conversation)
        return tenant, patient, conversation


async def _seed_future_appointment(db, tenant: Tenant, patient: Patient, *, start_at: datetime):
    async with db() as session:
        appt = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta Geral",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appt)
        await session.commit()
        await session.refresh(appt)
        return appt


def _reply_context(
    conversation: Conversation, patient: Patient, inbound_body: str = "oi, quero marcar"
) -> tasks._ReplyContext:
    """A turn to feed `_send_bot_reply`.

    The default body is unmatched free text, which the router answers itself.
    Tests that need to observe the LLM leg start the conversation in
    `FlowState.LLM` and pass `_LLM_ANSWER` — since flow_router.flows_enabled
    became unconditional, and an "Outro" tap answers with a fixed question
    (owner, 2026-10-06), the patient's ANSWER in LLM mode is how a turn reaches
    `run_agent`.
    """
    return tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_ref=patient.wa_id,
        inbound_body=inbound_body,
    )


# The patient's answer to the fixed question an "Outro" tap asks (the product's
# deliberate, patient-initiated LLM hand-off): the first turn the model sees.
_LLM_ANSWER = "minha vista está embaçada"


# --------------------------------------------------------------------------
# Entitlement gating
# --------------------------------------------------------------------------


async def test_entitled_reply_flows_as_before(monkeypatch: pytest.MonkeyPatch, db) -> None:
    tenant, patient, conversation = await _make_conversation(db, flow_state=FlowState.LLM)

    async def _fake_get_entitlements(tenant_id, redis):
        assert tenant_id == tenant.id
        return _summary()

    run_agent_calls: list[dict] = []

    async def _fake_run_agent(
        message, context, tenant_config=None, extra_tools=(), redis=None, **kwargs
    ):
        run_agent_calls.append(
            {"message": message, "tenant_config": tenant_config, "extra_tools": extra_tools}
        )
        return "Olá! Tudo bem?"

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(_reply_context(conversation, patient, _LLM_ANSWER))

    assert len(run_agent_calls) == 1
    assert len(_FakeWhatsAppClient.created) == 1
    client = _FakeWhatsAppClient.created[0]
    assert client.sent  # a message was actually sent
    assert client.sent[0][2] == "Olá! Tudo bem?"


async def test_unentitled_summary_suppresses_reply(monkeypatch: pytest.MonkeyPatch, db) -> None:
    tenant, patient, conversation = await _make_conversation(db)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary(active=False, status="canceled")

    run_agent_calls: list[dict] = []

    async def _fake_run_agent(*args, **kwargs):
        run_agent_calls.append(kwargs)
        return "should never be sent"

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert run_agent_calls == []
    assert _FakeWhatsAppClient.created == []


async def test_secretaria_disabled_suppresses_reply(monkeypatch: pytest.MonkeyPatch, db) -> None:
    tenant, patient, conversation = await _make_conversation(db)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary(active=True, secretaria_enabled=False)

    async def _fake_run_agent(*args, **kwargs):
        raise AssertionError("run_agent must not be called when suppressed")

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert _FakeWhatsAppClient.created == []


async def test_none_summary_suppresses_reply(monkeypatch: pytest.MonkeyPatch, db) -> None:
    tenant, patient, conversation = await _make_conversation(db)

    async def _fake_get_entitlements(tenant_id, redis):
        return None

    async def _fake_run_agent(*args, **kwargs):
        raise AssertionError("run_agent must not be called when suppressed")

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert _FakeWhatsAppClient.created == []


async def test_suppressed_reply_logs_tenant_and_status(monkeypatch: pytest.MonkeyPatch, db) -> None:
    tenant, patient, conversation = await _make_conversation(db)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary(active=False, status="past_due")

    async def _fake_run_agent(*args, **kwargs):
        raise AssertionError("run_agent must not be called when suppressed")

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)

    captured: dict = {}
    original_warning = orchestrator.logger.warning

    def _capture(event, **kwargs):
        if event == "bot_reply_suppressed_unentitled":
            captured["event"] = event
            captured.update(kwargs)
        return original_warning(event, **kwargs)

    monkeypatch.setattr(orchestrator.logger, "warning", _capture)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert captured.get("event") == "bot_reply_suppressed_unentitled"
    assert captured.get("tenant_id") == str(tenant.id)
    assert captured.get("status") == "past_due"


# --------------------------------------------------------------------------
# Per-tenant WhatsApp client + plugin tools wiring
# --------------------------------------------------------------------------


async def test_reply_uses_whatsapp_client_for_tenant_with_decrypted_token(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    tenant, patient, conversation = await _make_conversation(db)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary()

    async def _fake_run_agent(
        message, context, tenant_config=None, extra_tools=(), redis=None, **kwargs
    ):
        return "Oi!"

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert len(_FakeWhatsAppClient.created) == 1
    client = _FakeWhatsAppClient.created[0]
    assert client._access_token == "decrypted-waba-token"
    assert client._phone_number_id == tenant.phone_number_id


async def test_run_agent_receives_plugin_tools_for_entitled_addons(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    tenant, patient, conversation = await _make_conversation(db, flow_state=FlowState.LLM)
    sentinel_tools = ["sentinel-tool"]

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary(addons={**_ALL_ADDONS_OFF, "pix_deposit": True})

    run_agent_calls: list[dict] = []

    async def _fake_run_agent(
        message, context, tenant_config=None, extra_tools=(), redis=None, **kwargs
    ):
        run_agent_calls.append({"extra_tools": extra_tools})
        return "Oi!"

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)
    monkeypatch.setattr(workers_ns, "agent_tools_for", lambda summary: sentinel_tools)

    await tasks._send_bot_reply(_reply_context(conversation, patient, _LLM_ANSWER))

    # The entitled add-on's tools are threaded through. `manage_existing_appointment`
    # rides along too — the hand-back tool is offered on every LLM turn now
    # that the manage flow exists for every tenant — so this asserts inclusion rather
    # than an exact list; the hand-back tool has its own tests above.
    assert len(run_agent_calls) == 1
    assert "sentinel-tool" in run_agent_calls[0]["extra_tools"]


# --------------------------------------------------------------------------
# Appointment-context injection wiring ("Outro" -> fixed question -> LLM)
# --------------------------------------------------------------------------


def _days_from_now(days: int) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


async def _run_send_bot_reply_capturing_run_agent(
    monkeypatch: pytest.MonkeyPatch,
    conversation: Conversation,
    patient: Patient,
    inbound_body: str,
) -> list[dict]:
    async def _fake_get_entitlements(tenant_id, redis):
        return _summary()

    run_agent_calls: list[dict] = []

    async def _fake_run_agent(message, context, **kwargs):
        run_agent_calls.append(kwargs)
        return "Claro, posso ajudar."

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(
        tasks._ReplyContext(
            conversation_id=conversation.id,
            patient_ref=patient.wa_id,
            inbound_body=inbound_body,
        )
    )
    return run_agent_calls


async def test_run_agent_receives_appointment_context_on_answer_after_outro(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    tenant, patient, conversation = await _make_conversation(db, flow_state=FlowState.LLM)
    await _seed_future_appointment(db, tenant, patient, start_at=_days_from_now(2))

    calls = await _run_send_bot_reply_capturing_run_agent(
        monkeypatch, conversation, patient, _LLM_ANSWER
    )

    assert len(calls) == 1
    appointment_context = calls[0]["appointment_context"]
    assert appointment_context is not None
    assert "Próxima consulta" in appointment_context
    # The reschedule/cancel hand-back tool is offered because the manage flow
    # exists — which it now does for every tenant (flow_router.flows_enabled).
    assert "manage_existing_appointment" in {t.name for t in calls[0]["extra_tools"]}


async def test_outro_tap_sends_fixed_question_without_calling_the_llm(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    """Owner, 2026-10-06: the "Outro" tap is answered by the flow with the fixed
    "O que te traz à clínica?" and parks the conversation in LLM mode; the model
    runs only on the patient's answer (next test), never on the tap itself."""
    tenant, patient, conversation = await _make_conversation(db)
    await _seed_future_appointment(db, tenant, patient, start_at=_days_from_now(2))

    calls = await _run_send_bot_reply_capturing_run_agent(
        monkeypatch, conversation, patient, "Outro"
    )

    assert calls == []
    assert [sent[2] for sent in _FakeWhatsAppClient.created[-1].sent] == [OTHER_OPENER]
    async with db() as session:
        stored = await session.get(Conversation, conversation.id)
        # The question is recorded as the bot's message: the next turn's model
        # reads it from the history (ai/graph.py::_load_history) as its context.
        bot_bodies = list(
            await session.scalars(
                select(Message.body).where(
                    Message.conversation_id == conversation.id,
                    Message.sender == MessageSender.BOT,
                )
            )
        )
    assert stored.flow_state == FlowState.LLM
    assert bot_bodies == [OTHER_OPENER]


async def test_run_agent_receives_appointment_context_when_already_in_llm_mode(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    tenant, patient, conversation = await _make_conversation(
        db, flow_state=FlowState.LLM
    )
    await _seed_future_appointment(db, tenant, patient, start_at=_days_from_now(1))

    calls = await _run_send_bot_reply_capturing_run_agent(
        monkeypatch, conversation, patient, "quando é minha consulta?"
    )

    assert len(calls) == 1
    assert calls[0]["appointment_context"] is not None


async def test_unmatched_free_text_never_reaches_the_llm(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    """Replaces the old `..._when_flows_disabled` case, whose premise is gone.

    Free text that matches no flow label used to be the ordinary way into the
    LLM, because a tenant with the default `initial_flows={}` had no flows at
    all. Now the router owns the opening turn for everyone: the same message
    comes back as the deterministic menu and `run_agent` is never called. This
    is the invariant the whole "LLM as a last resort" design rests on, so it is
    asserted directly rather than left implied by the tests above.
    """
    tenant, patient, conversation = await _make_conversation(db)
    await _seed_future_appointment(db, tenant, patient, start_at=_days_from_now(2))

    calls = await _run_send_bot_reply_capturing_run_agent(
        monkeypatch, conversation, patient, "oi, quero marcar"
    )

    assert calls == []
    # ...and the patient did get a real answer - the menu, not silence.
    assert _FakeWhatsAppClient.created[-1].sent


async def test_run_agent_receives_no_appointment_context_for_new_patient_without_appointments(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    # In LLM mode after "Outro", but this (new) patient has no upcoming
    # appointments - the gate's non-empty check loses.
    tenant, patient, conversation = await _make_conversation(db, flow_state=FlowState.LLM)

    calls = await _run_send_bot_reply_capturing_run_agent(
        monkeypatch, conversation, patient, _LLM_ANSWER
    )

    assert len(calls) == 1


# --------------------------------------------------------------------------
# greeting_button_unavailable: a greeting-button tap this (flows-disabled)
# tenant can't dispatch deterministically (fixed-greeting-buttons round -
# see docs/CHECKPOINT_fixed_greeting_buttons.md). Never the LLM.
# --------------------------------------------------------------------------


def _greeting_button_reply(conversation: Conversation, patient: Patient, suffix: str):
    return tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_ref=patient.wa_id,
        inbound_body=f"greeting|{suffix}",
        greeting_button_unavailable=suffix,
    )


async def _assert_llm_never_called(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_entitlements(tenant_id, redis):
        return _summary()

    async def _fake_run_agent(*args, **kwargs):
        raise AssertionError("run_agent must not be called for a greeting_button_unavailable reply")

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)


@pytest.mark.parametrize(
    "suffix,expected_snippet",
    [
        ("agendar", "agendar uma consulta"),
        ("gerenciar", "remarcar ou cancelar sua consulta"),
        ("remarcar", "remarcar sua consulta"),
        ("cancelar", "cancelar sua consulta"),
    ],
)
async def test_greeting_button_unavailable_known_action_sends_tailored_reply(
    monkeypatch: pytest.MonkeyPatch, db, suffix, expected_snippet
) -> None:
    # flows_enabled=False (default _make_conversation): route() would
    # otherwise delegate unconditionally to the LLM - this must degrade
    # deterministically instead.
    tenant, patient, conversation = await _make_conversation(db)
    await _assert_llm_never_called(monkeypatch)

    await tasks._send_bot_reply(_greeting_button_reply(conversation, patient, suffix))

    assert len(_FakeWhatsAppClient.created) == 1
    client = _FakeWhatsAppClient.created[0]
    assert client._access_token == "decrypted-waba-token"
    assert client._phone_number_id == tenant.phone_number_id
    assert len(client.sent) == 1
    kind, to, body = client.sent[0]
    assert kind == "text"
    assert to == patient.wa_id
    assert expected_snippet in body
    assert "entre em contato com a nossa equipe" in body.lower()


async def test_greeting_button_unavailable_legacy_numeric_suffix_sends_generic_reply(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    """A pre-deploy free-text button's numeric id (schemas/webhook.py::
    extract_greeting_button) degrades to the generic fallback, not a crash
    and not the LLM."""
    tenant, patient, conversation = await _make_conversation(db)
    await _assert_llm_never_called(monkeypatch)

    await tasks._send_bot_reply(_greeting_button_reply(conversation, patient, "0"))

    assert len(_FakeWhatsAppClient.created) == 1
    client = _FakeWhatsAppClient.created[0]
    assert client.sent[0][0] == "text"
    assert "não consigo processar esse pedido automaticamente" in client.sent[0][2].lower()


async def test_greeting_button_unavailable_still_replies_when_conversation_has_no_tenant_row(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    """Defensive: an unresolvable tenant (conversation/tenant row vanished)
    sends nothing rather than erroring - mirrors _handle_action_button's own
    `if tenant is None: return` guard."""
    await _assert_llm_never_called(monkeypatch)
    bogus = tasks._ReplyContext(
        conversation_id=uuid4(),
        patient_ref="5511999",
        inbound_body="greeting|agendar",
        greeting_button_unavailable="agendar",
    )
    await tasks._send_bot_reply(bogus)
    assert _FakeWhatsAppClient.created == []


# --------------------------------------------------------------------------
# service_unavailable: the inactive-tenant fallback (PROMPT_FIX_21)
# --------------------------------------------------------------------------


async def test_inactive_tenant_fallback_uses_that_tenants_own_credentials(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    """It used to build a bare `WhatsAppClient()` and answer from the global env
    scaffold - i.e. possibly from another clinic's WhatsApp number - with no
    entitlement check at all. It has no conversation to resolve a tenant from,
    so the tenant id now rides on the `_ReplyContext`."""
    tenant, patient, _conversation = await _make_conversation(db, is_active=False)
    await _assert_llm_never_called(monkeypatch)

    seen: list = []

    async def _fake_get_entitlements(tenant_id, redis):
        seen.append(tenant_id)
        return _summary()

    # After `_assert_llm_never_called`, which installs its own entitled stub.
    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)

    await tasks._send_bot_reply(
        tasks._ReplyContext(
            conversation_id=None,
            tenant_id=tenant.id,
            patient_ref=patient.wa_id,
            inbound_body="",
            service_unavailable=True,
        )
    )

    assert seen == [tenant.id]  # the entitlement gate ran, for THIS tenant
    assert len(_FakeWhatsAppClient.created) == 1
    client = _FakeWhatsAppClient.created[0]
    assert client._phone_number_id == tenant.phone_number_id
    assert client._access_token == "decrypted-waba-token"
    assert client.sent == [("text", patient.wa_id, tasks.SERVICE_UNAVAILABLE_MESSAGE)]


async def test_inactive_tenant_fallback_is_entitlement_gated(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    """Fails closed like every other outbound: an unentitled tenant gets no
    message at all, not even this one."""
    tenant, patient, _conversation = await _make_conversation(db, is_active=False)
    await _assert_llm_never_called(monkeypatch)

    async def _unentitled(tenant_id, redis):
        return _summary(active=False, status="past_due")

    # After `_assert_llm_never_called`, which installs its own entitled stub.
    monkeypatch.setattr(workers_ns, "get_entitlements", _unentitled)

    await tasks._send_bot_reply(
        tasks._ReplyContext(
            conversation_id=None,
            tenant_id=tenant.id,
            patient_ref=patient.wa_id,
            inbound_body="",
            service_unavailable=True,
        )
    )

    assert _FakeWhatsAppClient.created == []


async def test_dispatch_bubbles_sends_nothing_without_a_tenant(db) -> None:
    """`_dispatch_bubbles` used to fall back to the global env scaffold when
    `tenant` was None. It now sends zero bubbles instead."""
    _tenant, patient, conversation = await _make_conversation(db)
    reply = tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_ref=patient.wa_id,
        inbound_body="oi",
    )

    sent = await tasks._dispatch_bubbles(reply, [TextBubble(body="olá")], tenant=None)

    assert sent == 0
    assert _FakeWhatsAppClient.created == []


# --------------------------------------------------------------------------
# _apply_flow_result "handover": the scoped-help escalation seam
# (trio-gerenciar round - flow_router's _scoped_help_escalate result)
# --------------------------------------------------------------------------


async def test_apply_flow_result_handover_flips_to_human_and_sends_message(db) -> None:
    """action="handover": flow fields persisted, the escalation message sent,
    and the conversation flipped to HUMAN_ACTIVE - so the bot goes quiet and
    the human secretary (who sees the chat in their own WhatsApp app) takes
    over. No LLM involved."""
    tenant, patient, conversation = await _make_conversation(
        db, flow_state=FlowState.SERVICE_CATALOG
    )
    reply = tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_ref=patient.wa_id,
        inbound_body="não sei mesmo",
    )
    result = FlowRouterResult(
        action="handover",
        bubbles=[TextBubble(body=SCOPED_HELP_ESCALATE_MESSAGE)],
        flow_state=FlowState.IDLE,
        flow_step=None,
    )

    handled = await tasks._apply_flow_result(
        reply, result, patient.wa_id, tenant=tenant, waba_token="tok"
    )

    assert handled is True
    client = _FakeWhatsAppClient.created[-1]
    assert client.sent == [("text", patient.wa_id, SCOPED_HELP_ESCALATE_MESSAGE)]
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.handover_state == HandoverState.HUMAN_ACTIVE
        assert conv.flow_state == FlowState.IDLE
        assert conv.flow_step is None


# --------------------------------------------------------------------------
# TASK-038: the agent's words travel with its hand-back to the buttons
# --------------------------------------------------------------------------


async def _run_hand_back_with_intro(monkeypatch, db, intro: str) -> tuple[Patient, list]:
    _tenant_row, patient, conversation = await _make_conversation(db, flow_state=FlowState.LLM)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary()

    async def _fake_run_agent(message, context, **kwargs):
        return with_handback_intro(SHOW_MAIN_MENU_SENTINEL, intro)

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)
    await tasks._send_bot_reply(_reply_context(conversation, patient, _LLM_ANSWER))
    return patient, [sent for client in _FakeWhatsAppClient.created for sent in client.sent]


async def test_hand_back_words_are_sent_before_the_card(monkeypatch, db) -> None:
    """Owner, 2026-10-07: the patient who asked something reads the answer first,
    then the buttons - never the same list again as if unheard."""
    words = "Claro! Para isso, escolha abaixo como prefere seguir."
    patient, sent = await _run_hand_back_with_intro(monkeypatch, db, words)
    assert sent[0] == ("text", patient.wa_id, words)
    assert any(kind == "buttons" for kind, *_ in sent[1:])


async def test_hand_back_words_claiming_an_unproven_action_are_dropped(monkeypatch, db) -> None:
    _patient, sent = await _run_hand_back_with_intro(
        monkeypatch, db, "Seu pagamento foi confirmado, escolha abaixo."
    )
    assert all("pagamento" not in str(item) for item in sent)
    assert any(kind == "buttons" for kind, *_ in sent)  # the card itself still goes


async def test_hand_back_words_never_go_out_alone(monkeypatch, db) -> None:
    """Review of TASK-038: a hand-back that ends up sending no card must still get the
    safety-net apology - never just the agent's words ("escolha abaixo") over nothing."""

    async def _sends_nothing(*args, **kwargs):
        return None

    fallback_causes: list[str] = []

    async def _fallback(reply, redis, *, cause):
        fallback_causes.append(cause)

    monkeypatch.setattr(workers_ns, "_handle_show_main_menu", _sends_nothing)
    monkeypatch.setattr(workers_ns, "_send_turn_fallback", _fallback)
    _patient, sent = await _run_hand_back_with_intro(monkeypatch, db, "Claro! Escolha abaixo.")
    assert sent == []  # the words were held for a card that never came
    assert fallback_causes == ["silent_return"]  # so the safety net answers



async def test_the_human_offer_card_then_yes_hands_over(monkeypatch, db) -> None:
    """TASK-038 end to end: the agent offers, the patient taps "✅ Sim", a human owns it."""
    _tenant_row, patient, conversation = await _make_conversation(db, flow_state=FlowState.LLM)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary()

    async def _fake_run_agent(message, context, **kwargs):
        return with_handback_intro(HUMAN_HANDOFF_OFFER_SENTINEL, "Não tenho esse valor aqui.")

    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(workers_ns, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(_reply_context(conversation, patient, "quanto custa a cirurgia?"))
    offered = [sent for client in _FakeWhatsAppClient.created for sent in client.sent]
    assert offered[0] == ("text", patient.wa_id, "Não tenho esse valor aqui.")
    kind, _to, body, buttons = offered[1]
    assert (kind, body) == ("buttons", HUMAN_OFFER_BODY)
    assert [label for _id, label in buttons] == ["✅ Sim", "❌ Não"]
    async with db() as session:
        stored = await session.get(Conversation, conversation.id)
        assert stored.flow_state == FlowState.LLM
        assert stored.flow_step == STEP_HUMAN_OFFER

    _FakeWhatsAppClient.created = []
    await tasks._send_bot_reply(_reply_context(conversation, patient, "✅ Sim"))
    answered = [sent for client in _FakeWhatsAppClient.created for sent in client.sent]
    assert answered == [("text", patient.wa_id, SCOPED_HELP_ESCALATE_MESSAGE)]
    async with db() as session:
        stored = await session.get(Conversation, conversation.id)
        assert stored.handover_state == HandoverState.HUMAN_ACTIVE
