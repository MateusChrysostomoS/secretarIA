"""Doctor-initiated cancellation: the patient is ALWAYS told, and told properly.

Ground truth: services/cancellation_notice.py, api/hub/calendar.py,
workers/tasks.py::send_cancellation_notice.

The bug this closes: cancelling from the hub notified the patient only when the
doctor happened to type a message. A blank box cancelled the consultation in
silence and the patient found out by turning up. The notice is now composed
server-side and sent unconditionally; the doctor's text is a *justification*
quoted inside it.

Fixture shape mirrors test_hub_calendar_money.py (fake Calendar, fake arq pool,
in-memory SQLite) so no Google or Redis call is ever attempted.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from arq import Retry  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub import calendar as hub_calendar  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Professional,
    Tenant,
)
from secretaria.services import cancellation_notice as notice  # noqa: E402
from secretaria.services.tenant_config import set_google_refresh_token  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

CALENDAR = "/tenants/me/calendar"
PATIENT_WA = "5511988887777"
# Where an abandoned notice escalates to. Must never appear in a log line.
CLINIC_EMAIL = "contato@clinica.example"


# ---------------------------------------------------------------------------
# Pure builders — no DB, no network
# ---------------------------------------------------------------------------


def test_text_with_justification_quotes_the_doctors_words():
    text = notice.build_cancellation_text("Dra. Ana", "Imprevisto do medico")
    assert text.startswith("O médico Dra. Ana desmarcou a sua consulta!")
    assert 'Justificativa do médico: "Imprevisto do medico"' in text


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_text_without_justification_is_just_the_standard_sentence(blank):
    """Cancelling with no reason is a supported path, not a degraded one — it
    must not render an empty quote."""
    text = notice.build_cancellation_text("Dra. Ana", blank)
    assert text == "O médico Dra. Ana desmarcou a sua consulta!"
    assert "Justificativa" not in text


def test_text_without_a_professional_never_invents_a_name():
    assert notice.build_cancellation_text(None, None) == (
        "O médico responsável desmarcou a sua consulta!"
    )


def test_every_button_label_fits_whatsapps_20_char_cap():
    """send_buttons truncates past 20 SILENTLY, so an over-long label would
    reach the patient cut in half (the spec's own wording was 21 and 24)."""
    for _bid, label in notice.rebook_buttons(uuid4()):
        assert len(label) <= 20, label


def test_window_is_open_only_for_a_recent_inbound():
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    assert notice.is_inside_window(now - timedelta(hours=23, minutes=59), now=now)
    assert not notice.is_inside_window(now - timedelta(hours=24, minutes=1), now=now)


def test_never_having_written_counts_as_outside_the_window():
    """The safe direction: a free-form send would just be rejected by Meta."""
    assert notice.is_inside_window(None) is False


def test_deep_link_keeps_only_digits():
    assert notice.whatsapp_deep_link("+55 (11) 98888-7777") == "https://wa.me/5511988887777"
    assert notice.whatsapp_deep_link(None) is None
    assert notice.whatsapp_deep_link("abc") is None


# ---------------------------------------------------------------------------
# Fixtures
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
    # Exposed so the N+1 guard below can listen on statements without reaching
    # into the fixture's internals.
    maker.probe_engine = engine.sync_engine
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    async with db() as session:
        t = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=None, language="pt-BR")
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


class _FakeCalendarService:
    """No Google call is ever attempted; `next_events` is what /events sees."""

    next_events: list[dict] = []

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    async def cancel_event(self, event_id: str) -> None:
        self.cancelled.append(event_id)

    async def check_availability(self, start, end):
        return _FakeCalendarService.next_events


class _FakeArqPool:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def enqueue_job(self, name: str, *args) -> None:
        self.calls.append((name, *args))


@pytest.fixture(autouse=True)
def _override(db, tenant, monkeypatch: pytest.MonkeyPatch):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    monkeypatch.setattr(hub_calendar, "CalendarService", _FakeCalendarService)
    _FakeCalendarService.next_events = []
    app.state.arq_pool = _FakeArqPool()
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.state.arq_pool = None


