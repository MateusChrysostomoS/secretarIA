"""The pra-quem answer resumes the AI draft parked in flow_draft (TASK-030 P2, spec §4.3)."""

import os

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import datetime as dt  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import ConsentEvent, Conversation, FlowState, Tenant  # noqa: E402
from secretaria.services import booking_hold as booking_hold_service  # noqa: E402
from secretaria.services.attendee import (  # noqa: E402
    ATTENDEE_SELF,
    CONSENT_KIND_THIRD_PARTY_BOOKING,
    LABEL_ATTENDEE_AUTH_CONFIRM,
    LABEL_ATTENDEE_SELF,
)
from secretaria.services.booking_draft import BookingDraft, draft_record  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    ATTENDEE_NEXT_BOOK,
    STEP_AWAITING_ATTENDEE_AUTH,
    STEP_AWAITING_ATTENDEE_CHOICE,
    STEP_AWAITING_SERVICE,
    STEP_AWAITING_SLOT,
)
from secretaria.services.greeting_template import CONSENT_BUTTON_LABEL  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import draft_resolution  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
# Inside the 20-day window whatever the hour the suite runs at.
DAY = (datetime.now(TZ) + timedelta(days=3)).date()


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


class _WireClient:
    sends: list[tuple] = []

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        _WireClient.sends.append(("text", body))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}

    async def send_buttons(self, to, body, buttons):
        _WireClient.sends.append(("buttons", body))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        _WireClient.sends.append(("list", body))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}


class _Agenda:
    """The clinic's agenda: free times per day."""

    def __init__(self, free):
        self.tzinfo = TZ
        self._free = free

    async def list_available_days(self, start_day, days, slot_minutes=None):
        return [datetime(d.year, d.month, d.day, tzinfo=TZ) for d in sorted(self._free)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        times = self._free.get(day.date(), [])[:max_slots]
        return [{"start": f"{day.date().isoformat()}T{t}", "end": "", "label": t} for t in times]


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(booking_hold_service, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    _WireClient.sends = []
    monkeypatch.setattr(workers_ns, "WhatsAppClient", _WireClient)

    async def _fake_resolve(session, tenant_id, patient_id, **kwargs):
        return None

    async def _fake_token(session, tenant_id):
        return "decrypted-waba-token"

    async def _fake_entitlements(tenant_id, redis):
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

    async def _agenda_for(session, tenant, target):
        return _Agenda({DAY: ["10:00", "10:40"]})

    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _fake_resolve)
    monkeypatch.setattr(workers_ns, "get_waba_token", _fake_token)
    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_entitlements)
    monkeypatch.setattr(workers_ns, "_appointment_calendar", _agenda_for)
    return db


async def _seed_tenant(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            clinic_description="Oftalmologia.",
            initial_flows={},
            appointment_types=[
                {"name": "Primeira Consulta", "duration_min": 40, "is_active": True}
            ],
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


_wam_seq = iter(range(1, 10_000))


async def _wa_turn(tenant: Tenant, body: str):
    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Perfil",
        wam_id=f"wamid.in.{next(_wam_seq)}",
        body=body,
    )
    if reply is not None:
        await tasks._send_bot_reply(reply, redis=None)


async def _onboard(tenant) -> None:
    await _wa_turn(tenant, "oi")
    await _wa_turn(tenant, "joão conta")
    await _wa_turn(tenant, CONSENT_BUTTON_LABEL)


async def _conversation(db, tenant) -> Conversation:
    async with db() as session:
        return await session.scalar(select(Conversation).where(Conversation.tenant_id == tenant.id))


async def _consents(db, tenant) -> int:
    async with db() as session:
        return await session.scalar(
            select(func.count())
            .select_from(ConsentEvent)
            .where(
                ConsentEvent.tenant_id == tenant.id,
                ConsentEvent.kind == CONSENT_KIND_THIRD_PARTY_BOOKING,
            )
        )


async def _park(db, tenant, *, step, attendee, draft, saved_at=None) -> None:
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            row = await session.get(Conversation, conversation.id)
            row.flow_state = FlowState.SERVICE_CATALOG
            row.flow_step = step
            row.flow_selected_type = ATTENDEE_NEXT_BOOK
            row.flow_attendee_name = attendee
            row.flow_draft = draft_record(draft, saved_at=saved_at or datetime.now(UTC))


_FULL_OTHER = BookingDraft(
    service="Primeira Consulta", attendee="other", day=DAY, time=dt.time(10, 0)
)


