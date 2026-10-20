"""arq job: push a clinic's future secretarIA-agenda events to Google after it connects.

Enqueued by the OAuth callback (api/hub/oauth.py) after every successful connection -
the clinic's account or one professional's - with the tenant id (and the professional's,
for the log). The work itself is services/calendar_sync.py::push_future_local_events,
which resolves which agendas a credential now covers; this module only retries it.

Retries: a Google outage (`CalendarUnavailableError`) is retried with a growing delay;
a rejected credential (`GoogleTokenRevokedError`, the clinic must reconnect - and that
reconnection enqueues this job again) and per-event failures are not. Re-running is
always safe: a pushed event is never pushed twice.
"""

from uuid import UUID

from arq import Retry

from secretaria.core.logging import get_logger
from secretaria.services.calendar_sync import push_future_local_events

logger = get_logger(__name__)

# arq's own default `max_tries` is 5; stop asking a little before it gives up for us.
_MAX_TRIES = 4
_RETRY_BASE_SECONDS = 60


async def sync_local_calendar_to_google(
    ctx: dict, tenant_id: str, professional_id: str | None = None
) -> dict:
    """Run the connect-time sync for `tenant_id`; the summary is the job's result."""
    summary = await push_future_local_events(UUID(tenant_id))
    job_try = int(ctx.get("job_try") or 1)
    if summary.unavailable and not summary.reconnect_required and job_try < _MAX_TRIES:
        logger.info(
            "calendar_local_sync_retry",
            tenant_id=tenant_id,
            professional_id=professional_id,
            job_try=job_try,
        )
        raise Retry(defer=_RETRY_BASE_SECONDS * job_try)
    return summary.as_dict()
