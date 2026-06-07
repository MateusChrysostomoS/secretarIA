"""Dev shortcut: store the Fase A GOOGLE_REFRESH_TOKEN as the tenant's
encrypted Calendar credential — the exact same row the hosted OAuth callback
writes — so the /tenants/me/calendar/* endpoints work WITHOUT standing up the
full Google OAuth web client.

Use this only in development. In production the refresh token only ever enters
the system through GET /oauth/google/callback.

Usage:
    uv run python scripts/seed_calendar_credential.py
    uv run python scripts/seed_calendar_credential.py <tenant_id>
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


async def main() -> None:
    setup_logging()
    settings = get_settings()
    token = settings.GOOGLE_REFRESH_TOKEN
    if not token:
        raise SystemExit(
            "GOOGLE_REFRESH_TOKEN is empty in .env. "
            "Generate one first with: uv run python scripts/gcal_auth.py"
        )
    if not settings.ENCRYPTION_KEY:
        raise SystemExit(
            "ENCRYPTION_KEY is empty in .env. Generate one with: "
            'uv run python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )

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
            await set_google_refresh_token(session, tenant.id, token)
        logger.info("seed_calendar_credential_done", tenant_id=str(tenant.id))
        print(f"Calendar refresh token stored (encrypted) for tenant {tenant.id}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
