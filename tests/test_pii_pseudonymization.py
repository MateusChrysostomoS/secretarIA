"""The scrub/re-hydrate step between this repo and OpenAI.

Covers the two directions that matter to a patient — nothing identifying goes
out un-masked, nothing tokenized comes back — plus the tool boundary, which is
where the ReAct loop would otherwise slip past both.

Real SQLite, not mocks, for the map itself: token STABILITY across turns is the
whole reason the map is persisted, and a mocked store proves nothing about it.
"""

import os
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from pseudonymize_core import Pseudonymizer  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import graph, pii, scoped_help  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    ConversationPiiTokenMap,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services import pii_pseudonymization as store  # noqa: E402

PATIENT_NAME = "Joao da Silva"
PATIENT_PHONE = "5511988887777"
# A DIFFERENT spelling of a phone, typed by the patient mid-conversation, so the
# pattern rules (not the registered wa_id) are what has to catch it.
TYPED_PHONE = "(11) 97777-6666"


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


@pytest.fixture(autouse=True)
def _patch_session_factory(monkeypatch: pytest.MonkeyPatch, db):
    # The store imports the factory at module level (workers/tasks.py's
    # pattern), so the attribute is what has to move.
    monkeypatch.setattr(store, "async_session_factory", db)
    yield


async def _seed(db, *, body: str) -> Conversation:
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinica Teste", phone_number_id="12345")
        session.add(tenant)
        await session.flush()
        patient = Patient(tenant_id=tenant.id, wa_id=PATIENT_PHONE, name=PATIENT_NAME)
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                wam_id="wamid.1",
                body=body,
            )
        )
        await session.commit()
        return conversation


# --------------------------------------------------------------------------
# (a) nothing identifying reaches the model
# --------------------------------------------------------------------------


