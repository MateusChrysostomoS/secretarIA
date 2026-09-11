"""A tapped row's id is routing data, never display text - end to end.

Found live on 2026-09-10 in the staff console: a patient tapped a doctor in the
"Com qual profissional você gostaria de agendar?" list and her reply showed up
as "Dr. Fulano (8faa12e1-…)". A data-carrying row's id (`_PAYLOAD_ROW_PREFIXES`,
schemas/webhook.py) used to be folded into the message text on purpose - the
flow router and the agent read the payload back out of it - and that same
string was `Message.body`, i.e. exactly what the console and the patient portal
render.

The split pinned here, through the real webhook entry point
(`workers/tasks.py::_handle_patient_messages`) and a real (in-memory) DB:

  * `Message.body` holds what the patient SAW - the row title - and nothing
    else; `Message.interactive_reply_id` holds the raw row id;
  * the booking path still resolves the exact row that was tapped: the router
    reads `inbound_routing_text` (the old string, recomposed in memory), and the
    agent reads the same recomposition out of its history.

The assertions come in pairs on purpose. A fix that only did the first half
would pass every display check and quietly stop booking the doctor the patient
picked - the router would be left matching a truncated title.

DB tests use the in-memory-sqlite-on-StaticPool pattern from
test_brain_message_pipeline.py / test_bot_reply_gating.py.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import graph, scoped_help  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Professional,
    Tenant,
)
from secretaria.schemas.webhook import (  # noqa: E402
    WebhookMessage,
    WebhookValue,
    extract_inbound_body,
    extract_inbound_reply_id,
    inbound_routing_text,
)
from secretaria.services import flow_router  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
_TZ = ZoneInfo("America/Sao_Paulo")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")

# Two doctors whose list rows render IDENTICALLY: past the row-title cap only
# the shared prefix survives, so on screen only the row id tells them apart.
_TWIN_NAMES = (
    "Dra. Ana Beatriz Figueiredo Albuquerque Souza",
    "Dra. Ana Beatriz Figueiredo Albuquerque Lopes",
)


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


class _RecordingWhatsAppClient:
    """Stands in for WhatsAppClient: records every send, reaches nothing."""

    # Mirrors WhatsAppClient: the CALLER records the outbound row.
    persists_outbound = False
    created: list["_RecordingWhatsAppClient"] = []

    def __init__(self, access_token=None, phone_number_id=None):
        self.sent: list[tuple] = []
        _RecordingWhatsAppClient.created.append(self)

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls(access_token=waba_token, phone_number_id=tenant.phone_number_id)

    async def send_text_message(self, to, body):
        self.sent.append(("text", to, body))
        return {"messages": [{"id": f"wamid.out.{uuid4()}"}]}

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", to, body, buttons))
        return {"messages": [{"id": f"wamid.out.{uuid4()}"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        self.sent.append(("list", to, body, rows))
        return {"messages": [{"id": f"wamid.out.{uuid4()}"}]}


_agent_calls: list[str] = []


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    # The agent's two history readers open sessions of their own.
    monkeypatch.setattr(graph, "async_session_factory", db)
    monkeypatch.setattr(scoped_help, "async_session_factory", db)
    # A developer's local allowlist must not silently drop the test patient.
    monkeypatch.setattr(tasks, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    _RecordingWhatsAppClient.created = []
    monkeypatch.setattr(tasks, "WhatsAppClient", _RecordingWhatsAppClient)
    _agent_calls.clear()

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

    async def _no_opening_state(session, tenant_id, patient_id, **kwargs):
        return None

    async def _agent(message, *args, **kwargs):
        _agent_calls.append(message)
        return "resposta da LLM"

    monkeypatch.setattr(tasks, "get_waba_token", _fake_token)
    monkeypatch.setattr(tasks, "get_entitlements", _fake_entitlements)
    monkeypatch.setattr(tasks, "resolve_patient_opening_state", _no_opening_state)
    monkeypatch.setattr(tasks, "run_agent", _agent)
    yield


def _stop_at_the_reply_seam(monkeypatch: pytest.MonkeyPatch) -> list:
    """End the turn where the reply would start; return what reached it."""
    replies: list = []

    async def _capture(reply, redis=None):
        replies.append(reply)

    monkeypatch.setattr(tasks, "_send_bot_reply", _capture)
    return replies


async def _seed(
    db,
    *,
    professional_names: tuple[str, ...] = ("Dra. Ana",),
    flow_state: FlowState = FlowState.IDLE,
    flow_step: str | None = None,
):
    """A live clinic and a consented, returning patient mid-conversation."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            initial_flows={"enabled": True, "menu_label": "Como posso ajudar?"},
            appointment_types=[{"name": "Consulta Geral", "duration_min": 30, "is_active": True}],
            business_hours={day: [{"start": "08:00", "end": "18:00"}] for day in _WEEKDAYS},
        )
        session.add(tenant)
        await session.flush()
        professionals = [
            Professional(tenant_id=tenant.id, name=name, is_active=True)
            for name in professional_names
        ]
        patient = Patient(
            tenant_id=tenant.id,
            wa_id=WA_ID,
            name="Maria",
            # Past the LGPD gate: these tests are about what a tap stores and
            # routes, and an unconsented patient never reaches the router.
            lgpd_accepted_at=datetime.now(UTC),
        )
        session.add_all([*professionals, patient])
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_state=flow_state,
            flow_step=flow_step,
        )
        session.add(conversation)
        await session.flush()
        # A prior turn, so the tap is not a first contact (no verbatim greeting
        # short-circuits it - see `_route_inbound_turn`'s ladder).
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.BOT,
                body="Com qual profissional você gostaria de agendar?",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
        for obj in (tenant, patient, conversation, *professionals):
            await session.refresh(obj)
        return tenant, professionals, conversation


