"""Seeds and fakes shared by the TASK-032 R2 reminder-engine tests.

The in-memory database fixture is R1's (`tests/_reminder_fixtures.py::db`,
imported by each test module). This module only adds plain builders and a
recording WhatsApp double, so every test file stays explicit about what it
patches.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from dataclasses import dataclass  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

from sqlalchemy import select  # noqa: E402

from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentReminder,
    AppointmentStatus,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Professional,
    Tenant,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, CHANNEL_WHATSAPP  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.whatsapp import TenantWhatsAppCredentialMissing  # noqa: E402

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
WA_ID = "5511988887777"


class FakeWhatsAppClient:
    """Records every send; installed in place of `WhatsAppClient`.

    `failing_templates` makes `send_template` refuse those names (a template
    Meta has not approved); `fail_everything` makes every send raise (Meta down).
    """

    persists_outbound = False
    created: list["FakeWhatsAppClient"] = []
    failing_templates: set[str] = set()
    fail_everything: bool = False

    def __init__(self, tenant_id=None) -> None:
        self.tenant_id = tenant_id
        self.sent: list[tuple] = []
        FakeWhatsAppClient.created.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.created = []
        cls.failing_templates = set()
        cls.fail_everything = False

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        if not (tenant.phone_number_id or "").strip() or not (waba_token or "").strip():
            raise TenantWhatsAppCredentialMissing(tenant.id, ("access_token",))
        return cls(tenant_id=tenant.id)

    @classmethod
    def all_sent(cls) -> list[tuple]:
        return [item for client in cls.created for item in client.sent]

    def _gate(self) -> None:
        if FakeWhatsAppClient.fail_everything:
            raise RuntimeError("meta unavailable")

    async def send_text_message(self, to, body):
        self._gate()
        self.sent.append(("text", to, body))
        return {"messages": [{"id": "wamid.text"}]}

    async def send_buttons(self, to, body, buttons):
        self._gate()
        self.sent.append(("buttons", to, body, list(buttons)))
        return {"messages": [{"id": "wamid.buttons"}]}

    async def send_template(self, to, template, lang, variables, button_payloads=None):
        self._gate()
        if template in FakeWhatsAppClient.failing_templates:
            raise RuntimeError("template not approved")
        self.sent.append(("template", to, template, lang, list(variables), button_payloads))
        return {"messages": [{"id": "wamid.template"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        self._gate()
        self.sent.append(("list", to, body, button_label, rows, section_title))
        return {"messages": [{"id": "wamid.list"}]}


async def fake_waba_token(session, tenant_id) -> str:
    return "decrypted-waba-token"


async def entitled(tenant_id, redis) -> EntitlementSummary:
    return EntitlementSummary(
        tenant_id=str(tenant_id),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons={},
        limits={},
    )


@dataclass
class World:
    tenant: Tenant
    patient: Patient
    conversation: Conversation
    appointment: Appointment
    start_at: datetime  # the appointment start, tz-aware UTC (SQLite reads it back naive)


async def seed_world(
    db,
    *,
    v2: bool = True,
    channel: str = CHANNEL_WHATSAPP,
    wa_id: str | None = WA_ID,
    email: str | None = None,
    opt_out: bool = False,
    start_at: datetime | None = None,
    status: AppointmentStatus = AppointmentStatus.SCHEDULED,
    attendee_name: str | None = None,
    timezone: str = "America/Sao_Paulo",
    requirements: list[str] | None = None,
    confirmation_count: int = 0,
    professional_name: str | None = None,
    last_inbound_at: datetime | None = None,
    phone_number_id: str | None = "pnid-1",
    google_event_id: str | None = None,
) -> World:
    """One clinic, one patient, one conversation, one appointment."""
    start = start_at or NOW + timedelta(days=3)
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica Olhar",
            phone_number_id=phone_number_id,
            is_active=True,
            timezone=timezone,
            reminders_v2_enabled=v2,
            appointment_types=[
                {"name": "Consulta", "is_active": True, "requirements": list(requirements or [])}
            ],
        )
        session.add(tenant)
        await session.flush()
        professional_id = None
        if professional_name:
            professional = Professional(id=uuid4(), tenant_id=tenant.id, name=professional_name)
            session.add(professional)
            await session.flush()
            professional_id = professional.id
        if channel == CHANNEL_BRAIN_MESSAGE:
            patient = Patient(
                id=uuid4(),
                tenant_id=tenant.id,
                wa_id=None,
                channel=CHANNEL_BRAIN_MESSAGE,
                external_id=str(uuid4()),
                name="Maria",
                email=email,
                reminder_opt_out=opt_out,
            )
        else:
            patient = Patient(
                id=uuid4(),
                tenant_id=tenant.id,
                wa_id=wa_id,
                name="Maria",
                email=email,
                reminder_opt_out=opt_out,
            )
        session.add(patient)
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        if last_inbound_at is not None:
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND,
                    sender=MessageSender.PATIENT,
                    body="oi",
                    created_at=last_inbound_at,
                )
            )
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            conversation_id=conversation.id,
            google_event_id=google_event_id if google_event_id is not None else f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            status=status,
            professional_id=professional_id,
            attendee_name=attendee_name,
            confirmation_count=confirmation_count,
        )
        session.add(appointment)
        await session.commit()
        for row in (tenant, patient, conversation, appointment):
            await session.refresh(row)
        return World(tenant, patient, conversation, appointment, start)


async def add_reminder(
    db,
    world: World,
    *,
    kind: str = "day",
    due_at: datetime | None = None,
    status: str = "pending",
    with_prompt: bool = True,
    attempts: int = 0,
    appointment_start_at: datetime | None = None,
) -> UUID:
    """A planned row, as R1's `schedule_reminders` would have written it."""
    due = due_at or NOW - timedelta(minutes=1)
    async with db() as session:
        row = AppointmentReminder(
            id=uuid4(),
            tenant_id=world.tenant.id,
            appointment_id=world.appointment.id,
            patient_id=world.patient.id,
            kind=kind,
            appointment_start_at=appointment_start_at or world.start_at,
            due_at=due,
            status=status,
            channel="whatsapp",
            with_prompt=with_prompt,
            attempts=attempts,
            warn_due_at=due + timedelta(hours=2),
        )
        session.add(row)
        await session.commit()
        return row.id


