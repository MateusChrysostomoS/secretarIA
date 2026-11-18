"""TASK-085 R / S7a - the agenda says WHAT a pending change on a sinal would change.

`GET /events` gains `pending_change_summary` (additive, contrato §3.9): plain Portuguese, built from
the open change's `pending_edit` against the row it was asked on - "Consulta → Avaliação; Dra. Ana →
Dr. Bruno; 10:00 → 11:30" - while the visit is SUSPENDED (D-R4, also while the patient chooses) or
the change already stands and waits for its Pix (D-R5). Null otherwise (no change, a legacy one, a
closed one). Names come only from THIS clinic's professionals and units.
"""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from httpx import AsyncClient
from sqlalchemy import select

from secretaria.models import Professional, Tenant, Unit
from secretaria.models.deposit_adjustment import DepositAdjustment
from secretaria.services.payments import service_change
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeCalendar,
    acting,
    r7,
    sent,
    set_tenant,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests.test_hub_edit_appointment import _iso, _paid_world, _post, provider  # noqa: F401
from tests.test_sinal_trocas_s4_hub import _events, _suspended_world

SP = ZoneInfo("America/Sao_Paulo")
ANA, BRUNO, CENTRO, LESTE = uuid4(), uuid4(), uuid4(), uuid4()
NAMES = {str(ANA): "Dra. Ana", str(BRUNO): "Dr. Bruno"}
UNITS = {str(CENTRO): "Centro", str(LESTE): "Leste"}


def _local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=SP).astimezone(UTC)


def _pending(**changes) -> dict:
    """A `pending_edit` as `serialize_edit` stores it: the target + the row it was asked on."""
    expected = {
        "start_at": _local(26, 10).isoformat(),
        "end_at": _local(26, 10, 30).isoformat(),
        "appointment_type": "Consulta",
        "google_event_id": "evt-1",
        "professional_id": str(ANA),
        "unit_id": str(CENTRO),
        "insurance": None,
    }
    data = {
        "appointment_type": "Consulta",
        "professional_id": str(ANA),
        "unit_id": str(CENTRO),
        "insurance": None,
        "start_at": expected["start_at"],
        "end_at": expected["end_at"],
        "expected": expected,
    }
    data.update(changes)
    return data


def _text(data: dict) -> str | None:
    return service_change.pending_change_summary_text(
        data, tz=SP, professional_names=NAMES, unit_names=UNITS
    )


# --------------------------------------------------------------------------
# the sentence (pure)
# --------------------------------------------------------------------------


def test_every_field_that_changes_in_the_order_the_clinic_reads_it():
    data = _pending(
        appointment_type="Retorno",
        professional_id=str(BRUNO),
        unit_id=str(LESTE),
        insurance="Unimed",
        start_at=_local(26, 11, 30).isoformat(),
        end_at=_local(26, 12).isoformat(),
    )
    assert _text(data) == (
        "Consulta → Retorno; Dra. Ana → Dr. Bruno; Unidade Centro → Unidade Leste; "
        "Particular → Unimed; 10:00 → 11:30"
    )


def test_only_what_changes_is_said():
    assert _text(_pending(appointment_type="Retorno")) == "Consulta → Retorno"
    assert _text(_pending(professional_id=str(BRUNO))) == "Dra. Ana → Dr. Bruno"
    assert _text(_pending(insurance="Unimed")) == "Particular → Unimed"


def test_another_day_shows_the_dates():
    data = _pending(start_at=_local(27, 11, 30).isoformat(), end_at=_local(27, 12).isoformat())
    assert _text(data) == "26/11 10:00 → 27/11 11:30"


def test_a_convenio_back_to_private_and_a_clinic_agenda():
    data = _pending(insurance="Particular", professional_id=None)
    data["expected"]["insurance"] = "Unimed"
    assert _text(data) == "Dra. Ana → Agenda da clínica; Unimed → Particular"


def test_an_unknown_id_is_never_shown_raw():
    stranger = uuid4()  # a professional of ANOTHER clinic: not in the names this clinic has
    assert _text(_pending(professional_id=str(stranger))) == "Dra. Ana → Outro profissional"


