"""The canonical service catalog: identity, resolution and lookup.

`models/service.py` gives a service a stable id at the CLINIC level. This
module is how the rest of the system uses it:

  - `normalize` defines identity. Two names that normalize to the same string
    ARE the same service; `Service.normalized_name` is unique per tenant, so
    the duplicate cannot be written in the first place.
  - `resolve_entries` turns the per-professional JSON list into entries whose
    NAME and descriptive copy come from the catalog, while `price`,
    `duration_min` and "does this professional offer it" stay per professional.
    Every existing consumer (the deterministic flow, the prompt, scoped_help,
    the Pix price lookup) keeps receiving exactly the dict shape it already
    reads — resolution changes where the values come from, not what they are.
  - `professionals_offering` answers "who else offers this service?" by id
    instead of by string, which is what makes a doctor-swap flow possible.

Rollout, in one rule: **id first, normalized name second, raw string never.**
An entry that already carries `service_id` resolves by id. An entry that does
not (every entry, until its tenant is backfilled) is matched to the catalog by
normalized name. An entry that matches nothing is passed through exactly as it
is today, so a tenant with no catalog yet behaves as if this module did not
exist. That is what lets the table ship before the backfill.

Pure functions take the already-loaded rows; only `load_service_catalog`
touches a session (CLAUDE.md's layering rule).
"""

import re
import unicodedata
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models.service import Service

# The optional key added to each per-professional / per-tenant JSON entry that
# points at a canonical `services` row. Absent on every entry until that tenant
# is backfilled — resolution falls back to the normalized name, so nothing
# breaks in the meantime.
SERVICE_ID_KEY = "service_id"

# Fields the CATALOG owns once an entry is linked. `description` /
# `long_description` / `requirements` used to live on each professional's copy
# of the service, which is why a professional save that omitted them blanked
# them; now they have exactly one home.
_CATALOG_OWNED_FIELDS = ("description", "long_description", "requirements")

_WHITESPACE = re.compile(r"\s+")


def normalize(name: str | None) -> str:
    """The identity key for a service name.

    Trim, collapse internal whitespace, strip accents, casefold. "Limpeza",
    " limpeza " and "LIMPEZA" are one service; "Limpeza" and "Limpeza Dental"
    are two, and this function will never claim otherwise — telling those apart
    is a human's call (see `find_near_duplicates`, which only WARNS).
    """
    text = _WHITESPACE.sub(" ", (name or "").strip())
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return without_accents.casefold()


def find_near_duplicates(candidate: str | None, existing: Sequence[Any]) -> list[str]:
    """Existing service names suspiciously close to `candidate`. ADVISORY ONLY.

    Nothing in this codebase merges on this signal — it exists so the hub can
    ask "did you mean 'Limpeza Dental'?" before a second, near-identical
    service is created, and so the backfill report can flag groups a human
    should look at. Two names are "close" when one starts with the other (the
    real-world shape of the problem: "Limpeza" vs "Limpeza Dental"), excluding
    the exact match, which is not a near-duplicate but the same service.
    """
    target = normalize(candidate)
    if not target:
        return []
    out: list[str] = []
    for item in existing:
        name = item if isinstance(item, str) else getattr(item, "name", "")
        other = normalize(name)
        if not other or other == target:
            continue
        if other.startswith(target) or target.startswith(other):
            out.append(name)
    return out


def entry_service_id(entry: dict) -> UUID | None:
    """The canonical service this JSON entry points at, or None.

    Tolerant on purpose: the value round-trips through JSON as a string, and a
    malformed one must read as "not linked" rather than explode a booking.
    """
    raw = (entry or {}).get(SERVICE_ID_KEY)
    if raw is None:
        return None
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _index(services: Sequence[Service] | None) -> tuple[dict[UUID, Service], dict[str, Service]]:
    by_id: dict[UUID, Service] = {}
    by_name: dict[str, Service] = {}
    for service in services or []:
        by_id[service.id] = service
        key = service.normalized_name or normalize(service.name)
        # First wins: `normalized_name` is unique per tenant, so a collision
        # here would mean the caller mixed two tenants' catalogs — never
        # silently prefer the later one.
        by_name.setdefault(key, service)
    return by_id, by_name


