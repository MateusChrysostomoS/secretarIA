"""TASK-093 Plano 2 / Task 1: funções puras do aviso neutro de pagamento."""

from datetime import UTC, date, datetime
from uuid import UUID

from secretaria.services.entitlements_client import EntitlementSummary
from secretaria.services.payment_notice import (
    PAYMENT_NOTICE_TEXT,
    clinic_today,
    notice_key,
    seconds_until_clinic_midnight,
    should_notify,
)

TENANT = UUID("11111111-1111-1111-1111-111111111111")


def _summary(*, active=True, secretaria_enabled=True) -> EntitlementSummary:
    return EntitlementSummary(
        tenant_id=str(TENANT),
        status="active" if active else "past_due",
        active=active,
        secretaria_enabled=secretaria_enabled,
        plan=None,
        secretaria_tier=None,
        addons={},
        limits={},
    )


def test_text_is_the_fixed_neutral_notice():
    assert PAYMENT_NOTICE_TEXT == (
        "No momento esta clínica não está atendendo por aqui. "
        "Para falar com a clínica, entre em contato por telefone."
    )


def test_should_notify_none_is_silence():
    assert should_notify(None) is False


def test_should_notify_paid_clinic_is_silence():
    assert should_notify(_summary()) is False


def test_should_notify_inactive():
    assert should_notify(_summary(active=False)) is True


def test_should_notify_secretaria_off():
    assert should_notify(_summary(secretaria_enabled=False)) is True


def test_clinic_today_uses_clinic_timezone():
    # 02h30 UTC de 25/11 = 23h30 de 24/11 em São Paulo (UTC-3).
    now = datetime(2026, 11, 25, 2, 30, tzinfo=UTC)
    assert clinic_today(now, "America/Sao_Paulo") == date(2026, 11, 24)
    assert clinic_today(now, "UTC") == date(2026, 11, 25)


def test_clinic_today_falls_back_on_missing_or_bad_timezone():
    now = datetime(2026, 11, 25, 2, 30, tzinfo=UTC)
    assert clinic_today(now, None) == date(2026, 11, 24)
    assert clinic_today(now, "Nao/Existe") == date(2026, 11, 24)


def test_seconds_until_midnight_just_before():
    # 23h59 em São Paulo = 02h59 UTC do dia seguinte.
    now = datetime(2026, 11, 25, 2, 59, tzinfo=UTC)
    assert 60 <= seconds_until_clinic_midnight(now, "America/Sao_Paulo") <= 120


def test_seconds_until_midnight_has_a_floor_of_60():
    # 23h59m59s em São Paulo: falta 1s, mas o mínimo é 60.
    now = datetime(2026, 11, 25, 2, 59, 59, tzinfo=UTC)
    assert seconds_until_clinic_midnight(now, "America/Sao_Paulo") == 60


def test_seconds_until_midnight_mid_day():
    # 12h00 em São Paulo: faltam 12h.
    now = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
    assert seconds_until_clinic_midnight(now, "America/Sao_Paulo") == 12 * 3600


def test_notice_key_exact_format():
    assert (
        notice_key(TENANT, "5511999990000", date(2026, 11, 24))
        == f"payment_notice:{TENANT}:5511999990000:2026-11-24"
    )
