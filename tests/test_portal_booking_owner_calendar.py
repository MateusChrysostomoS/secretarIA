"""A gated Portal booking lives on the OWNING doctor's calendar (live bug, 2026-10-05).

Reproduced in production at a multi-doctor clinic: `_promote_booking_hold` resolved
the owner against the flow router's SimpleNamespace roster, `_appointment_calendar`
only recognises a real `Professional`, and the event fell through to the CLINIC
calendar. Two visible consequences, both proven live:

  - double booking: the doctor's own agenda still showed the slot as free;
  - the reschedule (which acts on the doctor's agenda) got a Google 404, the raw
    HttpError escaped to the runner, the LLM re-opened the day picker, and the
    patient looped on "✅ Confirmar" forever.

Everything here is real (orchestrator, turn_router, flow_runner, flow_router,
load_tenant_config, resolve_professional_calendar, _appointment_calendar) except
Google - an in-memory store PER calendarId that answers 404 to a patch on a
calendar that does not hold the event, exactly like Google - and the LLM, which
answers with the manage_existing_appointment hand-back it gives in production.
"""
import os

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import httplib2  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai.graph import MANAGE_APPOINTMENT_SENTINEL_PREFIX  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    Conversation,
    FlowState,
    HandoverState,
    Message,
    MessageDirection,
    Patient,
    Professional,
    Tenant,
)
from secretaria.services import booking_hold as holds  # noqa: E402
from secretaria.services.calendar import CalendarService  # noqa: E402
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.pending_identity import VerifyOutcome, VerifyResult  # noqa: E402
from secretaria.services.tenant_config import set_google_refresh_token  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared.llm_context import _appointment_calendar  # noqa: E402
from secretaria.workers.shared.text import CALENDAR_UNAVAILABLE_MESSAGE  # noqa: E402

EXTERNAL_ID = "00000000-0000-4000-8000-0000000000aa"
TZ = ZoneInfo("America/Sao_Paulo")


class FakeGoogle:
    """Events live INSIDE a calendarId, like on Google."""

    def __init__(self) -> None:
        self.calendars: dict[str | None, dict[str, tuple[datetime, datetime]]] = {}
        self.ops: list[tuple] = []
        self._n = 0

    def install(self, monkeypatch) -> None:
        google = self

        async def create_event(self, start, end, summary, description="", reminders=None):
            google._n += 1
            eid = f"evt-{google._n}"
            google.calendars.setdefault(self._calendar_id, {})[eid] = (start, end)
            google.ops.append(("create", self._calendar_id, eid))
            return {"id": eid, "htmlLink": f"https://calendar.google.com/{eid}"}

        async def update_event(self, event_id, start, end):
            google.ops.append(("patch", self._calendar_id, event_id))
            cal = google.calendars.get(self._calendar_id, {})
            if event_id not in cal:
                # googleapiclient raises exactly this for events.patch on a
                # calendar that does not contain the event.
                raise HttpError(httplib2.Response({"status": "404"}), b'{"error":"notFound"}')
            cal[event_id] = (start, end)
            return {"id": event_id}

        async def cancel_event(self, event_id):
            google.ops.append(("delete", self._calendar_id, event_id))
            # The real cancel_event treats 404/410 as success (idempotent).
            google.calendars.get(self._calendar_id, {}).pop(event_id, None)

        async def list_available_days(self, start_day, days, slot_minutes=None, **kw):
            base = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            return [base + timedelta(days=3), base + timedelta(days=4)]

        async def list_free_slots(self, day, slot_minutes=None, max_slots=6, **kw):
            start = day.replace(hour=14, minute=40, tzinfo=None)
            end = start + timedelta(minutes=slot_minutes or 30)
            return [
                {
                    "start": start.isoformat(timespec="minutes"),
                    "end": end.isoformat(timespec="minutes"),
                    "label": "14:40",
                }
            ]

        for name, fn in {
            "create_event": create_event,
            "update_event": update_event,
            "cancel_event": cancel_event,
            "list_available_days": list_available_days,
            "list_free_slots": list_free_slots,
        }.items():
            monkeypatch.setattr(CalendarService, name, fn)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
