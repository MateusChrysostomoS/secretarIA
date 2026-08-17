"""Tests for the reminder action-button routing (PROMPT S3 section 3).

Two layers:
  - Pure `schemas/webhook.py::extract_action_button` decoding (both carriers,
    malformed/unknown ids).
  - End-to-end `workers/tasks.py::_handle_action_button` at "the tasks.py
    seam" (fake WhatsAppClient + in-memory sqlite), mirroring the DB-backed
    pattern established by test_agent_menu_tools.py /
    test_reminders_plugin.py: a real DB, `async_session_factory` monkeypatched
    in place (both `core.database`'s and `workers.tasks`'s own module-level
    binding — tasks.py imports it at module scope).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    HandoverState,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Tenant,
)
from secretaria.schemas.webhook import (  # noqa: E402
    WebhookMessage,
    extract_action_button,
    extract_greeting_button,
    extract_inbound_body,
)
from secretaria.services.flow_router import STEP_MANAGE_DAY  # noqa: E402
from secretaria.services.payments import deposit_lifecycle  # noqa: E402
from secretaria.services.tenant_config import set_asaas_api_key  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

# --------------------------------------------------------------------------
# Pure: extract_action_button
# --------------------------------------------------------------------------


def _interactive_msg(button_id: str) -> WebhookMessage:
    return WebhookMessage.model_validate(
        {
            "id": "wamid.1",
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": button_id, "title": "x"},
            },
        }
    )


def _template_button_msg(payload: str) -> WebhookMessage:
    return WebhookMessage.model_validate(
        {
            "id": "wamid.2",
            "from": "5511999999999",
            "type": "button",
            "button": {"payload": payload, "text": "x"},
        }
    )


@pytest.mark.parametrize(
    "action", ["apptconfirm", "apptresched", "apptcancel", "apptcancelyes"]
)
def test_extract_action_button_both_carriers_agree(action):
    appointment_id = str(uuid4())
    raw = f"{action}|{appointment_id}"
    assert extract_action_button(_interactive_msg(raw)) == (action, appointment_id)
    assert extract_action_button(_template_button_msg(raw)) == (action, appointment_id)


def test_extract_action_button_malformed_uuid_returns_none():
    assert extract_action_button(_interactive_msg("apptconfirm|not-a-uuid")) is None
    assert extract_action_button(_template_button_msg("apptcancel|123")) is None


def test_extract_action_button_unknown_prefix_returns_none():
    assert extract_action_button(_interactive_msg(f"slot|{uuid4()}")) is None
    assert extract_action_button(_interactive_msg("confirm_yes")) is None


def test_extract_action_button_no_button_at_all_returns_none():
    msg = WebhookMessage.model_validate(
        {"id": "wamid.3", "from": "551199", "type": "text", "text": {"body": "oi"}}
    )
    assert extract_action_button(msg) is None


# --------------------------------------------------------------------------
# End-to-end: _handle_action_button
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
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


class _FakeWhatsAppClient:
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

    async def send_template(self, to, template, lang, variables, button_payloads=None):
        self.sent.append(("template", to, template, lang, variables, button_payloads))
        return {"messages": [{"id": "wamid.test"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        self.sent.append(("list", to, body, button_label, rows, section_title))
        return {"messages": [{"id": "wamid.test"}]}


class _FakeAsaasClient:
    """Minimal fake — only `refund_payment` is exercised by these tests."""

    async def refund_payment(self, payment_id, value_cents):
        return {"id": payment_id, "status": "REFUNDED"}

    async def delete_payment(self, payment_id):
        return {"deleted": True}


async def _fake_get_waba_token(session, tenant_id):
    return "decrypted-waba-token"


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch, db):
    # ai/tools + plugins import async_session_factory lazily (patch the
    # source); workers/tasks imports it at module level (patch the attribute)
    # — same split as test_agent_menu_tools.py.
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(tasks, "async_session_factory", db)
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(tasks, "WhatsAppClient", _FakeWhatsAppClient)
    monkeypatch.setattr(tasks, "get_waba_token", _fake_get_waba_token)
    yield


async def _seed(
    db,
    *,
    appointment_start_at: datetime | None = None,
    appointment_status: AppointmentStatus = AppointmentStatus.SCHEDULED,
    pix_refund_window_hours: int = 24,
    pix_retention_policy: str = "total",
    pix_reschedule_limit: int = 2,
):
    """tenant + patient + conversation + one appointment (no google_event_id
    -> _execute_appointment_cancel's Calendar branch is a structural no-op)."""
    start_at = appointment_start_at or (datetime.now(UTC) + timedelta(days=2))
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
            # Model default. The deterministic flows no longer read an on/off
            # key (flow_router.flows_enabled), so there is nothing to switch here.
            initial_flows={},
            pix_deposit_enabled=True,
            pix_refund_window_hours=pix_refund_window_hours,
            pix_retention_policy=pix_retention_policy,
            pix_reschedule_limit=pix_reschedule_limit,
        )
        session.add(tenant)
        await session.flush()
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        appointment = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id="",
            appointment_type="Consulta",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=appointment_status,
        )
        session.add(appointment)
        await session.commit()
        for obj in (tenant, patient, conversation, appointment):
            await session.refresh(obj)
        return tenant, patient, conversation, appointment


