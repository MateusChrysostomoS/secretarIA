# ruff: noqa: F811
"""TASK-085 T4c: the AI and the texts read the price/duration of the UNIT (DB-backed readers).

The clinic: Dra. Ana does "Consulta" (base R$ 200,00 / 30 min) at Sede (08-09h) and Centro
(10-11h). Exceptions per unit live in `professional_service_unit_terms`. Characterization
tests (no exception) pin today's output byte for byte; the others prove SPEC D3/D7/R2:
a price that differs between the units is never shown before the unit, one unit means that
unit's value, and a booked consultation is read at ITS unit.
"""

import os

os.environ.setdefault("APP_ENV", "test")

from dataclasses import dataclass  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai import (  # noqa: E402
    availability_tool as at,
    scoped_help,
    tools as ai_tools,
)
from secretaria.ai.prompts import _format_appointment_types  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    Patient,
    Professional,
    ProfessionalServiceUnitTerms,
    ProfessionalUnit,
    Service,
    Tenant,
    Unit,
)
from secretaria.services import booking_hold  # noqa: E402
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_SOLE,
)
from secretaria.services.local_calendar import LocalCalendarService  # noqa: E402
from secretaria.services.service_catalog import load_service_catalog  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    list_active_professionals,
    load_tenant_config,
    professional_appointment_types,
)
from secretaria.services.unit_booking import attach_unit_options  # noqa: E402
from secretaria.workers.shared.greeting import (  # noqa: E402
    _adapt_greeting_has_upcoming,
    _flow_professionals,
    _load_upcoming_greeting_data,
)
from secretaria.workers.shared.llm_context import _appointment_context_text  # noqa: E402
from tests._local_agenda import wire_local_agenda  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

TZ = "America/Sao_Paulo"
WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ALL_DAY = {d: [{"start": "08:00", "end": "18:00"}] for d in WEEK}
SEDE_HOURS = {d: [{"start": "08:00", "end": "09:00"}] for d in WEEK}
CENTRO_HOURS = {d: [{"start": "10:00", "end": "11:00"}] for d in WEEK}
HOURS = {"Sede": SEDE_HOURS, "Centro": CENTRO_HOURS}


@dataclass
class World:
    tenant: Tenant
    service: Service
    ana: Professional
    bia: Professional | None
    sede: Unit
    centro: Unit


def _ana_entry(service_id: UUID) -> dict:
    return {
        "name": "Consulta",
        "service_id": str(service_id),
        "price": "R$ 200,00",
        "duration_min": 30,
        "is_active": True,
        "requirements": ["Trazer exames"],
    }


