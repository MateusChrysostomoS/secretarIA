"""Tests for plugins/precheck_handoff.py — "your pre-consult is waiting".

Same in-memory-sqlite pattern as tests/test_professional_notification.py: a
real DB monkeypatched in place of `async_session_factory`. The brain-api call
(`request_precheck_handoff`) and the WhatsApp send (`WhatsAppClient`) are both
monkeypatched — neither an HTTP client nor a Meta socket is this file's
subject (tests/test_precheck_handoff.py and tests/test_waba_fail_closed.py own
those). What IS this file's subject is the hook's own responsibilities:

  - it is CORE (fires with every addon off) and reaches BOTH booking paths,
    which is the entire point: the deterministic router never asks an LLM
    anything, so before this hook a flow-booked patient was never offered a
    pre-consult at all;
  - only SEEDED / ALREADY_ACTIVE produce a message — a clinic without PreCheck,
    a missing `Clinic.brain_tenant_id`, a conflict and an outage are all
    SILENT, because nobody asked this hook for anything to begin with;
  - at most one message per appointment even when the arq job runs twice, and
    a message that did NOT go out gives the ledger key back so a re-run still
    can;
  - a failure here — including the real client's fail-closed credential check —
    cannot stop another post_booking hook in the same sweep;
  - no phone number and no patient name ever reaches the message body or a log
    line.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime  # noqa: E402
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

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Appointment, Patient, ProcessedEvent, Tenant  # noqa: E402
from secretaria.plugins import precheck_handoff as ph  # noqa: E402
from secretaria.plugins.base import PostBookingContext  # noqa: E402
from secretaria.plugins.registry import enabled_plugins  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.precheck import HandoffOutcome, HandoffResult  # noqa: E402

# The patient's identifiers in the shapes the DB really carries them. No test
# in this file may find any of them in a message body or a log line.
PATIENT_WA_ID = "5511988887777"
PATIENT_PHONE = "+55 11 98888-7777"
PATIENT_NAME = "Maria Silva"
# The PLATFORM's PreCheck number — shared by every tenant, and the one thing
# here that is allowed to appear in the message.
PRECHECK_NUMBER = "551140028922"

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "analytics_bi_advanced": False,
    "human_backup_24_7": False,
}


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


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
    monkeypatch.setattr(ph, "async_session_factory", db)
    yield


@pytest.fixture(autouse=True)
def precheck_number(monkeypatch: pytest.MonkeyPatch):
    """Configure the platform PreCheck number for every test in this file.

    `get_settings` is lru_cached, so the cache is cleared on BOTH ends: on the
    way in so the new value is seen, and on the way out so a test that changed
    it cannot leak a stale Settings into the rest of the suite.
    """
    from secretaria.config import get_settings

    monkeypatch.setenv("PRECHECK_WHATSAPP_NUMBER", PRECHECK_NUMBER)
    monkeypatch.setenv("PRECHECK_HANDOFF_PREFILL", "Olá! Quero fazer minha pré-consulta.")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _unset_precheck_number(monkeypatch: pytest.MonkeyPatch) -> None:
    from secretaria.config import get_settings

    monkeypatch.setenv("PRECHECK_WHATSAPP_NUMBER", "")
    get_settings.cache_clear()


class _Handoff:
    """Stands in for `request_precheck_handoff`: records asks, replays one outcome."""

    def __init__(self, outcome: HandoffOutcome = HandoffOutcome.SEEDED, explode: bool = False):
        self.outcome = outcome
        self.explode = explode
        self.calls: list[tuple] = []

    async def __call__(self, tenant_id, phone_number):
        if self.explode:
            raise RuntimeError("brain-api down")
        self.calls.append((tenant_id, phone_number))
        return HandoffResult(self.outcome)


@pytest.fixture
def handoff(monkeypatch: pytest.MonkeyPatch) -> _Handoff:
    fake = _Handoff()
    monkeypatch.setattr(ph, "request_precheck_handoff", fake)
    return fake


class _WhatsApp:
    """Stands in for `WhatsAppClient`, as both the class and the client.

    `for_tenant` returns `self`, so one object records the credential it was
    handed AND every message that left through it.
    """

    def __init__(self, explode: bool = False):
        self.explode = explode
        self.built: list[tuple] = []
        self.calls: list[dict] = []

    def for_tenant(self, tenant, access_token):
        self.built.append((tenant, access_token))
        return self

    async def send_text_message(self, to: str, body: str) -> dict:
        if self.explode:
            raise RuntimeError("meta 5xx")
        self.calls.append({"to": to, "body": body})
        return {"messages": [{"id": "wamid.TEST"}]}

    @property
    def bodies(self) -> list[str]:
        return [call["body"] for call in self.calls]


@pytest.fixture(autouse=True)
def whatsapp(monkeypatch: pytest.MonkeyPatch) -> _WhatsApp:
    """Autouse so no test can reach the real Cloud API by forgetting to ask."""
    fake = _WhatsApp()
    monkeypatch.setattr(ph, "WhatsAppClient", fake)
    return fake


class _LogRecorder:
    """Records every structlog call — see tests/test_professional_notification.py.

    `caplog` is empty for these modules (structlog renders through its own
    factory, not stdlib logging), so a leak assertion over `caplog.records`
    would pass against any code at all.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **fields) -> None:
            self.records.append((level, event, fields))

        return _log

    @property
    def events(self) -> list[str]:
        return [event for _level, event, _fields in self.records]

    def at(self, level: str) -> list[tuple[str, dict]]:
        return [(e, f) for lvl, e, f in self.records if lvl == level]

    @property
    def text(self) -> str:
        return "\n".join(f"{lvl} {event} {fields}" for lvl, event, fields in self.records)


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(ph, "logger", recorder)
    return recorder