def _tap(row_id: str, title: str, wam_id: str = "wamid.tap.1") -> WebhookValue:
    """The `messages` change Meta delivers for one list-row tap: the id and the
    title arrive as two separate fields."""
    return WebhookValue.model_validate(
        {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "contacts": [{"wa_id": WA_ID, "profile": {"name": "Maria"}}],
            "messages": [
                {
                    "id": wam_id,
                    "from": WA_ID,
                    "type": "interactive",
                    "interactive": {
                        "type": "list_reply",
                        "list_reply": {"id": row_id, "title": title},
                    },
                }
            ],
        }
    )


async def _inbound_row(db) -> Message:
    async with db() as session:
        (row,) = (
            await session.scalars(
                select(Message).where(Message.direction == MessageDirection.INBOUND)
            )
        ).all()
        return row


# --------------------------------------------------------------------------
# 1. Stored and shown: the title, never the id
# --------------------------------------------------------------------------


async def test_a_doctor_tap_is_stored_as_its_title_alone(db, monkeypatch) -> None:
    """The live bug, at the row it lived in."""
    replies = _stop_at_the_reply_seam(monkeypatch)
    _tenant, (ana,), _conversation = await _seed(db)
    title = flow_router._professional_row_title(ana.name)

    await tasks._handle_patient_messages(_tap(f"prof|{ana.id}", title))

    row = await _inbound_row(db)
    assert row.body == title
    assert str(ana.id) not in row.body
    assert row.interactive_reply_id == f"prof|{ana.id}"
    # The payload did not vanish: it rides beside the text, into the reply path.
    (reply,) = replies
    assert reply.inbound_body == f"{title} ({ana.id})"