async def _connect_calendar(db, tenant) -> None:
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "fake-refresh-token")
        await session.commit()


def _wire_event(event_id: str) -> dict:
    return {
        "id": event_id,
        "summary": "Consulta",
        "start": "2026-08-19T10:00:00Z",
        "end": "2026-08-19T10:30:00Z",
    }


async def _seed(
    db,
    tenant,
    *,
    google_event_id: str = "evt-1",
    with_professional: bool = True,
    with_patient: bool = True,
    last_inbound: datetime | None = None,
) -> Appointment:
    """One appointment, optionally owned by a professional and linked to a
    patient whose most recent inbound message lands at `last_inbound`."""
    async with db() as session:
        professional = None
        if with_professional:
            professional = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana")
            session.add(professional)
        patient = None
        if with_patient:
            patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id=PATIENT_WA, name="Maria")
            session.add(patient)
            await session.flush()
            if last_inbound is not None:
                conv = Conversation(id=uuid4(), tenant_id=tenant.id, patient_id=patient.id)
                session.add(conv)
                await session.flush()
                session.add(
                    Message(
                        id=uuid4(),
                        conversation_id=conv.id,
                        direction=MessageDirection.INBOUND,
                        sender=MessageSender.PATIENT,
                        body="oi",
                        created_at=last_inbound,
                    )
                )
        await session.flush()
        appt = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id if patient else None,
            professional_id=professional.id if professional else None,
            google_event_id=google_event_id,
            appointment_type="Consulta",
            start_at=datetime.now(UTC) + timedelta(days=1),
            end_at=datetime.now(UTC) + timedelta(days=1, minutes=30),
            phone=PATIENT_WA,
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appt)
        await session.commit()
        await session.refresh(appt)
        return appt


async def _events(client: AsyncClient):
    return await client.get(
        f"{CALENDAR}/events",
        params={"start": "2026-08-18T00:00:00Z", "end": "2026-08-20T00:00:00Z"},
    )


# ---------------------------------------------------------------------------
# Part 1 (§2) — the id map that unblocks the agenda's cancel button
# ---------------------------------------------------------------------------


async def test_events_carry_the_local_appointment_id(client: AsyncClient, db, tenant):
    await _connect_calendar(db, tenant)
    appt = await _seed(db, tenant, google_event_id="evt-1")
    _FakeCalendarService.next_events = [_wire_event("evt-1")]

    resp = await _events(client)

    assert resp.status_code == 200
    assert resp.json()[0]["appointment_id"] == str(appt.id)


async def test_google_only_event_has_no_appointment_id(client: AsyncClient, db, tenant):
    """Typed straight into Google Calendar — the UI must keep cancel disabled
    rather than guess an id, because the deletion is irreversible."""
    await _connect_calendar(db, tenant)
    _FakeCalendarService.next_events = [_wire_event("evt-not-ours")]

    resp = await _events(client)

    assert resp.json()[0]["appointment_id"] is None


async def test_another_tenants_event_id_never_resolves(client: AsyncClient, db, tenant):
    """Isolation invariant: a shared or mis-configured Google account must not
    hand this doctor a working cancel button for another clinic's patient."""
    await _connect_calendar(db, tenant)
    async with db() as session:
        other = Tenant(id=uuid4(), clinic_name="Other", phone_number_id=None)
        session.add(other)
        await session.flush()
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=other.id,
                google_event_id="evt-foreign",
                appointment_type="Consulta",
                start_at=datetime.now(UTC),
                end_at=datetime.now(UTC) + timedelta(minutes=30),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()
    _FakeCalendarService.next_events = [_wire_event("evt-foreign")]

    resp = await _events(client)

    assert resp.json()[0]["appointment_id"] is None


