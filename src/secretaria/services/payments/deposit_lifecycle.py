"""The Pix deposit (sinal) money brain: every state transition + every
patient-facing pt-BR copy for a money event lives HERE, nowhere else.

`maybe_create_deposit` is called right after a booking commits (a later wave
wires the call site — this FOUNDATION wave only builds the function).
`on_appointment_cancelled` / `on_no_show` / `register_reschedule` are the
three ways an EXISTING deposit's money outcome can change; `apply_asaas_event`
is the worker-side core that reacts to Asaas' own webhook. None of these
functions commit — callers own the transaction, exactly like every other
`services/` module in this repo (see CLAUDE.md's layering rule), with the
single deliberate exception of `apply_asaas_event`'s own claim+mutate step
(see its docstring for why).

`_asaas_client_for` is the ONE construction seam for `AsaasClient` — tests
monkeypatch it to install a fake recording client, so nothing here ever hits
a real network in a test run.
"""

import hmac
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, AppointmentStatus, Patient, Professional, Tenant
from secretaria.models.pix_deposit import PixDeposit, PixDepositStatus
from secretaria.models.processed_asaas_event import ProcessedAsaasEvent
from secretaria.services import tenant_config as cfg
from secretaria.services.calendar import CalendarService
from secretaria.services.payments.asaas import AsaasClient
from secretaria.services.payments.money import format_brl, parse_brl_to_cents
from secretaria.services.tenant_config import load_tenant_config
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)


def _asaas_client_for(api_key: str) -> AsaasClient:
    """Construction seam so tests can monkeypatch in a fake recording client."""
    return AsaasClient(api_key)


def _tenant_tz(tenant: Tenant) -> ZoneInfo:
    try:
        return ZoneInfo(tenant.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive timestamp (e.g. from SQLite) as UTC; pass tz-aware through."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def get_deposit_for_appointment(
    session: AsyncSession, appointment_id: UUID
) -> PixDeposit | None:
    """S3 convenience: the one deposit row for an appointment, or None."""
    return await session.scalar(
        select(PixDeposit).where(PixDeposit.appointment_id == appointment_id)
    )


# --------------------------------------------------------------------------
# maybe_create_deposit — best-effort, called right after a booking commits
# --------------------------------------------------------------------------


async def _price_text_for_appointment(
    session: AsyncSession, tenant: Tenant, appointment: Appointment
) -> str | None:
    """Best-effort free-text price lookup, honoring a professional-level
    catalog override. Mirrors the matching logic of the old
    services/payments/pix.py stub's `_price_for_appointment` (match by exact
    `appointment_type` name), but resolves through the OWNING professional's
    own appointment_types when the appointment has one, falling back to the
    tenant's — exactly like `services/tenant_config.professional_appointment_types`.
    """
    if not appointment.appointment_type:
        return None
    catalog = cfg.active_appointment_types(tenant)
    if appointment.professional_id is not None:
        professional = await session.get(Professional, appointment.professional_id)
        if professional is not None:
            catalog = cfg.professional_appointment_types(professional, tenant)
    for entry in catalog:
        if entry.get("name") == appointment.appointment_type:
            price = entry.get("price")
            if price:
                return str(price)
    return None


def _deposit_request_text(
    tenant: Tenant, appointment: Appointment, amount_cents: int, copy_paste: str | None
) -> str:
    appt_type = appointment.appointment_type or "sua consulta"
    lines = [
        f"Para confirmar {appt_type} na {tenant.clinic_name}, pedimos um sinal de "
        f"{format_brl(amount_cents)} via Pix."
    ]
    if copy_paste:
        lines.append("Pix copia e cola:")
        lines.append(copy_paste)
    lines.append("Sua reserva fica garantida assim que o pagamento for confirmado.")
    lines.append("O código Pix vence ainda hoje.")
    return "\n".join(lines)


