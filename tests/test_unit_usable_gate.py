"""TASK-090 Adendo 1 §A - the secretária only works with a USABLE unit (active + complete address).

The REAL rule (`@pytest.mark.real_unit_gate`; the suite default in conftest treats every active
unit as usable and never gates a reply):

* `professional_units.unit_is_usable` - the one predicate (same test as creating a unit);
* patient-facing readers - the flow's unit options, the AI unit tools, the greeting region -
  only list usable units; the manager's views keep every unit;
* `workers/shared/unit_gate.reply_blocked_no_usable_unit` - silence without a usable unit,
  logged with ids only, and FAILS OPEN on a check error.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.ai import tools as ai_tools
from secretaria.core import database as core_database
from secretaria.models import Unit
from secretaria.models.professional_unit import ProfessionalUnit
from secretaria.plugins import multi_unit as mu
from secretaria.services import professional_units, unit_booking
from secretaria.services.clinic_region import load_clinic_region
from secretaria.workers.shared import unit_gate
from tests._local_agenda import seed_professional, seed_tenant, wire_local_agenda
from tests._reminder_fixtures import db  # noqa: F401 - fixture

pytestmark = pytest.mark.real_unit_gate

COMPLETE = {
    "cep": "13010100",
    "street": "Rua Barão de Jaguara",
    "number": "100",
    "city": "Campinas",
    "state": "SP",
}


@pytest.fixture(autouse=True)
def _wired(db, monkeypatch):  # noqa: F811
    wire_local_agenda(monkeypatch, db)
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(unit_gate, "async_session_factory", db)


async def _unit(db, tenant, name, *, active=True, **parts):  # noqa: F811
    async with db() as session:
        unit = Unit(id=uuid4(), tenant_id=tenant.id, name=name, is_active=active, **parts)
        session.add(unit)
        await session.commit()
        return unit


async def _link(db, professional, unit, *, sort_order=0):  # noqa: F811
    async with db() as session:
        session.add(
            ProfessionalUnit(
                professional_id=professional.id,
                unit_id=unit.id,
                business_hours=None,
                is_active=True,
                sort_order=sort_order,
            )
        )
        await session.commit()


# ----------------------------------------------------------------- the predicate


def _row(**overrides) -> SimpleNamespace:
    return SimpleNamespace(**{"is_active": True, **COMPLETE, **overrides})


def test_an_active_unit_with_a_complete_address_is_usable() -> None:
    assert professional_units.unit_is_usable(_row())
    # Complement and neighborhood are optional; a masked CEP is the same 8 digits.
    assert professional_units.unit_is_usable(_row(cep="13010-100", neighborhood=None))


@pytest.mark.parametrize(
    "overrides",
    [
        {"cep": None},
        {"cep": "1301010"},
        {"street": "  "},
        {"number": None},
        {"city": None},
        {"state": "XX"},
        {"is_active": False},
    ],
    ids=["no-cep", "short-cep", "blank-street", "no-number", "no-city", "bad-uf", "inactive"],
)
def test_a_missing_part_or_an_inactive_unit_is_not_usable(overrides) -> None:
    assert not professional_units.unit_is_usable(_row(**overrides))


def test_a_legacy_text_only_unit_is_not_usable() -> None:
    legacy = SimpleNamespace(is_active=True, address="Rua A, 1 - Campinas")
    assert not professional_units.unit_is_usable(legacy)


async def test_has_usable_units_is_per_tenant(db) -> None:  # noqa: F811
    tenant = await seed_tenant(db)
    other = await seed_tenant(db, name="Outra")
    await _unit(db, tenant, "Incompleta", street="Rua A", city="Campinas", state="SP")
    await _unit(db, tenant, "Desativada", active=False, **COMPLETE)
    await _unit(db, other, "Completa", **COMPLETE)
    async with db() as session:
        assert await professional_units.has_usable_units(session, tenant.id) is False
        assert await professional_units.has_usable_units(session, other.id) is True
        assert [u.name for u in await professional_units.usable_units(session, other.id)] == [
            "Completa"
        ]


# ------------------------------------------------------------ patient-facing readers


async def test_the_flow_offers_only_usable_units_the_manager_sees_all(db) -> None:  # noqa: F811
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    sede = await _unit(db, tenant, "Sede", **COMPLETE)
    nova = await _unit(db, tenant, "Nova", street="Rua B", city="Campinas", state="SP")
    await _link(db, doctor, sede, sort_order=0)
    await _link(db, doctor, nova, sort_order=1)

    async with db() as session:
        patient = await unit_booking.unit_options_for(session, tenant, [doctor])
        manager = await unit_booking.unit_options_for(
            session, tenant, [doctor], usable_only=False
        )
        staff_ids = await professional_units.available_unit_ids(session, doctor)

    assert [o.name for o in patient[doctor.id]] == ["Sede"]
    assert [o.name for o in manager[doctor.id]] == ["Sede", "Nova"]
    # The staff view of the doctor's units is unchanged (hub, staff edits).
    assert staff_ids == [sede.id, nova.id]


async def test_the_list_units_tool_lists_only_usable_units(db) -> None:  # noqa: F811
    tenant = await seed_tenant(db)
    await _unit(db, tenant, "Sede", **COMPLETE)
    await _unit(db, tenant, "Nova", street="Rua B", city="Campinas", state="SP")
    token = ai_tools._tenant_id_ctx.set(tenant.id)
    try:
        result = await mu.list_units.ainvoke({})
        _unit_row, error = await mu._resolve_unit_or_error(tenant.id, "Nova")
    finally:
        ai_tools._tenant_id_ctx.reset(token)
    assert [u["name"] for u in result["units"]] == ["Sede"]
    # Naming the incomplete unit is refused like an unknown one, listing only the usable ones.
    assert _unit_row is None
    assert "Sede" in error["error"] and "Unidades disponíveis: Sede." in error["error"]


async def test_the_greeting_region_ignores_incomplete_units(db) -> None:  # noqa: F811
    tenant = await seed_tenant(db)
    await _unit(db, tenant, "Sede", **COMPLETE)
    # Another state, address incomplete: it would widen "em Campinas" to "no Brasil".
    await _unit(db, tenant, "Recife", city="Recife", state="PE")
    async with db() as session:
        region = await load_clinic_region(session, tenant)
    assert region is not None and (region.level, region.name) == ("city", "Campinas")


# ------------------------------------------------------------------- the reply gate


def _capture(monkeypatch) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []

    def _warning(event, **kwargs):
        seen.append((event, kwargs))

    monkeypatch.setattr(unit_gate.logger, "warning", _warning)
    return seen


async def test_the_gate_blocks_a_clinic_without_a_usable_unit(db, monkeypatch) -> None:  # noqa: F811
    tenant = await seed_tenant(db)
    await _unit(db, tenant, "Incompleta", street="Rua A", number="1", city="Campinas", state="SP")
    seen = _capture(monkeypatch)
    conversation_id = uuid4()

    assert await unit_gate.reply_blocked_no_usable_unit(
        tenant.id, conversation_id=conversation_id
    )
    assert seen == [
        (
            "bot_reply_suppressed_no_complete_unit",
            {"tenant_id": str(tenant.id), "conversation_id": str(conversation_id)},
        )
    ]


async def test_the_gate_lets_a_clinic_with_a_usable_unit_through(db, monkeypatch) -> None:  # noqa: F811
    tenant = await seed_tenant(db)
    await _unit(db, tenant, "Incompleta", street="Rua A", city="Campinas", state="SP")
    await _unit(db, tenant, "Sede", **COMPLETE)
    seen = _capture(monkeypatch)
    assert not await unit_gate.reply_blocked_no_usable_unit(tenant.id)
    assert seen == []


async def test_the_gate_fails_open_when_the_check_fails(monkeypatch) -> None:
    async def _boom(session, tenant_id):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(professional_units, "has_usable_units", _boom)
    seen = _capture(monkeypatch)
    tenant_id = uuid4()
    assert not await unit_gate.reply_blocked_no_usable_unit(tenant_id)
    assert [event for event, _ in seen] == ["unit_gate_check_failed"]
    assert seen[0][1]["error_type"] == "RuntimeError"
    assert "database unreachable" not in str(seen[0][1])


async def test_the_gate_without_a_tenant_leaves_the_decision_to_the_callers_gates() -> None:
    assert not await unit_gate.reply_blocked_no_usable_unit(None)
