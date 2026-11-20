"""TASK-087 T6: the AI never offers or confirms a day the service is not done on.

The clinic: Dra. Ana does "Cirurgia" (blocked on Mondays at the base) and "Retorno" (no block).
* get_availability drops a blocked day for the named service - per unit - and stays unfiltered
  with no service named (R5);
* the three booking tools (`create_event`, `create_event_for_professional`,
  `create_event_at_unit`) refuse a blocked day with the patient sentence, before any write;
* the conversation state tells the model which days the service is NOT done on (only when some
  day is blocked - nothing changes for a clinic that does not use the feature).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from contextlib import contextmanager  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.ai import availability_tool as at, tools as ai_tools  # noqa: E402
from secretaria.ai.unit_scope import service_day_refusal_for  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    Professional,
    ProfessionalServiceUnitTerms,
    ProfessionalUnit,
    Service,
    Tenant,
    Unit,
)
from secretaria.plugins import multi_professional as mp, multi_unit as mu  # noqa: E402
from secretaria.services import booking_hold  # noqa: E402
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_SOLE,
)
from secretaria.services.local_calendar import LocalCalendarService  # noqa: E402
from secretaria.services.service_terms import UnitTerms  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    RuntimeAppointmentType,
    TenantRuntimeConfig,
    load_tenant_config,
)
from secretaria.services.unit_booking import UnitOption  # noqa: E402
from tests._local_agenda import wire_local_agenda  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ALL_DAY = {d: [{"start": "08:00", "end": "18:00"}] for d in WEEK}
SEDE_HOURS = {d: [{"start": "08:00", "end": "09:00"}] for d in WEEK}
CENTRO_HOURS = {d: [{"start": "10:00", "end": "11:00"}] for d in WEEK}
HOURS = {"Sede": SEDE_HOURS, "Centro": CENTRO_HOURS}

# 2026-07-10 is a Friday; 2026-07-13 a Monday; 2026-07-14 a Tuesday.
FRIDAY = {"start": "2026-07-10T09:00:00", "end": "2026-07-10T09:30:00", "summary": "Consulta"}
MONDAY = {"start": "2026-07-13T09:00:00", "end": "2026-07-13T09:30:00", "summary": "Consulta"}
TUESDAY = {"start": "2026-07-14T09:00:00", "end": "2026-07-14T09:30:00", "summary": "Consulta"}
MONDAY_REFUSAL = "Cirurgia com Dra. Ana não está disponível às segundas"


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)


async def _world(
    db,  # noqa: F811
    *,
    base_blocked: list[str] | None = None,
    units: tuple[str, ...] = (),
    unit_days: dict[str, list[str]] | None = None,
    second_doctor: bool = False,
):
    """Tenant + Ana (Cirurgia with `base_blocked`, Retorno unblocked) linked to `units`.

    `unit_days` = {unit name: blocked weekdays} -> one exception row per unit (days only).
    """
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica Teste",
            phone_number_id=str(uuid4())[:12],
            timezone="America/Sao_Paulo",
            business_hours=ALL_DAY,
            appointment_types=[
                {"name": "Cirurgia", "duration_min": 30, "is_active": True},
                {"name": "Retorno", "duration_min": 30, "is_active": True},
            ],
        )
        session.add(tenant)
        await session.flush()
        cirurgia = Service(
            id=uuid4(), tenant_id=tenant.id, name="Cirurgia", normalized_name="cirurgia"
        )
        retorno = Service(
            id=uuid4(), tenant_id=tenant.id, name="Retorno", normalized_name="retorno"
        )
        by_unit = {
            name: Unit(id=uuid4(), tenant_id=tenant.id, name=name, address=f"Rua {name}, 1")
            for name in ("Sede", "Centro")
        }
        session.add_all([cirurgia, retorno, *by_unit.values()])
        await session.flush()

        def _entries(blocked):
            return [
                {
                    "name": "Cirurgia",
                    "service_id": str(cirurgia.id),
                    "duration_min": 30,
                    "is_active": True,
                    **({"blocked_weekdays": blocked} if blocked else {}),
                },
                {
                    "name": "Retorno",
                    "service_id": str(retorno.id),
                    "duration_min": 30,
                    "is_active": True,
                },
            ]

        ana = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            business_hours=ALL_DAY,
            appointment_types=_entries(base_blocked),
        )
        session.add(ana)
        bruno = None
        if second_doctor:
            bruno = Professional(
                id=uuid4(),
                tenant_id=tenant.id,
                name="Dr. Bruno",
                is_active=True,
                business_hours=ALL_DAY,
                appointment_types=_entries(None),
            )
            session.add(bruno)
        await session.flush()
        for position, name in enumerate(units):
            session.add(
                ProfessionalUnit(
                    professional_id=ana.id,
                    unit_id=by_unit[name].id,
                    business_hours=HOURS[name] if len(units) > 1 else ALL_DAY,
                    sort_order=position,
                )
            )
        for name, days in (unit_days or {}).items():
            session.add(
                ProfessionalServiceUnitTerms(
                    tenant_id=tenant.id,
                    professional_id=ana.id,
                    service_id=cirurgia.id,
                    unit_id=by_unit[name].id,
                    blocked_weekdays=days,
                )
            )
        await session.commit()
    return SimpleNamespace(
        tenant=tenant, ana=ana, bruno=bruno, cirurgia=cirurgia, units=by_unit
    )


# ======================================================================= get_availability


def _next_monday():
    today = (datetime.now(UTC) - timedelta(hours=3)).date()
    day = today + timedelta(days=2)
    while day.weekday() != 0:
        day += timedelta(days=1)
    return day


async def _availability(db, monkeypatch, world, args: dict) -> dict:  # noqa: F811
    monkeypatch.setattr(booking_hold, "async_session_factory", db)
    wire_local_agenda(monkeypatch, db)
    async with db() as session:
        config = await load_tenant_config(session, await session.get(Tenant, world.tenant.id))
    agenda = LocalCalendarService.for_config(config, professional_id=world.ana.id)
    monday = _next_monday()
    payload = {
        "day_from": monday.isoformat(),
        "day_to": (monday + timedelta(days=2)).isoformat(),
        **args,
    }
    tokens = [
        (ai_tools._tenant_config_ctx, ai_tools._tenant_config_ctx.set(config)),
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(config.tenant_id)),
        (ai_tools._booking_topology_ctx, ai_tools._booking_topology_ctx.set(BOOKING_TOPOLOGY_SOLE)),
        (ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(agenda)),
    ]
    try:
        return await at.get_availability.ainvoke(payload)
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _days(result: dict) -> set[str]:
    return {window["day"] for window in result["windows"]}


@pytest.mark.asyncio
async def test_availability_tool_skips_blocked_day_for_service(db, monkeypatch):  # noqa: F811
    world = await _world(db, base_blocked=["monday"])
    result = await _availability(db, monkeypatch, world, {"service": "Cirurgia"})
    monday = _next_monday()
    assert monday.isoformat() not in _days(result)
    assert (monday + timedelta(days=1)).isoformat() in _days(result)
    assert (monday + timedelta(days=2)).isoformat() in _days(result)


@pytest.mark.asyncio
async def test_availability_tool_other_service_keeps_the_blocked_day(db, monkeypatch):  # noqa: F811
    world = await _world(db, base_blocked=["monday"])
    result = await _availability(db, monkeypatch, world, {"service": "Retorno"})
    assert _next_monday().isoformat() in _days(result)


@pytest.mark.asyncio
async def test_availability_tool_without_service_unfiltered(db, monkeypatch):  # noqa: F811
    """R5: no service named yet = nothing to filter by (the confirmation covers the rest)."""
    world = await _world(db, base_blocked=["monday"])
    result = await _availability(db, monkeypatch, world, {})
    assert _next_monday().isoformat() in _days(result)


@pytest.mark.asyncio
async def test_availability_tool_reads_each_units_own_days(db, monkeypatch):  # noqa: F811
    """Sede blocks Mondays on its own; Centro inherits the base (nothing blocked): on a Monday
    only Centro's windows are left, on the other days both units show."""
    world = await _world(db, units=("Sede", "Centro"), unit_days={"Sede": ["monday"]})
    result = await _availability(db, monkeypatch, world, {"service": "Cirurgia"})
    monday = _next_monday()
    on_monday = {w.get("unit") for w in result["windows"] if w["day"] == monday.isoformat()}
    tuesday = (monday + timedelta(days=1)).isoformat()
    on_tuesday = {w.get("unit") for w in result["windows"] if w["day"] == tuesday}
    assert on_monday == {"Centro"}
    assert on_tuesday == {"Sede", "Centro"}


