"""Tests for plugins/professional_notification.py — "a patient booked with you".

Same in-memory-sqlite pattern as tests/test_pix_deposit_plugin.py: a real DB
monkeypatched in place of `async_session_factory`. The brain-api lookup
(`fetch_professional_emails`) and the SMTP send
(`send_transactional_email_result`) are both monkeypatched — this file is not
the place to re-test an HTTP client or an SMTP socket, only the hook's OWN
responsibilities:

  - it is CORE (fires without any addon) and reaches both booking sources;
  - it addresses the RIGHT professional, and never one from another tenant;
  - a professional with no linked email, and a disabled mailer, are logged
    no-ops rather than exceptions;
  - the time is rendered in the TENANT's timezone, not the server's;
  - exactly one email per appointment even when the arq job runs twice — but a
    TRANSIENT failure schedules a resend that really runs, rather than handing
    the ledger key back to a retry nothing was ever going to perform;
  - a switched-off mailer is a no-op, not a failure: it schedules nothing and
    raises no alarm, because no number of attempts turns EMAIL_ENABLED=false
    into a delivered email;
  - a failure here cannot stop another post_booking hook in the same sweep;
  - no phone number ever reaches the email body or the logs.
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
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    Patient,
    ProcessedEvent,
    Professional,
    Tenant,
)
from secretaria.plugins import professional_notification as notif  # noqa: E402
from secretaria.plugins.base import PostBookingContext  # noqa: E402
from secretaria.plugins.registry import enabled_plugins  # noqa: E402
from secretaria.services.email import EmailOutcome  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402

# The patient's phone, in the two shapes the DB really carries it:
# `Patient.wa_id` (the WhatsApp id) and `Appointment.phone`. No test in this
# file may ever find either of them in an email body or a log line.
PATIENT_WA_ID = "5511988887777"
PATIENT_PHONE = "+55 11 98888-7777"
DOCTOR_EMAIL = "ana@clinica.example"

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


class _Mailer:
    """Records every send and replays a fixed `EmailOutcome`.

    An outcome rather than a bool because the hook now treats the two kinds of
    "did not send" differently: SEND_FAILED buys a real resend, DISABLED buys
    nothing. A fake that could only say False could not test that distinction.
    """

    def __init__(self, outcome: EmailOutcome = EmailOutcome.SENT):
        self.outcome = outcome
        self.calls: list[dict] = []

    async def __call__(self, *, to: str, template: str, variables: dict) -> EmailOutcome:
        self.calls.append({"to": to, "template": template, "variables": variables})
        return self.outcome

    @property
    def bodies(self) -> list[str]:
        """Every rendered body — the actual text that would leave the server.

        Rendered through the REAL template so the privacy assertions below test
        what a doctor would receive, not what the hook happened to pass along.
        """
        from secretaria.services.email import _TEMPLATES, _SafeDict

        out = []
        for call in self.calls:
            tpl = _TEMPLATES[call["template"]]
            safe = _SafeDict(call["variables"])
            out.append(tpl.subject.format_map(safe) + "\n" + tpl.body.format_map(safe))
        return out


@pytest.fixture
def mailer(monkeypatch: pytest.MonkeyPatch) -> _Mailer:
    sender = _Mailer()
    monkeypatch.setattr(notif, "send_transactional_email_result", sender)
    return sender


class _FakeRedis:
    """The arq pool, recording what the hook asks to be enqueued."""

    def __init__(self, explode: bool = False):
        self.jobs: list[tuple] = []
        self.explode = explode

    async def enqueue_job(self, name, *args, **kwargs):
        if self.explode:
            raise RuntimeError("redis down")
        self.jobs.append((name, args, kwargs))
        return object()


@pytest.fixture
def redis() -> _FakeRedis:
    return _FakeRedis()


class _LogRecorder:
    """Stands in for the module's structlog logger, recording every call.

    Neither of pytest's log fixtures can be trusted for this module:

    * `caplog` is EMPTY — structlog here renders through its own
      PrintLoggerFactory, not stdlib logging. Every "nothing forbidden was
      logged" assertion over `caplog.records` therefore passes vacuously,
      against any code at all. This file used to contain exactly that.
    * `capsys` depends on how the suite happens to be configured: it runs at
      WARNING with a colour ConsoleRenderer, so INFO lines never reach stdout
      and the level marker arrives wrapped in ANSI escapes. A test that passes
      alone then fails in the full run.

    Recording the calls sidesteps both, and is the pattern
    tests/test_build_identity.py already uses for the worker.
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
        """Everything that was logged, flattened — for leak assertions."""
        return "\n".join(f"{lvl} {event} {fields}" for lvl, event, fields in self.records)


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(notif, "logger", recorder)
    return recorder


