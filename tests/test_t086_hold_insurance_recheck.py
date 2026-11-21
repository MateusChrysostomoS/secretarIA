"""TASK-086 A19/A20: the convênio is re-checked when a held booking becomes real.

A19 - Portal code gate (`workers/shared/booking_hold.py::_promote_booking_hold`): the hold may
live up to an hour while the e-mail code is typed, and the clinic may stop accepting the
patient's convênio for that (service, unit) meanwhile. The promotion re-reads the matrix from
the database BEFORE the event is written: refused -> no event, no appointment, the hold released,
the account still confirmed, and the conversation back at the convênio question with the
service and doctor kept. Accepted (or untouched matrix) -> booked exactly as before.

A20 - Pix promotion (`services/booking_promotion.py::promote`): the patient already paid, so the
booking is NEVER refused there; a refused cell is only logged (ids only).

Same end-to-end harness as tests/test_confirm_recheck_slot.py section 4 (Google in memory, the
whole worker pipeline real).
"""

import os

from tests._patching import workers_ns

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    BookingHold,
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    Patient,
    PixDeposit,
    Professional,
    ProfessionalInsurancePlan,
    Service,
    Tenant,
    TenantInsurancePlan,
)
from secretaria.models.insurance_acceptance_exception import (  # noqa: E402
    InsuranceAcceptanceException,
)
from secretaria.models.pix_deposit import PixDepositStatus  # noqa: E402
from secretaria.services import (  # noqa: E402  # noqa: E402
    booking_hold as holds,
    booking_promotion,
    insurance_acceptance,
)
from secretaria.services.calendar import CalendarService  # noqa: E402
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.flow_router import STEP_AWAITING_INSURANCE  # noqa: E402
from secretaria.services.pending_identity import (  # noqa: E402
    CODE_ACCEPTED_MESSAGE,
    RequestCodeOutcome,
    RequestCodeResult,
    VerifyOutcome,
    VerifyResult,
)
from secretaria.services.tenant_config import set_google_refresh_token  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared import booking_hold as worker_holds  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
EXTERNAL_ID = "00000000-0000-4000-8000-0000000000cd"


