"""messages.interactive - an interactive send's structure, recorded beside its history text.

The staff console used to receive only `Message.body`, which
`workers/tasks.py::_bubble_history_body` flattens for the agent's history - so a
list of doctors reached it as "... (opções: A, B)" and the LGPD button did not
reach it at all (found live 2026-09-10,
z_prompts/PROMPT_BRAIN_MESSAGE_INTERACTIVE_BUBBLES_RENDERING.md). These tests
pin the two halves apart:

- the WhatsApp record sites (`_dispatch_bubbles`, `_send_greeting`,
  `_send_consent_notice`) store `interactive` = the card WhatsApp drew (ids,
  titles, list button and heading) AND leave `body` byte for byte what the
  agent's history has always read;
- `WhatsAppClient` builds its payload FROM the same record, so the stored copy
  and the delivered card cannot drift;
- Brain-Message rows stay text only - that patient never sees a control;
- the hub endpoint serves the card and the tap id, and a malformed blob costs
  one message its controls, never the whole thread.
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
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.core.whatsapp_limits import MAX_BUTTONS_PER_MESSAGE, MAX_LIST_ROWS  # noqa: E402
from secretaria.models import Conversation, HandoverState, Message, Patient, Tenant  # noqa: E402
from secretaria.models.message import MessageDirection, MessageSender  # noqa: E402
from secretaria.services.channel_sender import BrainMessageSender  # noqa: E402
from secretaria.services.flow_router import MenuBubble  # noqa: E402
from secretaria.services.whatsapp import (  # noqa: E402
    WhatsAppClient,
    interactive_buttons_record,
    interactive_list_record,
)
from secretaria.workers import tasks  # noqa: E402

PATIENT_REF = "5511999990000"


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


@pytest_asyncio.fixture
async def seeded(db) -> tuple[Tenant, Conversation]:
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()
        patient = Patient(tenant_id=tenant.id, wa_id=PATIENT_REF, name="Ana Paciente")
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, handover_state=HandoverState.BOT_ACTIVE
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(conversation)
        return tenant, conversation


class _RecordingClient:
    """Stands in for WhatsAppClient: records every send, answers the way Meta does."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _ok(self) -> dict:
        return {"messages": [{"id": f"wamid.test-{len(self.calls)}"}]}

    async def send_text_message(self, to, body):
        self.calls.append(("text", {"to": to, "body": body}))
        return self._ok()

    async def send_buttons(self, to, body, buttons):
        self.calls.append(("buttons", {"to": to, "body": body, "buttons": buttons}))
        return self._ok()

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        self.calls.append(
            (
                "list",
                {
                    "to": to,
                    "body": body,
                    "button_label": button_label,
                    "rows": rows,
                    "section_title": section_title,
                },
            )
        )
        return self._ok()


@pytest.fixture
def whatsapp(monkeypatch: pytest.MonkeyPatch, db) -> _RecordingClient:
    client = _RecordingClient()
    monkeypatch.setattr(tasks, "async_session_factory", db)
    monkeypatch.setattr(tasks, "_tenant_client", lambda tenant, waba_token: client)
    return client


def _reply(conversation: Conversation, **fields) -> "tasks._ReplyContext":
    return tasks._ReplyContext(
        conversation_id=conversation.id, patient_ref=PATIENT_REF, inbound_body="", **fields
    )


async def _rows(db, conversation_id) -> list[Message]:
    async with db() as session:
        return list(
            (
                await session.scalars(
                    select(Message).where(Message.conversation_id == conversation_id)
                )
            ).all()
        )


# --------------------------------------------------------------------------
# WhatsApp record sites
# --------------------------------------------------------------------------


