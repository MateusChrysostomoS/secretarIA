"""The Asaas onboarding status (GET /myAccount/status) of a clinic account (TASK-071, Plan 4).

The four states are read when the key is connected and on the daily recheck. A key that cannot
read them (403: only Clientes/Cobrancas/Webhooks) is NOT a problem: the fields stay None and the
connection is untouched. A state outside the four known ones is UNKNOWN, never approved.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from secretaria.models import Tenant
from secretaria.services.payments import accounts, asaas_connection, asaas_provider
from secretaria.services.payments.asaas import AsaasError
from secretaria.services.payments.asaas_probes import Verdict, VerdictKind
from secretaria.services.payments.onboarding import AccountOnboarding, parse_account_status
from tests.payments_support import FakeAsaasClient, FakeRedis, make_sessionmaker

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
KEY = "$aact_prod_0123456789abcdef0123456789abcdef0123456789"
BASE = "https://secretaria.example.test"
CAPS = {"customers": True, "payments": True, "webhooks": True}
ACCEPTED = Verdict(VerdictKind.ACCEPTED, None, (), (), dict(CAPS))


def _probes(verdict):
    async def run(api_key):
        return verdict

    return run


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture
def asaas(monkeypatch) -> FakeAsaasClient:
    fake = FakeAsaasClient()
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    return fake


async def _connect(db):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()
        await asaas_connection.connect_asaas_account(
            session, tenant_id=tenant.id, api_key=KEY, webhook_base_url=BASE, now=NOW,
            run_probes=_probes(ACCEPTED),
        )
        await session.commit()
    return tenant


async def _row(db, tenant):
    async with db() as session:
        return await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")


def test_parse_known_statuses():
    parsed = parse_account_status(
        {
            "general": "APPROVED",
            "documentation": "PENDING",
            "commercialInfo": "AWAITING_APPROVAL",
            "bankAccountInfo": "REJECTED",
            "id": "ignored",
        }
    )
    assert parsed == AccountOnboarding("APPROVED", "PENDING", "AWAITING_APPROVAL", "REJECTED")


def test_parse_unknown_value_is_UNKNOWN_not_approved():
    parsed = parse_account_status(
        {
            "general": "APPROVED_WITH_SOMETHING_NEW",
            "documentation": "APPROVED",
            "commercialInfo": 7,
            # bankAccountInfo absent
        }
    )
    assert parsed == AccountOnboarding("UNKNOWN", "APPROVED", "UNKNOWN", "UNKNOWN")
    assert not parsed.all_approved


def test_parse_unexpected_payload_is_none():
    assert parse_account_status({}) is None
    assert parse_account_status({"id": "x", "name": "y"}) is None
    assert parse_account_status(None) is None
    assert parse_account_status(["APPROVED"]) is None  # type: ignore[arg-type]


def test_all_approved_only_when_the_four_are_approved():
    assert AccountOnboarding("APPROVED", "APPROVED", "APPROVED", "APPROVED").all_approved
    assert not AccountOnboarding("APPROVED", "APPROVED", "APPROVED", "PENDING").all_approved


async def test_connect_stores_the_onboarding_states(db, asaas):
    asaas.account_status = {
        "general": "APPROVED",
        "documentation": "PENDING",
        "commercialInfo": "APPROVED",
        "bankAccountInfo": "AWAITING_APPROVAL",
    }
    tenant = await _connect(db)
    row = await _row(db, tenant)
    assert (
        row.onboarding_general,
        row.onboarding_docs,
        row.onboarding_commercial,
        row.onboarding_bank,
    ) == ("APPROVED", "PENDING", "APPROVED", "AWAITING_APPROVAL")
    assert row.onboarding_checked_at is not None


async def test_403_on_status_is_unreadable_not_error(db, asaas):
    asaas.errors["get_account_status"] = AsaasError(403, "insufficient_permission")
    tenant = await _connect(db)
    row = await _row(db, tenant)
    assert row.status == "connected"  # the connection is untouched
    assert row.onboarding_general is None and row.onboarding_bank is None
    assert row.onboarding_checked_at is not None  # we did look


async def test_recheck_updates_onboarding(db, asaas):
    asaas.account_status = {
        "general": "PENDING",
        "documentation": "PENDING",
        "commercialInfo": "PENDING",
        "bankAccountInfo": "PENDING",
    }
    tenant = await _connect(db)
    assert (await _row(db, tenant)).onboarding_docs == "PENDING"

    asaas.account_status = {
        "general": "APPROVED",
        "documentation": "APPROVED",
        "commercialInfo": "APPROVED",
        "bankAccountInfo": "APPROVED",
    }
    async with db() as session:
        outcome = await asaas_connection.recheck_asaas_account(
            session, tenant_id=tenant.id, redis=FakeRedis(), now=NOW,
            run_probes=_probes(ACCEPTED),
        )
        await session.commit()
    row = await _row(db, tenant)
    assert outcome == "accepted"
    assert row.onboarding_docs == "APPROVED" and row.onboarding_bank == "APPROVED"


async def test_recheck_losing_read_access_clears_the_states(db, asaas):
    tenant = await _connect(db)
    assert (await _row(db, tenant)).onboarding_general == "APPROVED"
    asaas.errors["get_account_status"] = AsaasError(403, "insufficient_permission")
    async with db() as session:
        await asaas_connection.recheck_asaas_account(
            session, tenant_id=tenant.id, redis=FakeRedis(), now=NOW,
            run_probes=_probes(ACCEPTED),
        )
        await session.commit()
    row = await _row(db, tenant)
    assert row.status == "connected" and row.onboarding_general is None


async def test_connecting_a_key_stamps_when_it_was_stored(db, asaas):
    """A charge that worked before this moment belongs to the previous key (charge_health)."""
    tenant = await _connect(db)
    assert (await _row(db, tenant)).key_connected_at is not None
