"""TASK-082 S1 - how a paged catalogue card is sent, recorded and projected.

`_send_bubble` decides per channel (CONTRACT.md §c6): a sender with
`SUPPORTS_OPTION_SOURCE` (the Portal) records page 1 (full titles, no 24-char cut)
+ the pinned rows + the pointer; any other sender (WhatsApp) gets the bubble's own
rows exactly as before and never sees `source`. The wire (`InteractiveRead`) carries
`source` without `v`/`scope`.
"""

import os

os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai.formatter import OptionSource, SlotsBubble  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Conversation, Message, Patient, Tenant  # noqa: E402
from secretaria.schemas.conversation import interactive_read_or_none  # noqa: E402
from secretaria.services import list_options as lo  # noqa: E402
from secretaria.services.channel_sender import (  # noqa: E402
    CHANNEL_BRAIN_MESSAGE,
    BrainMessageSender,
)
from secretaria.services.whatsapp import WhatsAppClient  # noqa: E402
from secretaria.workers.shared.bubbles import (  # noqa: E402
    _bubble_interactive,
    _send_bubble,
)

LONG = "Consulta oftalmológica completa com mapeamento de retina"
NAMES = [LONG] + [f"Serviço {i:02d}" for i in range(1, 25)]
PORTAL_ROWS = [(f"svc|{n}", n, None) for n in NAMES]
WHATSAPP_ROWS = [(f"svc|{n}", n[:24], None) for n in NAMES[:9]] + [("svchelp|0", "Não sei", None)]


def _bubble(items=PORTAL_ROWS) -> SlotsBubble:
    return SlotsBubble(
        body="Qual serviço você gostaria de agendar?",
        rows=list(WHATSAPP_ROWS),
        button_label="Ver serviços",
        section_title="Serviços",
        source=OptionSource(
            kind="service",
            scope={"list": "doctor", "professional_id": "11111111-1111-1111-1111-111111111111"},
            items=list(items),
            pinned=[("svchelp|0", "Não sei", None)],
        ),
    )


@pytest_asyncio.fixture
async def conversation_id():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session, session.begin():
        tenant = Tenant(id=uuid4(), clinic_name="C", phone_number_id="1")
        session.add(tenant)
        await session.flush()
        patient = Patient(tenant_id=tenant.id, channel=CHANNEL_BRAIN_MESSAGE, external_id="p1")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        cid = conversation.id
    yield maker, cid
    await engine.dispose()


async def _only_row(maker, cid) -> Message:
    async with maker() as session:
        [row] = (await session.scalars(select(Message).where(Message.conversation_id == cid))).all()
        return row


async def test_the_portal_records_page_one_full_titles_and_the_pointer(conversation_id) -> None:
    maker, cid = conversation_id
    sender = BrainMessageSender(conversation_id=cid, session_factory=maker)
    assert sender.SUPPORTS_OPTION_SOURCE is True

    await _send_bubble(sender, "p1", _bubble())

    row = await _only_row(maker, cid)
    card = row.interactive
    assert [o["id"] for o in card["options"]] == [r[0] for r in PORTAL_ROWS[:10]] + ["svchelp|0"]
    assert card["options"][0]["title"] == LONG  # no 24-char cut on the Portal
    assert card["source"] == {
        "v": 1,
        "kind": "service",
        "scope": {"list": "doctor", "professional_id": "11111111-1111-1111-1111-111111111111"},
        "pinned_ids": ["svchelp|0"],
        "total": 25,
        "next_cursor": lo.encode_cursor(10),
    }
    # The agent's history does not believe the list ended at ten.
    assert "e mais 15" in row.body
    assert "Serviço 10" not in row.body


async def test_a_short_list_has_no_cursor_and_no_e_mais(conversation_id) -> None:
    maker, cid = conversation_id
    sender = BrainMessageSender(conversation_id=cid, session_factory=maker)
    await _send_bubble(sender, "p1", _bubble(PORTAL_ROWS[:4]))
    row = await _only_row(maker, cid)
    assert row.interactive["source"]["next_cursor"] is None
    assert row.interactive["source"]["total"] == 4
    assert len(row.interactive["options"]) == 5
    assert "e mais" not in row.body


async def test_a_portal_list_without_a_pointer_is_recorded_as_before(conversation_id) -> None:
    maker, cid = conversation_id
    sender = BrainMessageSender(conversation_id=cid, session_factory=maker)
    bubble = _bubble()
    bubble.source = None
    await _send_bubble(sender, "p1", bubble)
    row = await _only_row(maker, cid)
    assert "source" not in row.interactive
    assert [o["id"] for o in row.interactive["options"]] == [r[0] for r in WHATSAPP_ROWS]


class _RecordingClient:
    """A WhatsApp-shaped client: no SUPPORTS_OPTION_SOURCE, records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_list(self, to, body, button_label, rows, section_title="Opções", **kwargs):
        self.calls.append({"rows": rows, "kwargs": kwargs})
        return {}


async def test_whatsapp_gets_the_bubbles_own_rows_and_never_the_pointer() -> None:
    client = _RecordingClient()
    await _send_bubble(client, "5511999999999", _bubble())
    [call] = client.calls
    assert call["rows"] == WHATSAPP_ROWS
    assert call["kwargs"] == {}
    assert not getattr(WhatsAppClient, "SUPPORTS_OPTION_SOURCE", False)
    # The WhatsApp history record never carries the pointer either.
    record = _bubble_interactive(_bubble())
    assert "source" not in record
    assert [o["id"] for o in record["options"]] == [r[0] for r in WHATSAPP_ROWS]


def test_the_wire_carries_source_without_version_or_scope() -> None:
    stored = {
        "kind": "list",
        "body": "b",
        "options": [{"id": "svc|A", "title": "A", "description": None}],
        "source": {
            "v": 1,
            "kind": "professional",
            "scope": {"insurance": "Unimed"},
            "pinned_ids": ["profhelp|0"],
            "total": 37,
            "next_cursor": "eyJ2IjoxLCJvIjoxMH0",
        },
    }
    wire = interactive_read_or_none(stored, message_id="m").model_dump()
    assert wire["source"] == {
        "kind": "professional",
        "total": 37,
        "pinned_ids": ["profhelp|0"],
        "next_cursor": "eyJ2IjoxLCJvIjoxMH0",
    }


def test_an_old_card_has_no_source_on_the_wire() -> None:
    stored = {"kind": "list", "body": "b", "options": [{"id": "x", "title": "X"}]}
    read = interactive_read_or_none(stored, message_id="m")
    assert read.source is None
    # Absent, not null: a card that does not page keeps today's exact shape.
    assert "source" not in read.model_dump()
    assert "source" not in read.model_dump(mode="json")


def test_a_broken_source_costs_the_card_only_its_pointer() -> None:
    stored = {
        "kind": "list",
        "body": "b",
        "options": [{"id": "x", "title": "X"}],
        "source": {"kind": "service", "total": "lots"},
    }
    read = interactive_read_or_none(stored, message_id="m")
    assert read is not None and read.source is None
