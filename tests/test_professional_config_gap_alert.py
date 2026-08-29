"""FEAT 41 — a patient reaches a professional nobody can book, and somebody hears about it.

Two failures used to look identical from the outside and are nothing alike:

  * the doctor's agenda is genuinely full in the window we scan — normal, it
    fixes itself, `NO_AVAILABLE_DAYS_MESSAGE`, nobody is alerted;
  * the doctor has no availability window (or no service) configured AT ALL —
    nothing will ever come free, and only a human can fix it.

Both fell into the first branch, so the patient got a polite "não encontrei
horários" and the clinic learned nothing at all. This file pins the split.

Two layers, per the `conversation-flow-state` convention:

  * pure router (`SimpleNamespace` snapshots, a fake calendar, no DB) — that
    the STATIC check fires, carries the right gap, and does NOT steal the
    genuinely-full-agenda case;
  * the CALL SITE against a real in-memory sqlite DB — that the email actually
    goes out, to both addresses, exactly once per (tenant, professional, gap)
    silence window. That layer matters most: the debounce and the recipient
    resolution live in the guard, and a router-only test walks straight past
    them.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai.formatter import SlotsBubble, TextBubble  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    FlowState,
    Patient,
    Professional,
    Tenant,
)
from secretaria.services import flow_router  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    NO_AVAILABLE_DAYS_MESSAGE,
    PROFESSIONAL_NO_SERVICES_MESSAGE,
    STEP_AWAITING_SERVICE_CONFIRM,
    FlowRouterResult,
    _enter_professional_services,
    route,
)
from secretaria.workers import tasks  # noqa: E402

_SERVICE = "Consulta Geral"
_TZ = ZoneInfo("America/Sao_Paulo")
_HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}
PATIENT_NAME = "Maria Silva"
PATIENT_WA = "5511999998888"
CLINIC_EMAIL = "clinica@example.com"
DOCTOR_EMAIL = "dra.ana@example.com"


# ---------------------------------------------------------------------------
# Layer 1 — the router, pure
# ---------------------------------------------------------------------------


def _tenant(business_hours=_HOURS):
    return SimpleNamespace(
        initial_flows={"buttons": ["Serviços e Custo", "Horários", "Outro"]},
        appointment_types=[
            {"name": _SERVICE, "duration_min": 30, "is_active": True, "sort_order": 0}
        ],
        appointment_duration_min=30,
        business_hours=business_hours,
        collect_insurance=False,
        insurances=None,
    )


def _professional(*, business_hours=None, appointment_types=None, name="Dra. Ana"):
    """One professional snapshot, shaped exactly like `workers/tasks.py` builds.

    NULL on a config column means "inherit the clinic's", which is why both
    default to None: that is the state a real row is in until someone gives
    this doctor their own.
    """
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=appointment_types,
        business_hours=business_hours,
    )


def _conversation(**kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type=_SERVICE,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeCalendar:
    """A calendar that answers whatever `days` it was built with."""

    tzinfo = _TZ

    def __init__(self, has_days: bool = True) -> None:
        self.has_days = has_days
        self.day_scans = 0

    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.day_scans += 1
        if not self.has_days:
            return []
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        return [{"start": "2026-09-07T08:00", "end": "2026-09-07T08:30", "label": "08:00"}]


async def _confirm_service(tenant, professionals, calendar, selected_id=None):
    """Tap "Sim" on the service card — the step right before the day picker."""
    return await route(
        _conversation(flow_selected_professional_id=selected_id),
        tenant,
        calendar,
        "Sim",
        professionals=professionals,
    )


async def test_no_hours_anywhere_is_a_config_gap_not_an_empty_agenda():
    """The clinic has no hours and the doctor has none of their own: static gap."""
    ana = _professional()
    calendar = _FakeCalendar(has_days=True)

    res = await _confirm_service(_tenant(business_hours={}), [ana], calendar)

    assert res.action == "professional_config_incomplete"
    assert res.professional_config_gap == "hours"
    assert res.flow_selected_professional_id == ana.id
    assert res.flow_state is FlowState.IDLE
    (bubble,) = res.bubbles
    assert isinstance(bubble, TextBubble)
    # The new copy, and provably NOT the full-agenda one — collapsing the two
    # back into one string is the exact regression this file exists to stop.
    assert "Dra. Ana" in bubble.body
    assert bubble.body != NO_AVAILABLE_DAYS_MESSAGE
    assert "não encontrei horários livres" not in bubble.body.lower()
    # STATIC: decided before the calendar was ever asked.
    assert calendar.day_scans == 0


async def test_own_empty_hours_override_is_also_a_gap():
    """An own `{}` inherits NOTHING — the doctor is closed all week, on purpose
    or by accident, and either way no patient can book them."""
    ana = _professional(business_hours={})

    res = await _confirm_service(_tenant(), [ana], _FakeCalendar())

    assert res.action == "professional_config_incomplete"
    assert res.professional_config_gap == "hours"


async def test_full_agenda_still_answers_the_old_no_days_message():
    """REGRESSION: hours ARE configured, the window just has nothing free.

    Normal, self-correcting, nobody's mistake — so it must keep falling into
    the plain reply it always did, and must NOT alert anyone."""
    ana = _professional(business_hours=_HOURS)
    calendar = _FakeCalendar(has_days=False)

    res = await _confirm_service(_tenant(), [ana], calendar)

    assert res.action == "reply"
    assert res.professional_config_gap is None
    assert res.bubbles[0].body == NO_AVAILABLE_DAYS_MESSAGE
    # It reached the calendar: this branch is the *dynamic* one.
    assert calendar.day_scans == 1


async def test_a_complete_professional_still_reaches_the_day_picker():
    """REGRESSION: the happy path is untouched by the new branch."""
    ana = _professional(business_hours=_HOURS)
    calendar = _FakeCalendar(has_days=True)

    res = await _confirm_service(_tenant(), [ana], calendar)

    assert res.action == "reply"
    assert isinstance(res.bubbles[0], SlotsBubble)
    assert res.professional_config_gap is None


async def test_multi_doctor_judges_the_selected_professional_only():
    """One broken doctor on a clinic where the others are fine."""
    ana = _professional(business_hours={}, name="Dra. Ana")
    bruno = _professional(business_hours=_HOURS, name="Dr. Bruno")

    broken = await _confirm_service(_tenant(), [ana, bruno], _FakeCalendar(), selected_id=ana.id)
    healthy = await _confirm_service(_tenant(), [ana, bruno], _FakeCalendar(), selected_id=bruno.id)

    assert broken.action == "professional_config_incomplete"
    assert broken.flow_selected_professional_id == ana.id
    assert healthy.action == "reply"
    assert isinstance(healthy.bubbles[0], SlotsBubble)


def test_empty_service_list_signals_the_services_gap():
    """The check that already existed now SAYS why it is a dead end.

    Same bubbles as before — only the `action`/`gap` are new, so the patient's
    experience is unchanged and the clinic's is not."""
    ana = _professional(appointment_types=[])

    res = _enter_professional_services(ana, _tenant())

    assert res.action == "professional_config_incomplete"
    assert res.professional_config_gap == "services"
    assert res.flow_selected_professional_id == ana.id
    assert res.bubbles[-1].body == PROFESSIONAL_NO_SERVICES_MESSAGE