async def test_a_list_records_its_rows_and_keeps_the_history_body_byte_for_byte(
    db, seeded, whatsapp
) -> None:
    tenant, conversation = seeded
    bubble = SlotsBubble(
        body="Com qual profissional você gostaria de agendar?",
        rows=[
            ("prof|aaa", "🩺 Dr. A", "Oftalmologia"),
            ("prof|bbb", "🩺 Dr. B"),
            ("prof|unsure", "Não sei"),
        ],
        button_label="Ver profissionais",
        section_title="Profissionais",
    )
    # The agent's history contract, pinned as a literal: this change must not
    # move a single character of it.
    history = (
        "Com qual profissional você gostaria de agendar?\n(opções: 🩺 Dr. A, 🩺 Dr. B, Não sei)"
    )
    assert tasks._bubble_history_body(bubble) == history

    sent = await tasks._dispatch_bubbles(
        _reply(conversation), [bubble], tenant=tenant, waba_token="t"
    )

    assert sent == 1
    [row] = await _rows(db, conversation.id)
    assert row.body == history
    assert row.interactive == {
        "kind": "list",
        "body": "Com qual profissional você gostaria de agendar?",
        "button_label": "Ver profissionais",
        "section_title": "Profissionais",
        "options": [
            {"id": "prof|aaa", "title": "🩺 Dr. A", "description": "Oftalmologia"},
            {"id": "prof|bbb", "title": "🩺 Dr. B", "description": None},
            {"id": "prof|unsure", "title": "Não sei", "description": None},
        ],
    }
    # And it is what went out: the ids a later tap will carry are the ids sent.
    [(kind, call)] = whatsapp.calls
    assert kind == "list"
    assert [(o["id"], o["title"]) for o in row.interactive["options"]] == [
        (r[0], r[1]) for r in call["rows"]
    ]


async def test_a_confirm_card_and_a_menu_record_buttons_with_the_ids_sent(
    db, seeded, whatsapp
) -> None:
    tenant, conversation = seeded
    confirm = ButtonBubble(body="Confirmo sua consulta amanhã às 15:00?")
    menu = MenuBubble(body="Como posso ajudar?", labels=["🗓️ Agendar", "Outro"])

    await tasks._dispatch_bubbles(
        _reply(conversation), [confirm, menu], tenant=tenant, waba_token="t"
    )

    rows = {row.body: row for row in await _rows(db, conversation.id)}
    # A confirm card's history body was always the bare body; still is.
    assert rows["Confirmo sua consulta amanhã às 15:00?"].interactive == {
        "kind": "buttons",
        "body": "Confirmo sua consulta amanhã às 15:00?",
        "options": [
            {"id": tasks.BUTTON_ID_CONFIRM, "title": "Confirmar"},
            {"id": tasks.BUTTON_ID_CANCEL, "title": "Cancelar"},
        ],
    }
    menu_row = rows["Como posso ajudar?\n(opções: 🗓️ Agendar, Outro)"]
    assert menu_row.interactive["options"] == [
        {"id": "menu|0", "title": "🗓️ Agendar"},
        {"id": "menu|1", "title": "Outro"},
    ]
    assert [c[1]["buttons"] for c in whatsapp.calls] == [
        [(o["id"], o["title"]) for o in rows[body].interactive["options"]]
        for body in ("Confirmo sua consulta amanhã às 15:00?", menu_row.body)
    ]


async def test_a_text_bubble_records_no_structure(db, seeded, whatsapp) -> None:
    tenant, conversation = seeded
    await tasks._dispatch_bubbles(
        _reply(conversation), [TextBubble(body="Olá!")], tenant=tenant, waba_token="t"
    )
    [row] = await _rows(db, conversation.id)
    assert row.body == "Olá!"
    assert row.interactive is None


async def test_the_lgpd_notice_records_its_concordo_button(db, seeded, whatsapp) -> None:
    tenant, conversation = seeded
    await tasks._send_consent_notice(_reply(conversation), tenant=tenant, waba_token="t")

    [row] = await _rows(db, conversation.id)
    # History unchanged: the bare notice, as before.
    assert row.body == tasks.LGPD_CONSENT_MESSAGE
    assert row.interactive == {
        "kind": "buttons",
        "body": tasks.LGPD_CONSENT_MESSAGE,
        "options": [{"id": "consent|accept", "title": tasks.CONSENT_BUTTON_LABEL}],
    }
    [(_, call)] = whatsapp.calls
    assert call["buttons"] == [("consent|accept", tasks.CONSENT_BUTTON_LABEL)]