async def _claimed(db, appointment_id) -> bool:
    """Is the ledger key for this appointment currently held?"""
    async with db() as session:
        row = await session.scalar(
            select(ProcessedEvent.id).where(
                ProcessedEvent.event_id == f"profnotif:{appointment_id}"
            )
        )
    return row is not None


@pytest.fixture(autouse=True)
def _patch_session_factory(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(notif, "async_session_factory", db)
    yield


def _patch_lookup(monkeypatch: pytest.MonkeyPatch, emails: dict[str, str] | None) -> None:
    async def _fetch(tenant_id):
        return emails

    monkeypatch.setattr(notif, "fetch_professional_emails", _fetch)


def _patch_agenda_url(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    from secretaria.config import get_settings

    monkeypatch.setattr(get_settings(), "DOCTOR_AGENDA_URL", url, raising=False)


async def _make_rows(
    db,
    *,
    timezone: str = "America/Sao_Paulo",
    with_professional: bool = True,
    insurance: str | None = None,
    with_patient: bool = True,
):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinica Boa Saude",
            phone_number_id=str(uuid4())[:12],
            timezone=timezone,
        )
        session.add(tenant)
        professional = None
        professional_id = None
        if with_professional:
            professional = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana")
            professional_id = professional.id
            session.add(professional)
        patient = None
        patient_id = None
        if with_patient:
            patient = Patient(
                id=uuid4(),
                tenant_id=tenant.id,
                wa_id=PATIENT_WA_ID,
                name="Maria Silva",
            )
            patient_id = patient.id
            session.add(patient)
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient_id,
            professional_id=professional_id,
            google_event_id="evt-1",
            appointment_type="Primeira consulta",
            # 17:00 UTC == 14:00 in America/Sao_Paulo (UTC-3).
            start_at=datetime(2026, 8, 3, 17, 0, tzinfo=UTC),
            end_at=datetime(2026, 8, 3, 17, 30, tzinfo=UTC),
            phone=PATIENT_PHONE,
            insurance=insurance,
        )
        session.add(appointment)
        await session.commit()
        for row in (tenant, appointment, professional, patient):
            if row is not None:
                await session.refresh(row)
        return tenant, patient, appointment, professional


def _ctx(tenant, patient, appointment, *, source="flow", redis=None) -> PostBookingContext:
    """A PostBookingContext carrying DETACHED rows — what
    plugins/post_booking.py::run_post_booking_hooks hands a hook in production.

    `redis` defaults to None, matching a hook running without a reachable pool:
    the tests that care about the resend pass a fake in explicitly."""
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
    """Every addon off, plain tier: the hook still runs. Telling a doctor that
    somebody booked with them is not a sellable feature.

    Also the regression guard for `registry._spec_enabled`: before this round
    an empty `entitlement_keys` fell out of `any(())` as permanently disabled.
    """
    summary = _summary(addons=dict(_ALL_ADDONS_OFF))
    assert "professional_notification" in [s.id for s in enabled_plugins(summary)]