async def _send_deposit_request(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient,
    amount_cents: int,
    copy_paste: str | None,
    waba_token: str | None,
) -> None:
    token = waba_token if waba_token is not None else await cfg.get_waba_token(session, tenant.id)
    client = WhatsAppClient.for_tenant(tenant, token)
    text = _deposit_request_text(tenant, appointment, amount_cents, copy_paste)
    await client.send_text_message(to=patient.wa_id, body=text)


async def maybe_create_deposit(
    session: AsyncSession,
    *,
    tenant: Tenant,
    patient: Patient | None,
    appointment: Appointment,
    waba_token: str | None,
) -> PixDeposit | None:
    """Best-effort: create a Pix deposit charge for a freshly booked appointment.

    Every guard below is a distinct, count-only debug log + None return — a
    tenant without the feature configured (or a price the parser can't read)
    is an expected, silent no-op, never an error. Once a real Asaas payment
    id exists, ANY later failure (QR fetch, WhatsApp send) still persists the
    PixDeposit row (a "partial" result — the payment is real and must be
    resolvable by the webhook later) rather than returning None and losing
    track of a live Asaas charge. Nothing here ever raises into the caller —
    a payment/notification hiccup must never block or unwind the booking
    itself, which is already committed by the time this runs.
    """
    if not tenant.pix_deposit_enabled:
        logger.debug("pix_deposit_skipped_disabled", tenant_id=str(tenant.id))
        return None
    if patient is None:
        logger.debug("pix_deposit_skipped_no_patient", tenant_id=str(tenant.id))
        return None

    api_key = await cfg.get_asaas_api_key(session, tenant.id)
    if not api_key:
        logger.debug("pix_deposit_skipped_no_api_key", tenant_id=str(tenant.id))
        return None

    price_text = await _price_text_for_appointment(session, tenant, appointment)
    price_cents = parse_brl_to_cents(price_text)
    if price_cents is None:
        logger.debug(
            "pix_deposit_skipped_unparseable_price",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
        )
        return None

    amount_cents = round(price_cents * tenant.pix_deposit_percent / 100)
    if amount_cents <= 0:
        logger.debug(
            "pix_deposit_skipped_zero_amount",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
        )
        return None

    client = _asaas_client_for(api_key)

    try:
        customer_id = patient.asaas_customer_id
        if not customer_id:
            customer_id = await client.create_customer(patient.name or "Paciente", patient.wa_id)
            patient.asaas_customer_id = customer_id
    except Exception as exc:
        logger.warning(
            "pix_deposit_customer_failed", error=str(exc), tenant_id=str(tenant.id)
        )
        return None

    due_date = datetime.now(_tenant_tz(tenant)).date().isoformat()
    description = f"Sinal - {appointment.appointment_type or 'consulta'} - {tenant.clinic_name}"
    try:
        payment = await client.create_pix_payment(
            customer_id, amount_cents, str(appointment.id), description[:255], due_date
        )
    except Exception as exc:
        logger.warning(
            "pix_deposit_payment_failed",
            error=str(exc),
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
        )
        return None

    payment_id = payment.get("id")
    if not payment_id:
        logger.warning(
            "pix_deposit_payment_missing_id",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
        )
        return None

    copy_paste: str | None = None
    try:
        qr = await client.get_pix_qr(str(payment_id))
        copy_paste = qr.get("payload")
    except Exception as exc:
        logger.warning(
            "pix_deposit_qr_failed",
            error=str(exc),
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
        )

    deposit = PixDeposit(
        tenant_id=tenant.id,
        appointment_id=appointment.id,
        patient_id=patient.id,
        asaas_payment_id=str(payment_id),
        amount_cents=amount_cents,
        percent_applied=tenant.pix_deposit_percent,
        pix_copy_paste=copy_paste,
        status=PixDepositStatus.AWAITING,
    )
    session.add(deposit)
    await session.flush()

    try:
        await _send_deposit_request(
            session, tenant, appointment, patient, amount_cents, copy_paste, waba_token
        )
    except Exception as exc:
        logger.warning(
            "pix_deposit_send_failed",
            error=str(exc),
            tenant_id=str(tenant.id),
            deposit_id=str(deposit.id),
        )

    logger.info(
        "pix_deposit_created",
        tenant_id=str(tenant.id),
        appointment_id=str(appointment.id),
        deposit_id=str(deposit.id),
    )
    return deposit


