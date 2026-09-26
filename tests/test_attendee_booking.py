"""Booking for someone else: "Essa consulta é pra você?" (services/attendee.py).

Origin: z_prompts/PROMPT_AGENDAR_PARA_TERCEIRO_SECRETARIA.md. The owner's
symptom, closed one proof at a time:

    "não existe pergunta nem fluxo pra marcar consulta pra outra pessoa,
     nem a frase de autorização."

1. Router level (pure, fake calendar): the "é pra mim" path books EXACTLY what
   it booked before; the "outra pessoa" path asks the name, shows the owner's
   sentence with the name in it, needs an explicit Confirmar, "Cancelar" goes
   back one step, and the appointment / Google event / confirmation carry the
   ATTENDEE's name.
2. Worker level (real `_persist_inbound_message` on SQLite): the consent row is
   written exactly once, never on the "é pra mim" path, and the appointment row
   gets the column.
3. The PII proof: the third party's name reaches OpenAI only as an ATENDIDO
   token - through the real history load and the real pseudonymizer.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import graph  # noqa: E402
from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.core.whatsapp_limits import MAX_BUTTON_LABEL_CHARS  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    ConsentEvent,
    Conversation,
    FlowState,
    Message,
    Patient,
    Tenant,
)
from secretaria.plugins import professional_notification, reminders  # noqa: E402
from secretaria.services import pii_pseudonymization as pii_store  # noqa: E402
from secretaria.services.attendee import (  # noqa: E402
    ATTENDEE_NAME_INVALID,
    ATTENDEE_NAME_LLM_PLACEHOLDER,
    ATTENDEE_NAME_REQUEST,
    ATTENDEE_QUESTION_BODY,
    CONSENT_KIND_THIRD_PARTY_BOOKING,
    LABEL_ATTENDEE_AUTH_BACK,
    LABEL_ATTENDEE_AUTH_CONFIRM,
    LABEL_ATTENDEE_OTHER,
    LABEL_ATTENDEE_SELF,
    authorization_body,
)
from secretaria.services.channel_sender import CHANNEL_WHATSAPP  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    LABEL_BOOK,
    LABEL_BOOK_SERVICE,
    LABEL_CONFIRM,
    STEP_AWAITING_ATTENDEE_AUTH,
    STEP_AWAITING_ATTENDEE_CHOICE,
    STEP_AWAITING_ATTENDEE_NAME,
    STEP_AWAITING_SERVICE,
    FlowRouterResult,
    route,
)
from secretaria.services.greeting_template import CONSENT_BUTTON_LABEL  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

_TZ = ZoneInfo("America/Sao_Paulo")


# --------------------------------------------------------------------------
# 1. Router level
# --------------------------------------------------------------------------


def _tenant():
    return SimpleNamespace(
        initial_flows={},
        appointment_types=[
            {"name": "Primeira Consulta", "duration_min": 40, "is_active": True, "sort_order": 0}
        ],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        collect_insurance=False,
        insurances=[],
    )


class _FakeCalendar:
    def __init__(self):
        self.tzinfo = _TZ
        self.created: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(days)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        return [{"start": "2026-06-15T08:00:00-03:00", "label": "Seg 15/06 08:00"}]

    async def create_event(self, start, end, summary, description=""):
        self.created.append((start, end, summary))
        return {"id": "evt123", "htmlLink": "https://cal/evt123"}


def _snapshot(result: FlowRouterResult) -> SimpleNamespace:
    """The conversation as `_apply_flow_result` would leave it after `result`."""
    return SimpleNamespace(
        id=uuid4(),
        flow_state=result.flow_state,
        flow_step=result.flow_step,
        flow_selected_type=result.flow_selected_type,
        flow_selected_day=result.flow_selected_day,
        flow_selected_slot=result.flow_selected_slot,
        flow_selected_professional_id=result.flow_selected_professional_id,
        flow_selected_insurance=result.flow_selected_insurance,
        flow_managing_appointment_id=result.flow_managing_appointment_id,
        flow_attendee_name=result.flow_attendee_name,
        patient_id=None,
    )


async def _drive(taps: list[str], cal: _FakeCalendar, results=None) -> list[FlowRouterResult]:
    """Feed `taps` through `route()`, threading the state like the worker does."""
    results = results if results is not None else []
    conversation = _snapshot(
        results[-1] if results else FlowRouterResult(action="reply", flow_state=FlowState.IDLE)
    )
    for tap in taps:
        result = await route(conversation, _tenant(), cal, tap, patient_name="João Conta")
        results.append(result)
        conversation = _snapshot(result)
    return results


def _row_tap(result: FlowRouterResult) -> str:
    """What tapping the first row of the result's list echoes back."""
    bubble = next(b for b in result.bubbles if isinstance(b, SlotsBubble))
    row_id, title = bubble.rows[0][0], bubble.rows[0][1]
    return f"{title} ({row_id.split('|', 1)[1]})"