async def _seed_deposit(
    db,
    appointment,
    *,
    status: PixDepositStatus = PixDepositStatus.PAID,
    reschedule_count: int = 0,
    amount_cents: int = 10000,
) -> PixDeposit:
    async with db() as session:
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=appointment.tenant_id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            asaas_payment_id=f"pay-{uuid4()}",
            amount_cents=amount_cents,
            percent_applied=30,
            status=status,
            reschedule_count=reschedule_count,
        )
        session.add(deposit)
        await session.commit()
        await session.refresh(deposit)
        return deposit


def _reply_ctx(conversation, *, patient_wa_id: str = "5511999999999") -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=conversation.id, patient_wa_id=patient_wa_id, inbound_body=""
    )


# --------------------------------------------------------------------------
# apptconfirm
# --------------------------------------------------------------------------


async def test_apptconfirm_future_scheduled_confirms_and_leaves_flow_state_untouched(
    db,
):
    tenant, patient, conversation, appt = await _seed(
        db, appointment_start_at=datetime.now(UTC) + timedelta(days=1)
    )
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        conv.flow_state = FlowState.SERVICE_CATALOG
        conv.flow_step = "awaiting_day"
        await session.commit()

    await tasks._handle_action_button(_reply_ctx(conversation), "apptconfirm", str(appt.id))

    client = _FakeWhatsAppClient.created[-1]
    kind, to, body = client.sent[0]
    assert kind == "text"
    assert "Presença confirmada" in body

    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.CONFIRMED
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.SERVICE_CATALOG  # untouched
        assert conv.flow_step == "awaiting_day"  # untouched


async def test_apptconfirm_cancelled_appointment_replies_not_active(db):
    tenant, patient, conversation, appt = await _seed(
        db,
        appointment_status=AppointmentStatus.CANCELLED,
        appointment_start_at=datetime.now(UTC) + timedelta(days=1),
    )

    await tasks._handle_action_button(_reply_ctx(conversation), "apptconfirm", str(appt.id))

    body = _FakeWhatsAppClient.created[-1].sent[0][2]
    assert "não está mais ativa" in body
    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.CANCELLED  # unchanged


async def test_apptconfirm_past_appointment_replies_not_active(db):
    tenant, patient, conversation, appt = await _seed(
        db, appointment_start_at=datetime.now(UTC) - timedelta(hours=2)
    )

    await tasks._handle_action_button(_reply_ctx(conversation), "apptconfirm", str(appt.id))

    body = _FakeWhatsAppClient.created[-1].sent[0][2]
    assert "não está mais ativa" in body


# --------------------------------------------------------------------------
# apptcancel / apptcancelyes
# --------------------------------------------------------------------------


async def test_apptcancel_inside_window_warns_and_leaves_appointment_scheduled(db):
    tenant, patient, conversation, appt = await _seed(
        db,
        appointment_start_at=datetime.now(UTC) + timedelta(hours=1),
        pix_refund_window_hours=24,
        pix_retention_policy="total",
    )
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)

    await tasks._handle_action_button(_reply_ctx(conversation), "apptcancel", str(appt.id))

    client = _FakeWhatsAppClient.created[-1]
    kind, to, body, buttons = client.sent[0]
    assert kind == "buttons"
    assert "não são reembolsáveis" in body
    assert [bid for bid, _label in buttons] == [
        f"apptcancelyes|{appt.id}",
        f"apptresched|{appt.id}",
    ]

    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.SCHEDULED  # untouched


