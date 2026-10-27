"""decide_service_change: the pure rule of a service change on a PAID appointment (2026-10-26 spec).

Money is in cents. With `percent_applied=100` the new deposit equals the new price, which keeps the
table below readable; the rounding cases use a real percentage.
"""

import pytest

from secretaria.services.payments.service_change_policy import (
    MIN_DIFFERENCE_CENTS,
    ChangeDecision,
    decide_service_change,
)


def _decide(*, paid=10_000, price=10_000, percent=100, days=10.0, min_days=2) -> ChangeDecision:
    return decide_service_change(
        paid_deposit_cents=paid,
        percent_applied=percent,
        new_price_cents=price,
        days_until=days,
        min_days=min_days,
    )


def test_the_floor_is_ten_reais():
    assert MIN_DIFFERENCE_CENTS == 1000


def test_same_deposit_is_no_change():
    decision = _decide(price=10_000)
    assert decision == ChangeDecision(action="no_change", diff_cents=0)


def test_dearer_service_charges_only_the_difference():
    decision = _decide(price=12_500)
    assert decision.action == "increase_charge" and decision.diff_cents == 2_500


def test_dearer_service_charges_even_on_the_day_of_the_visit():
    # min_days never blocks an increase
    assert _decide(price=12_500, days=0, min_days=30).action == "increase_charge"


def test_dearer_service_under_the_floor_is_free():
    decision = _decide(price=10_999)
    assert decision.action == "increase_free" and decision.diff_cents == 999


def test_dearer_service_exactly_at_the_floor_charges():
    decision = _decide(price=11_000)
    assert decision.action == "increase_charge" and decision.diff_cents == 1_000


def test_cheaper_service_with_time_asks_the_patient():
    decision = _decide(price=7_500, days=10, min_days=2)
    assert decision.action == "decrease_ask" and decision.diff_cents == 2_500


def test_cheaper_service_too_close_to_the_visit_is_kept_by_the_clinic():
    decision = _decide(price=7_500, days=1.5, min_days=2)
    assert decision.action == "decrease_keep" and decision.diff_cents == 2_500


def test_cheaper_service_exactly_min_days_away_still_asks():
    assert _decide(price=7_500, days=2, min_days=2).action == "decrease_ask"


def test_cheaper_service_under_the_floor_is_kept_by_the_clinic():
    decision = _decide(price=9_001, days=10)
    assert decision.action == "decrease_keep" and decision.diff_cents == 999


def test_cheaper_service_exactly_at_the_floor_asks():
    decision = _decide(price=9_000, days=10)
    assert decision.action == "decrease_ask" and decision.diff_cents == 1_000


def test_zero_min_days_always_asks_but_the_floor_still_applies():
    assert _decide(price=7_500, days=0, min_days=0).action == "decrease_ask"
    assert _decide(price=9_500, days=0, min_days=0).action == "decrease_keep"


@pytest.mark.parametrize("price", [None, 0, -100])
def test_unreadable_new_price_blocks_the_change(price):
    decision = _decide(price=price)
    assert decision.action == "blocked" and decision.reason == "price_unavailable"
    assert decision.diff_cents == 0


def test_the_new_deposit_uses_the_percentage_that_was_paid():
    # paid 30% of R$ 200 = R$ 60; the new service costs R$ 300 -> deposit R$ 90 -> diff R$ 30
    decision = _decide(paid=6_000, percent=30, price=30_000)
    assert decision.action == "increase_charge" and decision.diff_cents == 3_000


def test_the_new_deposit_rounds_half_up_to_the_cent():
    # 30% of 10_005 = 3001.5 -> 3002 (half up), so the diff against a paid 3000 is 2
    decision = _decide(paid=3_000, percent=30, price=10_005)
    assert decision.action == "increase_free" and decision.diff_cents == 2
    # 50% of 10_001 = 5000.5 -> 5001 (round() would give 5000)
    assert _decide(paid=5_000, percent=50, price=10_001).diff_cents == 1


def test_a_non_positive_paid_deposit_or_percent_cannot_be_priced():
    assert _decide(paid=0, price=5_000).action == "blocked"
    assert _decide(percent=0, price=5_000).action == "blocked"


def test_diff_is_always_a_magnitude_never_negative():
    for price in (2_000, 5_000, 10_000, 15_000):
        assert _decide(price=price).diff_cents >= 0


def test_decision_is_immutable():
    decision = _decide(price=12_500)
    with pytest.raises(AttributeError):
        decision.diff_cents = 1  # type: ignore[misc]