async def _claimed(db, appointment_id) -> bool:
    """Is the ledger key for this appointment currently held?"""
    async with db() as session:
        row = await session.scalar(
            select(ProcessedEvent.id).where(ProcessedEvent.event_id == f"precheck:{appointment_id}")
        )
    return row is not None


async def _make_rows(db, *, with_patient: bool = True, phone_number_id: str | None = None):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinica Boa Saude",
            # `tenants.phone_number_id` is UNIQUE: a test building two clinics
            # needs two numbers. Explicit "" is how the credential test asks
            # for a tenant the real client must refuse to send for.
            phone_number_id=str(uuid4())[:12] if phone_number_id is None else phone_number_id,
            timezone="America/Sao_Paulo",
        )
        session.add(tenant)
        patient = None
        patient_id = None
        if with_patient:
            patient = Patient(
                id=uuid4(), tenant_id=tenant.id, wa_id=PATIENT_WA_ID, name=PATIENT_NAME
            )
            patient_id = patient.id
            session.add(patient)
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient_id,
            google_event_id="evt-1",
            appointment_type="Primeira consulta",
            start_at=datetime(2026, 8, 3, 17, 0, tzinfo=UTC),
            end_at=datetime(2026, 8, 3, 17, 30, tzinfo=UTC),
            phone=PATIENT_PHONE,
        )
        session.add(appointment)
        await session.commit()
        for row in (tenant, patient, appointment):
            if row is not None:
                await session.refresh(row)
        return tenant, patient, appointment


def _ctx(tenant, patient, appointment, *, source="flow", redis=None) -> PostBookingContext:
    """A PostBookingContext carrying DETACHED rows — what
    plugins/post_booking.py::run_post_booking_hooks hands a hook in production."""
    return PostBookingContext(
        tenant=tenant,
        patient=patient,
        appointment=appointment,
        waba_token="tok",
        source=source,
        redis=redis,
    )


# --------------------------------------------------------------------------
# CORE, not an add-on
# --------------------------------------------------------------------------


def test_plugin_is_core_and_needs_no_addon():
    """Every addon off, plain tier: the hook still runs.

    Deliberate — the real "may this clinic use PreCheck?" gate lives in
    brain-api and comes back as `NOT_ENTITLED`. A second local gate could only
    drift from it.
    """
    summary = _summary(addons=dict(_ALL_ADDONS_OFF))
    assert "precheck_handoff" in [s.id for s in enabled_plugins(summary)]


