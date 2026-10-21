"""The main menu offers "Gerenciar consulta" to a patient with something booked.

Found live on the Portal (2026-10-05, clínica QA multi-médico): a patient with a
confirmed future consultation typed "menu" and got only [🗓️ Agendar, Outro] -
no visible way to reschedule or cancel. The menu is now decided at the worker
seam every menu passes through (`workers/shared/flow_runner.py::
_offer_manage_on_main_menu`), after the turn's own write committed, so:

  * `/menu`, the router's menus and the agent's hand-back all gain the entry;
  * a menu that follows the patient's LAST cancellation drops it again;
  * the label is the one `route()` already sends to `_enter_manage`.

In-memory-sqlite pattern from test_agent_menu_tools.py / test_flow_cancel_money.py.
"""

import os

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
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

from secretaria.ai.formatter import TextBubble  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.core.whatsapp_limits import (  # noqa: E402
    MAX_BUTTON_LABEL_CHARS,
    MAX_BUTTONS_PER_MESSAGE,
)
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
    Tenant,
)
from secretaria.services.flow_router import (  # noqa: E402
    LABEL_MANAGE_APPOINTMENT,
    LABEL_RESCHEDULE,
    STEP_MANAGE_ACTION,
    FlowRouterResult,
    MenuBubble,
    is_main_menu,
    main_menu_buttons,
    route,
)
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import flow_runner  # noqa: E402

PLAIN_MENU = ["🗓️ Agendar", "Outro"]
MANAGE_MENU = ["🗓️ Agendar", "Gerenciar consulta", "Outro"]


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


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    captured: list = []

    async def _fake_dispatch(reply, bubbles, tenant=None, waba_token=None):
        captured.extend(bubbles)
        return len(bubbles)

    monkeypatch.setattr(workers_ns, "_dispatch_bubbles", _fake_dispatch)
    return captured


async def _seed(db, *, appointments=()):
    """A Portal patient; `appointments` = (start offset, status) pairs."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Chrysostomo For Eyes",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
        )
        session.add(tenant)
        await session.flush()
        patient = Patient(
            tenant_id=tenant.id,
            channel="brain_message",
            external_id=str(uuid4()),
            name="Maria",
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, flow_state=FlowState.LLM
        )
        session.add(conversation)
        rows = []
        for offset, status in appointments:
            start = datetime.now(UTC) + offset
            appt = Appointment(
                tenant_id=tenant.id,
                patient_id=patient.id,
                google_event_id=f"evt-{uuid4()}",
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                status=status,
            )
            session.add(appt)
            rows.append(appt)
        await session.commit()
        for obj in (tenant, patient, conversation, *rows):
            await session.refresh(obj)
        return tenant, patient, conversation, rows


def _reply(conversation) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_ref="portal-ref",
        inbound_body="menu",
    )


# --------------------------------------------------------------------------
# The labels themselves
# --------------------------------------------------------------------------


def test_manage_menu_keeps_agendar_and_fits_whatsapp():
    labels = main_menu_buttons(has_upcoming=True)
    assert labels == MANAGE_MENU
    assert labels[1] == LABEL_MANAGE_APPOINTMENT
    assert len(labels) <= MAX_BUTTONS_PER_MESSAGE
    assert all(len(label) <= MAX_BUTTON_LABEL_CHARS for label in labels)


def test_default_menu_is_unchanged():
    assert main_menu_buttons() == PLAIN_MENU


def test_is_main_menu_only_matches_the_drawn_main_menu():
    assert is_main_menu(MenuBubble(body="Como posso te ajudar?", labels=main_menu_buttons()))
    assert not is_main_menu(MenuBubble(body="x", labels=["Remarcar", "❌ Cancelar", "Voltar"]))
    assert not is_main_menu(MenuBubble(body="x", labels=MANAGE_MENU))
    assert not is_main_menu(TextBubble(body="Como posso te ajudar?"))


async def test_router_menu_is_recognised_by_the_worker_seam():
    """The router's own menu (`_menu_bubbles`) is what the seam personalises."""
    tenant = SimpleNamespace(initial_flows={}, appointment_types=[], business_hours={})
    conversation = SimpleNamespace(
        flow_state=FlowState.IDLE,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_managing_appointment_id=None,
        patient_id=None,
    )
    res = await route(conversation, tenant, None, "bom dia")
    assert res.flow_state == FlowState.MENU
    assert is_main_menu(res.bubbles[-1])