@pytest.mark.asyncio
async def test_availability_tool_all_units_blocked_leaves_no_window_that_day(
    db,  # noqa: F811
    monkeypatch,
):
    world = await _world(
        db, base_blocked=["monday"], units=("Sede", "Centro"), unit_days={"Sede": ["monday"]}
    )
    # Sede blocks Monday itself, Centro inherits the base block: Monday is closed everywhere.
    result = await _availability(db, monkeypatch, world, {"service": "Cirurgia"})
    assert _next_monday().isoformat() not in _days(result)


# ============================================================ the three booking tools refuse


class _Agenda:
    """A fake agenda with the `with_unit` seam; `created` records every write."""

    def __init__(self, log=None):
        self.tzinfo = UTC
        self.log = log if log is not None else {"created": []}

    def with_unit(self, unit_id, location, *, business_hours=None):
        return _Agenda(self.log)

    async def list_free_slots(self, day, max_slots=6, **_kw):
        return []

    async def create_event(self, start, end, summary, description=""):
        self.log["created"].append(start)
        return {"id": f"evt-{len(self.log['created'])}", "status": "confirmed", "htmlLink": None}


def _runtime(name):
    return RuntimeAppointmentType(name=name, description=None, duration_min=30)


@contextmanager
def _context(tenant_id, *, calendar=None, sole=None, catalog=()):
    config = TenantRuntimeConfig(
        tenant_id=tenant_id, clinic_name="Clinic", language="pt-BR", timezone="UTC",
        appointment_duration_min=30,
        appointment_types=[_runtime(name) for name in catalog], business_hours={},
        google_calendar_id="primary", google_refresh_token=None, professional_id=sole,
    )
    tokens = [
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(tenant_id)),
        (ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(calendar)),
        (ai_tools._tenant_config_ctx, ai_tools._tenant_config_ctx.set(config)),
        (
            ai_tools._booking_topology_ctx,
            ai_tools._booking_topology_ctx.set(
                BOOKING_TOPOLOGY_SOLE if sole is not None else BOOKING_TOPOLOGY_MULTI
            ),
        ),
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