async def test_apptcancel_inside_window_partial_policy_warning_wording(db):
    tenant, patient, conversation, appt = await _seed(
        db,
        appointment_start_at=datetime.now(UTC) + timedelta(hours=1),
        pix_refund_window_hours=24,
        pix_retention_policy="partial",
    )
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID, amount_cents=10000)
    async with db() as session:
        t = await session.get(Tenant, tenant.id)
        t.pix_partial_refund_percent = 50
        await session.commit()

    await tasks._handle_action_button(_reply_ctx(conversation), "apptcancel", str(appt.id))

    body = _FakeWhatsAppClient.created[-1].sent[0][2]
    assert "reembolso parcial de 50%" in body
    assert "R$ 50,00" in body  # 50% of R$ 100,00


async def test_apptcancelyes_executes_cancel_with_retained_notice(db):
    """total retention + inside window -> CANCELLED_RETAINED needs no PSP
    call at all (see deposit_lifecycle.on_appointment_cancelled), so this
    exercises the full money hook with zero Asaas faking."""
    tenant, patient, conversation, appt = await _seed(
        db,
        appointment_start_at=datetime.now(UTC) + timedelta(hours=1),
        pix_refund_window_hours=24,
        pix_retention_policy="total",
    )
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)

    await tasks._handle_action_button(_reply_ctx(conversation), "apptcancelyes", str(appt.id))

    client = _FakeWhatsAppClient.created[-1]
    kind, to, body = client.sent[0]
    assert kind == "text"
    assert "Consulta cancelada." in body
    assert "retido pela clínica" in body

    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.CANCELLED
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appt.id)
        )
        assert deposit.status == PixDepositStatus.CANCELLED_RETAINED


async def test_apptcancel_outside_window_cancels_immediately_with_refund_notice(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant, patient, conversation, appt = await _seed(
        db,
        appointment_start_at=datetime.now(UTC) + timedelta(hours=100),
        pix_refund_window_hours=24,
    )
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)
    async with db() as session:
        await set_asaas_api_key(session, tenant.id, "asaas-key-1234567890")
        await session.commit()
    monkeypatch.setattr(
        deposit_lifecycle, "_asaas_client_for", lambda api_key: _FakeAsaasClient()
    )

    await tasks._handle_action_button(_reply_ctx(conversation), "apptcancel", str(appt.id))

    client = _FakeWhatsAppClient.created[-1]
    kind, to, body = client.sent[0]
    assert kind == "text"
    assert "Consulta cancelada." in body
    assert "estorno" in body.lower()

    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.CANCELLED
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appt.id)
        )
        assert deposit.status == PixDepositStatus.CANCELLED_REFUNDED


async def test_apptcancel_no_deposit_cancels_immediately_no_notice(db):
    tenant, patient, conversation, appt = await _seed(
        db, appointment_start_at=datetime.now(UTC) + timedelta(hours=100)
    )

    await tasks._handle_action_button(_reply_ctx(conversation), "apptcancel", str(appt.id))

    client = _FakeWhatsAppClient.created[-1]
    kind, to, body = client.sent[0]
    assert body == "Consulta cancelada."  # no trailing notice
    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.CANCELLED


# --------------------------------------------------------------------------
# apptresched
# --------------------------------------------------------------------------


async def test_apptresched_at_limit_sends_keep_or_cancel_and_leaves_flow_untouched(db):
    tenant, patient, conversation, appt = await _seed(db, pix_reschedule_limit=2)
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID, reschedule_count=2)

    await tasks._handle_action_button(_reply_ctx(conversation), "apptresched", str(appt.id))

    client = _FakeWhatsAppClient.created[-1]
    kind, to, body, buttons = client.sent[0]
    assert kind == "buttons"
    assert "2x" in body
    assert [bid for bid, _label in buttons] == [
        f"apptconfirm|{appt.id}",
        f"apptcancel|{appt.id}",
    ]

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_managing_appointment_id is None  # untouched
        assert conv.flow_state == FlowState.IDLE  # untouched


