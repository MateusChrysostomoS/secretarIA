"""TASK-090 Adendo 1 - re-review C1 / I1 / M3 (owner-safe ruling, 2026-11-23).

Two active doctors, one hidden (Dra. Bia: linked only to a unit with an incomplete address):

* C1 - the TOPOLOGY stays the full roster's: the clinic keeps the multi-doctor paths, so a
  booking with the visible doctor (Dra. Ana) reads HER service list, HER unit hours and HER
  agenda, never the clinic's - in the flow (end to end), in `get_availability` and in the
  draft resolver;
* I1 - Dra. Bia's EXISTING consultation still resolves to her (services, units) in the router;
* M3 - the turn's own load says "not servable" when every active doctor is hidden.

All real usable-unit rule (`real_unit_gate`).
"""

import os

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
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

from secretaria.ai import graph  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Professional,
    Tenant,
    Unit,
)
from secretaria.models.calendar_event import CalendarEvent  # noqa: E402
from secretaria.models.professional_unit import ProfessionalUnit  # noqa: E402
from secretaria.services import (  # noqa: E402
    booking_hold as holds,
    flow_router as fr,
    flow_units,
    pii_pseudonymization as pii_store,
)
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    booking_topology,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import unit_gate  # noqa: E402
from tests._local_agenda import wire_local_agenda  # noqa: E402

pytestmark = pytest.mark.real_unit_gate

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511977776666"
WEEK = {
    d: [{"start": "08:00", "end": "18:00"}]
    for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}
MONDAY_MORNING = {"monday": [{"start": "08:00", "end": "12:00"}]}
COMPLETE = {
    "cep": "13010100",
    "street": "Rua Barão de Jaguara",
    "number": "100",
    "city": "Campinas",
    "state": "SP",
}


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
        _WireClient.sends.append(("text", body, None))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}

    async def send_buttons(self, to, body, buttons):
        _WireClient.sends.append(("buttons", body, [label for _id, label in buttons]))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        _WireClient.sends.append(("list", body, list(rows)))
        return {"messages": [{"id": f"wamid.out.{len(_WireClient.sends)}"}]}


@pytest.fixture(autouse=True)
def wired(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(graph, "async_session_factory", db)
    monkeypatch.setattr(pii_store, "async_session_factory", db)
    monkeypatch.setattr(holds, "async_session_factory", db)
    monkeypatch.setattr(unit_gate, "async_session_factory", db)
    wire_local_agenda(monkeypatch, db)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    _WireClient.sends = []
    monkeypatch.setattr(workers_ns, "WhatsAppClient", _WireClient)

    async def _none(*_a, **_k):
        return None

    async def _token(session, tenant_id):
        return "decrypted-waba-token"

    async def _entitlements(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id), status="active", active=True, secretaria_enabled=True,
            plan="bronze", secretaria_tier="basico", addons={}, limits={},
        )

    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _none)
    monkeypatch.setattr(workers_ns, "get_waba_token", _token)
    monkeypatch.setattr(workers_ns, "get_entitlements", _entitlements)
    monkeypatch.setattr(workers_ns, "enqueue_post_booking_hooks", _none)
    return db