# --------------------------------------------------------------------------
# on_appointment_cancelled / on_no_show — resolving an EXISTING deposit
# --------------------------------------------------------------------------


async def _void_awaiting(
    session: AsyncSession, tenant: Tenant, deposit: PixDeposit, now: datetime
) -> None:
    """Best-effort void an unpaid Asaas charge, then mark the deposit EXPIRED."""
    if deposit.asaas_payment_id:
        api_key = await cfg.get_asaas_api_key(session, tenant.id)
        if api_key:
            try:
                await _asaas_client_for(api_key).delete_payment(deposit.asaas_payment_id)
            except Exception as exc:
                logger.warning(
                    "pix_deposit_void_failed",
                    error=str(exc),
                    tenant_id=str(tenant.id),
                    deposit_id=str(deposit.id),
                )
    deposit.status = PixDepositStatus.EXPIRED
    deposit.resolved_at = now


async def _refund(
    session: AsyncSession,
    tenant: Tenant,
    deposit: PixDeposit,
    *,
    value_cents: int | None,
    full: bool,
    now: datetime,
) -> str:
    """Issue a full/partial refund via Asaas. On ANY failure (missing key,
    missing payment id, or the PSP call itself raising) the deposit STAYS
    PAID — never pretend a refund happened — and "refund_failed" is returned."""
    api_key = await cfg.get_asaas_api_key(session, tenant.id)
    if not api_key or not deposit.asaas_payment_id:
        logger.error(
            "pix_refund_failed",
            tenant_id=str(tenant.id),
            deposit_id=str(deposit.id),
            appointment_id=str(deposit.appointment_id),
        )
        return "refund_failed"

    try:
        await _asaas_client_for(api_key).refund_payment(deposit.asaas_payment_id, value_cents)
    except Exception as exc:
        logger.error(
            "pix_refund_failed",
            error=str(exc),
            tenant_id=str(tenant.id),
            deposit_id=str(deposit.id),
            appointment_id=str(deposit.appointment_id),
        )
        return "refund_failed"

    if full:
        deposit.status = PixDepositStatus.CANCELLED_REFUNDED
        deposit.refunded_amount_cents = deposit.amount_cents
        deposit.resolved_at = now
        return "refunded"

    deposit.status = PixDepositStatus.CANCELLED_RETAINED
    deposit.refunded_amount_cents = value_cents
    deposit.resolved_at = now
    return "partial_refund"