async def _book(first_taps: list[str]) -> tuple[list[FlowRouterResult], _FakeCalendar]:
    """Run `first_taps`, then service -> Sim -> first day -> first slot -> Confirmar."""
    cal = _FakeCalendar()
    results = await _drive(first_taps, cal)
    assert results[-1].flow_step == STEP_AWAITING_SERVICE
    await _drive(["Primeira Consulta", LABEL_BOOK_SERVICE], cal, results)
    await _drive([_row_tap(results[-1])], cal, results)  # day
    await _drive([_row_tap(results[-1])], cal, results)  # slot
    await _drive([LABEL_CONFIRM], cal, results)
    return results, cal


def test_the_buttons_fit_whatsapp_and_the_sentence_is_the_owners() -> None:
    for label in (
        LABEL_ATTENDEE_SELF,
        LABEL_ATTENDEE_OTHER,
        LABEL_ATTENDEE_AUTH_CONFIRM,
        LABEL_ATTENDEE_AUTH_BACK,
    ):
        assert len(label) <= MAX_BUTTON_LABEL_CHARS, label
    assert authorization_body("Maria da Silva") == (
        "Ao informar os dados de *Maria da Silva*, você confirma que tem autorização "
        "para compartilhar essas informações com a clínica."
    )


async def test_agendar_asks_pra_quem_first() -> None:
    [result] = await _drive([LABEL_BOOK], _FakeCalendar())
    assert result.flow_state == FlowState.SERVICE_CATALOG
    assert result.flow_step == STEP_AWAITING_ATTENDEE_CHOICE
    [bubble] = result.bubbles
    assert bubble.body == ATTENDEE_QUESTION_BODY
    assert bubble.labels == [LABEL_ATTENDEE_SELF, LABEL_ATTENDEE_OTHER]


async def test_self_path_books_exactly_what_it_booked_before() -> None:
    """VALIDATION 1: "Sim, é pra mim" -> same appointment dict, same event, no attendee."""
    results, cal = await _book([LABEL_BOOK, LABEL_ATTENDEE_SELF])
    final = results[-1]
    assert final.appointment is not None
    assert set(final.appointment) == {
        "google_event_id",
        "google_event_link",
        "appointment_type",
        "start_at",
        "end_at",
    }
    assert cal.created[0][2] == "Primeira Consulta - João Conta"
    assert "Paciente:" not in final.bubbles[0].body
    assert not any(r.attendee_authorized for r in results)
    assert all(r.flow_attendee_name is None for r in results)


async def test_other_path_name_sentence_confirm_and_back() -> None:
    """VALIDATION 2: name -> sentence with the name -> explicit confirm; Cancelar backs up."""
    results = await _drive(
        [
            LABEL_BOOK,
            LABEL_ATTENDEE_OTHER,
            "Oi, tudo bem?",  # not a name: re-asked, never delegated
            "maria da silva",
            LABEL_ATTENDEE_AUTH_BACK,  # back one step: pra-quem again, name dropped
            LABEL_ATTENDEE_OTHER,
            "maria da silva",
            LABEL_ATTENDEE_AUTH_CONFIRM,
        ],
        _FakeCalendar(),
    )
    ask, name_req, reask, card, back, name_req2, card2, confirmed = results
    assert ask.flow_step == STEP_AWAITING_ATTENDEE_CHOICE
    assert name_req.flow_step == STEP_AWAITING_ATTENDEE_NAME
    assert name_req.bubbles == [TextBubble(body=ATTENDEE_NAME_REQUEST)]
    assert reask.action == "reply"
    assert reask.bubbles == [TextBubble(body=ATTENDEE_NAME_INVALID)]
    # The sentence, with the WHOLE normalized name, as a two-button question.
    assert card.flow_step == STEP_AWAITING_ATTENDEE_AUTH
    [bubble] = card.bubbles
    assert isinstance(bubble, ButtonBubble)
    assert bubble.body == authorization_body("Maria da Silva")
    assert (bubble.confirm_label, bubble.cancel_label) == (
        LABEL_ATTENDEE_AUTH_CONFIRM,
        LABEL_ATTENDEE_AUTH_BACK,
    )
    assert card.flow_attendee_name == "Maria da Silva"
    assert not card.attendee_authorized  # shown is not accepted
    # Cancelar: back to pra-quem, the booking entry kept, the name gone.
    assert back.flow_step == STEP_AWAITING_ATTENDEE_CHOICE
    assert back.flow_selected_type == ask.flow_selected_type
    assert back.flow_attendee_name is None
    assert name_req2.flow_step == STEP_AWAITING_ATTENDEE_NAME
    assert card2.flow_step == STEP_AWAITING_ATTENDEE_AUTH
    # Confirmar: the booking continues exactly where "é pra mim" would.
    assert confirmed.attendee_authorized is True
    assert confirmed.flow_step == STEP_AWAITING_SERVICE
    assert confirmed.flow_attendee_name == "Maria da Silva"
    assert sum(r.attendee_authorized for r in results) == 1