async def test_patient_name_and_phone_are_masked_before_reaching_the_model(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spy stands exactly where the OpenAI request is built."""
    conversation = await _seed(
        db, body=f"Oi, aqui e o {PATIENT_NAME}, meu telefone e {TYPED_PHONE}"
    )
    seen: list = []

    async def _spy(messages):  # noqa: ANN001
        seen.append(messages)
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _spy)

    async def _history(_cid):  # noqa: ANN001
        return [HumanMessage(content=f"Oi, aqui e o {PATIENT_NAME}, meu telefone e {TYPED_PHONE}")]

    monkeypatch.setattr(graph, "_load_history", _history)

    await graph.run_agent("oi", context={"conversation_id": str(conversation.id)})

    sent = seen[0][0].content
    assert PATIENT_NAME not in sent
    assert "97777-6666" not in sent
    assert "[PACIENTE_" in sent
    assert "[TELEFONE_" in sent
    # The message TYPE has to survive the rewrite - the model reads turn
    # ownership from the class, not from the text.
    assert isinstance(seen[0][0], HumanMessage)


async def test_bot_turns_in_the_history_are_masked_too(db, monkeypatch: pytest.MonkeyPatch) -> None:
    """The clinic's own prior replies quote the patient back by name."""
    conversation = await _seed(db, body="oi")
    seen: list = []

    async def _spy(messages):  # noqa: ANN001
        seen.append(messages)
        return "ok"

    async def _history(_cid):  # noqa: ANN001
        return [
            HumanMessage(content="oi"),
            AIMessage(content=f"Perfeito, {PATIENT_NAME}! Qual dia prefere?"),
        ]

    monkeypatch.setattr(graph, "invoke_agent", _spy)
    monkeypatch.setattr(graph, "_load_history", _history)

    await graph.run_agent("oi", context={"conversation_id": str(conversation.id)})

    assert PATIENT_NAME not in seen[0][1].content
    assert isinstance(seen[0][1], AIMessage)


# --------------------------------------------------------------------------
# (b) nothing tokenized reaches the patient
# --------------------------------------------------------------------------


async def test_reply_is_rehydrated_before_reaching_the_patient(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation = await _seed(db, body=f"Sou o {PATIENT_NAME}")
    captured: dict = {}

    async def _echo_tokens(messages):  # noqa: ANN001
        # The model quotes the patient back - by the token it was shown.
        content = messages[0].content
        captured["sent"] = content
        return f"Combinado, {content.split('Sou o ')[1]}! Ate quinta."

    monkeypatch.setattr(graph, "invoke_agent", _echo_tokens)

    async def _history(_cid):  # noqa: ANN001
        return [HumanMessage(content=f"Sou o {PATIENT_NAME}")]

    monkeypatch.setattr(graph, "_load_history", _history)

    reply = await graph.run_agent("oi", context={"conversation_id": str(conversation.id)})

    assert "[PACIENTE_" in captured["sent"]  # it really was masked on the way out
    assert reply == f"Combinado, {PATIENT_NAME}! Ate quinta."
    assert "[PACIENTE_" not in reply


# --------------------------------------------------------------------------
# the tool boundary - both directions
# --------------------------------------------------------------------------


def _fake_tool(record: dict):
    """A stand-in for create_event: records what actually reached it."""
    from langchain_core.tools import tool as make_tool

    @make_tool
    async def create_event(summary: str, description: str = "") -> dict:
        """Cria um evento na agenda da clinica."""
        record["summary"] = summary
        record["description"] = description
        return {"status": "confirmed", "summary": summary}

    return create_event


async def test_tool_arguments_are_rehydrated_before_they_leave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The doctor's real calendar must never receive a token.

    create_event's own docstring tells the model to title the event
    "Consulta - Joao Silva"; the model takes that name from the masked history,
    so without this guard the clinic's agenda fills up with [PACIENTE_xxxx].
    """
    record: dict = {}
    wrapped = pii.wrap_tools_with_pseudonymizer([_fake_tool(record)])[0]

    p = Pseudonymizer()
    token = p.add_identifier("PACIENTE", PATIENT_NAME)
    tok = store._pseudonymizer_ctx.set(p)
    try:
        await wrapped.ainvoke({"summary": f"Consulta - {token}", "description": ""})
    finally:
        store._pseudonymizer_ctx.reset(tok)

    assert record["summary"] == f"Consulta - {PATIENT_NAME}"
    assert "[PACIENTE_" not in record["summary"]


async def test_tool_results_are_masked_before_re_entering_the_model() -> None:
    """check_availability hands back real Google Calendar event titles."""
    from langchain_core.tools import tool as make_tool

    @make_tool
    async def check_availability(start: str, end: str) -> dict:
        """Lista eventos que conflitam com a janela."""
        return {
            "busy": [
                {
                    "summary": f"Consulta - {PATIENT_NAME}",
                    "start": start,
                    "end": end,
                    "contato": TYPED_PHONE,
                }
            ]
        }

    wrapped = pii.wrap_tools_with_pseudonymizer([check_availability])[0]
    p = Pseudonymizer()
    p.add_identifier("PACIENTE", PATIENT_NAME)
    tok = store._pseudonymizer_ctx.set(p)
    try:
        out = await wrapped.ainvoke({"start": "2026-09-08T10:00", "end": "2026-09-08T11:00"})
    finally:
        store._pseudonymizer_ctx.reset(tok)

    busy = out["busy"][0]
    assert PATIENT_NAME not in busy["summary"]
    assert "[PACIENTE_" in busy["summary"]
    # A phone the map had never seen becomes a NEW entry, found by pattern.
    assert "97777-6666" not in busy["contato"]
    assert "[TELEFONE_" in busy["contato"]


async def test_wrapping_preserves_the_agent_cache_key_and_sentinels() -> None:
    """Name/description/schema must survive, and hand-backs must still raise.

    graph.build_agent keys its cache on tool NAMES and catches the hand-back
    sentinels by TYPE; a wrapper that renamed a tool or swallowed an exception
    would break either silently.
    """
    from langchain_core.tools import tool as make_tool

    from secretaria.ai.tools import ShowMainMenuRequested

    @make_tool
    async def show_main_menu() -> str:
        """Volta ao menu."""
        raise ShowMainMenuRequested()

    wrapped = pii.wrap_tools_with_pseudonymizer([show_main_menu])[0]
    assert wrapped.name == show_main_menu.name
    assert wrapped.description == show_main_menu.description
    assert wrapped.args_schema is show_main_menu.args_schema

    p = Pseudonymizer()
    tok = store._pseudonymizer_ctx.set(p)
    try:
        with pytest.raises(ShowMainMenuRequested):
            await wrapped.ainvoke({})
    finally:
        store._pseudonymizer_ctx.reset(tok)


async def test_tools_are_untouched_outside_a_pseudonymized_turn() -> None:
    """Dev terminal / direct callers behave exactly as before this feature."""
    record: dict = {}
    wrapped = pii.wrap_tools_with_pseudonymizer([_fake_tool(record)])[0]
    out = await wrapped.ainvoke({"summary": f"Consulta - {PATIENT_NAME}", "description": ""})
    assert record["summary"] == f"Consulta - {PATIENT_NAME}"
    assert out["summary"] == f"Consulta - {PATIENT_NAME}"


# --------------------------------------------------------------------------
# the map itself: stability across turns, and its lifetime
# --------------------------------------------------------------------------


async def test_the_same_phone_keeps_its_token_on_a_later_turn(db) -> None:
    """The reason the map is persisted at all.

    Two turns, days apart. If the second turn minted a fresh token for the
    same number, the model would read one patient as two.
    """
    conversation = await _seed(db, body="oi")

    first = await store.load_pseudonymizer(conversation.id)
    masked_once = first.scrub(f"pode me ligar no {TYPED_PHONE}")
    await store.persist_pseudonymizer(conversation.id, first)

    second = await store.load_pseudonymizer(conversation.id)
    masked_twice = second.scrub(f"o {TYPED_PHONE} continua valendo")

    token = masked_once.split("no ")[1]
    assert token.startswith("[TELEFONE_")
    assert token in masked_twice
    # And the round trip still resolves it on the later turn.
    assert second.rehydrate(masked_twice) == f"o {TYPED_PHONE} continua valendo"


async def test_the_patient_name_is_registered_even_when_the_map_is_empty(db) -> None:
    """Name can be filled in AFTER the first turn, so it is re-registered."""
    conversation = await _seed(db, body="oi")
    p = await store.load_pseudonymizer(conversation.id)
    assert PATIENT_NAME in p.tokens.values()
    assert PATIENT_PHONE in p.tokens.values()


async def test_an_empty_map_writes_no_row(db) -> None:
    """A conversation that never tokenized anything gets no bookkeeping row."""
    conversation = await _seed(db, body="oi")
    await store.persist_pseudonymizer(conversation.id, Pseudonymizer())
    async with db() as session:
        assert await session.get(ConversationPiiTokenMap, conversation.id) is None


async def test_a_broken_store_degrades_instead_of_dropping_the_turn(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing to read the map must never cost a patient their turn."""
    conversation = await _seed(db, body="oi")

    def _explode():
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "async_session_factory", _explode)
    p = await store.load_pseudonymizer(conversation.id)
    assert p.tokens == {}
    # Still masks by pattern - the safe direction to fail in.
    assert "[TELEFONE_" in p.scrub(f"ligue {TYPED_PHONE}")
    # And persisting through the same broken store does not raise either.
    await store.persist_pseudonymizer(conversation.id, p)


# --------------------------------------------------------------------------
# the second entry point: ai/scoped_help.py
# --------------------------------------------------------------------------


async def test_scoped_help_masks_the_history_and_rehydrates_the_question(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`clarify.question` is sent to the patient verbatim by the flow router.

    `pick.choice`, by contrast, is copied out of the (never-masked) options
    block in the system prompt, so it needs no re-hydration - covered below.
    """
    conversation = await _seed(db, body=f"Sou o {PATIENT_NAME}, tenho dor no joelho")
    seen: list = []

    class _FakeModel:
        async def ainvoke(self, messages):  # noqa: ANN001
            seen.append(messages)
            name_token = messages[-1].content.split("Sou o ")[1].split(",")[0]
            return scoped_help._ScopedHelpDecision(
                action="clarify", question=f"{name_token}, a dor e ao subir escada?"
            )

    monkeypatch.setattr(scoped_help, "_get_decision_model", lambda: _FakeModel())
    monkeypatch.setattr(scoped_help, "async_session_factory", db)

    outcome = await scoped_help.run_professional_help(
        conversation_id=conversation.id,
        professionals=[type("P", (), {"name": "Dra. Ana", "specialty": "Ortopedia"})()],
        patient_message="tenho dor no joelho",
        final_round=False,
    )

    assert PATIENT_NAME not in seen[0][-1].content
    assert "[PACIENTE_" in seen[0][-1].content
    assert outcome.kind == "clarify"
    assert outcome.question == f"{PATIENT_NAME}, a dor e ao subir escada?"
    assert "[PACIENTE_" not in outcome.question


async def test_scoped_help_without_a_conversation_id_masks_and_saves_nothing(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Router unit tests / dev snapshots have no key to persist under."""
    seen: list = []

    class _FakeModel:
        async def ainvoke(self, messages):  # noqa: ANN001
            seen.append(messages)
            return scoped_help._ScopedHelpDecision(action="pick", choice="Dra. Ana")

    monkeypatch.setattr(scoped_help, "_get_decision_model", lambda: _FakeModel())

    outcome = await scoped_help.run_professional_help(
        conversation_id=None,
        professionals=[type("P", (), {"name": "Dra. Ana", "specialty": "Ortopedia"})()],
        patient_message=f"meu telefone e {TYPED_PHONE}",
        final_round=False,
    )

    assert "97777-6666" not in seen[0][-1].content
    assert "[TELEFONE_" in seen[0][-1].content
    # `choice` comes from the un-masked options block, so it survives as-is.
    assert outcome.kind == "pick"
    assert outcome.choice == "Dra. Ana"
    async with db() as session:
        assert (await session.get(ConversationPiiTokenMap, uuid4())) is None


# --------------------------------------------------------------------------
# check_availability: agenda compartilhada, título alheio não passa
# --------------------------------------------------------------------------


class _CalendarWithBusy:
    """Devolve a forma crua do Google: id + summary + intervalo."""

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    async def check_availability(self, start, end):  # noqa: ANN001
        return self._events


async def _busy_via_tool(db, events: list[dict], *, conversation, tenant_id) -> list[dict]:
    from secretaria.ai import tools as ai_tools

    toks = [
        ai_tools._calendar_ctx.set(_CalendarWithBusy(events)),
        ai_tools._tenant_id_ctx.set(tenant_id),
        ai_tools._conversation_id_ctx.set(conversation.id if conversation else None),
    ]
    try:
        out = await ai_tools.check_availability.ainvoke(
            {"start": "2026-09-08T10:00:00", "end": "2026-09-08T11:00:00"}
        )
    finally:
        ai_tools._calendar_ctx.reset(toks[0])
        ai_tools._tenant_id_ctx.reset(toks[1])
        ai_tools._conversation_id_ctx.reset(toks[2])
    return out["busy"]


async def test_foreign_event_returns_only_the_interval(db, monkeypatch) -> None:
    """O nome de outro paciente na agenda da clínica não chega ao modelo."""
    from secretaria.core import database as core_database

    conversation = await _seed(db, body="oi")
    monkeypatch.setattr(core_database, "async_session_factory", db)

    busy = await _busy_via_tool(
        db,
        [
            {
                "id": "evt-de-outra-pessoa",
                "summary": "Consulta - Maria Silva",
                "start": "2026-09-08T10:00:00-03:00",
                "end": "2026-09-08T10:30:00-03:00",
            }
        ],
        conversation=conversation,
        tenant_id=conversation.tenant_id,
    )

    assert busy == [
        {
            "start": "2026-09-08T10:00:00-03:00",
            "end": "2026-09-08T10:30:00-03:00",
            "do_paciente": False,
        }
    ]
    # Nem o nome, nem o id que cancel_event aceitaria.
    payload = repr(busy)
    assert "Maria Silva" not in payload
    assert "evt-de-outra-pessoa" not in payload


async def test_the_patients_own_event_keeps_its_summary(db, monkeypatch) -> None:
    """A consulta do próprio paciente continua citável — esse é o caso legítimo."""
    from secretaria.core import database as core_database
    from secretaria.models import Appointment, AppointmentStatus

    conversation = await _seed(db, body="oi")
    monkeypatch.setattr(core_database, "async_session_factory", db)

    from datetime import UTC, datetime

    async with db() as session:
        session.add(
            Appointment(
                tenant_id=conversation.tenant_id,
                patient_id=conversation.patient_id,
                conversation_id=conversation.id,
                google_event_id="evt-do-proprio",
                appointment_type="Consulta",
                start_at=datetime(2026, 9, 8, 13, 0, tzinfo=UTC),
                end_at=datetime(2026, 9, 8, 13, 30, tzinfo=UTC),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()

    busy = await _busy_via_tool(
        db,
        [
            {
                "id": "evt-do-proprio",
                "summary": f"Consulta - {PATIENT_NAME}",
                "start": "2026-09-08T10:00:00-03:00",
                "end": "2026-09-08T10:30:00-03:00",
            }
        ],
        conversation=conversation,
        tenant_id=conversation.tenant_id,
    )

    assert busy[0]["do_paciente"] is True
    assert busy[0]["summary"] == f"Consulta - {PATIENT_NAME}"
    assert busy[0]["id"] == "evt-do-proprio"


async def test_without_patient_context_everything_is_hidden(db) -> None:
    """Falha FECHADA: sem contexto, todo evento é tratado como de terceiro."""
    busy = await _busy_via_tool(
        db,
        [{"id": "evt-x", "summary": "Consulta - Maria Silva", "start": "s", "end": "e"}],
        conversation=None,
        tenant_id=None,
    )
    assert busy == [{"start": "s", "end": "e", "do_paciente": False}]