class _RescheduleDayCalendar:
    """Minimal calendar for the reminder-button reschedule hand-off.

    `_handle_action_button` now resolves the appointment's OWN agenda before
    handing to `enter_manage_action`, because that opens the tappable day
    picker inside the same turn.
    """

    tzinfo = ZoneInfo("America/Sao_Paulo")

    async def list_available_days(self, start_day, days, slot_minutes=None):
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(days)]


def _async_return(value):
    async def _call(*args, **kwargs):
        return value

    return _call


async def test_apptresched_under_limit_enters_manage_flow_preselected(db, monkeypatch):
    tenant, patient, conversation, appt = await _seed(db, pix_reschedule_limit=2)
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID, reschedule_count=0)
    monkeypatch.setattr(
        tasks, "_appointment_calendar", _async_return(_RescheduleDayCalendar())
    )

    await tasks._handle_action_button(_reply_ctx(conversation), "apptresched", str(appt.id))

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MANAGE_BOOKING
        assert conv.flow_step == STEP_MANAGE_DAY
        assert conv.flow_managing_appointment_id == appt.id

    client = _FakeWhatsAppClient.created[-1]
    # The tappable day picker, built on THIS appointment's own agenda.
    kind, _to, body, _button_label, rows, _section = client.sent[0]
    assert kind == "list"
    assert "remarcar" in body.lower()
    assert rows[0][0].startswith("day|")


async def test_apptresched_without_a_resolvable_calendar_hands_off(db, monkeypatch):
    """No agenda for the appointment (owner gone, or the build failed): hand to
    a human. Never a day list off some other calendar, never the LLM."""
    tenant, patient, conversation, appt = await _seed(db, pix_reschedule_limit=2)
    monkeypatch.setattr(tasks, "_appointment_calendar", _async_return(None))

    await tasks._handle_action_button(_reply_ctx(conversation), "apptresched", str(appt.id))

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.handover_state == HandoverState.HUMAN_ACTIVE


async def test_apptresched_without_deposit_and_flows_enters_manage_flow(db):
    """No deposit at all -> never blocked, straight into the day-ask."""
    tenant, patient, conversation, appt = await _seed(db)

    await tasks._handle_action_button(_reply_ctx(conversation), "apptresched", str(appt.id))

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MANAGE_BOOKING
        assert conv.flow_managing_appointment_id == appt.id


async def test_apptresched_enters_manage_flow_on_an_unconfigured_tenant(db):
    """Replaces the old polite-fallback case, whose premise is gone.

    A clinic that never had `initial_flows` written (`_seed`'s default `{}` — the
    model default every tenant is provisioned with) used to get "entre em contato
    com a nossa equipe" here, because the reminder's Remarcar button had no manage
    flow to enter. It now behaves exactly like the configured tenant above.
    """
    tenant, patient, conversation, appt = await _seed(db)

    await tasks._handle_action_button(_reply_ctx(conversation), "apptresched", str(appt.id))

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MANAGE_BOOKING
        assert conv.flow_managing_appointment_id == appt.id


# --------------------------------------------------------------------------
# Cross-tenant safety
# --------------------------------------------------------------------------


async def test_foreign_tenant_appointment_id_is_polite_miss_nothing_mutated(db):
    tenant_a, patient_a, conversation_a, appt_a = await _seed(db)
    tenant_b, patient_b, conversation_b, appt_b = await _seed(db)

    await tasks._handle_action_button(
        _reply_ctx(conversation_a), "apptconfirm", str(appt_b.id)
    )

    body = _FakeWhatsAppClient.created[-1].sent[0][2]
    assert body == tasks._APPOINTMENT_NOT_FOUND_TEXT

    async with db() as session:
        refreshed_b = await session.get(Appointment, appt_b.id)
        assert refreshed_b.status == AppointmentStatus.SCHEDULED  # untouched


async def test_unknown_appointment_id_is_polite_miss(db):
    tenant, patient, conversation, appt = await _seed(db)

    await tasks._handle_action_button(_reply_ctx(conversation), "apptconfirm", str(uuid4()))

    body = _FakeWhatsAppClient.created[-1].sent[0][2]
    assert body == tasks._APPOINTMENT_NOT_FOUND_TEXT


# --------------------------------------------------------------------------
# Full wiring: _persist_inbound_message bypasses human handover
# --------------------------------------------------------------------------