class _Recorder:
    """structlog caches its loggers: record the refusal log calls directly."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def info(self, event: str, **kw) -> None:
        self.calls.append((event, kw))

    def warning(self, event: str, **kw) -> None:
        self.calls.append((event, kw))

    def events(self, name: str) -> list[dict]:
        return [kw for event, kw in self.calls if event == name]


@pytest.fixture
def refusal_log(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(insurance_acceptance, "logger", recorder)
    return recorder


class _FakeGoogle:
    def __init__(self) -> None:
        self.created: list[tuple] = []

    def install(self, monkeypatch) -> None:
        google = self

        async def check_availability(self, start, end, max_results=50):
            return []

        async def create_event(self, start, end, summary, description="", reminders=None, **kw):
            eid = kw.get("event_id") or f"evt-{len(google.created) + 1}"
            google.created.append((self._calendar_id, eid))
            return {"id": eid, "htmlLink": f"https://calendar.google.com/{eid}"}

        async def list_available_days(self, start_day, days, slot_minutes=None, **kw):
            base = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            return [base + timedelta(days=3)]

        async def list_free_slots(self, day, slot_minutes=None, max_slots=6, **kw):
            return []

        for name, fn in {
            "check_availability": check_availability,
            "create_event": create_event,
            "list_available_days": list_available_days,
            "list_free_slots": list_free_slots,
        }.items():
            monkeypatch.setattr(CalendarService, name, fn)


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


@pytest.fixture
def google(monkeypatch) -> _FakeGoogle:
    g = _FakeGoogle()
    g.install(monkeypatch)
    return g


@pytest.fixture
def portal(monkeypatch, db):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    monkeypatch.setattr(holds, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))

    async def _none(*a, **k):
        return None

    async def _token(session, tenant_id):
        return "decrypted-waba-token"

    async def _ent(tenant_id, redis):
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

    async def _verify(tenant_id, external_id, code):
        return VerifyResult(VerifyOutcome.VERIFIED)

    async def _request(tenant_id, external_id):
        return RequestCodeResult(RequestCodeOutcome.NOT_PENDING)

    async def _no_llm(*a, **k):
        raise AssertionError("no turn of this journey may reach the LLM")

    monkeypatch.setattr(workers_ns, "resolve_patient_opening_state", _none)
    monkeypatch.setattr(workers_ns, "get_waba_token", _token)
    monkeypatch.setattr(workers_ns, "get_entitlements", _ent)
    monkeypatch.setattr(workers_ns, "verify_code", _verify)
    monkeypatch.setattr(workers_ns, "enqueue_post_booking_hooks", _none)
    monkeypatch.setattr(workers_ns, "run_agent", _no_llm)
    monkeypatch.setattr(holds, "request_code", _request)
    monkeypatch.setattr(workers_ns, "request_code", _request)


async def _seed(db, mode: str = "shared", *, phone_number_id: str = "1234567890"):
    """A two-doctor clinic collecting convênios; Consulta is a linked catalog service."""
    async with db() as s:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica QA",
            phone_number_id=phone_number_id,
            is_active=True,
            timezone="America/Sao_Paulo",
            initial_flows={},
            google_calendar_id="clinic-cal",
            google_calendar_mode="shared_account",
            collect_insurance=True,
            insurance_mode=mode,
        )
        s.add(tenant)
        await s.flush()
        consulta = Service(tenant_id=tenant.id, name="Consulta", normalized_name="consulta")
        s.add(consulta)
        await s.flush()
        tenant.appointment_types = [
            {
                "name": "Consulta",
                "is_active": True,
                "duration_min": 30,
                "service_id": str(consulta.id),
            }
        ]
        # TASK-088: the convênio question is the day step's own choke point (`_ask_day`), which
        # first checks the doctor has hours - so the fixture doctors have some.
        hours = {"monday": [{"start": "08:00", "end": "18:00"}]}
        dr_a = Professional(
            tenant_id=tenant.id, name="Dr. A", google_calendar_id="cal-dr-a", business_hours=hours
        )
        dr_b = Professional(
            tenant_id=tenant.id, name="Dr. B", google_calendar_id="cal-dr-b", business_hours=hours
        )
        s.add_all([dr_a, dr_b])
        await s.flush()
        if mode == "shared":
            plan = TenantInsurancePlan(tenant_id=tenant.id, custom_name="Plano A")
        else:
            plan = ProfessionalInsurancePlan(
                tenant_id=tenant.id, professional_id=dr_a.id, custom_name="Plano A"
            )
        s.add(plan)
        # TASK-088: a second plan the clinic never refuses, so the refused one is asked again from
        # a list (with nothing else accepted the booking goes Particular instead, with a line).
        s.add(
            TenantInsurancePlan(tenant_id=tenant.id, custom_name="Plano B")
            if mode == "shared"
            else ProfessionalInsurancePlan(
                tenant_id=tenant.id, professional_id=dr_a.id, custom_name="Plano B"
            )
        )
        patient = Patient(
            id=uuid4(),
            tenant_id=tenant.id,
            channel=CHANNEL_BRAIN_MESSAGE,
            external_id=EXTERNAL_ID,
            name="Maria",
            lgpd_accepted_at=datetime.now(UTC),
        )
        s.add(patient)
        await s.flush()
        conv = Conversation(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_state=FlowState.AWAITING_EMAIL_CODE,
        )
        s.add(conv)
        await set_google_refresh_token(s, tenant.id, "clinic-token")
        await s.commit()
        return SimpleNamespace(
            tenant=tenant,
            dr_a=dr_a,
            consulta=consulta,
            plan=plan,
            patient=patient,
            conv=conv,
            mode=mode,
        )


async def _refuse(db, w, *, tenant_id=None):
    async with db() as s:
        if w.mode == "shared":
            row = InsuranceAcceptanceException(
                tenant_id=tenant_id or w.tenant.id,
                service_id=w.consulta.id,
                unit_id=None,
                tenant_plan_id=w.plan.id,
            )
        else:
            row = InsuranceAcceptanceException(
                tenant_id=tenant_id or w.tenant.id,
                professional_id=w.dr_a.id,
                service_id=w.consulta.id,
                unit_id=None,
                professional_plan_id=w.plan.id,
            )
        s.add(row)
        await s.commit()


def _window() -> tuple[datetime, datetime]:
    start = (datetime.now(TZ) + timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    return start, start + timedelta(minutes=30)


async def _hold(w, insurance: str | None = "Plano A"):
    start, end = _window()
    held = await holds.place_hold(
        tenant_id=w.tenant.id,
        conversation_id=w.conv.id,
        patient_id=w.patient.id,
        professional_id=w.dr_a.id,
        appointment_type="Consulta",
        insurance=insurance,
        start_at=start.astimezone(UTC),
        end_at=end.astimezone(UTC),
    )
    assert held is not None


async def _portal(w, text_, reply_id=None):
    await tasks.process_brain_message_inbound(
        {}, str(w.tenant.id), EXTERNAL_ID, text=text_, interactive_reply_id=reply_id
    )


async def _outbound(db, conv) -> list[str]:
    async with db() as s:
        return list(
            (
                await s.scalars(
                    select(Message.body)
                    .where(
                        Message.conversation_id == conv.id,
                        Message.direction == MessageDirection.OUTBOUND,
                    )
                    .order_by(Message.created_at, text("messages.rowid"))
                )
            ).all()
        )


async def _rows(db, model) -> list:
    async with db() as s:
        return list((await s.scalars(select(model))).all())


# --------------------------------------------------------------------------
# A19 - the Portal code gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_a_convenio_refused_while_the_code_was_typed_books_nothing_and_asks_again(
    db, google, portal, refusal_log, mode
):
    w = await _seed(db, mode)
    await _hold(w)
    # While the code was being typed, the clinic stopped taking Plano A for Consulta.
    await _refuse(db, w)

    await _portal(w, "123456")

    assert google.created == [], "no event may be written"
    assert await _rows(db, Appointment) == []
    assert await _rows(db, BookingHold) == [], "the hold must go back on sale"
    sent = await _outbound(db, w.conv)
    assert CODE_ACCEPTED_MESSAGE in sent
    assert worker_holds.INSURANCE_REFUSED_AT_CONFIRM_MESSAGE in "\n".join(sent)
    assert not any("confirmado" in body for body in sent)
    async with db() as s:
        row = await s.get(Conversation, w.conv.id)
    # Back at the convênio question, the service and the doctor kept, the old answer gone.
    assert row.flow_state == FlowState.SERVICE_CATALOG
    assert row.flow_step == STEP_AWAITING_INSURANCE
    assert row.flow_selected_type == "Consulta"
    assert row.flow_selected_professional_id == w.dr_a.id
    assert row.flow_selected_insurance is None
    [logged] = refusal_log.events("booking_hold_insurance_refused")
    assert logged["tenant_id"] == str(w.tenant.id)
    assert logged["service_id"] == str(w.consulta.id)
    assert "Plano A" not in str(logged) and "Maria" not in str(logged)


async def test_an_accepted_convenio_books_exactly_as_before(db, google, portal, refusal_log):
    w = await _seed(db)
    await _hold(w)

    await _portal(w, "123456")

    [appointment] = await _rows(db, Appointment)
    assert appointment.insurance == "Plano A" and appointment.professional_id == w.dr_a.id
    assert google.created == [("cal-dr-a", "evt-1")]
    assert "Pronto! Seu agendamento está confirmado." in (await _outbound(db, w.conv))[-1]
    assert refusal_log.events("booking_hold_insurance_refused") == []


async def test_particular_is_never_refused(db, google, portal):
    w = await _seed(db)
    await _hold(w, insurance="Particular")
    await _refuse(db, w)

    await _portal(w, "123456")

    assert len(await _rows(db, Appointment)) == 1


async def test_another_clinics_matrix_never_refuses_this_booking(db, google, portal):
    w = await _seed(db)
    other = await _seed(db, phone_number_id="999")
    # A row of the OTHER clinic, even pointing at this clinic's own ids.
    await _refuse(db, w, tenant_id=other.tenant.id)
    await _hold(w)

    await _portal(w, "123456")

    assert len(await _rows(db, Appointment)) == 1


async def test_a_failed_matrix_read_books_as_before(db, google, portal, monkeypatch):
    """Fail-open like the slot re-check: a broken read never blocks a verified patient."""
    w = await _seed(db)
    await _hold(w)
    await _refuse(db, w)

    async def _boom(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(worker_holds, "booking_refused", _boom)

    await _portal(w, "123456")

    assert len(await _rows(db, Appointment)) == 1


# --------------------------------------------------------------------------
# A20 - the Pix promotion: never refused, only logged
# --------------------------------------------------------------------------


class _PixCalendar:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def create_event(self, start, end, summary, description="", **kw):
        self.created.append(kw.get("event_id"))
        return {"id": kw.get("event_id"), "htmlLink": None}


async def _promote(db, w):
    start, end = _window()
    calendar = _PixCalendar()
    async with db() as session:
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=w.tenant.id,
            asaas_payment_id=f"pay_{uuid4().hex[:8]}",
            provider="asaas",
            amount_cents=6000,
            percent_applied=30,
            status=PixDepositStatus.PAID,
        )
        session.add(deposit)
        await session.commit()
        snapshot = booking_promotion.BookingSnapshot(
            tenant_id=w.tenant.id,
            conversation_id=w.conv.id,
            patient_id=w.patient.id,
            professional_id=w.dr_a.id,
            appointment_type="Consulta",
            insurance="Plano A",
            attendee_name=None,
            start_at=start.astimezone(UTC),
            end_at=end.astimezone(UTC),
        )
        tenant = await session.get(Tenant, w.tenant.id)
        promoted = await booking_promotion.promote(session, tenant, deposit, snapshot, calendar)
    return promoted, calendar


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_a_paid_booking_is_promoted_even_when_the_convenio_was_refused_meanwhile(
    db, refusal_log, mode
):
    w = await _seed(db, mode)
    await _refuse(db, w)

    promoted, calendar = await _promote(db, w)

    assert promoted.created is True and len(calendar.created) == 1
    [appointment] = await _rows(db, Appointment)
    assert appointment.insurance == "Plano A"
    [logged] = refusal_log.events("booking_promotion_insurance_no_longer_accepted")
    assert logged["appointment_id"] == str(appointment.id)
    assert logged["service_id"] == str(w.consulta.id)
    assert "Plano A" not in str(logged)


async def test_a_paid_booking_with_an_accepted_convenio_logs_nothing(db, refusal_log):
    w = await _seed(db)

    promoted, _calendar = await _promote(db, w)

    assert promoted.created is True
    assert refusal_log.events("booking_promotion_insurance_no_longer_accepted") == []


async def test_booking_service_id_follows_the_doctors_linked_entry(db):
    w = await _seed(db)
    other = await _seed(db, phone_number_id="777")
    async with db() as session:
        assert (
            await booking_promotion.booking_service_id(session, w.tenant.id, w.dr_a.id, "consulta")
            == w.consulta.id
        )
        assert (
            await booking_promotion.booking_service_id(session, w.tenant.id, w.dr_a.id, "Exame")
            is None
        )
        # another clinic's doctor never resolves against this clinic
        assert (
            await booking_promotion.booking_service_id(
                session, w.tenant.id, other.dr_a.id, "Consulta"
            )
            is None
        )
