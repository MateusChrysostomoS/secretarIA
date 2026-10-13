"""arq 0.28 rejects a frozenset in a cron field (`_get_next_dt` raises RuntimeError), which
crashed the worker at start-up: the Pix expiry cron used a frozenset (TASK-016)."""

from arq.cron import next_cron

from secretaria.workers.arq_worker import WorkerSettings


def test_every_cron_job_can_compute_its_next_run():
    from datetime import UTC, datetime

    now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    for job in WorkerSettings.cron_jobs:
        next_cron(
            now,
            month=job.month,
            day=job.day,
            weekday=job.weekday,
            hour=job.hour,
            minute=job.minute,
            second=job.second,
            microsecond=job.microsecond,
        )
