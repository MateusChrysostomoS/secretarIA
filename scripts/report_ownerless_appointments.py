"""READ-ONLY report: future appointments saved without a professional owner.

Bookings made before `services/booking_scope.py` existed could reach the DB
with `professional_id = NULL` even on a clinic with a single active
professional — the flow only attributed an owner after an explicit multi-doctor
selection, and the LLM's base `create_event` never set one at all. Those rows
still price their Pix deposit against the tenant's LEGACY catalog, which is
empty on every clinic that configures services per professional.

This script only LOOKS. It runs no UPDATE, opens no write transaction and
creates nothing: deciding whether to backfill an existing row is an
operational call (it changes who a past appointment is attributed to, and
therefore analytics and usage metering), so it belongs in a separate, audited,
reversible procedure — not in a deploy.

For each tenant it prints:
  - future_ownerless      rows with no owner, starting from now on;
  - resolvable_to_sole    how many of those belong to a clinic that has
                          EXACTLY ONE active professional today, i.e. the
                          unambiguous candidates a backfill could fix;
  - unpriceable_type      of those, how many carry an `appointment_type` that
                          no longer names an active service in the effective
                          catalog (a free Calendar title, or a renamed/removed
                          service) — these need a human decision, never an
                          automatic one.

Ids only: no patient name, phone or clinical text is ever printed.

Usage:
    uv run python scripts/report_ownerless_appointments.py
    uv run python scripts/report_ownerless_appointments.py --tenant <uuid>
"""

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from secretaria.core.database import async_session_factory, engine
from secretaria.core.logging import setup_logging
from secretaria.models import Appointment, Professional, Tenant, is_live_status
from secretaria.services.booking_scope import canonical_service_name, sole_active_professional
from secretaria.services.tenant_config import (
    active_appointment_types,
    professional_appointment_types,
)


async def _report(tenant_filter: UUID | None) -> int:
    now = datetime.now(UTC)
    printed = 0
    async with async_session_factory() as session:
        tenants = list(
            await session.scalars(
                select(Tenant).where(Tenant.id == tenant_filter)
                if tenant_filter is not None
                else select(Tenant)
            )
        )
        for tenant in tenants:
            professionals = list(
                await session.scalars(
                    select(Professional).where(
                        Professional.tenant_id == tenant.id,
                        Professional.is_active.is_(True),
                    )
                )
            )
            rows = list(
                await session.scalars(
                    select(Appointment).where(
                        Appointment.tenant_id == tenant.id,
                        Appointment.professional_id.is_(None),
                        Appointment.start_at >= now,
                    )
                )
            )
            # Cancelled/no-show rows are tombstones: nothing left to attribute.
            live = [row for row in rows if is_live_status(row.status)]
            if not live:
                continue

            sole = sole_active_professional(professionals)
            catalog = (
                professional_appointment_types(sole, tenant)
                if sole is not None
                else active_appointment_types(tenant)
            )
            unpriceable = {
                row.id
                for row in live
                if canonical_service_name(catalog, row.appointment_type) is None
            }

            printed += 1
            print(f"\ntenant {tenant.id}  ({tenant.clinic_name})")
            print(f"  active_professionals : {len(professionals)}")
            print(f"  future_ownerless     : {len(live)}")
            print(f"  resolvable_to_sole   : {len(live) if sole is not None else 0}")
            print(f"  unpriceable_type     : {len(unpriceable)}")
            if sole is not None:
                print(f"  sole_professional_id : {sole.id}")
            for row in sorted(live, key=lambda r: r.start_at):
                flag = "  <- type not in catalog" if row.id in unpriceable else ""
                print(f"    appointment {row.id}  {row.start_at.isoformat()}{flag}")
    if printed == 0:
        print("No future ownerless appointments found.")
    return printed


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="Report a single tenant id", default=None)
    args = parser.parse_args()
    setup_logging()
    try:
        await _report(UUID(args.tenant) if args.tenant else None)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