async def test_action_button_bypasses_human_handover_end_to_end(db):
    tenant, patient, conversation, appt = await _seed(
        db, appointment_start_at=datetime.now(UTC) + timedelta(days=1)
    )
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        conv.handover_state = HandoverState.HUMAN_ACTIVE
        await session.commit()

    msg = WebhookMessage.model_validate(
        {
            "id": "wamid.action.1",
            "from": patient.wa_id,
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": f"apptconfirm|{appt.id}", "title": "Confirmar"},
            },
        }
    )
    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=patient.wa_id,
        patient_name=None,
        wam_id=msg.id,
        body=extract_inbound_body(msg),
        action_button=extract_action_button(msg),
    )
    assert reply is not None
    assert reply.action_button == ("apptconfirm", str(appt.id))

    await tasks._send_bot_reply(reply)

    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.CONFIRMED
        conv = await session.get(Conversation, conversation.id)
        # Handover state itself is never touched by this path.
        assert conv.handover_state == HandoverState.HUMAN_ACTIVE


# --------------------------------------------------------------------------
# _persist_inbound_message wiring: greeting-button short-circuit
# (fixed-greeting-buttons round - see docs/CHECKPOINT_fixed_greeting_buttons.md)
# --------------------------------------------------------------------------


def _greeting_button_msg(button_id: str, title: str = "x", wam_id: str = "wamid.g1"):
    return WebhookMessage.model_validate(
        {
            "id": wam_id,
            "from": "5511999999999",
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": button_id, "title": title},
            },
        }
    )


@pytest.mark.parametrize(
    ("suffix", "title"),
    [("remarcar", "Remarcar"), ("gerenciar", "Gerenciar consulta")],
)
async def test_greeting_button_is_never_short_circuited(db, suffix: str, title: str):
    """Every greeting-trio tap now falls through to the normal route()-backed
    dispatch — `greeting_button_unavailable` stays None and the label text is
    what gets routed.

    These two labels used to have a second, opposite outcome: on a tenant whose
    `initial_flows` lacked the `enabled` key (i.e. every tenant at provisioning
    time), `_persist_inbound_message` caught the tap and degraded it to the fixed
    "entre em contato com a nossa equipe" reply. That branch is unreachable since
    flow_router.flows_enabled became unconditional — `_seed`'s default tenant,
    which still carries `initial_flows={}`, is exactly the row that used to take
    it, so this test doubles as the guard against the gate returning.
    """
    tenant, patient, conversation, _appt = await _seed(db)
    msg = _greeting_button_msg(f"greeting|{suffix}", title=title)

    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=patient.wa_id,
        patient_name=None,
        wam_id=msg.id,
        body=extract_inbound_body(msg),
        greeting_button=extract_greeting_button(msg),
    )
    assert reply is not None
    assert reply.greeting_button_unavailable is None
    assert reply.action_button is None
    assert reply.inbound_body == title  # the label text, routed normally


async def test_greeting_button_outro_is_not_short_circuited(db):
    """"Outro" was always exempt from the (now removed) short-circuit: it
    promises the LLM, so the tap falls through as the plain "Outro" body."""
    tenant, patient, conversation, _appt = await _seed(db)
    msg = _greeting_button_msg("greeting|outro", title="Outro")

    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=patient.wa_id,
        patient_name=None,
        wam_id=msg.id,
        body=extract_inbound_body(msg),
        greeting_button=extract_greeting_button(msg),
    )
    assert reply is not None
    assert reply.greeting_button_unavailable is None
    assert reply.inbound_body == "Outro"


async def test_greeting_button_respects_human_handover(db):
    """UNLIKE action_button, a greeting-button tap is not time/money-critical:
    it respects an active human takeover exactly like any normal message
    (see _persist_inbound_message's docstring)."""
    tenant, patient, conversation, _appt = await _seed(db)
    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        conv.handover_state = HandoverState.HUMAN_ACTIVE
        await session.commit()

    msg = _greeting_button_msg("greeting|cancelar", title="Cancelar")
    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=patient.wa_id,
        patient_name=None,
        wam_id=msg.id,
        body=extract_inbound_body(msg),
        greeting_button=extract_greeting_button(msg),
    )
    assert reply is None
    assert _FakeWhatsAppClient.created == []