async def test_other_path_books_for_the_attendee() -> None:
    """VALIDATION 2+3: appointment, Google event and confirmation name the attendee."""
    results, cal = await _book(
        [LABEL_BOOK, LABEL_ATTENDEE_OTHER, "maria da silva", LABEL_ATTENDEE_AUTH_CONFIRM]
    )
    # Carried through service, day, slot and recap without any builder naming it.
    assert all(r.flow_attendee_name == "Maria da Silva" for r in results[3:-1])
    final = results[-1]
    assert final.appointment["attendee_name"] == "Maria da Silva"
    assert cal.created[0][2] == "Primeira Consulta - Maria da Silva"
    confirmation = final.bubbles[0].body
    assert "Paciente: Maria da Silva" in confirmation
    # The booking ended: the attendee is dropped from the conversation.
    assert final.flow_state == FlowState.IDLE
    assert final.flow_attendee_name is None
    # The recap card before Confirmar named them too.
    assert "Paciente: Maria da Silva" in results[-2].bubbles[0].body


def test_reminder_and_doctor_email_name_the_attendee() -> None:
    """VALIDATION 3: the other two messages that name the booking's patient."""
    tenant = SimpleNamespace(timezone="America/Sao_Paulo", clinic_name="Clínica X")
    appt = SimpleNamespace(
        appointment_type="Primeira Consulta",
        start_at=datetime(2026, 6, 15, 11, 0, tzinfo=UTC),
        attendee_name="Maria da Silva",
    )
    assert reminders._render_reminder_text(tenant, appt) == (
        "Lembrete: Maria da Silva tem Primeira Consulta agendado(a) para "
        "15/06/2026 às 08:00 na Clínica X."
    )
    account = SimpleNamespace(name="João Conta")
    assert professional_notification._patient_line_name(appt, account) == (
        "Maria da Silva (agendado por João Conta)"
    )
    appt.attendee_name = None
    assert professional_notification._patient_line_name(appt, account) == "João Conta"
    assert reminders._render_reminder_text(tenant, appt).startswith("Lembrete: você tem ")


# --------------------------------------------------------------------------
# 2. Worker level: the consent row and the appointment column
# --------------------------------------------------------------------------

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"


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


