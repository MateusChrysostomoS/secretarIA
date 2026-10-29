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

No money ever leaves through this module: a PAID deposit is never refunded
by the system (decision D15) - when the policy says it is due back, the
outcome is `refund_due` / `partial_refund_due` and the clinic returns it in
the provider's own panel. Provider calls (voiding an unpaid charge) go
through `services/payments/registry.py`, which tests replace with a fake.
"""

import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, Professional, Tenant
from secretaria.models.clinic_payment_account import PROVIDER_ASAAS
from secretaria.models.insurance import ProfessionalInsurancePlan, TenantInsurancePlan
from secretaria.models.payment_attention_item import (
    REASON_PAID_THEN_CANCELLED,
    REASON_REFUND_DUE_POLICY,
    PaymentAttentionItem,
)
from secretaria.models.pix_deposit import (
    DEPOSIT_TIMING_AFTER,
    DEPOSIT_TIMING_BEFORE,
    FULFILLMENT_NEEDS_ATTENTION,
    PixDeposit,
    PixDepositStatus,
)
from secretaria.models.processed_asaas_event import ProcessedAsaasEvent
from secretaria.services import tenant_config as cfg
from secretaria.services.booking_scope import canonical_service_name
from secretaria.services.calendar import CalendarService
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.payments import (
    accounts,
    adjustment_queries,
    attention,
    notify,
    payer_data,
    registry,
)
from secretaria.services.payments.money import format_brl, parse_brl_to_cents
from secretaria.services.payments.pix_message import pix_code_tail
from secretaria.services.payments.provider import ChargeRequest, PaymentStatus, ProviderError
from secretaria.services.service_catalog import load_service_catalog, service_by_name
from secretaria.services.tenant_config import load_tenant_config
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)


# The legacy route only ever receives these Asaas event names (anything else is a claimed no-op).
_LEGACY_EVENT_STATUS = {
    "PAYMENT_RECEIVED": PaymentStatus.PAID,
    "PAYMENT_CONFIRMED": PaymentStatus.PAID,
    "PAYMENT_OVERDUE": PaymentStatus.EXPIRED,
    "PAYMENT_DELETED": PaymentStatus.EXPIRED,
    "PAYMENT_REFUNDED": PaymentStatus.REFUNDED,
    "PAYMENT_PARTIALLY_REFUNDED": PaymentStatus.REFUNDED,
}


def _tenant_tz(tenant: Tenant) -> ZoneInfo:
    try:
        return ZoneInfo(tenant.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive timestamp (e.g. from SQLite) as UTC; pass tz-aware through."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# Structured reasons for a skipped deposit. Each one keeps its historical
# event name (`pix_deposit_skipped_<reason>`) AND carries `reason` as a field,
# so the existing alerts keep matching while the whole family stays groupable
# by one key. `no_service` used to be lumped into `unparseable_price`: it is
# the interesting one — the booking named a service the effective catalog does
# not have, which is a configuration/ownership problem, not a pricing one.
DEPOSIT_SKIP_DISABLED = "disabled"
DEPOSIT_SKIP_NO_PATIENT = "no_patient"
DEPOSIT_SKIP_NO_API_KEY = "no_api_key"
# The clinic chose a provider that has no wrapper registered (yet).
DEPOSIT_SKIP_NO_PROVIDER = "no_provider"
DEPOSIT_SKIP_NO_SERVICE = "no_service"
DEPOSIT_SKIP_UNPARSEABLE_PRICE = "unparseable_price"
DEPOSIT_SKIP_ZERO_AMOUNT = "zero_amount"
# The booking's convênio is one of the clinic's plans that the clinic marked
# "não cobra sinal" (tenant_insurance_plans.charge_deposit = false). Owner's
# decision 2026-09-25: per clinic AND per plan, never inferred from the
# operator - see models/insurance.py.
DEPOSIT_SKIP_INSURANCE_NO_CHARGE = "insurance_no_charge"
# TASK-017: the clinic charges BEFORE confirming, so this hook has nothing to add; and an
# appointment that already has a deposit row never gets a second charge.
DEPOSIT_SKIP_TIMING_BEFORE = "timing_before"
DEPOSIT_SKIP_ALREADY_CHARGED = "already_charged"


def _log_deposit_skip(tenant: Tenant, appointment: Appointment, reason: str) -> None:
    """One sanitized line per skipped deposit: ids and an enum, nothing else.

    Never the price, the service name, the patient or the phone — a skip is
    diagnosed from tenant + appointment + owner + reason, and those are enough
    to find the row in the hub.
    """
    logger.debug(
        f"pix_deposit_skipped_{reason}",
        reason=reason,
        tenant_id=str(tenant.id),
        appointment_id=str(appointment.id),
        professional_id=(
            str(appointment.professional_id) if appointment.professional_id else None
        ),
        has_owner=appointment.professional_id is not None,
    )


async def get_deposit_for_appointment(
    session: AsyncSession, appointment_id: UUID
) -> PixDeposit | None:
    """S3 convenience: the one deposit row for an appointment, or None.

    A PAID deposit comes back stamped with what the clinic holds for it (sinal + paid service
    differences - differences asked back, `adjustment_queries.held_total_cents`), so every
    pt-BR text built from this lookup (cancel notices and warnings, release texts) quotes the
    total, not the bare `amount_cents` (plan 2026-10-26 Task 8)."""
    deposit = await session.scalar(
        select(PixDeposit).where(PixDeposit.appointment_id == appointment_id)
    )
    if deposit is not None and deposit.status is PixDepositStatus.PAID:
        await adjustment_queries.held_total_cents(session, deposit)
    return deposit


@dataclass(frozen=True)
class AppointmentDepositView:
    """What the agenda may know about an appointment's deposit: the state of the
    money and its amount. Never the Pix payload, the Asaas id or the patient."""

    status: PixDepositStatus
    amount_cents: int
    # `fulfillment_status == needs_attention`: an attention item is open on this deposit.
    needs_attention: bool = False
    # PAID only (plan 2026-10-26): what the clinic holds when service differences changed it
    # (`adjustment_queries.held_total_cents`; None = `amount_cents`), and a difference Pix still
    # waiting for payment (patient's or clinic's change; None = none).
    held_cents: int | None = None
    pending_difference_cents: int | None = None


async def load_deposit_views(
    session: AsyncSession, tenant_id: UUID, appointment_ids: Iterable[UUID | None]
) -> dict[UUID, AppointmentDepositView]:
    """The deposit state of a whole page of appointments in ONE query.

    Read-only sibling of `get_deposit_for_appointment` for list endpoints (which
    must not do a lookup per row). `tenant_id` is in the WHERE: a deposit row of
    another clinic never comes back, whatever ids the caller passes. Appointments
    without a deposit are simply absent from the result; no ids (or only None) means
    no query. A charge with no appointment yet (the `before`-mode hold of spec F)
    can never match an id here. Selects three columns on purpose - the Pix
    copy-paste payload stays in the DB.
    """
    ids = {i for i in appointment_ids if i is not None}
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                PixDeposit.appointment_id,
                PixDeposit.status,
                PixDeposit.amount_cents,
                PixDeposit.fulfillment_status,
                PixDeposit.id,
            ).where(
                PixDeposit.tenant_id == tenant_id,
                PixDeposit.appointment_id.in_(ids),
            )
        )
    ).all()
    # At most ONE more query, for the PAID deposits' service-change adjustments (plan 2026-10-26).
    totals = await adjustment_queries.load_adjustment_totals(
        session,
        tenant_id,
        [row[4] for row in rows if row[1] is PixDepositStatus.PAID],
    )
    views: dict[UUID, AppointmentDepositView] = {}
    for appointment_id, status, amount_cents, fulfillment, deposit_id in rows:
        total = totals.get(deposit_id)
        views[appointment_id] = AppointmentDepositView(
            status=status,
            amount_cents=amount_cents,
            needs_attention=fulfillment == FULFILLMENT_NEEDS_ATTENTION,
            held_cents=amount_cents + total.held_delta_cents
            if total is not None and total.held_delta_cents
            else None,
            pending_difference_cents=total.pending_difference_cents if total else None,
        )
    return views


# --------------------------------------------------------------------------
# maybe_create_deposit — best-effort, called right after a booking commits
# --------------------------------------------------------------------------


async def _resolve_service_and_price(
    session: AsyncSession, tenant: Tenant, appointment: Appointment
) -> tuple[str | None, str | None]:
    """(canonical service name, free-text price) for one appointment.

    Resolves through the OWNING professional's own `appointment_types` when
    the appointment has one, falling back to the tenant's — exactly like
    `services/tenant_config.professional_appointment_types`. That fallback is
    the reason `Appointment.professional_id` must be populated even for a
    clinic with a single professional: without it this looked the price up in
    the tenant's LEGACY catalog, which is empty on every clinic that
    configures its services per professional (see services/booking_scope.py).

    The name itself is matched through the shared rule
    (`booking_scope.canonical_service_name`), so a stored type resolves here
    exactly as the booking surface canonicalised it. A `None` name means "no
    such active service"; a `None` price with a real name means the service
    exists but carries no readable price — two different skip reasons.
    """
    if not appointment.appointment_type:
        return None, None
    # The clinic's canonical catalog: with it, a historical `appointment_type`
    # stored under a different spelling still resolves to the right service
    # (services/service_catalog.py normalizes for identity) — which is what
    # "the price stops depending on an exactly-typed string" means. Empty for
    # a tenant not backfilled yet -> the pre-catalog exact-name behaviour.
    services = await load_service_catalog(session, tenant.id)
    catalog = cfg.active_appointment_types(tenant, services)
    if appointment.professional_id is not None:
        professional = await session.get(Professional, appointment.professional_id)
        if professional is not None:
            catalog = cfg.professional_appointment_types(professional, tenant, services)
    canonical = canonical_service_name(catalog, appointment.appointment_type)
    if canonical is None:
        # Historical rows keep whatever text was stored at booking time and are
        # NEVER rewritten (contract: no data migration on `appointments`), so
        # fall back to catalog identity — normalize the stored text, find the
        # canonical service, and price under its own name.
        service = service_by_name(services, appointment.appointment_type)
        canonical = service.name if service is not None else None
    if canonical is None:
        return None, None
    for entry in catalog:
        if entry.get("name") == canonical:
            price = entry.get("price")
            return canonical, (str(price) if price else None)
    return canonical, None


async def _price_text_for_appointment(
    session: AsyncSession, tenant: Tenant, appointment: Appointment
) -> str | None:
    """Best-effort free-text price for one appointment (see `_resolve_service_and_price`)."""
    _name, price = await _resolve_service_and_price(session, tenant, appointment)
    return price


def _deposit_request_text(
    tenant: Tenant,
    appointment: Appointment,
    amount_cents: int,
    copy_paste: str | None,
    payment_note: str | None = None,
) -> str:
    appt_type = appointment.appointment_type or "sua consulta"
    lines = [
        f"Para confirmar {appt_type} na {tenant.clinic_name}, pedimos um sinal de "
        f"{format_brl(amount_cents)} via Pix."
    ]
    lines.append("Sua reserva fica garantida assim que o pagamento for confirmado.")
    lines.append("O código Pix vence ainda hoje.")
    if payment_note:
        # TASK-008 §4.3: a custom "Outro" convênio's payment explanation,
        # repeated here (it already appeared once at booking confirmation) so
        # it is not buried in an earlier message by the time the Pix ask
        # arrives.
        lines.append("")
        lines.append(f"💳 Sobre o pagamento do convênio:\n{payment_note}")
    # ONE message: the code is its last lines (fenced - see `pix_message`).
    tail = pix_code_tail(copy_paste)
    if tail:
        lines.append("")
        lines.extend(tail)
    return "\n".join(lines)


async def _insurance_payment_note(session: AsyncSession, appointment: Appointment) -> str | None:
    """`custom_payment_note` of whichever custom "Outro" plan this booking used.

    None for a catalog-backed plan (its `note` is internal metadata, never
    patient-facing copy) or no plan at all.
    """
    professional_plan_id = getattr(appointment, "insurance_professional_plan_id", None)
    if professional_plan_id is not None:
        return await session.scalar(
            select(ProfessionalInsurancePlan.custom_payment_note).where(
                ProfessionalInsurancePlan.id == professional_plan_id
            )
        )
    plan_id = getattr(appointment, "insurance_plan_id", None)
    if plan_id is None:
        return None
    return await session.scalar(
        select(TenantInsurancePlan.custom_payment_note).where(TenantInsurancePlan.id == plan_id)
    )


async def _send_deposit_request(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient,
    amount_cents: int,
    copy_paste: str | None,
    waba_token: str | None,
) -> None:
    payment_note = await _insurance_payment_note(session, appointment)
    text = _deposit_request_text(tenant, appointment, amount_cents, copy_paste, payment_note)
    if patient.channel == CHANNEL_BRAIN_MESSAGE:
        # A Portal patient has no `wa_id`: the request travels as a Portal message, through the
        # ONE channel resolver of the payments code (`notify.resolve_sender`).
        await notify.deliver(
            session, tenant, patient, [notify.Text(text)], waba_token=waba_token
        )
        return
    token = waba_token if waba_token is not None else await cfg.get_waba_token(session, tenant.id)
    client = WhatsAppClient.for_tenant(tenant, token)
    await client.send_text_message(to=patient.wa_id, body=text)


async def _insurance_charges_deposit(
    session: AsyncSession, tenant: Tenant, appointment: Appointment
) -> bool:
    """False only when the booking's plan is one flagged "no deposit".

    Reads the plan BY ID - never re-matching the free-text `insurance`. TASK-008:
    checks `insurance_professional_plan_id` FIRST (set only under
    `insurance_mode == "independent"`, where there is no clinic-level
    `tenant_insurance_plans` row to read `charge_deposit` from at all), then
    falls back to the pre-existing `insurance_plan_id` (the clinic's own row,
    catalog-backed or custom - both read the SAME column, since TASK-008
    re-pointed this FK at `tenant_insurance_plans.id`). No plan id at all
    ("Particular", a typed "Outro convênio", no answer, every row older than
    the catalog) -> True: the tenant's own pix_deposit_* policy decides,
    exactly as before the catalog existed.
    """
    professional_plan_id = getattr(appointment, "insurance_professional_plan_id", None)
    if professional_plan_id is not None:
        charge = await session.scalar(
            select(ProfessionalInsurancePlan.charge_deposit).where(
                ProfessionalInsurancePlan.id == professional_plan_id
            )
        )
        return charge is not False
    plan_id = getattr(appointment, "insurance_plan_id", None)
    if plan_id is None:
        return True
    charge = await session.scalar(
        select(TenantInsurancePlan.charge_deposit).where(
            TenantInsurancePlan.id == plan_id,
            TenantInsurancePlan.tenant_id == tenant.id,
        )
    )
    return charge is not False


async def maybe_create_deposit(
    session: AsyncSession,
    *,
    tenant: Tenant,
    patient: Patient | None,
    appointment: Appointment,
    waba_token: str | None,
    collect_payer_data: bool = False,
    redis=None,
    conversation_id: UUID | None = None,
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

    `collect_payer_data` (True only from the `after` hook, decision D7): when the provider says it
    needs a payer field (`payer_document_required` / `payer_email_required`) and none was given,
    raise `payer_data.PayerDataNeeded(kind, provider)` instead of skipping in silence, so the
    caller can ASK the patient. Every other caller keeps the silent skip. `redis` and
    `conversation_id` locate the transient answer (encrypted, 15 minutes): it goes to the provider
    and to nobody else and is forgotten right after that call.
    """
    if not tenant.pix_deposit_enabled:
        _log_deposit_skip(tenant, appointment, DEPOSIT_SKIP_DISABLED)
        return None
    if (tenant.deposit_timing or DEPOSIT_TIMING_AFTER) == DEPOSIT_TIMING_BEFORE:
        # `before` timing charges BEFORE the appointment exists (`booking_charge.py`); the
        # post-booking hook of the same booking must never ask for a second deposit.
        _log_deposit_skip(tenant, appointment, DEPOSIT_SKIP_TIMING_BEFORE)
        return None
    if await get_deposit_for_appointment(session, appointment.id) is not None:
        _log_deposit_skip(tenant, appointment, DEPOSIT_SKIP_ALREADY_CHARGED)
        return None
    if not await _insurance_charges_deposit(session, tenant, appointment):
        _log_deposit_skip(tenant, appointment, DEPOSIT_SKIP_INSURANCE_NO_CHARGE)
        return None
    if patient is None:
        _log_deposit_skip(tenant, appointment, DEPOSIT_SKIP_NO_PATIENT)
        return None

    provider_name = tenant.payment_provider or PROVIDER_ASAAS
    provider = registry.find_provider(provider_name)
    if provider is None:
        _log_deposit_skip(tenant, appointment, DEPOSIT_SKIP_NO_PROVIDER)
        return None
    account = await accounts.load_charge_account(
        session, tenant_id=tenant.id, provider=provider_name, timing="after"
    )
    if account is None:
        _log_deposit_skip(tenant, appointment, DEPOSIT_SKIP_NO_API_KEY)
        return None

    service_name, price_text = await _resolve_service_and_price(session, tenant, appointment)
    price_cents = parse_brl_to_cents(price_text)
    if price_cents is None:
        # Either the stored `appointment_type` names no active service in the
        # effective catalog, or that service carries no readable price. Both
        # mean the amount is UNPROVEN, so no charge is created — the booking
        # itself already happened and is untouched.
        _log_deposit_skip(
            tenant,
            appointment,
            DEPOSIT_SKIP_UNPARSEABLE_PRICE if service_name else DEPOSIT_SKIP_NO_SERVICE,
        )
        return None

    amount_cents = round(price_cents * tenant.pix_deposit_percent / 100)
    if amount_cents <= 0:
        _log_deposit_skip(tenant, appointment, DEPOSIT_SKIP_ZERO_AMOUNT)
        return None

    # The reference we send the provider is the deposit's OWN id (not the appointment's:
    # in `before` timing there is no appointment yet), generated up front so it is inside the
    # charge from the first byte and comes back on every webhook and `get_charge`.
    deposit_id = uuid4()
    customer_id = await accounts.get_customer_id(
        session, tenant_id=tenant.id, patient_id=patient.id, provider=provider_name
    )
    payer = await payer_data.recall_all(
        redis, tenant_id=tenant.id, conversation_id=conversation_id
    )
    description = f"Sinal - {appointment.appointment_type or 'consulta'} - {tenant.clinic_name}"
    try:
        created = await provider.create_pix_charge(
            account,
            ChargeRequest(
                amount_cents=amount_cents,
                external_reference=str(deposit_id),
                description=description[:255],
                due_date=datetime.now(_tenant_tz(tenant)).date(),
                payer_name=patient.name or "Paciente",
                payer_phone=patient.wa_id,
                # Collected by the F2 flow (transiently, decision D7); never stored nor logged.
                provider_customer_id=customer_id,
                payer_document=payer.document,
                payer_email=payer.email,
            ),
        )
    except Exception as exc:
        needed = payer_data.kind_for_code(exc.code) if isinstance(exc, ProviderError) else None
        if collect_payer_data and needed is not None and not payer.has(needed):
            # Nothing was charged and nothing was stored: let the caller ask (what the patient
            # already gave stays for the answer that completes the request).
            raise payer_data.PayerDataNeeded(needed, provider_name) from None
        await payer_data.discard(redis, tenant_id=tenant.id, conversation_id=conversation_id)
        # Closed vocabulary only (reason/stage/status/code_hint are None when the provider did
        # not classify the failure): no free text, no ids, nothing about the payer.
        provider_error = exc if isinstance(exc, ProviderError) else None
        logger.warning(
            "pix_deposit_charge_failed",
            code=provider_error.code if provider_error else type(exc).__name__,
            reason=str(provider_error.reason) if provider_error and provider_error.reason else None,
            stage=provider_error.stage if provider_error else None,
            status=provider_error.status_code if provider_error else None,
            code_hint=provider_error.code_hint if provider_error else None,
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
        )
        return None

    # The provider saw it: forget it (the deposit row and the log never carry it).
    await payer_data.discard(redis, tenant_id=tenant.id, conversation_id=conversation_id)

    if created.provider_customer_id and created.provider_customer_id != customer_id:
        await accounts.remember_customer_id(
            session,
            tenant_id=tenant.id,
            patient_id=patient.id,
            provider=provider_name,
            provider_customer_id=created.provider_customer_id,
        )
    copy_paste = created.copy_paste

    deposit = PixDeposit(
        id=deposit_id,
        tenant_id=tenant.id,
        appointment_id=appointment.id,
        patient_id=patient.id,
        provider=provider_name,
        provider_charge_id=created.provider_charge_id,
        # Dual-written until the "narrow" phase: pre-core readers key on this column.
        asaas_payment_id=(
            created.provider_charge_id if provider_name == PROVIDER_ASAAS else None
        ),
        external_reference=str(deposit_id),
        amount_cents=amount_cents,
        percent_applied=tenant.pix_deposit_percent,
        pix_copy_paste=copy_paste,
        status=PixDepositStatus.AWAITING,
        deposit_timing=DEPOSIT_TIMING_AFTER,
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
) -> str:
    """Void an UNPAID charge at the provider, then mark the deposit EXPIRED.

    Returns "voided", or "payment_in_flight" when the provider says the charge was paid in
    the meantime: a paid charge is NEVER deleted (`cancel_charge` is only for unpaid ones) -
    the deposit stays AWAITING and the webhook / reconciliation settles it (and, the
    consultation being cancelled, opens an attention item). A provider outage or failed void
    also leaves it AWAITING so the reconciliation job can still discover a late payment.
    """
    charge_id = deposit.provider_charge_id or deposit.asaas_payment_id
    provider = registry.find_provider(deposit.provider)
    account = await cfg.get_payment_account(session, tenant.id, deposit.provider)
    if charge_id and provider is not None and account is not None:
        state = None
        try:
            state = await provider.get_charge(account, charge_id)
        except Exception as exc:
            logger.warning(
                "pix_deposit_void_state_unknown",
                error_type=type(exc).__name__,
                tenant_id=str(tenant.id),
                deposit_id=str(deposit.id),
            )
            return "payment_in_flight"
        if state is not None and state.status is PaymentStatus.PAID:
            logger.warning(
                "pix_deposit_void_skipped_paid",
                tenant_id=str(tenant.id),
                deposit_id=str(deposit.id),
            )
            return "payment_in_flight"
        try:
            await provider.cancel_charge(account, charge_id)
        except Exception as exc:
            logger.warning(
                "pix_deposit_void_failed",
                error_type=type(exc).__name__,
                tenant_id=str(tenant.id),
                deposit_id=str(deposit.id),
            )
            return "payment_in_flight"
    else:
        return "payment_in_flight"
    deposit.status = PixDepositStatus.EXPIRED
    deposit.resolved_at = now
    return "voided"