async def on_appointment_cancelled(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    waba_token: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Resolve a deposit's money outcome for a cancelled appointment.

    Returns one of "voided" / "refunded" / "partial_refund" / "retained" /
    "refund_failed", or None when there is no deposit at all or it is already
    resolved (a second cancel call must be a safe no-op). `waba_token` is
    accepted for signature symmetry with the rest of this module's tenant-
    scoped functions but unused: this function sends NO message itself — see
    `cancellation_notice` below for the copy; the caller (a later wave, which
    owns the actual conversational turn) decides whether/how to send it. Does
    NOT commit — caller's transaction.
    """
    deposit = await get_deposit_for_appointment(session, appointment.id)
    if deposit is None or deposit.status not in (PixDepositStatus.AWAITING, PixDepositStatus.PAID):
        return None

    now = now or datetime.now(UTC)

    if deposit.status is PixDepositStatus.AWAITING:
        await _void_awaiting(session, tenant, deposit, now)
        return "voided"

    # PAID: refund window decides full refund vs. the tenant's retention policy.
    start_at = appointment.start_at
    if start_at is None:
        hours_until = None
        logger.info(
            "pix_refund_window_unknown_start",
            tenant_id=str(tenant.id),
            deposit_id=str(deposit.id),
        )
    else:
        hours_until = (_as_utc(start_at) - now).total_seconds() / 3600

    if hours_until is None or hours_until > tenant.pix_refund_window_hours:
        return await _refund(session, tenant, deposit, value_cents=None, full=True, now=now)

    if tenant.pix_retention_policy == "partial":
        partial_amount = round(deposit.amount_cents * tenant.pix_partial_refund_percent / 100)
        return await _refund(
            session, tenant, deposit, value_cents=partial_amount, full=False, now=now
        )

    # "total": clinic keeps the whole deposit, no PSP call at all.
    deposit.status = PixDepositStatus.CANCELLED_RETAINED
    deposit.refunded_amount_cents = 0
    deposit.resolved_at = now
    return "retained"


def cancellation_notice(outcome: str | None, tenant: Tenant, deposit: PixDeposit) -> str | None:
    """The honest pt-BR line to append after resolving a cancellation's money
    outcome. Callers (a later wave) own the rest of the conversational reply;
    this returns ONLY the money-specific sentence, or None when there is
    nothing worth telling the patient (voided, or no outcome at all)."""
    if outcome == "refunded":
        return "O estorno do sinal foi solicitado — normalmente cai em poucos dias úteis."
    if outcome == "partial_refund":
        refunded = format_brl(deposit.refunded_amount_cents or 0)
        return (
            f"Como o cancelamento ficou fora do prazo, {refunded} do sinal serão estornados; "
            "o restante fica retido pela clínica."
        )
    if outcome == "retained":
        return (
            f"Como o cancelamento ocorreu com menos de {tenant.pix_refund_window_hours}h de "
            "antecedência, o sinal fica retido pela clínica."
        )
    if outcome == "refund_failed":
        return "O estorno será processado pela clínica."
    return None


async def on_no_show(
    session: AsyncSession, *, tenant: Tenant, appointment: Appointment
) -> str | None:
    """Resolve a deposit's money outcome when the doctor marks a no-show.

    AWAITING (never paid) -> best-effort void, "voided". PAID -> retained in
    full (a no-show is never refunded), "retained". None when there is no
    deposit or it is already resolved. Sends no message. Does NOT commit.
    """
    deposit = await get_deposit_for_appointment(session, appointment.id)
    if deposit is None or deposit.status not in (PixDepositStatus.AWAITING, PixDepositStatus.PAID):
        return None

    now = datetime.now(UTC)
    if deposit.status is PixDepositStatus.AWAITING:
        await _void_awaiting(session, tenant, deposit, now)
        return "voided"

    deposit.status = PixDepositStatus.NO_SHOW_RETAINED
    deposit.refunded_amount_cents = 0
    deposit.resolved_at = now
    return "retained"


async def register_reschedule(
    session: AsyncSession, *, tenant: Tenant, appointment: Appointment
) -> tuple[bool, int]:
    """Count a reschedule against `tenant.pix_reschedule_limit`.

    Returns (allowed, reschedule_count_after). No deposit at all -> always
    (True, 0) — nothing to limit. At the limit, the count is NOT incremented
    (a repeated blocked attempt keeps reporting the same count). The
    appointment row itself needs no other change here — see
    models/pix_deposit.py's docstring for why a reschedule never re-points
    the deposit's FK. Does NOT commit — caller's transaction.
    """
    deposit = await get_deposit_for_appointment(session, appointment.id)
    if deposit is None:
        return True, 0
    if deposit.reschedule_count >= tenant.pix_reschedule_limit:
        return False, deposit.reschedule_count
    deposit.reschedule_count += 1
    return True, deposit.reschedule_count


# --------------------------------------------------------------------------
# apply_asaas_event — the worker core reacting to Asaas' own webhook
# --------------------------------------------------------------------------


async def _notify_paid(session: AsyncSession, tenant: Tenant, deposit: PixDeposit) -> None:
    appointment = await session.get(Appointment, deposit.appointment_id)
    text = "Sinal recebido! Sua consulta está garantida."
    if appointment is not None and appointment.start_at is not None:
        when = _as_utc(appointment.start_at).astimezone(_tenant_tz(tenant)).strftime(
            "%d/%m/%Y às %H:%M"
        )
        text = f"{text} {when}."

    to = appointment.phone if appointment is not None else None
    if not to and deposit.patient_id is not None:
        patient = await session.get(Patient, deposit.patient_id)
        to = patient.wa_id if patient is not None else None
    if not to:
        logger.info(
            "pix_deposit_paid_notice_skipped_no_phone",
            tenant_id=str(tenant.id),
            deposit_id=str(deposit.id),
        )
        return

    waba_token = await cfg.get_waba_token(session, tenant.id)
    await WhatsAppClient.for_tenant(tenant, waba_token).send_text_message(to=to, body=text)


async def _notify_expired(session: AsyncSession, tenant: Tenant, appointment: Appointment) -> None:
    # Best-effort Google Calendar event delete, reusing the same per-tenant
    # calendar construction the patient-cancel path uses
    # (api/hub/calendar.py::_get_calendar + CalendarService.cancel_event) —
    # skipped entirely (not an error) when the tenant has no Calendar
    # connected at all.
    if appointment.google_event_id:
        try:
            config = await load_tenant_config(session, tenant)
            if config.google_refresh_token:
                await CalendarService.from_tenant_config(config).cancel_event(
                    appointment.google_event_id
                )
        except Exception as exc:
            logger.warning(
                "pix_deposit_expired_calendar_delete_failed",
                error=str(exc),
                tenant_id=str(tenant.id),
                appointment_id=str(appointment.id),
            )

    to = appointment.phone
    if not to and appointment.patient_id is not None:
        patient = await session.get(Patient, appointment.patient_id)
        to = patient.wa_id if patient is not None else None
    if not to:
        logger.info(
            "pix_deposit_expired_notice_skipped_no_phone",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
        )
        return

    waba_token = await cfg.get_waba_token(session, tenant.id)
    text = "O prazo para pagamento do sinal expirou e sua reserva foi liberada."
    await WhatsAppClient.for_tenant(tenant, waba_token).send_text_message(to=to, body=text)


async def apply_asaas_event(
    session: AsyncSession,
    *,
    event_id: str,
    event_type: str,
    payment_id: str | None,
    access_token_header: str | None,
) -> str:
    """Apply one Asaas webhook event — the worker's core (workers/payments_tasks.py
    ::process_asaas_event calls this). Returns a short outcome string
    ("unknown_payment" | "auth_failed" | "duplicate" | "paid" | "expired" |
    "noop"), consumed for logging/tests.

    Authenticity is verified HERE, not in the API handler
    (api/webhook_asaas.py): unlike Meta's pure-HMAC-over-the-raw-body check
    (verifiable with zero DB access), Asaas' own webhook auth is a PER-TENANT
    shared token — resolving "which tenant" requires the
    payment_id -> PixDeposit -> tenant_id lookup first, a DB round-trip the
    fast-ACK handler must never make. So the handler stays dumb+fast; all of
    resolve -> authenticate -> dedupe -> mutate happens here.

    Transaction shape: the spec for this function is "the claim INSERT and
    the state mutation happen in the SAME transaction" (mirroring
    workers/tasks.py's `_persist_inbound_message` / plugins/reminders.py's
    `_claim_reminder` check-then-claim pattern). Both of THOSE call
    `session.begin()` as the FIRST operation on a brand-new session. This
    function cannot do that: resolving the deposit/tenant/expected-token
    BEFORE the claim (required so a failed auth check makes NO claim at all)
    necessarily issues prior reads on `session`, which triggers SQLAlchemy's
    "autobegin" — and `session.begin()` raises `InvalidRequestError` if
    called on a session that already has an autobegun transaction
    (confirmed empirically, not merely assumed). So instead: the claim INSERT
    and every mutation ride on that SAME autobegun transaction, closed by one
    explicit `session.commit()`; a genuine duplicate (the rare concurrent-
    delivery race, not the common already-claimed fast path below) surfaces
    as `IntegrityError` on the unique `event_id` constraint at that commit (or
    at an earlier autoflush), caught and rolled back. The atomicity guarantee
    — claim and mutation committed or rolled back together — is identical
    either way; only the literal `session.begin()` API call differs.
    """
    if not payment_id:
        logger.info("asaas_event_no_payment_id", event_id=event_id, event_type=event_type)
        return "unknown_payment"

    deposit = await session.scalar(
        select(PixDeposit).where(PixDeposit.asaas_payment_id == payment_id)
    )
    if deposit is None:
        logger.info("asaas_event_unknown_payment", event_id=event_id, event_type=event_type)
        return "unknown_payment"

    tenant = await session.get(Tenant, deposit.tenant_id)
    if tenant is None:
        logger.warning("asaas_event_tenant_missing", tenant_id=str(deposit.tenant_id))
        return "unknown_payment"

    expected_token = await cfg.get_asaas_webhook_token(session, tenant.id)
    if (
        not expected_token
        or not access_token_header
        or not hmac.compare_digest(access_token_header, expected_token)
    ):
        logger.warning("asaas_webhook_auth_failed", tenant_id=str(tenant.id))
        return "auth_failed"

    slot_freed = False
    expired_appointment: Appointment | None = None
    outcome = "noop"

    try:
        existing = await session.scalar(
            select(ProcessedAsaasEvent.id).where(ProcessedAsaasEvent.event_id == event_id)
        )
        if existing is not None:
            return "duplicate"
        session.add(ProcessedAsaasEvent(event_id=event_id))

        if event_type in ("PAYMENT_RECEIVED", "PAYMENT_CONFIRMED"):
            if deposit.status == PixDepositStatus.AWAITING:
                deposit.status = PixDepositStatus.PAID
                deposit.paid_at = datetime.now(UTC)
                outcome = "paid"
        elif event_type in ("PAYMENT_OVERDUE", "PAYMENT_DELETED"):
            if deposit.status == PixDepositStatus.AWAITING:
                deposit.status = PixDepositStatus.EXPIRED
                deposit.resolved_at = datetime.now(UTC)
                expired_appointment = await session.get(Appointment, deposit.appointment_id)
                if expired_appointment is not None:
                    expired_appointment.status = AppointmentStatus.CANCELLED
                    slot_freed = True
                outcome = "expired"
        elif event_type == "PAYMENT_REFUNDED":
            logger.info(
                "asaas_event_refund_observed",
                tenant_id=str(tenant.id),
                deposit_id=str(deposit.id),
            )
        else:
            logger.info(
                "asaas_event_type_unhandled", event_type=event_type, tenant_id=str(tenant.id)
            )

        await session.commit()
    except IntegrityError:
        await session.rollback()
        return "duplicate"

    # Best-effort notifications, AFTER commit — never allowed to undo the
    # already-committed state mutation above.
    try:
        if outcome == "paid":
            await _notify_paid(session, tenant, deposit)
        elif outcome == "expired" and slot_freed and expired_appointment is not None:
            await _notify_expired(session, tenant, expired_appointment)
    except Exception as exc:
        logger.warning(
            "asaas_event_notification_failed",
            error=str(exc),
            tenant_id=str(tenant.id),
            deposit_id=str(deposit.id),
        )

    return outcome