def test_plugin_is_disabled_for_an_inactive_subscription():
    """Core still means "while the subscription is live"."""
    summary = _summary(active=False, status="canceled")
    assert "precheck_handoff" not in [s.id for s in enabled_plugins(summary)]


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["flow", "agent"])
async def test_offers_the_pre_consult_from_both_booking_paths(db, handoff, whatsapp, source):
    """Both commit points funnel into the same arq job, so both must reach the
    hook. The deterministic router is the one that matters most: it never
    consults an LLM, so `iniciar_pre_consulta` could never fire for it.
    """
    tenant, patient, appointment = await _make_rows(db)

    await ph._post_booking(_ctx(tenant, patient, appointment, source=source))

    assert handoff.calls == [(tenant.id, PATIENT_WA_ID)]
    assert len(whatsapp.calls) == 1
    assert whatsapp.calls[0]["to"] == PATIENT_WA_ID
    body = whatsapp.bodies[0]
    assert f"https://wa.me/{PRECHECK_NUMBER}" in body
    assert "pré-consulta" in body
    # The tenant's own WABA credential, never the env scaffold.
    assert whatsapp.built == [(tenant, "tok")]


async def test_an_already_active_session_still_gets_the_link(db, handoff, whatsapp):
    """ALREADY_ACTIVE means the session is waiting at the other number — the
    patient still needs to be told how to reach it."""
    tenant, patient, appointment = await _make_rows(db)
    handoff.outcome = HandoffOutcome.ALREADY_ACTIVE

    await ph._post_booking(_ctx(tenant, patient, appointment))

    assert len(whatsapp.calls) == 1


async def test_the_handoff_is_asked_for_this_conversations_tenant(db, handoff, whatsapp):
    """Isolation invariant: the hand-off is always for the booking's own clinic."""
    tenant_a, patient_a, appointment_a = await _make_rows(db)
    tenant_b, _patient_b, _appointment_b = await _make_rows(db)

    await ph._post_booking(_ctx(tenant_a, patient_a, appointment_a))

    assert handoff.calls == [(tenant_a.id, PATIENT_WA_ID)]
    assert tenant_b.id not in [call[0] for call in handoff.calls]


# --------------------------------------------------------------------------
# Silence: this hook was not asked for anything, so it apologises for nothing
# --------------------------------------------------------------------------


async def test_a_clinic_without_precheck_gets_no_message(db, handoff, whatsapp, log):
    """The acceptance criterion: nothing changes for a clinic that did not buy
    PreCheck — no extra message, and no visible error either."""
    tenant, patient, appointment = await _make_rows(db)
    handoff.outcome = HandoffOutcome.NOT_ENTITLED

    await ph._post_booking(_ctx(tenant, patient, appointment))

    assert whatsapp.calls == []
    assert log.at("error") == []
    assert "precheck_handoff_post_booking_skipped" in log.events


@pytest.mark.parametrize(
    "outcome",
    [HandoffOutcome.NO_CLINIC, HandoffOutcome.CONFLICT, HandoffOutcome.UNAVAILABLE],
)
async def test_no_message_for_any_non_deliverable_outcome(db, handoff, whatsapp, outcome):
    """A missing `Clinic.brain_tenant_id`, an already-running session elsewhere
    and a plain outage are all the same to the patient: silence."""
    tenant, patient, appointment = await _make_rows(db)
    handoff.outcome = outcome

    await ph._post_booking(_ctx(tenant, patient, appointment))

    assert whatsapp.calls == []


async def test_no_precheck_number_configured_is_a_logged_noop(
    db, monkeypatch, handoff, whatsapp, log
):
    """Without the platform number there is no link to send, so brain-api is
    not even asked — and the reason is on the record rather than implied."""
    tenant, patient, appointment = await _make_rows(db)
    _unset_precheck_number(monkeypatch)

    await ph._post_booking(_ctx(tenant, patient, appointment))

    assert handoff.calls == []
    assert whatsapp.calls == []
    events = log.at("info")
    assert events and events[0][1]["reason"] == "precheck_number_not_configured"
    # Nothing was claimed either: a structural no-op must not burn a key.
    assert not await _claimed(db, appointment.id)


async def test_a_booking_with_no_patient_is_a_noop(db, handoff, whatsapp):
    """A block slot created from the doctor hub has nobody to invite."""
    tenant, _patient, appointment = await _make_rows(db, with_patient=False)

    await ph._post_booking(_ctx(tenant, None, appointment))

    assert handoff.calls == []
    assert whatsapp.calls == []
    assert not await _claimed(db, appointment.id)


