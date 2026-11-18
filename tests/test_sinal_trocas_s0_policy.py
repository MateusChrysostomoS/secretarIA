"""TASK-085 R S0 - the pure pieces of the rule: the new sinal is always new price x percentage
paid (TASK-085 R D-R12 revoked D-R3's "a convênio flagged no deposit = new sinal 0": a convênio
never exempts the sinal), and the ONE predicate "did a money field change?" (service, unit,
doctor, convênio - D-R1/D-R3) shared by the patient's and the clinic's edit paths."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.services.payments.service_change_policy import (
    MONEY_FIELD_DOCTOR,
    MONEY_FIELD_INSURANCE,
    MONEY_FIELD_SERVICE,
    MONEY_FIELD_UNIT,
    MONEY_FIELDS,
    ChangeDecision,
    MoneyTerms,
    decide_service_change,
    money_fields_changed,
)


def _decide(*, paid=6_000, price=20_000, percent=30, days=10.0, min_days=2):
    return decide_service_change(
        paid_deposit_cents=paid,
        percent_applied=percent,
        new_price_cents=price,
        days_until=days,
        min_days=min_days,
    )


# --- the new sinal is always price x percentage (D-R12) -----------------------------------


def test_the_new_booking_always_charges_a_sinal():
    assert _decide(price=30_000) == ChangeDecision(action="increase_charge", diff_cents=3_000)
    assert _decide(price=20_000) == ChangeDecision(action="no_change", diff_cents=0)


@pytest.mark.parametrize("price", [None, 0])
def test_an_unreadable_new_price_blocks_the_change(price):
    """No "convênio without sinal" escape any more (D-R12): without a readable price there is
    no new sinal to compare, so the change is blocked as before D-R3."""
    decision = _decide(price=price)
    assert decision == ChangeDecision(action="blocked", diff_cents=0, reason="price_unavailable")


# --- money_fields_changed ---------------------------------------------------------------


def _terms(**kw) -> MoneyTerms:
    base = {"service": "Consulta", "professional_id": None, "unit_id": None, "insurance": None}
    base.update(kw)
    return MoneyTerms(**base)


def test_the_four_money_fields_are_frozen():
    assert MONEY_FIELDS == (
        MONEY_FIELD_SERVICE,
        MONEY_FIELD_UNIT,
        MONEY_FIELD_DOCTOR,
        MONEY_FIELD_INSURANCE,
    ) == ("service", "unit", "doctor", "insurance")


def test_nothing_changed_is_the_empty_set():
    assert money_fields_changed(_terms(), _terms()) == frozenset()


def test_each_field_is_reported_by_its_own_name():
    doctor, unit = uuid4(), uuid4()
    assert money_fields_changed(_terms(), _terms(service="Avaliação")) == {"service"}
    assert money_fields_changed(_terms(), _terms(unit_id=unit)) == {"unit"}
    assert money_fields_changed(_terms(), _terms(professional_id=doctor)) == {"doctor"}
    assert money_fields_changed(_terms(), _terms(insurance="Unimed")) == {"insurance"}
    assert money_fields_changed(
        _terms(), _terms(service="Avaliação", professional_id=doctor)
    ) == {"service", "doctor"}


def test_service_names_compare_like_the_catalog():
    assert money_fields_changed(_terms(service="Avaliação"), _terms(service=" avaliacao ")) == set()


def test_ids_compare_as_uuid_or_text_and_empty_insurance_is_none():
    doctor = uuid4()
    assert money_fields_changed(
        _terms(professional_id=doctor, unit_id=None, insurance=""),
        _terms(professional_id=str(doctor), unit_id="", insurance="  "),
    ) == set()
    assert money_fields_changed(_terms(insurance="Unimed "), _terms(insurance="unimed")) == set()


def test_terms_of_an_appointment_and_of_an_edit():
    doctor, unit, other_unit = uuid4(), uuid4(), uuid4()
    appointment = SimpleNamespace(
        appointment_type="Consulta", professional_id=doctor, unit_id=unit, insurance="Unimed"
    )
    before = MoneyTerms.of_appointment(appointment)
    assert before == MoneyTerms(
        service="Consulta", professional_id=doctor, unit_id=unit, insurance="Unimed"
    )
    # Keys absent from the R6 edit dict are unchanged; a key present with None clears it.
    assert money_fields_changed(before, before.with_edit({"start_at": "x"})) == set()
    assert money_fields_changed(before, before.with_edit({"unit_id": None})) == {"unit"}
    assert money_fields_changed(
        before, before.with_edit({"appointment_type": "Retorno", "unit_id": other_unit})
    ) == {"service", "unit"}
    assert money_fields_changed(before, before.with_edit({"insurance": None})) == {"insurance"}
