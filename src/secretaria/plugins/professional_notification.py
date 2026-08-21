"""CORE post_booking hook: tell the professional a patient just booked with them.

Until this existed, a doctor learned about a new appointment only by opening
the agenda (or from Google Calendar, when the clinic had connected one).
Nothing pushed. This closes that.

Shape mirrors `plugins/ehr.py` — a `_post_booking` coroutine registered through
one `PluginSpec` — with three deliberate differences:

**CORE, not an add-on.** `entitlement_keys=()`. Telling a professional that
somebody booked with them is product hygiene, not a sellable feature. See
`registry._spec_enabled`, which this round taught to read an empty tuple as
"core, enabled while the subscription is active"; it previously fell out of
`any(())` as silently disabled, and this module is the first to register a real
hook that way.

**The address is not ours.** `Professional` has no email column and will not
get one: brain-api owns identity and already stores the address on its own
`users` row, linked by `professional_id`. We ask, per booking
(`services/brain_professionals.fetch_professional_emails`). A professional
created without an invite has no linked user and therefore no address — this
hook is then a logged no-op for them, which is honest, and which the
professionals screen of both frontends now says out loud.

**Exactly one email per appointment.** Claimed through `ProcessedEvent` — the
same durable ledger the webhook pipeline and `plugins/reminders.py` already
use, namespaced `profnotif:<appointment_id>`. Reminders can afford "claimed
means done" (a missed reminder beats a duplicate); here an SMTP outage must
not permanently swallow the mail, so a claim whose send did not happen is
given back.

**A retry that actually exists.** Giving the key back is not, by itself, a
retry — and this module used to treat it as one. Nothing re-runs a
`post_booking` hook: `registry.run_post_booking` wraps every hook in its own
try/except by design (see below), so a hook cannot even ask for one by
raising, and the outer `run_post_booking_hooks` job returns normally either
way. A released key with nothing scheduled behind it is just a lost email
with a log line. So a transient failure ENQUEUES a job of its own —
`retry_professional_notification`, below, registered in
`workers/arq_worker.py` — which raises `arq.Retry` and therefore really does
run again. The key is released only AFTER that job is on the queue, never
before.

Only TRANSIENT failures are worth that. `EMAIL_ENABLED=false` is a
configuration choice, not an outage: no number of attempts turns it into a
delivered email, so it stays a logged no-op. `services/email.py::EmailOutcome`
is what draws that line.

Failure is contained by construction: `registry.run_post_booking` wraps each
hook in its own try/except, so SMTP being down cannot disturb the already-
committed booking nor stop `pix_deposit` running in the same sweep. That
containment is precisely why the retry has to be a separate job.

Never logs an email address, a phone number, or any patient content — only
ids, counts and a reason string.
"""

from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from arq import Retry
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, ProcessedEvent, Professional, Tenant
from secretaria.plugins.base import PluginSpec, PostBookingContext
from secretaria.plugins.registry import register
from secretaria.services.brain_professionals import fetch_professional_emails
from secretaria.services.email import EmailOutcome, send_transactional_email_result

logger = get_logger(__name__)

TEMPLATE_ID = "appointment_booked_professional"

# Fallbacks for a booking that reached here missing a display field. Never
# invent something that could read as fact — "horário a confirmar" is visibly a
# placeholder, a made-up time would not be.
_UNKNOWN_WHEN = "horário a confirmar"
_UNKNOWN_SERVICE = "Consulta"
_UNKNOWN_PATIENT = "Paciente"


def _ledger_key(appointment_id: UUID) -> str:
    return f"profnotif:{appointment_id}"


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive timestamp (e.g. from SQLite) as UTC; pass tz-aware through."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _local_when(start_at: datetime | None, timezone: str | None) -> str:
    """`start_at` rendered in the TENANT's timezone, never the server's.

    A clinic in America/Sao_Paulo reading "17:00" for a 14:00 consultation
    would be worse than useless. An unparseable tz falls back to UTC rather
    than raising — the same defensive shape `plugins/reminders.py` uses.
    """
    if start_at is None:
        return _UNKNOWN_WHEN
    try:
        tz = ZoneInfo(timezone or "UTC")
    except Exception:
        tz = UTC
    return _as_utc(start_at).astimezone(tz).strftime("%d/%m/%Y às %H:%M")


