"""The "Checklist para ter seu sinal e evitar no-show" (TASK-071, Plan 4, task 4B).

Eight items. The system fills what it can see; the two things only the manager can confirm
(the Pix key registered at Asaas, and - when the API key cannot read it - the Asaas registration)
are clicks kept per clinic. A click is whitelisted, carries who/when, survives a reconnection,
and never turns the Pix-key item green against the evidence of a charge that has no Pix code.
"""

import json
from datetime import datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from secretaria.api.hub import deps as hub_deps
from secretaria.core.database import get_session
from secretaria.core.subscription import SubscriptionClaim
from secretaria.models import Tenant
from secretaria.services.payments import (
    accounts,
    asaas_connection,
    asaas_provider,
    checklist,
)
from secretaria.services.payments.asaas_probes import Verdict, VerdictKind
from secretaria.services.payments.failure_reason import FailureReason
from tests.payments_support import FakeArqPool, FakeAsaasClient, make_sessionmaker
from tests.test_deposit_readiness import ALL_APPROVED, _seed, _type

KEYS = (
    "connect_asaas",
    "asaas_registration",
    "pix_key_registered",
    "service_prices",
    "sinal_on",
    "payment_notices",
    "notice_emails",
    "test_charge",
)
ENDPOINT = "/tenants/me/payment-accounts/asaas/checklist"
HEADERS = {"authorization": "Bearer role-test-token", "content-type": "application/json"}


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _view(session, tenant):
    return {i.key: i for i in await checklist.build_checklist_items(session, tenant_id=tenant.id)}


def test_only_the_two_manual_items_exist():
    assert checklist.MANUAL_ITEMS == ("pix_key_registered", "onboarding_done")


async def test_only_whitelisted_items_can_be_set(db):
    async with db() as session:
        tenant, row = await _seed(session)
        for bad in ("test_charge", "connect_asaas", "anything", "", "__proto__"):
            with pytest.raises(ValueError):
                await checklist.set_manual_item(
                    session, tenant_id=tenant.id, item=bad, done=True, by="manager"
                )
        await session.refresh(row)
        assert not row.setup_checklist  # nothing leaked into the column


async def test_manual_item_persists_with_user_and_time(db):
    async with db() as session:
        tenant, row = await _seed(session)
        tenant_id = tenant.id
        await checklist.set_manual_item(
            session, tenant_id=tenant_id, item="pix_key_registered", done=True, by="manager"
        )
        await session.commit()
        session.expire_all()
        row = await accounts.get_account_row(session, tenant_id=tenant_id, provider="asaas")
        entry = row.setup_checklist["pix_key_registered"]
        assert entry["done"] is True and entry["by"] == "manager"
        assert datetime.fromisoformat(entry["at"]).tzinfo is not None
        assert set(row.setup_checklist) == {"pix_key_registered"}

        await checklist.set_manual_item(
            session, tenant_id=tenant_id, item="pix_key_registered", done=False, by="manager"
        )
        await session.commit()
        session.expire_all()
        row = await accounts.get_account_row(session, tenant_id=tenant_id, provider="asaas")
        assert row.setup_checklist["pix_key_registered"]["done"] is False


async def test_a_click_before_connecting_creates_a_pending_row_that_gates_nothing(db):
    """The manager may register the Pix key BEFORE pasting the API key."""
    async with db() as session:
        tenant, _ = await _seed(session, account=None)
        await checklist.set_manual_item(
            session, tenant_id=tenant.id, item="pix_key_registered", done=True, by="manager"
        )
        await session.commit()
        row = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
        assert row.status == "pending"
        items = await _view(session, tenant)
    assert items["connect_asaas"].state == "pending"  # a bare row is not a connection
    assert items["pix_key_registered"].state == "done"


async def test_the_eight_items_and_who_fills_each(db):
    async with db() as session:
        tenant, _ = await _seed(session)
        items = await checklist.build_checklist_items(session, tenant_id=tenant.id)
    assert tuple(i.key for i in items) == KEYS
    source = {i.key: i.source for i in items}
    assert source["pix_key_registered"] == "user"
    assert {k: v for k, v in source.items() if k != "pix_key_registered"} == {
        "connect_asaas": "system",
        "asaas_registration": "system",  # readable in this seed
        "service_prices": "system",
        "sinal_on": "system",
        "payment_notices": "system",
        "notice_emails": "system",
        "test_charge": "system",
    }