async def get_reminder(db, reminder_id: UUID) -> AppointmentReminder:
    async with db() as session:
        return await session.get(AppointmentReminder, reminder_id)


async def reload_appointment(db, appointment_id: UUID) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def outbound_messages(db, conversation_id: UUID) -> list[Message]:
    async with db() as session:
        rows = await session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.OUTBOUND,
            )
            .order_by(Message.created_at)
        )
        return list(rows)


async def seed_paid_deposit(db, world: World) -> None:
    async with db() as session:
        session.add(
            PixDeposit(
                id=uuid4(),
                tenant_id=world.tenant.id,
                appointment_id=world.appointment.id,
                patient_id=world.patient.id,
                asaas_payment_id=f"pay-{uuid4()}",
                amount_cents=10000,
                percent_applied=30,
                status=PixDepositStatus.PAID,
            )
        )
        await session.commit()


class HookSpy:
    """Stands in for services/reminder_hooks.py's three hooks and records the calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def install(self, monkeypatch, hooks_module) -> "HookSpy":
        async def booked(appointment_id, *, now=None):
            self.calls.append(("booked", appointment_id))
            return 0

        async def moved(appointment_id, *, now=None):
            self.calls.append(("moved", appointment_id))
            return 0

        async def closed(appointment_id, *, reason):
            self.calls.append(("closed", appointment_id, reason))
            return 0

        monkeypatch.setattr(hooks_module, "after_appointment_booked", booked)
        monkeypatch.setattr(hooks_module, "after_appointment_rescheduled", moved)
        monkeypatch.setattr(hooks_module, "after_appointment_closed", closed)
        return self
