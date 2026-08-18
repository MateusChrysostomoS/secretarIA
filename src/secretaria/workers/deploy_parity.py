"""arq cron: the recurring deploy-divergence alarm (FIX_01 §5.2).

`on_startup` in `workers/arq_worker.py` already compares this worker against
the API — but a startup check only fires when THIS process restarts. The
failure mode that caused the 2026-08-16 incident is the opposite one: the
worker keeps running happily while somebody deploys the API alone. Without a
recurring check that divergence sits unnoticed until the next restart, which
may be days.

Kept in its own module (not `arq_worker.py`, which is the registry, and not
`tasks.py`, which is the WhatsApp pipeline) following the split already used by
`payments_tasks.py` and `onboarding_cron.py`.
"""

from secretaria.core.build_info import (
    ParityVerdict,
    build_identity,
    check_deploy_parity,
    publish_build_identity,
)


async def check_deploy_parity_cron(ctx: dict) -> ParityVerdict:
    """Re-announce this worker's build identity and alarm if the API differs.

    Re-publishing on every tick (rather than only at startup) also keeps this
    worker's entry authoritative if Redis was flushed under it, so the API's
    `GET /build` does not go permanently blind between worker restarts.

    Returns the verdict so it lands in arq's own job-result log; the alarm
    itself is the `deploy_sha_divergence` WARNING emitted by
    `check_deploy_parity`.
    """
    identity = build_identity("worker")
    redis = ctx.get("redis")
    await publish_build_identity(redis, identity)
    return await check_deploy_parity(redis, identity)
