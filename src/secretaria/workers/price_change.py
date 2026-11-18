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

from datetime import UTC, datetime, timedelta
from uuid import UUID

from secretaria.core.logging import get_logger
from secretaria.services.payments import price_change

logger = get_logger(__name__)

# The arq timeout of this job (registered with `arq.worker.func(..., timeout=JOB_TIMEOUT)`; arq's
# default is 300 s and a timed-out job is NOT retried). A large clinic's sweep costs a brain-api
# lookup plus an SMTP send per consultation that needs a notice (~1-2 s each, up to the SMTP 15 s
# timeout on a bad one): the sweep stops itself at `price_change._SWEEP_BUDGET` (25 min, room for
# ~750+ e-mails) and enqueues a "resume" follow-up, so this only has to cover the budget plus ONE
# slow consultation and the final enqueue - 30 min does, with margin.
JOB_TIMEOUT = timedelta(minutes=30)


async def sweep_price_changes(
    ctx: dict, tenant_id: str, changed_at: str, after: str | None = None
) -> dict:
    """One clinic's sweep. `tenant_id` is the clinic's uuid as text, `changed_at` the ISO instant
    of the edit (timezone-aware). Returns counts for arq's job log (e.g. {"sent": n}); never
    raises for one consultation's error (the service logs and goes on). `after` (fix round 2):
    the cursor a "resume" follow-up carries (`price_change.format_cursor`), so it goes on from
    where the previous run stopped. Bad arguments (not a uuid, not an instant, no timezone, not a
    cursor) -> {"sent": 0, "error": "bad_arguments"}, never a retry."""
    try:
        clinic = UUID(tenant_id)
        changed = datetime.fromisoformat(changed_at)
        if changed.tzinfo is None:
            raise ValueError("changed_at must be timezone-aware")
        cursor = price_change.parse_cursor(after) if after is not None else None
    except (TypeError, ValueError):
        logger.warning("price_change_sweep_bad_arguments")
        return {"sent": 0, "error": "bad_arguments"}
    sent = await price_change.sweep_price_changes(
        clinic, changed_at=changed, now=datetime.now(UTC), redis=ctx.get("redis"), after=cursor
    )
    return {"sent": sent}
