"""What stands between a clinic and a working Pix deposit (TASK-071, Plan 4, task 3).

`evaluate_deposit_readiness` lists the pending items of ONE clinic, read from rows only (no call
to Asaas); the badge is green ONLY with proof (a real charge that worked), never because nothing
is missing. Prices are read with the same `parse_brl_to_cents` the deposit gate uses.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import Professional, Tenant
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, readiness
from secretaria.services.payments.failure_reason import FailureReason
from tests.payments_support import make_sessionmaker

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
ALL_APPROVED = ("APPROVED",) * 4


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


def _type(name="Consulta", price="R$ 200,00", **extra):
    return {"name": name, "duration_min": 30, "is_active": True, "price": price, **extra}


async def _seed(
    session,
    *,
    enabled=True,
    timing="before",
    types=None,
    account="connected",
    onboarding=ALL_APPROVED,
    contact_email="clinica@example.test",
    professionals=(),
    percent=30,
):
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinic",
        phone_number_id=str(uuid4())[:12],
        pix_deposit_enabled=enabled,
        pix_deposit_percent=percent,
        deposit_timing=timing,
        contact_email=contact_email,
        appointment_types=types if types is not None else [_type()],
    )
    session.add(tenant)
    await session.flush()
    for name, own_types in professionals:
        session.add(
            Professional(
                id=uuid4(), tenant_id=tenant.id, name=name, is_active=True,
                appointment_types=own_types,
            )
        )
    row = None
    if account is not None:
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
        row.status = account
        row.last_verified_at = NOW if account == "connected" else None
        if onboarding is not None:
            (
                row.onboarding_general,
                row.onboarding_docs,
                row.onboarding_commercial,
                row.onboarding_bank,
            ) = onboarding
    await session.commit()
    return tenant, row


async def _codes(session, tenant, **kwargs) -> dict[str, readiness.ReadinessItem]:
    items = await readiness.evaluate_deposit_readiness(session, tenant_id=tenant.id, **kwargs)
    return {item.code: item for item in items}


async def test_sinal_off(db):
    async with db() as session:
        tenant, _ = await _seed(session, enabled=False)
        found = await _codes(session, tenant)
    assert set(found) == {"sinal_off"}
    assert found["sinal_off"].severity == "blocker"


async def test_sinal_off_has_no_payment_blockers(db):
    """A clinic that switched the deposit off is not told about its Asaas account or prices."""
    async with db() as session:
        tenant, _ = await _seed(session, enabled=False, account=None, types=[_type(price="")])
        found = await _codes(session, tenant)
    assert set(found) == {"sinal_off"}


async def test_after_timing_counts_as_off(db):
    async with db() as session:
        tenant, _ = await _seed(session, timing="after")
        found = await _codes(session, tenant)
    assert set(found) == {"sinal_off"}


async def test_no_account(db):
    async with db() as session:
        tenant, _ = await _seed(session, account=None)
        found = await _codes(session, tenant)
    assert found["no_account"].severity == "blocker"
    assert "account_incomplete" not in found


async def test_revoked_account_is_no_account(db):
    async with db() as session:
        tenant, _ = await _seed(session, account="revoked")
        found = await _codes(session, tenant)
    assert "no_account" in found


async def test_account_incomplete_carries_the_existing_reason(db):
    async with db() as session:
        tenant, row = await _seed(session, account="incomplete")
        row.incomplete_reason = "key_expired_or_disabled"
        await session.commit()
        found = await _codes(session, tenant)
    assert found["account_incomplete"].severity == "blocker"
    assert "no_account" not in found
    assert "chave" in found["account_incomplete"].text.lower()


async def test_no_prices(db):
    async with db() as session:
        tenant, _ = await _seed(session, types=[_type(price=""), _type("Retorno", price=None)])
        found = await _codes(session, tenant)
    assert found["no_prices"].severity == "blocker"
    assert "Sem preço" in found["no_prices"].text
    assert "services_without_price" not in found


async def test_services_without_price_lists_who_does_not_charge(db):
    async with db() as session:
        tenant, _ = await _seed(
            session,
            types=[],
            professionals=[
                ("Dra. Ana", [_type("Consulta", "R$ 300,00"), _type("Retorno", "a combinar")]),
                ("Dr. Beto", [_type("Consulta", "R$ 250,00")]),
            ],
        )
        found = await _codes(session, tenant)
    item = found["services_without_price"]
    assert item.severity == "warning"
    assert item.names == ("Dra. Ana — Retorno",)
    assert "no_prices" not in found


async def test_inactive_services_are_ignored(db):
    async with db() as session:
        tenant, _ = await _seed(
            session,
            types=[_type("Consulta", "R$ 200,00"), _type("Antigo", "", is_active=False)],
        )
        found = await _codes(session, tenant)
    assert "services_without_price" not in found and "no_prices" not in found


@pytest.mark.parametrize(
    ("price", "priced"),
    [
        ("R$ 500,00", True),
        ("3", True),
        ("10", True),
        ("R$ 1.250,50", True),
        ("", False),
        (None, False),
        ("a combinar", False),
        ("R$ 0,00", False),  # a deposit of R$ 0,00 charges nobody
    ],
)
async def test_price_formats(db, price, priced):
    async with db() as session:
        tenant, _ = await _seed(session, types=[_type(price=price)])
        found = await _codes(session, tenant)
    assert ("no_prices" not in found) is priced


async def test_amount_below_the_measured_minimum_is_a_warning(db):
    """Measured on the real Asaas (2026-10-30): the smallest Pix charge is R$ 5,00."""
    assert readiness.MIN_PIX_CENTS == 500
    async with db() as session:
        # R$ 10,00 at 30% = R$ 3,00 -> flagged; R$ 200,00 at 30% = R$ 60,00 -> fine
        low, _ = await _seed(session, types=[_type("Retorno", "R$ 10,00")], percent=30)
        fine, _ = await _seed(session, types=[_type(price="R$ 200,00")], percent=30)
        edge, _ = await _seed(session, types=[_type(price="R$ 50,00")], percent=10)  # R$ 5,00
        found_low = await _codes(session, low)
        found_fine = await _codes(session, fine)
        found_edge = await _codes(session, edge)
    assert found_low["amount_below_minimum"].severity == "warning"
    assert found_low["amount_below_minimum"].names == ("Retorno: sinal de R$ 3,00",)
    assert "R$ 5,00" in found_low["amount_below_minimum"].text
    assert "amount_below_minimum" not in found_fine
    assert "amount_below_minimum" not in found_edge  # exactly the minimum is accepted


async def test_onboarding_pending_when_any_state_is_not_approved(db):
    async with db() as session:
        tenant, _ = await _seed(
            session, onboarding=("APPROVED", "PENDING", "APPROVED", "AWAITING_APPROVAL")
        )
        found = await _codes(session, tenant)
    assert found["onboarding_pending"].severity == "warning"
    assert "onboarding_unreadable" not in found


async def test_onboarding_unreadable_is_info_and_never_approval(db):
    async with db() as session:
        tenant, _ = await _seed(session, onboarding=None)
        found = await _codes(session, tenant)
    assert found["onboarding_unreadable"].severity == "info"
    assert "onboarding_pending" not in found


async def test_unknown_onboarding_value_is_pending(db):
    async with db() as session:
        tenant, _ = await _seed(session, onboarding=("APPROVED", "APPROVED", "APPROVED", "NEW"))
        found = await _codes(session, tenant)
    assert "onboarding_pending" in found


async def test_last_charge_failing_uses_the_stored_reason(db):
    async with db() as session:
        tenant, row = await _seed(session)
        await accounts.record_charge_failure(
            session, tenant_id=tenant.id, provider="asaas",
            reason=FailureReason.qr_unavailable, stage="qr",
        )
        await session.commit()
        found = await _codes(session, tenant)
    assert found["last_charge_failing"].severity == "blocker"
    assert "código Pix" in found["last_charge_failing"].text
    assert "never_tested" not in found


async def test_never_tested(db):
    async with db() as session:
        tenant, _ = await _seed(session)
        found = await _codes(session, tenant)
    assert found["never_tested"].severity == "warning"


async def test_contact_email_and_doctor_emails(db):
    async with db() as session:
        tenant, _ = await _seed(
            session, contact_email=None, professionals=[("Dra. Ana", [_type()])]
        )
        pro = (await session.execute(select(Professional))).scalars().one()
        found = await _codes(session, tenant, professional_emails={})
        found_unknown = await _codes(session, tenant, professional_emails=None)
        found_ok = await _codes(session, tenant, professional_emails={str(pro.id): "a@x.test"})
    assert found["no_contact_email"].severity == "warning"
    assert found["professionals_without_email"].names == ("Dra. Ana",)
    assert "professionals_without_email" not in found_unknown  # brain-api did not answer
    assert "professionals_without_email" not in found_ok


async def test_ok_requires_proof(db):
    """Nothing pending is NOT proof: green only after a real charge worked."""
    async with db() as session:
        tenant, row = await _seed(session)
        found = await _codes(session, tenant)
        assert not [i for i in found.values() if i.severity == "blocker"]
        assert readiness.badge_for(list(found.values()), accounts.charge_health(row)) == "untested"

        await accounts.record_charge_success(session, tenant_id=tenant.id, provider="asaas")
        await session.commit()
        await session.refresh(row)
        found = await _codes(session, tenant)
        assert readiness.badge_for(list(found.values()), accounts.charge_health(row)) == "ok"


async def test_a_blocker_keeps_the_badge_off_even_with_old_proof(db):
    async with db() as session:
        tenant, row = await _seed(session, types=[_type(price="")])
        await accounts.record_charge_success(session, tenant_id=tenant.id, provider="asaas")
        await session.commit()
        await session.refresh(row)
        found = await _codes(session, tenant)
    assert readiness.badge_for(list(found.values()), accounts.charge_health(row)) == "untested"


async def test_failing_charge_makes_the_badge_failing(db):
    async with db() as session:
        tenant, row = await _seed(session)
        await accounts.record_charge_failure(
            session, tenant_id=tenant.id, provider="asaas",
            reason=FailureReason.pix_key_missing, stage="charge",
        )
        await session.commit()
        await session.refresh(row)
        found = await _codes(session, tenant)
    assert readiness.badge_for(list(found.values()), accounts.charge_health(row)) == "failing"


async def test_reconnecting_with_a_new_key_withdraws_the_proof(db):
    """A success recorded with the OLD key is not proof for the new one."""
    async with db() as session:
        tenant, row = await _seed(session)
        await accounts.record_charge_success(session, tenant_id=tenant.id, provider="asaas")
        await session.commit()
        await session.refresh(row)
        assert accounts.charge_health(row) == "ok"

        # the key was replaced
        row.credentials_changed_at = datetime.now(UTC) + timedelta(seconds=5)
        await session.commit()
        assert accounts.charge_health(row) == "untested"

        # a charge that worked with the new key
        row.last_charge_ok_at = datetime.now(UTC) + timedelta(seconds=10)
        await session.commit()
        assert accounts.charge_health(row) == "ok"


async def test_an_old_failure_that_a_success_had_buried_stays_buried_after_a_new_key(db):
    """Failure Monday, success Tuesday, new key Wednesday: nothing has failed with the new key."""
    async with db() as session:
        tenant, row = await _seed(session)
        row.last_charge_failure_reason = "pix_key_missing"
        row.last_charge_failure_at = datetime(2026, 10, 26, 12, tzinfo=UTC)
        row.last_charge_ok_at = datetime(2026, 10, 27, 12, tzinfo=UTC)
        row.credentials_changed_at = datetime(2026, 10, 28, 12, tzinfo=UTC)
        await session.commit()
        assert accounts.charge_health(row) == "untested"

        # a failure NEWER than the success is not buried: it stays
        row.last_charge_failure_at = datetime(2026, 10, 27, 18, tzinfo=UTC)
        assert accounts.charge_health(row) == "failing"


async def test_deposit_on_but_charged_after_has_its_own_sentence(db):
    async with db() as session:
        tenant, _ = await _seed(session, timing="after")
        found = await _codes(session, tenant)
    assert "depois" in found["sinal_off"].text
    assert "desligado" not in found["sinal_off"].text


async def test_a_numeric_price_is_priced_like_the_gate_does(db):
    async with db() as session:
        tenant, _ = await _seed(session, types=[_type(price=200)])
        found = await _codes(session, tenant)
    assert "no_prices" not in found
