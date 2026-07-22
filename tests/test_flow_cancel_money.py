"""Tests for the deterministic manage flow's Pix-deposit money hooks
(PROMPT S3 section 4): `workers/tasks.py::_apply_flow_result` /
`_apply_deposit_awareness`.

Exercises `_apply_flow_result` directly with a hand-built `FlowRouterResult`
(the exact shape `flow_router._begin_cancel` / `_manage_cancel` /
`_begin_reschedule` / `_manage_reschedule` produce) — this is the ONE seam
every manage-flow entry path funnels through (direct tap, LLM sentinel
hand-back, or a reminder-button preselection), so testing it here covers all
of them without needing to drive the full flow router / a fake Calendar.
In-memory-sqlite pattern established by test_agent_menu_tools.py /
test_action_buttons.py.
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
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Tenant,
)
from secretaria.services.flow_router import (  # noqa: E402
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_DAY,
    FlowRouterResult,
    MenuBubble,
)
from secretaria.workers import tasks  # noqa: E402


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


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(tasks, "WhatsAppClient", _FakeWhatsAppClient)
    yield


async def _seed(
    db,
    *,
    appointment_start_at: datetime | None = None,
    pix_refund_window_hours: int = 24,
    pix_retention_policy: str = "total",
    pix_reschedule_limit: int = 2,
):
    start_at = appointment_start_at or (datetime.now(UTC) + timedelta(days=2))
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
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
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
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
) -> PixDeposit:
    async with db() as session:
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=appointment.tenant_id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            asaas_payment_id=f"pay-{uuid4()}",
            amount_cents=10000,
            percent_applied=30,
            status=status,
            reschedule_count=reschedule_count,
        )
        session.add(deposit)
        await session.commit()
        await session.refresh(deposit)
        return deposit


def _reply_ctx(conversation) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=conversation.id, patient_wa_id="5511999999999", inbound_body="Sim"
    )


# --------------------------------------------------------------------------
# STEP_MANAGE_CANCEL_CONFIRM: the Sim/Não question itself carries the warning
# --------------------------------------------------------------------------


async def test_cancel_confirm_question_gets_deposit_warning_inside_window(db):
    tenant, patient, conversation, appt = await _seed(
        db, appointment_start_at=datetime.now(UTC) + timedelta(hours=1)
    )
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)

    result = FlowRouterResult(
        action="reply",
        bubbles=[MenuBubble(body="Confirmar o cancelamento?\n\nresumo", labels=["Sim", "Não"])],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CANCEL_CONFIRM,
        flow_managing_appointment_id=appt.id,
    )
    handled = await tasks._apply_flow_result(
        _reply_ctx(conversation), result, patient.wa_id, redis=None, tenant=tenant, waba_token="tok"
    )
    assert handled is True

    client = _FakeWhatsAppClient.created[-1]
    kind, to, body, buttons = client.sent[0]
    assert kind == "buttons"
    assert "não são reembolsáveis" in body
    assert "Confirmar o cancelamento?" in body  # original question preserved
    assert [label for _bid, label in buttons] == ["Sim", "Não"]

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_step == STEP_MANAGE_CANCEL_CONFIRM  # normal persistence, unaffected
        assert conv.flow_managing_appointment_id == appt.id


async def test_cancel_confirm_question_unchanged_without_paid_deposit(db):
    tenant, patient, conversation, appt = await _seed(
        db, appointment_start_at=datetime.now(UTC) + timedelta(hours=1)
    )
    # No deposit at all.
    result = FlowRouterResult(
        action="reply",
        bubbles=[MenuBubble(body="Confirmar o cancelamento?\n\nresumo", labels=["Sim", "Não"])],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CANCEL_CONFIRM,
        flow_managing_appointment_id=appt.id,
    )
    await tasks._apply_flow_result(
        _reply_ctx(conversation), result, patient.wa_id, redis=None, tenant=tenant, waba_token="tok"
    )

    body = _FakeWhatsAppClient.created[-1].sent[0][2]
    assert body == "Confirmar o cancelamento?\n\nresumo"


async def test_cancel_confirm_question_unchanged_outside_window(db):
    tenant, patient, conversation, appt = await _seed(
        db,
        appointment_start_at=datetime.now(UTC) + timedelta(hours=100),
        pix_refund_window_hours=24,
    )
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)
    result = FlowRouterResult(
        action="reply",
        bubbles=[MenuBubble(body="Confirmar o cancelamento?\n\nresumo", labels=["Sim", "Não"])],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_CANCEL_CONFIRM,
        flow_managing_appointment_id=appt.id,
    )
    await tasks._apply_flow_result(
        _reply_ctx(conversation), result, patient.wa_id, redis=None, tenant=tenant, waba_token="tok"
    )

    body = _FakeWhatsAppClient.created[-1].sent[0][2]
    assert body == "Confirmar o cancelamento?\n\nresumo"


# --------------------------------------------------------------------------
# Cancel completion (appointment_cancel_id): money applied + notice appended
# --------------------------------------------------------------------------


async def test_cancel_completion_applies_money_and_appends_notice(db):
    tenant, patient, conversation, appt = await _seed(
        db, appointment_start_at=datetime.now(UTC) + timedelta(hours=1)
    )
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)

    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="Pronto! Sua consulta foi cancelada. ✅")],
        flow_state=FlowState.IDLE,
        flow_step=None,
        appointment_cancel_id=appt.google_event_id,
    )
    handled = await tasks._apply_flow_result(
        _reply_ctx(conversation), result, patient.wa_id, redis=None, tenant=tenant, waba_token="tok"
    )
    assert handled is True

    client = _FakeWhatsAppClient.created[-1]
    body = client.sent[0][2]
    assert "Pronto! Sua consulta foi cancelada." in body
    assert "retido pela clínica" in body  # total policy, inside window

    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.CANCELLED
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appt.id)
        )
        assert deposit.status == PixDepositStatus.CANCELLED_RETAINED


async def test_cancel_completion_without_deposit_no_notice(db):
    tenant, patient, conversation, appt = await _seed(db)
    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="Pronto! Sua consulta foi cancelada. ✅")],
        flow_state=FlowState.IDLE,
        flow_step=None,
        appointment_cancel_id=appt.google_event_id,
    )
    await tasks._apply_flow_result(
        _reply_ctx(conversation), result, patient.wa_id, redis=None, tenant=tenant, waba_token="tok"
    )

    body = _FakeWhatsAppClient.created[-1].sent[0][2]
    assert body == "Pronto! Sua consulta foi cancelada. ✅"

    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.CANCELLED


# --------------------------------------------------------------------------
# Reschedule entry pre-check (freshly-targeted STEP_MANAGE_DAY): blocked
# --------------------------------------------------------------------------


async def test_reschedule_entry_blocked_sends_keep_or_cancel_and_resets_flow(db):
    tenant, patient, conversation, appt = await _seed(db, pix_reschedule_limit=1)
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID, reschedule_count=1)

    result = FlowRouterResult(
        action="reply",
        bubbles=[
            TextBubble(body="Para quando você gostaria de remarcar? (ex: amanhã, sexta, 12/06)")
        ],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_DAY,
        flow_managing_appointment_id=appt.id,
    )
    handled = await tasks._apply_flow_result(
        _reply_ctx(conversation), result, patient.wa_id, redis=None, tenant=tenant, waba_token="tok"
    )
    assert handled is True

    client = _FakeWhatsAppClient.created[-1]
    kind, to, body, buttons = client.sent[0]
    assert kind == "buttons"
    assert "1x" in body
    assert [bid for bid, _label in buttons] == [
        f"apptconfirm|{appt.id}",
        f"apptcancel|{appt.id}",
    ]

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MENU
        assert conv.flow_step is None
        assert conv.flow_managing_appointment_id is None


async def test_reschedule_entry_not_blocked_under_limit_shows_day_ask(db):
    tenant, patient, conversation, appt = await _seed(db, pix_reschedule_limit=2)
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID, reschedule_count=0)

    day_ask = "Para quando você gostaria de remarcar? (ex: amanhã, sexta, 12/06)"
    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=day_ask)],
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=STEP_MANAGE_DAY,
        flow_managing_appointment_id=appt.id,
    )
    await tasks._apply_flow_result(
        _reply_ctx(conversation), result, patient.wa_id, redis=None, tenant=tenant, waba_token="tok"
    )

    body = _FakeWhatsAppClient.created[-1].sent[0][2]
    assert body == day_ask

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
        assert conv.flow_state == FlowState.MANAGE_BOOKING
        assert conv.flow_step == STEP_MANAGE_DAY
        assert conv.flow_managing_appointment_id == appt.id


# --------------------------------------------------------------------------
# Reschedule completion (appointment_reschedule): register_reschedule
# --------------------------------------------------------------------------


async def test_reschedule_completion_increments_count(db):
    tenant, patient, conversation, appt = await _seed(db, pix_reschedule_limit=2)
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID, reschedule_count=0)

    new_start = datetime.now(UTC) + timedelta(days=5)
    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="Pronto! Sua consulta foi remarcada. ✅")],
        flow_state=FlowState.IDLE,
        flow_step=None,
        appointment_reschedule={
            "google_event_id": appt.google_event_id,
            "start_at": new_start,
            "end_at": new_start + timedelta(minutes=30),
        },
    )
    handled = await tasks._apply_flow_result(
        _reply_ctx(conversation), result, patient.wa_id, redis=None, tenant=tenant, waba_token="tok"
    )
    assert handled is True

    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.RESCHEDULED
        # SQLite drops tzinfo on round-trip; compare tz-aware to tz-aware.
        stored_start = (
            refreshed.start_at
            if refreshed.start_at.tzinfo
            else refreshed.start_at.replace(tzinfo=UTC)
        )
        assert stored_start == new_start
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appt.id)
        )
        assert deposit.reschedule_count == 1


async def test_reschedule_completion_race_never_crashes_flow(db):
    """Entry was pre-checked and allowed it, but a race left the deposit
    already at the limit by completion time — the reschedule STILL completes
    (never unwind an already-persisted Calendar move); only the counter
    doesn't advance further."""
    tenant, patient, conversation, appt = await _seed(db, pix_reschedule_limit=1)
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID, reschedule_count=1)

    new_start = datetime.now(UTC) + timedelta(days=5)
    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="Pronto! Sua consulta foi remarcada. ✅")],
        flow_state=FlowState.IDLE,
        flow_step=None,
        appointment_reschedule={
            "google_event_id": appt.google_event_id,
            "start_at": new_start,
            "end_at": new_start + timedelta(minutes=30),
        },
    )
    handled = await tasks._apply_flow_result(
        _reply_ctx(conversation), result, patient.wa_id, redis=None, tenant=tenant, waba_token="tok"
    )
    assert handled is True  # never raises/crashes

    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.RESCHEDULED  # still completes
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appt.id)
        )
        assert deposit.reschedule_count == 1  # not incremented further