async def test_the_id_map_is_one_query_not_one_per_event(client: AsyncClient, db, tenant):
    """A month of a busy clinic is hundreds of events; the N+1 shape would put
    that many round trips behind a screen the doctor opens constantly."""
    await _connect_calendar(db, tenant)
    for n in range(12):
        await _seed(
            db, tenant, google_event_id=f"evt-{n}", with_professional=False, with_patient=False
        )
    _FakeCalendarService.next_events = [_wire_event(f"evt-{n}") for n in range(12)]

    seen: list[str] = []

    def _before_cursor(conn, cursor, statement, params, context, executemany):
        if "FROM appointments" in statement:
            seen.append(statement)

    event.listen(db.probe_engine, "before_cursor_execute", _before_cursor)
    try:
        resp = await _events(client)
    finally:
        event.remove(db.probe_engine, "before_cursor_execute", _before_cursor)

    assert resp.status_code == 200
    assert len([e for e in resp.json() if e["appointment_id"]]) == 12
    assert len(seen) == 1, f"expected ONE matching query, got {len(seen)}"


# ---------------------------------------------------------------------------
# Part 2 (§3) — the notice itself
# ---------------------------------------------------------------------------


async def test_cancel_notifies_even_with_no_justification(client: AsyncClient, db, tenant):
    """The whole point of the change: a blank box used to mean silence."""
    from secretaria.main import app

    await _connect_calendar(db, tenant)
    appt = await _seed(db, tenant)

    resp = await client.post(f"{CALENDAR}/appointments/{appt.id}/cancel", json={"confirm": True})

    assert resp.status_code == 200
    pool: _FakeArqPool = app.state.arq_pool
    assert len(pool.calls) == 1
    assert pool.calls[0][0] == "send_cancellation_notice"
    assert pool.calls[0][4] is None  # justification


async def test_cancel_passes_the_justification_and_the_doctors_name(
    client: AsyncClient, db, tenant
):
    from secretaria.main import app

    await _connect_calendar(db, tenant)
    appt = await _seed(db, tenant)

    resp = await client.post(
        f"{CALENDAR}/appointments/{appt.id}/cancel",
        json={"confirm": True, "justification": "Imprevisto do medico"},
    )

    assert resp.status_code == 200
    call = app.state.arq_pool.calls[0]
    assert call[3] == "Dra. Ana"  # professional_name
    assert call[4] == "Imprevisto do medico"  # justification
    assert call[6] is False  # not authorised => never the paid path


async def test_custom_message_is_gone(client: AsyncClient, db, tenant):
    """Replaced by `justification`, not kept beside it — two fields with
    confusable semantics is how a cancellation ships the wrong words."""
    from secretaria.schemas.calendar import AppointmentCancel

    assert "custom_message" not in AppointmentCancel.model_fields
    assert "justification" in AppointmentCancel.model_fields


async def test_cancelled_appointment_stays_cancelled(client: AsyncClient, db, tenant):
    await _connect_calendar(db, tenant)
    appt = await _seed(db, tenant)

    await client.post(f"{CALENDAR}/appointments/{appt.id}/cancel", json={"confirm": True})

    async with db() as session:
        stored = await session.get(Appointment, appt.id)
        assert stored.status == AppointmentStatus.CANCELLED


# ---------------------------------------------------------------------------
# The 24h window (§3.1) — the cost is never incurred without authorisation
# ---------------------------------------------------------------------------


async def test_preview_reports_inside_window_and_the_free_link(client: AsyncClient, db, tenant):
    await _connect_calendar(db, tenant)
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=1))

    body = (await client.get(f"{CALENDAR}/appointments/{appt.id}/cancel-preview")).json()

    assert body["inside_window"] is True
    assert body["professional_name"] == "Dra. Ana"
    assert body["whatsapp_link"] == f"https://wa.me/{PATIENT_WA}"


async def test_preview_reports_outside_window_when_the_patient_went_quiet(
    client: AsyncClient, db, tenant
):
    await _connect_calendar(db, tenant)
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=30))

    body = (await client.get(f"{CALENDAR}/appointments/{appt.id}/cancel-preview")).json()

    assert body["inside_window"] is False
    assert body["cost_is_estimate"] is True


async def test_preview_never_mutates_anything(client: AsyncClient, db, tenant):
    """It is read before the doctor has decided anything — it must not cancel,
    and must not send."""
    from secretaria.main import app

    await _connect_calendar(db, tenant)
    appt = await _seed(db, tenant)

    await client.get(f"{CALENDAR}/appointments/{appt.id}/cancel-preview")

    async with db() as session:
        assert (await session.get(Appointment, appt.id)).status == AppointmentStatus.SCHEDULED
    assert app.state.arq_pool.calls == []


