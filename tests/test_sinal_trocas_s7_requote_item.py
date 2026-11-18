"""TASK-085 R / S7a - the clinic's item when the NEW sinal Pix could not be created (D-R6).

S2 reused `service_diff_collect_due` ("O serviço ... foi trocado por um mais caro ... a troca já
valeu"), which does not describe this case: an UNPAID sinal whose amount changed with an edit, the
old Pix voided and the new one refused. It gets its own reason (`sinal_requote_failed`, <= 32
chars) and its own sentence for the clinic.
"""

from secretaria.models.payment_attention_item import (
    ATTENTION_REASONS,
    REASON_SERVICE_DIFF_COLLECT_DUE,
    REASON_SINAL_REQUOTE_FAILED,
)
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.payments.attention_view import REASON_TEXT, reason_text
from secretaria.services.payments.provider import ProviderError
from tests.test_sinal_trocas_s2_requote import (  # noqa: F401 - fixtures (autouse included)
    _items,
    _voided,
    _whatsapp,
    db,
    provider,
)


def test_the_reason_fits_the_column_and_is_known():
    assert len(REASON_SINAL_REQUOTE_FAILED) <= 32
    assert REASON_SINAL_REQUOTE_FAILED in ATTENTION_REASONS
    assert REASON_SINAL_REQUOTE_FAILED in REASON_TEXT


def test_the_clinic_reads_what_happened_and_the_amount_to_collect():
    text = reason_text(REASON_SINAL_REQUOTE_FAILED, None, 9000)
    assert "sinal" in text and "R$ 90,00" in text
    assert "Pix antigo foi cancelado" in text
    assert "mais caro" not in text and "troca já valeu" not in text


async def test_a_refused_new_sinal_opens_the_dedicated_item(db, provider):  # noqa: F811
    tenant, patient, appointment, old = await _voided(db, provider)
    provider.create_error = ProviderError("create_failed", status_code=500)

    created = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=old.amount_cents
    )

    assert created is None
    [item] = await _items(db)
    assert item.reason == REASON_SINAL_REQUOTE_FAILED
    assert item.reason != REASON_SERVICE_DIFF_COLLECT_DUE
    assert (item.amount_cents, item.patient_id, item.deposit_id) == (9000, patient.id, None)


async def test_a_payer_question_that_cannot_be_asked_opens_the_dedicated_item(db, provider):  # noqa: F811
    tenant, _patient, appointment, old = await _voided(db, provider)
    provider.create_error = ProviderError("payer_document_required", status_code=400)

    await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=old.amount_cents
    )

    assert [i.reason for i in await _items(db)] == [REASON_SINAL_REQUOTE_FAILED]