CANCELLED_BY_PATIENT = "patient"
CANCELLED_BY_CLINIC = "clinic"

_CANCELLATION_REASONS = (REASON_REFUND_DUE_POLICY, REASON_PAID_THEN_CANCELLED)


async def _cancellation_already_handled(
    session: AsyncSession, tenant_id: UUID, deposit_id: UUID
) -> bool:
    """True when an item of a cancellation reason already exists for this deposit (a second
    cancel call, from any surface, must never open a second item)."""
    found = await session.scalar(
        select(PaymentAttentionItem.id).where(
            PaymentAttentionItem.tenant_id == tenant_id,
            PaymentAttentionItem.deposit_id == deposit_id,
            PaymentAttentionItem.reason.in_(_CANCELLATION_REASONS),
        )
    )
    return found is not None


async def on_appointment_cancelled(
    session: AsyncSession,
    *,
    tenant: Tenant,
    appointment: Appointment,
    waba_token: str | None = None,
    now: datetime | None = None,
    cancelled_by: str = CANCELLED_BY_PATIENT,
) -> str | None:
    """Resolve a deposit's money outcome for a cancelled appointment. NEVER refunds.

    Returns one of "voided" / "payment_in_flight" / "refund_due" / "partial_refund_due" /
    "retained", or None when there is no deposit, it is already resolved, or this
    cancellation was already handled (a second call is a safe no-op).

    A PAID deposit is never returned by us (decision D15): when the clinic's policy says money
    is due (an early patient cancellation, a partial-retention cancellation, or ANY
    cancellation made by the clinic) the function opens ONE attention item carrying the
    calculated `suggested_refund_cents`, and the clinic returns it in the provider's own panel.
    `waba_token` is accepted for signature symmetry and unused: this function sends NO
    message itself. Does NOT commit - caller's transaction.

    Service-change adjustments (plan 2026-10-26 Task 8): an unpaid difference Pix is voided and
    its adjustment cancelled, an unanswered refund question is closed
    (`adjustment_queries.settle_open_adjustments`); every amount below is what the clinic HOLDS
    (`adjustment_queries.held_total_cents`: sinal + paid differences - differences already asked
    back, which keep their own `service_diff_refund_due` item), never the bare `amount_cents`.
    """
    deposit = await get_deposit_for_appointment(session, appointment.id)
    if deposit is None or deposit.status not in (PixDepositStatus.AWAITING, PixDepositStatus.PAID):
        return None

    now = now or datetime.now(UTC)

    if deposit.status is PixDepositStatus.AWAITING:
        return await _void_awaiting(session, tenant, deposit, now)

    # Before the dedupe below: settling is idempotent, and a difference Pix must never stay
    # payable for a consultation that no longer exists.
    await adjustment_queries.settle_open_adjustments(
        session, tenant=tenant, deposit=deposit, now=now
    )
    if await _cancellation_already_handled(session, tenant.id, deposit.id):
        return None
    held = await adjustment_queries.held_total_cents(session, deposit)

    slot = {
        "slot_start": appointment.start_at,
        "slot_end": appointment.end_at,
        "service_label": appointment.appointment_type,
    }
    charge_ref = deposit.provider_charge_id or deposit.asaas_payment_id or str(deposit.id)

    async def _ask_the_clinic(reason: str, suggested_cents: int) -> None:
        await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason=reason,
            dedupe_key=attention.attention_dedupe_key(deposit.provider, charge_ref, reason),
            deposit=deposit,
            suggested_refund_cents=suggested_cents,
            **slot,
        )

    if cancelled_by == CANCELLED_BY_CLINIC:
        await _ask_the_clinic(REASON_PAID_THEN_CANCELLED, held)
        return "refund_due"

    # PAID, cancelled by the patient who already used every allowed reschedule: the deposit stays
    # with the clinic whatever the refund window says (the patient was told so, see
    # `reschedule_rules_text`). Nothing is due, so nothing is asked of the clinic.
    if at_reschedule_limit(tenant, deposit):
        deposit.status = PixDepositStatus.CANCELLED_RETAINED
        deposit.refunded_amount_cents = 0
        deposit.resolved_at = now
        return "retained"

    # PAID, cancelled by the patient: the refund window decides what the policy says is due.
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
        await _ask_the_clinic(REASON_REFUND_DUE_POLICY, held)
        return "refund_due"

    if tenant.pix_retention_policy == "partial":
        partial_amount = round(held * tenant.pix_partial_refund_percent / 100)
        await _ask_the_clinic(REASON_REFUND_DUE_POLICY, partial_amount)
        return "partial_refund_due"

    # "total": the clinic keeps the whole deposit; nothing is due, nothing to ask.
    deposit.status = PixDepositStatus.CANCELLED_RETAINED
    deposit.refunded_amount_cents = 0
    deposit.resolved_at = now
    return "retained"