async def _booked(db):  # noqa: F811
    async with db() as session:
        return list(await session.scalars(select(Appointment)))


@pytest.mark.asyncio
async def test_create_event_refuses_blocked_day(db):  # noqa: F811
    """The base tool of a one-doctor clinic WITHOUT units: the base list still applies."""
    world = await _world(db, base_blocked=["monday"])
    agenda = _Agenda()
    with _context(
        world.tenant.id, calendar=agenda, sole=world.ana.id, catalog=("Cirurgia", "Retorno")
    ):
        refused = await ai_tools.create_event.ainvoke(
            {"appointment_type": "Cirurgia", **MONDAY}
        )
        other_day = await ai_tools.create_event.ainvoke(
            {"appointment_type": "Cirurgia", **TUESDAY}
        )
        other_service = await ai_tools.create_event.ainvoke(
            {"appointment_type": "Retorno", **MONDAY}
        )
    assert MONDAY_REFUSAL in refused["error"]
    assert "Posso ver outro dia?" in refused["error"]
    assert "id" in other_day and "id" in other_service
    assert len(agenda.log["created"]) == 2  # nothing written for the refused Monday
    assert len(await _booked(db)) == 2


@pytest.mark.asyncio
async def test_create_event_refuses_blocked_day_at_the_only_unit(db):  # noqa: F811
    world = await _world(db, base_blocked=["monday"], units=("Centro",))
    agenda = _Agenda()
    with _context(
        world.tenant.id, calendar=agenda, sole=world.ana.id, catalog=("Cirurgia", "Retorno")
    ):
        refused = await ai_tools.create_event.ainvoke(
            {"appointment_type": "Cirurgia", **MONDAY}
        )
    assert MONDAY_REFUSAL in refused["error"]
    assert agenda.log["created"] == [] and await _booked(db) == []


