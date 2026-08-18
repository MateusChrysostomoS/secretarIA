"""READ-ONLY audit: NULL versus EMPTY on every professional's own config.

`Professional.business_hours` / `Professional.appointment_types` have three
states, and `services/tenant_config.py`'s resolvers now distinguish all three:
NULL inherits the tenant's legacy column, `{}` / `[]` is an own override that
inherits nothing, non-empty is an own override with content. Before that change
the resolvers tested truthiness, so NULL and EMPTY behaved identically — which
means any EMPTY row that exists today was written under the old semantics and
is about to start meaning something different.

This script exists to size that, exactly once, before anyone decides whether a
backfill is warranted. It answers one question: how many rows would change
behaviour, and in which direction?

It only LOOKS. No UPDATE, no INSERT, no write transaction, no DDL. Turning an
EMPTY into a NULL (or the reverse) changes what a real clinic's WhatsApp offers
patients, so it is an operational decision with its own authorisation, dry-run
and rollback — never a side effect of a deploy or of running a report.

WHAT IT PRINTS — aggregate counts only:
  professionals_total / professionals_active
  hours_null / hours_empty / hours_set        (and the same for services)
  active_would_lose_hours     ACTIVE rows whose own hours are EMPTY while the
                              tenant's legacy column is NON-empty. These are the
                              only rows whose patient-facing behaviour actually
                              changes: they used to serve the clinic's hours and
                              now serve none. Everything else already behaved
                              the way the new resolvers behave.
  active_would_lose_services  same, for appointment_types.

PRIVACY, deliberately: this prints NO tenant id, NO professional id, NO clinic
name, NO weekday, NO service name and NO price — only counts. An audit artifact
gets pasted into tickets and chat logs, so it must not carry a clinic's
configuration or anything that identifies one. `--per-tenant` widens it to
per-tenant COUNTS (still no ids, still no values), for judging whether the
affected rows are concentrated in one clinic or spread thin.

Usage:
    uv run python scripts/audit_professional_config_nullness.py
    uv run python scripts/audit_professional_config_nullness.py --per-tenant
"""

import argparse
import asyncio
from dataclasses import dataclass, field

from sqlalchemy import select

from secretaria.core.database import async_session_factory, engine
from secretaria.core.logging import setup_logging
from secretaria.models import Professional, Tenant
from secretaria.services.tenant_config import (
    professional_inherits_appointment_types,
    professional_inherits_business_hours,
)


@dataclass
class Counts:
    """Pure tallies. Nothing here can hold an id, a name or a config value."""

    professionals_total: int = 0
    professionals_active: int = 0
    hours_null: int = 0
    hours_empty: int = 0
    hours_set: int = 0
    services_null: int = 0
    services_empty: int = 0
    services_set: int = 0
    active_would_lose_hours: int = 0
    active_would_lose_services: int = 0

    def as_lines(self) -> list[str]:
        return [
            f"  professionals_total        : {self.professionals_total}",
            f"  professionals_active       : {self.professionals_active}",
            f"  hours_null (inherits)      : {self.hours_null}",
            f"  hours_empty (own, empty)   : {self.hours_empty}",
            f"  hours_set (own, content)   : {self.hours_set}",
            f"  services_null (inherits)   : {self.services_null}",
            f"  services_empty (own, empty): {self.services_empty}",
            f"  services_set (own, content): {self.services_set}",
            f"  active_would_lose_hours    : {self.active_would_lose_hours}",
            f"  active_would_lose_services : {self.active_would_lose_services}",
        ]


@dataclass
class TenantBucket:
    """Per-tenant counts, numbered positionally so no tenant id is ever printed."""

    counts: Counts = field(default_factory=Counts)


def _classify(counts: Counts, professional: Professional, tenant: Tenant) -> None:
    counts.professionals_total += 1
    active = bool(professional.is_active)
    if active:
        counts.professionals_active += 1

    # --- hours ---
    if professional_inherits_business_hours(professional):
        counts.hours_null += 1
    elif not professional.business_hours:
        counts.hours_empty += 1
        # Only a NON-empty tenant column means the old truthiness fallback was
        # actually serving something this row will now stop serving.
        if active and tenant.business_hours:
            counts.active_would_lose_hours += 1
    else:
        counts.hours_set += 1

    # --- services ---
    if professional_inherits_appointment_types(professional):
        counts.services_null += 1
    elif not professional.appointment_types:
        counts.services_empty += 1
        if active and tenant.appointment_types:
            counts.active_would_lose_services += 1
    else:
        counts.services_set += 1


async def _audit(per_tenant: bool) -> None:
    overall = Counts()
    buckets: list[TenantBucket] = []

    async with async_session_factory() as session:
        tenants = list(await session.scalars(select(Tenant)))
        for tenant in tenants:
            bucket = TenantBucket()
            rows = await session.scalars(
                select(Professional).where(Professional.tenant_id == tenant.id)
            )
            for professional in rows:
                _classify(overall, professional, tenant)
                if per_tenant:
                    _classify(bucket.counts, professional, tenant)
            if per_tenant and bucket.counts.professionals_total:
                buckets.append(bucket)

    print("professional config nullness - READ ONLY, nothing was written")
    print(f"tenants scanned: {len(tenants)}")
    print("\nTOTAL")
    for line in overall.as_lines():
        print(line)

    if per_tenant:
        # Numbered, not identified: enough to see concentration, not enough to
        # point at a clinic.
        print("\nPER TENANT (anonymous, ordered arbitrarily)")
        for index, bucket in enumerate(buckets, start=1):
            print(f"\n tenant #{index}")
            for line in bucket.counts.as_lines():
                print(" " + line)

    affected = overall.active_would_lose_hours + overall.active_would_lose_services
    print(
        "\nverdict: "
        + (
            "no active professional changes behaviour - no backfill is warranted."
            if affected == 0
            else f"{affected} active row(s) change behaviour. A backfill MAY be warranted; "
            "it needs its own authorisation, dry-run and rollback plan."
        )
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--per-tenant",
        action="store_true",
        help="Also print anonymous per-tenant counts (no ids, no values)",
    )
    args = parser.parse_args()
    setup_logging()
    try:
        await _audit(args.per_tenant)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