# ---------------------------------------------------------------------------
# The worker job — what actually reaches the patient, and what it costs
# ---------------------------------------------------------------------------


class _FakeWhatsApp:
    """Records sends. `for_tenant` hands back the instance the test holds."""

    current: "_FakeWhatsApp | None" = None

    def __init__(self) -> None:
        self.buttons: list[dict] = []
        self.templates: list[dict] = []
        self.explode = False

    @classmethod
    def for_tenant(cls, tenant, token):
        return cls.current

    async def send_buttons(self, to, body, buttons):
        if self.explode:
            raise RuntimeError("whatsapp down")
        self.buttons.append({"to": to, "body": body, "buttons": buttons})

    async def send_template(self, to, template, lang, variables, button_payloads=None):
        if self.explode:
            raise RuntimeError("whatsapp down")
        self.templates.append(
            {
                "to": to,
                "template": template,
                "lang": lang,
                "variables": variables,
                "button_payloads": button_payloads,
            }
        )


@pytest.fixture
def wa(db, monkeypatch: pytest.MonkeyPatch) -> _FakeWhatsApp:
    client = _FakeWhatsApp()
    _FakeWhatsApp.current = client
    monkeypatch.setattr(tasks, "WhatsAppClient", _FakeWhatsApp)
    monkeypatch.setattr(tasks, "async_session_factory", db)

    async def _token(session, tenant_id):
        return "fake-waba-token"

    monkeypatch.setattr(tasks, "get_waba_token", _token)
    return client


@pytest.fixture
def metered(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    recorded: list[dict] = []

    async def _emit(*, tenant_id, feature, amount, event_id):
        recorded.append({"feature": feature, "event_id": event_id, "amount": amount})
        return True

    monkeypatch.setattr(tasks, "emit_usage_event", _emit)
    return recorded


async def _run(appt, *, justification=None, extra=None, allow_paid=False):
    await tasks.send_cancellation_notice(
        {},
        str(appt.tenant_id),
        str(appt.id),
        "Dra. Ana",
        justification,
        extra,
        allow_paid,
    )


async def test_inside_the_window_the_notice_is_free_and_carries_the_buttons(
    db, tenant, wa, metered
):
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))

    await _run(appt, justification="Imprevisto do medico")

    assert len(wa.buttons) == 1
    assert wa.templates == []  # nothing billable happened
    sent = wa.buttons[0]
    assert sent["to"] == PATIENT_WA
    assert "O médico Dra. Ana desmarcou a sua consulta!" in sent["body"]
    assert 'Justificativa do médico: "Imprevisto do medico"' in sent["body"]
    assert [bid for bid, _ in sent["buttons"]] == [
        f"rebooksame|{appt.id}",
        f"rebookother|{appt.id}",
        f"rebookno|{appt.id}",
    ]
    assert metered == []  # free window => no meter tick


async def test_outside_the_window_without_authorisation_sends_nothing_but_says_so(
    db, tenant, wa, metered, capsys
):
    """The patient has NOT been told. That is the doctor's choice (they were
    shown the price), but it must never be silent — this is the one path where
    a cancellation goes unannounced.

    Reads `capsys`, not `caplog`: structlog is configured with a PrintLogger
    here, so records go straight to stdout and never reach the stdlib handler
    caplog installs. A caplog-based assertion would see an empty list and pass
    no matter what the code did.
    """
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=40))

    await _run(appt, allow_paid=False)

    assert wa.buttons == [] and wa.templates == []
    assert metered == []
    assert "cancellation_notice_not_sent" in capsys.readouterr().out