@pytest.mark.asyncio
async def test_create_event_for_professional_refuses_blocked_day(db, monkeypatch):  # noqa: F811
    world = await _world(db, base_blocked=["monday"], second_doctor=True)
    agenda = _Agenda()

    async def _calendar(tenant_id, professional):
        return agenda

    monkeypatch.setattr(mp, "_professional_calendar", _calendar)
    with _context(world.tenant.id):
        refused = await mp.create_event_for_professional.ainvoke(
            {"professional_name": "Dra. Ana", "appointment_type": "Cirurgia", **MONDAY}
        )
        ok_day = await mp.create_event_for_professional.ainvoke(
            {"professional_name": "Dra. Ana", "appointment_type": "Cirurgia", **TUESDAY}
        )
        colleague = await mp.create_event_for_professional.ainvoke(
            {"professional_name": "Dr. Bruno", "appointment_type": "Cirurgia", **MONDAY}
        )
    assert MONDAY_REFUSAL in refused["error"]
    assert "id" in ok_day and "id" in colleague
    assert len(agenda.log["created"]) == 2


@pytest.mark.asyncio
async def test_create_event_at_unit_uses_the_units_own_days(db):  # noqa: F811
    """Sede blocks Fridays on its own; Centro inherits the base (nothing)."""
    world = await _world(db, units=("Sede", "Centro"), unit_days={"Sede": ["friday"]})
    agenda = _Agenda()
    window = {**FRIDAY, "start": "2026-07-10T08:00:00", "end": "2026-07-10T08:30:00"}
    centro_window = {**FRIDAY, "start": "2026-07-10T10:00:00", "end": "2026-07-10T10:30:00"}
    with _context(
        world.tenant.id, calendar=agenda, sole=world.ana.id, catalog=("Cirurgia", "Retorno")
    ):
        refused = await mu.create_event_at_unit.ainvoke(
            {"unit_name": "Sede", "appointment_type": "Cirurgia", **window}
        )
        booked = await mu.create_event_at_unit.ainvoke(
            {"unit_name": "Centro", "appointment_type": "Cirurgia", **centro_window}
        )
    assert "Cirurgia com Dra. Ana não está disponível às sextas" in refused["error"]
    assert "id" in booked
    assert len(agenda.log["created"]) == 1


# ====================================================== unit_scope.service_day_refusal_for