def test_a_professional_with_services_is_not_a_gap():
    """REGRESSION: the service list still renders for a configured doctor."""
    ana = _professional(
        appointment_types=[
            {"name": _SERVICE, "duration_min": 30, "is_active": True, "sort_order": 0}
        ]
    )

    res = _enter_professional_services(ana, _tenant())

    assert res.action == "reply"
    assert res.professional_config_gap is None


# ---------------------------------------------------------------------------
# Layer 2 — the call site: the email, the recipients, the debounce
# ---------------------------------------------------------------------------


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
def _wire_db(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    yield


class _FakeRedis:
    """Minimal async stub covering the two commands the debounce uses."""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.ttls: dict[str, int] = {}

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def setex(self, key, seconds, value):
        self.store[key] = value
        self.ttls[key] = seconds


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every alert email the handler tried to send, as plain dicts."""
    calls: list[dict] = []

    async def _fake_send(
        to_email, clinic_name, professional_name, gap, patient_name=None, patient_phone=None
    ):
        calls.append(
            {
                "to": to_email,
                "clinic": clinic_name,
                "professional": professional_name,
                "gap": gap,
                "patient_name": patient_name,
                "patient_phone": patient_phone,
            }
        )

    monkeypatch.setattr(tasks, "send_professional_config_incomplete_alert", _fake_send)
    return calls


@pytest.fixture(autouse=True)
def _no_whatsapp(monkeypatch: pytest.MonkeyPatch) -> list:
    """The patient-facing send is not what this layer is about — capture it."""
    captured: list = []

    async def _fake_dispatch(reply, bubbles, tenant=None, waba_token=None):
        captured.extend(bubbles)
        return len(bubbles)

    monkeypatch.setattr(tasks, "_dispatch_bubbles", _fake_dispatch)
    return captured


class _LogRecorder:
    """Records every structlog call — `caplog` is empty for this module, so an
    assertion over `caplog.records` would pass against any code at all."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **fields) -> None:
            self.records.append((level, event, fields))

        return _log

    @property
    def text(self) -> str:
        return "\n".join(f"{lvl} {event} {fields}" for lvl, event, fields in self.records)


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(tasks, "logger", recorder)
    return recorder


async def _seed(
    db,
    *,
    contact_email: str | None = CLINIC_EMAIL,
    professional_email: str | None = DOCTOR_EMAIL,
    professionals: int = 1,
) -> tuple[Tenant, list[Professional], Patient, Conversation]:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Chrysostomo For Eyes",
            phone_number_id=str(uuid4())[:12],
            contact_email=contact_email,
        )
        session.add(tenant)
        await session.flush()
        rows = [
            Professional(
                tenant_id=tenant.id,
                name=f"Dra. Ana {index}",
                is_active=True,
                email=professional_email,
            )
            for index in range(professionals)
        ]
        patient = Patient(tenant_id=tenant.id, wa_id=PATIENT_WA, name=PATIENT_NAME)
        session.add_all([*rows, patient])
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, flow_state=FlowState.IDLE
        )
        session.add(conversation)
        await session.commit()
        for obj in (tenant, patient, conversation, *rows):
            await session.refresh(obj)
        return tenant, rows, patient, conversation