async def test_authorizing_a_third_party_lands_the_parked_draft_on_its_slot_list(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _park(
        db, tenant, step=STEP_AWAITING_ATTENDEE_AUTH, attendee="Maria da Silva", draft=_FULL_OTHER
    )

    await _wa_turn(tenant, LABEL_ATTENDEE_AUTH_CONFIRM)

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SLOT
    assert conversation.flow_selected_type == "Primeira Consulta"
    assert conversation.flow_selected_day == DAY.isoformat()
    assert conversation.flow_attendee_name == "Maria da Silva"
    assert conversation.flow_draft is None  # consumed
    assert await _consents(db, tenant) == 1  # the authorization is still recorded, once
    kind, body = _WireClient.sends[-1]
    assert kind == "list"
    assert body.startswith(f"Horários livres em {DAY.strftime('%d/%m')}")


async def test_answering_pra_mim_overrides_a_parked_other(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _park(db, tenant, step=STEP_AWAITING_ATTENDEE_CHOICE, attendee=None, draft=_FULL_OTHER)

    await _wa_turn(tenant, LABEL_ATTENDEE_SELF)

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SLOT  # no name is asked
    assert conversation.flow_attendee_name == ATTENDEE_SELF
    assert await _consents(db, tenant) == 0


async def test_a_draft_older_than_30_minutes_is_not_resumed(wired, monkeypatch) -> None:
    log = _Log()
    monkeypatch.setattr(draft_resolution, "logger", log)
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _park(
        db,
        tenant,
        step=STEP_AWAITING_ATTENDEE_AUTH,
        attendee="Maria da Silva",
        draft=_FULL_OTHER,
        saved_at=datetime.now(UTC) - timedelta(minutes=31),
    )

    await _wa_turn(tenant, LABEL_ATTENDEE_AUTH_CONFIRM)

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SERVICE  # the plain next question
    assert conversation.flow_draft is None
    assert await _consents(db, tenant) == 1
    skipped = [f for e, f in log.events if e == "booking_draft_resume_skipped"]
    assert [f["reason"] for f in skipped] == ["expired_or_invalid"]


async def test_a_resolver_failure_falls_back_to_the_plain_next_question(wired, monkeypatch) -> None:
    log = _Log()
    monkeypatch.setattr(draft_resolution, "logger", log)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("resolver bug")

    monkeypatch.setattr(draft_resolution, "resolve_booking_draft", _boom)
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _park(
        db, tenant, step=STEP_AWAITING_ATTENDEE_AUTH, attendee="Maria da Silva", draft=_FULL_OTHER
    )

    await _wa_turn(tenant, LABEL_ATTENDEE_AUTH_CONFIRM)

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SERVICE
    assert conversation.flow_draft is None
    assert [f["error_type"] for e, f in log.events if e == "booking_draft_resume_failed"] == [
        "RuntimeError"
    ]


async def test_a_conversation_of_another_tenant_is_never_loaded(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    conversation = await _conversation(db, tenant)
    stranger = Tenant(id=uuid4(), clinic_name="Outra", phone_number_id="999", is_active=True)
    reply = tasks._ReplyContext(
        conversation_id=conversation.id, patient_ref=WA_ID, inbound_body="x"
    )
    assert await draft_resolution._load_draft_context(reply, stranger) is None


async def test_draft_context_reloads_clinic_settings_instead_of_using_the_turn_snapshot(wired):
    db = wired
    stale_tenant = await _seed_tenant(db)
    await _onboard(stale_tenant)
    conversation = await _conversation(db, stale_tenant)
    async with db() as session:
        fresh = await session.get(Tenant, stale_tenant.id)
        fresh.appointment_types = [{"name": "Serviço novo", "duration_min": 20, "is_active": True}]
        fresh.collect_insurance = True
        fresh.insurance_mode = "shared"
        fresh.insurances = ["Plano novo"]
        await session.commit()
    reply = tasks._ReplyContext(
        conversation_id=conversation.id, patient_ref=WA_ID, inbound_body="x"
    )
    ctx = await draft_resolution._load_draft_context(reply, stale_tenant)
    assert [item["name"] for item in ctx.tenant_snapshot.appointment_types] == ["Serviço novo"]
    assert ctx.tenant_snapshot.collect_insurance is True
    assert ctx.tenant_snapshot.insurance_mode == "shared"
    assert ctx.tenant_snapshot.insurances == ["Plano novo"]


async def test_lazy_draft_calendar_uses_the_reloaded_clinic_settings(wired, monkeypatch):
    db = wired
    stale_tenant = await _seed_tenant(db)
    await _onboard(stale_tenant)
    conversation = await _conversation(db, stale_tenant)
    async with db() as session:
        fresh = await session.get(Tenant, stale_tenant.id)
        fresh.google_calendar_id = "updated-test-calendar"
        await session.commit()
    reply = tasks._ReplyContext(
        conversation_id=conversation.id, patient_ref=WA_ID, inbound_body="x"
    )
    ctx = await draft_resolution._load_draft_context(reply, stale_tenant)

    async def build_agenda(session, tenant, target):
        assert target == "tenant"
        if tenant.google_calendar_id == "updated-test-calendar":
            return _Agenda({DAY: ["11:20"]})
        return _Agenda({DAY: ["10:00"]})

    monkeypatch.setattr(draft_resolution, "_appointment_calendar", build_agenda)
    agenda = await draft_resolution._draft_calendar_source(stale_tenant, ctx)(None)
    slots = await agenda.list_free_slots(datetime(DAY.year, DAY.month, DAY.day))
    assert [slot["label"] for slot in slots] == ["11:20"]
