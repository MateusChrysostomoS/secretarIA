"""open_attention_item: the ONLY writer of `payment_attention_items` (spec section 10A.1)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from secretaria.models import PaymentAttentionItem, PaymentWebhookEvent, PixDeposit, Tenant
from secretaria.models.payment_attention_item import ATTENTION_REASONS
from secretaria.models.pix_deposit import (
    FULFILLMENT_CONFIRMED,
    FULFILLMENT_NEEDS_ATTENTION,
    FULFILLMENT_NONE,
    FULFILLMENT_RESOLVED,
    PixDepositStatus,
)
from secretaria.services.payments import attention
from secretaria.services.payments.db_utils import insert_ignore
from tests.payments_support import make_sessionmaker

PAID_AT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _tenant(session) -> Tenant:
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    session.add(tenant)
    await session.flush()
    return tenant


async def _deposit(session, tenant, **kw) -> PixDeposit:
    fields = dict(
        tenant_id=tenant.id,
        amount_cents=6000,
        percent_applied=30,
        provider="asaas",
        provider_charge_id="pay_1",
        status=PixDepositStatus.PAID,
        paid_at=PAID_AT,
    )
    fields.update(kw)
    deposit = PixDeposit(**fields)
    session.add(deposit)
    await session.flush()
    return deposit


async def _count(session) -> int:
    return await session.scalar(select(func.count()).select_from(PaymentAttentionItem))


def test_every_reason_of_the_spec_is_known():
    assert ATTENTION_REASONS == {
        "late_slot_taken",
        "paid_then_cancelled",
        "refund_due_policy",
        "duplicate_payment",
        "amount_under",
        "amount_over",
        "orphan_payment",
        "reference_mismatch",
        "promotion_failed",
        "refunded_externally",
    }


def test_dedupe_key_shape():
    key = attention.attention_dedupe_key("asaas", "pay_1", "amount_under")
    assert key == "asaas:pay_1:amount_under"
    assert len(attention.attention_dedupe_key("asaas", "x" * 500, "amount_under")) <= 220


async def test_opens_one_item_inheriting_the_deposit_and_flags_it(db):
    async with db() as session:
        tenant = await _tenant(session)
        deposit = await _deposit(session, tenant, fulfillment_status=FULFILLMENT_NONE)
        outcome = await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason="amount_under",
            dedupe_key=attention.attention_dedupe_key("asaas", "pay_1", "amount_under"),
            deposit=deposit,
            paid_amount_cents=1000,
            service_label="Consulta",
        )
        await session.commit()
        item = await session.scalar(select(PaymentAttentionItem))

    assert outcome.created is True and outcome.item_id == item.id
    assert (item.provider, item.provider_charge_id) == ("asaas", "pay_1")
    assert (item.amount_cents, item.paid_amount_cents) == (6000, 1000)
    assert item.deposit_id == deposit.id and item.status == "open"
    assert item.service_label == "Consulta" and item.suggested_refund_cents is None
    assert deposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION


async def test_replay_of_the_same_situation_never_duplicates(db):
    async with db() as session:
        tenant = await _tenant(session)
        deposit = await _deposit(session, tenant)
        key = attention.attention_dedupe_key("asaas", "pay_1", "late_slot_taken")
        first = await attention.open_attention_item(
            session, tenant_id=tenant.id, reason="late_slot_taken", dedupe_key=key, deposit=deposit
        )
        second = await attention.open_attention_item(
            session, tenant_id=tenant.id, reason="late_slot_taken", dedupe_key=key, deposit=deposit
        )
        await session.commit()
        assert (first.created, second.created) == (True, False)
        assert second.item_id is None
        assert await _count(session) == 1


async def test_two_reasons_on_one_charge_are_two_items(db):
    async with db() as session:
        tenant = await _tenant(session)
        deposit = await _deposit(session, tenant)
        for reason in ("amount_over", "paid_then_cancelled"):
            outcome = await attention.open_attention_item(
                session,
                tenant_id=tenant.id,
                reason=reason,
                dedupe_key=attention.attention_dedupe_key("asaas", "pay_1", reason),
                deposit=deposit,
            )
            assert outcome.created is True
        await session.commit()
        assert await _count(session) == 2


async def test_a_deposit_of_another_tenant_is_refused_and_nothing_is_written(db):
    async with db() as session:
        mine, theirs = await _tenant(session), await _tenant(session)
        foreign = await _deposit(session, theirs)
        with pytest.raises(ValueError, match="another tenant"):
            await attention.open_attention_item(
                session,
                tenant_id=mine.id,
                reason="amount_under",
                dedupe_key="k",
                deposit=foreign,
            )
        await session.commit()
        assert await _count(session) == 0
        assert foreign.fulfillment_status == FULFILLMENT_NONE


async def test_the_same_dedupe_key_may_exist_in_two_tenants(db):
    async with db() as session:
        a, b = await _tenant(session), await _tenant(session)
        for tenant in (a, b):
            outcome = await attention.open_attention_item(
                session,
                tenant_id=tenant.id,
                reason="orphan_payment",
                dedupe_key="asaas:pay_1:orphan_payment",
                provider="asaas",
                provider_charge_id="pay_1",
            )
            assert outcome.created is True
        await session.commit()
        assert await _count(session) == 2


async def test_an_orphan_item_needs_no_deposit(db):
    async with db() as session:
        tenant = await _tenant(session)
        outcome = await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason="orphan_payment",
            dedupe_key="asaas:pay_9:orphan_payment",
            provider="asaas",
            provider_charge_id="pay_9",
            paid_amount_cents=6000,
            paid_at=PAID_AT,
        )
        await session.commit()
        item = await session.scalar(select(PaymentAttentionItem))
    assert outcome.created is True
    assert item.deposit_id is None and item.paid_amount_cents == 6000


async def test_an_unknown_reason_is_a_programming_error(db):
    async with db() as session:
        tenant = await _tenant(session)
        with pytest.raises(ValueError, match="unknown attention reason"):
            await attention.open_attention_item(
                session, tenant_id=tenant.id, reason="refund_it", dedupe_key="k", provider="asaas"
            )


async def test_mark_refund_seen_touches_only_this_tenants_open_items_once(db):
    async with db() as session:
        tenant, other = await _tenant(session), await _tenant(session)
        deposit = await _deposit(session, tenant)
        other_deposit = await _deposit(session, other, provider_charge_id="pay_2")
        for owner, dep, charge in ((tenant, deposit, "pay_1"), (other, other_deposit, "pay_2")):
            await attention.open_attention_item(
                session,
                tenant_id=owner.id,
                reason="refund_due_policy",
                dedupe_key=f"asaas:{charge}:refund_due_policy",
                deposit=dep,
            )
        now = datetime(2026, 9, 30, tzinfo=UTC)
        assert await attention.mark_refund_seen(
            session, tenant_id=tenant.id, deposit_id=deposit.id, now=now
        ) is True
        assert await attention.mark_refund_seen(
            session, tenant_id=tenant.id, deposit_id=deposit.id, now=now
        ) is False  # already recorded
        assert await attention.mark_refund_seen(
            session, tenant_id=tenant.id, deposit_id=other_deposit.id, now=now
        ) is False  # not this tenant's deposit
        await session.commit()
        seen = (await session.scalars(select(PaymentAttentionItem.provider_refund_seen_at))).all()
    assert sorted(x is not None for x in seen) == [False, True]


async def test_has_open_item_is_tenant_scoped(db):
    async with db() as session:
        tenant, other = await _tenant(session), await _tenant(session)
        deposit = await _deposit(session, tenant)
        assert await attention.has_open_item(
            session, tenant_id=tenant.id, deposit_id=deposit.id
        ) is False
        await attention.open_attention_item(
            session, tenant_id=tenant.id, reason="amount_over", dedupe_key="k", deposit=deposit
        )
        assert await attention.has_open_item(
            session, tenant_id=tenant.id, deposit_id=deposit.id
        ) is True
        assert await attention.has_open_item(
            session, tenant_id=other.id, deposit_id=deposit.id
        ) is False


def test_needs_attention_is_never_overwritten_by_a_later_fulfillment_step():
    deposit = PixDeposit(tenant_id=uuid4(), amount_cents=1, percent_applied=1)
    deposit.fulfillment_status = FULFILLMENT_NEEDS_ATTENTION
    assert attention.mark_fulfillment(deposit, FULFILLMENT_CONFIRMED) is False
    assert deposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION
    assert attention.mark_fulfillment(deposit, FULFILLMENT_RESOLVED) is True
    assert deposit.fulfillment_status == FULFILLMENT_RESOLVED


async def test_insert_ignore_returns_the_id_once_then_none(db):
    async with db() as session:
        tenant = await _tenant(session)
        values = dict(tenant_id=tenant.id, provider="asaas", event_id="evt_1", event_type="X")
        key = ["tenant_id", "provider", "event_id"]
        first = await insert_ignore(session, PaymentWebhookEvent, values, index_elements=key)
        again = await insert_ignore(session, PaymentWebhookEvent, values, index_elements=key)
        await session.commit()
        total = await session.scalar(select(func.count()).select_from(PaymentWebhookEvent))
    assert first is not None and again is None and total == 1