@pytest.mark.asyncio
async def test_service_day_refusal_for_is_none_when_allowed_or_no_service(db):  # noqa: F811
    world = await _world(db, base_blocked=["monday"])
    entry = {
        "name": "Cirurgia",
        "service_id": str(world.cirurgia.id),
        "blocked_weekdays": ["monday"],
    }
    monday = datetime(2026, 7, 13, 9, 0)
    tuesday = datetime(2026, 7, 14, 9, 0)
    async with db() as session:
        blocked = await service_day_refusal_for(
            session, world.tenant.id, world.ana, entry, None, monday
        )
        free = await service_day_refusal_for(
            session, world.tenant.id, world.ana, entry, None, tuesday
        )
        # R5: no service known = nothing to refuse.
        unknown = await service_day_refusal_for(
            session, world.tenant.id, world.ana, None, None, monday
        )
    assert blocked == "Cirurgia com Dra. Ana não está disponível às segundas. Posso ver outro dia?"
    assert free is None and unknown is None


@pytest.mark.asyncio
async def test_service_day_refusal_for_ignores_another_clinics_rows(db):  # noqa: F811
    world = await _world(db, units=("Sede", "Centro"))
    other = await _world(db, units=("Sede", "Centro"), unit_days={"Sede": ["monday"]})
    entry = {"name": "Cirurgia", "service_id": str(world.cirurgia.id)}
    sede = world.units["Sede"].id
    async with db() as session:
        # `other`'s row is another tenant's: it can never block this clinic's Monday.
        got = await service_day_refusal_for(
            session, world.tenant.id, world.ana, entry, sede, datetime(2026, 7, 13, 9, 0)
        )
    assert got is None
    assert other.tenant.id != world.tenant.id


# ============================================================================ llm_context


_OPEN = {"monday": [{"start": "08:00", "end": "12:00"}]}


def _unit(name):
    return UnitOption(id=uuid4(), name=name, address=f"Rua {name}, 1", business_hours=_OPEN)


def _doctor(name, entries, units=(), terms=None):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        appointment_types=entries,
        business_hours=_OPEN,
        units=list(units),
        service_units={},
        service_unit_terms=terms or {},
    )


def _state(professionals):
    from secretaria.services.llm_context import build_conversation_state
    from tests.test_flow_router import _conversation, _tenant

    return build_conversation_state(_conversation(), _tenant(), professionals)


def _entry(name, service_id, blocked=None):
    entry = {"name": name, "is_active": True, "duration_min": 30, "service_id": str(service_id)}
    if blocked is not None:
        entry["blocked_weekdays"] = blocked
    return entry


def test_llm_context_mentions_blocked_days_without_units():
    cirurgia, retorno = uuid4(), uuid4()
    ana = _doctor(
        "Dra. Ana",
        [_entry("Cirurgia", cirurgia, ["monday", "friday"]), _entry("Retorno", retorno)],
    )
    text = _state([ana])
    assert "Cirurgia (não às segundas e sextas)" in text
    assert "Retorno" in text and "Retorno (não" not in text


def test_llm_context_mentions_blocked_days_per_unit():
    sede, centro = _unit("Sede"), _unit("Centro")
    cirurgia = uuid4()
    ana = _doctor(
        "Dra. Ana",
        [_entry("Cirurgia", cirurgia, ["monday"]), _entry("Retorno", uuid4())],
        units=[sede, centro],
        terms={cirurgia: {centro.id: UnitTerms(None, None, frozenset({"friday"}))}},
    )
    bruno = _doctor("Dr. Bruno", [_entry("Retorno", uuid4())], units=[sede])
    text = _state([ana, bruno])
    assert "Sede: Cirurgia (não às segundas), Retorno" in text
    assert "Centro: Cirurgia (não às sextas), Retorno" in text


def test_llm_context_weekend_days_use_aos():
    cirurgia = uuid4()
    ana = _doctor("Dra. Ana", [_entry("Cirurgia", cirurgia, ["saturday"])])
    assert "Cirurgia (não aos sábados)" in _state([ana])


def test_llm_context_mixed_days_give_each_its_article():
    # Review P6: the same list as the patient's sentence (`unit_booking.weekday_list_pt`).
    cirurgia = uuid4()
    ana = _doctor("Dra. Ana", [_entry("Cirurgia", cirurgia, ["saturday", "friday"])])
    assert "Cirurgia (não às sextas e aos sábados)" in _state([ana])


