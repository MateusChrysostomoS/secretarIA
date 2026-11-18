"""TASK-085 R / S7a - integration fixes on the money engine (services/payments/service_change.py).

Contract: tasks/TASK-085/sinal-trocas/contrato.md (§2.2, §3.6, §5). Pinned here:

* a change the CLINIC asked for (`pending_edit["source"] == "hub"`) never spends the patient's
  reschedule allowance when its Pix is paid - in the full-edit branch (`mode` suspend) AND in the
  legacy branch (`mode` NULL); the patient's own change still counts (S1);
* `void_unseen_charge`: the provider only (no session write), "ok" / "failed"; a charge that could
  not be voided becomes ONE clinic item (`unseen_charge_to_void`) with its own sentence;
* `confirm_card_line_text(..., reserved=False)`: the "before" line without the promise of both
  times reserved (the legacy fallback) - no caller splits the wording any more;
* the hooks run after a commit are independent: a failing hook never makes the next one (or the
  caller) read an expired tenant, and never undoes what an earlier hook did.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.models import Appointment, PaymentAttentionItem, PixDeposit, Tenant
from secretaria.models.deposit_adjustment import (
    MODE_SUSPEND,
    STATUS_APPLIED,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import REASON_UNSEEN_CHARGE_TO_VOID
from secretaria.services import booking_hold
from secretaria.services.appointment_status import SOURCE_FLOW, SOURCE_HUB
from secretaria.services.payments import notify, service_change
from secretaria.services.payments.attention_view import reason_text
from secretaria.services.payments.provider import ChargeState, PaymentStatus, ProviderError
from tests import test_service_change as base
from tests.test_sinal_trocas_s1_engine import _seed
from tests.test_sinal_trocas_s1_unpaid import CLOCK, _webhook

NOW = base.NOW
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks


@pytest.fixture(autouse=True)
def hold_clock(monkeypatch):
    CLOCK["now"] = NOW
    monkeypatch.setattr(booking_hold, "_now", lambda: CLOCK["now"])


@pytest.fixture
def replans(monkeypatch):
    seen: list = []

    async def fake(appointment_id, **kw):
        seen.append(appointment_id)
        return 0

    from secretaria.services import reminder_hooks

    monkeypatch.setattr(reminder_hooks, "after_appointment_rescheduled", fake)
    return seen


@pytest.fixture
def sent(monkeypatch):
    out: list = []

    async def fake_deliver(session, tenant, patient, messages, **kw):
        out.append(list(messages))

    monkeypatch.setattr(notify, "deliver", fake_deliver)
    return out


async def _moved_request(db, seeded, *, source):
    """A dearer change that ALSO moves the visit a day later, asked by `source`."""
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        row = await session.get(Appointment, seeded[2].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        new_start = row.start_at + timedelta(days=1)
        result = await service_change.request_paid_change(
            session,
            tenant=tenant,
            appointment=row,
            deposit=deposit,
            new_service="Avaliação",
            pending_edit={
                **base._edit(row, "Avaliação", 60),
                "start_at": new_start,
                "end_at": new_start + timedelta(minutes=60),
                "time_changed": True,
            },
            now=NOW,
            source=source,
        )
        await session.commit()
    return result, new_start


async def _pay(db, seeded, provider, adjustment_id):
    provider.state = ChargeState(
        status=PaymentStatus.PAID,
        paid_amount_cents=3000,
        external_reference=str(adjustment_id),
        paid_at=NOW + timedelta(minutes=5),
    )
    return await _webhook(db, seeded[0].id, PaymentStatus.PAID, at=NOW + timedelta(minutes=5))


async def _reschedules(db, seeded) -> int:
    async with db() as session:
        return (await session.get(PixDeposit, seeded[3].id)).reschedule_count or 0


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=NOW.tzinfo)


# --------------------------------------------------------------------------------------------
# 2. a clinic-initiated move never consumes the patient's allowance
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "counted"), [(SOURCE_HUB, 0), (SOURCE_FLOW, 1)])
async def test_a_paid_suspended_move_counts_only_when_the_patient_asked(
    db, provider, sent, replans, source, counted
):
    seeded = await _seed(db, timing="before")
    created, new_start = await _moved_request(db, seeded, source=source)
    assert created.kind == "suspended"

    result = await _pay(db, seeded, provider, created.adjustment_id)

    assert result.outcome == "adjustment_applied"
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
    assert _utc(row.start_at) == _utc(new_start)  # the move was applied
    assert await _reschedules(db, seeded) == counted


@pytest.mark.parametrize(("source", "counted"), [(SOURCE_HUB, 0), (SOURCE_FLOW, 1)])
async def test_a_paid_legacy_move_counts_only_when_the_patient_asked(
    db, provider, sent, replans, monkeypatch, source, counted
):
    # The legacy deferred mode (`mode` NULL): a "before" clinic without room for a useful Pix.
    monkeypatch.setattr(service_change, "timing_is_before", lambda tenant: True)
    monkeypatch.setattr(service_change, "suspend_pix_expiry", lambda *a, **k: None)
    seeded = await _seed(db, timing="before")
    created, new_start = await _moved_request(db, seeded, source=source)
    assert created.kind == "awaiting_payment"
    async with db() as session:
        assert (await session.get(DepositAdjustment, created.adjustment_id)).mode is None

    result = await _pay(db, seeded, provider, created.adjustment_id)

    assert result.outcome == "adjustment_applied"
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
    assert _utc(row.start_at) == _utc(new_start)
    assert await _reschedules(db, seeded) == counted


# --------------------------------------------------------------------------------------------
# 3. the public void of a charge nobody saw
# --------------------------------------------------------------------------------------------


async def _unseen(db, provider):
    """An "after" clinic's applied change: its Pix exists (the caller is about to roll back)."""
    seeded = await _seed(db, timing="after")
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        row = await session.get(Appointment, seeded[2].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        result = await service_change.request_paid_change(
            session,
            tenant=tenant,
            appointment=row,
            deposit=deposit,
            new_service="Avaliação",
            pending_edit=base._edit(row, "Avaliação", 60),
            now=NOW,
        )
        assert result.kind == "applied_awaiting_payment"
        charge = await service_change.unseen_charge_of(
            session, tenant=tenant, deposit=deposit, result=result
        )
        await session.rollback()
    return seeded, charge


async def test_the_unseen_charge_is_read_before_the_rollback(db, provider):
    seeded, charge = await _unseen(db, provider)
    assert charge is not None
    assert (charge.tenant_id, charge.provider, charge.charge_id) == (
        seeded[0].id,
        "asaas",
        "pay_diff_1",
    )
    assert (charge.amount_cents, charge.patient_id) == (3000, seeded[1].id)
    assert charge.service_label == "Avaliação"


async def test_void_unseen_charge_calls_only_the_provider(db, provider):
    seeded, charge = await _unseen(db, provider)
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        outcome = await service_change.void_unseen_charge(
            session, tenant_id=tenant.id, charge=charge
        )
        assert not session.new and not session.dirty and not session.deleted
    assert outcome == "ok"
    assert ("cancel_charge", "pay_diff_1") in provider.calls


async def test_void_unseen_charge_reports_a_failure_without_raising(db, provider):
    seeded, charge = await _unseen(db, provider)
    provider.cancel_error = ProviderError("cancel_failed", status_code=500)
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        outcome = await service_change.void_unseen_charge(
            session, tenant_id=tenant.id, charge=charge
        )
        assert not session.new and not session.dirty
    assert outcome == "failed"


async def test_a_charge_that_could_not_be_voided_is_one_clinic_item(db, provider):
    seeded, charge = await _unseen(db, provider)
    for _ in range(2):  # a retry never opens a second item
        async with db() as session:
            await service_change.open_unvoided_charge_item(session, charge)
            await session.commit()
    async with db() as session:
        [item] = (await session.scalars(select(PaymentAttentionItem))).all()
    assert item.reason == REASON_UNSEEN_CHARGE_TO_VOID
    assert len(REASON_UNSEEN_CHARGE_TO_VOID) <= 32
    assert (item.tenant_id, item.provider_charge_id, item.amount_cents) == (
        seeded[0].id,
        "pay_diff_1",
        3000,
    )
    assert item.deposit_id is None and item.patient_id == seeded[1].id
    text = reason_text(item.reason, item.suggested_refund_cents, item.amount_cents)
    assert "R$ 30,00" in text and "cancel" in text.lower()


# --------------------------------------------------------------------------------------------
# 5. the card line says what the engine will do - no caller splits the wording
# --------------------------------------------------------------------------------------------


def test_the_before_card_line_promises_the_reservation_only_when_there_is_room():
    reserved = service_change.confirm_card_line_text("increase_charge", 5000, timing="before")
    plain = service_change.confirm_card_line_text(
        "increase_charge", 5000, timing="before", reserved=False
    )
    assert "reservado" in reserved
    assert plain is not None and "reservad" not in plain and "guardada" not in plain
    assert plain.startswith("Diferença do sinal: R$ 50,00. Ao confirmar, enviamos o Pix")
    assert plain.endswith("só muda depois do pagamento.")
    # "after" never promised a reservation: the flag changes nothing there.
    assert service_change.confirm_card_line_text(
        "increase_charge", 5000, timing="after", reserved=False
    ) == service_change.confirm_card_line_text("increase_charge", 5000, timing="after")


def test_the_worker_no_longer_splits_the_engines_wording():
    from secretaria.workers.shared import deposit

    assert not hasattr(deposit, "_RESERVED_SENTENCE")


# --------------------------------------------------------------------------------------------
# 7. the hooks after a commit are independent
# --------------------------------------------------------------------------------------------


async def _expired_adjustment(db, provider):
    seeded = await _seed(db, timing="after")
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        row = await session.get(Appointment, seeded[2].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        result = await service_change.request_paid_change(
            session,
            tenant=tenant,
            appointment=row,
            deposit=deposit,
            new_service="Avaliação",
            pending_edit=base._edit(row, "Avaliação", 60),
            now=NOW,
        )
        await session.commit()
    return seeded, result.adjustment_id


async def test_a_failing_hook_never_breaks_the_next_one(db, provider, monkeypatch):
    seeded, adjustment_id = await _expired_adjustment(db, provider)
    seen: list = []

    async def _failing(session, tenant, notice):
        tenant.clinic_name = "half-written"
        raise RuntimeError("boom")

    async def _next(session, tenant, notice):
        # Reads the tenant and the database like every real hook does.
        adjustment = await session.get(DepositAdjustment, notice.adjustment_id)
        seen.append((tenant.id, tenant.clinic_name, adjustment.id))

    monkeypatch.setattr(service_change, "ADJUSTMENT_EXPIRED_HOOKS", [_failing, _next])
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        await service_change.run_expired_hooks(session, tenant, adjustment_id)
        # The caller keeps using the same tenant after the hooks (run_post_commit does).
        assert tenant.id == seeded[0].id

    assert seen == [(seeded[0].id, "Clinic", adjustment_id)]


async def test_a_failing_hook_never_undoes_what_an_earlier_one_did(db, provider, monkeypatch):
    seeded, adjustment_id = await _expired_adjustment(db, provider)

    async def _writes(session, tenant, notice):
        tenant.clinic_name = "Clinic renamed by a hook"

    async def _failing(session, tenant, notice):
        raise RuntimeError("boom")

    monkeypatch.setattr(service_change, "ADJUSTMENT_EXPIRED_HOOKS", [_writes, _failing])
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        await service_change.run_expired_hooks(session, tenant, adjustment_id)
    async with db() as session:
        assert (await session.get(Tenant, seeded[0].id)).clinic_name == "Clinic renamed by a hook"


async def test_the_move_followup_still_runs_after_a_failing_applied_hook(
    db, provider, sent, replans, monkeypatch
):
    seeded = await _seed(db, timing="before")
    created, _new_start = await _moved_request(db, seeded, source=SOURCE_FLOW)
    followups: list = []

    async def _failing(session, tenant, notice):
        tenant.clinic_name = "half-written"
        raise RuntimeError("boom")

    async def _after_move(session, tenant, adjustment_id, *, redis=None):
        followups.append((tenant.id, tenant.clinic_name))

    monkeypatch.setattr(service_change, "ADJUSTMENT_APPLIED_HOOKS", [_failing])
    monkeypatch.setattr(service_change, "after_paid_move", _after_move)

    result = await _pay(db, seeded, provider, created.adjustment_id)

    assert result.outcome == "adjustment_applied"
    assert followups == [(seeded[0].id, "Clinic")]
    async with db() as session:
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
    assert (adjustment.mode, adjustment.status) == (MODE_SUSPEND, STATUS_APPLIED)


async def test_a_failing_unpaid_notice_still_replans_the_old_visit(db, provider, monkeypatch):
    """`run_unpaid_followup` rolls back a failed notice; the re-plan that follows reads
    `tenant.id` - never an expired row."""
    seeded = await _seed(db, timing="before")
    created, _new_start = await _moved_request(db, seeded, source=SOURCE_FLOW)
    replanned: list = []

    async def _deliver(session, tenant, patient, messages, **kw):
        tenant.clinic_name = "half-written"
        raise RuntimeError("send failed")

    async def _followup(tenant_id, appointment_id):
        replanned.append((tenant_id, appointment_id))

    monkeypatch.setattr(notify, "deliver", _deliver)
    monkeypatch.setattr(service_change, "followup_kept_old", _followup)
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        await service_change.run_unpaid_followup(
            session, tenant, created.adjustment_id, "kept_old", now=NOW
        )
    assert replanned == [(seeded[0].id, seeded[2].id)]


async def test_an_unknown_id_is_never_a_charge(db, provider):
    seeded = await _seed(db, timing="after")
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        missing = service_change.ServiceChangeResult(
            kind="applied_awaiting_payment", adjustment_id=uuid4()
        )
        assert (
            await service_change.unseen_charge_of(
                session, tenant=tenant, deposit=deposit, result=missing
            )
            is None
        )
        blocked = service_change.ServiceChangeResult(kind="blocked")
        assert (
            await service_change.unseen_charge_of(
                session, tenant=tenant, deposit=deposit, result=blocked
            )
            is None
        )