def google(monkeypatch) -> FakeGoogle:
    g = FakeGoogle()
    g.install(monkeypatch)
    return g


@pytest.fixture
def llm_calls() -> list[str]:
    return []


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db, llm_calls):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(holds, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))

    async def _none(*a, **k):
        return None

    async def _token(session, tenant_id):
        return "decrypted-waba-token"

    async def _ent(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id), status="active", active=True, secretaria_enabled=True,
            plan="bronze", secretaria_tier="basico", addons={}, limits={},
        )

    async def _verify(tenant_id, external_id, code):
        return VerifyResult(VerifyOutcome.VERIFIED)

    async def _run_agent(inbound_body, **kwargs):
        # What the model does in production on this turn: it sees the manage
        # flow, re-reads "remarcar" in the history and calls
        # manage_existing_appointment(action="reschedule").
        llm_calls.append(inbound_body)
        return MANAGE_APPOINTMENT_SENTINEL_PREFIX + "reschedule"

    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _none)
    monkeypatch.setattr(workers_ns, "get_waba_token", _token)
    monkeypatch.setattr(workers_ns, "get_entitlements", _ent)
    monkeypatch.setattr(workers_ns, "verify_code", _verify)
    monkeypatch.setattr(workers_ns, "enqueue_post_booking_hooks", _none)
    monkeypatch.setattr(workers_ns, "run_agent", _run_agent)
    yield


async def _seed(db):
    async with db() as s:
        tenant = Tenant(
            id=uuid4(), clinic_name="Clínica QA", phone_number_id="1234567890",
            is_active=True, timezone="America/Sao_Paulo", initial_flows={},
            google_calendar_id="clinic-cal", google_calendar_mode="shared_account",
        )
        s.add(tenant)
        await s.flush()
        dr_a = Professional(tenant_id=tenant.id, name="Dr. A", google_calendar_id="cal-dr-a")
        dr_b = Professional(tenant_id=tenant.id, name="Dr. B", google_calendar_id="cal-dr-b")
        s.add_all([dr_a, dr_b])
        patient = Patient(
            id=uuid4(), tenant_id=tenant.id, channel=CHANNEL_BRAIN_MESSAGE,
            external_id=EXTERNAL_ID, name="Maria", lgpd_accepted_at=datetime.now(UTC),
        )
        s.add(patient)
        await s.flush()
        conv = Conversation(
            id=uuid4(), tenant_id=tenant.id, patient_id=patient.id,
            flow_state=FlowState.AWAITING_EMAIL_CODE,
        )
        s.add(conv)
        # A CONNECTED clinic (the env-var token no longer stands in for a tenant's).
        await set_google_refresh_token(s, tenant.id, "clinic-token")
        await s.commit()
        for row in (tenant, dr_a, dr_b, patient, conv):
            await s.refresh(row)
        return tenant, dr_a, patient, conv


async def _outbound(db, conv) -> list[str]:
    async with db() as s:
        return list(
            (
                await s.scalars(
                    select(Message.body)
                    .where(
                        Message.conversation_id == conv.id,
                        Message.direction == MessageDirection.OUTBOUND,
                    )
                    .order_by(Message.created_at, text("messages.rowid"))
                )
            ).all()
        )


async def _portal(tenant, text_, reply_id=None):
    await tasks.process_brain_message_inbound(
        {}, str(tenant.id), EXTERNAL_ID, text=text_, interactive_reply_id=reply_id
    )