def test_plugin_is_disabled_for_an_inactive_subscription():
    """Core still means "while the subscription is live" — a lapsed clinic does
    not keep sending mail on the platform's behalf."""
    summary = _summary(active=False, status="canceled")
    assert "professional_notification" not in [s.id for s in enabled_plugins(summary)]


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["flow", "agent"])
async def test_emails_the_owning_professional_from_both_booking_paths(
    db, monkeypatch, mailer, source
):
    """Both commit points funnel into the same arq job, so both must reach the
    hook — the deterministic router and the LLM tools alike."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    _patch_agenda_url(monkeypatch, "https://app.example/secretaria/agenda")

    await notif._post_booking(_ctx(tenant, patient, appointment, source=source))

    assert len(mailer.calls) == 1
    call = mailer.calls[0]
    assert call["to"] == DOCTOR_EMAIL
    assert call["template"] == "appointment_booked_professional"
    body = mailer.bodies[0]
    assert "Dra. Ana" in body
    assert "Maria Silva" in body
    assert "Primeira consulta" in body
    assert "https://app.example/secretaria/agenda" in body


async def test_time_is_rendered_in_the_tenants_timezone(db, monkeypatch, mailer):
    """17:00 UTC is 14:00 in São Paulo. A doctor reading the server's clock
    would show up three hours late."""
    tenant, patient, appointment, professional = await _make_rows(
        db, timezone="America/Sao_Paulo"
    )
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})

    await notif._post_booking(_ctx(tenant, patient, appointment))

    assert mailer.calls[0]["variables"]["when"] == "03/08/2026 às 14:00"
    assert "14:00" in mailer.bodies[0]
    assert "17:00" not in mailer.bodies[0]


async def test_utc_tenant_renders_utc(db, monkeypatch, mailer):
    """Companion to the test above — proves the conversion is real and not a
    constant offset baked in somewhere."""
    tenant, patient, appointment, professional = await _make_rows(db, timezone="UTC")
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})

    await notif._post_booking(_ctx(tenant, patient, appointment))

    assert mailer.calls[0]["variables"]["when"] == "03/08/2026 às 17:00"


async def test_insurance_appears_only_when_there_is_one(db, monkeypatch, mailer):
    tenant, patient, appointment, professional = await _make_rows(db, insurance="Unimed")
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    await notif._post_booking(_ctx(tenant, patient, appointment))
    assert "Convênio: Unimed" in mailer.bodies[0]

    tenant2, patient2, appointment2, professional2 = await _make_rows(db, insurance=None)
    _patch_lookup(monkeypatch, {str(professional2.id): DOCTOR_EMAIL})
    await notif._post_booking(_ctx(tenant2, patient2, appointment2))
    assert "Convênio" not in mailer.bodies[1]


async def test_agenda_link_is_omitted_when_unconfigured(db, monkeypatch, mailer):
    """A mail with no link is fine; a mail with a broken link is not. The path
    differs between the two frontends, so an unset URL prints nothing."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    _patch_agenda_url(monkeypatch, "")

    await notif._post_booking(_ctx(tenant, patient, appointment))

    assert "Ver na agenda" not in mailer.bodies[0]
    assert "http" not in mailer.bodies[0]


# --------------------------------------------------------------------------
# Privacy: what must NEVER leave the building
# --------------------------------------------------------------------------


@pytest.mark.parametrize("insurance", [None, "Unimed"])
async def test_no_phone_number_in_the_email_body(db, monkeypatch, mailer, insurance):
    """SMTP is cleartext-in-transit into a mailbox this product does not
    control. The doctor needs to recognise the appointment, not to hold the
    patient's contact details."""
    tenant, patient, appointment, professional = await _make_rows(db, insurance=insurance)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})

    await notif._post_booking(_ctx(tenant, patient, appointment))

    body = mailer.bodies[0]
    for forbidden in (PATIENT_WA_ID, PATIENT_PHONE, "98888", "988887777"):
        assert forbidden not in body, forbidden
    # ...and nothing priced or clinical either.
    assert "R$" not in body