async def test_pix_key_item_is_the_users_alone(db):
    async with db() as session:
        tenant, _ = await _seed(session)
        items = await _view(session, tenant)
        assert items["pix_key_registered"].state == "pending"
        await checklist.set_manual_item(
            session, tenant_id=tenant.id, item="pix_key_registered", done=True, by="manager"
        )
        await session.commit()
        items = await _view(session, tenant)
    assert items["pix_key_registered"].state == "done"
    assert items["pix_key_registered"].link  # "Abrir painel do Asaas"


@pytest.mark.parametrize("reason", [FailureReason.qr_unavailable, FailureReason.pix_key_missing])
async def test_pix_key_item_warns_when_qr_unavailable_even_if_checked(db, reason):
    async with db() as session:
        tenant, _ = await _seed(session)
        await checklist.set_manual_item(
            session, tenant_id=tenant.id, item="pix_key_registered", done=True, by="manager"
        )
        await accounts.record_charge_failure(
            session, tenant_id=tenant.id, provider="asaas", reason=reason, stage="qr"
        )
        await session.commit()
        items = await _view(session, tenant)
    item = items["pix_key_registered"]
    assert item.state == "warning"
    hint = item.hint.lower()
    assert "você marcou como feito" in hint and "confira a chave" in hint


async def test_pix_key_warning_clears_after_a_charge_works(db):
    async with db() as session:
        tenant, _ = await _seed(session)
        await checklist.set_manual_item(
            session, tenant_id=tenant.id, item="pix_key_registered", done=True, by="manager"
        )
        await accounts.record_charge_failure(
            session, tenant_id=tenant.id, provider="asaas",
            reason=FailureReason.qr_unavailable, stage="qr",
        )
        await session.commit()
        await accounts.record_charge_success(session, tenant_id=tenant.id, provider="asaas")
        await session.commit()
        items = await _view(session, tenant)
    assert items["pix_key_registered"].state == "done"
    assert items["test_charge"].state == "done"


async def test_onboarding_item_uses_system_state_when_readable_else_user_click(db):
    async with db() as session:
        # readable and approved -> done by the system, a click is not needed
        tenant, _ = await _seed(session, onboarding=ALL_APPROVED)
        item = (await _view(session, tenant))["asaas_registration"]
        assert (item.state, item.source) == ("done", "system")

        # readable and pending -> pending; a manager click does NOT override what Asaas says
        tenant, _ = await _seed(
            session, onboarding=("APPROVED", "PENDING", "APPROVED", "APPROVED")
        )
        await checklist.set_manual_item(
            session, tenant_id=tenant.id, item="onboarding_done", done=True, by="manager"
        )
        await session.commit()
        item = (await _view(session, tenant))["asaas_registration"]
        assert (item.state, item.source) == ("pending", "system")

        # unreadable -> "unknown" until the manager confirms, then done by the user
        tenant, _ = await _seed(session, onboarding=None)
        item = (await _view(session, tenant))["asaas_registration"]
        assert (item.state, item.source) == ("unknown", "user")
        assert "Confira no painel do Asaas" in item.hint
        await checklist.set_manual_item(
            session, tenant_id=tenant.id, item="onboarding_done", done=True, by="manager"
        )
        await session.commit()
        item = (await _view(session, tenant))["asaas_registration"]
        assert (item.state, item.source) == ("done", "user")


async def test_unknown_onboarding_value_is_never_done(db):
    async with db() as session:
        tenant, _ = await _seed(session, onboarding=("APPROVED", "APPROVED", "APPROVED", "NEW"))
        item = (await _view(session, tenant))["asaas_registration"]
    assert item.state == "pending"


async def test_system_items_follow_the_data(db):
    async with db() as session:
        tenant, _ = await _seed(
            session,
            enabled=False,
            types=[_type("Consulta", "R$ 100,00"), _type("Retorno", "")],
            contact_email=None,
        )
        items = await _view(session, tenant)
    assert items["sinal_on"].state == "pending"
    assert items["service_prices"].state == "warning"
    assert "Retorno" in items["service_prices"].hint
    assert items["notice_emails"].state == "warning"
    assert items["payment_notices"].state == "done"  # connected: the webhook is read back
    assert items["test_charge"].state == "pending"


async def test_no_prices_is_pending_not_done(db):
    async with db() as session:
        tenant, _ = await _seed(session, types=[_type(price="")])
        items = await _view(session, tenant)
    assert items["service_prices"].state == "pending"