async def _world(
    db,
    *,
    units: tuple[str, ...] = ("Sede", "Centro"),
    exceptions: dict | None = None,
    second: bool = False,
) -> World:
    """The clinic. `exceptions` = {unit name: (price, duration_min)} for Ana's Consulta."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica Teste",
            phone_number_id=str(uuid4())[:12],
            timezone=TZ,
            business_hours=ALL_DAY,
            # The clinic's legacy list: what a clinic with 2+ professionals reads.
            appointment_types=[
                {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 180,00"}
            ],
        )
        session.add(tenant)
        await session.flush()
        service = Service(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Consulta",
            normalized_name="consulta",
            requirements=["Trazer exames"],
        )
        sede = Unit(id=uuid4(), tenant_id=tenant.id, name="Sede", address="Rua A, 1")
        centro = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro", address="Rua B, 2")
        session.add_all([service, sede, centro])
        await session.flush()
        ana = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            business_hours=ALL_DAY,
            appointment_types=[_ana_entry(service.id)],
        )
        bia = None
        session.add(ana)
        if second:
            bia = Professional(
                id=uuid4(),
                tenant_id=tenant.id,
                name="Dra. Bia",
                is_active=True,
                business_hours=ALL_DAY,
                appointment_types=[{**_ana_entry(service.id), "price": "R$ 300,00"}],
            )
            session.add(bia)
        await session.flush()
        by_name = {"Sede": sede, "Centro": centro}
        for position, name in enumerate(units):
            session.add(
                ProfessionalUnit(
                    professional_id=ana.id,
                    unit_id=by_name[name].id,
                    business_hours=HOURS[name],
                    sort_order=position,
                )
            )
        for name, (price, duration) in (exceptions or {}).items():
            session.add(
                ProfessionalServiceUnitTerms(
                    tenant_id=tenant.id,
                    professional_id=ana.id,
                    service_id=service.id,
                    unit_id=by_name[name].id,
                    price=price,
                    duration_min=duration,
                )
            )
        await session.commit()
    return World(tenant, service, ana, bia, sede, centro)


async def _foreign_row(db, w: World, unit: Unit, price: str, duration: int | None = None):
    """A row naming Ana and her unit but carried by ANOTHER clinic (must never be read)."""
    async with db() as session:
        stranger = Tenant(id=uuid4(), clinic_name="Outra", phone_number_id=str(uuid4())[:12])
        session.add(stranger)
        await session.flush()
        session.add(
            ProfessionalServiceUnitTerms(
                tenant_id=stranger.id,
                professional_id=w.ana.id,
                service_id=w.service.id,
                unit_id=unit.id,
                price=price,
                duration_min=duration,
            )
        )
        await session.commit()


async def _config(db, w: World):
    async with db() as session:
        return await load_tenant_config(session, await session.get(Tenant, w.tenant.id))


# =============================================================== B1/B3 load_tenant_config


_TODAY_LINE = "- Consulta (30 min) - R$ 200,00 (há orientações: use get_service_info)"


@pytest.mark.asyncio
async def test_char_b1_single_professional_two_units_is_todays_list(db):
    with_units = await _config(db, await _world(db))
    without_units = await _config(db, await _world(db, units=()))
    for config in (with_units, without_units):
        [t] = config.appointment_types
        assert (t.name, t.price, t.duration_min, t.requirements) == (
            "Consulta",
            "R$ 200,00",
            30,
            ["Trazer exames"],
        )
        assert getattr(t, "price_hidden", False) is False
        assert _format_appointment_types(config.appointment_types, 30) == _TODAY_LINE
    assert with_units.appointment_types == without_units.appointment_types


@pytest.mark.asyncio
async def test_char_b1_multi_professional_clinic_reads_the_legacy_list(db):
    config = await _config(db, await _world(db, second=True))
    [t] = config.appointment_types
    assert (t.price, t.duration_min) == ("R$ 180,00", 30)
    assert _format_appointment_types(config.appointment_types, 30) == _TODAY_LINE.replace(
        "200", "180"
    )


# ======================================================================= B4 get_service_info


def _ctx(config, *, tenant_id=None, topology=BOOKING_TOPOLOGY_SOLE):
    return [
        (ai_tools._tenant_config_ctx, ai_tools._tenant_config_ctx.set(config)),
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(tenant_id or config.tenant_id)),
        (ai_tools._booking_topology_ctx, ai_tools._booking_topology_ctx.set(topology)),
    ]


def _reset(tokens):
    for var, token in reversed(tokens):
        var.reset(token)


async def _service_info(config, args: dict, topology=BOOKING_TOPOLOGY_SOLE) -> dict:
    tokens = _ctx(config, topology=topology)
    try:
        return await ai_tools.get_service_info.ainvoke(args)
    finally:
        _reset(tokens)


_TODAY_INFO = {
    "servico": "Consulta",
    "duracao_min": 30,
    "preco": "R$ 200,00",
    "descricao": None,
    "descricao_completa": None,
    "orientacoes": ["Trazer exames"],
}


@pytest.mark.asyncio
async def test_char_b4_get_service_info_is_todays(db, monkeypatch):
    monkeypatch.setattr(core_database, "async_session_factory", db)
    config = await _config(db, await _world(db))
    assert await _service_info(config, {"service_name": "consulta"}) == _TODAY_INFO
    multi = await _config(db, await _world(db, second=True))
    got = await _service_info(multi, {"service_name": "Consulta"}, BOOKING_TOPOLOGY_MULTI)
    assert got == {**_TODAY_INFO, "preco": "R$ 180,00"}


# ====================================================================== B5 scoped help


class _FakeModel:
    def __init__(self):
        self.calls: list = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return scoped_help._ScopedHelpDecision(action="escalate")


async def _help_prompt(db, monkeypatch, w: World, *, selected: Professional | None = None) -> str:
    """The system prompt `run_service_help` sends for Ana's list, over a real conversation."""
    async with db() as session:
        patient = Patient(tenant_id=w.tenant.id, wa_id="5511900000000")
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=w.tenant.id,
            patient_id=patient.id,
            flow_selected_professional_id=selected.id if selected is not None else None,
        )
        session.add(conversation)
        await session.commit()
        tenant = await session.get(Tenant, w.tenant.id)
        catalog = await load_service_catalog(session, tenant.id)
        services = professional_appointment_types(
            await session.get(Professional, (selected or w.ana).id), tenant, catalog
        )
    fake = _FakeModel()
    monkeypatch.setattr(scoped_help, "_get_decision_model", lambda: fake)
    monkeypatch.setattr(scoped_help, "async_session_factory", db)

    async def _load(_cid):
        from pseudonymize_core import Pseudonymizer

        return Pseudonymizer()

    async def _persist(_cid, _p):
        return None

    monkeypatch.setattr(scoped_help, "load_pseudonymizer", _load)
    monkeypatch.setattr(scoped_help, "persist_pseudonymizer", _persist)
    await scoped_help.run_service_help(
        conversation_id=conversation.id,
        services=services,
        patient_message="quero uma consulta",
        final_round=False,
    )
    return fake.calls[0][0].content