async def _seed(
    db, *, with_ana: bool = True, with_caio: bool = False, state: FlowState | None = None
):
    """Clinic list "Consulta Clínica" (all week); Dra. Ana does "Retina" at the complete Sede on
    Monday mornings; Dra. Bia does "Retina" too but is linked only to the incomplete Nova.
    `with_caio`: Dr. Caio, no unit link at all (legacy, offered), also does "Retina" - so the
    doctor list of "Retina" really opens (Ana + Caio, never Bia). `state`: the conversation's
    flow state (default: the clinic service list)."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            timezone="America/Sao_Paulo",
            business_hours=WEEK,
            initial_flows={},
            appointment_types=[
                {"name": "Consulta Clínica", "duration_min": 30, "is_active": True}
            ],
        )
        session.add(tenant)
        await session.flush()
        retina = [{"name": "Retina", "duration_min": 30, "is_active": True}]
        ana = Professional(
            id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=with_ana,
            appointment_types=retina,
        )
        bia = Professional(
            id=uuid4(), tenant_id=tenant.id, name="Dra. Bia", is_active=True,
            appointment_types=retina,
        )
        if with_caio:
            session.add(
                Professional(
                    id=uuid4(), tenant_id=tenant.id, name="Dr. Caio", is_active=True,
                    appointment_types=retina,
                )
            )
        sede = Unit(id=uuid4(), tenant_id=tenant.id, name="Sede", **COMPLETE)
        nova = Unit(id=uuid4(), tenant_id=tenant.id, name="Nova", city="Campinas", state="SP")
        session.add_all([ana, bia, sede, nova])
        await session.flush()
        session.add_all(
            [
                ProfessionalUnit(
                    professional_id=ana.id, unit_id=sede.id, business_hours=MONDAY_MORNING,
                    sort_order=0,
                ),
                ProfessionalUnit(professional_id=bia.id, unit_id=nova.id, sort_order=0),
            ]
        )
        patient = Patient(
            id=uuid4(), tenant_id=tenant.id, wa_id=WA_ID, name="Maria",
            lgpd_accepted_at=datetime.now(UTC),
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_state=state or FlowState.SERVICE_CATALOG,
            flow_step=None if state is not None else fr.STEP_AWAITING_CATALOG_SERVICE,
            flow_attendee_name="",
        )
        session.add(conversation)
        await session.flush()
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.BOT,
                body="Qual serviço você gostaria de agendar?",
            )
        )
        await session.commit()
        return tenant, ana, bia, sede, nova, patient, conversation


_seq = iter(range(1, 100_000))


async def _turn(body: str, reply_id: str | None = None):
    reply = await tasks._persist_inbound_message(
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id=f"wamid.in.{next(_seq)}",
        body=body,
        interactive_reply_id=reply_id,
    )
    assert reply is not None
    await tasks._send_bot_reply(reply, redis=None)


async def _conversation(db, conversation_id) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, conversation_id)


def _ids(rows) -> list[str]:
    return [row[0] for row in rows]


async def _finish_booking(db, conversation, sede) -> None:
    """Drive whatever step the conversation is on to the confirmation and confirm."""
    for _ in range(6):
        row = await _conversation(db, conversation.id)
        kind, _body, rows = _WireClient.sends[-1]
        if row.flow_step == fr.STEP_AWAITING_DAY:
            days = [r for r in _ids(rows) if r.startswith("day|")]
            # Dra. Ana's hours AT THE SEDE (Monday mornings), never the clinic's all week.
            assert days and {datetime.fromisoformat(d.split("|")[1]).weekday() for d in days} == {0}
            await _turn("dia", days[0])
        elif row.flow_step == fr.STEP_AWAITING_SLOT:
            slots = [r for r in _ids(rows) if r.startswith("slot|")]
            await _turn("hora", slots[0])
        elif row.flow_step == fr.STEP_AWAITING_CONFIRMATION:
            await _turn(fr.LABEL_CONFIRM)
            break
        elif row.flow_step == flow_units.STEP_AWAITING_UNIT:
            await _turn("Sede", f"unit|{sede.id}")
        else:
            pytest.fail(f"unexpected step {row.flow_step!r}: {_WireClient.sends[-1][1]!r}")


async def _assert_booked_with_ana_at_sede(db, ana, sede) -> None:
    async with db() as session:
        [appointment] = list(await session.scalars(select(Appointment)))
        [event] = list(await session.scalars(select(CalendarEvent)))
    assert appointment.professional_id == ana.id
    assert appointment.unit_id == sede.id
    assert appointment.appointment_type == "Retina"
    # HER agenda, not the clinic's (professional_id NULL would be the clinic agenda).
    assert event.professional_id == ana.id
    assert event.id == appointment.google_event_id


async def test_c1_the_roster_of_the_turn_stays_multi_even_copied(db) -> None:
    """The real roster (not a hand-built one): offered = visible, `everyone` = all active, and a
    plain `list(...)` copy - what several booking paths make - still counts every doctor."""
    from secretaria.services.booking_scope import everyone_of, resolve_booking_owner_id
    from secretaria.services.tenant_config import list_active_professionals
    from secretaria.workers.shared.patient_roster import build_patient_roster

    tenant, ana, bia, *_rest = await _seed(db)
    async with db() as session:
        rows = await list_active_professionals(session, tenant.id)
        roster = await build_patient_roster(session, tenant, rows, None)
    for view in (roster, list(roster), roster[:], [p for p in roster]):
        assert [p.name for p in view] == ["Dra. Ana"]
        assert {p.id for p in everyone_of(view)} == {ana.id, bia.id}
        assert fr._is_multi_professional(view)
        assert booking_topology(view) == BOOKING_TOPOLOGY_MULTI
        # No implied owner on a 2-doctor clinic; an explicit visible pick only.
        assert resolve_booking_owner_id(view, None) is None
        assert resolve_booking_owner_id(view, ana.id) == ana.id
        assert resolve_booking_owner_id(view, bia.id) is None


async def test_c1_the_flow_books_the_visible_doctor_on_her_agenda_and_hours(db) -> None:
    tenant, ana, bia, sede, _nova, _patient, conversation = await _seed(db, with_caio=True)
    await _turn("Retina")
    kind, _body, rows = _WireClient.sends[-1]
    row = await _conversation(db, conversation.id)
    # The doctor list really opens: Dra. Ana and Dr. Caio, never the hidden Dra. Bia.
    assert row.flow_step == fr.STEP_AWAITING_SERVICE_PROFESSIONAL
    assert kind == "list" and f"prof|{ana.id}" in _ids(rows)
    assert f"prof|{bia.id}" not in _ids(rows)
    # Typing her name never selects her.
    await _turn("Dra. Bia")
    assert (await _conversation(db, conversation.id)).flow_selected_professional_id != bia.id
    async with db() as session, session.begin():
        again = await session.get(Conversation, conversation.id)
        again.flow_state = FlowState.SERVICE_CATALOG
        again.flow_step = fr.STEP_AWAITING_SERVICE_PROFESSIONAL
        again.flow_selected_type = "Retina"
    await _turn("Dra. Ana", f"prof|{ana.id}")
    await _finish_booking(db, conversation, sede)
    await _assert_booked_with_ana_at_sede(db, ana, sede)


async def test_c1_the_flow_with_one_visible_doctor_still_books_on_her_agenda(db) -> None:
    """Only Dra. Ana offers Retina (Bia hidden): the flow picks her and books on HER agenda."""
    tenant, ana, _bia, sede, _nova, _patient, conversation = await _seed(db)
    await _turn("Retina")
    await _finish_booking(db, conversation, sede)
    await _assert_booked_with_ana_at_sede(db, ana, sede)


async def _ai_draft_turn(monkeypatch, draft) -> None:
    from secretaria.ai.graph import BOOKING_DRAFT_SENTINEL_PREFIX

    async def _run_agent(message, context, **kwargs):
        return BOOKING_DRAFT_SENTINEL_PREFIX + draft.to_payload()

    monkeypatch.setattr(workers_ns, "run_agent", _run_agent)
    await _turn("quero marcar retina com a dra ana")


async def test_c1_an_ai_draft_for_her_service_resolves_on_her_catalog_and_agenda(
    db, monkeypatch
) -> None:
    """Re-review C1-bis scenario 1: the AI's draft "Retina" was dropped as not in the CLINIC
    list. Now it resolves on Dra. Ana's catalog, selects her, and the booking finished through
    the main turn lands on her agenda at her unit."""
    from secretaria.services.booking_draft import BookingDraft

    tenant, ana, bia, sede, _nova, _patient, conversation = await _seed(
        db, state=FlowState.LLM
    )
    await _ai_draft_turn(monkeypatch, BookingDraft(service="Retina", attendee="self"))
    row = await _conversation(db, conversation.id)
    assert row.flow_selected_type == "Retina"
    assert row.flow_selected_professional_id == ana.id
    await _finish_booking(db, conversation, sede)
    await _assert_booked_with_ana_at_sede(db, ana, sede)


async def test_c1_an_ai_draft_for_the_clinic_list_never_books_without_a_doctor(
    db, monkeypatch
) -> None:
    """Re-review C1-bis scenario 2: "Consulta Clínica" (the clinic's legacy list, which no
    bookable doctor offers) used to land on the day list with no doctor and book on the CLINIC
    agenda with professional_id = Ana and no unit. Now it is not offered at all."""
    from secretaria.services.booking_draft import BookingDraft

    tenant, *_rest, conversation = await _seed(db, state=FlowState.LLM)
    await _ai_draft_turn(monkeypatch, BookingDraft(service="Consulta Clínica", attendee="self"))
    row = await _conversation(db, conversation.id)
    assert row.flow_step != fr.STEP_AWAITING_DAY
    assert row.flow_selected_type != "Consulta Clínica"
    assert all("Consulta Clínica" not in str(send) for send in _WireClient.sends)


async def test_c1_get_availability_reads_the_visible_doctors_own_agenda(db, monkeypatch) -> None:
    from secretaria.ai import availability_tool as at, tools as ai_tools
    from secretaria.plugins import multi_professional as mp

    tenant, ana, _bia, *_rest = await _seed(db)
    agendas: list = []

    async def _calendar(tenant_id, professional):
        agendas.append(professional.name)
        return SimpleNamespace(default_slot_minutes=30, owner=professional.name)

    monkeypatch.setattr(mp, "_professional_calendar", _calendar)
    clinic = SimpleNamespace(default_slot_minutes=30, owner="clinic")
    tokens = [
        ai_tools._tenant_id_ctx.set(tenant.id),
        ai_tools._calendar_ctx.set(clinic),
        ai_tools._tenant_config_ctx.set(type("Cfg", (), {"tenant_id": tenant.id})()),
    ]
    try:
        roster = await mp._active_professionals(tenant.id)
        target = await at._resolve_target(tenant.id, "", "Retina")
        unnamed = await at._resolve_target(tenant.id, "", "")
    finally:
        ai_tools._tenant_config_ctx.reset(tokens[2])
        ai_tools._calendar_ctx.reset(tokens[1])
        ai_tools._tenant_id_ctx.reset(tokens[0])
    assert booking_topology(roster) == BOOKING_TOPOLOGY_MULTI and len(roster) == 1
    assert target.professional.id == ana.id and target.owner_id == ana.id
    assert target.calendar.owner == "Dra. Ana" and agendas[0] == "Dra. Ana"
    assert unnamed.professional.id == ana.id and unnamed.calendar.owner == "Dra. Ana"


async def test_c1_the_draft_resolver_keeps_multi_and_opens_her_agenda(db, monkeypatch) -> None:
    from secretaria.workers.shared import draft_resolution as dr

    tenant, ana, bia, *_rest = await _seed(db)
    reply = tasks._ReplyContext(
        conversation_id=_rest[-1].id, patient_ref=WA_ID, inbound_body="", tenant_id=tenant.id
    )
    ctx = await dr._load_draft_context(reply, tenant)
    assert ctx.topology == BOOKING_TOPOLOGY_MULTI
    assert [p.name for p in ctx.professionals] == ["Dra. Ana"]
    assert {p.name for p in ctx.professionals.everyone} == {"Dra. Ana", "Dra. Bia"}
    targets: list = []

    async def _calendar(session, tenant, target):
        targets.append(target)
        return object()

    monkeypatch.setattr(dr, "_appointment_calendar", _calendar)
    await dr._draft_calendar_source(tenant, ctx)(ctx.professionals[0])
    assert getattr(targets[0], "id", None) == ana.id  # her own row, never "tenant"


async def test_i1_a_hidden_doctors_existing_consultation_keeps_her_and_her_unit(db) -> None:
    from secretaria.services.tenant_config import list_active_professionals
    from secretaria.workers.shared.patient_roster import build_patient_roster

    tenant, _ana, bia, _sede, nova, *_rest = await _seed(db)
    async with db() as session:
        rows = await list_active_professionals(session, tenant.id)
        roster = await build_patient_roster(session, tenant, rows, None)
    appointment = {"professional_id": str(bia.id), "unit_id": str(nova.id)}
    owner, unit_id = fr._appointment_owner_and_unit_id(appointment, roster)
    assert owner is not None and owner.id == bia.id
    assert [s["name"] for s in owner.appointment_types] == ["Retina"]
    # Her own (incomplete) unit stays on her snapshot: the consultation keeps its unit lines.
    assert flow_units.find_unit(owner, unit_id) is not None
    # ... while a NEW booking can never select her.
    assert fr._find_professional_by_id(roster, bia.id) is None


async def test_m3_the_turn_itself_says_not_servable_when_every_doctor_is_hidden(
    db, monkeypatch
) -> None:
    """Only Dra. Bia is active (hidden); the Sede is complete but nobody attends there. The
    turn's own load decides (no second read by the gate) and nothing is sent."""
    tenant, *_rest = await _seed(db, with_ana=False)

    def _no_session():
        raise AssertionError("the gate must reuse the answer the turn already read")

    monkeypatch.setattr(unit_gate, "async_session_factory", _no_session)
    events: list[str] = []
    monkeypatch.setattr(unit_gate.logger, "warning", lambda event, **_kw: events.append(event))
    await _turn("Retina")
    assert _WireClient.sends == []
    assert events == ["bot_reply_suppressed_no_complete_unit"]