async def test_everything_in_place_counts_eight_of_eight(db):
    async with db() as session:
        tenant, _ = await _seed(session)
        await checklist.set_manual_item(
            session, tenant_id=tenant.id, item="pix_key_registered", done=True, by="manager"
        )
        await accounts.record_charge_success(session, tenant_id=tenant.id, provider="asaas")
        await session.commit()
        out = await checklist.build_checklist(session, tenant_id=tenant.id)
    assert (out.done, out.total) == (8, 8)
    assert out.badge == "ok"


async def test_badge_stays_grey_until_proof(db):
    async with db() as session:
        tenant, _ = await _seed(session)
        await checklist.set_manual_item(
            session, tenant_id=tenant.id, item="pix_key_registered", done=True, by="manager"
        )
        await session.commit()
        out = await checklist.build_checklist(session, tenant_id=tenant.id)
    assert out.done == 7 and out.badge == "untested"


async def test_reconnect_keeps_manual_clicks(db, monkeypatch):
    fake = FakeAsaasClient()
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)

    async def probes(api_key):
        return Verdict(VerdictKind.ACCEPTED, None, (), (), {"customers": True})

    key_a = "$aact_prod_0123456789abcdef0123456789abcdef0123456789"
    key_b = "$aact_prod_ffffffffffffffffffffffffffffffffffffffffff"
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="C", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()
        for key in (key_a, key_b):
            await asaas_connection.connect_asaas_account(
                session, tenant_id=tenant.id, api_key=key, webhook_base_url="https://x.test",
                run_probes=probes,
            )
            await session.commit()
            if key == key_a:
                for item in checklist.MANUAL_ITEMS:
                    await checklist.set_manual_item(
                        session, tenant_id=tenant.id, item=item, done=True, by="manager"
                    )
                await session.commit()
        row = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
    assert row.setup_checklist["pix_key_registered"]["done"] is True
    assert row.setup_checklist["onboarding_done"]["done"] is True


# --- the endpoints -------------------------------------------------------------------------


@pytest_asyncio.fixture
async def world(monkeypatch):
    from secretaria.main import app

    engine, maker = await make_sessionmaker()
    async with maker() as session:
        tenant, _ = await _seed(session)

    async def _session():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(app.state, "arq_pool", FakeArqPool(), raising=False)
    role = {"value": "manager"}

    async def _verify(token, *, fresh=False):
        return SubscriptionClaim(tenant_id=tenant.id, active=True, role=role["value"])

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    yield tenant, maker, role
    app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


async def test_non_manager_cannot_set_item(client, world):
    tenant, maker, role = world
    for value in ("secretary", "doctor", None):
        role["value"] = value
        response = await client.put(
            ENDPOINT,
            content=json.dumps({"item": "pix_key_registered", "done": True}),
            headers=HEADERS,
        )
        assert response.status_code == 403, value
    async with maker() as session:
        row = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
        assert not row.setup_checklist


async def test_manager_sets_an_item_and_reads_it_back(client, world):
    tenant, maker, role = world
    put = await client.put(
        ENDPOINT, content=json.dumps({"item": "pix_key_registered", "done": True}), headers=HEADERS
    )
    assert put.status_code == 200
    got = await client.get(ENDPOINT, headers=HEADERS)
    body = got.json()
    assert [i["key"] for i in body["items"]] == list(KEYS)
    by_key = {i["key"]: i for i in body["items"]}
    assert by_key["pix_key_registered"]["state"] == "done"
    assert by_key["pix_key_registered"]["source"] == "user"
    assert body["progress"] == {"done": 7, "total": 8}
    assert body["badge"] == "untested"
    assert body["asaas_panel_url"].startswith("https://")


async def test_a_doctor_may_read_but_not_write(client, world):
    tenant, maker, role = world
    role["value"] = "doctor"
    assert (await client.get(ENDPOINT, headers=HEADERS)).status_code == 200


async def test_bad_bodies_are_rejected_without_echo(client, world):
    for body in (
        {"item": "test_charge", "done": True},
        {"item": "pix_key_registered", "done": "yes"},
        {"item": "pix_key_registered"},
        {"item": "pix_key_registered", "done": True, "x": 1},
    ):
        response = await client.put(ENDPOINT, content=json.dumps(body), headers=HEADERS)
        assert response.status_code == 422, body
        assert response.json()["detail"]["code"] == "invalid_body"