class _WireClient:
    sends: list[tuple] = []

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        _WireClient.sends.append(("text", body, None))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}

    async def send_buttons(self, to, body, buttons):
        _WireClient.sends.append(("buttons", body, [label for _id, label in buttons]))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        _WireClient.sends.append(("list", body, None))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    monkeypatch.setattr(graph, "async_session_factory", db)
    monkeypatch.setattr(pii_store, "async_session_factory", db)
    monkeypatch.setattr(tasks, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    _WireClient.sends = []
    monkeypatch.setattr(tasks, "WhatsAppClient", _WireClient)

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

    monkeypatch.setattr(tasks, "resolve_patient_opening_state", _fake_resolve)
    monkeypatch.setattr(tasks, "get_waba_token", _fake_token)
    monkeypatch.setattr(tasks, "get_entitlements", _fake_entitlements)
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
    return reply


async def _onboard(tenant) -> None:
    """First contact: greeting -> the patient's OWN name -> LGPD accepted."""
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


async def test_worker_other_path_writes_one_consent_row(wired) -> None:
    """VALIDATION 4: exactly one ConsentEvent, at the Confirmar tap, with the subject ref."""
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)

    await _wa_turn(tenant, LABEL_BOOK)
    assert _WireClient.sends[-1] == (
        "buttons",
        ATTENDEE_QUESTION_BODY,
        [LABEL_ATTENDEE_SELF, LABEL_ATTENDEE_OTHER],
    )
    await _wa_turn(tenant, LABEL_ATTENDEE_OTHER)
    await _wa_turn(tenant, "maria da silva")
    assert _WireClient.sends[-1][1] == authorization_body("Maria da Silva")
    assert await _consents(db, tenant) == 0  # shown, not accepted
    assert (await _conversation(db, tenant)).flow_attendee_name == "Maria da Silva"

    await _wa_turn(tenant, LABEL_ATTENDEE_AUTH_CONFIRM)
    assert await _consents(db, tenant) == 1
    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SERVICE
    assert conversation.flow_attendee_name == "Maria da Silva"
    async with db() as session:
        row = await session.scalar(
            select(ConsentEvent).where(ConsentEvent.kind == CONSENT_KIND_THIRD_PARTY_BOOKING)
        )
    assert row.wa_id == WA_ID
    assert row.legal_basis.startswith("TODO_LAWYER")

    # The next booking step does not write a second row.
    await _wa_turn(tenant, "Primeira Consulta")
    assert await _consents(db, tenant) == 1


async def test_worker_self_path_writes_no_consent_row(wired) -> None:
    """VALIDATION 4: never on the "é pra mim" path."""
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _wa_turn(tenant, LABEL_BOOK)
    await _wa_turn(tenant, LABEL_ATTENDEE_SELF)
    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SERVICE
    assert conversation.flow_attendee_name is None
    assert await _consents(db, tenant) == 0


async def test_worker_appointment_row_gets_the_attendee_column(wired) -> None:
    """The dict the router returns lands on `appointments.attendee_name`."""
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    conversation = await _conversation(db, tenant)
    reply = tasks._ReplyContext(
        channel=CHANNEL_WHATSAPP,
        conversation_id=conversation.id,
        patient_ref=WA_ID,
        inbound_body=LABEL_CONFIRM,
        tenant_id=tenant.id,
    )
    start = datetime(2026, 6, 15, 11, 0, tzinfo=UTC)
    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="ok")],
        flow_state=FlowState.IDLE,
        appointment={
            "google_event_id": "evt-attendee",
            "appointment_type": "Primeira Consulta",
            "start_at": start,
            "end_at": start + timedelta(minutes=40),
            "attendee_name": "Maria da Silva",
        },
    )
    await tasks._apply_flow_result(reply, result, WA_ID, redis=None, tenant=tenant, waba_token="t")
    async with db() as session:
        appt = await session.scalar(
            select(Appointment).where(Appointment.google_event_id == "evt-attendee")
        )
    assert appt.attendee_name == "Maria da Silva"
    assert appt.patient_id == conversation.patient_id  # the account still owns it


async def test_worker_stale_attendee_step_expires(wired) -> None:
    """The name step accepts free text, so it gets the universal silence floor."""
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _wa_turn(tenant, LABEL_BOOK)
    await _wa_turn(tenant, LABEL_ATTENDEE_OTHER)
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            await session.execute(
                update(Message)
                .where(Message.conversation_id == conversation.id)
                .values(created_at=datetime.now(UTC) - timedelta(days=3))
            )
    # Days later: a greeting must NOT be read as a third party's name.
    await _wa_turn(tenant, "Maria bom dia")
    conversation = await _conversation(db, tenant)
    assert conversation.flow_step not in (
        STEP_AWAITING_ATTENDEE_NAME,
        STEP_AWAITING_ATTENDEE_AUTH,
    )
    assert conversation.flow_attendee_name is None
    assert await _consents(db, tenant) == 0


# --------------------------------------------------------------------------
# 3. The PII proof: the attendee's name reaches OpenAI only as a token
# --------------------------------------------------------------------------