async def _book_via_portal_gate(db, tenant, dr_a, patient, conv):
    """The steps production ran: hold placed, code typed, hold promoted."""
    start_local = (datetime.now(TZ) + timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    held = await holds.place_hold(
        tenant_id=tenant.id, conversation_id=conv.id, patient_id=patient.id,
        professional_id=dr_a.id, appointment_type="Cirurgia de Catarata", insurance=None,
        start_at=start_local.astimezone(UTC),
        end_at=(start_local + timedelta(minutes=30)).astimezone(UTC),
    )
    assert held is not None
    # The REAL code turn: Portal pipeline -> orchestrator -> _promote_booking_hold.
    await _portal(tenant, "123456")
    async with db() as s:
        rows = list((await s.scalars(select(Appointment))).all())
    assert len(rows) == 1
    return rows[0]


async def _reschedule_to_1440(tenant):
    await _portal(tenant, "Remarcar")
    day = (datetime.now(TZ) + timedelta(days=3)).date()
    await _portal(tenant, f"🗓️ {day.strftime('%d/%m')}", f"day|{day.isoformat()}|0")
    slot = f"{day.isoformat()}T14:40"
    await _portal(tenant, "🗓️ 14:40", f"slot|{slot}")
    await _portal(tenant, "✅ Confirmar", "confirm_yes")
    return slot


@pytest.mark.asyncio
async def test_portal_promotion_books_on_the_owning_doctors_calendar(db, google):
    tenant, dr_a, patient, conv = await _seed(db)
    appt = await _book_via_portal_gate(db, tenant, dr_a, patient, conv)
    assert appt.professional_id == dr_a.id
    # Never the clinic calendar: that is the double booking.
    assert google.ops == [("create", "cal-dr-a", appt.google_event_id)]


@pytest.mark.asyncio
async def test_portal_promoted_appointment_can_be_rescheduled_end_to_end(db, google, llm_calls):
    tenant, dr_a, patient, conv = await _seed(db)
    appt = await _book_via_portal_gate(db, tenant, dr_a, patient, conv)

    slot = await _reschedule_to_1440(tenant)

    assert llm_calls == []
    assert (await _outbound(db, conv))[-1].startswith("Pronto! Sua consulta foi remarcada.")
    assert ("patch", "cal-dr-a", appt.google_event_id) in google.ops
    async with db() as s:
        row = await s.get(Appointment, appt.id)
    assert row.start_at.astimezone(TZ).strftime("%Y-%m-%dT%H:%M") == slot


@pytest.mark.asyncio
async def test_portal_promoted_appointment_cancel_removes_the_real_event(db, google, llm_calls):
    tenant, dr_a, patient, conv = await _seed(db)
    appt = await _book_via_portal_gate(db, tenant, dr_a, patient, conv)
    await _portal(tenant, "❌ Cancelar")
    await _portal(tenant, "✅ Sim", "menu|0")
    assert "Pronto! Sua consulta foi cancelada" in (await _outbound(db, conv))[-1]
    assert not any(appt.google_event_id in evs for evs in google.calendars.values())


@pytest.mark.asyncio
async def test_a_reschedule_google_refuses_goes_to_a_person_not_the_llm(db, google, llm_calls):
    """The event is gone from the owner's agenda (moved or deleted by hand, or a
    legacy booking on the clinic calendar): Google answers 404 to the patch.
    That used to escape as a raw HttpError, the runner delegated to the LLM and
    the patient looped on the day picker. Now it is the calendar-unavailable
    path: one honest message, the conversation handed to a person."""
    tenant, dr_a, patient, conv = await _seed(db)
    appt = await _book_via_portal_gate(db, tenant, dr_a, patient, conv)
    google.calendars["cal-dr-a"].pop(appt.google_event_id)

    await _reschedule_to_1440(tenant)

    assert llm_calls == []
    assert (await _outbound(db, conv))[-1] == CALENDAR_UNAVAILABLE_MESSAGE
    async with db() as s:
        row = await s.get(Conversation, conv.id)
    assert row.handover_state == HandoverState.HUMAN_ACTIVE


@pytest.mark.asyncio
async def test_appointment_calendar_fails_closed_on_a_roster_snapshot(db):
    """A SimpleNamespace copy of a professional must never resolve to the
    clinic calendar again - None ("unavailable") is the honest answer."""
    tenant, dr_a, _, _ = await _seed(db)
    snapshot = SimpleNamespace(id=dr_a.id, name=dr_a.name)
    async with db() as s:
        assert await _appointment_calendar(s, tenant, snapshot) is None
        assert await _appointment_calendar(s, tenant, "tenant") is not None
