"""Charge health of a clinic payment account (TASK-071, Plan 3, task 1).

The account row remembers the last charge failure (reason + stage + when) and the last
success, so the clinic sees "ok / failing / untested" without a failed Pix ever blocking a
booking. `record_*` flush (the caller commits); `charge_health` is a pure function of the row.
"""

import contextlib
import io
import pathlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select

from secretaria.models import ClinicPaymentAccount, Tenant
from secretaria.services.payments import accounts
from secretaria.services.payments.failure_reason import FailureReason
from tests.payments_support import make_sessionmaker

REPO = pathlib.Path(__file__).resolve().parent.parent
REVISION = "d4f7a1c9e2b6"
PREVIOUS_HEAD = "e8a3c6f1b4d9"


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _account(session, provider: str = "asaas") -> ClinicPaymentAccount:
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    session.add(tenant)
    await session.flush()
    return await accounts.get_or_create_account_row(session, tenant_id=tenant.id, provider=provider)


async def test_health_untested_when_never_charged(db):
    async with db() as session:
        row = await _account(session)
        assert row.last_charge_failure_reason is None
        assert row.last_charge_failure_stage is None
        assert row.last_charge_failure_at is None
        assert row.last_charge_ok_at is None
        assert accounts.charge_health(row) == "untested"


async def test_record_failure_then_success_flips_health(db):
    async with db() as session:
        row = await _account(session)
        await accounts.record_charge_failure(
            session,
            tenant_id=row.tenant_id,
            provider="asaas",
            reason=FailureReason.customer_document_invalid,
            stage="customer",
        )
        await session.commit()
        await session.refresh(row)
        assert row.last_charge_failure_reason == "customer_document_invalid"
        assert row.last_charge_failure_stage == "customer"
        assert row.last_charge_failure_at is not None
        assert accounts.charge_health(row) == "failing"

        await accounts.record_charge_success(session, tenant_id=row.tenant_id, provider="asaas")
        await session.commit()
        await session.refresh(row)
        assert row.last_charge_ok_at is not None
        assert accounts.charge_health(row) == "ok"
        # The last failure stays on the row as history; only the health verdict flips.
        assert row.last_charge_failure_reason == "customer_document_invalid"


async def test_failure_after_success_is_failing(db):
    async with db() as session:
        row = await _account(session)
        await accounts.record_charge_success(session, tenant_id=row.tenant_id, provider="asaas")
        assert accounts.charge_health(row) == "ok"
        row.last_charge_ok_at = datetime.now(UTC) - timedelta(minutes=5)
        await accounts.record_charge_failure(
            session,
            tenant_id=row.tenant_id,
            provider="asaas",
            reason="charge_rejected",
            stage="charge",
        )
        await session.commit()
        await session.refresh(row)
        assert row.last_charge_failure_reason == "charge_rejected"
        assert accounts.charge_health(row) == "failing"


async def test_record_without_account_row_does_nothing(db):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()
        await accounts.record_charge_failure(
            session,
            tenant_id=tenant.id,
            provider="asaas",
            reason=FailureReason.no_account,
            stage="charge",
        )
        await accounts.record_charge_success(session, tenant_id=tenant.id, provider="asaas")
        assert await session.scalar(select(ClinicPaymentAccount)) is None


async def test_record_is_scoped_to_tenant_and_provider(db):
    async with db() as session:
        row = await _account(session)
        other = await _account(session)
        await accounts.record_charge_failure(
            session,
            tenant_id=row.tenant_id,
            provider="mercadopago",  # no such row for this tenant
            reason=FailureReason.unknown,
            stage="charge",
        )
        await accounts.record_charge_failure(
            session,
            tenant_id=row.tenant_id,
            provider="asaas",
            reason=FailureReason.unknown,
            stage="charge",
        )
        assert row.last_charge_failure_at is not None
        assert other.last_charge_failure_at is None


def test_charge_health_handles_naive_and_aware_datetimes():
    aware = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    naive_later = datetime(2026, 10, 28, 12, 5)  # SQLite hands back naive values
    row = SimpleNamespace(last_charge_failure_at=naive_later, last_charge_ok_at=aware)
    assert accounts.charge_health(row) == "failing"
    row = SimpleNamespace(last_charge_failure_at=aware, last_charge_ok_at=naive_later)
    assert accounts.charge_health(row) == "ok"


def test_charge_health_equal_timestamps_is_ok_and_only_success_is_ok():
    moment = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    tie = SimpleNamespace(last_charge_failure_at=moment, last_charge_ok_at=moment)
    assert accounts.charge_health(tie) == "ok"
    only_ok = SimpleNamespace(last_charge_failure_at=None, last_charge_ok_at=moment)
    assert accounts.charge_health(only_ok) == "ok"
    only_failure = SimpleNamespace(last_charge_failure_at=moment, last_charge_ok_at=None)
    assert accounts.charge_health(only_failure) == "failing"


# --- migration -------------------------------------------------------------------------------


def _config() -> Config:
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    return cfg


def _offline(direction: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        if direction == "up":
            command.upgrade(_config(), f"{PREVIOUS_HEAD}:{REVISION}", sql=True)
        else:
            command.downgrade(_config(), f"{REVISION}:{PREVIOUS_HEAD}", sql=True)
    return out.getvalue()


def test_the_revision_is_chained_after_the_previous_head():
    script = ScriptDirectory.from_config(_config())
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    heads = script.get_heads()
    assert len(heads) == 1
    assert REVISION in {r.revision for r in script.walk_revisions(head=heads[0])}


def test_upgrade_is_additive_and_downgrade_drops_the_columns():
    up = _offline("up").lower()
    for column in (
        "last_charge_failure_reason",
        "last_charge_failure_stage",
        "last_charge_failure_at",
        "last_charge_ok_at",
    ):
        assert column in up, column
    assert "drop" not in up and "not null" not in up
    down = _offline("down").lower()
    assert "drop column" in down or "drop " in down


@pytest.mark.parametrize(
    "column,length",
    [("last_charge_failure_reason", 32), ("last_charge_failure_stage", 16)],
)
def test_the_model_column_lengths_match_the_brief(column, length):
    assert ClinicPaymentAccount.__table__.c[column].type.length == length
    assert ClinicPaymentAccount.__table__.c[column].nullable is True