async def test_outside_the_window_with_authorisation_sends_the_paid_template(
    db, tenant, wa, metered
):
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=40))

    await _run(appt, justification="Imprevisto do medico", allow_paid=True)

    assert wa.buttons == []  # free-form is not allowed out here
    assert len(wa.templates) == 1
    sent = wa.templates[0]
    assert sent["lang"] == "pt_BR"
    assert "O médico Dra. Ana desmarcou a sua consulta!" in sent["variables"][0]
    # The quick-reply payloads carry the same ids the router routes on, so a
    # tap both reopens the 24h window and lands in the rebooking branch.
    assert sent["button_payloads"] == [
        f"rebooksame|{appt.id}",
        f"rebookother|{appt.id}",
        f"rebookno|{appt.id}",
    ]
    assert metered == [
        {"feature": "reminders", "amount": 1, "event_id": f"cancelnotice:{appt.id}"}
    ]


async def test_a_patient_who_never_wrote_is_treated_as_outside_the_window(
    db, tenant, wa, metered
):
    """No conversation at all — a free-form send would just be rejected."""
    appt = await _seed(db, tenant, last_inbound=None)

    await _run(appt, allow_paid=False)

    assert wa.buttons == [] and wa.templates == []


async def test_running_the_job_twice_sends_one_message(db, tenant, wa, metered):
    """arq retries jobs. Inside the window a duplicate is inbox noise; outside
    it is a second real charge."""
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))

    await _run(appt)
    await _run(appt)

    assert len(wa.buttons) == 1


async def test_a_paid_send_is_never_charged_twice(db, tenant, wa, metered):
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=40))

    await _run(appt, allow_paid=True)
    await _run(appt, allow_paid=True)

    assert len(wa.templates) == 1
    assert len(metered) == 1


async def test_a_failed_send_raises_so_arq_really_retries(db, tenant, wa, metered):
    """A claim is a lock, not a receipt — but giving the lock back is only half
    of it, and this job used to stop there.

    arq re-runs a job for `arq.Retry` and nothing else (arq.worker: a plain
    exception is logged as a permanent failure, and a job that RETURNS is
    finished). So the old `except: release; log; return` handed the key back to
    a retry that was never going to happen, and the patient was simply never
    told their consultation was cancelled."""
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))

    wa.explode = True
    with pytest.raises(Retry):
        await _run(appt)
    assert wa.buttons == []

    # arq now re-runs the same job: the key is free, so the claim succeeds.
    wa.explode = False
    await _run(appt)
    assert len(wa.buttons) == 1  # the retry got through

    await _run(appt)
    assert len(wa.buttons) == 1  # ...and now it IS a receipt


async def test_a_failed_paid_send_retries_and_is_charged_exactly_once(db, tenant, wa, metered):
    """Outside the 24h window every send is billed, so the retry path is the one
    place a duplicate costs the clinic real money.

    This is also the case that failed CONSTANTLY before the template was
    approved on Meta's side: outside the window, an unapproved
    `appointment_cancelled` means every single notice took this branch."""
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=40))

    wa.explode = True
    with pytest.raises(Retry):
        await _run(appt, allow_paid=True)
    assert wa.templates == []
    assert metered == []  # nothing sent, nothing billed

    wa.explode = False
    await _run(appt, allow_paid=True)
    assert len(wa.templates) == 1
    assert len(metered) == 1

    # A late duplicate of the same job must not buy a second template.
    await _run(appt, allow_paid=True)
    assert len(wa.templates) == 1
    assert len(metered) == 1


async def test_metering_can_never_trigger_a_second_billed_send(db, tenant, wa, monkeypatch):
    """The message already went out. A metering blip must not reach the retry
    path, because that retry would send — and charge — again.

    Guaranteed structurally: `_emit_cancellation_usage` sits OUTSIDE the try
    that catches send failures. This drives the nastier version of the same
    thing, a metering call that raises outright."""
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=40))

    async def _boom(**kwargs):
        raise RuntimeError("meter down")

    monkeypatch.setattr(tasks, "emit_usage_event", _boom)

    await _run(appt, allow_paid=True)  # must not raise Retry

    assert len(wa.templates) == 1


