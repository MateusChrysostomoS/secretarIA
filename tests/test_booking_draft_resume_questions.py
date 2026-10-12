"""The answer to a convênio, doctor or service question resumes the AI draft (TASK-030 P3).

End to end through the worker: the AI's draft is handed back (`_handle_set_booking_draft`,
P2b) on a switched-on clinic, the resolver lands on a booking question and parks the rest
in `flow_draft`, and the patient's answer - a real WhatsApp turn - re-runs the resolver
over it. Harness: tests/test_booking_draft_continuation.py (P2a).
"""

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
import json  # noqa: E402
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

from secretaria.ai.graph import BOOKING_DRAFT_SENTINEL_PREFIX  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Conversation, FlowState, Professional, Tenant  # noqa: E402
from secretaria.services import booking_hold as booking_hold_service  # noqa: E402
from secretaria.services.booking_draft import BookingDraft, draft_record  # noqa: E402
from secretaria.services.channel_sender import CHANNEL_WHATSAPP  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_DAY,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_PROFESSIONAL,
    STEP_AWAITING_SERVICE,
)
from secretaria.services.greeting_template import CONSENT_BUTTON_LABEL  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import draft_resolution  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
# Inside the 20-day window whatever the hour the suite runs at.
DAY = (datetime.now(TZ) + timedelta(days=3)).date()
WHEN = f"{DAY.strftime('%d/%m/%Y')} às 10:00"
EVERY_DAY = {
    day: [{"start": "08:00", "end": "18:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}
FULL_DRAFT = {"t": "Primeira Consulta", "w": "self", "d": DAY.isoformat(), "h": "10:00"}


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
    """Every doctor's agenda: 10:00 and 10:30 free on DAY."""

    def __init__(self):
        self.tzinfo = TZ

    async def list_available_days(self, start_day, days, slot_minutes=None):
        return [datetime(DAY.year, DAY.month, DAY.day, tzinfo=TZ)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if day.date() != DAY:
            return []
        iso = DAY.isoformat()
        return [{"start": f"{iso}T{t}", "end": "", "label": t} for t in ("10:00", "10:30")][
            :max_slots
        ]


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, db):
    agenda = _Agenda()
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
        return agenda

    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _fake_resolve)
    monkeypatch.setattr(workers_ns, "get_waba_token", _fake_token)
    monkeypatch.setattr(workers_ns, "get_entitlements", _fake_entitlements)
    monkeypatch.setattr(workers_ns, "_appointment_calendar", _agenda_for)
    # The turn's own calendar (the route() half of an answer) is the same fake agenda.
    monkeypatch.setattr(workers_ns, "_flow_turn_calendar", lambda conv, config, cal: agenda)
    return db


async def _seed_tenant(db, *, collect=False, doctors=()) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            clinic_description="Oftalmologia.",
            initial_flows={"ai_draft_v2": True},
            appointment_types=[
                {"name": "Primeira Consulta", "duration_min": 30, "is_active": True}
            ],
            business_hours=EVERY_DAY,
            collect_insurance=collect,
            insurance_mode="shared" if collect else None,
            insurances=["Unimed"] if collect else None,
        )
        session.add(tenant)
        await session.flush()
        for name, services in doctors:
            session.add(
                Professional(
                    tenant_id=tenant.id,
                    name=name,
                    is_active=True,
                    appointment_types=[
                        {"name": service, "duration_min": 30, "is_active": True}
                        for service in services
                    ],
                )
            )
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


async def _professional(db, tenant, name) -> Professional:
    async with db() as session:
        return await session.scalar(
            select(Professional).where(
                Professional.tenant_id == tenant.id, Professional.name == name
            )
        )


async def _hand_back(db, tenant, payload: dict) -> None:
    conversation = await _conversation(db, tenant)
    reply = tasks._ReplyContext(
        channel=CHANNEL_WHATSAPP,
        conversation_id=conversation.id,
        patient_ref=WA_ID,
        inbound_body="quinta às 10h, pra mim",
        tenant_id=tenant.id,
    )
    await tasks._handle_set_booking_draft(
        reply,
        BOOKING_DRAFT_SENTINEL_PREFIX + json.dumps(payload),
        tenant,
        None,
        [],
        WA_ID,
        waba_token="t",
    )


async def _park(db, tenant, *, step, draft, saved_at, selected_type=None) -> None:
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            row = await session.get(Conversation, conversation.id)
            row.flow_state = FlowState.SERVICE_CATALOG
            row.flow_step = step
            row.flow_selected_type = selected_type
            row.flow_attendee_name = ""
            row.flow_draft = draft_record(draft, saved_at=saved_at)


async def test_the_convenio_answer_lands_the_parked_draft_on_the_express_card(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db, collect=True)
    await _onboard(tenant)
    await _hand_back(db, tenant, FULL_DRAFT)
    parked = await _conversation(db, tenant)
    assert parked.flow_step == STEP_AWAITING_INSURANCE
    assert parked.flow_draft["d"] == DAY.isoformat()

    await _wa_turn(tenant, "Unimed")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_CONFIRMATION
    assert conversation.flow_selected_insurance == "Unimed"
    assert conversation.flow_selected_slot == f"{DAY.isoformat()}T10:00"
    assert conversation.flow_draft is None  # consumed
    (details_kind, details), (card_kind, card) = _WireClient.sends[-2:]
    assert (details_kind, card_kind) == ("text", "buttons")
    assert "Convênio: Unimed" in details
    assert card == f"Primeira Consulta\n{WHEN}"


async def test_the_doctor_answer_lands_the_parked_draft_with_that_doctor(wired) -> None:
    db = wired
    tenant = await _seed_tenant(
        db, doctors=[("Dra. Ana", ["Primeira Consulta"]), ("Dr. Beto", ["Primeira Consulta"])]
    )
    await _onboard(tenant)
    await _hand_back(db, tenant, FULL_DRAFT)
    assert (await _conversation(db, tenant)).flow_step == STEP_AWAITING_PROFESSIONAL

    await _wa_turn(tenant, "Dr. Beto")

    conversation = await _conversation(db, tenant)
    beto = await _professional(db, tenant, "Dr. Beto")
    assert conversation.flow_step == STEP_AWAITING_CONFIRMATION
    assert conversation.flow_selected_professional_id == beto.id
    assert _WireClient.sends[-1] == (
        "buttons",
        f"Primeira Consulta\nProfissional: Dr. Beto\n{WHEN}",
    )


async def test_a_doctor_who_does_not_offer_the_parked_service_keeps_the_day_waiting(
    wired, monkeypatch
) -> None:
    log = _Log()
    monkeypatch.setattr(draft_resolution, "logger", log)
    db = wired
    tenant = await _seed_tenant(
        db,
        doctors=[
            ("Dra. Ana", ["Primeira Consulta", "Retorno"]),
            ("Dr. Beto", ["Primeira Consulta"]),
            ("Dr. Caio", ["Retorno"]),
        ],
    )
    await _onboard(tenant)
    await _hand_back(db, tenant, FULL_DRAFT)

    await _wa_turn(tenant, "Dr. Caio")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SERVICE  # Dr. Caio's own services
    assert conversation.flow_draft["d"] == DAY.isoformat()  # the day still waits
    (resumed,) = [fields for event, fields in log.events if event == "booking_draft_resumed"]
    assert resumed["answered_step"] == STEP_AWAITING_PROFESSIONAL
    assert resumed["dropped"] == {"service": "not_offered_by_professional"}

    await _wa_turn(tenant, "Retorno")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_CONFIRMATION
    assert _WireClient.sends[-1] == ("buttons", f"Retorno\nProfissional: Dr. Caio\n{WHEN}")


async def test_a_draft_expired_while_waiting_for_the_convenio_answer_continues_the_buttons(
    wired, monkeypatch
) -> None:
    log = _Log()
    monkeypatch.setattr(draft_resolution, "logger", log)
    db = wired
    tenant = await _seed_tenant(db, collect=True)
    await _onboard(tenant)
    await _park(
        db,
        tenant,
        step=STEP_AWAITING_INSURANCE,
        draft=BookingDraft(
            service="Primeira Consulta", attendee="self", day=DAY, time=dt.time(10, 0)
        ),
        saved_at=datetime.now(UTC) - timedelta(minutes=31),
        selected_type="Primeira Consulta",
    )

    await _wa_turn(tenant, "Unimed")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_DAY  # the plain next question
    assert conversation.flow_selected_insurance == "Unimed"
    assert conversation.flow_draft is None
    skipped = [fields for event, fields in log.events if event == "booking_draft_resume_skipped"]
    assert [fields["reason"] for fields in skipped] == ["expired_or_invalid"]


async def test_the_menu_drops_a_parked_draft(wired) -> None:
    db = wired
    tenant = await _seed_tenant(db, collect=True)
    await _onboard(tenant)
    await _park(
        db,
        tenant,
        step=STEP_AWAITING_INSURANCE,
        draft=BookingDraft(service="Primeira Consulta", attendee="self", day=DAY),
        saved_at=datetime.now(UTC),
        selected_type="Primeira Consulta",
    )
    conversation = await _conversation(db, tenant)
    reply = tasks._ReplyContext(
        channel=CHANNEL_WHATSAPP,
        conversation_id=conversation.id,
        patient_ref=WA_ID,
        inbound_body="/menu",
        tenant_id=tenant.id,
    )

    await tasks._handle_show_main_menu(reply, tenant, [], WA_ID, waba_token="t", source="command")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_state == FlowState.MENU
    assert conversation.flow_draft is None


async def test_full_agenda_after_insurance_answer_keeps_no_free_days_reply(wired, monkeypatch):
    from secretaria.services import flow_router as fr

    db = wired
    log = _Log()
    monkeypatch.setattr(draft_resolution, "logger", log)
    tenant = await _seed_tenant(db, collect=True)
    await _onboard(tenant)
    await _hand_back(db, tenant, FULL_DRAFT)
    assert (await _conversation(db, tenant)).flow_step == STEP_AWAITING_INSURANCE

    async def no_days(self, start_day, days, slot_minutes=None):
        return []

    async def no_slots(self, day, slot_minutes=None, max_slots=6):
        return []

    monkeypatch.setattr(_Agenda, "list_available_days", no_days)
    monkeypatch.setattr(_Agenda, "list_free_slots", no_slots)
    await _wa_turn(tenant, "Unimed")

    conversation = await _conversation(db, tenant)
    assert conversation.flow_state == FlowState.MENU
    assert conversation.flow_step is None
    assert conversation.flow_draft is None
    assert ("text", fr.NO_AVAILABLE_DAYS_MESSAGE) in _WireClient.sends[-2:]
    assert not [fields for event, fields in log.events if event == "booking_draft_resumed"]
