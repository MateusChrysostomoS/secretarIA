"""TASK-082 F1 (CONTRACT.md D10) - a typed "ver mais" at a WhatsApp list pages it.

Owner decision O6: WhatsApp has no search, only the lists, and a list past the limit
ends with a "Ver mais" row. A patient who TYPES "ver mais" (any case/accents, with or
without "serviços" / "profissionais" / "convênios") gets the next page of the most
recent list card of THIS conversation, exactly as if they had tapped its "Ver mais"
row. No such row on that card -> nothing changes (the text routes as typed, `main`'s
behaviour). Resolved in the WhatsApp inbound path before routing
(workers/whatsapp/typed_more.py); the router stays DB-free. The Portal is untouched.

In-memory SQLite, the same pattern as tests/test_bot_allowlist.py.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from itertools import count  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.workers.shared.bubbles import _bubble_interactive  # noqa: E402
from secretaria.workers.whatsapp import (
    inbound as wa_inbound,  # noqa: E402
    typed_more as tm,  # noqa: E402
)
from tests._patching import workers_ns  # noqa: E402
from tests.test_flow_router_insurance import _FakeCalendar  # noqa: E402

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
DOCTOR_NAMES = [f"Dra. Médica {i:02d}" for i in range(10)]


# --------------------------------------------------------------------------
# Pure: which texts are a typed "ver mais", and which row they mean
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "kind"),
    [
        ("ver mais", ""),
        ("Ver Mais", ""),
        ("  VER   MAIS  ", ""),
        ("ver mais.", ""),
        ("ver mais profissionais", "profissionais"),
        ("Ver mais Serviços", "servicos"),
        ("ver mais servicos", "servicos"),
        ("ver mais convênios", "convenios"),
        ("VER MAIS CONVENIOS!", "convenios"),
    ],
)
def test_typed_more_is_recognised(typed, kind):
    assert tm.typed_more_kind(typed) == kind


@pytest.mark.parametrize(
    "typed",
    [None, "", "mais", "ver", "ver mais dias", "quero ver mais opções", "ver mais médicos?", "sim"],
)
def test_anything_else_is_not_a_typed_more(typed):
    assert tm.typed_more_kind(typed) is None


def _card(*rows: tuple[str, str]) -> dict:
    return {"kind": "list", "options": [{"id": rid, "title": t} for rid, t in rows]}


def test_the_more_row_of_the_card_is_found():
    card = _card(
        ("prof|a", "🥼 Dra. A"), ("profmore|1", "Ver mais profissionais"), ("profhelp|0", "Não sei")
    )
    assert tm.more_row(card, "") == ("profmore|1", "Ver mais profissionais")
    assert tm.more_row(card, "profissionais") == ("profmore|1", "Ver mais profissionais")
    # Asked for another list's "Ver mais": not this card's row.
    assert tm.more_row(card, "servicos") is None
    assert tm.more_row(_card(("insmore|2", "Ver mais convênios")), "convenios") == (
        "insmore|2",
        "Ver mais convênios",
    )
    assert tm.more_row(_card(("svcmore|3", "Ver mais serviços")), "servicos") == (
        "svcmore|3",
        "Ver mais serviços",
    )


@pytest.mark.parametrize(
    "card",
    [None, {}, {"options": None}, _card(("prof|a", "Dra. A"), ("profhelp|0", "Não sei")), "x"],
)
def test_a_card_without_a_more_row_offers_nothing(card):
    assert tm.more_row(card, "") is None


# --------------------------------------------------------------------------
# DB: the most recent outbound card of THIS conversation
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _router_tenant():
    return SimpleNamespace(
        initial_flows={"enabled": True},
        appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        timezone="America/Sao_Paulo",
        insurance_plans=None,
        insurances=None,
        insurance_mode=None,
        insurance_accepted_by={},
        collect_insurance=False,
    )


def _doctors() -> list:
    return [
        SimpleNamespace(
            id=uuid4(),
            name=name,
            specialty=None,
            about=None,
            context_doctor_message=None,
            appointment_types=None,
            business_hours=None,
        )
        for name in DOCTOR_NAMES
    ]


async def _seed(db, wa_id: str = WA_ID, tenant: Tenant | None = None):
    async with db() as session, session.begin():
        if tenant is None:
            tenant = Tenant(
                id=uuid4(), clinic_name="Clinic", phone_number_id=PHONE_NUMBER_ID, is_active=True
            )
            session.add(tenant)
            await session.flush()
        patient = Patient(tenant_id=tenant.id, wa_id=wa_id)
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
    return tenant, patient, conversation


_CLOCK = count()


async def _outbound(db, conversation, interactive: dict | None, body: str = "x") -> None:
    # Explicit, strictly increasing times: SQLite's CURRENT_TIMESTAMP has 1 s resolution,
    # so two cards written in the same second would tie on `created_at`.
    async with db() as session, session.begin():
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.BOT,
                body=body,
                interactive=interactive,
                created_at=datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
                + timedelta(seconds=next(_CLOCK)),
            )
        )


def _first_doctor_page(doctors):
    [bubble] = fr._enter_professional_list(_router_tenant(), doctors).bubbles
    return bubble


async def _resolve(db, tenant_id, patient_id, text):
    async with db() as session:
        return await tm.typed_more_tap(
            session, tenant_id=tenant_id, patient_id=patient_id, text=text
        )


async def _route(doctors, body):
    return await fr.route(
        SimpleNamespace(
            id=uuid4(),
            flow_state=fr.FlowState.SERVICE_CATALOG,
            flow_step=fr.STEP_AWAITING_PROFESSIONAL,
            flow_selected_type=None,
            flow_selected_day=None,
            flow_selected_slot=None,
            flow_selected_professional_id=None,
            flow_selected_insurance=None,
            flow_managing_appointment_id=None,
            flow_attendee_name=None,
            flow_draft=None,
            flow_replaces_appointment_id=None,
            flow_edit_draft=None,
            patient_id=None,
        ),
        _router_tenant(),
        _FakeCalendar(),
        body,
        professionals=doctors,
    )


def _ids(bubble) -> list[str]:
    return [row[0] for row in bubble.rows]


@pytest.mark.parametrize("typed", ["ver mais", "Ver Mais", "ver mais profissionais"])
async def test_ten_doctors_typed_more_gives_page_two_like_the_tap(db, typed):
    """The owner's example: 10 doctors -> 8 + Ver mais + Não sei; then 2 + Não sei."""
    doctors = _doctors()
    page_one = _first_doctor_page(doctors)
    assert _ids(page_one) == [
        *(f"prof|{d.id}" for d in doctors[:8]),
        "profmore|1",
        "profhelp|0",
    ]
    tenant, patient, conversation = await _seed(db)
    await _outbound(db, conversation, _bubble_interactive(page_one))

    tap = await _resolve(db, tenant.id, patient.id, typed)
    assert tap == ("profmore|1", fr.LABEL_MORE_PROFESSIONALS)

    by_typing = await _route(doctors, inbound_routing_text(tap[1], tap[0]))
    by_tap = await _route(doctors, inbound_routing_text(fr.LABEL_MORE_PROFESSIONALS, "profmore|1"))
    assert (
        _ids(by_typing.bubbles[-1])
        == _ids(by_tap.bubbles[-1])
        == [f"prof|{doctors[8].id}", f"prof|{doctors[9].id}", "profhelp|0"]
    )


