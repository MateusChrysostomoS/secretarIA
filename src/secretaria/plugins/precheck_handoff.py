"""CORE post_booking hook: offer the patient their pre-consult right after booking.

Until this existed, the bridge to PreCheck had exactly one trigger: the agent
tool `ai/tools.py::iniciar_pre_consulta`, called only when the LLM decided
mid-conversation that the moment had come. A patient who booked through the
DETERMINISTIC flow router never met an LLM at all, so for them the pre-consult
simply never happened — the confirmation bubble
(`services/flow_router.py`, "Pronto! Seu agendamento está confirmado.") ends
with a calendar link and nothing else. This closes that: every booking, by
either path, now offers the pre-consult.

Shape mirrors `plugins/professional_notification.py` — a `_post_booking`
coroutine registered through one `PluginSpec`, one `ProcessedEvent` claim per
appointment — with four deliberate differences:

**No local entitlement gate.** `entitlement_keys=()`, and not because PreCheck
is free: the real gate already exists one hop away, inside brain-api
(`api/internal.py`: `ent.status in ACTIVE_STATUSES and ent.products.precheck`),
and `request_precheck_handoff` reports it back as `NOT_ENTITLED`. Duplicating
that check here would create a second source of truth for "may this clinic use
PreCheck?" which can only ever drift from the first. So the hook asks, every
time, for every tenant, and treats a "no" as a silent no-op. `()` still means
"while the subscription is active" (`registry._spec_enabled`), which is the one
gate this module does keep.

**Silence is the failure mode.** Nothing but SEEDED / ALREADY_ACTIVE produces a
message. The agent tool can afford to explain itself — it was ASKED to start a
pre-consult, so "temporarily unavailable" is a real answer to a real question.
Here nobody asked: this fires on its own after a booking, and a patient who
never expected a second message must not receive an apology for one that did
not come. NOT_ENTITLED (the clinic does not have PreCheck), NO_CLINIC (nobody
set `Clinic.brain_tenant_id` on PreCheck's side), CONFLICT and UNAVAILABLE all
end the same way: no message, one log line.

**No retry job.** `professional_notification` buys one because a doctor's
booking email is a commitment that must not evaporate over an SMTP blip. A
pre-consult invitation is an offer, not a commitment: the clinic still has the
patient, the appointment still exists, and `iniciar_pre_consulta` remains
reachable the moment the patient says anything at all. So a failure here
releases the claim, says so at WARNING, and stops. The claim is released rather
than kept precisely because there IS one thing that re-runs this: arq retrying
the surrounding `run_post_booking_hooks` job.

**Ordering is practical, not guaranteed.** The confirmation bubble is sent
inline by the booking turn; this hook runs in the separate arq job that turn
enqueued (`plugins/post_booking.py`), which has to be picked up, load rows and
call brain-api before it sends anything. In practice it lands after the
confirmation. Nothing in arq PROMISES that, and the honest statement is that
two bubbles arriving in the other order would be odd but harmless — not that
the order is enforced. Registration order also puts `pix_deposit` ahead of this
hook (see `plugins/__init__.py`), so a clinic charging a deposit asks for money
before asking for the questionnaire.

**Two channels, one appointment — and only ONE of them says anything.** A
WhatsApp patient is named to brain-api by `phone_number` and invited with a
`wa.me` deep link to PreCheck's shared number, because there the pre-consult
genuinely IS somewhere else: another number, another thread, an app this hook
cannot open for them. A Portal patient is named by `external_id` and told
nothing at all — the hand-off itself is the whole delivery.

That asymmetry is the point rather than an omission (TASK-004 §3c). On the
Portal the two conversations are TABS OF ONE SCREEN, so there is no elsewhere
to send anyone: once the hand-off opens the pre-consult, the patient's own
screen notices the thread stopped being empty and goes there by itself
(`Brain-Message-Frontend`: `precheckRevealVerdict` in `lib/patient-portal.ts`
and THE REVEAL in `components/portal/PortalConversation.tsx`). Until TASK-004
this branch sent a bubble telling the patient to open the Pre-consulta tab —
the WhatsApp shape copied where it does not belong, an instruction for a tap
the interface can perform on its own. It was removed, not replaced: the
presentation the owner wants read is PreCheck's OWN (welcome + LGPD gate),
inside the pre-consult, not an announcement from secretarIA that a tab moved.

Which branch runs is decided ONCE, by `ctx.patient.channel`, and a patient has
exactly one; the two can therefore never both fire for the same appointment,
and the single `ProcessedEvent` claim below covers both.

Before TASK-003 the Portal branch did not exist and this hook skipped every
Brain-Message patient on `no_patient_phone` — `Patient.wa_id` is NULL for all
of them by design (`models/patient.py`). That skip was also, incidentally, what
made this hook and `plugins/pending_identity.py` disjoint. It no longer does:
see that module's docstring, which now records what the two hooks share and
why neither can suppress the other.

Idempotency is the `ProcessedEvent` ledger, namespaced
`precheck:<appointment_id>` — the same durable claim the webhook pipeline,
`plugins/reminders.py` and `professional_notification` use.
`seed_handoff_session` is already idempotent on PreCheck's side (a second call
answers `already_active` instead of duplicating the session), but the WhatsApp
SEND is not: without the claim, an arq retry of the post_booking job would hand
the same patient the same `wa.me` invitation twice. The Portal branch keeps the
claim although it sends nothing — one ledger and one decision point for both,
and a retry that skips a redundant round-trip to brain-api.

Never LOGS a phone number, a patient name, or the message body — only ids and
an outcome string. `services/precheck.py` hashes the phone before logging it;
this module simply never has a reason to mention one.

That promise survived this module starting to SEND the patient's name (FEAT
39: `patient_name` + `booked_service` go out in the hand-off body so PreCheck
can open the questionnaire already knowing who booked what). Sending and
logging are different acts and only the first one changed: no log line here or
in `services/precheck.py` takes either value, including the failure paths,
which record `error_type` / a hashed phone / a status code and nothing else.
The two values are read straight off the detached `ctx` rows and passed
through with no filtering — deliberately. `Patient.name` is usually the
WhatsApp profile name rather than a name the patient typed — it is filled from
`contact.profile.name` on the inbound webhook
(workers/tasks.py::_handle_patient_messages) — and no column today tells those
two apart, so a heuristic here could only guess; the product decision was to
forward what we have, raw, or nothing at all.
"""

