"""How big a clinic's catalogue may grow (TASK-082 S4, CONTRACT.md §e).

The owner's numbers (2026-11-12): 40 services per doctor, 120 per clinic, 60 convênios
per clinic. Enforced where a save happens - the hub routes and
services/hub_configuration.py - not in a Pydantic schema, because the rule needs the
STORED count: "no growth past the limit". A save is refused only when the resulting
count is above the limit AND above what is stored, so a clinic that is already above a
limit (it existed before the limit did) can still load, edit, reorder and delete; it
just cannot add. Reads are never limited.

Lives in `core/` because it imports nothing (the layering rule of CLAUDE.md).
"""

from __future__ import annotations

SERVICES_PER_PROFESSIONAL = 40
SERVICES_PER_CLINIC = 120
INSURANCE_PLANS_PER_CLINIC = 60

CATALOG_LIMIT_EXCEEDED = "catalog_limit_exceeded"


class CatalogLimitExceeded(Exception):
    """A save that would grow a catalogue past its limit. The route answers 422."""

    def __init__(self, limit: int, count: int) -> None:
        super().__init__(f"{count} > {limit}")
        self.limit = limit
        self.count = count

    def detail(self) -> dict:
        """The 422 body's `detail` (CONTRACT.md §e)."""
        return {"code": CATALOG_LIMIT_EXCEEDED, "limit": self.limit, "count": self.count}


def check_growth(*, limit: int, before: int, after: int) -> None:
    """Raise `CatalogLimitExceeded` when `after` is above `limit` AND above `before`."""
    if after > limit and after > before:
        raise CatalogLimitExceeded(limit, after)


def entry_count(entries: object) -> int:
    """How many entries a stored/patched JSON list holds (None or a non-list = 0)."""
    return len(entries) if isinstance(entries, list) else 0
