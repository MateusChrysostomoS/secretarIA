"""post_booking dispatch: react to a freshly committed appointment, off the hot path.

Wires `plugins/base.py`'s `post_booking` extension point (declared since the
addon plugins existed, but never called — see `PluginSpec`'s docstring) to
BOTH booking commit points: `ai/tools.py:_persist_appointment` (the agent
path) and `workers/tasks.py:_apply_flow_result` (the deterministic-flow
path). Neither commit path runs plugin hooks inline — a slow or misbehaving
hook (an EHR push, a Pix send) must never delay the patient's booking
confirmation. Instead each commit site calls `enqueue_post_booking_hooks`
right after its OWN transaction has already committed successfully — a
fire-and-forget arq enqueue, not an await of the hook itself.

The job body lives HERE (not workers/tasks.py), mirroring the addon-plugin
split already established by `plugins/reminders.py` (see that module's
docstring): `workers/arq_worker.py` just imports and registers
`run_post_booking_hooks` as an arq job function.

Fail-open at every layer:
  - `enqueue_post_booking_hooks` swallows a missing/unreachable arq pool
    (no Redis reachable — e.g. unit tests, or a dev script that invokes the
    agent directly without one) and logs instead of raising. The booking
    itself is NEVER blocked, delayed, or rolled back by this.
  - `run_post_booking_hooks` (the job itself) does ONE `get_entitlements`
    read, then delegates to `registry.run_post_booking`, which wraps EACH
    plugin's hook in its own try/except — one failing hook never stops a
    later one from running.
"""

from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Patient, Tenant
from secretaria.plugins.base import PostBookingContext
from secretaria.plugins.registry import run_post_booking
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.tenant_config import get_waba_token

logger = get_logger(__name__)


async def enqueue_post_booking_hooks(
    redis, tenant_id: UUID, appointment_id: UUID, source: str
) -> None:
    """Fire-and-forget enqueue of `run_post_booking_hooks`. Never raises.

    `redis` is the arq Redis pool — `ctx["redis"]` inside an arq job, or
    `app.state.arq_pool` from the API process; both are `ArqRedis` instances
    exposing the same `enqueue_job`. `redis=None` (no pool reachable — e.g.
    unit tests, or a dev script invoking the agent directly) is a deliberate
    silent no-op logged at INFO: the booking already succeeded, and
    post_booking hooks are best-effort extras, never a precondition for a
    successful booking reply.
    """
    if redis is None:
        logger.info(
            "post_booking_enqueue_skipped_no_redis",
            tenant_id=str(tenant_id),
            appointment_id=str(appointment_id),
        )
        return
    try:
        await redis.enqueue_job(
            "run_post_booking_hooks", str(tenant_id), str(appointment_id), source
        )
    except Exception as exc:
        logger.warning(
            "post_booking_enqueue_failed",
            error=str(exc),
            tenant_id=str(tenant_id),
            appointment_id=str(appointment_id),
        )


async def run_post_booking_hooks(
    ctx: dict, tenant_id: str, appointment_id: str, source: str = "agent"
) -> None:
    """arq job: run every entitled plugin's `post_booking` hook for one appointment.

    Registered in `workers/arq_worker.py`. Loads the tenant + appointment (+
    patient, when the appointment has one) fresh — the enqueueing call site
    only hands over ids — reads entitlements ONCE, then fans out via
    `registry.run_post_booking` (no short-circuit; every entitled plugin with
    a hook runs, each independently wrapped in its own try/except by the
    registry).
    """
    redis = ctx.get("redis")
    async with async_session_factory() as session:
        appointment = await session.get(Appointment, UUID(appointment_id))
        if appointment is None:
            logger.warning("post_booking_appointment_not_found", appointment_id=appointment_id)
            return
        tenant = await session.get(Tenant, UUID(tenant_id))
        if tenant is None:
            logger.warning("post_booking_tenant_not_found", tenant_id=tenant_id)
            return
        patient = (
            await session.get(Patient, appointment.patient_id)
            if appointment.patient_id is not None
            else None
        )
        # Decrypted inside the session (single seam) — see services/tenant_config.py.
        waba_token = await get_waba_token(session, tenant.id)

    summary = await get_entitlements(tenant.id, redis)
    if summary is None:
        logger.info("post_booking_skipped_unresolvable_entitlements", tenant_id=tenant_id)
        return

    await run_post_booking(
        summary,
        PostBookingContext(
            tenant=tenant,
            patient=patient,
            appointment=appointment,
            waba_token=waba_token,
            source=source,  # type: ignore[arg-type]
        ),
    )
