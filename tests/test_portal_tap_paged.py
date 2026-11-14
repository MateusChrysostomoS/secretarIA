"""TASK-082 S1 - a Portal tap on a row loaded AFTER page 1 (CONTRACT.md §d).

The card stores page 1 only, so a row the patient scrolled to (or searched for) is
in no stored `options`: the 10-card window check alone would drop its id. A tap is
now also honoured when a recent card WITH a pointer re-resolves that id inside its
own stored scope, for this tenant, right now. Everything else is still dropped and
routed as typed text. WhatsApp taps never pass through here.

Reuses the worker fixtures of tests/test_brain_message_interactive_tap.py (in-memory
SQLite, no Meta client, the agent stubbed).
"""

from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.core.database import Base
from secretaria.models import Conversation, FlowState, Patient, Professional, Tenant
from secretaria.services import flow_router as fr
from secretaria.services.booking_scope import canonical_service_name
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.insurance_catalog import match_plan
from secretaria.workers.shared.bubbles import _send_bubble
from secretaria.workers.shared.flow_simulation_input import load_simulation_inputs
from tests.test_brain_message_interactive_tap import (  # noqa: F401 - _wire is autouse
    EXTERNAL_ID,
    _agent_calls,
    _inbound_row,
    _seed,
    _stop_at_the_reply_seam,
    _turn,
    _wire,
)