@pytest.mark.parametrize(
    ("row_id", "title", "payload"),
    [
        ("slot|2026-06-15T08:00", "🗓️ 08:00", "2026-06-15T08:00"),
        ("day|2026-06-15|1", "🗓️ Seg, 15/06", "2026-06-15|1"),
        ("daymore|2", "Ver mais dias", "2"),
        ("dayagain|1", "⬅️ Outro dia", "1"),
        ("dayback|service", "⬅️ Outro serviço", "service"),
    ],
)
async def test_every_other_data_row_is_stored_clean_too(
    db, monkeypatch, row_id: str, title: str, payload: str
) -> None:
    """Not just `prof|`: every prefix of the payload family shared the string."""
    replies = _stop_at_the_reply_seam(monkeypatch)
    await _seed(db)

    await tasks._handle_patient_messages(_tap(row_id, title))

    row = await _inbound_row(db)
    assert row.body == title
    assert payload not in row.body
    assert row.interactive_reply_id == row_id
    (reply,) = replies
    assert reply.inbound_body == f"{title} ({payload})"


async def test_the_staff_console_shows_the_title_and_never_the_id(db, monkeypatch) -> None:
    """Where the leak was seen: the console's own read of the conversation."""
    _stop_at_the_reply_seam(monkeypatch)
    tenant, (ana,), conversation = await _seed(db)
    title = flow_router._professional_row_title(ana.name)
    await tasks._handle_patient_messages(_tap(f"prof|{ana.id}", title))

    from secretaria.config import get_settings

    get_settings.cache_clear()
    from secretaria.main import app

    async def _session():
        async with db() as session:
            yield session

    async def _tenant():
        return tenant

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get(f"/tenants/me/conversations/{conversation.id}/messages")
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_tenant, None)

    assert response.status_code == 200, response.text
    assert str(ana.id) not in response.text
    inbound = [m for m in response.json() if m["direction"] == "inbound"]
    assert [m["body"] for m in inbound] == [title]


# --------------------------------------------------------------------------
# 2. Still routed: the booking path picks the row that was tapped
# --------------------------------------------------------------------------


def _twin_snapshots() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            id=uuid4(),
            name=name,
            specialty=None,
            about=None,
            appointment_types=None,
            business_hours=None,
        )
        for name in _TWIN_NAMES
    ]


def _conversation_snapshot(**overrides) -> SimpleNamespace:
    fields = dict(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=flow_router.STEP_AWAITING_PROFESSIONAL,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        patient_id=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _tenant_snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        initial_flows={"enabled": True, "menu_label": "Como posso ajudar?"},
        appointment_types=[
            {"name": "Consulta Geral", "duration_min": 30, "is_active": True, "sort_order": 0}
        ],
        appointment_duration_min=30,
        business_hours={day: [{"start": "08:00", "end": "18:00"}] for day in _WEEKDAYS},
        collect_insurance=False,
        insurances=None,
    )


async def test_the_title_alone_cannot_tell_the_twin_doctors_apart() -> None:
    """Why the end-to-end test below can only pass through the id.

    Both rows render the same title, so routing on the text the patient saw
    never lands on the second doctor: the name fallback finds nobody, or the
    FIRST twin. Only the row id can name the one that was tapped.
    """
    souza, lopes = _twin_snapshots()
    title = flow_router._professional_row_title(lopes.name)
    assert title == flow_router._professional_row_title(souza.name)

    blind = await flow_router.route(
        _conversation_snapshot(), _tenant_snapshot(), None, title, professionals=[souza, lopes]
    )
    assert blind.flow_selected_professional_id != lopes.id

    tapped = await flow_router.route(
        _conversation_snapshot(),
        _tenant_snapshot(),
        None,
        inbound_routing_text(title, f"prof|{lopes.id}"),
        professionals=[souza, lopes],
    )
    assert tapped.flow_selected_professional_id == lopes.id


