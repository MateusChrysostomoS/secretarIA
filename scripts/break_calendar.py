"""Dev/test: store an INVALID Google Calendar refresh token for a tenant so the
next booking attempt exercises the outage path (patient notice + human handover
+ owner alert email).

Why not just NULL the token? Nulling yields either a "credentials missing"
RuntimeError (env GOOGLE_REFRESH_TOKEN empty) or a silent fall back to that env
token (when it is set) — neither raises CalendarUnavailableError, so NO alert
email is sent. This script stores a properly Fernet-encrypted but bogus token;
Google rejects it on refresh with invalid_grant -> RefreshError ->
CalendarUnavailableError, which is exactly what triggers the alert.

Use this only in development/testing.

Usage:
    uv run python scripts/break_calendar.py
    uv run python scripts/break_calendar.py <tenant_id>
"""

import asyncio
import sys
from uuid import UUID

from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory, engine
from secretaria.core.logging import get_logger, setup_logging
from secretaria.models import Tenant
from secretaria.services.tenant_config import set_google_refresh_token

logger = get_logger(__name__)

# Non-empty (so it clears the "credentials missing" guard) but guaranteed to be
# rejected by Google's token endpoint with invalid_grant.
BOGUS_TOKEN = "1//INVALID-REVOKED-REFRESH-TOKEN-FOR-TESTING"


async def main() -> None:
    setup_logging()
    settings = get_settings()
    if not settings.ENCRYPTION_KEY:
        raise SystemExit("ENCRYPTION_KEY is empty; cannot encrypt the test token.")

    wanted_id = sys.argv[1] if len(sys.argv) > 1 else None
    async with async_session_factory() as session:
        async with session.begin():
            if wanted_id:
                tenant = await session.get(Tenant, UUID(wanted_id))
            else:
                rows = (await session.scalars(select(Tenant).limit(2))).all()
                if len(rows) != 1:
                    raise SystemExit(
                        f"Expected exactly one tenant, found {len(rows)}. "
                        "Pass a tenant_id explicitly."
                    )
                tenant = rows[0]
            if tenant is None:
                raise SystemExit("Tenant not found.")
            await set_google_refresh_token(session, tenant.id, BOGUS_TOKEN)
        logger.info("break_calendar_done", tenant_id=str(tenant.id))
        print(
            f"Stored an INVALID Calendar token for tenant {tenant.id}.\n"
            "The next booking attempt will raise CalendarUnavailableError and "
            "(if SMTP_* + contact_email are set) send the alert email — once per "
            "CALENDAR_ALERT_SILENCE_SECONDS (default 4h) per tenant."
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