def test_nothing_to_say_is_null():
    assert _text(_pending()) is None
    assert _text({}) is None
    assert _text({"appointment_type": "Retorno"}) is None  # without the row it was asked on


def test_a_unit_absent_from_the_change_is_not_a_change():
    data = _pending(appointment_type="Retorno")
    del data["unit_id"]
    assert _text(data) == "Consulta → Retorno"


# --------------------------------------------------------------------------
# the names come from THIS clinic only
# --------------------------------------------------------------------------


async def test_the_names_are_read_from_the_clinic_only(db, acting):
    from tests._r7_support import setup_world

    world = await setup_world(db, acting)
    other = await setup_world(db, acting, phone_number_id="pnid-2")
    async with db() as session:
        mine = Professional(id=uuid4(), tenant_id=world.tenant.id, name="Dra. Ana")
        theirs = Professional(id=uuid4(), tenant_id=other.tenant.id, name="Dr. Alheio")
        unit = Unit(id=uuid4(), tenant_id=other.tenant.id, name="Alheia")
        session.add_all([mine, theirs, unit])
        await session.commit()
    data = _pending(professional_id=str(theirs.id), unit_id=str(unit.id))
    data["expected"].update(professional_id=str(mine.id), unit_id=None)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        summaries = await service_change.pending_change_summaries(
            session, tenant, {world.appointment.id: data}
        )
    assert summaries == {
        world.appointment.id: "Dra. Ana → Outro profissional; Sem unidade → Outra unidade"
    }


# --------------------------------------------------------------------------
# GET /events
# --------------------------------------------------------------------------


async def test_a_suspended_visit_says_what_the_change_would_do(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    world = await _paid_world(db, acting)
    await set_tenant(db, world.tenant.id, deposit_timing="before")
    new = world.start_at + timedelta(hours=2)
    response = await _post(client, world.appointment.id, service="Avaliação", start_at=_iso(new))
    assert response.json()["service_change"]["status"] == "suspended"
    FakeCalendar.reset()

    event = await _events(client, world, monkeypatch)

    old_local, new_local = world.start_at.astimezone(SP), new.astimezone(SP)
    times = f"{old_local:%H:%M} → {new_local:%H:%M}"
    if old_local.date() != new_local.date():
        times = f"{old_local:%d/%m %H:%M} → {new_local:%d/%m %H:%M}"
    assert event["suspended"] is True
    assert event["pending_change_summary"] == f"Consulta → Avaliação; {times}"
    # D-R10: once its old event left the agenda the visit is read from the "aguardando" list -
    # with the same summary (mapa-u §1.6).
    window = {
        "start": (world.start_at - timedelta(days=1)).isoformat(),
        "end": (world.start_at + timedelta(days=3)).isoformat(),
    }
    [row] = (await client.get(f"{CALENDAR}/awaiting-payment", params=window)).json()
    assert (row["kind"], row["appointment_id"]) == ("difference", str(world.appointment.id))
    assert row["change_summary"] == event["pending_change_summary"]


async def test_an_applied_change_waiting_for_its_pix_says_it_too(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    world = await _paid_world(db, acting)
    await _post(client, world.appointment.id, service="Avaliação")
    FakeCalendar.reset()

    event = await _events(client, world, monkeypatch)

    assert event["deposit"]["pending_difference_mode"] == "applied"
    assert event["pending_change_summary"] == "Consulta → Avaliação"


async def test_a_visit_without_a_pending_change_says_nothing(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    world = await _paid_world(db, acting)

    event = await _events(client, world, monkeypatch)

    assert event["pending_change_summary"] is None


async def test_a_closed_change_says_nothing(client: AsyncClient, db, acting, provider, monkeypatch):
    world = await _suspended_world(db, acting, client)
    async with db() as session:
        adjustment = (await session.scalars(select(DepositAdjustment))).one()
        adjustment.status = "kept_old"
        await session.commit()

    event = await _events(client, world, monkeypatch)

    assert event["suspended"] is False and event["pending_change_summary"] is None
