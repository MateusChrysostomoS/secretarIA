"""pix_deposit plugin: ask for a Pix deposit right after a booking (post_booking).

CORE capability of SecretarIA (`entitlement_keys=()`), NOT an addon — decision
of the owner, 2026-10-28 (docs/CHECKPOINT_sinal_padrao.md). It used to be gated
by the `pix_deposit` entitlement, which silently skipped the deposit for every
clinic that had connected Asaas but never bought the addon. What gates a real
charge now is only the clinic's own setup, checked inside
`maybe_create_deposit` (services/payments/deposit_lifecycle.py): deposits
switched on (`tenants.pix_deposit_enabled`), a priced service, and a connected
payment account. The registry still respects `summary.active` (an inactive
subscription runs no plugin at all).

Hook contract: `registry.run_post_booking` (plugins/post_booking.py) calls
`_post_booking` with a DETACHED `PostBookingContext` — the session that
loaded `ctx.tenant`/`ctx.patient`/`ctx.appointment` has already closed by the
time this hook runs (see `PostBookingContext`'s docstring). Money-lifecycle
work needs a live, session-attached row (`maybe_create_deposit` mutates
`patient.asaas_customer_id` and adds a `PixDeposit` row), so this hook opens
its OWN session and RE-FETCHES all three by id before calling into
`deposit_lifecycle`, mirroring workers/payments_tasks.py's
`process_asaas_event` and every other fire-and-forget arq-adjacent hook in
this codebase. `ctx.patient is None` (e.g. a block slot created via the
doctor hub, which has no patient to charge) is a silent no-op, logged at
DEBUG — never an error. `maybe_create_deposit` itself never raises (every
failure mode is a logged, silent no-op — see its own docstring), but this
hook stays defensive on top since `registry.run_post_booking` wraps each
hook's call in its own try/except anyway (belt AND suspenders: a hook must
never be the one plugin that breaks a later sibling's turn in that sweep).
"""

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, Tenant
from secretaria.plugins.base import PluginSpec, PostBookingContext
from secretaria.plugins.registry import register
from secretaria.services.payments import deposit_lifecycle, notify, payer_data, payer_data_after

logger = get_logger(__name__)


async def _post_booking(ctx: PostBookingContext) -> None:
    if ctx.patient is None:
        logger.debug("pix_deposit_post_booking_skipped_no_patient", tenant_id=str(ctx.tenant.id))
        return

    try:
        async with async_session_factory() as session:
            tenant = await session.get(Tenant, ctx.tenant.id)
            patient = await session.get(Patient, ctx.patient.id)
            appointment = await session.get(Appointment, ctx.appointment.id)
            if tenant is None or patient is None or appointment is None:
                logger.warning(
                    "pix_deposit_post_booking_row_missing",
                    tenant_id=str(ctx.tenant.id),
                    appointment_id=str(ctx.appointment.id),
                )
                return

            conversation_id = await notify.conversation_id_for(session, tenant.id, patient.id)
            try:
                await deposit_lifecycle.maybe_create_deposit(
                    session,
                    tenant=tenant,
                    patient=patient,
                    appointment=appointment,
                    waba_token=ctx.waba_token,
                    # Decision D7, `after` timing: the provider may need a payer field. Ask for it
                    # right before the Pix instead of skipping the Pix in silence.
                    collect_payer_data=True,
                    redis=ctx.redis,
                    conversation_id=conversation_id,
                )
            except payer_data.PayerDataNeeded as need:
                await payer_data_after.ask_after_booking(
                    session,
                    tenant=tenant,
                    patient=patient,
                    appointment=appointment,
                    kind=need.kind,
                    provider=need.provider,
                    waba_token=ctx.waba_token,
                    redis=ctx.redis,
                    conversation_id=conversation_id,
                )
            await session.commit()
    except Exception as exc:
        logger.warning(
            "pix_deposit_post_booking_failed",
            error=str(exc),
            tenant_id=str(ctx.tenant.id),
            appointment_id=str(ctx.appointment.id),
        )


PIX_DEPOSIT_SPEC = PluginSpec(id="pix_deposit", entitlement_keys=(), post_booking=_post_booking)
register(PIX_DEPOSIT_SPEC)