async def _bias_consultation(db, tenant, bia, nova, patient) -> Appointment:
    start = datetime.now(UTC) + timedelta(days=5)
    async with db() as session:
        appointment = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id,
            professional_id=bia.id,
            unit_id=nova.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Retina",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appointment)
        await session.commit()
        return appointment


def _bia_keeps_nova(professionals, bia, nova) -> None:
    owner = fr._find_owner_by_id(professionals, bia.id)
    assert owner is not None and flow_units.find_unit(owner, nova.id) is not None
    assert fr._find_professional_by_id(professionals, bia.id) is None  # never for a new booking


class _Stop(Exception):
    pass


async def test_i1_the_ai_manage_handback_keeps_the_hidden_doctors_unit(db, monkeypatch) -> None:
    """Re-review I-1: the AI's "manage this consultation" hand-back (v2) builds the patient
    roster, so Dra. Bia's consultation at the incomplete Nova keeps that unit (and its hours)."""
    from secretaria.services.manage_request import ManageRequest
    from secretaria.workers.shared import sentinels

    tenant, _ana, bia, _sede, nova, patient, conversation = await _seed(db, state=FlowState.LLM)
    async with db() as session, session.begin():
        row = await session.get(Tenant, tenant.id)
        row.initial_flows = {fr.AI_DRAFT_V2_FLAG: True}
    await _bias_consultation(db, tenant, bia, nova, patient)
    seen: list = []

    async def _spy(request, **kwargs):
        seen.append(kwargs["professionals"])
        raise _Stop

    monkeypatch.setattr(sentinels, "resolve_manage_request", _spy)
    reply = tasks._ReplyContext(
        conversation_id=conversation.id, patient_ref=WA_ID, inbound_body="", tenant_id=tenant.id
    )
    async with db() as session:
        fresh = await session.get(Tenant, tenant.id)
    with pytest.raises(_Stop):
        await sentinels._handle_manage_appointment(
            reply, ManageRequest(action="reschedule").to_payload(), fresh, [], WA_ID
        )
    [professionals] = seen
    _bia_keeps_nova(professionals, bia, nova)