@pytest_asyncio.fixture
async def db():
    """The same in-memory database the imported `_wire` fixture patches the worker onto."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


LONG = "Consulta oftalmológica completa com mapeamento de retina"
SERVICE_NAMES = (
    [f"Serviço {i:02d}" for i in range(20)] + [LONG] + [f"Serviço {i:02d}" for i in range(20, 24)]
)
DOCTOR_NAMES = tuple(f"Dr. Número {i:02d}" for i in range(13))


async def _with_services(db, tenant, names=SERVICE_NAMES):
    async with db() as session, session.begin():
        row = await session.get(Tenant, tenant.id)
        row.appointment_types = [
            {"name": n, "duration_min": 30, "is_active": True, "sort_order": i}
            for i, n in enumerate(names)
        ]


async def _inputs(db, tenant):
    async with db() as session:
        return await load_simulation_inputs(session, await session.get(Tenant, tenant.id))


async def _send(db, conversation, bubble) -> None:
    await _send_bubble(
        BrainMessageSender(conversation_id=conversation.id, session_factory=db), EXTERNAL_ID, bubble
    )


async def _send_service_card(db, tenant, conversation) -> None:
    inputs = await _inputs(db, tenant)
    services = fr.active_appointment_types(inputs.tenant)
    await _send(
        db,
        conversation,
        fr._service_list_bubble(inputs.tenant, services, scope=fr.service_scope(None)),
    )


async def _send_doctor_card(db, tenant, conversation) -> None:
    inputs = await _inputs(db, tenant)
    [bubble] = fr._enter_professional_list(inputs.tenant, inputs.professionals).bubbles
    await _send(db, conversation, bubble)


# --------------------------------------------------------------------------
# Accepted: a row beyond page 1, re-resolved under the card's scope
# --------------------------------------------------------------------------


async def test_a_page_three_service_tap_is_honoured(db, monkeypatch) -> None:
    tenant, _, conversation = await _seed(db)
    await _with_services(db, tenant)
    await _send_service_card(db, tenant, conversation)
    replies = _stop_at_the_reply_seam(monkeypatch)

    await _turn(tenant, "Serviço 23", "svc|Serviço 23")

    assert len(replies) == 1
    assert (await _inbound_row(db, conversation.id)).interactive_reply_id == "svc|Serviço 23"


async def test_a_page_two_doctor_tap_carries_its_id_to_the_router(db, monkeypatch) -> None:
    tenant, doctors, conversation = await _seed(db, professional_names=DOCTOR_NAMES)
    await _send_doctor_card(db, tenant, conversation)
    last = max(doctors, key=lambda p: p.name)
    title = f"🥼 {last.name}"
    replies = _stop_at_the_reply_seam(monkeypatch)

    await _turn(tenant, title, f"prof|{last.id}")

    [reply] = replies
    assert reply.inbound_body == f"{title} ({last.id})"
    assert (await _inbound_row(db, conversation.id)).interactive_reply_id == f"prof|{last.id}"


async def test_a_page_two_doctor_tap_selects_that_doctor_end_to_end(db) -> None:
    """Real worker, real router: the 13th doctor (never on the card) is selected by id."""
    tenant, doctors, conversation = await _seed(
        db,
        professional_names=DOCTOR_NAMES,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_PROFESSIONAL,
    )
    await _send_doctor_card(db, tenant, conversation)
    last = max(doctors, key=lambda p: p.name)

    await _turn(tenant, f"🥼 {last.name}", f"prof|{last.id}")

    async with db() as session:
        conv = await session.get(Conversation, conversation.id)
    assert conv.flow_selected_professional_id == last.id
    assert conv.flow_step == fr.STEP_AWAITING_SERVICE
    assert _agent_calls == []


# --------------------------------------------------------------------------
# Rejected: forged, cross-kind, foreign, or from another conversation
# --------------------------------------------------------------------------


async def _assert_dropped(db, tenant, conversation, monkeypatch, text, reply_id) -> None:
    replies = _stop_at_the_reply_seam(monkeypatch)
    await _turn(tenant, text, reply_id)
    [reply] = replies
    assert reply.inbound_body == text  # routed as typed text, payload never attached
    assert (await _inbound_row(db, conversation.id)).interactive_reply_id is None


async def test_a_forged_service_name_is_dropped(db, monkeypatch) -> None:
    tenant, _, conversation = await _seed(db)
    await _with_services(db, tenant)
    await _send_service_card(db, tenant, conversation)
    await _assert_dropped(db, tenant, conversation, monkeypatch, "Inventado", "svc|Inventado")


async def test_a_doctor_id_against_a_service_card_is_dropped(db, monkeypatch) -> None:
    tenant, [ana], conversation = await _seed(db)
    await _with_services(db, tenant)
    await _send_service_card(db, tenant, conversation)
    await _assert_dropped(db, tenant, conversation, monkeypatch, "Dra. Ana", f"prof|{ana.id}")


async def test_another_clinics_doctor_is_dropped(db, monkeypatch) -> None:
    tenant, _, conversation = await _seed(db, professional_names=DOCTOR_NAMES)
    await _send_doctor_card(db, tenant, conversation)
    async with db() as session, session.begin():
        other = Tenant(id=uuid4(), clinic_name="Outra", phone_number_id="999")
        session.add(other)
        await session.flush()
        stranger = Professional(tenant_id=other.id, name="Dr. Número 99", is_active=True)
        session.add(stranger)
        await session.flush()
        stranger_id = stranger.id
    await _assert_dropped(
        db, tenant, conversation, monkeypatch, "🥼 Dr. Número 99", f"prof|{stranger_id}"
    )


async def test_a_paged_card_of_another_conversation_vouches_for_nothing(db, monkeypatch) -> None:
    tenant, _, conversation = await _seed(db)
    await _with_services(db, tenant)
    async with db() as session, session.begin():
        neighbour = Patient(
            tenant_id=tenant.id, channel=CHANNEL_BRAIN_MESSAGE, external_id="neighbour"
        )
        session.add(neighbour)
        await session.flush()
        their = Conversation(tenant_id=tenant.id, patient_id=neighbour.id)
        session.add(their)
        await session.flush()
    await _send_service_card(db, tenant, their)
    await _assert_dropped(db, tenant, conversation, monkeypatch, "Serviço 23", "svc|Serviço 23")


async def test_a_service_removed_since_the_card_is_dropped(db, monkeypatch) -> None:
    tenant, _, conversation = await _seed(db)
    await _with_services(db, tenant)
    await _send_service_card(db, tenant, conversation)
    await _with_services(db, tenant, SERVICE_NAMES[:-1])  # "Serviço 23" is gone
    await _assert_dropped(db, tenant, conversation, monkeypatch, "Serviço 23", "svc|Serviço 23")


# --------------------------------------------------------------------------
# Downstream handlers resolve against the FULL catalogue, by full name
# --------------------------------------------------------------------------


def test_a_full_long_service_name_resolves_to_its_entry() -> None:
    services = [{"name": n} for n in SERVICE_NAMES]
    assert canonical_service_name(services, LONG) == LONG
    assert canonical_service_name(services, f"🏥 {LONG}") == LONG
    assert canonical_service_name(services, "Serviço 23") == "Serviço 23"


def test_a_full_long_plan_name_resolves_to_its_plan() -> None:
    plans = [
        {"id": str(i), "name": f"Plano de Saúde Corporativo Número {i:02d}"} for i in range(15)
    ]
    assert match_plan(plans, "Plano de Saúde Corporativo Número 14") == plans[14]


def test_a_doctor_tap_resolves_by_id_against_the_whole_roster() -> None:
    from types import SimpleNamespace

    roster = [SimpleNamespace(id=uuid4(), name=n) for n in DOCTOR_NAMES]
    last = roster[-1]
    assert fr._match_professional(roster, f"🥼 {last.name} ({last.id})") is last
    assert fr._match_professional(roster, f"🥼 {last.name}") is last