def test_llm_context_silent_without_block():
    sede, centro = _unit("Sede"), _unit("Centro")
    plain = _doctor(
        "Dra. Ana", [_entry("Cirurgia", uuid4()), _entry("Retorno", uuid4())], units=[sede, centro]
    )
    solo = _doctor("Dra. Bia", [_entry("Cirurgia", uuid4(), []), _entry("Retorno", uuid4())])
    text = _state([plain, solo])
    assert "não às" not in text and "não aos" not in text
    assert "Sede: Cirurgia, Retorno | Centro: Cirurgia, Retorno" in text
    assert "Dra. Bia: Cirurgia, Retorno" in text


# ====================================================== D4: days-only rows hide no price


def test_nearest_price_is_kept_when_the_owner_only_has_days_only_rows():
    from secretaria.workers.shared import llm_context

    service_id, unit_id, owner_id = uuid4(), uuid4(), uuid4()
    owner = SimpleNamespace(
        id=owner_id,
        # an inherited list (no own entries) would hide the price if days-only rows counted
        appointment_types=None,
        service_unit_terms={
            service_id: {unit_id: UnitTerms(None, None, frozenset({"monday"}))},
        },
    )
    matched = RuntimeAppointmentType(
        name="Cirurgia", description=None, duration_min=30, price="R$ 300,00"
    )
    nearest = {"professional_id": str(owner_id), "unit_id": str(unit_id)}
    assert llm_context._nearest_price(nearest, "Cirurgia", matched, [owner]) == "R$ 300,00"
    # a row that does carry a price keeps the old guarded behaviour (never a wrong price)
    owner.service_unit_terms = {service_id: {unit_id: UnitTerms("R$ 1,00", None, None)}}
    assert llm_context._nearest_price(nearest, "Cirurgia", matched, [owner]) is None


# ====================================================== A4: unreadable days = its own message


@pytest.mark.asyncio
async def test_unreadable_days_refuse_with_the_service_days_message_on_all_three_tools(
    db,  # noqa: F811
    monkeypatch,
):
    from secretaria.ai import unit_scope

    world = await _world(db, base_blocked=["monday"], second_doctor=True)
    agenda = _Agenda()

    async def _calendar(tenant_id, professional):
        return agenda

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("days read down")

    monkeypatch.setattr(mp, "_professional_calendar", _calendar)
    monkeypatch.setattr(unit_scope, "service_day_refusal_for", _boom)
    expected = ai_tools.SERVICE_DAYS_LOOKUP_FAILED_ERROR
    assert expected != ai_tools.UNIT_LOOKUP_FAILED_ERROR
    with _context(
        world.tenant.id, calendar=agenda, sole=world.ana.id, catalog=("Cirurgia", "Retorno")
    ):
        base = await ai_tools.create_event.ainvoke({"appointment_type": "Cirurgia", **TUESDAY})
        other = await mp.create_event_for_professional.ainvoke(
            {"professional_name": "Dr. Bruno", "appointment_type": "Cirurgia", **TUESDAY}
        )
    assert base["error"] == expected and other["error"] == expected
    assert agenda.log["created"] == [] and await _booked(db) == []

    unit_world = await _world(db, units=("Sede", "Centro"))
    unit_agenda = _Agenda()
    with _context(
        unit_world.tenant.id,
        calendar=unit_agenda,
        sole=unit_world.ana.id,
        catalog=("Cirurgia", "Retorno"),
    ):
        at_unit = await mu.create_event_at_unit.ainvoke(
            {
                "unit_name": "Centro",
                "appointment_type": "Cirurgia",
                **{**TUESDAY, "start": "2026-07-14T10:00:00", "end": "2026-07-14T10:30:00"},
            }
        )
    assert at_unit["error"] == expected
    assert unit_agenda.log["created"] == [] and await _booked(db) == []