async def test_only_the_most_recent_card_counts(db):
    doctors = _doctors()
    tenant, patient, conversation = await _seed(db)
    await _outbound(db, conversation, _bubble_interactive(_first_doctor_page(doctors)))
    # A newer card without "Ver mais" (the list's last page, or another question).
    await _outbound(
        db, conversation, {"kind": "list", "options": [{"id": "profhelp|0", "title": "Não sei"}]}
    )
    assert await _resolve(db, tenant.id, patient.id, "ver mais") is None


async def test_a_plain_text_reply_after_the_card_does_not_hide_it(db):
    doctors = _doctors()
    tenant, patient, conversation = await _seed(db)
    await _outbound(db, conversation, _bubble_interactive(_first_doctor_page(doctors)))
    await _outbound(db, conversation, None, body="Claro! Qual prefere?")
    assert await _resolve(db, tenant.id, patient.id, "ver mais") == (
        "profmore|1",
        fr.LABEL_MORE_PROFESSIONALS,
    )


async def test_no_card_or_no_conversation_changes_nothing(db):
    tenant, patient, _ = await _seed(db)
    assert await _resolve(db, tenant.id, patient.id, "ver mais") is None
    assert await _resolve(db, tenant.id, uuid4(), "ver mais") is None


async def test_another_conversations_card_is_never_used(db):
    doctors = _doctors()
    tenant, _, theirs = await _seed(db, wa_id="5511900000001")
    await _outbound(db, theirs, _bubble_interactive(_first_doctor_page(doctors)))
    _, mine, _ = await _seed(db, wa_id="5511900000002", tenant=tenant)
    assert await _resolve(db, tenant.id, mine.id, "ver mais") is None


async def test_the_card_of_the_same_patient_id_under_another_clinic_is_never_used(db):
    doctors = _doctors()
    tenant, patient, conversation = await _seed(db)
    await _outbound(db, conversation, _bubble_interactive(_first_doctor_page(doctors)))
    assert await _resolve(db, uuid4(), patient.id, "ver mais") is None


# --------------------------------------------------------------------------
# The WhatsApp inbound path hands the router the tap
# --------------------------------------------------------------------------


@pytest.fixture
def captured(monkeypatch, db):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    calls: list[dict] = []

    async def _fake_route(session, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(wa_inbound, "_route_inbound_turn", _fake_route)
    return calls


async def _persist(body: str, wam_id: str, reply_id: str | None = None):
    return await wa_inbound._persist_inbound_message(
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        patient_name=None,
        wam_id=wam_id,
        body=body,
        interactive_reply_id=reply_id,
    )


async def test_a_typed_more_reaches_the_router_as_the_tap(db, captured):
    _, _, conversation = await _seed(db)
    await _outbound(db, conversation, _bubble_interactive(_first_doctor_page(_doctors())))
    await _persist("Ver mais", "wamid.typed.more.1")
    [call] = captured
    assert call["interactive_reply_id"] == "profmore|1"
    assert call["body"] == fr.LABEL_MORE_PROFESSIONALS
    assert inbound_routing_text(call["body"], call["interactive_reply_id"]) == (
        "Ver mais profissionais (1)"
    )


async def test_other_typed_text_reaches_the_router_unchanged(db, captured):
    _, _, conversation = await _seed(db)
    await _outbound(db, conversation, _bubble_interactive(_first_doctor_page(_doctors())))
    await _persist("sim", "wamid.typed.more.2")
    await _persist("ver mais", "wamid.typed.more.3", reply_id="prof|x")  # a real tap wins
    assert [(c["body"], c["interactive_reply_id"]) for c in captured] == [
        ("sim", None),
        ("ver mais", "prof|x"),
    ]