@pytest.mark.parametrize(
    "outcome", [EmailOutcome.SENT, EmailOutcome.SEND_FAILED, EmailOutcome.UNKNOWN_TEMPLATE]
)
async def test_no_phone_or_email_in_the_logs(db, monkeypatch, mailer, log, outcome):
    """Count-only observability: ids and reasons, never personal data.

    See `_LogRecorder` for why neither `caplog` nor `capsys` can carry this
    assertion. The non-empty guard is what makes the rest of it mean something.

    Parametrised across the failure paths too, because those are where context
    gets dumped "just to help debugging" — and they are the paths this round
    added.
    """
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    mailer.outcome = outcome

    await notif._post_booking(_ctx(tenant, patient, appointment, redis=_FakeRedis()))

    assert log.records, "nothing was logged — the assertions below would be void"
    for forbidden in (PATIENT_WA_ID, PATIENT_PHONE, DOCTOR_EMAIL, "Maria Silva"):
        assert forbidden not in log.text, forbidden


# --------------------------------------------------------------------------
# The no-op paths — honest, logged, never an exception
# --------------------------------------------------------------------------


async def test_professional_without_linked_email_is_a_noop(db, monkeypatch, mailer):
    """A professional created without an invite has no brain-api user, so no
    address. Accepted consequence of not duplicating the column — and the
    professionals screen now says so out loud."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {})  # brain-api answered: nobody linked

    await notif._post_booking(_ctx(tenant, patient, appointment))

    assert mailer.calls == []


async def test_lookup_failure_sends_nothing_but_schedules_another_look(
    db, monkeypatch, mailer, redis
):
    """`None` means "we could not find out", which is not the same as "nobody".

    brain-api being briefly unreachable is the textbook transient failure: no
    mail goes out now, and something has to come back and ask again. Before
    this round it was filed next to "this doctor has no address" and the email
    was simply gone."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, None)

    await notif._post_booking(_ctx(tenant, patient, appointment, redis=redis))

    assert mailer.calls == []
    assert [job[0] for job in redis.jobs] == ["retry_professional_notification"]


async def test_appointment_without_professional_is_a_noop(db, monkeypatch, mailer):
    tenant, patient, appointment, _ = await _make_rows(db, with_professional=False)
    _patch_lookup(monkeypatch, {"whoever": DOCTOR_EMAIL})

    await notif._post_booking(_ctx(tenant, patient, appointment))

    assert mailer.calls == []


async def test_email_disabled_is_a_noop_that_leaves_no_claim(db, monkeypatch, mailer, redis):
    """EMAIL_ENABLED=false. The claim must be given back, or turning the mailer
    on later would find every past booking already "sent" — and nothing may be
    scheduled, because retrying a kill-switch is retrying a decision."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    mailer.outcome = EmailOutcome.DISABLED

    await notif._post_booking(_ctx(tenant, patient, appointment, redis=redis))

    assert len(mailer.calls) == 1  # it tried
    assert not await _claimed(db, appointment.id)  # ...and released
    assert redis.jobs == []  # ...and asked for nothing


async def test_a_switched_off_mailer_raises_no_alarm(db, monkeypatch, mailer, redis, log):
    """A clinic that never enabled mail is not an incident. If DISABLED escalated,
    every booking of every such clinic would page somebody forever."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    mailer.outcome = EmailOutcome.DISABLED

    await notif._post_booking(_ctx(tenant, patient, appointment, redis=redis))

    # Load-bearing guard: without it, "no error was logged" would also pass
    # against a module that logged nothing at all.
    assert (
        "professional_notification_skipped",
        {"appointment_id": str(appointment.id), "reason": "email_disabled"},
    ) in log.at("info")
    assert log.at("error") == []