@pytest.mark.asyncio
async def test_char_b5_scoped_help_block_has_no_money(db, monkeypatch):
    # TASK-088 R38: the real help prompt lists the service without its price.
    prompt = await _help_prompt(db, monkeypatch, await _world(db))
    assert "Serviços disponíveis (os ÚNICOS que existem):\n- Consulta\n\n" in prompt
    w = await _world(db, second=True)
    prompt = await _help_prompt(db, monkeypatch, w, selected=w.bia)
    assert "- Consulta\n" in prompt and "R$" not in prompt


# =================================================================== A11 get_availability


def _clinic_today():
    return (datetime.now(UTC) - timedelta(hours=3)).date()


async def _availability(db, monkeypatch, w: World, args: dict | None = None) -> dict:
    """get_availability for Dra. Ana on the real native agenda, days 1..3 from tomorrow."""
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(booking_hold, "async_session_factory", db)
    wire_local_agenda(monkeypatch, db)
    config = await _config(db, w)
    agenda = LocalCalendarService.for_config(config, professional_id=w.ana.id)
    tomorrow = _clinic_today() + timedelta(days=1)
    payload = {
        "service": "Consulta",
        "day_from": tomorrow.isoformat(),
        "day_to": (tomorrow + timedelta(days=2)).isoformat(),
        **(args or {}),
    }
    tokens = _ctx(config)
    tokens.append((ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(agenda)))
    try:
        return await at.get_availability.ainvoke(payload)
    finally:
        _reset(tokens)


def _by_unit(result: dict) -> dict[str, set[tuple[str, str]]]:
    out: dict[str, set[tuple[str, str]]] = {}
    for window in result["windows"]:
        out.setdefault(window.get("unit"), set()).add((window["start"], window["end"]))
    return out


@pytest.mark.asyncio
async def test_char_a11_two_units_without_exceptions(db, monkeypatch):
    result = await _availability(db, monkeypatch, await _world(db))
    assert result["slot_minutes"] == 30
    assert _by_unit(result) == {"Sede": {("08:00", "09:00")}, "Centro": {("10:00", "11:00")}}
    assert len(result["windows"]) == 6  # 3 days x 2 units