async def _llm_history(db, tenant, monkeypatch, body: str) -> str:
    """Move the conversation to LLM, route one inbound, return what OpenAI gets.

    SQLite stores `created_at` to the second, so the rows are re-stamped in
    insertion order first: the redaction depends on which bot row precedes an
    answer (same technique as tests/test_patient_name_step.py).
    """
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE messages SET created_at = "
                    "datetime('now', '-300 seconds', '+' || rowid || ' seconds')"
                )
            )
            row = await session.get(Conversation, conversation.id)
            row.flow_state = FlowState.LLM
    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Perfil",
        wam_id=f"wamid.llm.{next(_wam_seq)}",
        body=body,
    )
    assert reply is not None
    seen: list = []

    async def _spy(messages):  # noqa: ANN001
        seen.append(messages)
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _spy)
    await graph.run_agent(reply.inbound_body, context={"conversation_id": str(conversation.id)})
    return "\n".join(str(message.content) for message in seen[0])


async def test_attendee_name_reaches_the_llm_only_as_a_token(wired, monkeypatch) -> None:
    """VALIDATION 5: captured through the real flow, masked by the real pipeline.

    The name sits raw in stored rows: the typed answer, the authorization
    sentence the bot sent back, and the patient's own mention in the LLM turn.
    None of them may reach the model raw - nor the REFUSED first answer.
    """
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _wa_turn(tenant, LABEL_BOOK)
    await _wa_turn(tenant, LABEL_ATTENDEE_OTHER)
    await _wa_turn(tenant, "Minha mãe, Beatriz")  # refused (a list), re-asked
    assert _WireClient.sends[-1] == ("text", ATTENDEE_NAME_INVALID, None)
    await _wa_turn(tenant, "maria da silva")
    await _wa_turn(tenant, LABEL_ATTENDEE_AUTH_CONFIRM)

    history = await _llm_history(
        db, tenant, monkeypatch, "A consulta da Maria da Silva é em jejum?"
    )
    assert "[ATENDIDO_" in history
    assert "maria da silva" not in history.casefold()
    assert "beatriz" not in history.casefold()
    # Both answers to the attendee-name question reach the model redacted.
    assert history.count(ATTENDEE_NAME_LLM_PLACEHOLDER) == 2


async def test_attendee_name_stays_masked_after_the_booking_ends(wired, monkeypatch) -> None:
    """VALIDATION 5: the flow field is cleared at booking; the appointment keeps masking."""
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _wa_turn(tenant, LABEL_BOOK)
    await _wa_turn(tenant, LABEL_ATTENDEE_OTHER)
    await _wa_turn(tenant, "maria da silva")
    await _wa_turn(tenant, LABEL_ATTENDEE_AUTH_CONFIRM)
    conversation = await _conversation(db, tenant)
    start = datetime(2026, 6, 15, 11, 0, tzinfo=UTC)
    async with db() as session:
        async with session.begin():
            row = await session.get(Conversation, conversation.id)
            row.flow_attendee_name = None  # what the booking's IDLE result does
            session.add(
                Appointment(
                    tenant_id=tenant.id,
                    patient_id=conversation.patient_id,
                    conversation_id=conversation.id,
                    google_event_id="evt-done",
                    start_at=start,
                    end_at=start + timedelta(minutes=40),
                    attendee_name="Maria da Silva",
                )
            )
    history = await _llm_history(db, tenant, monkeypatch, "Obrigado!")
    assert "[ATENDIDO_" in history  # the authorization sentence the bot sent
    assert "maria da silva" not in history.casefold()


async def test_patient_name_is_still_the_pacient_token(wired, monkeypatch) -> None:
    """Registering ATENDIDO must not disturb the patient's own PACIENTE token."""
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    async with db() as session:
        patient = await session.scalar(select(Patient).where(Patient.tenant_id == tenant.id))
    assert patient.name == "João Conta"
    history = await _llm_history(db, tenant, monkeypatch, "Aqui é o João Conta")
    assert "[PACIENTE_" in history
    assert "[ATENDIDO_" not in history
    assert "joão conta" not in history.casefold()


# --------------------------------------------------------------------------
# 4. Review findings (TASK-007 REVIEW): leaks across bookings and hand-backs
# --------------------------------------------------------------------------


async def test_cancelled_authorization_still_masks_the_name(wired, monkeypatch) -> None:
    """Finding 2: "Cancelar" leaves no row, yet the card in history holds the name."""
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _wa_turn(tenant, LABEL_BOOK)
    await _wa_turn(tenant, LABEL_ATTENDEE_OTHER)
    await _wa_turn(tenant, "maria da silva")
    await _wa_turn(tenant, LABEL_ATTENDEE_AUTH_BACK)
    conversation = await _conversation(db, tenant)
    assert conversation.flow_attendee_name is None
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Appointment)) == 0
    history = await _llm_history(db, tenant, monkeypatch, "quanto custa?")
    assert "[ATENDIDO_" in history  # the authorization card, masked
    assert "maria da silva" not in history.casefold()


