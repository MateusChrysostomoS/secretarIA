"""Populate the canonical service catalog from the existing JSON entries.

DELIVERY 2 of three (see migrations/versions/d1c2b3a4e5f6_service_catalog.py
and docs/CHECKPOINT_service_catalog.md). The migration created an empty table;
this fills it, one tenant at a time, on purpose — never as part of a deploy.

What it does, per tenant:

  1. Scan `tenants.appointment_types` and every `professionals.appointment_types`.
  2. Group entries by NORMALIZED name (trim, collapse whitespace, strip
     accents, casefold — services/service_catalog.py::normalize). Two entries
     in one group are the same service by definition; that is what
     normalization means.
  3. Create ONE `services` row per group, keeping the MOST FREQUENT raw
     spelling as the canonical name and the first non-empty description /
     long_description / requirements found anywhere in the group — so no copy
     any professional had written is lost.
  4. Stamp `service_id` onto every JSON entry it grouped. The entries keep
     every field they already have: nothing is removed until delivery 3.

What it deliberately does NOT do:

  - It never merges names that merely LOOK alike. "Limpeza" and "Limpeza
    Dental" normalize differently, so they stay two services, and the report
    only flags them for a human to look at. Guessing there would silently
    destroy a real distinction.
  - It never touches `appointments`. Historical rows keep the free text that
    was stored at booking time and are resolved by normalized name on READ
    (services/payments/deposit_lifecycle.py) — rewriting history to fit a new
    model is not reversible and not ours to do.
  - It refuses to consolidate a tenant whose groups contain MORE THAN ONE
    spelling until a human says so (`--accept-variants`). Those groups are
    exactly the ones worth eyeballing before they collapse into one name.

Report mode (the default) opens no write transaction whatsoever.

Usage:
    uv run python scripts/backfill_service_catalog.py                  # report all
    uv run python scripts/backfill_service_catalog.py --tenant <uuid>  # report one
    uv run python scripts/backfill_service_catalog.py --tenant <uuid> --apply
    uv run python scripts/backfill_service_catalog.py --tenant <uuid> --apply \
        --accept-variants
"""

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import select

from secretaria.core.database import async_session_factory, engine
from secretaria.core.logging import setup_logging
from secretaria.models import Professional, Tenant
from secretaria.models.service import Service
from secretaria.services.service_catalog import (
    SERVICE_ID_KEY,
    find_near_duplicates,
    normalize,
)


@dataclass
class _Group:
    """All entries across one tenant that normalize to the same service name."""

    normalized: str
    spellings: Counter = field(default_factory=Counter)
    description: str | None = None
    long_description: str | None = None
    requirements: list | None = None
    # Where the entries live, so `--apply` can stamp them: (owner, index).
    # `owner` is the Tenant row or a Professional row.
    locations: list = field(default_factory=list)

    @property
    def canonical_name(self) -> str:
        """The most frequent spelling; ties break on first-seen order."""
        return self.spellings.most_common(1)[0][0]

    @property
    def has_variants(self) -> bool:
        return len(self.spellings) > 1


def collect_groups(tenant: Tenant, professionals: list[Professional]) -> dict[str, _Group]:
    """Group every stored entry for one tenant by normalized name.

    Order matters and is deliberate: the tenant's legacy list first, then
    professionals by name, so "first non-empty description wins" and "ties
    break on first seen" are deterministic across runs.
    """
    groups: dict[str, _Group] = {}
    owners: list = [tenant, *sorted(professionals, key=lambda p: (p.name or "", str(p.id)))]
    for owner in owners:
        for index, entry in enumerate(owner.appointment_types or []):
            if not isinstance(entry, dict):
                continue
            raw_name = str(entry.get("name") or "").strip()
            key = normalize(raw_name)
            if not key:
                continue
            group = groups.setdefault(key, _Group(normalized=key))
            group.spellings[raw_name] += 1
            group.locations.append((owner, index))
            if not group.description and entry.get("description"):
                group.description = entry["description"]
            if not group.long_description and entry.get("long_description"):
                group.long_description = entry["long_description"]
            if not group.requirements and entry.get("requirements"):
                group.requirements = list(entry["requirements"])
    return groups