async def test_a_greeting_records_its_buttons_and_a_plain_one_records_none(
    db, seeded, whatsapp
) -> None:
    tenant, conversation = seeded
    await tasks._send_greeting(
        _reply(
            conversation,
            greeting_override="Olá! Sou a secretária da clínica.",
            greeting_buttons=["🗓️ Agendar", "Outro"],
        ),
        tenant=tenant,
        waba_token="t",
    )
    await tasks._send_greeting(
        _reply(conversation, greeting_override="Bem-vinda de volta!"),
        tenant=tenant,
        waba_token="t",
    )

    rows = {row.body: row for row in await _rows(db, conversation.id)}
    card = rows["Olá! Sou a secretária da clínica."].interactive
    assert card["kind"] == "buttons"
    # Whatever ids `_send_greeting` minted ("greeting|agendar", ...) are the
    # ones recorded - the property the console's tap-linking relies on.
    assert [(o["id"], o["title"]) for o in card["options"]] == whatsapp.calls[0][1]["buttons"]
    assert [o["title"] for o in card["options"]] == ["🗓️ Agendar", "Outro"]
    assert rows["Bem-vinda de volta!"].interactive is None


async def test_brain_message_rows_stay_text_only(db, seeded) -> None:
    """That patient reads the options as text and types; recording a card
    would make the console draw controls the patient never had."""
    _, conversation = seeded
    sender = BrainMessageSender(conversation_id=conversation.id, session_factory=db)

    await sender.send_buttons(
        to="ext-1", body="Aceita os termos?", buttons=[("consent|accept", "✅ Concordo")]
    )

    [row] = await _rows(db, conversation.id)
    assert row.body == "Aceita os termos?\n(opções: ✅ Concordo)"
    assert row.interactive is None


# --------------------------------------------------------------------------
# The record IS the payload
# --------------------------------------------------------------------------


def _payload_capturing_client() -> tuple[WhatsAppClient, dict]:
    # Only the payload builder is exercised: no credentials, no network.
    client = WhatsAppClient.__new__(WhatsAppClient)
    captured: dict = {}

    async def _post(payload, to=None):
        captured["payload"] = payload
        return {"messages": [{"id": "wamid.x"}]}

    client._post = _post
    return client, captured


async def test_send_list_payload_is_the_record_caps_included() -> None:
    client, captured = _payload_capturing_client()
    long_name = "Dra. Maria Aparecida dos Santos Oliveira"
    rows = [("prof|1", long_name, "d" * 120)] + [
        (f"row|{i}", f"Opção {i}", None) for i in range(MAX_LIST_ROWS + 2)
    ]

    await client.send_list(
        to="5511",
        body="Qual?",
        button_label="Ver profissionais",
        rows=rows,
        section_title="Profissionais",
    )

    record = interactive_list_record("Qual?", "Ver profissionais", rows, "Profissionais")
    action = captured["payload"]["interactive"]["action"]
    assert captured["payload"]["interactive"]["body"]["text"] == record["body"]
    assert action["button"] == record["button_label"]
    assert action["sections"][0]["title"] == record["section_title"]
    assert [(r["id"], r["title"], r.get("description")) for r in action["sections"][0]["rows"]] == [
        (o["id"], o["title"], o["description"]) for o in record["options"]
    ]
    # The record holds what the phone shows, not what was asked for.
    assert len(record["options"]) == MAX_LIST_ROWS
    assert record["options"][0]["title"] != long_name
    assert len(record["options"][0]["description"]) < 120


async def test_send_buttons_payload_is_the_record_caps_included() -> None:
    client, captured = _payload_capturing_client()
    buttons = [
        (f"b|{i}", f"Um rótulo comprido demais {i}") for i in range(MAX_BUTTONS_PER_MESSAGE + 1)
    ]

    await client.send_buttons(to="5511", body="Escolha", buttons=buttons)

    record = interactive_buttons_record("Escolha", buttons)
    sent = captured["payload"]["interactive"]["action"]["buttons"]
    assert [(b["reply"]["id"], b["reply"]["title"]) for b in sent] == [
        (o["id"], o["title"]) for o in record["options"]
    ]
    assert len(record["options"]) == MAX_BUTTONS_PER_MESSAGE
    assert all(
        o["title"] != label for o, (_, label) in zip(record["options"], buttons, strict=False)
    )