async def test_the_retry_gives_up_and_tells_the_clinic(db, tenant, wa, metered, monkeypatch):
    """Past the budget the patient still has not been told, and nobody is
    reading the worker log. A patient who does not know will travel to a
    consultation that no longer exists — so the failure leaves the machine and
    reaches the clinic, with the free `wa.me` link the hub already offers."""
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.contact_email = CLINIC_EMAIL
        await session.commit()

    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))

    alerts: list[dict] = []

    async def _alert(to_email, clinic_name, whatsapp_link):
        alerts.append({"to": to_email, "clinic": clinic_name, "link": whatsapp_link})

    monkeypatch.setattr(tasks, "send_cancellation_escalation_alert", _alert)

    wa.explode = True
    # Last permitted attempt: no budget left, so it must NOT raise.
    await tasks.send_cancellation_notice(
        {"job_try": tasks.CANCEL_NOTICE_MAX_TRIES},
        str(appt.tenant_id),
        str(appt.id),
        "Dra. Ana",
        None,
        None,
        False,
    )

    assert wa.buttons == []
    assert len(alerts) == 1
    assert alerts[0]["to"] == CLINIC_EMAIL
    assert PATIENT_WA in (alerts[0]["link"] or "")  # the doctor can write in one tap


async def test_the_retry_stops_once_the_notice_is_too_late_to_help(db, tenant, wa, metered):
    """A cancellation notice landing hours later reaches somebody who has
    already left. Past the validity window the job stops trying rather than
    delivering something worse than nothing."""
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))

    wa.explode = True
    await tasks.send_cancellation_notice(
        {
            "job_try": 1,
            "enqueue_time": datetime.now(UTC)
            - timedelta(seconds=tasks.CANCEL_NOTICE_VALIDITY_S),
        },
        str(appt.tenant_id),
        str(appt.id),
        "Dra. Ana",
        None,
        None,
        False,
    )

    assert wa.buttons == []


async def test_giving_up_is_never_silent(db, tenant, wa, metered, capsys):
    """Even with no clinic email on file, the abandonment carries a stable alarm
    field an ops filter can key on."""
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))

    wa.explode = True
    await tasks.send_cancellation_notice(
        {"job_try": tasks.CANCEL_NOTICE_MAX_TRIES},
        str(appt.tenant_id),
        str(appt.id),
        "Dra. Ana",
        None,
        None,
        False,
    )

    logged = capsys.readouterr().out
    assert "cancellation_notice_abandoned" in logged
    assert "cancellation_notice_undelivered" in logged
    assert PATIENT_WA not in logged


async def test_the_deposit_notice_rides_along_rather_than_arriving_separately(
    db, tenant, wa, metered
):
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))

    await _run(appt, extra="O sinal foi retido pela clínica.")

    assert len(wa.buttons) == 1
    assert "retido pela clínica" in wa.buttons[0]["body"]


async def test_an_appointment_from_another_tenant_is_never_notified(db, tenant, wa, metered):
    """Isolation guard on the job itself, not just on the endpoint."""
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))
    async with db() as session:
        other = Tenant(id=uuid4(), clinic_name="Other", phone_number_id=None)
        session.add(other)
        await session.commit()
        other_id = other.id

    await tasks.send_cancellation_notice(
        {}, str(other_id), str(appt.id), "Dra. Ana", None, None, False
    )

    assert wa.buttons == [] and wa.templates == []


async def test_the_job_never_logs_a_phone_or_the_justification(db, tenant, wa, metered, capsys):
    """Count-only observability: ids and reasons, never patient content.

    `capsys` rather than `caplog` for the reason spelled out above — and the
    non-empty guard matters MORE here than the assertions do: a "nothing
    forbidden appears in the log" check passes trivially against an empty
    string, so without it this test would still pass if the logging vanished.
    """
    appt = await _seed(db, tenant, last_inbound=datetime.now(UTC) - timedelta(hours=2))

    # Deliberately the FAILURE path: it logs at ERROR (so it survives the
    # suite's WARNING log level) and it is where context tends to get dumped
    # "just to help debugging" — exactly the leak this guards against.
    wa.explode = True
    with pytest.raises(Retry):
        await _run(appt, justification="Fui chamado para uma cirurgia")

    logged = capsys.readouterr().out
    assert "cancellation_notice_failed" in logged, "nothing was logged — assertions below are void"
    for forbidden in (PATIENT_WA, "Fui chamado para uma cirurgia"):
        assert forbidden not in logged, forbidden