async def test_a_broken_template_escalates_instead_of_retrying(db, monkeypatch, mailer, redis):
    """UNKNOWN_TEMPLATE/RENDER_FAILED are OUR bug: the next attempt fails
    identically. Loud, once, rather than five times and then silence."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    mailer.outcome = EmailOutcome.UNKNOWN_TEMPLATE

    await notif._post_booking(_ctx(tenant, patient, appointment, redis=redis))

    assert redis.jobs == []
    assert not await _claimed(db, appointment.id)


async def test_a_booking_with_no_patient_still_notifies(db, monkeypatch, mailer):
    """A hub-created block has no patient row. The appointment is real, so the
    doctor still hears about it — with a placeholder, not an invented name."""
    tenant, _patient, appointment, professional = await _make_rows(db, with_patient=False)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})

    await notif._post_booking(_ctx(tenant, None, appointment))

    assert len(mailer.calls) == 1
    assert mailer.calls[0]["variables"]["patient_name"] == "Paciente"


# --------------------------------------------------------------------------
# Idempotency: one email per appointment, but a failure stays retryable
# --------------------------------------------------------------------------


async def test_running_the_hook_twice_sends_one_email(db, monkeypatch, mailer):
    """arq retries jobs. A second "nova consulta marcada" is direct noise in a
    doctor's inbox."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})

    await notif._post_booking(_ctx(tenant, patient, appointment))
    await notif._post_booking(_ctx(tenant, patient, appointment))

    assert len(mailer.calls) == 1


async def test_a_failed_send_schedules_a_resend_and_then_delivers(
    db, monkeypatch, mailer, redis
):
    """The whole point of FIX 32, end to end.

    The hook cannot retry itself — `registry.run_post_booking` swallows what it
    raises — so a transient SMTP failure has to put a REAL job on the queue.
    This drives that job by hand and proves the email actually arrives, which
    the old shape (release the key, log, return) never did."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})

    mailer.outcome = EmailOutcome.SEND_FAILED
    await notif._post_booking(_ctx(tenant, patient, appointment, redis=redis))
    assert len(mailer.calls) == 1
    name, args, kwargs = redis.jobs[0]
    assert name == "retry_professional_notification"
    assert args == (str(tenant.id), str(appointment.id))
    assert kwargs["_defer_by"] == notif.RETRY_DEFER_S
    # Released only now that the resend exists — the resend re-claims it.
    assert not await _claimed(db, appointment.id)

    mailer.outcome = EmailOutcome.SENT
    await notif.retry_professional_notification({}, str(tenant.id), str(appointment.id))
    assert len(mailer.calls) == 2  # the resend got through

    # ...and now the claim IS a receipt: another sweep sends no third copy.
    await notif._post_booking(_ctx(tenant, patient, appointment, redis=redis))
    await notif.retry_professional_notification({}, str(tenant.id), str(appointment.id))
    assert len(mailer.calls) == 2


async def test_a_failure_with_no_pool_says_so_instead_of_going_quiet(
    db, monkeypatch, mailer, log
):
    """No Redis means no resend can be scheduled. That is the exact outcome this
    module used to produce for EVERY transient failure, silently; now it is an
    ERROR carrying a stable alarm field."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    mailer.outcome = EmailOutcome.SEND_FAILED

    await notif._post_booking(_ctx(tenant, patient, appointment, redis=None))

    errors = log.at("error")
    assert errors, "a lost email must not be a silent one"
    assert errors[0][1]["alarm"] == "professional_notification_undelivered"