@pytest.mark.asyncio
async def test_char_a11_one_unit_without_exceptions(db, monkeypatch):
    result = await _availability(db, monkeypatch, await _world(db, units=("Centro",)))
    assert result["slot_minutes"] == 30 and result["unit"] == "Centro"
    assert {(w["start"], w["end"]) for w in result["windows"]} == {("10:00", "11:00")}


# ================================================================ A12 greeting (next consult)


def _appointment(w: World, unit: Unit | None, *, owner: Professional | None = None) -> dict:
    owner = owner or w.ana
    return {
        "id": str(uuid4()),
        "appointment_type": "Consulta",
        "insurance": None,
        "start_at": datetime(2026, 12, 7, 14, 0, tzinfo=UTC),
        "end_at": datetime(2026, 12, 7, 14, 30, tzinfo=UTC),
        "professional_id": str(owner.id),
        **({"unit_id": str(unit.id)} if unit is not None else {}),
    }


async def _greeting(db, w: World, appointment: dict) -> tuple[dict | None, str]:
    async with db() as session:
        tenant = await session.get(Tenant, w.tenant.id)
        data = await _load_upcoming_greeting_data(session, tenant, [appointment])
    text = _adapt_greeting_has_upcoming("Olá!", [appointment], tenant, data)
    return data.nearest_service, text


_TODAY_GREETING = (
    "Olá!\n\nVi aqui que você já tem uma consulta marcada para 07/12 às 11:00, com Dra. Ana.\n"
    "Consulta — R$ 200,00"
)


@pytest.mark.asyncio
async def test_char_a12_greeting_without_exceptions(db):
    w = await _world(db)
    for unit in (w.centro, w.sede, None):
        service, text = await _greeting(db, w, _appointment(w, unit))
        assert service["price"] == "R$ 200,00"
        assert text.startswith(_TODAY_GREETING)


# ========================================================== A13 "Próxima consulta" for the LLM


async def _roster(db, w: World):
    """The worker's roster snapshot (workers/orchestrator.py): resolved + units attached."""
    async with db() as session:
        tenant = await session.get(Tenant, w.tenant.id)
        rows = await list_active_professionals(session, tenant.id)
        catalog = await load_service_catalog(session, tenant.id)
        roster = _flow_professionals(rows, catalog)
        await attach_unit_options(session, tenant, rows, roster)
    return roster


_TODAY_CONTEXT = "Próxima consulta: 07/12 às 11:00 — Consulta — Dra. Ana — {unit} — R$ 200,00"


@pytest.mark.asyncio
async def test_char_a13_next_consultation_line_without_exceptions(db):
    w = await _world(db)
    config = await _config(db, w)
    roster = await _roster(db, w)
    names = {str(p.id): p.name for p in roster}
    units = {str(u.id): u.name for p in roster for u in p.units}
    for unit in (w.centro, w.sede):
        text = _appointment_context_text(
            [_appointment(w, unit)], TZ, names, config.appointment_types, unit_names=units
        )
        assert text.splitlines()[0] == _TODAY_CONTEXT.format(unit=unit.name)
        # Written before T4c without `professionals`; the worker now passes the roster too.
        assert text == _appointment_context_text(
            [_appointment(w, unit)],
            TZ,
            names,
            config.appointment_types,
            unit_names=units,
            professionals=roster,
        )


# =========================================================================================
# With per-unit values (each fails before TASK-085 T4c)
# =========================================================================================

SEDE_200_CENTRO_250 = {"Centro": ("R$ 250,00", None)}


@pytest.mark.asyncio
async def test_b1_b3_price_that_differs_between_units_is_hidden(db):
    """SPEC §10 item 2 (AI part): Sede R$ 200 / Centro R$ 250 -> no price before the unit."""
    config = await _config(db, await _world(db, exceptions=SEDE_200_CENTRO_250))
    [t] = config.appointment_types
    assert (t.price, t.price_hidden, t.duration_min) == (None, True, 30)
    section = _format_appointment_types(config.appointment_types, 30)
    assert "R$" not in section
    assert section.startswith("- Consulta (30 min) - valor depende da unidade escolhida")