# ====================================================== A5: one terms read, units of the service


async def _add_norte(db, world, *, mark_units: tuple[str, ...] | None):  # noqa: F811
    """A third unit (Norte) for Ana; `mark_units` restricts Cirurgia to those unit names."""
    from secretaria.models import ProfessionalServiceUnit

    async with db() as session:
        norte = Unit(id=uuid4(), tenant_id=world.tenant.id, name="Norte", address="Rua Norte, 1")
        session.add(norte)
        await session.flush()
        session.add(
            ProfessionalUnit(
                professional_id=world.ana.id, unit_id=norte.id, business_hours=ALL_DAY, sort_order=2
            )
        )
        for name in mark_units or ():
            unit = world.units.get(name)
            session.add(
                ProfessionalServiceUnit(
                    professional_id=world.ana.id,
                    service_id=world.cirurgia.id,
                    unit_id=unit.id,
                )
            )
        await session.commit()
    return norte


def _patch_tool_load_terms(monkeypatch, behaviour):
    """Replace `load_terms` ONLY while the availability tool reads (the config load uses it too)."""
    from secretaria.services import service_terms

    real_load, real_scope, active = service_terms.load_terms, at._service_unit_scope, []

    async def load(*args, **kwargs):
        if active:
            return await behaviour(real_load, *args, **kwargs)
        return await real_load(*args, **kwargs)

    async def scope(*args, **kwargs):
        active.append(1)
        try:
            return await real_scope(*args, **kwargs)
        finally:
            active.pop()

    monkeypatch.setattr(service_terms, "load_terms", load)
    monkeypatch.setattr(at, "_service_unit_scope", scope)


@pytest.mark.asyncio
async def test_availability_reads_the_terms_once_for_all_units(db, monkeypatch):  # noqa: F811
    world = await _world(db, units=("Sede", "Centro"), unit_days={"Sede": ["monday"]})
    await _add_norte(db, world, mark_units=None)
    calls: list[int] = []

    async def counting(real, *args, **kwargs):
        calls.append(1)
        return await real(*args, **kwargs)

    _patch_tool_load_terms(monkeypatch, counting)
    result = await _availability(db, monkeypatch, world, {"service": "Cirurgia"})
    assert len(calls) == 1
    monday = _next_monday().isoformat()
    assert {w.get("unit") for w in result["windows"] if w["day"] == monday} == {"Centro", "Norte"}


@pytest.mark.asyncio
async def test_availability_unreadable_terms_fall_back_to_the_base_for_every_unit(
    db,  # noqa: F811
    monkeypatch,
):
    world = await _world(
        db, base_blocked=["monday"], units=("Sede", "Centro"), unit_days={"Sede": []}
    )

    async def boom(_real, *_args, **_kwargs):
        raise RuntimeError("terms down")

    _patch_tool_load_terms(monkeypatch, boom)
    result = await _availability(db, monkeypatch, world, {"service": "Cirurgia"})
    # Sede's own "nothing blocked" is unreadable: the base (Monday blocked) holds everywhere.
    assert _next_monday().isoformat() not in _days(result)
    assert (_next_monday() + timedelta(days=1)).isoformat() in _days(result)


@pytest.mark.asyncio
async def test_availability_only_lists_the_units_that_do_the_service(db, monkeypatch):  # noqa: F811
    world = await _world(db, units=("Sede", "Centro"))
    await _add_norte(db, world, mark_units=("Sede", "Centro"))
    result = await _availability(db, monkeypatch, world, {"service": "Cirurgia"})
    assert {w.get("unit") for w in result["windows"]} == {"Sede", "Centro"}
    # another service of the same doctor has no marks: every unit
    other = await _availability(db, monkeypatch, world, {"service": "Retorno"})
    assert {w.get("unit") for w in other["windows"]} == {"Sede", "Centro", "Norte"}
