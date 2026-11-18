"""TASK-085 R / S7a - the clinic's edit undone after the engine created the Pix of its difference.

The patient never saw that Pix (it is sent after the commit). `apply_staff_edit` voids it through
`service_change.void_unseen_charge` whatever made the edit roll back (the money decision moved
under the lock, or any later failure), and a void the provider refuses becomes ONE clinic item
(`unseen_charge_to_void`) written AFTER the rollback - never only a log line.
"""

# ruff: noqa: F811

from datetime import timedelta

import pytest
from sqlalchemy import select

from secretaria.models import PaymentAttentionItem
from secretaria.models.payment_attention_item import REASON_UNSEEN_CHARGE_TO_VOID
from secretaria.services import staff_appointment_edit as sae
from secretaria.services.payments import service_change
from secretaria.services.payments.provider import ProviderError
from secretaria.services.staff_appointment_edit import StaffEditError
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment
from tests.test_sinal_trocas_s4_staff import _timing_sequence
from tests.test_staff_appointment_edit import (  # noqa: F401 - autouse + fixtures
    _adjustments,
    _calendars,
    _edit,
    _paid_world,
    _req,
    _utc,
    provider,
)


async def _items(db):
    async with db() as session:
        return list((await session.scalars(select(PaymentAttentionItem))).all())


def _spy_void(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = service_change.void_unseen_charge

    async def _spy(session, *, tenant_id, charge):
        calls.append(charge.charge_id)
        return await real(session, tenant_id=tenant_id, charge=charge)

    monkeypatch.setattr(service_change, "void_unseen_charge", _spy)
    return calls


async def test_a_moved_decision_voids_through_the_public_helper(db, provider, monkeypatch):
    calls = _spy_void(monkeypatch)
    world = await _paid_world(db)
    _timing_sequence(monkeypatch, True, False)

    with pytest.raises(StaffEditError):
        await _edit(db, world, _req(service="Avaliação", start_at=world.start_at + timedelta(1)))

    assert calls == ["pay_diff_1"]
    assert provider.called("cancel_charge") == 1
    assert await _items(db) == [] and await _adjustments(db) == []


async def test_a_refused_void_of_a_moved_decision_opens_one_item(db, provider, monkeypatch):
    provider.cancel_error = ProviderError("cancel_failed", status_code=500)
    world = await _paid_world(db)
    _timing_sequence(monkeypatch, True, False)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Avaliação", start_at=world.start_at + timedelta(1)))

    assert err.value.extra["reason"] == "deposit_not_paid"  # the clinic's answer is unchanged
    [item] = await _items(db)
    assert item.reason == REASON_UNSEEN_CHARGE_TO_VOID
    assert (item.tenant_id, item.provider_charge_id, item.amount_cents) == (
        world.tenant.id,
        "pay_diff_1",
        5000,
    )
    assert item.deposit_id is None and item.patient_id == world.patient.id
    assert await _adjustments(db) == []  # the edit itself is undone


async def test_a_later_failure_voids_the_pix_the_engine_created(db, provider, monkeypatch):
    """An "after" clinic: the engine wrote the dearer change AND created its Pix; something after
    it fails before the commit - the Pix nobody saw is voided (it used to stay at the provider)."""
    calls = _spy_void(monkeypatch)
    world = await _paid_world(db)

    def _boom(appointment):
        raise RuntimeError("late failure")

    monkeypatch.setattr(sae, "appointment_email_version", _boom)

    with pytest.raises(RuntimeError):
        await _edit(db, world, _req(service="Avaliação"))

    assert calls == ["pay_diff_1"]
    assert provider.called("cancel_charge") == 1
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, _utc(row.start_at)) == ("Consulta", world.start_at)
    assert await _adjustments(db) == [] and await _items(db) == []


async def test_a_refused_void_after_a_later_failure_opens_one_item(db, provider, monkeypatch):
    provider.cancel_error = ProviderError("cancel_failed", status_code=500)
    world = await _paid_world(db)

    def _boom(appointment):
        raise RuntimeError("late failure")

    monkeypatch.setattr(sae, "appointment_email_version", _boom)

    with pytest.raises(RuntimeError):
        await _edit(db, world, _req(service="Avaliação"))

    assert [item.reason for item in await _items(db)] == [REASON_UNSEEN_CHARGE_TO_VOID]


async def test_an_edit_without_a_pix_never_calls_the_provider_to_void(db, provider, monkeypatch):
    calls = _spy_void(monkeypatch)
    world = await _paid_world(db)

    def _boom(appointment):
        raise RuntimeError("late failure")

    monkeypatch.setattr(sae, "appointment_email_version", _boom)

    with pytest.raises(RuntimeError):
        await _edit(db, world, _req(service="Retorno"))  # cheaper: a question, no Pix

    assert calls == [] and provider.called("cancel_charge") == 0


def test_the_private_void_of_the_staff_edit_is_gone():
    assert not hasattr(sae, "_void_unseen_pix")