async def test_tapping_manage_from_the_menu_opens_the_appointment_card():
    """The new button needs no new route: at MENU it reaches `_enter_manage`,
    whose single-appointment shortcut lands on the Remarcar/Cancelar card."""
    tenant = SimpleNamespace(initial_flows={}, appointment_types=[], business_hours={})
    conversation = SimpleNamespace(
        flow_state=FlowState.MENU,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_managing_appointment_id=None,
        patient_id=None,
    )
    start = datetime.now(UTC) + timedelta(days=3)
    upcoming = [
        {
            "id": str(uuid4()),
            "google_event_id": "evt-1",
            "appointment_type": "Consulta",
            "start_at": start,
            "end_at": start + timedelta(minutes=30),
            "professional_id": None,
        }
    ]
    res = await route(
        conversation, tenant, None, LABEL_MANAGE_APPOINTMENT, upcoming_appointments=upcoming
    )
    assert res.flow_state == FlowState.MANAGE_BOOKING
    assert res.flow_step == STEP_MANAGE_ACTION
    assert LABEL_RESCHEDULE in res.bubbles[0].labels


# --------------------------------------------------------------------------
# `/menu` (and the agent's hand-back): same seam, decided by the database
# --------------------------------------------------------------------------


async def test_menu_command_offers_manage_to_a_patient_with_a_future_appointment(db, sent):
    tenant, _patient, conversation, _ = await _seed(
        db, appointments=[(timedelta(days=2), AppointmentStatus.CONFIRMED)]
    )
    await tasks._handle_show_main_menu(
        _reply(conversation), tenant, [], None, source="command"
    )
    (menu,) = sent
    assert isinstance(menu, MenuBubble)
    assert menu.labels == MANAGE_MENU


async def test_rescheduled_appointment_counts_as_upcoming(db, sent):
    tenant, _patient, conversation, _ = await _seed(
        db, appointments=[(timedelta(days=5), AppointmentStatus.RESCHEDULED)]
    )
    await tasks._handle_show_main_menu(_reply(conversation), tenant, [], None)
    assert sent[0].labels == MANAGE_MENU


@pytest.mark.parametrize(
    "appointments",
    [
        [],
        [(timedelta(days=2), AppointmentStatus.CANCELLED)],
        [(-timedelta(days=1), AppointmentStatus.SCHEDULED)],
    ],
    ids=["nothing_booked", "only_cancelled", "only_past"],
)
async def test_menu_command_stays_plain_without_a_live_future_appointment(
    db, sent, appointments
):
    tenant, _patient, conversation, _ = await _seed(db, appointments=appointments)
    await tasks._handle_show_main_menu(
        _reply(conversation), tenant, [], None, source="command"
    )
    assert sent[0].labels == PLAIN_MENU


async def test_menu_after_cancelling_the_last_appointment_drops_the_entry(db, sent):
    """Decided AFTER this turn's write commits: the menu that closes the
    cancellation must not offer to manage the appointment just cancelled."""
    tenant, _patient, conversation, (appt,) = await _seed(
        db, appointments=[(timedelta(days=2), AppointmentStatus.SCHEDULED)]
    )
    result = FlowRouterResult(
        action="reply",
        bubbles=[
            TextBubble(body="Consulta cancelada."),
            MenuBubble(body="Como posso te ajudar?", labels=main_menu_buttons()),
        ],
        flow_state=FlowState.MENU,
        appointment_cancel_id=appt.google_event_id,
    )
    await tasks._apply_flow_result(_reply(conversation), result, None, tenant=tenant)
    assert sent[-1].labels == PLAIN_MENU


async def test_menu_after_cancelling_one_of_two_keeps_the_entry(db, sent):
    tenant, _patient, conversation, (first, _second) = await _seed(
        db,
        appointments=[
            (timedelta(days=2), AppointmentStatus.SCHEDULED),
            (timedelta(days=9), AppointmentStatus.SCHEDULED),
        ],
    )
    result = FlowRouterResult(
        action="reply",
        bubbles=[MenuBubble(body="Como posso te ajudar?", labels=main_menu_buttons())],
        flow_state=FlowState.MENU,
        appointment_cancel_id=first.google_event_id,
    )
    await tasks._apply_flow_result(_reply(conversation), result, None, tenant=tenant)
    assert sent[-1].labels == MANAGE_MENU


async def test_other_cards_are_never_touched(db, sent):
    tenant, _patient, conversation, _ = await _seed(
        db, appointments=[(timedelta(days=2), AppointmentStatus.SCHEDULED)]
    )
    card = MenuBubble(body="O que você gostaria de fazer?", labels=["Remarcar", "Voltar"])
    result = FlowRouterResult(action="reply", bubbles=[card], flow_state=FlowState.MENU)
    await tasks._apply_flow_result(_reply(conversation), result, None, tenant=tenant)
    assert sent[0].labels == ["Remarcar", "Voltar"]


async def test_lookup_failure_sends_the_plain_menu(db, sent, monkeypatch):
    tenant, _patient, conversation, _ = await _seed(
        db, appointments=[(timedelta(days=2), AppointmentStatus.SCHEDULED)]
    )

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(flow_runner, "has_upcoming_appointment", _boom)
    await tasks._handle_show_main_menu(_reply(conversation), tenant, [], None)
    assert sent[0].labels == PLAIN_MENU