async def _claim(appointment_id: UUID) -> bool:
    """Insert the ledger row. True iff THIS call claimed the send.

    Claim-insert + IntegrityError race pattern, identical to
    `plugins/reminders.py::_claim_reminder` and the message pipeline's
    `_event_already_processed`.
    """
    key = _ledger_key(appointment_id)
    async with async_session_factory() as session:
        try:
            async with session.begin():
                existing = await session.scalar(
                    select(ProcessedEvent.id).where(ProcessedEvent.event_id == key)
                )
                if existing is not None:
                    return False
                session.add(ProcessedEvent(event_id=key))
        except IntegrityError:
            return False
    return True


async def _release(appointment_id: UUID) -> None:
    """Give the ledger row back. States one fact: the send did NOT happen.

    The half of idempotency reminders does not implement: there, "claimed"
    means "done, never repeat". Here a claim is only a lock, and a send that
    never left the process never consumed it — leaving the row would turn a
    transient outage into a permanently lost notification.

    What it is NOT is a retry. It schedules nothing. Whoever calls it is
    responsible for saying what happens next, and on the transient path that
    means enqueueing `retry_professional_notification` FIRST and releasing
    only once it is on the queue.

    Best-effort: a failure to release is logged and swallowed, since the
    caller is already on its failure path.
    """
    key = _ledger_key(appointment_id)
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == key))
    except Exception as exc:
        logger.warning(
            "professional_notification_release_failed",
            appointment_id=str(appointment_id),
            error=str(exc),
        )


async def _load_professional(tenant_id: UUID, professional_id: UUID) -> Professional | None:
    """Re-fetch the professional, scoped to THIS tenant.

    The tenant filter is the isolation invariant, not a formality: it is what
    makes it impossible for a mis-set `professional_id` to address a doctor
    belonging to another clinic.
    """
    async with async_session_factory() as session:
        return await session.scalar(
            select(Professional).where(
                Professional.id == professional_id, Professional.tenant_id == tenant_id
            )
        )


def _skip(appointment_id, reason: str) -> None:
    logger.info(
        "professional_notification_skipped",
        appointment_id=str(appointment_id),
        reason=reason,
    )


# How hard the resend tries before giving up, and how long the attempt stays
# worth making. Justified in `_retry_decision`.
RETRY_MAX_TRIES = 5
RETRY_DEFER_S = 300
RETRY_VALIDITY_S = 60 * 60

# What `_deliver` reports back. Not an enum: three call sites, all in this
# module, and the strings are what the log field carries anyway.
_SENT = "sent"
_SKIPPED = "skipped"
_TRANSIENT = "transient"

RETRY_JOB_NAME = "retry_professional_notification"