@pytest.mark.asyncio
async def test_b1_same_price_in_every_unit_is_shown_with_the_base_duration(db):
    config = await _config(db, await _world(db, exceptions={"Centro": ("200", 40)}))
    [t] = config.appointment_types
    assert (t.price, t.price_hidden, t.duration_min) == ("R$ 200,00", False, 30)
    assert _format_appointment_types(config.appointment_types, 30) == _TODAY_LINE


@pytest.mark.asyncio
async def test_b1_only_one_unit_shows_that_units_value(db):
    """R2: the service is done at one unit only, and that unit has its own value."""
    w = await _world(db, units=("Centro",), exceptions={"Centro": ("R$ 250,00", 40)})
    config = await _config(db, w)
    [t] = config.appointment_types
    assert (t.price, t.price_hidden, t.duration_min) == ("R$ 250,00", False, 40)
    assert _format_appointment_types(config.appointment_types, 30) == (
        "- Consulta (40 min) - R$ 250,00 (há orientações: use get_service_info)"
    )


@pytest.mark.asyncio
async def test_b1_multi_professional_clinic_keeps_the_legacy_list(db):
    config = await _config(db, await _world(db, second=True, exceptions=SEDE_200_CENTRO_250))
    [t] = config.appointment_types
    assert (t.price, t.price_hidden, t.duration_min) == ("R$ 180,00", False, 30)


@pytest.mark.asyncio
async def test_b1_another_clinics_rows_are_never_read(db):
    w = await _world(db)
    await _foreign_row(db, w, w.centro, "R$ 999,00", 90)
    [t] = (await _config(db, w)).appointment_types
    assert (t.price, t.price_hidden, t.duration_min) == ("R$ 200,00", False, 30)


@pytest.mark.asyncio
async def test_b4_hidden_price_says_it_depends_on_the_unit(db, monkeypatch):
    monkeypatch.setattr(core_database, "async_session_factory", db)
    config = await _config(db, await _world(db, exceptions=SEDE_200_CENTRO_250))
    got = await _service_info(config, {"service_name": "Consulta"})
    assert got == {**_TODAY_INFO, "preco": None, "preco_depende_da_unidade": True}


@pytest.mark.asyncio
async def test_b4_with_the_unit_states_that_units_price_and_duration(db, monkeypatch):
    monkeypatch.setattr(core_database, "async_session_factory", db)
    config = await _config(db, await _world(db, exceptions={"Centro": ("R$ 250,00", 40)}))
    centro = await _service_info(config, {"service_name": "Consulta", "unit_name": " centro "})
    sede = await _service_info(config, {"service_name": "Consulta", "unit_name": "Sede"})
    unknown = await _service_info(config, {"service_name": "Consulta", "unit_name": "Norte"})
    assert centro == {**_TODAY_INFO, "preco": "R$ 250,00", "duracao_min": 40}
    assert sede == _TODAY_INFO
    assert set(unknown) == {"error"} and "Sede" in unknown["error"] and "Centro" in unknown["error"]


@pytest.mark.asyncio
async def test_b5_scoped_help_never_shows_a_price_that_differs(db, monkeypatch):
    prompt = await _help_prompt(db, monkeypatch, await _world(db, exceptions=SEDE_200_CENTRO_250))
    block = prompt.split("Serviços disponíveis (os ÚNICOS que existem):\n")[1].split("\n\n")[0]
    # R38: not even the "valor depende da unidade" sentence - money is said only at the end.
    assert block == "- Consulta"
    assert "R$" not in block and "valor depende" not in block