async def test_a_handoff_that_raises_is_contained(db, monkeypatch, whatsapp, log):
    """`request_precheck_handoff` fails closed by contract; if it ever stops
    doing so, the hook must not leave a claim behind with no message."""
    tenant, patient, appointment = await _make_rows(db)
    monkeypatch.setattr(ph, "request_precheck_handoff", _Handoff(explode=True))

    await ph._post_booking(_ctx(tenant, patient, appointment))

    assert whatsapp.calls == []
    assert not await _claimed(db, appointment.id)
    assert log.at("warning")


# --------------------------------------------------------------------------
# Idempotency: at most one message per appointment, failures stay retryable
# --------------------------------------------------------------------------


async def test_running_the_hook_twice_sends_one_message(db, handoff, whatsapp):
    """arq retries jobs. A second identical invitation is direct noise to the
    patient — and the claim is taken BEFORE the hand-off, so the second run
    does not even re-ask brain-api."""
    tenant, patient, appointment = await _make_rows(db)

    await ph._post_booking(_ctx(tenant, patient, appointment))
    await ph._post_booking(_ctx(tenant, patient, appointment))

    assert len(whatsapp.calls) == 1
    assert len(handoff.calls) == 1


async def test_two_appointments_each_get_their_own_message(db, handoff, whatsapp):
    """The claim is per appointment, not per patient: booking twice is two
    invitations, not one."""
    tenant, patient, first = await _make_rows(db)
    async with db() as session:
        second = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id="evt-2",
            appointment_type="Retorno",
            start_at=datetime(2026, 9, 1, 17, 0, tzinfo=UTC),
            end_at=datetime(2026, 9, 1, 17, 30, tzinfo=UTC),
        )
        session.add(second)
        await session.commit()
        await session.refresh(second)

    await ph._post_booking(_ctx(tenant, patient, first))
    await ph._post_booking(_ctx(tenant, patient, second))

    assert len(whatsapp.calls) == 2


async def test_a_refused_handoff_gives_the_claim_back(db, handoff, whatsapp):
    """A clinic that gets PreCheck enabled tomorrow must not find every past
    booking permanently marked as "already sent"."""
    tenant, patient, appointment = await _make_rows(db)
    handoff.outcome = HandoffOutcome.NOT_ENTITLED

    await ph._post_booking(_ctx(tenant, patient, appointment))

    assert not await _claimed(db, appointment.id)


async def test_a_failed_send_gives_the_claim_back_so_a_rerun_delivers(db, monkeypatch, handoff):
    """The hook schedules no retry of its own — but arq re-running the
    surrounding job is a real second chance, and it only exists if the key is
    released. A second hand-off is an idempotent `already_active`."""
    tenant, patient, appointment = await _make_rows(db)
    exploding = _WhatsApp(explode=True)
    monkeypatch.setattr(ph, "WhatsAppClient", exploding)

    await ph._post_booking(_ctx(tenant, patient, appointment))
    assert exploding.calls == []
    assert not await _claimed(db, appointment.id)

    working = _WhatsApp()
    monkeypatch.setattr(ph, "WhatsAppClient", working)
    handoff.outcome = HandoffOutcome.ALREADY_ACTIVE
    await ph._post_booking(_ctx(tenant, patient, appointment))

    assert len(working.calls) == 1
    # ...and now the claim IS a receipt: a third sweep sends nothing.
    await ph._post_booking(_ctx(tenant, patient, appointment))
    assert len(working.calls) == 1


async def test_a_missing_waba_credential_is_contained_not_raised(db, monkeypatch, handoff, log):
    """The REAL client, fail-closed (PROMPT_FIX_21): a tenant with no
    `phone_number_id` raises rather than borrowing the env scaffold's number.
    That exception must die inside this hook."""
    from secretaria.services.whatsapp import WhatsAppClient

    monkeypatch.setattr(ph, "WhatsAppClient", WhatsAppClient)
    tenant, patient, appointment = await _make_rows(db, phone_number_id="")

    await ph._post_booking(_ctx(tenant, patient, appointment))

    warnings = log.at("warning")
    assert warnings and warnings[0][1]["reason"] == "send_failed"
    assert not await _claimed(db, appointment.id)


# --------------------------------------------------------------------------
# LGPD: the patient is never the payload
# --------------------------------------------------------------------------