async def test_llm_expiry_drops_the_attendee() -> None:
    """Finding 1: a quiet LLM conversation must not keep a third party's name."""
    conversation = SimpleNamespace(
        flow_state=FlowState.LLM,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_managing_appointment_id=None,
        flow_attendee_name="Maria da Silva",
    )
    tenant = SimpleNamespace(initial_flows={})
    stale = datetime.now(UTC) - timedelta(days=3)
    assert tasks._expire_stale_llm_state(conversation, tenant, stale) is True
    assert conversation.flow_attendee_name is None


async def test_chat_booking_consumes_the_attendee(wired, monkeypatch) -> None:
    """Finding 1: after the LLM books for Maria, the next chat booking is the patient's."""
    from secretaria.ai import tools
    from secretaria.core import database

    db = wired
    monkeypatch.setattr(database, "async_session_factory", db)
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    conversation = await _conversation(db, tenant)
    async with db() as session:
        async with session.begin():
            row = await session.get(Conversation, conversation.id)
            row.flow_attendee_name = "Maria da Silva"
    tenant_token = tools._tenant_id_ctx.set(tenant.id)
    conv_token = tools._conversation_id_ctx.set(conversation.id)
    try:
        assert await tools._conversation_attendee_name() == "Maria da Silva"
        start = datetime(2026, 6, 15, 11, 0, tzinfo=UTC)
        await tools._persist_appointment(
            {"id": "evt-chat"},
            start,
            start + timedelta(minutes=40),
            "Primeira Consulta",
            attendee_name="Maria da Silva",
        )
        assert await tools._conversation_attendee_name() is None
    finally:
        tools._conversation_id_ctx.reset(conv_token)
        tools._tenant_id_ctx.reset(tenant_token)
    async with db() as session:
        appt = await session.scalar(
            select(Appointment).where(Appointment.google_event_id == "evt-chat")
        )
    assert appt.attendee_name == "Maria da Silva"


async def test_llm_choose_doctor_hand_back_keeps_the_attendee(wired, monkeypatch) -> None:
    """Finding 3: `select_professional_and_continue` bypasses route()."""
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    conversation = await _conversation(db, tenant)
    professional = SimpleNamespace(
        id=uuid4(),
        name="Dra. Ana",
        specialty="Cardiologia",
        about=None,
        appointment_types=[{"name": "Primeira Consulta", "duration_min": 40, "is_active": True}],
        business_hours=None,
    )
    reply = tasks._ReplyContext(
        channel=CHANNEL_WHATSAPP,
        conversation_id=conversation.id,
        patient_ref=WA_ID,
        inbound_body="quero a Dra. Ana",
        tenant_id=tenant.id,
    )
    snapshot = (SimpleNamespace(flow_attendee_name="Maria da Silva"), _tenant())
    await tasks._handle_select_professional(
        reply,
        f"{tasks.SELECT_PROFESSIONAL_SENTINEL_PREFIX}{professional.id}",
        tenant,
        snapshot,
        [professional],
        WA_ID,
        waba_token="t",
    )
    conversation = await _conversation(db, tenant)
    assert conversation.flow_state == FlowState.SERVICE_CATALOG
    assert conversation.flow_attendee_name == "Maria da Silva"


async def test_stale_booking_with_an_attendee_expires(wired) -> None:
    """Finding 5: after Confirmar, a list left open for days drops the booking."""
    db = wired
    tenant = await _seed_tenant(db)
    await _onboard(tenant)
    await _wa_turn(tenant, LABEL_BOOK)
    await _wa_turn(tenant, LABEL_ATTENDEE_OTHER)
    await _wa_turn(tenant, "maria da silva")
    await _wa_turn(tenant, LABEL_ATTENDEE_AUTH_CONFIRM)
    conversation = await _conversation(db, tenant)
    assert conversation.flow_step == STEP_AWAITING_SERVICE
    async with db() as session:
        async with session.begin():
            await session.execute(
                update(Message)
                .where(Message.conversation_id == conversation.id)
                .values(created_at=datetime.now(UTC) - timedelta(days=3))
            )
    await _wa_turn(tenant, "Primeira Consulta")  # a stale tap on the old list
    conversation = await _conversation(db, tenant)
    assert conversation.flow_attendee_name is None
    assert conversation.flow_step != "awaiting_service_confirm"