def cancellation_notice(outcome: str | None, tenant: Tenant, deposit: PixDeposit) -> str | None:
    """The honest pt-BR line to append after resolving a cancellation's money
    outcome. Callers own the rest of the conversational reply; this returns ONLY the
    money-specific sentence, or None when there is nothing worth telling the patient.

    It never says the money "will be refunded" or that a refund "was requested": the platform
    does not return money - the CLINIC does, manually - so the promise is exactly that.
    The partial amount is computed on what the clinic holds (`adjustment_queries.held_cents_hint`,
    stamped by `on_appointment_cancelled` / `get_deposit_for_appointment`). When the
    cancellation cancelled an unpaid difference Pix of a service change
    (`adjustment_queries.voided_difference_hint`), one more sentence tells the patient not to pay
    it - never a promise about money.
    """
    text = _cancellation_money_line(outcome, tenant, deposit)
    voided = adjustment_queries.voided_difference_hint(deposit)
    if text is None or voided <= 0:
        return text
    return (
        f"{text} O Pix da diferença do sinal ({format_brl(voided)}) foi cancelado: "
        "não pague esse código."
    )


def _cancellation_money_line(
    outcome: str | None, tenant: Tenant, deposit: PixDeposit
) -> str | None:
    if outcome == "refund_due":
        return "A clínica fará a devolução do seu sinal; você será avisado."
    if outcome == "partial_refund_due":
        held = adjustment_queries.held_cents_hint(deposit)
        partial = round(held * tenant.pix_partial_refund_percent / 100)
        return (
            f"Como o cancelamento ficou fora do prazo, a clínica fará a devolução de "
            f"{format_brl(partial)} do sinal; o restante fica retido. Você será avisado."
        )
    if outcome == "retained":
        if used_every_reschedule(tenant, deposit):
            return (
                f"Como você já usou as {tenant.pix_reschedule_limit} remarcações permitidas, "
                "o sinal fica retido pela clínica."
            )
        return (
            f"Como o cancelamento ocorreu com menos de {tenant.pix_refund_window_hours}h de "
            "antecedência, o sinal fica retido pela clínica."
        )
    if outcome == "payment_in_flight":
        return (
            "Recebemos o pagamento do seu sinal agora; a clínica vai conferir e, se for o caso, "
            "fará a devolução. Você será avisado."
        )
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
        return await _void_awaiting(session, tenant, deposit, now)

    deposit.status = PixDepositStatus.NO_SHOW_RETAINED
    deposit.refunded_amount_cents = 0
    deposit.resolved_at = now
    return "retained"