async def test_i1_the_service_change_rebook_keeps_the_hidden_doctors_unit(db, monkeypatch) -> None:
    """Re-review I-1: "Outro horário" after a service change (`_rebook_entry`) builds the
    patient roster too."""
    from secretaria.workers.shared import service_change_actions as sca

    tenant, _ana, bia, _sede, nova, patient, _conversation = await _seed(db)
    appointment = await _bias_consultation(db, tenant, bia, nova, patient)
    target = {
        "id": str(appointment.id),
        "professional_id": str(bia.id),
        "unit_id": str(nova.id),
        "rebook_adjustment_id": str(uuid4()),
    }

    async def _upcoming(*_args, **_kwargs):
        return [target]

    async def _calendar(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(sca, "load_upcoming_appointments", _upcoming)
    monkeypatch.setattr(sca, "_appointment_calendar", _calendar)
    async with db() as session:
        fresh = await session.get(Tenant, tenant.id)
        entry = await sca._rebook_entry(session, fresh, patient.id, appointment.id)
    assert entry is not None
    _appointments, professionals, _calendar_used = entry
    _bia_keeps_nova(professionals, bia, nova)


async def test_m_b_the_ai_state_block_never_says_one_doctor_and_names_the_managed_one(db) -> None:
    """Re-review M-b: with Dra. Bia hidden the AI is told the clinic has several doctors (Ana is
    the one offered), and while Bia's own consultation is managed she is "Médico já escolhido"."""
    from secretaria.services import llm_context
    from secretaria.services.tenant_config import list_active_professionals
    from secretaria.workers.shared.patient_roster import build_patient_roster

    tenant, ana, bia, *_rest = await _seed(db)
    async with db() as session:
        rows = await list_active_professionals(session, tenant.id)
        roster = await build_patient_roster(session, tenant, rows, None)
    clinic = SimpleNamespace(appointment_types=[], initial_flows={})
    lines = "\n".join(llm_context._topology_lines(clinic, roster))
    assert "um só médico" not in lines and "vários médicos" in lines
    assert "Dra. Ana" in lines and "Dra. Bia" not in lines
    managed = SimpleNamespace(
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=None,
        flow_selected_professional_id=bia.id,
        flow_selected_type=None,
        flow_attendee_name=None,
    )
    state = llm_context.build_conversation_state(managed, clinic, roster) or ""
    assert "Médico já escolhido: Dra. Bia" in state
