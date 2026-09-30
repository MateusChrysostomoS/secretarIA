"""POST /webhooks/{provider}/{webhook_slug}: the persist-first per-clinic payment webhook
(spec section 8.1) and the payload minimisation behind it (section 6.3)."""

import json
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event as sa_event, select
from sqlalchemy.exc import OperationalError

from secretaria.api import webhook_payments
from secretaria.core.database import get_session
from secretaria.models import PaymentWebhookEvent, Tenant
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts
from secretaria.services.payments.provider import NormalizedPaymentEvent, PaymentStatus
from secretaria.services.payments.webhook_payload import (
    event_from_dict,
    event_to_dict,
    minimize_payload,
)
from tests.payments_support import FakeArqPool, make_sessionmaker

TOKEN = "webhook-token-secret-0123456789"

# What Asaas really sends for a paid Pix, with every field that identifies the payer.
ASAAS_PAID = {
    "id": "evt_05b708f961d739ea7eba7e4db318f621&368604920",
    "event": "PAYMENT_RECEIVED",
    "dateCreated": "2026-09-29 10:00:00",
    "payment": {
        "id": "pay_080225913252",
        "customer": "cus_000005219613",
        "value": 60.0,
        "netValue": 59.01,
        "billingType": "PIX",
        "status": "RECEIVED",
        "description": "Sinal - Consulta - Clinica Marina Souza",
        "externalReference": "dep-1",
        "invoiceUrl": "https://www.asaas.com/i/080225913252",
        "transactionReceiptUrl": "https://www.asaas.com/comprovantes/1",
        "payer": {"name": "Maria da Silva", "cpfCnpj": "52998224725", "email": "maria@x.test"},
        "phone": "11999999999",
    },
}


@pytest_asyncio.fixture
async def world(monkeypatch):
    engine, maker = await make_sessionmaker()
    pool = FakeArqPool()

    async def _session():
        async with maker() as session:
            yield session

    from secretaria.main import app

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(app.state, "arq_pool", pool, raising=False)
    async with maker() as session:
        tenant = Tenant(id=uuid4(), clinic_name="A", phone_number_id="pn-a")
        other = Tenant(id=uuid4(), clinic_name="B", phone_number_id="pn-b")
        session.add_all([tenant, other])
        await session.flush()
        rows = {}
        for label, owner in (("a", tenant), ("b", other)):
            row = await accounts.get_or_create_account_row(
                session, tenant_id=owner.id, provider="asaas"
            )
            cfg.set_payment_account_secrets(row, api_key="key", webhook_token=TOKEN + label)
            rows[label] = row
        await session.commit()
        info = {
            "tenant": tenant,
            "other": other,
            "slug": rows["a"].webhook_slug,
            "other_slug": rows["b"].webhook_slug,
        }
    yield info, pool, maker, engine
    app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


def _headers(label: str = "a", **extra) -> dict:
    return {"asaas-access-token": TOKEN + label, "content-type": "application/json", **extra}


async def _rows(maker) -> list[PaymentWebhookEvent]:
    async with maker() as session:
        return list((await session.scalars(select(PaymentWebhookEvent))).all())


async def test_an_authentic_event_is_persisted_then_enqueued_by_row_id_only(
    client: AsyncClient, world
):
    info, pool, maker, _ = world
    response = await client.post(
        f"/webhooks/asaas/{info['slug']}", content=json.dumps(ASAAS_PAID), headers=_headers()
    )
    rows = await _rows(maker)

    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert len(rows) == 1
    row = rows[0]
    assert (row.tenant_id, row.provider) == (info["tenant"].id, "asaas")
    assert row.event_id == ASAAS_PAID["id"] and row.status == "received"
    assert row.provider_charge_id == "pay_080225913252"
    assert pool.jobs == [("process_payment_event", (str(row.id),), {})]
    assert TOKEN not in json.dumps(pool.jobs) and "cpf" not in json.dumps(pool.jobs).lower()