async def test_a_failed_enqueue_is_reported_not_assumed(db, monkeypatch, mailer, log):
    """A pool that raises is the same loss as no pool at all."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    mailer.outcome = EmailOutcome.SEND_FAILED
    ctx = _ctx(tenant, patient, appointment, redis=_FakeRedis(explode=True))

    await notif._post_booking(ctx)

    assert "professional_notification_retry_enqueue_failed" in log.events
    assert log.at("error")[0][1]["alarm"] == "professional_notification_undelivered"


async def test_the_resend_job_raises_so_arq_really_runs_it_again(db, monkeypatch, mailer):
    """`arq.Retry` is the ONLY thing arq re-runs a job for (arq.worker: a plain
    exception is logged and the job is finished). A resend job that returned
    normally on failure would be the original defect, moved."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    mailer.outcome = EmailOutcome.SEND_FAILED

    with pytest.raises(Retry):
        await notif.retry_professional_notification({}, str(tenant.id), str(appointment.id))

    # And the key is free, so the next attempt can claim it.
    assert not await _claimed(db, appointment.id)


@pytest.mark.parametrize(
    "ctx",
    [
        {"job_try": notif.RETRY_MAX_TRIES},
        {
            "job_try": 1,
            "enqueue_time": datetime.now(UTC) - timedelta(seconds=notif.RETRY_VALIDITY_S),
        },
    ],
    ids=["attempts_exhausted", "past_validity_window"],
)
async def test_the_resend_gives_up_visibly_rather_than_forever(
    db, monkeypatch, mailer, log, ctx
):
    """Both bounds end the same way: stop, and say so with the stable alarm.

    Retrying forever would be its own bug — and a booking mail delivered
    tomorrow morning is noise, not news. Note it does NOT raise: past the
    budget there is nothing left for arq to run again."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    mailer.outcome = EmailOutcome.SEND_FAILED

    await notif.retry_professional_notification(ctx, str(tenant.id), str(appointment.id))

    assert "professional_notification_abandoned" in log.events
    assert log.at("error")[0][1]["alarm"] == "professional_notification_undelivered"


async def test_the_resend_never_sends_a_second_copy(db, monkeypatch, mailer):
    """A resend racing a mail that already went out must find the claim held."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})

    await notif._post_booking(_ctx(tenant, patient, appointment))
    assert len(mailer.calls) == 1

    await notif.retry_professional_notification({}, str(tenant.id), str(appointment.id))
    assert len(mailer.calls) == 1


async def test_the_resend_never_crosses_tenants(db, monkeypatch, mailer):
    """Same isolation guard the hook has, on the job that can be enqueued with
    two ids that were never checked against each other."""
    tenant, patient, appointment, professional = await _make_rows(db)
    other_tenant, _p, _a, _prof = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})

    await notif.retry_professional_notification({}, str(other_tenant.id), str(appointment.id))

    assert mailer.calls == []


async def test_two_appointments_each_get_their_own_email(db, monkeypatch, mailer):
    """The ledger key is per appointment, not per professional or per tenant."""
    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    async with db() as session:
        second = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            professional_id=professional.id,
            google_event_id="evt-2",
            appointment_type="Retorno",
            start_at=datetime(2026, 8, 4, 17, 0, tzinfo=UTC),
            end_at=datetime(2026, 8, 4, 17, 30, tzinfo=UTC),
        )
        session.add(second)
        await session.commit()
        await session.refresh(second)

    await notif._post_booking(_ctx(tenant, patient, appointment))
    await notif._post_booking(_ctx(tenant, patient, second))

    assert len(mailer.calls) == 2


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------


async def test_a_professional_from_another_tenant_is_never_notified(db, monkeypatch, mailer):
    """The re-fetch is tenant-scoped, so an appointment pointing at a foreign
    professional id resolves to nobody rather than to that doctor."""
    tenant_a, patient_a, appointment_a, _ = await _make_rows(db, with_professional=False)
    _tenant_b, _p_b, _appt_b, professional_b = await _make_rows(db)

    # Force clinic A's appointment to name clinic B's doctor.
    async with db() as session:
        appt = await session.get(Appointment, appointment_a.id)
        appt.professional_id = professional_b.id
        await session.commit()
        await session.refresh(appt)
        appointment_a = appt
    _patch_lookup(monkeypatch, {str(professional_b.id): DOCTOR_EMAIL})

    await notif._post_booking(_ctx(tenant_a, patient_a, appointment_a))

    assert mailer.calls == []