from urllib.parse import quote
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import ProcessedEvent
from secretaria.plugins.base import PluginSpec, PostBookingContext
from secretaria.plugins.registry import register
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.precheck import HandoffOutcome, request_precheck_handoff
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)

# The two outcomes that mean a PreCheck session is waiting for this patient at
# the other number. Everything else is a no-op — see the module docstring.
_DELIVERABLE = (HandoffOutcome.SEEDED, HandoffOutcome.ALREADY_ACTIVE)

# The second bubble. Deliberately NOT a hub-configurable field: an unconsumed
# config knob is worse than a fixed string, and nothing here varies per clinic
# — the number is the platform's, shared by every tenant.
#
# It says "outro número" out loud because that is the surprising part: the
# patient is being sent from the clinic's WhatsApp to a different one, and a
# link that silently opens an unknown contact reads like a scam.
_MESSAGE = (
    "Para agilizar seu atendimento, você pode responder agora à sua pré-consulta. 📋\n\n"
    "É rápido, e acontece em outro número de WhatsApp. É só tocar no link "
    "abaixo e enviar a mensagem:\n\n"
    "{link}"
)


def _ledger_key(appointment_id: UUID) -> str:
    return f"precheck:{appointment_id}"


async def _claim(appointment_id: UUID) -> bool:
    """Insert the ledger row. True iff THIS call claimed the send.

    Claim-insert + IntegrityError race pattern, identical to
    `plugins/professional_notification.py::_claim` and `plugins/reminders.py`.
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
    """Give the ledger row back. States one fact: the message did NOT go out.

    Keeping a claim whose send never happened would turn one bad minute — a
    brain-api blip, a Meta 5xx — into a permanently missing invitation, since
    nothing else would ever be allowed to try. Releasing it means an arq retry
    of the surrounding `run_post_booking_hooks` job gets a real second chance.
    It schedules nothing by itself; this module deliberately has no retry job
    (see the module docstring).

    Best-effort: a failure to release is logged and swallowed, since every
    caller is already on its failure path.
    """
    key = _ledger_key(appointment_id)
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == key))
    except Exception as exc:
        logger.warning(
            "precheck_handoff_release_failed",
            appointment_id=str(appointment_id),
            error=str(exc),
        )


def _skip(ctx: PostBookingContext, reason: str) -> None:
    logger.info(
        "precheck_handoff_post_booking_skipped",
        reason=reason,
        tenant_id=str(ctx.tenant.id),
        appointment_id=str(ctx.appointment.id),
    )


def _link(number: str, prefill: str) -> str:
    """The wa.me deep link to PreCheck's shared number.

    The prefilled text is COSMETIC — routing already happened server-side when
    brain-api had PreCheck seed the session (see `services/precheck.py`). It
    exists so the patient's first message is a sentence rather than a blank
    they have to invent.
    """
    return f"https://wa.me/{number}?text={quote(prefill)}"


async def _post_booking(ctx: PostBookingContext) -> None:
    """Offer the pre-consult to the patient who just booked. At most once.

    Order of operations matters: every free local check happens BEFORE the
    ledger claim (so a structural no-op never burns and returns a key), the
    claim happens before the brain-api call (so two concurrent sweeps cannot
    both seed and both send), and the claim is released on every path that
    ends without a message.

    The channel is resolved first and decides everything after it: which
    handle names the patient to brain-api, which preconditions are even
    relevant (the platform WhatsApp number matters only to the branch that
    builds a `wa.me` link out of it), and which sender delivers.
    """
    if ctx.patient is None:
        # A block slot created from the doctor hub: a real appointment with
        # nobody on the other end to invite.
        _skip(ctx, "no_patient")
        return

    settings = get_settings()
    portal = ctx.patient.channel == CHANNEL_BRAIN_MESSAGE
    phone = "" if portal else (ctx.patient.wa_id or "").strip()
    external_id = (ctx.patient.external_id or "").strip() if portal else ""
    number = "" if portal else (settings.PRECHECK_WHATSAPP_NUMBER or "").strip()

    if portal:
        if not external_id:
            # A Brain-Message row with no handle should not exist, but the
            # column is nullable and brain-api would 404 on an empty string.
            _skip(ctx, "no_patient_external_id")
            return
        # Nothing else to resolve. This branch writes no message, so it needs
        # no conversation to write one on; what reacts to the hand-off is the
        # patient's own screen (see the module docstring).
    else:
        if not number:
            # The platform-wide PreCheck number is not configured in this
            # environment. Nothing tenant-specific about it, nothing to retry,
            # and — unlike on the Portal — nothing to send without it.
            _skip(ctx, "precheck_number_not_configured")
            return
        if not phone:
            _skip(ctx, "no_patient_phone")
            return

    if not await _claim(ctx.appointment.id):
        _skip(ctx, "already_sent")
        return

    try:
        result = await request_precheck_handoff(
            ctx.tenant.id,
            phone or None,
            # Exactly one of the two handles, decided above by the channel.
            external_id=external_id or None,
            # Both may be None — a patient row with no name yet, an
            # appointment booked without a type. That is the ordinary case,
            # not a failure: `request_precheck_handoff` simply leaves the key
            # out of the body, and the hand-off proceeds exactly as it did
            # before FEAT 39.
            # The questionnaire is about whoever will be ATTENDED: on a booking
            # made for someone else (services/attendee.py) that is the
            # attendee, not the account that booked - the same person the
            # patient just authorized sharing with the clinic.
            patient_name=ctx.appointment.attendee_name or ctx.patient.name,
            booked_service=ctx.appointment.appointment_type,
        )
        outcome = result.outcome
    except Exception as exc:
        # `request_precheck_handoff` fails closed into UNAVAILABLE by contract
        # and should never raise; belt and braces, because escaping here would
        # leave the claim held with no message behind it.
        logger.warning(
            "precheck_handoff_post_booking_failed",
            reason="handoff_raised",
            error=str(exc),
            tenant_id=str(ctx.tenant.id),
            appointment_id=str(ctx.appointment.id),
        )
        await _release(ctx.appointment.id)
        return

    if outcome not in _DELIVERABLE:
        # NOT_ENTITLED / NO_CLINIC / CONFLICT / UNAVAILABLE. The patient hears
        # nothing at all — this hook was not asked for anything, so it has
        # nothing to apologise for. `services/precheck.py` has already logged
        # the outcome with a hashed phone; this line ties it to the booking.
        await _release(ctx.appointment.id)
        _skip(ctx, f"handoff_{outcome.value}")
        return

    if not portal:
        # Only WhatsApp has a send. On the Portal the hand-off above already
        # WAS the delivery, so there is nothing here that could fail and
        # nothing to give the claim back for.
        try:
            # `for_tenant` FAILS CLOSED on a missing tenant credential rather
            # than falling back to the global env scaffold (PROMPT_FIX_21) —
            # it raises, which is why the send lives inside this try alongside
            # the client build.
            client = WhatsAppClient.for_tenant(ctx.tenant, ctx.waba_token)
            await client.send_text_message(
                to=phone,
                body=_MESSAGE.format(link=_link(number, settings.PRECHECK_HANDOFF_PREFILL)),
            )
        except Exception as exc:
            # The session IS seeded on PreCheck's side; only the invitation is
            # lost. Releasing the claim lets an arq retry of the surrounding
            # job send it, and a second seed is an idempotent `already_active`.
            # `error_type` only — a Meta error body echoes the recipient's
            # number and the message text back at us.
            logger.warning(
                "precheck_handoff_post_booking_failed",
                reason="send_failed",
                error_type=type(exc).__name__,
                tenant_id=str(ctx.tenant.id),
                appointment_id=str(ctx.appointment.id),
            )
            await _release(ctx.appointment.id)
            return

    logger.info(
        "precheck_handoff_post_booking_sent",
        outcome=outcome.value,
        source=ctx.source,
        # WHICH of the two branches ran. The one fact a reader of this line
        # could not otherwise recover, and the one that says whether a `wa.me`
        # link went out at all (on the Portal, none does: TASK-004 §3c).
        channel=ctx.patient.channel,
        tenant_id=str(ctx.tenant.id),
        appointment_id=str(ctx.appointment.id),
    )


PRECHECK_HANDOFF_SPEC = PluginSpec(
    id="precheck_handoff",
    entitlement_keys=(),
    post_booking=_post_booking,
)
register(PRECHECK_HANDOFF_SPEC)