def look_alike_pairs(groups: dict[str, _Group]) -> list[tuple[str, str]]:
    """Distinct services whose names resemble each other. NEVER merged.

    Advisory output for a human: these normalize differently, so the system
    treats them as different services and always will. Only a person can say
    whether the clinic meant them to be one.
    """
    names = [group.canonical_name for group in groups.values()]
    seen: set[frozenset[str]] = set()
    pairs: list[tuple[str, str]] = []
    for name in names:
        for other in find_near_duplicates(name, [n for n in names if n != name]):
            key = frozenset({name, other})
            if key in seen:
                continue
            seen.add(key)
            pairs.append((name, other))
    return pairs


def _print_report(tenant: Tenant, groups: dict[str, _Group]) -> int:
    """Print one tenant's groups. Returns how many groups have >1 spelling."""
    print(f"\ntenant {tenant.id}  ({tenant.clinic_name})")
    if not groups:
        print("  no service entries found")
        return 0

    variants = 0
    for group in groups.values():
        total = sum(group.spellings.values())
        places = len({id(owner) for owner, _ in group.locations})
        if group.has_variants:
            variants += 1
            spellings = ", ".join(f"{name!r}x{count}" for name, count in group.spellings.items())
            print(
                f"  [VARIANTS] -> {group.canonical_name!r}"
                f"  ({total} entries across {places} owners): {spellings}"
            )
        else:
            print(f"  {group.canonical_name!r}  ({total} entries across {places} owners)")

    for name, other in look_alike_pairs(groups):
        print(f"  [LOOK-ALIKE] {name!r} vs {other!r} — kept SEPARATE, review by hand")
    return variants


async def apply_groups(session, tenant: Tenant, groups: dict[str, _Group]) -> int:
    """Create the catalog rows and stamp `service_id`. Idempotent.

    Re-running finds the existing row by normalized name and only fills in
    entries that are not stamped yet, so a partially-applied tenant converges
    instead of duplicating.
    """
    existing = {
        row.normalized_name: row
        for row in await session.scalars(select(Service).where(Service.tenant_id == tenant.id))
    }
    created = 0
    for sort_order, (key, group) in enumerate(groups.items()):
        service = existing.get(key)
        if service is None:
            service = Service(
                id=uuid4(),
                tenant_id=tenant.id,
                name=group.canonical_name,
                normalized_name=key,
                description=group.description,
                long_description=group.long_description,
                requirements=group.requirements or [],
                is_active=True,
                sort_order=sort_order,
            )
            session.add(service)
            created += 1

        for owner, index in group.locations:
            # Re-assign the whole list: SQLAlchemy does not track in-place
            # mutation of a JSON column, so mutating the dict alone would be
            # silently dropped on flush.
            entries = list(owner.appointment_types or [])
            if index >= len(entries) or not isinstance(entries[index], dict):
                continue
            entry = dict(entries[index])
            if entry.get(SERVICE_ID_KEY) == str(service.id):
                continue
            entry[SERVICE_ID_KEY] = str(service.id)
            entries[index] = entry
            owner.appointment_types = entries
    return created


async def _run(tenant_filter: UUID | None, apply: bool, accept_variants: bool) -> None:
    async with async_session_factory() as session:
        tenants = list(
            await session.scalars(
                select(Tenant).where(Tenant.id == tenant_filter)
                if tenant_filter is not None
                else select(Tenant)
            )
        )
        total_variants = 0
        for tenant in tenants:
            professionals = list(
                await session.scalars(
                    select(Professional).where(Professional.tenant_id == tenant.id)
                )
            )
            groups = collect_groups(tenant, professionals)
            total_variants += _print_report(tenant, groups)

            if not apply:
                continue
            if any(group.has_variants for group in groups.values()) and not accept_variants:
                print(
                    "  SKIPPED: group(s) above have more than one spelling. "
                    "Review them, then re-run with --accept-variants."
                )
                continue
            created = await apply_groups(session, tenant, groups)
            await session.commit()
            print(f"  applied: {created} service(s) created, {len(groups)} group(s) linked")

        if not apply:
            print(
                f"\nReport only — nothing was written. {total_variants} group(s) with "
                f"more than one spelling across {len(tenants)} tenant(s)."
            )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=None, help="Restrict to one tenant id")
    parser.add_argument(
        "--apply", action="store_true", help="Write the catalog (default is report-only)"
    )
    parser.add_argument(
        "--accept-variants",
        action="store_true",
        help="Consolidate groups that have more than one spelling (review the report first)",
    )
    args = parser.parse_args()
    setup_logging()
    try:
        await _run(UUID(args.tenant) if args.tenant else None, args.apply, args.accept_variants)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