async def test_replay_is_acknowledged_but_never_stored_or_enqueued_twice(client, world):
    info, pool, maker, _ = world
    url = f"/webhooks/asaas/{info['slug']}"
    first = await client.post(url, content=json.dumps(ASAAS_PAID), headers=_headers())
    again = await client.post(url, content=json.dumps(ASAAS_PAID), headers=_headers())

    assert first.json() == {"status": "ok"} and again.json() == {"status": "duplicate"}
    assert again.status_code == 200
    assert len(await _rows(maker)) == 1
    assert len(pool.jobs) == 1


async def test_a_forged_token_is_401_with_no_body_no_row_and_no_job(client, world):
    info, pool, maker, _ = world
    response = await client.post(
        f"/webhooks/asaas/{info['slug']}",
        content=json.dumps(ASAAS_PAID),
        headers={**_headers(), "asaas-access-token": "wrong"},
    )
    assert response.status_code == 401 and response.content == b""
    assert await _rows(maker) == [] and pool.jobs == []


async def test_the_token_of_another_clinic_does_not_open_this_clinics_url(client, world):
    info, pool, maker, _ = world
    response = await client.post(
        f"/webhooks/asaas/{info['slug']}", content=json.dumps(ASAAS_PAID), headers=_headers("b")
    )
    assert response.status_code == 401
    assert await _rows(maker) == []


@pytest.mark.parametrize("path", ["asaas/no-such-slug", "nope/whatever", "mercadopago/x"])
async def test_unknown_slug_or_provider_is_404_with_no_body(client, world, path):
    response = await client.post(f"/webhooks/{path}", content=b"{}", headers=_headers())
    assert response.status_code == 404 and response.content == b""


async def test_a_slug_never_authenticates_for_another_provider(client, world):
    info, _, _, _ = world
    response = await client.post(
        f"/webhooks/mercadopago/{info['slug']}", content=b"{}", headers=_headers()
    )
    assert response.status_code == 404


async def test_oversized_and_non_json_bodies_are_refused_before_any_lookup(client, world):
    info, pool, maker, _ = world
    url = f"/webhooks/asaas/{info['slug']}"
    oversized = b"x" * (webhook_payments.MAX_BODY_BYTES + 1)
    big = await client.post(url, content=oversized, headers=_headers())
    plain = {**_headers(), "content-type": "text/plain"}
    text = await client.post(url, content=b"{}", headers=plain)
    assert big.status_code == 413
    assert text.status_code == 415
    assert await _rows(maker) == [] and pool.jobs == []


async def test_malformed_json_from_the_provider_is_acknowledged_and_ignored(client, world):
    info, pool, maker, _ = world
    response = await client.post(
        f"/webhooks/asaas/{info['slug']}", content=b"{not json", headers=_headers()
    )
    assert response.status_code == 200 and response.json() == {"status": "ignored"}
    assert await _rows(maker) == [] and pool.jobs == []


async def test_an_event_that_is_not_a_payment_is_acknowledged_and_ignored(client, world):
    info, _, maker, _ = world
    response = await client.post(
        f"/webhooks/asaas/{info['slug']}",
        content=json.dumps({"event": "ACCOUNT_STATUS_CHANGED"}),
        headers=_headers(),
    )
    assert response.json() == {"status": "ignored"}
    assert await _rows(maker) == []


async def test_the_stored_payload_is_minimised_and_carries_the_normalised_event(client, world):
    info, _, maker, _ = world
    await client.post(
        f"/webhooks/asaas/{info['slug']}", content=json.dumps(ASAAS_PAID), headers=_headers()
    )
    (row,) = await _rows(maker)
    stored = json.dumps(row.payload)

    for personal in ("Maria da Silva", "52998224725", "maria@x.test", "11999999999", "cus_0000",
                     "Marina Souza", "invoiceUrl", "asaas.com/comprovantes"):
        assert personal not in stored
    event = event_from_dict(row.payload["normalized"])
    assert (event.status, event.paid_amount_cents) == (PaymentStatus.PAID, 6000)
    assert event.external_reference == "dep-1"
    assert row.payload["raw"]["payment"]["id"] == "pay_080225913252"


