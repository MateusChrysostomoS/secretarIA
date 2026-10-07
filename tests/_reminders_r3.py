"""Wiring shared by the TASK-032 R3 tests: patches, a whole-turn driver, ageing.

The database fixture is R1's (`tests/_reminder_fixtures.py::db`) and the seeds
and the WhatsApp double are R2's (`tests/_reminders_v2.py`). This module adds
what a WHOLE inbound turn needs: the entitlement, a model-free agent, an owner
calendar that records deletes, and a driver that runs `_route_inbound_turn`
inside one transaction and then `_send_bot_reply`, exactly as the WhatsApp and
Portal wrappers do.
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

from sqlalchemy import select  # noqa: E402

from secretaria.core import database as core_database  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentReminder,
    AppointmentStatus,
    Conversation,
    Message,
    Patient,
    Tenant,
)
from secretaria.services.channel_sender import (  # noqa: E402
    CHANNEL_BRAIN_MESSAGE,
    CHANNEL_WHATSAPP,
)
from secretaria.workers.orchestrator import _send_bot_reply  # noqa: E402
from secretaria.workers.turn_router import _route_inbound_turn  # noqa: E402
from tests._patching import workers_ns  # noqa: E402
from tests._reminders_v2 import (  # noqa: E402
    WA_ID,
    FakeWhatsAppClient,
    entitled,
    fake_waba_token,
)

AGENT_REPLY = "Resposta da IA."


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class FakeCalendar:
    """Owner-calendar double: records deletes and creations, every day is open."""

    tzinfo = ZoneInfo("America/Sao_Paulo")

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.created: list[tuple] = []

    async def cancel_event(self, event_id):
        self.cancelled.append(event_id)

    async def create_event(self, start, end, summary, description=""):
        self.created.append((start, end, summary))
        return {"id": "evt-new", "htmlLink": "https://calendar.google.com/evt-new"}

    async def list_available_days(self, start_day, days, slot_minutes=None):
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(1, days)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        return []


def wire(monkeypatch, db, calendar: FakeCalendar | None = None) -> FakeCalendar:
    """Point every worker module at the in-memory DB and the doubles."""
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(workers_ns, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(workers_ns, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(workers_ns, "get_entitlements", entitled)

    async def _agent(message, *args, **kwargs):
        return AGENT_REPLY

    monkeypatch.setattr(workers_ns, "run_agent", _agent)
    owner = calendar or FakeCalendar()

    async def _owner_calendar(*args, **kwargs):
        return owner

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _owner_calendar)
    monkeypatch.setattr(workers_ns, "_calendar_for_appointment", _owner_calendar)
    return owner


def sent() -> list[tuple]:
    """Everything the WhatsApp double sent, in order."""
    return FakeWhatsAppClient.all_sent()


async def consent(db, world) -> None:
    async with db() as session:
        patient = await session.get(Patient, world.patient.id)
        patient.lgpd_accepted_at = datetime.now(UTC) - timedelta(days=30)
        await session.commit()


async def set_conversation(db, world, **fields) -> None:
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        for name, value in fields.items():
            setattr(conversation, name, value)
        await session.commit()


async def get_conversation(db, world) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, world.conversation.id)


async def add_appointment(
    db, world, *, start_at: datetime, status: AppointmentStatus = AppointmentStatus.SCHEDULED
) -> Appointment:
    async with db() as session:
        appointment = Appointment(
            id=uuid4(),
            tenant_id=world.tenant.id,
            patient_id=world.patient.id,
            conversation_id=world.conversation.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=status,
        )
        session.add(appointment)
        await session.commit()
        await session.refresh(appointment)
        return appointment


def _patient_ref(world) -> str:
    return world.patient.external_id if world.patient.wa_id is None else WA_ID


async def turn(db, world, body, *, interactive_reply_id=None, send: bool = True):
    """One inbound turn: route inside a transaction, then (by default) answer it."""
    channel = CHANNEL_BRAIN_MESSAGE if world.patient.wa_id is None else CHANNEL_WHATSAPP
    async with db() as session:
        async with session.begin():
            tenant = await session.get(Tenant, world.tenant.id)
            patient = await session.get(Patient, world.patient.id)
            reply = await _route_inbound_turn(
                session,
                tenant=tenant,
                patient=patient,
                patient_ref=_patient_ref(world),
                channel=channel,
                is_returning_patient=True,
                body=body,
                inbound_wam_id=None,
                interactive_reply_id=interactive_reply_id,
            )
    if send and reply is not None:
        await _send_bot_reply(reply)
    return reply


async def age_conversation(db, world, hours: float) -> None:
    """Pretend `hours` passed: every message and every reminder `sent_at` moves back."""
    delta = timedelta(hours=hours)
    async with db() as session:
        messages = await session.scalars(
            select(Message).where(Message.conversation_id == world.conversation.id)
        )
        for message in messages:
            message.created_at = _utc(message.created_at) - delta
        rows = await session.scalars(
            select(AppointmentReminder).where(AppointmentReminder.patient_id == world.patient.id)
        )
        for row in rows:
            if row.sent_at is not None:
                row.sent_at = _utc(row.sent_at) - delta
        await session.commit()


async def reminder_rows(db, appointment_id) -> list[AppointmentReminder]:
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder).where(
                    AppointmentReminder.appointment_id == appointment_id
                )
            )
        )


async def chat_rows(db, appointment_id) -> list[AppointmentReminder]:
    return [row for row in await reminder_rows(db, appointment_id) if row.kind == "chat"]