def _reply(conversation) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_wa_id=PATIENT_WA,
        inbound_body="quero marcar",
        tenant_id=conversation.tenant_id,
    )


def _result(professional, gap: str = "hours") -> FlowRouterResult:
    return FlowRouterResult(
        action="professional_config_incomplete",
        bubbles=[TextBubble(body="No momento não é possível agendar.")],
        flow_state=FlowState.IDLE,
        flow_selected_professional_id=professional.id,
        professional_config_gap=gap,
    )


async def test_alerts_both_addresses_with_the_patient_in_the_body(db, sent):
    tenant, (ana,), _patient, conversation = await _seed(db)

    await tasks._handle_professional_config_incomplete(
        _reply(conversation), _result(ana), redis=_FakeRedis(), tenant=tenant
    )

    assert sorted(call["to"] for call in sent) == sorted([CLINIC_EMAIL, DOCTOR_EMAIL])
    for call in sent:
        assert call["clinic"] == "Chrysostomo For Eyes"
        assert call["professional"] == ana.name
        assert call["gap"] == "hours"
        # The whole point of the alert: WHO tried, and on WHICH number.
        assert call["patient_name"] == PATIENT_NAME
        assert call["patient_phone"] == PATIENT_WA


@pytest.mark.parametrize(
    ("contact_email", "professional_email", "expected"),
    [
        (CLINIC_EMAIL, DOCTOR_EMAIL, [CLINIC_EMAIL, DOCTOR_EMAIL]),
        (CLINIC_EMAIL, None, [CLINIC_EMAIL]),
        (None, DOCTOR_EMAIL, [DOCTOR_EMAIL]),
        (None, None, []),
        # Whitespace is not an address. A clinic that "cleared" the field by
        # blanking it must not produce a send to "   ".
        ("   ", "   ", []),
        # The same address on both rows is one recipient, not two emails.
        (CLINIC_EMAIL, CLINIC_EMAIL, [CLINIC_EMAIL]),
    ],
)
async def test_recipients_are_whichever_addresses_exist(
    db, sent, contact_email, professional_email, expected
):
    """Either may be absent; neither present is a no-op, never a failure."""
    tenant, (ana,), _patient, conversation = await _seed(
        db, contact_email=contact_email, professional_email=professional_email
    )

    # Must not raise even with nobody to write to — the patient has already
    # been answered by the time this runs.
    await tasks._handle_professional_config_incomplete(
        _reply(conversation), _result(ana), redis=_FakeRedis(), tenant=tenant
    )

    assert sorted(call["to"] for call in sent) == sorted(expected)


async def test_the_patient_is_answered_even_when_nobody_can_be_alerted(db, sent, _no_whatsapp):
    tenant, (ana,), _patient, conversation = await _seed(
        db, contact_email=None, professional_email=None
    )

    await tasks._handle_professional_config_incomplete(
        _reply(conversation), _result(ana), redis=_FakeRedis(), tenant=tenant
    )

    assert sent == []
    assert [b.body for b in _no_whatsapp] == ["No momento não é possível agendar."]