# --------------------------------------------------------------------------
# The hub wire (GET /tenants/me/conversations/{id}/messages)
# --------------------------------------------------------------------------


@pytest.fixture
def hub(db, seeded):
    from secretaria.main import app

    tenant, _ = seeded

    async def _session():
        async with db() as session:
            yield session

    async def _tenant():
        return tenant

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _add(db, conversation: Conversation, created_at: datetime, **fields) -> None:
    async with db() as session:
        session.add(Message(conversation_id=conversation.id, created_at=created_at, **fields))
        await session.commit()


async def test_the_hub_serves_the_card_and_the_tap_that_answered_it(
    client: AsyncClient, db, seeded, hub
) -> None:
    _, conversation = seeded
    now = datetime.now(UTC)
    consent = interactive_buttons_record("Aceita os termos?", [("consent|accept", "✅ Concordo")])
    doctors = interactive_list_record(
        "Com qual profissional?",
        "Ver profissionais",
        [("prof|1", "🩺 Dr. A", "Retina")],
        "Profissionais",
    )
    bot, patient = MessageSender.BOT, MessageSender.PATIENT
    out_, in_ = MessageDirection.OUTBOUND, MessageDirection.INBOUND
    await _add(
        db,
        conversation,
        now - timedelta(minutes=3),
        direction=out_,
        sender=bot,
        body="Aceita os termos?",
        interactive=consent,
    )
    await _add(
        db,
        conversation,
        now - timedelta(minutes=2),
        direction=in_,
        sender=patient,
        body="✅ Concordo",
        interactive_reply_id="consent|accept",
    )
    await _add(
        db,
        conversation,
        now - timedelta(minutes=1),
        direction=out_,
        sender=bot,
        body="Com qual profissional?\n(opções: 🩺 Dr. A)",
        interactive=doctors,
    )
    await _add(db, conversation, now, direction=out_, sender=bot, body="Perfeito!")

    response = await client.get(f"/tenants/me/conversations/{conversation.id}/messages")

    assert response.status_code == 200
    first, tap, lst, text = response.json()
    assert first["body"] == "Aceita os termos?"
    assert first["interactive"] == {
        "kind": "buttons",
        "body": "Aceita os termos?",
        "options": [{"id": "consent|accept", "title": "✅ Concordo", "description": None}],
        "button_label": None,
        "section_title": None,
    }
    assert tap["interactive"] is None
    assert tap["interactive_reply_id"] == "consent|accept"
    # `body` keeps the history spelling; the card carries its own text.
    assert lst["body"] == "Com qual profissional?\n(opções: 🩺 Dr. A)"
    assert lst["interactive"]["kind"] == "list"
    assert lst["interactive"]["body"] == "Com qual profissional?"
    assert lst["interactive"]["button_label"] == "Ver profissionais"
    assert lst["interactive"]["section_title"] == "Profissionais"
    assert lst["interactive"]["options"] == [
        {"id": "prof|1", "title": "🩺 Dr. A", "description": "Retina"}
    ]
    assert text["interactive"] is None
    assert text["interactive_reply_id"] is None


async def test_a_malformed_card_costs_its_controls_never_the_thread(
    client: AsyncClient, db, seeded, hub
) -> None:
    _, conversation = seeded
    now = datetime.now(UTC)
    await _add(
        db,
        conversation,
        now - timedelta(minutes=1),
        direction=MessageDirection.OUTBOUND,
        sender=MessageSender.BOT,
        body="Escolha",
        interactive={"kind": "carousel"},
    )
    await _add(
        db,
        conversation,
        now,
        direction=MessageDirection.INBOUND,
        sender=MessageSender.PATIENT,
        body="oi",
    )

    response = await client.get(f"/tenants/me/conversations/{conversation.id}/messages")

    assert response.status_code == 200
    broken, after = response.json()
    assert broken["body"] == "Escolha"
    assert broken["interactive"] is None
    assert after["body"] == "oi"
