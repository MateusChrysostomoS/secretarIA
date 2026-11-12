"""Backfill: link every tenant's professionals to their shared per-person agenda (TASK-083).

For each tenant that has professionals it asks brain-api (`professional-accounts`, ONE call per
tenant) which account each professional is, and points professionals that are the same person at
the same `agendas` row (services/agenda_linking.py). Run it once after deploying the migration and
brain-api's route, and again whenever a person starts working at a second clinic.

Idempotent: a second run changes nothing. A brain-api failure for a tenant links nothing for that
tenant and is not an error. `--dry-run` does the same work and rolls it back, so it prints what
WOULD change and writes nothing.

Counts and tenant ids only: no professional name, account id or patient is ever printed.

Usage:
    uv run python scripts/reconcile_agendas.py --dry-run
    uv run python scripts/reconcile_agendas.py
"""

import argparse
import asyncio

from secretaria.core.database import async_session_factory, engine
from secretaria.core.logging import setup_logging
from secretaria.services.agenda_linking import reconcile_all_tenants


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Compute and print the changes, write nothing"
    )
    args = parser.parse_args()
    setup_logging()
    try:
        results = await reconcile_all_tenants(async_session_factory, dry_run=args.dry_run)
    finally:
        await engine.dispose()

    mode = "DRY RUN (nothing written)" if args.dry_run else "APPLIED"
    print(f"reconcile_agendas: {mode}")
    for tenant_id, result in results.items():
        note = "" if result.brain_ok else "  <- brain-api unavailable, nothing changed"
        print(
            f"tenant {tenant_id}  linked={result.linked} shared={result.shared} "
            f"changed={result.changed}{note}"
        )
    print(f"tenants processed: {len(results)}  changed: {sum(r.changed for r in results.values())}")


if __name__ == "__main__":
    asyncio.run(main())