async def test_second_patient_within_the_window_does_not_resend(db, sent):
    """The debounce is per (tenant, professional, gap) — NOT per patient.

    Two different people hitting the same broken doctor is one incident, and
    the clinic must not get an email per patient."""
    tenant, (ana,), _patient, conversation = await _seed(db)
    redis = _FakeRedis()

    await tasks._handle_professional_config_incomplete(
        _reply(conversation), _result(ana), redis=redis, tenant=tenant
    )
    first_round = len(sent)

    # A different patient; same doctor, same gap, still inside the window.
    other = tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_wa_id="5511777776666",
        inbound_body="quero marcar",
        tenant_id=conversation.tenant_id,
    )
    await tasks._handle_professional_config_incomplete(
        other, _result(ana), redis=redis, tenant=tenant
    )

    assert first_round == 2  # clinic + doctor
    assert len(sent) == 2  # ...and nothing more


async def test_a_different_professional_or_gap_does_resend(db, sent):
    """Proof the key is scoped per professional AND per gap, not per tenant.

    A tenant-wide key would mean one doctor's missing hours silenced every
    other doctor's alert for four hours — the failure mode that made the
    original incident invisible in the first place."""
    tenant, (ana, bruno), _patient, conversation = await _seed(db, professionals=2)
    redis = _FakeRedis()
    reply = _reply(conversation)

    await tasks._handle_professional_config_incomplete(
        reply, _result(ana, "hours"), redis=redis, tenant=tenant
    )
    # Same doctor, DIFFERENT gap.
    await tasks._handle_professional_config_incomplete(
        reply, _result(ana, "services"), redis=redis, tenant=tenant
    )
    # DIFFERENT doctor, same gap.
    await tasks._handle_professional_config_incomplete(
        reply, _result(bruno, "hours"), redis=redis, tenant=tenant
    )

    assert len(sent) == 6  # 3 distinct incidents x 2 recipients
    assert len(redis.store) == 3
    assert all(key.startswith(f"professional_config:alert:{tenant.id}:") for key in redis.store)
    # Never the calendar outage's key — one incident must not mute the other.
    assert not any(key.startswith("calendar:alert:") for key in redis.store)


async def test_without_redis_every_turn_alerts(db, sent):
    """No Redis means no debounce, not no alert: the same fail-open choice
    `_handle_calendar_unavailable` makes."""
    tenant, (ana,), _patient, conversation = await _seed(db)
    reply = _reply(conversation)

    await tasks._handle_professional_config_incomplete(reply, _result(ana), tenant=tenant)
    await tasks._handle_professional_config_incomplete(reply, _result(ana), tenant=tenant)

    assert len(sent) == 4


async def test_a_professional_from_another_tenant_is_never_mailed(db, sent):
    """Mailing one clinic about another's doctor is not a mistake worth risking."""
    tenant, (_ana,), _patient, conversation = await _seed(db)
    _other_tenant, (stranger,), _p2, _c2 = await _seed(db)

    await tasks._handle_professional_config_incomplete(
        _reply(conversation), _result(stranger), redis=_FakeRedis(), tenant=tenant
    )

    assert sent == []


async def test_no_log_line_carries_the_patient_or_the_recipients(db, sent, log):
    """The body is where the PII goes; a log line is not.

    `send_*` receives the name and number (asserted above) — this pins that
    nothing on the way there writes them, or the addresses, into structlog."""
    tenant, (ana,), _patient, conversation = await _seed(db)

    await tasks._handle_professional_config_incomplete(
        _reply(conversation), _result(ana), redis=_FakeRedis(), tenant=tenant
    )

    assert sent, "the alert must actually have run for this assertion to mean anything"
    text = log.text
    assert PATIENT_NAME not in text
    assert PATIENT_WA not in text
    assert CLINIC_EMAIL not in text
    assert DOCTOR_EMAIL not in text
    # ...while still saying enough to debug: ids and the gap category.
    assert str(ana.id) in text
    assert "hours" in text


async def test_flow_router_never_logs_the_gap_with_patient_content(monkeypatch):
    """Same rule one layer up: the router's own logging must stay categorical."""
    recorder = _LogRecorder()
    monkeypatch.setattr(flow_router, "logger", recorder)
    ana = _professional(business_hours={})

    await _confirm_service(_tenant(business_hours={}), [ana], _FakeCalendar())

    assert PATIENT_NAME not in recorder.text
    assert PATIENT_WA not in recorder.text