async def test_no_phone_or_patient_name_in_the_message_body(db, handoff, whatsapp):
    """The body carries the platform's number and nothing about the patient."""
    tenant, patient, appointment = await _make_rows(db)

    await ph._post_booking(_ctx(tenant, patient, appointment))

    body = whatsapp.bodies[0]
    assert PATIENT_WA_ID not in body
    assert PATIENT_PHONE not in body
    assert PATIENT_NAME not in body


@pytest.mark.parametrize(
    "outcome",
    [
        HandoffOutcome.SEEDED,
        HandoffOutcome.NOT_ENTITLED,
        HandoffOutcome.NO_CLINIC,
        HandoffOutcome.CONFLICT,
        HandoffOutcome.UNAVAILABLE,
    ],
)
async def test_no_phone_or_patient_name_in_the_logs(db, handoff, whatsapp, log, outcome):
    """Every outcome, including the happy one: ids only."""
    tenant, patient, appointment = await _make_rows(db)
    handoff.outcome = outcome

    await ph._post_booking(_ctx(tenant, patient, appointment))

    assert log.records, "the hook must say what it did"
    assert PATIENT_WA_ID not in log.text
    assert PATIENT_PHONE not in log.text
    assert PATIENT_NAME not in log.text


async def test_a_failed_send_logs_no_phone_and_no_body(db, monkeypatch, handoff, log):
    """The failure path is the tempting one to over-log: a Meta error body
    echoes the recipient's number and the message text straight back at us."""
    tenant, patient, appointment = await _make_rows(db)
    monkeypatch.setattr(ph, "WhatsAppClient", _WhatsApp(explode=True))

    await ph._post_booking(_ctx(tenant, patient, appointment))

    assert PATIENT_WA_ID not in log.text
    assert PATIENT_NAME not in log.text
    assert "wa.me" not in log.text


# --------------------------------------------------------------------------
# Containment: one hook's failure is not another hook's problem
# --------------------------------------------------------------------------


async def _run_sweep_with_pix(db, monkeypatch, tenant, patient, appointment) -> list[str]:
    """Run a real `run_post_booking` sweep with this hook FIRST and pix_deposit
    behind it, and report which siblings actually ran.

    Order is forced: with the natural import order pix_deposit registers
    earlier, so the assertion would hold whether or not the isolation existed.
    """
    from secretaria.plugins import pix_deposit
    from secretaria.plugins.registry import run_post_booking
    from secretaria.services.payments import deposit_lifecycle

    ran: list[str] = []

    async def _recorder(*args, **kwargs):
        ran.append("pix_deposit")
        return None

    monkeypatch.setattr(deposit_lifecycle, "maybe_create_deposit", _recorder)
    monkeypatch.setattr(pix_deposit, "async_session_factory", db)
    monkeypatch.setattr(
        "secretaria.plugins.registry.REGISTRY",
        {
            "precheck_handoff": ph.PRECHECK_HANDOFF_SPEC,
            "pix_deposit": pix_deposit.PIX_DEPOSIT_SPEC,
        },
    )
    summary = _summary(addons={**_ALL_ADDONS_OFF, "pix_deposit": True})
    await run_post_booking(summary, _ctx(tenant, patient, appointment))
    return ran


async def test_a_raising_hook_does_not_stop_the_other_post_booking_hooks(db, monkeypatch):
    """brain-api being down must not cost the tenant its Pix deposit send in the
    same sweep — nor, upstream of that, the booking itself, which was committed
    long before this job started."""
    tenant, patient, appointment = await _make_rows(db)
    monkeypatch.setattr(ph, "request_precheck_handoff", _Handoff(explode=True))

    ran = await _run_sweep_with_pix(db, monkeypatch, tenant, patient, appointment)

    assert ran == ["pix_deposit"]


async def test_a_refused_handoff_does_not_stop_the_other_post_booking_hooks(db, monkeypatch):
    """The ordinary "this clinic has no PreCheck" path does real work — it
    releases a ledger key — and none of it may leak into the sweep."""
    tenant, patient, appointment = await _make_rows(db)
    monkeypatch.setattr(ph, "request_precheck_handoff", _Handoff(HandoffOutcome.NOT_ENTITLED))

    ran = await _run_sweep_with_pix(db, monkeypatch, tenant, patient, appointment)

    assert ran == ["pix_deposit"]