def match_service(services: Sequence[Service] | None, entry: dict) -> Service | None:
    """The catalog row this entry refers to: by id first, by name second."""
    by_id, by_name = _index(services)
    service_id = entry_service_id(entry)
    if service_id is not None and service_id in by_id:
        return by_id[service_id]
    return by_name.get(normalize(entry.get("name")))


def service_by_name(services: Sequence[Service] | None, name: str | None) -> Service | None:
    """The catalog row whose canonical name normalizes to `name`, or None."""
    _by_id, by_name = _index(services)
    return by_name.get(normalize(name))


def resolve_entries(
    entries: Sequence[dict] | None, services: Sequence[Service] | None
) -> list[dict]:
    """Overlay the canonical catalog onto per-professional JSON entries.

    Returns NEW dicts of exactly the shape every current consumer already
    reads — same keys, same types — so nothing downstream needs to know this
    happened. For a linked entry:

      - `name` and the descriptive fields come from the CATALOG (one spelling
        per clinic, and a description that a professional save cannot blank);
      - `price`, `duration_min`, `is_active`, `sort_order` stay the
        PROFESSIONAL's (they legitimately differ between doctors);
      - `service_id` is stamped on the result even when the entry was matched
        by name, so callers downstream of resolution can group by identity
        without repeating the match.

    An entry the catalog does not know is returned unchanged — that is the
    pre-backfill world, and it must keep working. A `services=None` catalog
    short-circuits to exactly today's behaviour.

    A CLINIC-inactive service is dropped entirely: the clinic no longer offers
    it, so no professional does either. (A professional who does not offer an
    active service keeps expressing that through the entry's own `is_active`,
    which this function does not touch.)
    """
    if not services:
        return [dict(entry) for entry in (entries or []) if isinstance(entry, dict)]

    resolved: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        service = match_service(services, entry)
        if service is None:
            resolved.append(dict(entry))
            continue
        if not service.is_active:
            continue
        merged = dict(entry)
        merged["name"] = service.name
        merged[SERVICE_ID_KEY] = str(service.id)
        for field in _CATALOG_OWNED_FIELDS:
            value = getattr(service, field, None)
            merged[field] = list(value or []) if field == "requirements" else value
        resolved.append(merged)
    return resolved


def professionals_offering(
    service: Service | str | None,
    professionals: Sequence[Any] | None,
    tenant: Any,
    services: Sequence[Service] | None = None,
) -> list[Any]:
    """Which of `professionals` actually offer `service`. Order preserved.

    `service` may be a catalog row or a name — a name is resolved through the
    catalog first, never compared raw. Returns `[]` when nobody offers it,
    which is a real answer the caller must handle ("no other doctor does
    this"), not an error.

    A professional offers a service when their EFFECTIVE catalog (their own
    entries, or the tenant's legacy list when they have none — the same
    fallback `professional_appointment_types` applies) contains an ACTIVE entry
    resolving to it.
    """
    target = service if isinstance(service, Service) else service_by_name(services, service)
    if target is not None:
        target_key = target.normalized_name or normalize(target.name)
    else:
        target_key = normalize(service if isinstance(service, str) else None)
    if not target_key:
        return []

    out: list[Any] = []
    for professional in professionals or []:
        entries = getattr(professional, "appointment_types", None) or getattr(
            tenant, "appointment_types", None
        )
        for entry in resolve_entries(entries, services):
            if not entry.get("is_active", True):
                continue
            if normalize(entry.get("name")) == target_key:
                out.append(professional)
                break
    return out


async def load_service_catalog(session: AsyncSession, tenant_id: UUID) -> list[Service]:
    """Every catalog row for one tenant, ordered the way a clinic reads it.

    Includes INACTIVE rows: resolution needs them to know a linked entry
    points at a service the clinic retired (and must therefore be dropped),
    which it could not tell apart from a dangling reference otherwise.
    """
    rows = await session.scalars(
        select(Service)
        .where(Service.tenant_id == tenant_id)
        .order_by(Service.sort_order, Service.name)
    )
    return list(rows)