async def test_a_doctor_tap_still_selects_the_doctor_tapped_end_to_end(db) -> None:
    """The non-regression that matters: the production booking path.

    Real webhook entry, real `_send_bot_reply`, real flow router, real
    persistence - only the network edges are faked. The patient taps the SECOND
    of two doctors whose rows look identical; the conversation must move on with
    THAT doctor, and the stored text must still be the bare title.
    """
    _tenant, (souza, lopes), conversation = await _seed(
        db,
        professional_names=_TWIN_NAMES,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=flow_router.STEP_AWAITING_PROFESSIONAL,
    )
    title = flow_router._professional_row_title(lopes.name)
    assert title == flow_router._professional_row_title(souza.name)

    await tasks._handle_patient_messages(_tap(f"prof|{lopes.id}", title))

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_selected_professional_id == lopes.id
    assert conv.flow_state == FlowState.SERVICE_CATALOG
    assert conv.flow_step == flow_router.STEP_AWAITING_SERVICE
    # Deterministic all the way: the tap never needed the model...
    assert _agent_calls == []
    # ...and the next screen (that doctor's services) actually went out.
    sends = [send for client in _RecordingWhatsAppClient.created for send in client.sent]
    assert any(send[0] == "list" for send in sends)
    # Same turn, same row: what staff will see is still just the title.
    row = await _inbound_row(db)
    assert row.body == title
    assert str(lopes.id) not in row.body


class _Calendar:
    """Just enough CalendarService for the slot step, which lists nothing."""

    tzinfo = _TZ

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        return []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        return []


async def test_a_slot_tap_still_confirms_the_slot_tapped() -> None:
    """The second prefix re-checked: a free-slot row, parsed from a real tap."""
    msg = WebhookMessage.model_validate(
        {
            "id": "wamid.slot",
            "from": WA_ID,
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {"id": "slot|2026-06-15T08:00", "title": "🗓️ 08:00"},
            },
        }
    )
    body = extract_inbound_body(msg)
    assert body == "🗓️ 08:00"

    result = await flow_router.route(
        _conversation_snapshot(
            flow_step=flow_router.STEP_AWAITING_SLOT,
            flow_selected_type="Consulta Geral",
            flow_selected_day="2026-06-15",
        ),
        _tenant_snapshot(),
        _Calendar(),
        inbound_routing_text(body, extract_inbound_reply_id(msg)),
    )
    assert result.flow_step == flow_router.STEP_AWAITING_CONFIRMATION
    assert result.flow_selected_slot == "2026-06-15T08:00"


# --------------------------------------------------------------------------
# 3. The agent still reads the payload - on this turn and every later one
# --------------------------------------------------------------------------


async def test_the_agent_still_reads_a_slot_tap_with_its_iso(db, monkeypatch) -> None:
    """ai/prompts.py tells the model a [SLOTS] tap arrives as "<rótulo> (<iso>)",
    and the model books it a turn LATER, after the patient taps [CONFIRM] - by
    reading the ISO back out of its history. So the history, not only the
    current turn, must keep recomposing it from the stored id."""
    _stop_at_the_reply_seam(monkeypatch)
    _tenant, _professionals, conversation = await _seed(db, flow_state=FlowState.LLM)

    await tasks._handle_patient_messages(_tap("slot|2026-05-29T15:00:00", "🗓️ 15:00"))

    row = await _inbound_row(db)
    assert row.body == "🗓️ 15:00"
    expected = "🗓️ 15:00 (2026-05-29T15:00:00)"
    history = await graph._load_history(conversation.id)
    assert [m.content for m in history if isinstance(m, HumanMessage)] == [expected]
    help_history = await scoped_help._recent_history(conversation.id)
    assert [m.content for m in help_history if isinstance(m, HumanMessage)] == [expected]


async def test_a_row_stored_before_the_split_reads_back_to_the_agent_unchanged(db) -> None:
    """Old rows keep their payload inside `body` and carry no id: the agent must
    read them exactly as before, never with a second "(…)" appended."""
    _tenant, _professionals, conversation = await _seed(db)
    legacy = f"🥼 Dra. Ana ({uuid4()})"
    async with db() as session:
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                wam_id="wamid.legacy",
                body=legacy,
            )
        )
        await session.commit()

    history = await graph._load_history(conversation.id)
    assert [m.content for m in history if isinstance(m, HumanMessage)] == [legacy]
