"""Wiring shared by the TASK-032 R7 hub tests (status, confirm, attended, edit, cancel).

Import the fixtures by name - `from tests._r7_support import acting, r7  # noqa: F401` -
and the database fixture from `tests._reminder_fixtures`. `r7` is autouse: it overrides
the hub dependencies, points every worker module and `core.database` at the in-memory
DB (R3's `wire`), installs the WhatsApp double on the hub's sender module and a Google
Calendar double on the hub route, and records the Portal e-mails and usage events.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from secretaria.api.hub import calendar as hub_calendar  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentReminder,
    Professional,
    Tenant,
)
from secretaria.services import staff_patient_message as spm  # noqa: E402
from secretaria.services.email import EmailOutcome  # noqa: E402
from secretaria.services.tenant_config import set_google_refresh_token  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from tests._reminders_r3 import wire as wire_workers  # noqa: E402
from tests._reminders_v2 import WA_ID, FakeWhatsAppClient, fake_waba_token, seed_world  # noqa: E402

CALENDAR = "/tenants/me/calendar"


def soon(days: int = 3) -> datetime:
    """A start in the future, on a whole minute (the agenda never has seconds)."""
    return (datetime.now(UTC) + timedelta(days=days)).replace(second=0, microsecond=0)


def recent(hours: float = 1.0) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


class FakeCalendar:
    """Google Calendar double for the hub route: one instance per agenda label."""

    tzinfo = ZoneInfo("America/Sao_Paulo")
    instances: dict[str, "FakeCalendar"] = {}

    def __init__(self, label: str = "tenant") -> None:
        self.label = label
        self.free = True
        self.fail: Exception | None = None
        self.updated: list[tuple] = []
        self.created: list[tuple] = []
        self.cancelled: list[str] = []
        self.checked: list[tuple] = []
        self.during = None  # optional async callable run inside update_event_details

    @classmethod
    def get(cls, label: str) -> "FakeCalendar":
        return cls.instances.setdefault(label, cls(label))

    @classmethod
    def reset(cls) -> None:
        cls.instances = {}

    @classmethod
    def from_tenant_config(cls, config) -> "FakeCalendar":
        return cls.get("tenant")

    def references_same_calendar(self, other) -> bool:
        return getattr(other, "label", None) == self.label

    def _gate(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def is_slot_free(self, start, end, *, ignore_event_id=None) -> bool:
        self._gate()
        self.checked.append((start, end, ignore_event_id))
        return self.free

    async def update_event_details(self, event_id, start, end, summary, description="") -> dict:
        self._gate()
        self.updated.append((event_id, start, end, summary, description))
        if self.during is not None:
            during, self.during = self.during, None  # once: an undo must not re-trigger it
            await during()
        return {"id": event_id}

    async def create_event(self, start, end, summary, description="", reminders=None) -> dict:
        self._gate()
        self.created.append((start, end, summary, description))
        return {
            "id": f"evt-new-{self.label}",
            "htmlLink": f"https://calendar.google.com/{self.label}",
        }

    async def cancel_event(self, event_id) -> None:
        self._gate()
        self.cancelled.append(event_id)

    async def update_event(self, event_id, start, end) -> dict:
        self._gate()
        return {"id": event_id}

    async def check_availability(self, start, end) -> list:
        return []


class FakeArqPool:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    async def enqueue_job(self, name: str, *args, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.calls.append((name, *args))


def install_pool(pool) -> None:
    from secretaria.main import app

    app.state.arq_pool = pool


@pytest.fixture
def acting() -> dict:
    return {"tenant_id": None}


@pytest.fixture(autouse=True)
def r7(db, acting, monkeypatch: pytest.MonkeyPatch):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, acting["tenant_id"])

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    app.state.arq_pool = None
    wire_workers(monkeypatch, db)  # core.database + worker modules + WhatsApp double (reset)
    FakeCalendar.reset()
    monkeypatch.setattr(hub_calendar, "CalendarService", FakeCalendar)

    async def _resolve(session, tenant, professional, *, tenant_config=None, **_):
        return FakeCalendar.get(str(professional.id))

    monkeypatch.setattr(hub_calendar, "resolve_professional_calendar", _resolve)
    monkeypatch.setattr(spm, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(spm, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(spm, "portal_conversation_link", lambda tenant_id: "https://portal/x")
    doubles: dict[str, list] = {"mail": [], "usage": []}

    async def _send(to, template, variables) -> EmailOutcome:
        doubles["mail"].append((to, template, variables))
        return EmailOutcome.SENT

    async def _emit(**kwargs) -> bool:
        doubles["usage"].append(kwargs)
        return True

    monkeypatch.setattr(spm, "send_transactional_email_result", _send)
    monkeypatch.setattr(spm, "emit_usage_event", _emit)
    yield doubles
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.state.arq_pool = None


async def setup_world(db, acting, **kwargs):
    """`seed_world` with a future start and a recent inbound by default, acting as its clinic."""
    kwargs.setdefault("start_at", soon())
    kwargs.setdefault("last_inbound_at", recent())
    world = await seed_world(db, **kwargs)
    acting["tenant_id"] = world.tenant.id
    async with db() as session:
        await set_google_refresh_token(session, world.tenant.id, "fake-refresh-token")
        await session.commit()
    return world


async def set_tenant(db, tenant_id, **fields) -> None:
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        for name, value in fields.items():
            setattr(tenant, name, value)
        await session.commit()


async def set_appointment(db, appointment_id, **fields) -> None:
    async with db() as session:
        appointment = await session.get(Appointment, appointment_id)
        for name, value in fields.items():
            setattr(appointment, name, value)
        await session.commit()


async def add_professional(db, tenant_id, name: str, *, active: bool = True) -> UUID:
    async with db() as session:
        professional = Professional(id=uuid4(), tenant_id=tenant_id, name=name, is_active=active)
        session.add(professional)
        await session.commit()
        return professional.id


async def staff_rows(db, appointment_id, kind: str) -> list[AppointmentReminder]:
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder)
                .where(
                    AppointmentReminder.appointment_id == appointment_id,
                    AppointmentReminder.kind == kind,
                )
                .order_by(AppointmentReminder.created_at)
            )
        )


def sent() -> list[tuple]:
    return FakeWhatsAppClient.all_sent()


async def patch_status(client, appointment_id, status_value: str, **extra):
    return await client.patch(
        f"{CALENDAR}/appointments/{appointment_id}/status",
        json={"status": status_value, **extra},
    )


async def tap(world, action: str, reminder_id) -> None:
    """The patient taps a button of a card (WhatsApp; a Portal tap is the same handler)."""
    reply = tasks._ReplyContext(
        channel="whatsapp",
        conversation_id=world.conversation.id,
        patient_ref=world.patient.wa_id or WA_ID,
        inbound_body=action,
    )
    await tasks._handle_action_button(reply, action, str(reminder_id))