async def _deliver(tenant: Tenant, patient: Patient | None, appointment: Appointment) -> str:
    """Resolve the address and send the mail. Returns _SENT / _SKIPPED / _TRANSIENT.

    The whole body of the notification, shared by the `post_booking` hook and
    by the retry job so a resend is the same email — including the
    `ProcessedEvent` claim, which is what makes a retry safe to run against an
    appointment somebody already got mail for.

    _TRANSIENT is returned with the claim still HELD on purpose. Releasing it
    is the caller's job, and only AFTER it has actually scheduled the retry
    that will re-claim it: release-then-fail-to-schedule is the exact shape
    this module is being fixed for.
    """
    professional_id = appointment.professional_id
    if professional_id is None:
        # No owner to address. Since the booking-ownership round this only
        # happens for a tenant with zero or 2+ active professionals and no
        # explicit selection (services/booking_scope.resolve_booking_owner_id);
        # the common single-professional clinic now always resolves an owner.
        _skip(appointment.id, "no_professional")
        return _SKIPPED

    professional = await _load_professional(tenant.id, professional_id)
    if professional is None:
        # Either deleted between booking and this job, or — the reason the
        # tenant filter exists — not this clinic's to notify.
        _skip(appointment.id, "professional_not_found")
        return _SKIPPED

    emails = await fetch_professional_emails(tenant.id)
    if emails is None:
        # brain-api could not answer. "We do not know" is not "nobody", and it
        # is usually a blip — worth another look. No claim has been taken yet,
        # so there is nothing to give back here.
        logger.warning(
            "professional_notification_lookup_failed",
            appointment_id=str(appointment.id),
            tenant_id=str(tenant.id),
        )
        return _TRANSIENT
    to_email = emails.get(str(professional_id))
    if not to_email:
        _skip(appointment.id, "professional_without_email")
        return _SKIPPED

    if not await _claim(appointment.id):
        _skip(appointment.id, "already_notified")
        return _SKIPPED

    settings = get_settings()
    agenda_url = (settings.DOCTOR_AGENDA_URL or "").strip()
    insurance = (appointment.insurance or "").strip()
    variables = {
        "professional_name": professional.name,
        # `patient` is None for a booking with no patient row (a hub-created
        # block). The appointment is still real, so the mail still goes.
        "patient_name": (patient.name if patient else None) or _UNKNOWN_PATIENT,
        "service": appointment.appointment_type or _UNKNOWN_SERVICE,
        "when": _local_when(appointment.start_at, tenant.timezone),
        # Pre-rendered lines: empty when there is nothing to say, so the
        # template itself needs no conditionals (see services/email.py).
        "insurance_line": f"Convênio: {insurance}\n" if insurance else "",
        "agenda_line": f"Ver na agenda:\n{agenda_url}\n\n" if agenda_url else "",
        # The event on the clinic's own Google Calendar. Here — unlike the
        # patient-facing link (services/calendar.py::build_patient_calendar_link)
        # — the PRIVATE `htmlLink` is exactly right: the professional owns that
        # agenda, so it opens for them. NULL for a booking made while no
        # calendar was connected, and for rows that predate the column; same
        # "empty string when there is nothing to say" shape as the two lines
        # above, so the flat template needs no conditional.
        "calendar_line": (
            f"Ver no Google Agenda:\n{appointment.google_event_link}\n\n"
            if appointment.google_event_link
            else ""
        ),
    }

    outcome = await send_transactional_email_result(
        to=to_email, template=TEMPLATE_ID, variables=variables
    )
    if outcome is EmailOutcome.SENT:
        logger.info(
            "professional_notification_sent",
            appointment_id=str(appointment.id),
            tenant_id=str(tenant.id),
            professional_id=str(professional_id),
        )
        return _SENT

    if outcome.is_transient:
        # Claim deliberately NOT released here — see this function's docstring.
        return _TRANSIENT

    # Everything else is permanent: the mailer is switched off, or the
    # template/render is broken. Retrying changes nothing, so the key is
    # simply given back and nothing is scheduled.
    await _release(appointment.id)
    if outcome is EmailOutcome.DISABLED:
        _skip(appointment.id, "email_disabled")
    else:
        # A template id or a placeholder that does not exist is OUR bug, and it
        # silently costs every doctor their booking mail until somebody
        # notices. Stable alarm field so an ops filter can.
        logger.error(
            "professional_notification_undeliverable",
            alarm="professional_notification_undelivered",
            reason=outcome.value,
            appointment_id=str(appointment.id),
            tenant_id=str(tenant.id),
        )
    return _SKIPPED


async def _schedule_retry(redis, tenant_id: UUID, appointment_id: UUID) -> bool:
    """Put `retry_professional_notification` on the queue. True iff one is now pending.

    Deterministic `_job_id` so a booking can only ever have ONE retry chain:
    arq refuses a second enqueue under a job id already in flight, which is the
    cheap guard against two chains racing to claim the same ledger key. A
    refused duplicate still means "a retry exists", hence True.

    `redis is None` (unit tests, a dev script without Redis) is not an error,
    but it IS the difference between a scheduled resend and a lost one, so it
    returns False and the caller says so out loud.
    """
    if redis is None:
        return False
    try:
        await redis.enqueue_job(
            RETRY_JOB_NAME,
            str(tenant_id),
            str(appointment_id),
            _job_id=f"profnotifretry:{appointment_id}",
            _defer_by=RETRY_DEFER_S,
        )
        return True
    except Exception as exc:
        logger.warning(
            "professional_notification_retry_enqueue_failed",
            appointment_id=str(appointment_id),
            error=str(exc),
        )
        return False


def _retry_decision(ctx: dict) -> int | None:
    """Seconds to wait before the next attempt, or None to stop trying.

    Two bounds:

    * ATTEMPTS — `RETRY_MAX_TRIES` in total, matching arq's own default
      `max_tries` (5, `arq.worker.Worker`; `workers/arq_worker.py` sets no
      override) so this budget is the one that runs out, with an explicit
      alarm, rather than arq's, which logs "max retries exceeded" and drops
      the job.
    * VALIDITY — `RETRY_VALIDITY_S` from the original enqueue (arq preserves
      `enqueue_time` across retries). Generous compared with a cancellation
      notice, and deliberately: a doctor reading "a patient booked with you"
      forty minutes late has lost nothing, whereas a patient hearing about a
      cancellation late may already have travelled. An hour still bounds it,
      because a booking mail arriving the next morning is confusing rather
      than useful — the agenda is the source of truth by then.

    That is 5 attempts, 5 minutes apart, all inside one hour.

    An empty `ctx` — a direct call from a test — reads as "first attempt, no
    deadline known" and gets a retry.
    """
    job_try = ctx.get("job_try") or 1
    if job_try >= RETRY_MAX_TRIES:
        return None

    enqueued_at = ctx.get("enqueue_time")
    if isinstance(enqueued_at, datetime):
        age = (datetime.now(UTC) - _as_utc(enqueued_at)).total_seconds()
        if age + RETRY_DEFER_S >= RETRY_VALIDITY_S:
            return None

    return RETRY_DEFER_S