async def test_the_same_event_id_in_two_clinics_lands_in_two_rows_of_the_right_tenants(
    client, world
):
    info, pool, maker, _ = world
    await client.post(
        f"/webhooks/asaas/{info['slug']}", content=json.dumps(ASAAS_PAID), headers=_headers("a")
    )
    await client.post(
        f"/webhooks/asaas/{info['other_slug']}",
        content=json.dumps(ASAAS_PAID),
        headers=_headers("b"),
    )
    rows = await _rows(maker)
    assert sorted(str(r.tenant_id) for r in rows) == sorted(
        [str(info["tenant"].id), str(info["other"].id)]
    )
    assert len(pool.jobs) == 2


async def test_the_handler_does_one_select_and_one_insert_and_nothing_else(client, world):
    info, _, _, engine = world
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.split(None, 1)[0].upper())

    sa_event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        await client.post(
            f"/webhooks/asaas/{info['slug']}", content=json.dumps(ASAAS_PAID), headers=_headers()
        )
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", _record)
    assert statements == ["SELECT", "INSERT"]


async def test_a_database_failure_is_503_so_the_provider_retries(client, world, monkeypatch):
    info, pool, _, _ = world

    async def _boom(*args, **kwargs):
        raise OperationalError("insert", {}, Exception("db down"))

    monkeypatch.setattr(webhook_payments, "insert_ignore", _boom)
    response = await client.post(
        f"/webhooks/asaas/{info['slug']}", content=json.dumps(ASAAS_PAID), headers=_headers()
    )
    assert response.status_code == 503 and pool.jobs == []


async def test_a_queue_outage_still_answers_200_because_the_event_is_already_durable(
    client, world, monkeypatch
):
    from secretaria.main import app

    info, _, maker, _ = world
    monkeypatch.setattr(app.state, "arq_pool", FakeArqPool(fail_enqueue=True), raising=False)
    down = await client.post(
        f"/webhooks/asaas/{info['slug']}", content=json.dumps(ASAAS_PAID), headers=_headers()
    )
    monkeypatch.setattr(app.state, "arq_pool", None, raising=False)
    no_pool = await client.post(
        f"/webhooks/asaas/{info['slug']}",
        content=json.dumps({**ASAAS_PAID, "id": "evt_2"}),
        headers=_headers(),
    )
    assert (down.status_code, no_pool.status_code) == (200, 200)
    assert sorted(r.event_id for r in await _rows(maker)) == sorted([ASAAS_PAID["id"], "evt_2"])


@pytest.mark.parametrize(
    ("mode", "forwarded", "expected"),
    [
        ("off", None, 200),
        ("alert", None, 200),
        ("enforce", None, 403),
        ("enforce", "9.9.9.9", 403),
        ("enforce", "1.1.1.1, 52.67.12.206", 200),  # the LAST hop is the one our proxy saw
        ("enforce", "52.67.12.206, 9.9.9.9", 403),  # a client-supplied first entry is not trusted
    ],
)
async def test_asaas_ip_allowlist_modes(client, world, monkeypatch, mode, forwarded, expected):
    info, _, _, _ = world
    monkeypatch.setattr(webhook_payments, "_ip_mode", lambda: mode)
    headers = _headers()
    if forwarded:
        headers["x-forwarded-for"] = forwarded
    response = await client.post(
        f"/webhooks/asaas/{info['slug']}", content=json.dumps(ASAAS_PAID), headers=headers
    )
    assert response.status_code == expected


# ---- payload helpers -------------------------------------------------------------------


def test_minimize_payload_drops_personal_keys_at_any_depth_and_keeps_the_rest():
    out = minimize_payload(
        {
            "id": "evt",
            "list": [{"email": "a@b.c", "keep": 1}],
            "payment": {"Customer": "cus", "CPFCNPJ": "1", "value": 6.0, "nested": {"name": "n"}},
        }
    )
    assert out == {"id": "evt", "list": [{"keep": 1}], "payment": {"value": 6.0, "nested": {}}}


def test_normalised_event_roundtrips_through_json():
    event = NormalizedPaymentEvent(
        provider="asaas",
        event_id="e",
        provider_charge_id="p",
        status=PaymentStatus.PAID,
        paid_amount_cents=6000,
        external_reference="r",
        event_type="PAYMENT_RECEIVED",
    )
    assert event_from_dict(json.loads(json.dumps(event_to_dict(event)))) == event
