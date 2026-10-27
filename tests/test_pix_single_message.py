"""One message per Pix (owner, 2026-10-27): the explanation and the copia-e-cola travel together.

The code is the LAST lines of the text, fenced with triple backticks - WhatsApp draws it as
monospace, the Portal front turns the fence into a tappable copy box. Pure copy checks here; the
end-to-end sends (WhatsApp sender, Portal rows, clinic notice) are asserted in
test_before_booking_flow / test_payment_notify / test_hub_edit_appointment /
test_appointment_edit_paid_service.
"""

from types import SimpleNamespace

from secretaria.services.payments import deposit_lifecycle, service_change
from secretaria.services.payments.pix_message import pix_code_block, pix_code_tail
from secretaria.workers.shared.service_change_actions import service_change_bubbles

CODE = "00020126580014br.gov.bcb.pix0136abc6304ABCD"
FENCED = f"```{CODE}```"


def test_the_block_is_the_code_between_triple_backticks():
    assert pix_code_block(CODE) == FENCED
    assert pix_code_tail(CODE) == ["Pix copia e cola:", FENCED]
    assert pix_code_tail(None) == [] and pix_code_tail("  ") == []


def test_a_backtick_in_the_code_can_never_break_the_fence():
    assert pix_code_block("AB```CD") == "```ABCD```"


def test_initial_sinal_request_is_one_text_ending_with_the_code():
    text = deposit_lifecycle.before_request_text(
        "Clinica",
        service="Consulta",
        when_text="05/11 às 09:00",
        amount_cents=6000,
        minutes=30,
        until_text="10:30",
        payment_note="nota do convênio",
        copy_paste=CODE,
    )
    assert "R$ 60,00" in text and "nota do convênio" in text
    assert text.rstrip().endswith(FENCED)
    assert text.count(CODE) == 1


def test_initial_sinal_request_without_a_code_has_no_fence():
    text = deposit_lifecycle.before_request_text(
        "Clinica", service="Consulta", when_text="x", amount_cents=6000, minutes=30, until_text="y"
    )
    assert "```" not in text


def test_after_mode_sinal_request_ends_with_the_code():
    tenant = SimpleNamespace(clinic_name="Clinica")
    appointment = SimpleNamespace(appointment_type="Consulta")
    text = deposit_lifecycle._deposit_request_text(tenant, appointment, 6000, CODE, "nota")
    assert text.rstrip().endswith(FENCED) and text.count(CODE) == 1


def test_difference_pix_text_is_one_text_for_patient_and_clinic_requests():
    for source in (None, "hub"):
        text = service_change.difference_pix_text("Avaliação", 3000, CODE, 30, source=source)
        assert text.rstrip().endswith(FENCED) and text.count(CODE) == 1
        assert "só muda depois" in text


def test_the_patient_reply_of_a_service_change_is_one_bubble_with_the_code():
    outcome = SimpleNamespace(
        kind="awaiting_payment", diff_cents=3000, pix_copy_paste=CODE, adjustment_id=None
    )
    tenant = SimpleNamespace(deposit_hold_ttl_minutes=30)
    menu = SimpleNamespace(body="menu")
    bubbles = service_change_bubbles(
        [SimpleNamespace(body="recap"), menu], outcome, new_service="Avaliação", tenant=tenant
    )
    assert len(bubbles) == 2  # the Pix message + the menu that was already there
    assert bubbles[0].body.rstrip().endswith(FENCED)
    assert not any(getattr(b, "body", None) == CODE for b in bubbles)
