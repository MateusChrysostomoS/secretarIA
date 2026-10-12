"""Who is looking at the agenda: brain-api's answer -> the hub viewer (TASK-044 R7, spec §5.A)."""

# ruff: noqa: F811

from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from secretaria.api.hub import deps as hub_deps
from secretaria.core.subscription import SubscriptionClaim
from secretaria.services.agenda_visibility import CLINIC_WIDE, AgendaViewer, viewer_from_claim
from tests._agenda_viewer import view_as
from tests._r7_support import CALENDAR, acting, r7, setup_world  # noqa: F401
from tests._reminder_fixtures import db  # noqa: F401

P1, P2 = uuid4(), uuid4()


# ------------------------------------------------------------------ the pure rule


@pytest.mark.parametrize(
    ("scope", "professional_id", "expected"),
    [
        ("clinic", P1, AgendaViewer("clinic", P1)),
        ("clinic", None, AgendaViewer("clinic", None)),
        ("own", P1, AgendaViewer("own", P1)),
        ("own", None, AgendaViewer("own", None)),
        (None, P1, AgendaViewer("own", P1)),  # older brain-api + a doctor's token: fail closed
        (None, None, CLINIC_WIDE),  # older brain-api, no professional: today's view
    ],
)
def test_brain_apis_answer_becomes_the_viewer(scope, professional_id, expected):
    assert viewer_from_claim(scope, professional_id) == expected


def test_what_each_viewer_sees():
    assert CLINIC_WIDE.sees(P1) and CLINIC_WIDE.sees(None)
    assert CLINIC_WIDE.can_filter_own is False
    assert AgendaViewer("clinic", P1).can_filter_own is True
    own = AgendaViewer("own", P1)
    assert own.restricted and own.sees(P1)
    assert not own.sees(P2) and not own.sees(None)
    assert own.can_filter_own is False
    nobody = AgendaViewer("own", None)
    assert not nobody.sees(None) and not nobody.sees(P1)


# ------------------------------------------------------------------ the dependency


def _verify_as(monkeypatch, claim) -> None:
    async def _fake(token: str):
        return claim

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _fake)


async def test_the_dependency_reads_brain_apis_answer(db, acting, monkeypatch):
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    ana = world.appointment.professional_id
    _verify_as(
        monkeypatch,
        SubscriptionClaim(
            tenant_id=world.tenant.id, active=True, professional_id=ana, agenda_scope="own"
        ),
    )

    async with db() as session:
        viewer = await hub_deps.get_agenda_viewer(
            authorization="Bearer t", tenant=world.tenant, session=session
        )

    assert viewer == AgendaViewer("own", ana)


async def test_a_professional_of_another_clinic_is_dropped_never_trusted(db, acting, monkeypatch):
    world = await setup_world(db, acting)
    other = await setup_world(
        db, acting, phone_number_id="pnid-2", wa_id="5511900000002", professional_name="Dr. Fora"
    )
    _verify_as(
        monkeypatch,
        SubscriptionClaim(
            tenant_id=world.tenant.id,
            active=True,
            professional_id=other.appointment.professional_id,
            agenda_scope="clinic",
        ),
    )

    async with db() as session:
        viewer = await hub_deps.get_agenda_viewer(
            authorization="Bearer t", tenant=world.tenant, session=session
        )

    assert viewer == AgendaViewer("clinic", None)  # no toggle, never the other clinic's doctor


async def test_an_older_brain_api_restricts_a_doctors_token(db, acting, monkeypatch):
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    ana = world.appointment.professional_id
    _verify_as(
        monkeypatch, SubscriptionClaim(tenant_id=world.tenant.id, active=True, professional_id=ana)
    )

    async with db() as session:
        viewer = await hub_deps.get_agenda_viewer(
            authorization="Bearer t", tenant=world.tenant, session=session
        )

    assert viewer == AgendaViewer("own", ana)


@pytest.mark.parametrize("case", ["no_claim", "inactive", "other_tenant", "no_token"])
async def test_no_live_session_for_this_clinic_is_401(db, acting, monkeypatch, case):
    world = await setup_world(db, acting)
    claim = {
        "no_claim": None,
        "inactive": SubscriptionClaim(tenant_id=world.tenant.id, active=False),
        "other_tenant": SubscriptionClaim(tenant_id=uuid4(), active=True, agenda_scope="clinic"),
        "no_token": SubscriptionClaim(tenant_id=world.tenant.id, active=True),
    }[case]
    _verify_as(monkeypatch, claim)

    async with db() as session:
        with pytest.raises(HTTPException) as err:
            await hub_deps.get_agenda_viewer(
                authorization=None if case == "no_token" else "Bearer t",
                tenant=world.tenant,
                session=session,
            )

    assert err.value.status_code == 401


# ------------------------------------------------------------------ GET /viewer


async def test_a_manager_who_is_also_a_doctor_gets_the_toggle(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    view_as(AgendaViewer("clinic", world.appointment.professional_id))

    body = (await client.get(f"{CALENDAR}/viewer")).json()

    assert body == {
        "agenda_scope": "clinic",
        "professional_id": str(world.appointment.professional_id),
        "professional_name": "Dra. Ana",
        "can_filter_own": True,
    }


async def test_a_receptionist_sees_the_clinic_without_toggle(client: AsyncClient, db, acting):
    await setup_world(db, acting)  # conftest default: CLINIC_WIDE

    body = (await client.get(f"{CALENDAR}/viewer")).json()

    assert body == {
        "agenda_scope": "clinic",
        "professional_id": None,
        "professional_name": None,
        "can_filter_own": False,
    }


async def test_a_doctor_restricted_to_his_own_agenda(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, professional_name="Dr. Beto")
    view_as(AgendaViewer("own", world.appointment.professional_id))

    body = (await client.get(f"{CALENDAR}/viewer")).json()

    assert body["agenda_scope"] == "own" and body["can_filter_own"] is False
    assert body["professional_name"] == "Dr. Beto"
