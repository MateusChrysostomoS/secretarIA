"""The arq job of D-R14: the clinic changed a price; tell the patients whose sinal it changes.

TASK-085 R, 3rd round (tasks/TASK-085/sinal-trocas/mapa-u.md §2.2, §4.5). Enqueued by
`services/payments/price_change.py::enqueue_sweep` after the commit of a price edit in the hub,
under `_job_id = price_change.sweep_job_id(tenant, changed_at)` (one job per clinic per 5-minute
window) and `_defer_by = price_change.SWEEP_DEFER`. The job only parses its arguments and calls
`price_change.sweep_price_changes(tenant_id, changed_at=, now=, redis=ctx["redis"])`, which owns
every rule; one clinic per job, never another clinic's rows.

U0 (contract slice) froze the signature; UB1 implements it and registers it in
`workers/arq_worker.py::WorkerSettings.functions` (the function name IS the job name:
`price_change.SWEEP_JOB`). Layering: workers -> services. Logs: ids and counts only.
"""

_UB1 = "TASK-085 R UB1"


async def sweep_price_changes(ctx: dict, tenant_id: str, changed_at: str) -> dict:
    """One clinic's sweep. `tenant_id` is the clinic's uuid as text, `changed_at` the ISO instant
    of the edit (timezone-aware). Returns counts for arq's job log (e.g. {"sent": n}); never
    raises for one consultation's error (the service logs and goes on)."""
    raise NotImplementedError(_UB1)
