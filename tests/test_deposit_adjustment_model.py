"""DepositAdjustment (service change with a paid deposit, 2026-10-26 spec): the table that holds
the difference of the sinal, and the new attention reason that asks the clinic to give it back.
"""

import contextlib
import io
import pathlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from secretaria.models import DepositAdjustment, PaymentAttentionItem, PixDeposit, Tenant
from secretaria.models.deposit_adjustment import (
    ADJUSTMENT_STATUSES,
    KIND_DECREASE,
    KIND_INCREASE,
    STATUS_APPLIED,
    STATUS_ASKED,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_CANCELLED_VOID_PENDING,
    STATUS_CLINIC_KEPT,
    STATUS_COLLECT_DUE,
    STATUS_EXPIRED,
    STATUS_KEPT_CREDIT,
    STATUS_REFUND_REQUESTED,
)
from secretaria.models.payment_attention_item import ATTENTION_REASONS
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services.payments import attention
from secretaria.services.payments.attention_view import REASON_TEXT, reason_text
from tests.payments_support import make_sessionmaker

REPO = pathlib.Path(__file__).resolve().parent.parent
REVISION = "b3e8f1a6c2d9"
PREVIOUS_HEAD = "a7d2e9c4b1f6"


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _deposit(session) -> PixDeposit:
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    session.add(tenant)
    await session.flush()
    deposit = PixDeposit(
        tenant_id=tenant.id,
        amount_cents=6000,
        percent_applied=30,
        provider="asaas",
        provider_charge_id=f"pay_{uuid4().hex[:8]}",
        status=PixDepositStatus.PAID,
        paid_at=datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
    )
    session.add(deposit)
    await session.flush()
    return deposit


def _adjustment(deposit: PixDeposit, **kw) -> DepositAdjustment:
    fields = dict(
        tenant_id=deposit.tenant_id,
        deposit_id=deposit.id,
        kind=KIND_INCREASE,
        status=STATUS_AWAITING_PAYMENT,
        old_service="Consulta",
        new_service="Retorno completo",
        pending_edit={"service": "Retorno completo"},
        diff_cents=2500,
        provider_charge_id=f"adj_{uuid4().hex[:8]}",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    fields.update(kw)
    return DepositAdjustment(**fields)


def test_constants_match_the_spec():
    assert (KIND_INCREASE, KIND_DECREASE) == ("increase", "decrease")
    assert {
        STATUS_AWAITING_PAYMENT,
        STATUS_APPLIED,
        STATUS_EXPIRED,
        STATUS_CANCELLED,
        STATUS_ASKED,
        STATUS_REFUND_REQUESTED,
        STATUS_KEPT_CREDIT,
        STATUS_CLINIC_KEPT,
        STATUS_COLLECT_DUE,
        STATUS_CANCELLED_VOID_PENDING,
    } == ADJUSTMENT_STATUSES
    # Follow-up B (2026-10-27): both fit the VARCHAR(24) column (no migration).
    assert (STATUS_COLLECT_DUE, STATUS_CANCELLED_VOID_PENDING) == (
        "collect_due", "cancelled_void_pending",
    )
    assert all(len(status) <= 24 for status in ADJUSTMENT_STATUSES)
    assert STATUS_AWAITING_PAYMENT == "awaiting_payment"
    assert STATUS_REFUND_REQUESTED == "refund_requested"
    assert STATUS_KEPT_CREDIT == "kept_credit"
    assert STATUS_CLINIC_KEPT == "clinic_kept"


async def test_creates_an_adjustment_that_hangs_on_the_deposit(db):
    async with db() as session:
        deposit = await _deposit(session)
        session.add(_adjustment(deposit))
        await session.commit()
        row = await session.scalar(select(DepositAdjustment))

    assert row.deposit_id == deposit.id and row.tenant_id == deposit.tenant_id
    assert row.kind == "increase" and row.status == "awaiting_payment"
    assert row.diff_cents == 2500 and row.pending_edit == {"service": "Retorno completo"}
    assert row.created_at is not None and row.resolved_at is None and row.asked_at is None


async def test_a_deposit_can_have_more_than_one_adjustment(db):
    async with db() as session:
        deposit = await _deposit(session)
        session.add(_adjustment(deposit, status=STATUS_APPLIED))
        session.add(_adjustment(deposit, kind=KIND_DECREASE, provider_charge_id=None))
        await session.commit()
        rows = (await session.scalars(select(DepositAdjustment))).all()
    assert len(rows) == 2


async def test_provider_charge_id_is_unique(db):
    async with db() as session:
        deposit = await _deposit(session)
        session.add(_adjustment(deposit, provider_charge_id="adj_same"))
        session.add(_adjustment(deposit, provider_charge_id="adj_same"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_many_adjustments_without_a_charge_are_fine(db):
    # decrease rows have no Pix: NULL charge ids must not collide on the unique index
    async with db() as session:
        deposit = await _deposit(session)
        for _ in range(2):
            session.add(_adjustment(deposit, kind=KIND_DECREASE, provider_charge_id=None))
        await session.commit()


async def test_tenant_is_required(db):
    async with db() as session:
        deposit = await _deposit(session)
        session.add(_adjustment(deposit, tenant_id=None))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_deposit_is_required(db):
    async with db() as session:
        deposit = await _deposit(session)
        session.add(_adjustment(deposit, deposit_id=None))
        with pytest.raises(IntegrityError):
            await session.commit()


def test_the_new_attention_reason_is_known_and_has_text():
    assert "service_diff_refund_due" in ATTENTION_REASONS
    assert len("service_diff_refund_due") <= 32
    assert "{value}" in REASON_TEXT["service_diff_refund_due"]
    assert "R$" in reason_text("service_diff_refund_due", 2500)


async def test_open_attention_item_accepts_the_new_reason(db):
    async with db() as session:
        deposit = await _deposit(session)
        outcome = await attention.open_attention_item(
            session,
            tenant_id=deposit.tenant_id,
            reason="service_diff_refund_due",
            dedupe_key=attention.attention_dedupe_key(
                "asaas", deposit.provider_charge_id, "service_diff_refund_due"
            ),
            deposit=deposit,
            suggested_refund_cents=2500,
        )
        await session.commit()
        item = await session.scalar(select(PaymentAttentionItem))
    assert outcome.created is True
    assert item.reason == "service_diff_refund_due" and item.suggested_refund_cents == 2500


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


def test_the_revision_is_chained_after_the_min_days_column():
    script = ScriptDirectory.from_config(_config())
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    heads = script.get_heads()
    assert len(heads) == 1
    assert REVISION in {r.revision for r in script.walk_revisions(head=heads[0])}


def test_upgrade_creates_only_the_table_and_downgrade_drops_it():
    up = _offline("up").upper()
    assert "CREATE TABLE DEPOSIT_ADJUSTMENTS" in up
    assert "ALTER TABLE" not in up and "DROP" not in up
    down = _offline("down").upper()
    assert "DROP TABLE DEPOSIT_ADJUSTMENTS" in down


def test_the_model_matches_the_migration_columns():
    sql = _offline("up").lower()
    for column in DepositAdjustment.__table__.columns:
        assert column.name in sql, column.name