# --------------------------------------------------------------------------
# Containment: one hook's failure is not another hook's problem
# --------------------------------------------------------------------------


async def test_a_raising_hook_does_not_stop_the_other_post_booking_hooks(db, monkeypatch):
    """`registry.run_post_booking` wraps each hook itself. SMTP being down must
    not cost the tenant its Pix deposit send in the same sweep.

    Uses the REAL specs, not stand-ins — the failure is injected into this
    hook's own dependency (the brain-api lookup) so the exception really
    escapes `_post_booking`. The registry is re-ordered so the raiser runs
    FIRST; with the natural import order (pix_deposit registers earlier) the
    assertion would hold whether or not the isolation existed.
    """
    from secretaria.plugins import pix_deposit
    from secretaria.plugins.registry import run_post_booking
    from secretaria.services.payments import deposit_lifecycle

    tenant, patient, appointment, professional = await _make_rows(db)

    async def _boom(tenant_id):
        raise RuntimeError("brain-api down")

    monkeypatch.setattr(notif, "fetch_professional_emails", _boom)

    ran: list[str] = []

    async def _recorder(*args, **kwargs):
        ran.append("pix_deposit")
        return None

    monkeypatch.setattr(deposit_lifecycle, "maybe_create_deposit", _recorder)
    monkeypatch.setattr(pix_deposit, "async_session_factory", db)
    monkeypatch.setattr(
        "secretaria.plugins.registry.REGISTRY",
        {
            "professional_notification": notif.PROFESSIONAL_NOTIFICATION_SPEC,
            "pix_deposit": pix_deposit.PIX_DEPOSIT_SPEC,
        },
    )

    summary = _summary(addons={**_ALL_ADDONS_OFF, "pix_deposit": True})
    await run_post_booking(summary, _ctx(tenant, patient, appointment))

    assert ran == ["pix_deposit"]


async def test_a_transient_failure_does_not_stop_the_other_post_booking_hooks(db, monkeypatch):
    """The same containment, on the path this round added.

    A transient SMTP failure now does real work before returning — it enqueues
    a resend and releases the ledger key. None of that may leak into the sweep:
    `pix_deposit` still runs, and the booking is untouched. The registry's
    try/except is what guarantees it, which is also exactly why the resend had
    to become a job of its own rather than a `raise`.
    """
    from secretaria.plugins import pix_deposit
    from secretaria.plugins.registry import run_post_booking
    from secretaria.services.payments import deposit_lifecycle

    tenant, patient, appointment, professional = await _make_rows(db)
    _patch_lookup(monkeypatch, {str(professional.id): DOCTOR_EMAIL})
    sender = _Mailer(EmailOutcome.SEND_FAILED)
    monkeypatch.setattr(notif, "send_transactional_email_result", sender)

    ran: list[str] = []

    async def _recorder(*args, **kwargs):
        ran.append("pix_deposit")
        return None

    monkeypatch.setattr(deposit_lifecycle, "maybe_create_deposit", _recorder)
    monkeypatch.setattr(pix_deposit, "async_session_factory", db)
    monkeypatch.setattr(
        "secretaria.plugins.registry.REGISTRY",
        {
            "professional_notification": notif.PROFESSIONAL_NOTIFICATION_SPEC,
            "pix_deposit": pix_deposit.PIX_DEPOSIT_SPEC,
        },
    )

    redis = _FakeRedis()
    summary = _summary(addons={**_ALL_ADDONS_OFF, "pix_deposit": True})
    await run_post_booking(summary, _ctx(tenant, patient, appointment, redis=redis))

    assert ran == ["pix_deposit"]
    assert [job[0] for job in redis.jobs] == ["retry_professional_notification"]
