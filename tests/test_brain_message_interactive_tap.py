"""Real taps on the patient portal: the Brain-Message twin of a WhatsApp tap.

Origin: z_prompts/PROMPT_BRAIN_MESSAGE_PORTAL_INTERACTIVE_TAP.md. Before this,
the Brain-Message sender recorded a card as flattened text only, the portal
route served text only, and the inbound contract had no room for the id of a
tapped control - the portal patient typed the option's label. Now the channel
records the same `Message.interactive` WhatsApp does, serves it, and accepts
`interactive_reply_id` on the way in.

What is different from WhatsApp, and what most of this file defends: a
WhatsApp tap id arrives in a webhook Meta signed; a portal tap id arrives from
the patient's own browser. So the worker only honours an id that a recent card
of THAT conversation actually offered, and routes anything else as typed text
(`workers/tasks.py::_validated_brain_message_reply_id`).

Layout:
  1. the sender records the card, through the same builders as WhatsApp;
  2. the wire (GET .../messages) exposes the card and the tap;
  3. the id travels endpoint -> job -> `_route_inbound_turn`, and a forged one
     is dropped before it can reach the routing text;
  4. end to end on the production booking path: two doctors with identical row
     titles, the portal tap lands on the one tapped, deterministically.

Fixtures follow tests/test_brain_message_pipeline.py (in-memory SQLite on a
StaticPool; a WhatsApp client that fails the test if anything builds it).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from datetime import UTC, datetime  # noqa: E402
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

from secretaria.ai import graph, scoped_help  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.core.whatsapp_limits import (  # noqa: E402
    MAX_BUTTONS_PER_MESSAGE,
    MAX_INTERACTIVE_REPLY_ID_CHARS,
    MAX_LIST_ROWS,
)
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
from secretaria.services import flow_router  # noqa: E402
from secretaria.services.channel_sender import (  # noqa: E402
    CHANNEL_BRAIN_MESSAGE,
    BrainMessageSender,
    interactive_history_body,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.whatsapp import (  # noqa: E402
    interactive_buttons_record,
    interactive_list_record,
)
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"
EXTERNAL_ID = "bm-session-tap-1"
GOOD_KEY = "test-internal-key"
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
# Two doctors whose list rows render IDENTICALLY past the row-title cap: only
# the row id can tell them apart (tests/test_inbound_tap_display_vs_routing.py).
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


class _ExplodingWhatsAppClient:
    """Any use at all fails the test: a portal turn must never reach Meta."""

    @classmethod
    def for_tenant(cls, tenant, access_token):
        raise AssertionError("the Brain-Message path built a WhatsApp client")

    @classmethod
    def for_dev_scaffold(cls, settings=None):
        raise AssertionError("the Brain-Message path built a WhatsApp client")


_agent_calls: list[str] = []


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    monkeypatch.setattr(graph, "async_session_factory", db)
    monkeypatch.setattr(scoped_help, "async_session_factory", db)
    monkeypatch.setattr(tasks, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    monkeypatch.setattr(tasks, "WhatsAppClient", _ExplodingWhatsAppClient)
    _agent_calls.clear()

    async def _no_opening_state(session, tenant_id, patient_id, **kwargs):
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

    async def _agent(message, *args, **kwargs):
        _agent_calls.append(message)
        return "resposta da LLM"

    monkeypatch.setattr(tasks, "resolve_patient_opening_state", _no_opening_state)
    monkeypatch.setattr(tasks, "get_waba_token", _fake_token)
    monkeypatch.setattr(tasks, "get_entitlements", _fake_entitlements)
    monkeypatch.setattr(tasks, "run_agent", _agent)
    yield


async def _seed(
    db,
    *,
    professional_names: tuple[str, ...] = ("Dra. Ana",),
    flow_state: FlowState = FlowState.IDLE,
    flow_step: str | None = None,
):
    """A live clinic and a consented, returning PORTAL patient mid-conversation."""
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
            channel=CHANNEL_BRAIN_MESSAGE,
            external_id=EXTERNAL_ID,
            name="Maria",
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
        await session.commit()
        for obj in (tenant, patient, conversation, *professionals):
            await session.refresh(obj)
        return tenant, professionals, conversation


async def _rows(db, conversation_id) -> list[Message]:
    async with db() as session:
        return list(
            (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at, Message.id)
                )
            ).all()
        )


async def _inbound_row(db, conversation_id) -> Message:
    rows = [r for r in await _rows(db, conversation_id) if r.direction == MessageDirection.INBOUND]
    assert len(rows) == 1, rows
    return rows[0]


def _stop_at_the_reply_seam(monkeypatch: pytest.MonkeyPatch) -> list:
    """End the turn where the reply would start; return what reached it."""
    replies: list = []

    async def _capture(reply, redis=None):
        replies.append(reply)

    monkeypatch.setattr(tasks, "_send_bot_reply", _capture)
    return replies


async def _turn(tenant: Tenant, text: str, reply_id: str | None) -> None:
    """One portal turn, exactly as the arq worker runs it."""
    await tasks.process_brain_message_inbound(
        {}, str(tenant.id), EXTERNAL_ID, text=text, interactive_reply_id=reply_id
    )


# --------------------------------------------------------------------------
# 1. The sender records the card - the same record WhatsApp stores
# --------------------------------------------------------------------------


async def test_send_buttons_records_the_card_whatsapp_would(db) -> None:
    """Same builder, same caps: a fourth button is dropped here as on WhatsApp,
    and the history body is the flattened line it always was."""
    _, _, conversation = await _seed(db)
    sender = BrainMessageSender(conversation_id=conversation.id, session_factory=db)
    buttons = [(f"menu|{i}", f"Opção {i}") for i in range(MAX_BUTTONS_PER_MESSAGE + 1)]

    await sender.send_buttons(to=EXTERNAL_ID, body="Como posso ajudar?", buttons=buttons)

    [row] = await _rows(db, conversation.id)
    assert row.interactive == interactive_buttons_record("Como posso ajudar?", buttons)
    assert row.interactive["kind"] == "buttons"
    assert [o["id"] for o in row.interactive["options"]] == ["menu|0", "menu|1", "menu|2"]
    assert row.body == interactive_history_body("Como posso ajudar?", [b[1] for b in buttons])
    assert row.direction == MessageDirection.OUTBOUND and row.sender == MessageSender.BOT


async def test_send_list_records_the_card_whatsapp_would(db) -> None:
    _, _, conversation = await _seed(db)
    sender = BrainMessageSender(conversation_id=conversation.id, session_factory=db)
    rows = [(f"prof|{uuid4()}", f"Dra. {i}", "desc") for i in range(MAX_LIST_ROWS + 2)]

    await sender.send_list(
        to=EXTERNAL_ID,
        body="Com qual profissional?",
        button_label="Ver profissionais",
        rows=rows,
        section_title="Profissionais",
    )

    [row] = await _rows(db, conversation.id)
    assert row.interactive == interactive_list_record(
        "Com qual profissional?", "Ver profissionais", rows, "Profissionais"
    )
    assert row.interactive["kind"] == "list"
    assert len(row.interactive["options"]) == MAX_LIST_ROWS
    assert row.interactive["button_label"] == "Ver profissionais"
    assert row.body == interactive_history_body("Com qual profissional?", [r[1] for r in rows])


async def test_text_records_no_card(db) -> None:
    _, _, conversation = await _seed(db)
    sender = BrainMessageSender(conversation_id=conversation.id, session_factory=db)
    await sender.send_text_message(to=EXTERNAL_ID, body="Olá")
    [row] = await _rows(db, conversation.id)
    assert row.interactive is None


# --------------------------------------------------------------------------
# 2. The pure check
# --------------------------------------------------------------------------


def test_offered_reply_ids_collects_ids_and_shrugs_at_bad_blobs() -> None:
    cards = [
        {"kind": "buttons", "options": [{"id": "a", "title": "A"}, {"id": "b"}]},
        {"kind": "list", "options": [{"id": "c"}, {"title": "no id"}, "junk", None]},
        {"kind": "buttons"},
        {"options": None},
        None,
        "not a card",
        {"options": [{"id": 7}]},
    ]
    assert tasks.offered_reply_ids(cards) == {"a", "b", "c"}
    assert tasks.offered_reply_ids([]) == set()


# --------------------------------------------------------------------------
# 3. The wire and the endpoint
# --------------------------------------------------------------------------


class _FakeArqPool:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))


@pytest_asyncio.fixture
async def api(db):
    """The real ASGI app, on the in-memory DB, with a recording queue."""
    from httpx import ASGITransport, AsyncClient

    from secretaria.config import get_settings
    from secretaria.core.database import get_session

    get_settings.cache_clear()
    from secretaria.main import app

    async def _override_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    pool = _FakeArqPool()
    app.state.arq_pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac, pool
    app.dependency_overrides.clear()


async def _post_inbound(client, tenant, **fields):
    return await client.post(
        "/internal/brain-message/inbound",
        headers={"X-Internal-Api-Key": GOOD_KEY},
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, **fields},
    )


async def test_the_portal_route_serves_the_card_and_the_tap(api, db) -> None:
    client, _ = api
    tenant, _, conversation = await _seed(db)
    sender = BrainMessageSender(conversation_id=conversation.id, session_factory=db)
    await sender.send_buttons(
        to=EXTERNAL_ID, body="Aceita?", buttons=[("consent|accept", "✅ Concordo")]
    )
    async with db() as session:
        async with session.begin():
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND,
                    sender=MessageSender.PATIENT,
                    body="✅ Concordo",
                    interactive_reply_id="consent|accept",
                )
            )

    read = await client.get(
        f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages",
        params={"tenant_id": str(tenant.id)},
        headers={"X-Internal-Api-Key": GOOD_KEY},
    )
    assert read.status_code == 200, read.text
    by_direction = {r["direction"]: r for r in read.json()["data"]}
    card = by_direction["outbound"]["interactive"]
    assert card["kind"] == "buttons" and card["body"] == "Aceita?"
    assert card["options"] == [
        {"id": "consent|accept", "title": "✅ Concordo", "description": None}
    ]
    assert by_direction["outbound"]["interactive_reply_id"] is None
    assert by_direction["inbound"]["interactive"] is None
    assert by_direction["inbound"]["interactive_reply_id"] == "consent|accept"


async def test_the_endpoint_queues_the_tap_id(api, db) -> None:
    client, pool = api
    tenant, _, _ = await _seed(db)
    response = await _post_inbound(
        client, tenant, text="✅ Concordo", interactive_reply_id="consent|accept"
    )
    assert response.status_code == 202, response.text
    [(name, _args, kwargs)] = pool.jobs
    assert name == "process_brain_message_inbound"
    assert kwargs["interactive_reply_id"] == "consent|accept"
    # Without the field the job is enqueued exactly as before (None, not absent).
    response = await _post_inbound(client, tenant, text="oi")
    assert response.status_code == 202
    assert pool.jobs[1][2]["interactive_reply_id"] is None


@pytest.mark.parametrize("bad", ["", "x" * (MAX_INTERACTIVE_REPLY_ID_CHARS + 1)])
async def test_the_endpoint_bounds_the_tap_id(api, db, bad) -> None:
    client, pool = api
    tenant, _, _ = await _seed(db)
    response = await _post_inbound(client, tenant, text="oi", interactive_reply_id=bad)
    assert response.status_code == 422, response.text
    assert pool.jobs == []


# --------------------------------------------------------------------------
# 4. Offered ids route; forged ids are dropped before the routing text
# --------------------------------------------------------------------------


async def _offer_doctor(db, conversation, professional) -> str:
    """Record the doctor list card on the conversation; return the row title."""
    title = flow_router._professional_row_title(professional.name)
    await BrainMessageSender(conversation_id=conversation.id, session_factory=db).send_list(
        to=EXTERNAL_ID,
        body="Com qual?",
        button_label="Ver",
        rows=[(f"prof|{professional.id}", title, None)],
    )
    return title


async def test_an_offered_id_reaches_the_router_with_its_payload(db, monkeypatch) -> None:
    tenant, [ana], conversation = await _seed(db)
    title = await _offer_doctor(db, conversation, ana)
    replies = _stop_at_the_reply_seam(monkeypatch)

    await _turn(tenant, title, f"prof|{ana.id}")

    [reply] = replies
    # The router reads the WhatsApp string: "<title> (<uuid>)".
    assert reply.inbound_body == f"{title} ({ana.id})"
    row = await _inbound_row(db, conversation.id)
    assert row.body == title
    assert row.interactive_reply_id == f"prof|{ana.id}"


@pytest.mark.parametrize(
    "forged",
    [
        "prof|{other}",  # a doctor this conversation never listed
        "slot|2030-01-01T09:00:00-03:00",  # a slot never offered
        "consent|accept",  # a real id of the product, but not on THIS thread
    ],
)
async def test_a_forged_id_is_dropped_and_the_text_still_routes(db, monkeypatch, forged) -> None:
    tenant, [ana], conversation = await _seed(db)
    title = await _offer_doctor(db, conversation, ana)
    replies = _stop_at_the_reply_seam(monkeypatch)
    forged = forged.format(other=uuid4())

    await _turn(tenant, title, forged)

    [reply] = replies
    # Routed as typed text: the forged payload never touches the routing text.
    assert reply.inbound_body == title
    row = await _inbound_row(db, conversation.id)
    assert row.body == title
    assert row.interactive_reply_id is None


async def test_an_id_from_another_patients_conversation_is_not_offered(db, monkeypatch) -> None:
    """Scope: the card must be on the PATIENT'S conversation, not just the clinic's."""
    tenant, [ana], conversation = await _seed(db)
    async with db() as session:
        async with session.begin():
            other = Patient(
                tenant_id=tenant.id, channel=CHANNEL_BRAIN_MESSAGE, external_id="someone-else"
            )
            session.add(other)
            await session.flush()
            other_conv = Conversation(tenant_id=tenant.id, patient_id=other.id)
            session.add(other_conv)
    title = await _offer_doctor(db, other_conv, ana)
    replies = _stop_at_the_reply_seam(monkeypatch)

    await _turn(tenant, title, f"prof|{ana.id}")

    assert replies[0].inbound_body == title
    assert (await _inbound_row(db, conversation.id)).interactive_reply_id is None


async def test_a_card_past_the_window_no_longer_vouches_for_its_ids(db, monkeypatch) -> None:
    tenant, [ana], conversation = await _seed(db)
    title = await _offer_doctor(db, conversation, ana)
    # Push the card out of the window with newer cards offering other ids.
    # Explicit, strictly later timestamps: SQLite's server default ticks per
    # second, and the window is ordered by created_at.
    async with db() as session:
        async with session.begin():
            for i in range(tasks.BRAIN_MESSAGE_TAP_WINDOW):
                session.add(
                    Message(
                        conversation_id=conversation.id,
                        direction=MessageDirection.OUTBOUND,
                        sender=MessageSender.BOT,
                        body=f"card {i}",
                        interactive={
                            "kind": "buttons",
                            "body": "x",
                            "options": [{"id": f"menu|{i}", "title": "x"}],
                        },
                        created_at=datetime(2030, 1, 1, 12, 0, i, tzinfo=UTC),
                    )
                )
    replies = _stop_at_the_reply_seam(monkeypatch)

    await _turn(tenant, title, f"prof|{ana.id}")

    assert replies[0].inbound_body == title
    assert (await _inbound_row(db, conversation.id)).interactive_reply_id is None


# --------------------------------------------------------------------------
# 5. End to end on the production booking path
# --------------------------------------------------------------------------


async def _seed_twins(db):
    tenant, (souza, lopes), conversation = await _seed(
        db,
        professional_names=_TWIN_NAMES,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=flow_router.STEP_AWAITING_PROFESSIONAL,
    )
    title = flow_router._professional_row_title(lopes.name)
    assert title == flow_router._professional_row_title(souza.name)
    await BrainMessageSender(conversation_id=conversation.id, session_factory=db).send_list(
        to=EXTERNAL_ID,
        body="Com qual profissional você gostaria de agendar?",
        button_label="Ver profissionais",
        rows=[(f"prof|{p.id}", title, None) for p in (souza, lopes)],
    )
    return tenant, souza, lopes, conversation, title


async def test_a_portal_tap_selects_the_twin_doctor_tapped_end_to_end(db) -> None:
    """Real worker entry, real `_send_bot_reply`, real flow router, real
    persistence - no network edge at all on this channel. The patient taps the
    SECOND of two doctors whose rows look identical; the conversation must move
    on with THAT doctor, and the next screen must be recorded as a card."""
    tenant, _souza, lopes, conversation, title = await _seed_twins(db)

    await _turn(tenant, title, f"prof|{lopes.id}")

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_selected_professional_id == lopes.id
    assert conv.flow_state == FlowState.SERVICE_CATALOG
    assert conv.flow_step == flow_router.STEP_AWAITING_SERVICE
    assert _agent_calls == []
    assert (await _inbound_row(db, conversation.id)).body == title
    # The next screen (that doctor's services) went out as a real card.
    later = (await _rows(db, conversation.id))[1:]
    assert any(
        r.direction == MessageDirection.OUTBOUND
        and r.interactive is not None
        and r.interactive["kind"] == "list"
        for r in later
    )


async def test_the_title_alone_still_cannot_tell_the_twins_apart_on_the_portal(db) -> None:
    """Control: the same turn typed (no id) does not land on the second doctor."""
    tenant, _souza, lopes, conversation, title = await _seed_twins(db)

    await _turn(tenant, title, None)

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_selected_professional_id != lopes.id
