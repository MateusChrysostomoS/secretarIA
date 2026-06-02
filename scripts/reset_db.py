"""Wipe dynamic data from the database for clean testing.

By default truncates the conversation tables (messages, conversations,
patients, processed_events) and keeps tenants — the tenant row carries
WhatsApp credentials and almost never needs to be re-seeded between test
sessions.

Pass --tenants to also wipe the tenants table; the next inbound webhook
will auto-provision a fresh tenant from environment settings.

Refuses to run without --confirm. There is NO undo.

The same logic is exposed in Swagger at POST /admin/reset (guarded by
the X-Admin-Token header). Both call into `services.admin.wipe_data` so
they can never drift.

Usage:
    uv run python scripts/reset_db.py --confirm
    uv run python scripts/reset_db.py --confirm --tenants
"""

import argparse
import asyncio

from secretaria.core.database import engine
from secretaria.services.admin import (
    DEFAULT_WIPE_TABLES,
    TENANT_TABLE,
    wipe_data,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required. Acknowledges that this destroys data.",
    )
    parser.add_argument(
        "--tenants",
        action="store_true",
        help="Also truncate the tenants table.",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    if not args.confirm:
        targets = ", ".join(DEFAULT_WIPE_TABLES) + (
            f", {TENANT_TABLE}" if args.tenants else ""
        )
        print(
            "Refusing without --confirm. This would TRUNCATE: "
            f"{targets}. Re-run with --confirm to proceed."
        )
        return
    wiped = await wipe_data(include_tenants=args.tenants)
    print(f"Wiped: {', '.join(wiped)}")
    # Close the engine cleanly so the script exits without pending
    # asyncpg sockets emitting "Event loop is closed" tracebacks.
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
