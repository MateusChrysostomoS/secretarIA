"""The arq job of D-R14: the clinic changed a price; tell the patients whose sinal it changes.

TASK-085 R, 3rd round (tasks/TASK-085/sinal-trocas/mapa-u.md §2.2, §4.5). Enqueued by
`services/payments/price_change.py::enqueue_sweep` after the commit of a price edit in the hub,
under `_job_id = price_change.sweep_job_id(tenant, changed_at)` (one job per clinic per 5-minute
window, run after the window closed). The job only parses its arguments and calls
`price_change.sweep_price_changes(tenant_id, changed_at=, now=, redis=ctx["redis"])`, which owns
every rule; one clinic per job, never another clinic's rows.

Registered in `workers/arq_worker.py::WorkerSettings.functions` (the function name IS the job
name: `price_change.SWEEP_JOB`). Layering: workers -> services. Logs: ids and counts only.
"""

from datetime import UTC, datetime
from uuid import UUID

from secretaria.core.logging import get_logger
from secretaria.services.payments import price_change

logger = get_logger(__name__)


async def sweep_price_changes(ctx: dict, tenant_id: str, changed_at: str) -> dict:
    """One clinic's sweep. `tenant_id` is the clinic's uuid as text, `changed_at` the ISO instant
    of the edit (timezone-aware). Returns counts for arq's job log (e.g. {"sent": n}); never
    raises for one consultation's error (the service logs and goes on). Bad arguments (not a
    uuid, not an instant, no timezone) -> {"sent": 0, "error": "bad_arguments"}, never a retry."""
    try:
        clinic = UUID(tenant_id)
        changed = datetime.fromisoformat(changed_at)
        if changed.tzinfo is None:
            raise ValueError("changed_at must be timezone-aware")
    except (TypeError, ValueError):
        logger.warning("price_change_sweep_bad_arguments")
        return {"sent": 0, "error": "bad_arguments"}
    sent = await price_change.sweep_price_changes(
        clinic, changed_at=changed, now=datetime.now(UTC), redis=ctx.get("redis")
    )
    return {"sent": sent}