def at_reschedule_limit(tenant: Tenant, deposit: PixDeposit | None) -> bool:
    """Whether a PAID deposit has used every reschedule the clinic allows (limit 0 = no allowance
    configured: it never triggers the retention rule, so a clinic that blocks reschedules does not
    silently keep every cancelled deposit)."""
    return (
        deposit is not None
        and deposit.status is PixDepositStatus.PAID
        and used_every_reschedule(tenant, deposit)
    )


def used_every_reschedule(tenant: Tenant, deposit: PixDeposit) -> bool:
    """Counter only (status-blind): still true after the cancellation moved the deposit on."""
    limit = tenant.pix_reschedule_limit or 0
    return limit > 0 and (deposit.reschedule_count or 0) >= limit


def reschedule_rules_text(tenant: Tenant) -> str:
    """The pt-BR rule the patient is told, in plain words, before any reschedule is spent."""
    limit = tenant.pix_reschedule_limit or 0
    if limit <= 0:
        return "Com o sinal pago não é possível remarcar esta consulta."
    times = "vez" if limit == 1 else "vezes"
    return (
        f"Com o sinal pago você pode remarcar no máximo {limit} {times}. Depois disso não será "
        "possível remarcar, e se você cancelar o sinal fica com a clínica, sem devolução."
    )


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

    patient = await session.get(Patient, deposit.patient_id) if deposit.patient_id else None
    if patient is not None and patient.channel == CHANNEL_BRAIN_MESSAGE:
        await notify.deliver(session, tenant, patient, [notify.Text(text)])
        return
    to = appointment.phone if appointment is not None else None
    if not to and patient is not None:
        to = patient.wa_id
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
    # Best-effort Calendar event delete, reusing the same per-tenant
    # calendar construction the patient-cancel path uses
    # (api/hub/calendar.py::_get_calendar + CalendarService.cancel_event). A
    # clinic without Google books on the secretarIA's own agenda
    # (services/local_calendar.py), so the event is freed there too: skipping it
    # used to be harmless only while every agenda was Google's.
    if appointment.google_event_id:
        try:
            config = await load_tenant_config(session, tenant)
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

    patient = await session.get(Patient, appointment.patient_id) if appointment.patient_id else None
    if patient is not None and patient.channel == CHANNEL_BRAIN_MESSAGE:
        await notify.deliver(
            session,
            tenant,
            patient,
            [notify.Text("O prazo para pagamento do sinal expirou e sua reserva foi liberada.")],
        )
        return
    to = appointment.phone
    if not to and patient is not None:
        to = patient.wa_id
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

    # The money transitions are the payment core's (services/payments/event_processor.py):
    # the provider confirms a PAID event before any state moves, "paid" happens once,
    # OVERDUE/DELETED cancel only a `SCHEDULED` consultation, and no path refunds. This legacy
    # route keeps ITS authentication (the per-tenant token above) and ITS ledger
    # (`ProcessedAsaasEvent`); everything after the claim is the shared core.
    from secretaria.services.payments import event_processor, registry
    from secretaria.services.payments.provider import NormalizedPaymentEvent, PaymentStatus

    account = await cfg.get_payment_account(session, tenant.id, "asaas")
    event = NormalizedPaymentEvent(
        provider="asaas",
        event_id=event_id,
        provider_charge_id=payment_id,
        status=_LEGACY_EVENT_STATUS.get(event_type, PaymentStatus.OTHER),
        event_type=event_type,
    )
    try:
        existing = await session.scalar(
            select(ProcessedAsaasEvent.id).where(ProcessedAsaasEvent.event_id == event_id)
        )
        if existing is not None:
            return "duplicate"
        session.add(ProcessedAsaasEvent(event_id=event_id))

        result = await event_processor.apply_normalized_event(
            session,
            tenant=tenant,
            account=account,
            provider=registry.get_provider("asaas"),
            event=event,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return "duplicate"

    # Best-effort notifications, AFTER commit — never allowed to undo the
    # already-committed state mutation above.
    await event_processor.run_post_commit(session, tenant, result)

    if result.outcome == "paid":
        return "paid"
    if result.outcome in ("expired", "expired_appointment_kept"):
        return "expired"
    return "noop"


# --------------------------------------------------------------------------
# `before` timing copy (TASK-017) - every patient-facing pt-BR line of the deposit-gated
# booking lives here, next to the `after` copy above. NOTHING in this block promises, mentions
# or automates a refund (decision D15): a payment the system cannot honour is handed to the
# clinic (an attention item) and the patient is told the CLINIC will get in touch.
# --------------------------------------------------------------------------

# The taps of the "waiting for your Pix" card. Matched by label on BOTH channels (a Portal card
# id is revalidated against the cards the conversation offered, the label is what always arrives).
LABEL_DEPOSIT_PAID = "Já paguei"
LABEL_DEPOSIT_CHANGE = "Trocar horário"
LABEL_DEPOSIT_CANCEL = "Cancelar reserva"
DEPOSIT_WAIT_BUTTONS: tuple[tuple[str, str], ...] = (
    ("deposit_paid", LABEL_DEPOSIT_PAID),
    ("deposit_change", LABEL_DEPOSIT_CHANGE),
    ("deposit_cancel", LABEL_DEPOSIT_CANCEL),
)


def before_request_text(
    clinic_name: str,
    *,
    service: str,
    when_text: str,
    amount_cents: int,
    minutes: int,
    until_text: str,
    payment_note: str | None = None,
    copy_paste: str | None = None,
) -> str:
    """The Pix request (ONE message): what it is for, how much, until when and, when given, the
    copia-e-cola as its last lines (fenced - see `pix_message`)."""
    lines = [
        f"Para reservar {service} em {when_text} na {clinic_name}, pedimos um sinal de "
        f"{format_brl(amount_cents)} via Pix.",
        f"O horário fica reservado para você por {minutes} minutos (até {until_text}). "
        "A consulta só é confirmada depois que o pagamento for identificado.",
    ]
    if payment_note:
        lines.append(f"💳 Sobre o pagamento do convênio:\n{payment_note}")
    tail = pix_code_tail(copy_paste)
    if tail:
        lines.append("\n".join(tail))
    return "\n\n".join(lines)


def before_wait_card_text(until_text: str) -> str:
    """The body of the card that keeps the conversation while the Pix is open."""
    return (
        f"Estou aguardando o pagamento do Pix até {until_text}. "
        "Assim que ele for identificado, confirmo a sua consulta por aqui."
    )


BEFORE_STILL_WAITING_TEXT = (
    "Ainda não identifiquei o pagamento. Se você já pagou, aguarde um instante: "
    "assim que ele cair, confirmo a sua consulta por aqui."
)
BEFORE_PAYMENT_FOUND_TEXT = "Recebi o seu pagamento! Estou confirmando a sua consulta agora."
BEFORE_RELEASED_TEXT = (
    "Tudo bem, liberei o horário e cancelei o Pix. Quando quiser, é só pedir um novo agendamento."
)
BEFORE_CHANGE_TEXT = "Tudo bem, liberei o horário e cancelei o Pix. Quer escolher outro horário?"
BEFORE_EXPIRED_TEXT = (
    "O prazo do Pix acabou e o horário foi liberado. Quer escolher outro horário?"
)
# Only for what is NOT a technical failure of the deposit (TASK-071 Plan 3): a Pix already open
# for this conversation (`charge_pending`) or no patient to charge (`no_patient`). A technical
# failure books WITHOUT the deposit and tells the clinic (`deposit_failure_notice`).
BEFORE_BLOCKED_TEXT = (
    "Não consegui iniciar o seu agendamento agora. Vou passar a conversa para a equipe da "
    "clínica, que continua o seu atendimento por aqui."
)
# The deposit was dropped (technical failure) but the booking itself could not be written right
# now (database/agenda unreachable): the slot is not claimed as booked, and the fixed "quer que eu
# chame nosso atendente humano?" card follows (owner, 2026-10-29: the patient chooses).
BEFORE_CONFIRM_FAILED_TEXT = (
    "Não consegui concluir o seu agendamento agora, então o horário não ficou marcado."
)
BEFORE_LATE_TAKEN_TEXT = (
    "Recebemos o seu pagamento, mas o horário que você escolheu não está mais disponível. "
    "A clínica vai falar com você para remarcar ou resolver o valor."
)
BEFORE_PROMOTION_STUCK_TEXT = (
    "Recebemos o seu pagamento, mas não conseguimos confirmar a consulta agora. "
    "A clínica foi avisada e vai falar com você."
)


def before_late_reopened_text() -> str:
    """Said BEFORE the confirmation bubble when the payment came after the Pix window but the
    slot was still free, so the reservation was reopened."""
    return "Recebemos o seu pagamento e o horário ainda estava livre, então reabri a sua reserva."


def preview_cancellation_outcome(
    tenant: Tenant,
    deposit: PixDeposit | None,
    appointment: Appointment,
    *,
    now: datetime | None = None,
    cancelled_by: str = CANCELLED_BY_PATIENT,
) -> str | None:
    """What `on_appointment_cancelled` WOULD return for this deposit, with no I/O.

    Mirrors that function's branching one-to-one (same window arithmetic, same
    retention policy, same `cancelled_by` rule) so the hub can show the clinic the money
    consequence BEFORE a slot is released (TASK-032 R4). Since decision D15 (TASK-017) the
    platform NEVER refunds: "refund_due" / "partial_refund_due" mean the clinic returns the
    money itself, in the provider's panel. It cannot predict "payment_in_flight" (the
    provider's answer) nor an already-handled cancellation. Keep the two in step -
    `tests/test_deposit_release_preview.py` runs both on the same data and compares.
    """
    if deposit is None or deposit.status not in (PixDepositStatus.AWAITING, PixDepositStatus.PAID):
        return None
    if deposit.status is PixDepositStatus.AWAITING:
        return "voided"
    if cancelled_by == CANCELLED_BY_CLINIC:
        return "refund_due"
    if at_reschedule_limit(tenant, deposit):
        return "retained"
    now = now or datetime.now(UTC)
    start_at = appointment.start_at
    hours_until = None if start_at is None else (_as_utc(start_at) - now).total_seconds() / 3600
    if hours_until is None or hours_until > tenant.pix_refund_window_hours:
        return "refund_due"
    if tenant.pix_retention_policy == "partial":
        return "partial_refund_due"
    return "retained"


def release_warning_text(outcome: str | None, tenant: Tenant, deposit: PixDeposit) -> str:
    """The pt-BR warning the CLINIC reads before freeing a paid slot (not the patient notice).

    `cancellation_notice` above is the patient-facing sentence; this one is
    addressed to the clinic and always names the amount, because releasing a
    slot whose deposit was paid is the one release that moves money. Like every money line
    since decision D15 it never says the platform refunds: the CLINIC returns what is due,
    in the payment provider's own panel (an attention item is opened with the amount).
    The amount is what the clinic holds (sinal + paid service differences,
    `adjustment_queries.held_cents_hint`).
    """
    held = adjustment_queries.held_cents_hint(deposit)
    paid = f"O paciente já pagou o sinal ({format_brl(held)})."
    if outcome == "retained":
        return (
            f"{paid} Como faltam menos de {tenant.pix_refund_window_hours}h para a consulta, "
            "ao liberar o horário a clínica fica com o sinal."
        )
    if outcome == "partial_refund_due":
        refunded = round(held * tenant.pix_partial_refund_percent / 100)
        return (
            f"{paid} Como faltam menos de {tenant.pix_refund_window_hours}h para a consulta, "
            f"ao liberar o horário a clínica deve devolver {format_brl(refunded)} ao paciente "
            "pelo painel do provedor de pagamento; o restante fica retido pela clínica."
        )
    if outcome == "refund_due":
        return (
            f"{paid} Ao liberar o horário, a clínica deve devolver o valor por inteiro ao "
            "paciente pelo painel do provedor de pagamento."
        )
    return f"{paid} Confira a política de devolução do sinal antes de liberar o horário."
