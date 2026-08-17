"""Tests for services/booking_scope.py — the shared owner/service resolver.

Pure functions (no DB, no network, no config): every booking surface — the
deterministic flow router, the agent's base tools, the professional-aware
plugin tools and the Pix price lookup — resolves WHO owns a booking and WHICH
service it is through these, so they are tested once, here, and asserted
end-to-end in tests/test_booking_owner_persistence.py.
"""

import os
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
    booking_topology,
    canonical_service_name,
    resolve_booking_owner_id,
    service_entry_name,
    service_names,
    sole_active_professional,
)


def _professional(name="Dra. Ana"):
    return SimpleNamespace(id=uuid4(), name=name)


# --------------------------------------------------------------------------
# Topology
# --------------------------------------------------------------------------


def test_topology_none_is_distinct_from_unknown():
    # `[]` is a fact ("this clinic has no active professional"); `None` means
    # the caller could not load the roster. Only the first may be acted on.
    assert booking_topology([]) == BOOKING_TOPOLOGY_NONE
    assert booking_topology(None) == BOOKING_TOPOLOGY_UNKNOWN


def test_topology_counts_the_roster():
    assert booking_topology([_professional()]) == BOOKING_TOPOLOGY_SOLE
    assert booking_topology([_professional("A"), _professional("B")]) == BOOKING_TOPOLOGY_MULTI
    assert booking_topology([_professional(str(i)) for i in range(5)]) == BOOKING_TOPOLOGY_MULTI


# --------------------------------------------------------------------------
# sole_active_professional
# --------------------------------------------------------------------------


def test_sole_active_professional_only_when_exactly_one():
    ana = _professional()
    assert sole_active_professional([ana]) is ana
    assert sole_active_professional([]) is None
    assert sole_active_professional(None) is None
    assert sole_active_professional([ana, _professional("Dr. Bruno")]) is None


# --------------------------------------------------------------------------
# resolve_booking_owner_id
# --------------------------------------------------------------------------


def test_owner_is_the_single_active_professional_without_any_selection():
    """The regression this module exists for: one professional IS the owner."""
    ana = _professional()
    assert resolve_booking_owner_id([ana]) == ana.id


def test_no_owner_when_the_clinic_has_no_active_professional():
    assert resolve_booking_owner_id([]) is None
    assert resolve_booking_owner_id(None) is None


def test_no_owner_is_invented_for_several_professionals():
    ana, bruno = _professional("Dra. Ana"), _professional("Dr. Bruno")
    # Never "the first one" — an ambiguous roster with no pick has no owner.
    assert resolve_booking_owner_id([ana, bruno]) is None


def test_valid_selection_wins_over_the_roster():
    ana, bruno = _professional("Dra. Ana"), _professional("Dr. Bruno")
    assert resolve_booking_owner_id([ana, bruno], bruno.id) == bruno.id


def test_selection_is_validated_against_the_active_roster():
    """A stale/deactivated/model-suggested id resolves to None, never somebody else."""
    ana = _professional()
    assert resolve_booking_owner_id([ana], uuid4()) is None
    assert resolve_booking_owner_id([], uuid4()) is None
    assert resolve_booking_owner_id(None, uuid4()) is None


def test_single_professional_selection_still_resolves_to_that_professional():
    ana = _professional()
    assert resolve_booking_owner_id([ana], ana.id) == ana.id


# --------------------------------------------------------------------------
# Service names
# --------------------------------------------------------------------------


def _runtime_type(name):
    """A RuntimeAppointmentType-shaped object (attribute access, not a dict)."""
    return SimpleNamespace(name=name, duration_min=30, description=None)


def test_service_entry_name_handles_both_catalog_shapes():
    assert service_entry_name({"name": "Consulta"}) == "Consulta"
    assert service_entry_name(_runtime_type("Consulta")) == "Consulta"
    assert service_entry_name({"name": None}) == ""
    assert service_entry_name({}) == ""


def test_service_names_skips_empty_entries():
    catalog = [{"name": "Consulta"}, {"name": ""}, {"name": "Retorno"}]
    assert service_names(catalog) == ["Consulta", "Retorno"]
    assert service_names(None) == []


def test_canonical_service_name_returns_the_catalogs_own_spelling():
    catalog = [{"name": "Primeira Consulta"}, {"name": "Retorno"}]
    assert canonical_service_name(catalog, "  primeira consulta ") == "Primeira Consulta"
    assert canonical_service_name(catalog, "RETORNO") == "Retorno"


def test_canonical_service_name_matches_the_whatsapp_row_truncation():
    # send_list caps row titles at 24 chars, so a tap echoes the truncated
    # title — it must still resolve to the full catalog name.
    name = "Consulta de Acompanhamento Prolongada"
    assert canonical_service_name([{"name": name}], name[:24]) == name


def test_canonical_service_name_works_on_runtime_appointment_types():
    catalog = [_runtime_type("Primeira Consulta")]
    assert canonical_service_name(catalog, "primeira consulta") == "Primeira Consulta"


def test_canonical_service_name_is_none_for_anything_unproven():
    catalog = [{"name": "Primeira Consulta"}]
    # A free Calendar title is exactly what used to be stored as the type.
    assert canonical_service_name(catalog, "Consulta - João Silva") is None
    assert canonical_service_name(catalog, "") is None
    assert canonical_service_name(catalog, None) is None
    assert canonical_service_name([], "Primeira Consulta") is None
