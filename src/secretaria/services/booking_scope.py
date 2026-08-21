"""Booking scope: WHO owns a booking, WHICH service it is — one rule, everywhere.

Both facts used to be re-derived independently at each booking surface, and
the surfaces disagreed:

  - the deterministic flow (services/flow_router.py) attributed a booking to a
    professional ONLY after an explicit multi-doctor selection, so a clinic
    with a SINGLE active professional — whose catalog, hours and calendar the
    flow had already resolved THROUGH that professional (see
    `workers/tasks.py::_flow_tenant_snapshot` and
    `services/tenant_config.py::load_tenant_config`) — still booked with
    `Appointment.professional_id = NULL`;
  - the LLM's base `create_event` (ai/tools.py) stored the free Google
    Calendar title as `appointment_type` and never set an owner at all.

Everything downstream reads those two columns as authoritative: the Pix
deposit looks the price up by EXACT service name inside the OWNING
professional's catalog (services/payments/deposit_lifecycle.py), reminders and
analytics attribute work by professional. A NULL owner silently sent the price
lookup back to the tenant's legacy catalog — empty on exactly the clinics that
configure everything per professional — and a free-text type matched no entry
at all, so a perfectly configured clinic booked fine and then skipped the
deposit with `pix_deposit_skipped_unparseable_price`.

This module holds those rules as pure functions with NO imports beyond the
standard library, so every layer (`services/`, `ai/`, `plugins/`, `workers/`)
shares them without an import cycle. Nothing here touches the DB, the network
or a session — callers pass the already-loaded roster/catalog in, exactly like
`flow_router.route()` receives its snapshots.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from secretaria.core.whatsapp_limits import truncate_list_row_title

# --------------------------------------------------------------------------
# Topology: how many active professionals this tenant books with
# --------------------------------------------------------------------------
#
# The capability name for a tenant's real shape. It decides which booking
# surface may act tenant-level and which MUST resolve a professional first
# (ai/graph.py's agent tool set, ai/tools.py's defensive guards), and it is
# derived from the SAME active-professionals roster the flow router and the
# prompt already use — never from an entitlement, and never from the prompt.

BOOKING_TOPOLOGY_UNKNOWN = "unknown"
BOOKING_TOPOLOGY_NONE = "none"
BOOKING_TOPOLOGY_SOLE = "sole"
BOOKING_TOPOLOGY_MULTI = "multi"


def booking_topology(professionals: Sequence[Any] | None) -> str:
    """Name this tenant's booking topology from its ACTIVE professionals roster.

    `None` means "the caller could not load the roster" and stays UNKNOWN —
    deliberately distinct from `[]` (a tenant that genuinely has no active
    professional): callers must be able to tell "nobody" from "we don't know",
    since only the first is a fact they may act on.
    """
    if professionals is None:
        return BOOKING_TOPOLOGY_UNKNOWN
    count = len(list(professionals))
    if count == 0:
        return BOOKING_TOPOLOGY_NONE
    if count == 1:
        return BOOKING_TOPOLOGY_SOLE
    return BOOKING_TOPOLOGY_MULTI


def sole_active_professional(professionals: Sequence[Any] | None) -> Any | None:
    """The ONE active professional, or None when there is not exactly one.

    "Exactly one active professional IS the clinic" is the rule
    `load_tenant_config` already applies to hours/services/calendar/credential
    (contract v1 §10 item D). Named once here so the effective CATALOG
    (`workers/tasks.py::_flow_tenant_snapshot`) and the effective OWNER
    (`resolve_booking_owner_id` below) can never be resolved from different
    professionals.
    """
    roster = list(professionals or [])
    return roster[0] if len(roster) == 1 else None


def resolve_booking_owner_id(
    professionals: Sequence[Any] | None,
    selected_professional_id: UUID | None = None,
) -> UUID | None:
    """The `Appointment.professional_id` a booking made NOW must carry, or None.

    ONE rule for every booking surface:

    - an explicit selection wins, but ONLY when it names somebody on the
      ACTIVE roster — a stale, deactivated or model-suggested id resolves to
      None, never to "some other doctor";
    - with no selection, EXACTLY ONE active professional is the owner: it is
      the only unambiguous answer, and it is already whose catalog/hours/
      calendar the booking used;
    - zero or 2+ active professionals with no valid selection -> None. An
      owner is never invented, and never guessed from the roster's order.
    """
    roster = list(professionals or [])
    if selected_professional_id is not None:
        for professional in roster:
            if getattr(professional, "id", None) == selected_professional_id:
                return selected_professional_id
        return None
    professional = sole_active_professional(roster)
    return getattr(professional, "id", None) if professional is not None else None


# --------------------------------------------------------------------------
# Service catalog: the canonical name of a bookable service
# --------------------------------------------------------------------------


def service_entry_name(entry: Any) -> str:
    """Name of one catalog entry, whichever shape it travels in.

    The effective catalog crosses layers in two shapes: the stored dicts
    (`tenant_config.active_appointment_types` / `professional_appointment_types`,
    used by the flow router and the Pix price lookup) and
    `RuntimeAppointmentType` objects (`TenantRuntimeConfig.appointment_types`,
    what the agent's prompt and tools hold).
    """
    if isinstance(entry, dict):
        return str(entry.get("name") or "")
    return str(getattr(entry, "name", "") or "")


def service_names(services: Sequence[Any] | None) -> list[str]:
    """Every non-empty name in `services`, in catalog order."""
    return [name for name in (service_entry_name(e) for e in (services or [])) if name]


def canonical_service_name(services: Sequence[Any] | None, candidate: str | None) -> str | None:
    """The catalog's OWN spelling of the service `candidate` refers to, or None.

    Case- and whitespace-insensitive. The 24-character prefix comparison is
    the WhatsApp list-row contract (`send_list` caps row titles at 24, so a
    tap echoes the truncated title, cut by `truncate_list_row_title` — the SAME
    helper the row was RENDERED with, so the two can never drift) — harmless
    for the non-tap callers, and it
    keeps a tapped row and a typed name resolving to the same entry.

    None means "this clinic has no such active service". Callers must treat
    that as unproven and fail closed rather than storing the raw candidate:
    `Appointment.appointment_type` is read as a catalog key downstream, not as
    free text.
    """
    target = _norm(candidate)
    if not target:
        return None
    for entry in services or []:
        name = service_entry_name(entry)
        if not name:
            continue
        if _norm(name) == target or _norm(truncate_list_row_title(name)) == target:
            return name
    return None


def _norm(text: str | None) -> str:
    return (text or "").strip().casefold()