@pytest.mark.asyncio
async def test_b5_scoped_help_one_unit_shows_no_price_either(db, monkeypatch):
    w = await _world(db, units=("Centro",), exceptions={"Centro": ("R$ 250,00", None)})
    prompt = await _help_prompt(db, monkeypatch, w)
    assert "\n- Consulta\n" in prompt
    assert "R$" not in prompt and "valor depende" not in prompt


@pytest.mark.asyncio
async def test_a11_each_unit_is_scanned_with_its_own_duration(db, monkeypatch):
    """SPEC §10 item 4 (AI part): Centro 40 min vs Sede 30 min -> windows sized per unit."""
    w = await _world(db, exceptions={"Centro": (None, 40)})
    result = await _availability(db, monkeypatch, w)
    assert _by_unit(result) == {"Sede": {("08:00", "09:00")}, "Centro": {("10:00", "10:40")}}
    assert result["slot_minutes"] == 30  # several units, durations differ: the base (D7)


@pytest.mark.asyncio
async def test_a11_one_unit_uses_its_duration(db, monkeypatch):
    w = await _world(db, units=("Centro",), exceptions={"Centro": (None, 40)})
    result = await _availability(db, monkeypatch, w)
    assert result["slot_minutes"] == 40
    assert {(x["start"], x["end"]) for x in result["windows"]} == {("10:00", "10:40")}


@pytest.mark.asyncio
async def test_a11_another_clinics_rows_are_never_read(db, monkeypatch):
    w = await _world(db)
    await _foreign_row(db, w, w.centro, "R$ 999,00", 40)
    result = await _availability(db, monkeypatch, w)
    assert _by_unit(result) == {"Sede": {("08:00", "09:00")}, "Centro": {("10:00", "11:00")}}


@pytest.mark.asyncio
async def test_a12_greeting_reads_the_price_at_the_appointments_unit(db):
    w = await _world(db, exceptions=SEDE_200_CENTRO_250)
    service, text = await _greeting(db, w, _appointment(w, w.centro))
    assert service["price"] == "R$ 250,00"
    assert "\nConsulta — R$ 250,00" in text
    for unit in (w.sede, None):  # no row at Sede; a booking without a unit reads the base (D7)
        service, text = await _greeting(db, w, _appointment(w, unit))
        assert service["price"] == "R$ 200,00" and text.startswith(_TODAY_GREETING)


@pytest.mark.asyncio
async def test_a12_another_clinics_rows_are_never_read(db):
    w = await _world(db)
    await _foreign_row(db, w, w.centro, "R$ 999,00")
    service, text = await _greeting(db, w, _appointment(w, w.centro))
    assert service["price"] == "R$ 200,00" and text.startswith(_TODAY_GREETING)


async def _context_line(db, w: World, unit: Unit | None) -> str:
    config = await _config(db, w)
    roster = await _roster(db, w)
    text = _appointment_context_text(
        [_appointment(w, unit)],
        TZ,
        {str(p.id): p.name for p in roster},
        config.appointment_types,
        unit_names={str(u.id): u.name for p in roster for u in p.units},
        professionals=roster,
    )
    return text.splitlines()[0]


@pytest.mark.asyncio
async def test_a13_next_consultation_at_centro_reads_centros_price(db):
    w = await _world(db, exceptions=SEDE_200_CENTRO_250)
    assert await _context_line(db, w, w.centro) == _TODAY_CONTEXT.replace(
        "{unit} — R$ 200,00", "Centro — R$ 250,00"
    )
    assert await _context_line(db, w, w.sede) == _TODAY_CONTEXT.format(unit="Sede")
    assert await _context_line(db, w, None) == (
        "Próxima consulta: 07/12 às 11:00 — Consulta — Dra. Ana — R$ 200,00"
    )


@pytest.mark.asyncio
async def test_a13_another_clinics_rows_are_never_read(db):
    w = await _world(db)
    await _foreign_row(db, w, w.centro, "R$ 999,00")
    assert await _context_line(db, w, w.centro) == _TODAY_CONTEXT.format(unit="Centro")