async def retry_professional_notification(ctx: dict, tenant_id: str, appointment_id: str) -> None:
    """arq job: try the "nova consulta marcada" email again.

    Registered in `workers/arq_worker.py` and enqueued only by `_post_booking`
    below, on a transient failure. Exists because the hook itself cannot ask
    for a retry — `registry.run_post_booking` contains hook exceptions by
    contract, so raising in there is swallowed and the surrounding job still
    finishes successfully.

    Reloads the rows by id rather than trusting anything serialised into the
    job, and goes through the same `_deliver` (hence the same `ProcessedEvent`
    claim), so a mail that did get out in the meantime is skipped rather than
    duplicated.

    Raises `arq.Retry` while there is budget left — the one exception arq
    re-runs a job for. When the budget is gone it logs a stable alarm and
    stops: a doctor's booking mail is worth several attempts, not an infinite
    queue.
    """
    async with async_session_factory() as session:
        appointment = await session.get(Appointment, UUID(appointment_id))
        if appointment is None or str(appointment.tenant_id) != tenant_id:
            # Deleted meanwhile, or — the isolation guard — not this clinic's.
            _skip(appointment_id, "appointment_not_found")
            return
        tenant = await session.get(Tenant, UUID(tenant_id))
        if tenant is None:
            _skip(appointment_id, "tenant_not_found")
            return
        patient = (
            await session.get(Patient, appointment.patient_id)
            if appointment.patient_id is not None
            else None
        )

    result = await _deliver(tenant, patient, appointment)
    if result != _TRANSIENT:
        return

    defer = _retry_decision(ctx)
    # Released either way: the send did not happen, so the key was not
    # consumed. When `defer` is set, THIS job is the retry and re-claims it on
    # the next run — nothing else has to be scheduled.
    await _release(appointment.id)
    if defer is None:
        logger.error(
            "professional_notification_abandoned",
            alarm="professional_notification_undelivered",
            appointment_id=appointment_id,
            tenant_id=tenant_id,
        )
        return
    logger.warning(
        "professional_notification_retrying",
        appointment_id=appointment_id,
        tenant_id=tenant_id,
        retry_in_s=defer,
    )
    raise Retry(defer=defer)


async def _post_booking(ctx: PostBookingContext) -> None:
    """Email the appointment's owning professional. Best-effort, exactly once.

    A transient failure does not end here: it schedules
    `retry_professional_notification` and only then gives the ledger key back.
    """
    result = await _deliver(ctx.tenant, ctx.patient, ctx.appointment)
    if result != _TRANSIENT:
        return

    scheduled = await _schedule_retry(ctx.redis, ctx.tenant.id, ctx.appointment.id)
    # Order matters: the retry has to be on the queue BEFORE the key is free,
    # or a crash in between leaves a released key with nothing behind it.
    await _release(ctx.appointment.id)
    if scheduled:
        logger.warning(
            "professional_notification_retry_scheduled",
            appointment_id=str(ctx.appointment.id),
            tenant_id=str(ctx.tenant.id),
        )
        return
    # No pool, or the enqueue failed. The mail is lost and nothing will pick it
    # up — which is exactly the outcome this module used to produce silently,
    # so it is said out loud with the same alarm the retry job uses.
    logger.error(
        "professional_notification_not_sent",
        alarm="professional_notification_undelivered",
        reason="no_retry_scheduled",
        appointment_id=str(ctx.appointment.id),
        tenant_id=str(ctx.tenant.id),
    )


PROFESSIONAL_NOTIFICATION_SPEC = PluginSpec(
    id="professional_notification",
    entitlement_keys=(),
    post_booking=_post_booking,
)
register(PROFESSIONAL_NOTIFICATION_SPEC)
